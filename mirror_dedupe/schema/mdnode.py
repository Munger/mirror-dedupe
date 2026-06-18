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
## Module-level helpers ``_pool_path()`` and ``_sync_link()`` manage the
## content-addressed pool layout and hardlink installation.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import hashlib
import io
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..lib.exceptions import ExceptionMsg
from ..lib import fmt_size, LOG_LABEL_W
from ..lib.http_download import HTTPFetch, HTTPDownload, HTTPGet
from ..lib.subproc import kill_active_subprocesses
from ..lib.log import log
from ..lib.node_x import Node, NodeList, Serialisable, StreamMixin
from ..repo_vars import RepoVars


# ---------------------------------------------------------------------------
# Pool helpers (module-level so they can be used by scripts and tests)
# ---------------------------------------------------------------------------
# The staging lock map, pool path builder, and hardlink installer are
# module-level rather than class methods so that external code (tests,
# scripts/sync-hashes.sh, the pool-sweep coordinator) can use them
# without instantiating a node.

_staging_locks: dict[str, tuple[threading.Lock, int]] = {}
## @brief Per-hash staging locks with refcounts.
##
## Maps each staging key (hash or URI-derived) to a ``(Lock, refcount)``
## tuple so that concurrent threads downloading the same hash share one
## lock, and the entry is pruned when the last thread releases it.

_staging_locks_lock = threading.Lock()
## @brief Protects mutations to ``_staging_locks``.


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


def _sync_link(pool_path: Path, dest: Path, rv: RepoVars, hash_val: str) -> int:
    ## @brief Hardlink a pool file into a repo destination directory.
    ##
    ## Creates the parent directory tree if needed, removes any existing
    ## file at *dest*, links *pool_path* -> *dest*, and records the
    ## mapping in the repo inventory so future lookups are fast.
    ##
    ## @param pool_path  Existing file in the content-addressed pool.
    ## @param dest       Destination path under the repo root.
    ## @param rv         ``RepoVars`` containing the repo inventory.
    ## @param hash_val   SHA-256 hex digest of the content.
    ## @return The inode number of the linked file.

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    os.link(pool_path, dest)
    ino = dest.stat().st_ino
    if rv.inv is not None:
        rv.inv.add(hash_val, ino)
    return ino


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
    ) -> MDNode:
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
    ) -> MDNode:
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
                Path(self._repo_vars.repo_root) / self.get("path")
            ).read_bytes()
        return None

    def sync(
        self, *, config: Optional[Dict[str, Any]] = None
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
        ##   **Phase 2 (staging lock + stat fallback):** acquire a
        ##   per-hash lock so concurrent threads downloading the same
        ##   content race only once.  Recheck inventories after the lock
        ##   (double-checked locking).  Fall back to stat() on disk if
        ##   the inventories missed but the files exist.
        ##
        ##   **Phase 3 (genuine download):** download to a staging file
        ##   (hash-named in the pool's ``staging/`` directory), verify
        ##   the SHA-256 hash, atomically ``os.replace`` into the pool,
        ##   then hardlink into the repo.
        ##
        ## Logs each outcome with a descriptive label.
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
        if rv.inv is not None:
            rv.inv.stale_paths.discard(path_val)

        dest = Path(rv.repo_root) / path_val
        hash_val = self.checksum

        # _record is a closure rather than a method because it closes
        # over the local ``rv``, ``hash_val``, and ``self`` references.
        # This lets Phase 3 update hash_val (when the initial hash was
        # unknown) and still pass the correct value to the stats
        # accumulator without threading it through the return path.
        def _record(
            *, hit: int = 0, miss: int = 0, bytes_tx: int = 0
        ) -> None:
            ## @brief Record one sync outcome to the repo-level
            ##        SyncStats.
            ##
            ## Closes over ``rv``, ``self``, and ``hash_val`` (read at
            ## call time, so phase-3 updates to hash_val are reflected).
            ##
            ## @param hit      1 if served from pool, else 0.
            ## @param miss     1 if downloaded, else 0.
            ## @param bytes_tx Bytes transferred.
            ## @return None
            if rv.stats is not None:
                rv.stats.record(
                    hit=hit,
                    miss=miss,
                    bytes_tx=bytes_tx,
                    size=self.get("size", 0) or 0,
                    hash_val=hash_val,
                )

        # ---------------------------------------------------------
        # Phase 1 -- Inventory fast path (no disk beyond link)
        # ---------------------------------------------------------
        # If the hash is known and the repo inventory says it exists
        # on disk, skip all further work.  This is the common case
        # on subsequent syncs after the initial download.
        if hash_val:
            _inv_has = rv.inv is not None and rv.inv.has(hash_val)
            if _inv_has and dest.exists():
                _record(hit=1)
                log(
                    f"  {'Unchanged':<{LOG_LABEL_W}} "
                    f"{fmt_size(self.get('size') or 0):>7}  {path_val}"
                )
                return [dest]

            # Pool has the hash but repo does not: link from pool.
            if rv.pool_inv is not None and rv.pool_inv.has(hash_val):
                pool_path = _pool_path(rv.pool_root, hash_val)
                _sync_link(pool_path, dest, rv, hash_val)
                _record(hit=1)
                label = "Linked*" if _inv_has else "Linked"
                log(
                    f"  {label:<{LOG_LABEL_W}} "
                    f"{fmt_size(self.get('size') or 0):>7}  {path_val}"
                )
                return [dest]

        # ---------------------------------------------------------
        # Phase 2 -- Staging lock + stat fallback (cross-process)
        # ---------------------------------------------------------
        # Acquire a per-hash lock so that concurrent threads (from the
        # same repo's ThreadPoolExecutor) downloading the same content
        # race only once.  The first thread downloads; subsequent
        # threads recheck inventories after acquiring the lock and skip.
        staging_dir = Path(rv.pool_root) / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_key = (
            hash_val
            or hashlib.sha256(uri.encode()).hexdigest()
        )
        # Increment the refcount before acquiring the per-hash lock so
        # the entry cannot be pruned by a concurrent decref between
        # the get() and the acquisition.
        with _staging_locks_lock:
            lock, refs = _staging_locks.get(
                staging_key, (threading.Lock(), 0)
            )
            _staging_locks[staging_key] = (lock, refs + 1)
        with lock:
            try:
                if hash_val:
                    # Double-checked locking: another thread may have
                    # downloaded and linked this hash while we waited.
                    _inv_has = (
                        rv.inv is not None and rv.inv.has(hash_val)
                    )
                    if _inv_has and dest.exists():
                        _record(hit=1)
                        log(
                            f"  {'Unchanged':<{LOG_LABEL_W}} "
                            f"{fmt_size(self.get('size') or 0):>7}  "
                            f"{path_val}"
                        )
                        return [dest]

                    if (
                        rv.pool_inv is not None
                        and rv.pool_inv.has(hash_val)
                    ):
                        pool_path = _pool_path(rv.pool_root, hash_val)
                        _sync_link(pool_path, dest, rv, hash_val)
                        _record(hit=1)
                        label = "Linked*" if _inv_has else "Linked"
                        log(
                            f"  {label:<{LOG_LABEL_W}} "
                            f"{fmt_size(self.get('size') or 0):>7}  "
                            f"{path_val}"
                        )
                        return [dest]

                    # Stat fallback: the file exists on disk but
                    # neither inventory knew about it (e.g. after a
                    # manual restore or a concurrent process wrote it).
                    pool_path = _pool_path(rv.pool_root, hash_val)
                    if pool_path.exists():
                        # Re-populate the pool inventory from on-disk
                        # inode so future lookups are fast.
                        if rv.pool_inv is not None:
                            rv.pool_inv.add(
                                hash_val, pool_path.stat().st_ino
                            )
                        if (
                            dest.exists()
                            and dest.stat().st_ino
                            == pool_path.stat().st_ino
                        ):
                            # Already correctly linked - update repo
                            # inventory and return.
                            if rv.inv is not None:
                                rv.inv.add(
                                    hash_val, dest.stat().st_ino
                                )
                            _record(hit=1)
                            log(
                                f"  {'Unchanged*':<{LOG_LABEL_W}} "
                                f"{fmt_size(self.get('size') or 0):>7}"
                                f"  {path_val}"
                            )
                            return [dest]
                        # Pool file exists but dest is stale or
                        # missing - fix the link.
                        _sync_link(pool_path, dest, rv, hash_val)
                        _record(hit=1)
                        log(
                            f"  {'Linked':<{LOG_LABEL_W}} "
                            f"{fmt_size(self.get('size') or 0):>7}  "
                            f"{path_val}"
                        )
                        return [dest]

                # -------------------------------------------------
                # Phase 3 -- Genuine download
                # -------------------------------------------------
                # No inventory hit and no on-disk file.  Download to a
                # hash-named staging file, verify the hash, then
                # atomically replace into the pool and hardlink to the
                # repo.  The staging file is kept on failure so
                # subsequent retries can resume via curl -C -.
                tmp = str(staging_dir / staging_key)
                actual_hash = HTTPDownload(uri, tmp, expected_hash=hash_val)
                # Record the actual hash for dedup; for nodes that
                # did not have a known hash (e.g. Release file on
                # first sync) this is the authoritative value.
                self["hash"] = actual_hash
                hash_val = hash_val or actual_hash
                pool_path = _pool_path(rv.pool_root, hash_val)
                pool_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, pool_path)
                ino = _sync_link(pool_path, dest, rv, hash_val)
                sz = int(dest.stat().st_size)
                self["size"] = sz
                if rv.pool_inv is not None:
                    rv.pool_inv.add(hash_val, ino)
                _record(miss=1, bytes_tx=sz)
                log(
                    f"  {'Downloaded':<{LOG_LABEL_W}} "
                    f"{fmt_size(sz):>7}  {path_val}"
                )
                return [dest]
            finally:
                # Decref and prune the per-hash lock entry.  Runs on
                # every return (success) or raise (failure) in the
                # locked section.  When refs reaches 0 the entry is
                # removed so _staging_locks does not grow unboundedly.
                with _staging_locks_lock:
                    lock, refs = _staging_locks[staging_key]
                    if refs == 1:
                        del _staging_locks[staging_key]
                    else:
                        _staging_locks[staging_key] = (
                            lock,
                            refs - 1,
                        )


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
]
