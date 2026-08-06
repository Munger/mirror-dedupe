## @file mdnode.py
##
## @brief Mirror-dedupe specific ``Node`` subclass combining the generic
##        tree primitives from ``lib.node_x`` with mirror-specific content
##        operations (download, verify, hardlink).
##
## ``MDNode`` inherits from ``Node`` (thread-safe dict-backed tree node),
## ``StreamMixin`` (lazy child discovery), and ``Serialisable``
## (snapshot/restore/clone).  It adds:
##
##   * ``_repo_vars`` / ``_cache`` - runtime wiring for the sync pipeline
##   * ``probe_url()`` - HTTP reachability check
##   * ``checksum`` - SHA-256 hash accessor
##   * ``_open_binary()`` / ``_iter_lines()`` - file and decompression I/O
##   * ``fetch()``, ``read()``, ``sync()`` - staged download pipeline
##   * ``on_parse()``, ``parse()``, ``recurse()`` - tree-building protocol
##   * ``stream()`` - lazy child discovery (virtual, overrides
##     ``StreamMixin`` with a ``data`` parameter)
##
## Module-level helpers ``_pool_path()``, ``_sync_link()``,
## ``_compute_hash()``, and ``_file_to_dest()`` manage the
## content-addressed pool layout, hash verification, and file install.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


import hashlib
import io
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, cast

from ..lib.exceptions import ExceptionMsg
from ..lib.gpg import verify_release, verify_inrelease
from ..lib import fmt_size, LOG_LABEL_W, LOG
from ..lib.http_download import HTTPFetch, HTTPDownload, HTTPGet
from ..lib.subproc import kill_active_subprocesses
from ..lib.log import log
from ..lib.node_x import Node, NodeList, Serialisable, StreamMixin
from ..inventory import FileRecord
from ..repo_vars import RepoVars


# ---------------------------------------------------------------------------
# Pool helpers (module-level so they can be used by scripts and tests)
# ---------------------------------------------------------------------------
# The pool path builder, hardlink installer, and hash helper are
# module-level rather than class methods so that external code (tests,
# scripts/sync-hashes.sh, the pool-sweep coordinator) can use them
# without instantiating a node.
#
# Download coordination (formerly _staging_locks) is now handled by the
# FileRecord.lock on each pool inventory entry — no separate lock map needed.


def _pool_path(pool_root: str, hash_val: str) -> Path:
    ## @brief Build the deterministic pool path for a SHA-256 hash.
    ##
    ## Layout: ``<pool_root>/by-hash/SHA256/<ab>/<cd>/<full_hash>``
    ## where ``ab`` and ``cd`` are the first two and second two hex
    ## characters.  This two-level fan-out keeps directory sizes
    ## manageable (max 256 dirs at each level, ~65K leaf dirs).
    ##
    ## @param pool_root  Root directory of the content-addressed pool.
    ## @param hash_val   64-char SHA-256 hex digest.
    ## @return Absolute ``Path`` to the pool entry.

    return (
        Path(pool_root)
        / "by-hash"
        / "SHA256"
        / hash_val[:2]
        / hash_val[2:4]
        / hash_val
    )


def _compute_hash(path: Path) -> str:
    ## @brief Compute the SHA-256 hex digest of a file.
    ##
    ## Reads in 1 MiB chunks to bound peak memory on large files.
    ##
    ## @param path  File to hash.
    ## @return 64-character hex digest string.

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_to_dest(
    src: Path,
    dest: Path,
    *,
    hash_val: Optional[str] = None,
    move: bool = True,
) -> None:
    ## @brief Unified file-install primitive for the content-addressed pool.
    ##
    ## All operations that create a pool entry or back-link a file into the
    ## pool go through this function to guarantee:
    ##
    ##   1. When *hash_val* is provided, the source is hashed **before** any
    ##      filesystem change.  A mismatch raises ``ExceptionMsg`` immediately
    ##      so no corrupt data ever reaches the pool.
    ##   2. The destination parent directory is created if absent.
    ##   3. The file operation is atomic: ``os.replace`` (move) or
    ##      ``os.link`` (hardlink), never a copy.
    ##
    ## Usage patterns::
    ##
    ##   # staging → pool  (after download, hash already verified by HTTPDownload)
    ##   _file_to_dest(staging_path, pool_path, move=True)
    ##
    ##   # pool → repo  (hardlink install, no re-hash needed)
    ##   _file_to_dest(pool_path, repo_dest, move=False)
    ##
    ##   # repo → pool  (back-link recovery, must re-verify)
    ##   _file_to_dest(repo_dest, pool_path, hash_val=hv, move=False)
    ##
    ## @param src       Source file path.
    ## @param dest      Destination path (must not already exist for ``os.link``).
    ## @param hash_val  Expected SHA-256 hex digest.  When supplied the source
    ##                  is hashed before any write; mismatch raises ExceptionMsg.
    ## @param move      ``True`` (default) → ``os.replace(src, dest)``;
    ##                  ``False`` → ``os.link(src, dest)``.
    ## @raise ExceptionMsg  When *hash_val* is given and does not match.

    if hash_val is not None:
        actual = _compute_hash(src)
        if actual != hash_val:
            raise ExceptionMsg(
                0,
                f"Hash mismatch for {src.name}: "
                f"expected {hash_val}, got {actual}",
            )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if move:
        os.replace(str(src), str(dest))
    else:
        os.link(str(src), str(dest))


def _sync_link(
    pool_path: Path,
    dest: Path,
    rv: RepoVars,
    record: Optional[FileRecord] = None,
) -> None:
    ## @brief Hardlink a pool file into a repo destination directory.
    ##
    ## Creates the parent directory tree if needed, removes any existing
    ## file at *dest*, and links *pool_path* -> *dest*.  The pool inode is
    ## already known from the FileRecord so no post-link stat is needed.
    ##
    ## @param pool_path  Existing file in the content-addressed pool.
    ## @param dest       Destination path under the repo root.
    ## @param rv         ``RepoVars`` for this repo.
    ## @param record     Pool ``FileRecord`` to mark; None only when no
    ##                   inventory is available.

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    os.link(pool_path, dest)
    if record is not None and rv.repo_id >= 0:
        record.mark_linked(rv.repo_id)


# ============================================================================
# MDNode
# ============================================================================


class MDNode(Node, StreamMixin, Serialisable):
    ## @brief Concrete base for all mirror-dedupe schema nodes.
    ##
    ## Combines the generic tree primitives from ``lib.node_x`` with
    ## mirror-specific operations: HTTP probing, content synchronisation
    ## through a content-addressed pool, staged download with per-hash
    ## locking, and a tree-building protocol (``parse`` / ``recurse`` /
    ## ``stream``).
    ##
    ## Subclasses declare their structural children via ``_children``
    ## (inherited from ``Node``) and optionally override virtual methods
    ## ``on_parse()`` and ``stream()`` to implement their content
    ## discovery logic.

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Construct an MDNode and initialise mirror runtime state.
        ##
        ## Delegates payload initialisation to ``Node.__init__``, then
        ## sets ``_cache`` (for scan-mode HTTP caching) and
        ## ``_repo_vars`` (for sync-mode repo wiring) to ``None``.
        ## These use ``object.__setattr__`` because they are private
        ## instance attributes, not payload keys - the leading underscore
        ## in ``__setattr__`` triggers the ``object.__setattr__`` branch
        ## automatically in the inherited ``Node.__setattr__``, but we
        ## write them before calling that method for clarity.
        ##
        ## @param args    Optional single positional dict payload.
        ## @param kwargs  Optional keyword fields set via ``__setattr__``.
        ## @return None

        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_cache", None)
        object.__setattr__(self, "_repo_vars", None)

    def _validate_value(self, value: Any) -> None:
        ## @brief Relaxed validation that allows raw dicts in payloads.
        ##
        ## ``node_x.Node`` rejects raw ``dict`` values to enforce a clean
        ## tree contract where every structured value is a ``Node``
        ## subclass.  Mirror-dedupe stores configuration dicts
        ## (e.g. ``repo["params"]``) directly in the Node payload, so
        ## ``MDNode`` overrides to permit ``dict`` while still rejecting
        ## raw ``list`` (lists must use ``NodeList`` for thread safety
        ## and freeze propagation).
        ##
        ## @param value  The value to check.
        ## @return None
        ## @raise TypeError  If *value* is a plain list.

        # Node, NodeList, and dict are allowed.
        if isinstance(value, (Node, NodeList, dict)):
            return
        # Reject raw lists - use NodeList for thread safety and freeze.
        if isinstance(value, list):
            raise TypeError(
                "MDNode cannot contain plain lists. "
                "Use NodeList instead."
            )
        # Scalars and None are always safe.
        if value is None or isinstance(value, (str, int, float, bool, bytes)):
            return
        # Tuples are allowed but each element is validated recursively.
        if isinstance(value, tuple):
            for item in value:
                self._validate_value(item)
            return
        raise TypeError(
            f"MDNode cannot safely contain {type(value).__name__}."
        )

    # -- HTTP -----------------------------------------------------------------

    @classmethod
    def probe_url(cls, uri: str) -> Optional[bytes]:
        ## @brief Check whether a URL is reachable and return its content.
        ##
        ## Used during ``--test``, ``--scan``, and health checks to verify
        ## upstream connectivity without instantiating the full sync
        ## machinery.  Returns the response body so the caller can inspect
        ## it further if needed.
        ##
        ## Unlike ``HTTPFetch`` (retry, resume, hash validation), this is a
        ## lightweight single GET with ``--max-time 30`` - no HEAD probe,
        ## no retries, no temp files.
        ##
        ## @param uri  The URL to probe.
        ## @return The raw response bytes, or ``None`` if the URL is empty
        ##         or unreachable.

        if not uri:
            return None
        try:
            return HTTPGet(uri)
        except Exception:
            return None

    # -- Checksum -------------------------------------------------------------

    @property
    def checksum(self) -> str:
        ## @brief Return the SHA-256 checksum for this node.
        ##
        ## Checks ``self["hash"]`` - populated at construction for
        ## packages (from the Release file digest), or set by
        ## ``sync()`` after the first download for content that
        ## did not have a known hash (e.g. Release files on first
        ## sync).
        ##
        ## The hash is the authoritative deduplication key: two files
        ## with the same hash share one pool entry regardless of their
        ## URI or path.
        ##
        ## @return The hex digest string, or ``""`` when not available.

        return self.get("hash", "")

    # -- I/O helpers ----------------------------------------------------------

    def _open_binary(self, data: Optional[bytes] = None):
        ## @brief Return a binary file-like object for this node's content.
        ##
        ## Two modes:
        ##
        ##   * **Scan mode** (*data* provided): wraps *data* in
        ##     ``BytesIO`` - content is in memory from an HTTP fetch.
        ##   * **Sync mode** (*data* is ``None``): opens
        ##     ``repo_root / path`` in ``rb`` mode - content is on disk
        ##     after pool synchronisation.
        ##
        ## Callers are responsible for closing the returned handle.
        ##
        ## @param data  Optional bytes to read from (scan mode).
        ## @return A binary file-like object.

        if data is not None:
            # Scan mode: content already in memory from HTTPFetch.
            return io.BytesIO(data)
        if self._repo_vars is not None:
            # Sync mode: read from the hardlinked repo destination.
            return open(Path(self._repo_vars.repo_root) / self["path"], "rb")
        raise ExceptionMsg(0,
            "Cannot open file on disk: node has no _repo_vars "
            "and no data bytes were provided.",
        )

    def _iter_lines(self, data: Optional[bytes] = None):
        ## @brief Yield decoded text lines from this node's content.
        ##
        ## Handles ``.gz``, ``.xz``, ``.bz2``, and plain text
        ## transparently via the file extension in ``self["path"]``.
        ## Each yielded line has trailing ``\\n`` and ``\\r`` stripped.
        ##
        ## The decompression strategy is chosen per-extension:
        ##
        ##   * ``.gz`` - ``gzip.GzipFile`` wrapping the raw streamy
        ##   * ``.xz`` - ``lzma.LZMADecompressor`` with 64KB buffered
        ##     reads for streaming (avoids loading the full archive)
        ##   * ``.bz2`` - single-shot ``bz2.decompress`` (bzip2 is
        ##     not streamable; the whole file must be decompressed)
        ##   * Other - raw bytestream decoded line-by-line
        ##
        ## @param data  Optional bytes to read from (scan mode).
        ## @yield Decoded text lines.

        raw = self._open_binary(data)
        path: str = self.get("path", "")
        try:
            if path.endswith(".gz"):
                # gzip.GzipFile wraps a stream - random-access not needed.
                import gzip

                f = gzip.GzipFile(fileobj=raw)
                for line in f:
                    yield line.decode("utf-8", errors="replace").rstrip(
                        "\n\r"
                    )
            elif path.endswith(".xz"):
                # lzma.LZMADecompressor supports streaming: feed in chunks,
                # spit out completed lines from the decompressed buffer.
                import lzma

                decomp = lzma.LZMADecompressor()
                buf = b""
                while True:
                    chunk = raw.read(65536)
                    if not chunk:
                        break
                    buf += decomp.decompress(chunk)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        yield line.decode("utf-8", errors="replace").rstrip(
                            "\r"
                        )
                # Emit final partial line if the file does not end with \n.
                if buf:
                    yield buf.decode("utf-8", errors="replace").rstrip(
                        "\n\r"
                    )
            elif path.endswith(".bz2"):
                # bzip2 is not streamable - must decompress all at once.
                import bz2

                text = bz2.decompress(raw.read()).decode(
                    "utf-8", errors="replace"
                )
                for line in text.splitlines():
                    yield line.rstrip("\n\r")
            else:
                # Plain text - decode each readline() result.
                for line in raw:
                    yield line.decode("utf-8", errors="replace").rstrip(
                        "\n\r"
                    )
        finally:
            raw.close()

    # -- Streaming (virtual) --------------------------------------------------

    def stream(self, data: Optional[bytes] = None) -> Any:
        ## @brief Yield child nodes discovered by reading this node's
        ##        content.
        ##
        ## The default implementation is a no-op.  Subclasses override to
        ## parse their file format and yield child nodes as a side effect
        ## of materialising the subtree.
        ##
        ## This is the dynamic counterpart to ``_children`` (the static
        ## tree skeleton).  ``_children`` declares the known hierarchy
        ## at parse time; ``stream()`` discovers additional children
        ## after the node's content is available (e.g. packages parsed
        ## from a downloaded ``Packages.gz``).
        ##
        ## ``stream()`` is always a local read - never touches the
        ## network.  In scan mode *data* receives bytes from an HTTP
        ## fetch.  In sync mode *data* is ``None`` and the method reads
        ## from disk via ``_open_binary()``.
        ##
        ## The caller (``_sync_content()`` in ``Repo``) pushes yielded
        ## children onto the work stack so they are synchronised lazily,
        ## avoiding the need to materialise and retain the full child
        ## tree in memory.
        ##
        ## @param data  Optional bytes to parse (scan mode).
        ## @yield Child ``Node`` instances.
        ## @return An iterator over child nodes.

        return iter([])

    # -- Tree building --------------------------------------------------------

    def on_parse(
        self, *, config: Optional[Dict[str, Any]] = None
    ) -> None:
        ## @brief Populate this node's schema and create child nodes.
        ##
        ## Called by ``parse()``.  Subclasses override to populate their
        ## own payload fields and attach child nodes via the attributes
        ## declared in ``_children``.  The base implementation is a
        ## no-op.
        ##
        ## @param config  Optional configuration dict (network settings,
        ##                filters, etc.).
        ## @return None
        pass

    def parse(
        self, *, config: Optional[Dict[str, Any]] = None
    ) -> "MDNode":
        ## @brief Populate this node and recursively parse its children.
        ##
        ## Calls ``on_parse()`` to populate this node, then walks each
        ## attribute named in ``_children`` and calls ``parse()`` on
        ## each child node.  Children can be a ``NodeList`` of nodes or
        ## a single ``Node`` (e.g. a distribution's child Release).
        ##
        ## This is the primary entry point for tree construction.  It is
        ## called once during scan or sync initialisation.  For
        ## content-derived children (e.g. packages parsed from an
        ## archive), the subclass must override ``stream()`` in
        ## addition to ``on_parse()``.
        ##
        ## @param config  Optional configuration dict passed down to
        ##                all ``on_parse()`` calls.
        ## @return This node (for chaining).

        self.on_parse(config=config)
        self.recurse(config=config)
        return self

    def recurse(
        self, *, config: Optional[Dict[str, Any]] = None
    ) -> "MDNode":
        ## @brief Walk declared children and call ``parse()`` on each.
        ##
        ## Skips this node's ``on_parse()`` - use when the tree already
        ## has its structural children created (e.g. sync mode builds
        ## distributions from config, then recurses to populate them).
        ##
        ## @param config  Optional configuration dict passed down to
        ##                all ``on_parse()`` calls on children.
        ## @return This node (for chaining).

        for attr in type(self)._children:
            children = getattr(self, attr, None)
            if children is None:
                continue
            if isinstance(children, NodeList):
                for child in children:
                    child.parse(config=config)
            elif isinstance(children, Node):
                children.parse(config=config)
        return self

    # -- Content pipeline -----------------------------------------------------

    def fetch(
        self,
        uri: str,
        *,
        config: Optional[Dict[str, Any]] = None,
    ) -> Optional[bytes]:
        ## @brief Fetch *uri* content into memory.
        ##
        ## Three paths, checked in order:
        ##
        ##   1. **Cache hit** - if ``_cache`` is set (from a previous
        ##      fetch in the same mode), return it immediately.
        ##   2. **Sync mode** - if ``_repo_vars.sync_mode`` is set,
        ##      redirect to ``self.read()`` which synchronises the file
        ##      through the pool and then reads it from disk.
        ##   3. **Scan mode** - perform a pure in-memory HTTP fetch and
        ##      cache the result in ``_cache`` for subsequent calls.
        ##
        ## @param uri     URL to fetch.
        ## @param config  Optional configuration dict forwarded to
        ##                ``read()`` in sync mode.
        ## @return The raw bytes, or ``None`` if *uri* is empty.

        if not uri:
            return None
        # Return cached result if available (avoids re-downloading when
        # parse() calls fetch() and later read() calls sync() -> read_bytes()).
        if self._cache is not None:
            return self._cache
        # Sync mode: go through the pool synchronisation path.
        if self._repo_vars is not None and self._repo_vars.sync_mode:
            return self.read(config=config)
        # Scan mode: HTTP directly into memory.
        data = HTTPFetch(uri, expected_hash=self.get("hash"))
        self._cache = data
        return data

    def read(
        self, *, config: Optional[Dict[str, Any]] = None
    ) -> Optional[bytes]:
        ## @brief Return the content of this node's file from disk.
        ##
        ## Calls ``sync()`` first to ensure the file is in the pool and
        ## hardlinked to the repo destination, then reads the bytes from
        ## ``repo_root / path``.
        ##
        ## This is the disk counterpart to ``fetch()`` - where
        ## ``fetch()`` handles both scan and sync modes, ``read()`` is
        ## the sync-mode half: ensure the file exists, then return its
        ## contents.
        ##
        ## @param config  Optional configuration dict for sync
        ##                parameters.
        ## @return The raw bytes, or ``None`` if no URI or path is set.

        if not self.get("uri") or not self.get("path"):
            return None
        self.sync(config=config)
        if self._repo_vars is not None:
            return (
                Path(self._repo_vars.repo_root) / self.get("path", "")
            ).read_bytes()
        return None

    def sync(
        self, *, config: Optional[Dict[str, Any]] = None,
        check: Optional[str] = None,
    ) -> List[Path]:
        ## @brief Synchronise this node's file from upstream into the
        ##        pool and repo.
        ##
        ## Every sync outcome is resolved by one executable decision
        ## table defined inline below (rows R1-R6).  The table is the
        ## single authority: predicates are computed lazily and memoised,
        ## a state matches exactly one row, and a state that matches no
        ## row raises loudly instead of silently picking a behaviour.
        ##
        ## Predicates:
        ##   ``pool``  content present in the pool.  The in-memory
        ##             inventory is authoritative and maintained as we go
        ##             (``add``/``register`` fire the moment a file is
        ##             promoted); the pool path is only stat()ed to
        ##             reconcile when the record has no inode yet (mid-run
        ##             promote window or an external pool repair).
        ##   ``dest``  the repo destination path exists.
        ##   ``cur``   one stat(): dest is a hardlink to the current pool
        ##             object for this content (i.e. not stale).
        ##   ``ok``    dest content hashes to the expected digest —
        ##             evaluated lazily, only on the back-link rows.
        ##
        ## Rows 1-3 are lock-free: they only read the immutable pool
        ## object and write this node's own dest path, so they run on the
        ## coordinator thread with no worker handoff (the caller's
        ## ``pool_inv.has()`` gate admits exactly this set) and no lock.
        ## Rows 4-6 mutate the pool and run under the per-content
        ## ``FileRecord`` lock with a re-check (double-checked locking)
        ## so a given hash is downloaded at most once even when many
        ## nodes share it.
        ##
        ## Safety invariant: dest is only ever removed either immediately
        ## followed by a link to already-verified pool content (R2) or
        ## after a full re-hash proves it wrong (R5).  No bare destructive
        ## unlink exists in this method.
        ##
        ## @param config  Optional configuration dict (carries suite,
        ##                architecture, component filters).
        ## @param check   GPG verification mode (``"gpg_release"`` /
        ##                ``"gpg_inrelease"``) or None.
        ## @return List of paths hardlinked into the repo (one-element in
        ##         normal operation; empty if no uri/path is set).

        uri = self.get("uri")
        path_val = self.get("path")
        if not uri or not path_val:
            return []

        rv = self._repo_vars
        if rv is None:
            raise ExceptionMsg(0,
                f"sync() called on {path_val} without _repo_vars.",
            )

        # Declare this path as wanted before any I/O.  Removes it from
        # the pre-sync stale set so that _sweep_stale() does not delete
        # a file we are about to create or confirm.  set.discard() is
        # GIL-atomic in CPython; no separate lock needed.
        rv.stale_paths.discard(path_val)

        dest = Path(rv.repo_root) / path_val
        hash_val = self.checksum

        # --- Inner helpers -----------------------------------------------
        # Defined here so they close over rv, dest, path_val, self, and
        # hash_val without passing them as arguments on every call.

        def _record(
            *, hit: int = 0, miss: int = 0, bytes_tx: int = 0
        ) -> None:
            ## @brief Record one sync outcome to the repo-level SyncStats.
            if rv.stats is not None:
                rv.stats.record(hit=hit, miss=miss, bytes_tx=bytes_tx)

        def _log_outcome(label: str, size: int = 0) -> None:
            ## @brief Emit a fixed-width outcome line to the sync log.
            LOG(label, fmt_size(size) if size else "", path_val)

        def _link_outcome(record: FileRecord, label: str = "Linked") -> List[Path]:
            ## @brief Hardlink the current pool object into the repo.
            ##
            ## Wraps ``_sync_link`` + hit accounting + outcome log.  The
            ## caller supplies the label (``"Linked"`` or ``"Linked*"``);
            ## the action is identical either way — only the log differs.
            ## @param record  Pool ``FileRecord`` to mark.
            ## @param label   Outcome label to log.
            ## @return ``[dest]``
            _sync_link(_pool_path(rv.pool_root, hash_val), dest, rv, record)
            _record(hit=1)
            _log_outcome(label, self.get("size") or 0)
            return [dest]

        def _promote(
            staging_file: str, hv: str, label: str, record: FileRecord
        ) -> List[Path]:
            ## @brief Atomically move a complete staging file into the pool
            ##        and hardlink it into the repo destination.
            ##
            ## Hash verification is the caller's responsibility — this function
            ## does not re-hash, because staging promotions are either verified
            ## by HTTPDownload or by an explicit _compute_hash check before the
            ## call.  Called while ``record.lock`` is held by the caller.
            ##
            ## For temp-keyed records (hash-less files such as Release), the
            ## real hash is registered as a second lookup key after promotion
            ## so future hash-based lookups resolve to the same FileRecord.
            ##
            ## @param staging_file  Path to the completed staging file.
            ## @param hv            SHA-256 hex digest of the file.
            ## @param label         Log label (``"Downloaded"`` or ``"Resumed"``).
            ## @param record        Pool ``FileRecord`` to update in place.
            ## @return ``[dest]``
            pp = _pool_path(rv.pool_root, hv)
            _file_to_dest(Path(staging_file), pp, move=True)
            ## Update the record before linking so any thread waiting on
            ## record.lock sees in_pool=True as soon as the lock is released.
            _pst = pp.stat()
            ino = _pst.st_ino
            sz = int(_pst.st_size)
            record.inode = ino
            record.size = sz
            record.hash = hv
            if rv.pool_inv is not None and record.temp:
                rv.pool_inv.register(hv, record)
            _sync_link(pp, dest, rv, record)
            self["size"] = sz
            _record(miss=1, bytes_tx=sz)
            _log_outcome(label, sz)
            return [dest]

        # ----------------------------------------------------------------
        # The decision table.  Rows are data: each row names the predicate
        # values that select it and the handler that executes it.  The
        # dispatcher walks the rows in order, evaluating predicates lazily
        # and memoising results, and hands control to the first matching
        # row.  Adding or changing behaviour means editing a row here —
        # there is no decision code outside the table.
        # ----------------------------------------------------------------
        #   R1  pool=1 dest=1 cur=1  -> UNCHANGED
        #   R2  pool=1 dest=1 cur=0  -> LINK      (stale dest relinked)
        #   R3  pool=1 dest=0        -> LINK
        #   R4  pool=0 dest=1 ok=1   -> BACK_LINK (repo file re-pooled)
        #   R5  pool=0 dest=1 ok=0   -> CORRUPT, then DOWNLOAD
        #   R6  pool=0 dest=0        -> DOWNLOAD
        # ----------------------------------------------------------------

        class _Row:
            __slots__ = ("name", "spec", "handler")

            def __init__(self, name: str, spec: tuple, handler) -> None:
                ## @brief One decision-table row.
                ## @param name     Row identifier (R1-R6).
                ## @param spec     Tuple of ``(predicate, expected)`` pairs;
                ##                 predicates not listed are don't-care.
                ## @param handler  Callable invoked for this row; returns
                ##                 the ``List[Path]`` result of ``sync()``.
                self.name = name
                self.spec = spec
                self.handler = handler

        _ctx: Dict[str, Any] = {"record": None}

        def _p_pool() -> bool:
            _rec = _ctx["record"]
            return _rec is not None and _rec.in_pool

        def _stat_dest() -> Optional[Any]:
            ## @brief One cached stat() answers both the ``dest`` and ``cur``
            ##        predicates — the pool inode is already in the inventory
            ##        record, so no pool file stat is ever needed.
            ## @return ``os.stat_result`` or None when dest does not exist.
            if "r" not in _ctx:
                try:
                    _ctx["r"] = dest.stat()
                except OSError:
                    _ctx["r"] = None
            return _ctx["r"]

        def _p_dest() -> bool:
            return _stat_dest() is not None

        def _p_current() -> bool:
            _r = _stat_dest()
            _rec = _ctx["record"]
            if _r is None or _rec is None:
                return False
            return _r.st_ino == _rec.inode

        def _p_hash_ok() -> bool:
            try:
                return _compute_hash(dest) == hash_val
            except OSError:
                return False

        _PREDS: Dict[str, Any] = {
            "pool": _p_pool,
            "dest": _p_dest,
            "cur": _p_current,
            "ok": _p_hash_ok,
        }

        def _dispatch(rows: tuple) -> Optional[_Row]:
            ## @brief Evaluate *rows* in order and return the first match.
            ##
            ## Predicates are evaluated lazily per row and memoised, so the
            ## expensive re-hash (``ok``) is never computed unless a row
            ## that references it is reached.
            _cache: Dict[str, Any] = {}

            def _val(name: str) -> Any:
                if name not in _cache:
                    _cache[name] = _PREDS[name]()
                return _cache[name]

            for _row in rows:
                if all(_val(n) == e for n, e in _row.spec):
                    return _row
            return None

        def _run(_row: Optional[_Row]) -> List[Path]:
            ## @brief Execute the matching row, or raise if none matched.
            ##
            ## The table is exhaustive for hash-known files; a no-match here
            ## means the table was edited wrongly, so fail loudly rather than
            ## silently misbehaving.
            if _row is None:
                raise RuntimeError(
                    f"sync decision table matched no row for {path_val} "
                    f"(hash={hash_val[:16] or '(none)'}, pool={_p_pool()}, "
                    f"dest={_p_dest()}, cur={_p_current()}, "
                    f"hash_ok={_p_hash_ok()})",
                )
            return _row.handler()

        def _row_unchanged() -> List[Path]:
            _rec = _ctx["record"]
            _rec.mark_linked(rv.repo_id)
            _record(hit=1)
            _log_outcome("Unchanged", self.get("size") or 0)
            return [dest]

        def _row_link() -> List[Path]:
            ## Rows R2 and R3 share one action: link the verified pool
            ## object into the repo.  The label distinguishes a stale
            ## repo inventory (``Linked*``) from a clean link (``Linked``).
            _rec = _ctx["record"]
            _stale = bool(_rec.repo_mask & (1 << rv.repo_id)) if rv.repo_id >= 0 else False
            return _link_outcome(_rec, "Linked*" if _stale else "Linked")

        def _row_back_link() -> List[Path]:
            ## Row R4: pool lost but the repo file is verified (ok=1), so
            ## re-pool it.  The hash was already checked by the table under
            ## the lock, so no re-hash is needed here.
            _rec = _ctx["record"]
            _pp = _pool_path(rv.pool_root, hash_val)
            try:
                _file_to_dest(dest, _pp, move=False)
            except FileExistsError:
                pass  # concurrent back-link by another thread
            _pst = _pp.stat()
            _rec.inode = _pst.st_ino
            _rec.size = int(_pst.st_size)
            _rec.hash = hash_val
            if rv.pool_inv is not None:
                rv.pool_inv.add(hash_val, _pst.st_ino, int(_pst.st_size))
            _rec.mark_linked(rv.repo_id)
            _record(hit=1)
            _log_outcome("Pooled", self.get("size") or 0)
            return [dest]

        def _row_corrupt() -> List[Path]:
            ## Row R5: dest has been re-hashed and proven wrong (ok=0), so
            ## removing it before re-downloading satisfies the invariant.
            dest.unlink(missing_ok=True)
            return _row_download()

        def _row_download() -> List[Path]:
            ## Rows R5 and R6 share one action: fetch from upstream via
            ## staged download.  A complete staging file from a prior
            ## interrupted sync is promoted directly; partial files are
            ## left in place for curl to resume via -C -.
            tmp = str(staging_dir / staging_key)
            tmp_path = staging_dir / staging_key
            _partial = tmp_path.exists() and tmp_path.stat().st_size > 0

            def _check_gpg() -> None:
                if not (check and rv and rv.gpg_keyring_path):
                    return
                keyring = Path(rv.gpg_keyring_path)
                label = self.get("path", "")

                if check == 'gpg_inrelease':
                    # InRelease is a clearsigned document — gpgv verifies it
                    # directly from the staging file; no separate sig fetch needed.
                    ok = verify_inrelease(Path(tmp), keyring,
                                         connect_timeout=rv.connect_timeout,
                                         label=label)
                else:
                    # Release: use Release.gpg from pool if already synced,
                    # otherwise fetch it from upstream.
                    path_val = self.get("path", "")
                    local_sig = (
                        Path(rv.pool_root) / (path_val + ".gpg")
                        if rv.pool_root and path_val else None
                    )
                    sig_path = local_sig if (local_sig and local_sig.exists()) else None
                    ok = verify_release(
                        Path(tmp),
                        uri + ".gpg",
                        keyring,
                        connect_timeout=rv.connect_timeout,
                        inrelease_url=uri.rsplit("/", 1)[0] + "/InRelease",
                        label=label,
                        sig_path=sig_path,
                    )

                if not ok:
                    if rv.stats is not None:
                        rv.stats.add_gpg_failure()
                    raise ExceptionMsg(
                        0,
                        f"GPG verification failed for {self.get('path', uri)} — "
                        "repo may be compromised",
                    )

            try:
                if hash_val and _partial:
                    if _compute_hash(tmp_path) == hash_val:
                        _check_gpg()
                        return _promote(tmp, hash_val, "Resumed", record)

                actual_hash = HTTPDownload(uri, tmp, expected_hash=hash_val)
                # For nodes without a prior known hash (e.g. a Release file on
                # first sync) the downloaded hash is now authoritative.
                self["hash"] = actual_hash
                hv = hash_val or actual_hash
                _check_gpg()
                return _promote(tmp, hv, "Resumed" if _partial else "Downloaded", record)
            except ExceptionMsg:
                if self.get("optional"):
                    return []
                raise

        _FAST_ROWS = (
            _Row("R1", (("pool", 1), ("dest", 1), ("cur", 1)), _row_unchanged),
            _Row("R2", (("pool", 1), ("dest", 1), ("cur", 0)), _row_link),
            _Row("R3", (("pool", 1), ("dest", 0)), _row_link),
        )
        _ALL_ROWS = _FAST_ROWS + (
            _Row("R4", (("pool", 0), ("dest", 1), ("ok", 1)), _row_back_link),
            _Row("R5", (("pool", 0), ("dest", 1), ("ok", 0)), _row_corrupt),
            _Row("R6", (("pool", 0), ("dest", 0)), _row_download),
        )

        # ----------------------------------------------------------------
        # Lock-free rows (1-3) -- inventory fast path
        # ----------------------------------------------------------------
        # Only hash-known nodes reach this block: the coordinator fast path
        # (repo._sync_content) gates on pool_inv.has() == inode > 0, which is
        # exactly pool=1 here.  These rows read only the immutable pool
        # object and write this node's own dest path, so they run on the
        # coordinator thread with no worker handoff and no lock.
        if hash_val and rv.pool_inv is not None:
            _ctx["record"] = rv.pool_inv.get_record(hash_val)
            _row = _dispatch(_FAST_ROWS)
            if _row is not None:
                return _row.handler()

        # ----------------------------------------------------------------
        # Locked rows (4-6) -- FileRecord lock + fallbacks
        # ----------------------------------------------------------------
        # Acquire the per-content lock from the pool FileRecord so that
        # concurrent threads downloading the same content race only once.
        # inventory.get() returns a pre-locked record; the lock is held
        # for the duration of the decision and released in the finally block.
        staging_dir = Path(rv.pool_root) / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_key = hash_val or hashlib.sha256(uri.encode()).hexdigest()

        if rv.pool_inv is not None:
            record = rv.pool_inv.get(staging_key, is_temp=not bool(hash_val))
        else:
            record = FileRecord(temp=not bool(hash_val))
            record.lock.acquire()
        try:
            # Temp-keyed file (e.g. Release, first sync): outside the hash
            # table.  A peer thread may have promoted this URI while we
            # waited for the lock; a valid inode means the pool object
            # exists, so link to it directly without re-downloading.
            if not hash_val and record.in_pool:
                pool_path = _pool_path(rv.pool_root, record.hash or "")
                _sync_link(pool_path, dest, rv, record)
                _record(hit=1)
                _log_outcome("Linked", record.size)
                return [dest]

            # Re-resolve pool presence under the lock (double-checked
            # locking: another thread may have completed this download while
            # we waited).  The in-memory inventory is authoritative; a disk
            # stat reconciles the mid-run promote window and any external
            # pool repair.
            if hash_val and not record.in_pool:
                _pp = _pool_path(rv.pool_root, hash_val)
                try:
                    _pst = _pp.stat()
                except OSError:
                    _pst = None
                if _pst is not None:
                    record.inode = _pst.st_ino
                    record.size = int(_pst.st_size)
                    record.hash = hash_val
                    if rv.pool_inv is not None:
                        rv.pool_inv.add(hash_val, _pst.st_ino, int(_pst.st_size))

            # Drop the fast-path dest stat cache: a peer thread may have
            # linked dest while we waited for the record lock, so the locked
            # dispatch must re-stat.
            _ctx.pop("r", None)
            _ctx["record"] = record
            return _run(_dispatch(_ALL_ROWS))
        finally:
            record.lock.release()

    def _tree_iter(self) -> "Iterable[MDNode]":
        yield from cast("Iterable[MDNode]", super()._tree_iter())

    def abort(self) -> None:
        ## @brief Signal an immediate abort of this repo's sync.
        ##
        ## Sets the per-repo ``abort_event`` so the coordinator stops
        ## submitting new work on its next iteration, then kills all
        ## curl subprocesses belonging to this repo so in-flight worker
        ## downloads return immediately rather than running to completion.
        ##
        ## This method is idempotent: calling it more than once (e.g. from
        ## concurrent workers hitting the same failure) is safe -
        ## ``Event.set()`` and killing already-dead processes are both
        ## no-ops.
        ##
        ## After calling ``abort()``, raise ``RepoAbortError(reason)`` so
        ## the calling context (``on_parse()``, ``stream()``, etc.) exits
        ## immediately rather than waiting for the coordinator to detect
        ## the event on its next iteration.
        ##
        ## @return None
        rv = self._repo_vars
        if rv is None:
            return
        rv.abort_event.set()
        from ..lib.subproc import kill_repo_subprocesses
        kill_repo_subprocesses(rv.repo_name)


__all__ = [
    "MDNode",
    "_pool_path",
    "_sync_link",
    "_compute_hash",
    "_file_to_dest",
]
