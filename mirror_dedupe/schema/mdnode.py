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
        self, *, config: Optional[Dict[str, Any]] = None, check: Optional[str] = None
    ) -> List[Path]:
        ## @brief Synchronise this node's file from upstream into the
        ##        pool and repo.
        ##
        ## This is the core download + verify + hardlink primitive.
        ## It uses a three-phase strategy:
        ##
        ##   **Phase 1 (inventory fast path):** if the hash is known
        ##   and the destination exists, skip all I/O.  If the pool has
        ##   it but the repo does not, hardlink from the pool directly.
        ##
        ##   **Phase 2 (staging lock + fallbacks):** acquire a per-hash
        ##   lock so concurrent threads race only once.  Inside the lock,
        ##   recheck inventories (double-checked locking), fall back to
        ##   stat() if the pool file exists on disk but was not indexed,
        ##   and finally attempt back-link recovery if the repo file
        ##   exists on disk and its hash is correct (pool was damaged but
        ##   repo is intact — no network required).
        ##
        ##   **Phase 3 (staging promotion or download):** if a complete
        ##   staging file exists from a prior interrupted sync, promote
        ##   it directly.  Otherwise download to staging, verify the
        ##   SHA-256 hash, atomically ``os.replace`` into the pool, and
        ##   hardlink into the repo.  Partial staging files survive
        ##   failure so curl resumes them on the next attempt.
        ##
        ## Outcome labels logged on every return path:
        ##   ``Unchanged``  — inventory hit, no I/O
        ##   ``Linked``     — pool hit, repo hardlink created
        ##   ``Linked*``    — pool hit, repo inventory was stale
        ##   ``Unchanged*`` — pool/repo files confirmed via stat
        ##   ``Recovered``  — repo file back-linked into damaged pool
        ##   ``Resumed``    — staging file found (complete or partial via curl -C -)
        ##   ``Downloaded`` — fresh download, no prior staging file
        ##
        ## @param config  Optional configuration dict (carries suite,
        ##                architecture, component filters).
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

        # ---------------------------------------------------------
        # Phase 1 -- Inventory fast path (no disk I/O beyond link)
        # ---------------------------------------------------------
        # Common case on subsequent syncs: the pool FileRecord is warm and
        # the file is already on disk in the correct state.
        if hash_val:
            _record_p1 = rv.pool_inv.get_record(hash_val) if rv.pool_inv else None
            if _record_p1 is not None:
                _inv_has = rv.repo_id >= 0 and bool(_record_p1.repo_mask & (1 << rv.repo_id))
                _pool_has = _record_p1.in_pool

                if _inv_has and _pool_has:
                    if dest.exists():
                        # Startup scan confirmed the file exists in both repo
                        # and pool.  stale_paths.discard() already claimed it;
                        # skip the dest stat.
                        _record_p1.mark_linked(rv.repo_id)
                        _record(hit=1)
                        _log_outcome("Unchanged", self.get("size") or 0)
                        return [dest]
                    # Hash is known but this specific dest path was never
                    # linked (e.g. multiple paths share the same empty content
                    # — the per-hash repo bit is set by another path).
                    pool_path = _pool_path(rv.pool_root, hash_val)
                    _sync_link(pool_path, dest, rv, _record_p1)
                    _record(hit=1)
                    _log_outcome("Linked", self.get("size") or 0)
                    return [dest]

                if _inv_has:

                    # Repo bit set but pool inode absent.  The pool entry may
                    # have been deleted externally.  Check disk and repair.
                    pool_path = _pool_path(rv.pool_root, hash_val)
                    if pool_path.exists():
                        _pst = pool_path.stat()
                        if rv.pool_inv is not None:
                            rv.pool_inv.add(hash_val, _pst.st_ino, _pst.st_size)
                        _record_p1.mark_linked(rv.repo_id)
                        _record(hit=1)
                        _log_outcome("Unchanged", self.get("size") or 0)
                        return [dest]

                    # Pool file is gone but repo hardlink survives.  Verify the
                    # repo file's hash and back-link it into the pool to heal the
                    # damage without any network I/O.
                    _corrupt = False
                    pool_path = _pool_path(rv.pool_root, hash_val)
                    try:
                        _file_to_dest(dest, pool_path, hash_val=hash_val, move=False)
                    except FileExistsError:
                        # Another thread back-linked concurrently — pool is intact.
                        pass
                    except ExceptionMsg:
                        # Hash mismatch: repo file is corrupt.  Remove it so the
                        # Phase 2 double-check does not return a stale "Unchanged".
                        dest.unlink(missing_ok=True)
                        _corrupt = True
                    if not _corrupt and pool_path.exists():
                        _pst = pool_path.stat()
                        if rv.pool_inv is not None:
                            rv.pool_inv.add(hash_val, _pst.st_ino, _pst.st_size)
                        _record_p1.mark_linked(rv.repo_id)
                        _record(hit=1)
                        _log_outcome("Recovered", self.get("size") or 0)
                        return [dest]
                    # dest was corrupt — fall through to Phase 2 for re-download.

                # Pool has it but the repo hardlink is absent (or bit was stale).
                if _pool_has:
                    pool_path = _pool_path(rv.pool_root, hash_val)
                    _sync_link(pool_path, dest, rv, _record_p1)
                    _record(hit=1)
                    _log_outcome(
                        "Linked*" if _inv_has else "Linked",
                        self.get("size") or 0,
                    )
                    return [dest]

        # ---------------------------------------------------------
        # Phase 2 -- FileRecord lock + fallbacks
        # ---------------------------------------------------------
        # Acquire the per-content lock from the pool FileRecord so that
        # concurrent threads downloading the same content race only once.
        # inventory.get() returns a pre-locked record; the lock is held
        # for the duration of the download and released in the finally block.
        staging_dir = Path(rv.pool_root) / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_key = hash_val or hashlib.sha256(uri.encode()).hexdigest()

        if rv.pool_inv is not None:
            record = rv.pool_inv.get(staging_key, is_temp=not bool(hash_val))
        else:
            record = FileRecord(temp=not bool(hash_val))
            record.lock.acquire()
        try:
            if not hash_val and record.inode:
                # Temp-keyed file (e.g. Release): B acquired the lock after
                # A already promoted the file.  A valid inode means the pool
                # file exists; link to it directly without re-downloading.
                pool_path = _pool_path(rv.pool_root, record.hash or "")
                _sync_link(pool_path, dest, rv, record)
                _record(hit=1)
                _log_outcome("Linked", record.size)
                return [dest]

            if hash_val:
                # Double-checked locking: another thread may have completed
                # this download while we waited for the lock.
                if record.in_pool:
                    pool_path = _pool_path(rv.pool_root, hash_val)
                    _inv_has = bool(record.repo_mask & (1 << rv.repo_id)) if rv.repo_id >= 0 else False
                    if _inv_has and dest.exists():
                        record.mark_linked(rv.repo_id)
                        _record(hit=1)
                        _log_outcome("Unchanged", self.get("size") or 0)
                        return [dest]
                    _sync_link(pool_path, dest, rv, record)
                    _record(hit=1)
                    _log_outcome(
                        "Linked*" if _inv_has else "Linked",
                        self.get("size") or 0,
                    )
                    return [dest]

                # Stat fallback: pool file exists on disk but was not yet
                # indexed (e.g. after a manual restore or a concurrent write
                # that bypassed the inventory).
                pool_path = _pool_path(rv.pool_root, hash_val)
                if pool_path.exists():
                    _pst = pool_path.stat()
                    record.inode = _pst.st_ino
                    record.size = _pst.st_size
                    record.hash = hash_val
                    if rv.pool_inv is not None:
                        rv.pool_inv.add(hash_val, _pst.st_ino, _pst.st_size)
                    if dest.exists() and dest.stat().st_ino == _pst.st_ino:
                        record.mark_linked(rv.repo_id)
                        _record(hit=1)
                        _log_outcome("Unchanged*", self.get("size") or 0)
                        return [dest]
                    _sync_link(pool_path, dest, rv, record)
                    _record(hit=1)
                    _log_outcome("Linked", self.get("size") or 0)
                    return [dest]

                # Back-link recovery: neither the inventory nor the FileRecord
                # has the hash, but the repo file exists on disk (e.g.
                # inventories rebuilt after a crash).  Verify and back-link.
                if dest.exists():
                    _corrupt = False
                    try:
                        _file_to_dest(
                            dest, pool_path, hash_val=hash_val, move=False
                        )
                    except FileExistsError:
                        pass  # concurrent back-link by another thread
                    except ExceptionMsg:
                        dest.unlink(missing_ok=True)
                        _corrupt = True
                    if not _corrupt and pool_path.exists():
                        _pst = pool_path.stat()
                        record.inode = _pst.st_ino
                        record.size = _pst.st_size
                        record.hash = hash_val
                        if rv.pool_inv is not None:
                            rv.pool_inv.add(hash_val, _pst.st_ino, _pst.st_size)
                        record.mark_linked(rv.repo_id)
                        _record(hit=1)
                        _log_outcome("Recovered", self.get("size") or 0)
                        return [dest]

            # -------------------------------------------------
            # Phase 3 -- Staging promotion or fresh download
            # -------------------------------------------------
            # If a complete staging file survives from a prior interrupted
            # sync, promote it directly.  Partial files are left in place
            # for curl to resume via -C -.
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
                hash_val = hash_val or actual_hash
                _check_gpg()
                return _promote(tmp, hash_val, "Resumed" if _partial else "Downloaded", record)
            except ExceptionMsg:
                if self.get("optional"):
                    return []
                raise

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
