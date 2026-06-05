<!--
ARCHITECTURE.md : mirror-dedupe-dev architecture — sync pipeline design

Copyright (c) 2026 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
-->

# Architecture

mirror-dedupe-dev is a pool-based content-addressable mirroring tool with
schema-driven architecture.  This document covers the sync pipeline: how
bucket-based workers transform upstream package metadata into pool-hardlinked
repository trees.

## Pipeline Overview

```
upstream                                  local
─────────────────────────────────────     ─────────────────────────────────────
Release + Packages.gz (HTTP)              .mirror-dedupe/pending/00/00/
                                         .mirror-dedupe/pending/00/01/
                                              ⋮
                                         .mirror-dedupe/pending/ff/ff/
                                         .mirror-dedupe/done/
                                              ⋮
pool/by-hash/SHA256/00/00/<fullhash>
pool/by-hash/SHA256/00/01/<fullhash>
      ⋮
repo/dists/<suite>/<component>/...
repo/pool/main/...
```

Four phases, executed in order:

```
  Scan  ──→  Partition  ──→  Execute  ──→  Cleanup
```

### Phase 1: Scan

Discover repos, fetch Release files and Packages.gz, parse stanzas into
metadata-only Index nodes.  Packages.gz bytes are stored on the Index node
as `_raw_bytes` and serialised into `snapshot.json`.

The snapshot contains Index nodes (path, kind, metadata, _raw_bytes) but
**no Package children** — those are ephemeral and materialised only during
partitioning.

### Phase 2: Partition

Load snapshot.json.  For each Index:

1. Call `Index.parse()` — decompresses `_raw_bytes` and calls
   `_parse_packages()`, which creates `Bucket` children instead of direct
   `Package` children.
2. Each Bucket covers a contiguous range of `sha256[:2]` prefix values
   (e.g. `00`–`0F`), matching the pool directory layout
   `by-hash/SHA256/{first2}/{next2}/`.
3. Serialise each Bucket to a snapshot file at:
   `pending/{first2}/{next2}/`
4. The clone and its Buckets are garbage-collected before the next Index.

The `pending/` directory mirrors the pool layout:

```
pending/             done/
  ├── 00/              ├── 00/
  │   ├── 00/          │   ├── 01/
  │   └── 01/          │   └── 0a/
  └── ab/              └── ab/
      └── cd/              └── cd/
```

Only populated directories are created.  A Bucket at `pending/ab/cd/` covers
all packages whose `sha256[:4]` starts with `abcd` — that is, `sha256[:2]`
= `ab` and `sha256[2:4]` = `cd`.

### Phase 3: Execute

N worker processes share a per-repo quota.  Each worker:

1. Lists `pending/` to find available directories (first two levels).
2. Atomically claims a directory by `rename(pending/ab, working/ab)`.
3. For each bucket file `working/ab/{prefix}.json`:
   a. Deserialise the Bucket snapshot → Bucket node → Package children.
   b. For each Package: `pool.check(hash)` → `pool.fetch(url, hash)` if
      missing → `pool.link(hash, repo_path)`.
4. On completion: `rename(working/ab, done/ab)`.

Multiple workers claim directories in parallel; `rename()` provides atomic
exclusion — two workers cannot claim the same directory.

**Rebalancing**: each repo starts with a quota of worker slots.  When a
repo's queue drains, its slots are reallocated to repos still processing.
Since all `pending/` directories exist before execution starts, no per-file
coordination or queue server is needed.

### Phase 4: Cleanup

- Remove all `done/` directories.
- Remove stale `pending/` directories not in the current snapshot's set.
- Pool sweep: walk `by-hash/SHA256/*/*/`, remove files with `st_nlink <= 1`,
  prune empty directories.

## Directory layout

```
<repo-root>/
  dists/...
  pool/...
  .mirror-dedupe/
    snapshot.json              ← metadata + _raw_bytes, no Package children
    pending/                   ← per-sync generated buckets
      00/                      ← first two hex chars of SHA-256
        00/                    ← next two hex chars → bucket snapshot
        01/
        ...
      ff/
        ff/
    done/                      ← claimed + completed buckets
    working/                   ← claimed + in-progress buckets
```

## Data model (Bucket class)

```
Index
  └── Bucket (Node subclass)
        ├── first2: str       ← sha256[:2]
        ├── next2: str        ← sha256[2:4]
        └── packages: Packages
              ├── Package      ← path, hash, size, uri
              ├── Package
              └── ...
```

Each Bucket is a standalone Node that knows its pool directory range.  Its
`sync()` method iterates Package children and delegates to `pool.fetch()` /
`pool.link()`.

## Worker model

```
┌─────────────────────────────────────┐
│            Worker Pool              │
│  ┌──────┐ ┌──────┐ ┌──────┐        │
│  │  W1  │ │  W2  │ │  W3  │  ...   │
│  └──┬───┘ └──┬───┘ └──┬───┘        │
│     │        │        │            │
│     ▼        ▼        ▼            │
│  pending/  pending/  pending/      │
│  claim     claim     claim         │
│  rename    rename    rename        │
│  ──────    ──────    ──────        │
│  working/  working/  working/      │
│  process   process   process       │
│  ──────    ──────    ──────        │
│  done/     done/     done/         │
└─────────────────────────────────────┘
```

- Workers are multiprocessing (not threading), since `pool.fetch()` runs
  curl subprocesses and I/O.
- Worker count is configurable (`--workers N`).
- Workers batch-claim 2–3 pending directories at a time to reduce
  filesystem contention on `pending/`.

## Cross-session behaviour

Each `mirror-dedupe sync` run:

1. Regenerates `pending/` from the fresh snapshot.
2. Old pending directories from the previous run (left from a crash or
   interrupt) are removed during cleanup or are overwritten by the new
   partition phase.
3. Each repo's worker quota is recalculated based on total workers and
   repo sizes.
4. Previously-synced pool content is reused — `pool.check(hash)` skips
   files already present.
