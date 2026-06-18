<!--
ARCHITECTURE.md : mirror-dedupe architecture — sync pipeline design

Copyright (c) 2026 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
-->

# Architecture

mirror-dedupe is a pool-based content-addressable mirroring tool with a
schema-driven, stream architecture.  This document covers the directory
layout, the schema tree, and the sync pipeline.

## Directory layout

### Configuration

```
<config_dir>/                          default: /etc/mirror-dedupe/
  mirror-dedupe.conf                   global settings
  repos-available/<name>.conf          per-repo configurations
  repos-enabled/<name>.conf            symlinks → repos-available/
```

### Data

```
<mirror_root>/                         default: /srv/mirror/
  repos/                               repo_root
    <dest>/                            per-repo tree (dest from repo config)
      dists/
        <suite>/
          Release
          Release.gpg
          <component>/
            binary-<arch>/
              Packages.gz
            source/
              Sources.gz
      pool/
        <component>/
          <prefix>/
            <package>.deb              hardlinks into pool/by-hash/SHA256/
  pool/                                pool_root
    by-hash/SHA256/
      <ab>/                            first 2 hex chars of SHA-256
        <cd>/                          next 2 hex chars
          <fullhash>                   canonical pool entry (64-char hex)
    staging/                           temporary download area
  mirror-dedupe/                       tool metadata — not mirror content
    <name>/
      sync.lock                        per-repo exclusive flock (flock(2))
      stats.ndjson                     per-repo sync history (NDJSON, append-only)
```

The `mirror-dedupe/` directory holds tool metadata that is separate from
mirror content.  Each per-repo subdirectory contains two files: `sync.lock`
(an `flock(2)` exclusive lock held for the duration of a sync run) and
`stats.ndjson` (an append-only record of historical sync statistics).

**This directory is safe to delete when no sync is running.**  Deleting it
during a sync is dangerous: the lock file disappearing mid-sync removes the
mutual exclusion that prevents a second process from starting a concurrent
sync on the same repo, and an in-progress write to `stats.ndjson` will be
lost.  Use `--stats-reset` to clear statistics via the normal CLI.

The directory is intentionally excluded from snapshots.  Lock files are
transient by nature, and stats are non-critical — a snapshot restore leaves
mirror content fully correct regardless of whether stats are current.

`repo_root` and `pool_root` are always `<mirror_root>/repos` and
`<mirror_root>/pool`.  Both reside on the same logical volume by
construction; hardlink deduplication requires this.

## Schema tree

The in-memory schema mirrors the APT repository structure:

```
Repo  (Apt)
  └── Distributions
        └── Distribution
              └── Release          one per suite
                    └── Indices
                          └── Index    Packages.gz / Sources.gz / etc.
```

Nodes are `MDNode` subclasses.  Each node carries:

- `uri` — upstream HTTP URL
- `path` — destination path relative to `repo_root`
- `hash` / `size` — expected SHA-256 and byte count (set from Release or
  Packages metadata; absent on Release nodes whose hash is not known ahead
  of time)

`stream()` is the lazy child-materialisation hook.  It is called after a
node is synced and returns the node's children, which are then pushed onto
the work queue.  This is what makes the pipeline streaming: Index children
are only created after the Release file has been downloaded and parsed;
Package children only after Packages.gz has been downloaded and parsed.

## Inventories

Two in-memory inventories are built at startup:

**Pool inventory** (`Inventory.from_pool`) — one-time `find` scan of
`pool/by-hash/SHA256/`, producing a bidirectional `{SHA-256 ↔ inode}` map.
Built once before any repo sync starts and shared read-only across all
worker threads.

**Per-repo inventory** (`Inventory.from_path_file`) — built lazily when a
repo's sync slot opens.  `build-repo-paths.sh` runs a single sequential
`find` pass over all managed repo directories before sync workers start,
writing null-delimited `path\0inode\0` pairs to
`/tmp/mirror-dedupe/<dest>.paths` for each repo.  When a worker opens its
path file:

1. The file is unlinked immediately (kernel keeps the inode alive via the
   open fd — vanishes even on crash).
2. Every path found on disk is added to `stale_paths` — a stale candidate
   until `Node.sync()` claims it.
3. Each inode is cross-referenced against the pool inventory; matching
   inodes populate the hash index for fast pool-hit detection.

## Sync pipeline

### Startup (coordinator thread)

```
Config.load()
  └── derive repo_root, pool_root from mirror_root
  └── verify repos and pool on same filesystem (st_dev check)

Inventory.from_pool(pool_root)          single find pass over pool

_build_repo_path_files(repo_root, ...)  bash: single find pass over repos
                                        → /tmp/mirror-dedupe/<dest>.paths

assign RepoVars to each repo            (no per-repo inventory yet)
```

### Concurrent repo syncs

An outer `ThreadPoolExecutor(max_concurrent_syncs)` dispatches one thread
per repo.  Each thread runs `_sync_one()`:

```
Inventory.from_path_file(...)           load path file, unlink it
                                        → stale_paths + hash index

RepoLock.acquire()                      exclusive flock on mirror-dedupe/<name>/sync.lock

Repo.sync()
  _build_sync_tree()                    construct Dist/Release/Index nodes
                                        from config — no HTTP at this stage
  _sync_content(pool)                   dynamic work queue (see below)
  _sweep_stale()                        delete unclaimed files

RepoLock.release()
```

### Dynamic work queue (`_sync_content`)

An inner `ThreadPoolExecutor(parallel_downloads)` handles genuine downloads.
The coordinator thread drives discovery:

```
stack ← all nodes from _tree_iter()

while stack or futures:
    for each node (uri + path set):

        if pool_inv.has(node.hash):
            ── fast path ──────────────────────────────────────────
            node.sync()          os.link(pool_path, dest_path)
                                 removes path from stale_paths
            node.stream()    →   push children onto stack

        else:
            ── slow path ──────────────────────────────────────────
            future = pool.submit(node.sync)
            on completion:
                node.stream() → push children onto stack
```

The fast path runs on the coordinator thread — a pool hit is a single
`os.link()` call, cheap enough not to occupy a worker slot.  Discovery
(Release → Index → Package) and downloading therefore overlap in time.

### `Node.sync()` — three-phase strategy

1. **Inventory fast path** — if the destination already exists on disk,
   skip all I/O.  If the pool has the hash but the repo does not, hardlink
   from pool to dest.

2. **Staging lock + stat fallback** — acquire a per-hash `threading.Lock`
   so concurrent threads downloading the same content race only once.
   Double-checked locking: re-query inventories after acquiring the lock.
   Fall back to `stat()` if inventories missed but files exist.

3. **Genuine download** — curl downloads to `pool/staging/<hash>`, SHA-256
   is verified on the fly, the staging file is atomically `os.replace`d
   into `pool/by-hash/SHA256/<ab>/<cd>/<hash>`, then hardlinked into the
   repo dest.

### Stale sweep (`_sweep_stale`)

`stale_paths` was populated at startup with every file found in the repo
directory.  Every call to `Node.sync()` removes that node's path from the
set the moment it is declared wanted — whether the outcome is a pool hit,
a re-link, or a fresh download.

After `_sync_content` drains, whatever remains in `stale_paths` existed on
disk but was never wanted: old package versions, dropped architectures,
removed distributions.  These are deleted, and empty directories are pruned
bottom-up.

## Deduplication

Every unique file is stored exactly once in the pool, keyed by its SHA-256
hash.  When a file is needed in a repo it is hardlinked from the pool —
no bytes are copied.  Multiple repos or architectures sharing identical
files share the same pool inode.

A pool sweep (`--sweep-pool`) walks `pool/by-hash/SHA256/` and removes
files whose link count has dropped to 1 (no repo references them).  It is
safe to run only when no sync is in progress; `pool_sweep_safe()` checks
all repo lock files before proceeding.

## Concurrency safety

- The pool inventory is written once before workers start and read-only
  thereafter — no lock needed on reads.
- Each per-repo inventory is owned by exactly one worker thread.
- `Node.__setitem__` is protected by a per-node lock; workers update node
  payloads (path, hash, size) safely.
- Workers never add or remove structural children — the static tree
  skeleton is fully built by `_build_sync_tree()` before `_sync_content`
  runs.  The coordinator's `_tree_iter()` snapshot is therefore stable.
- Per-hash staging locks prevent duplicate downloads when the same content
  appears in multiple repos syncing concurrently.
