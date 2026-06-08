#!/usr/bin/env bash
## @file sync-hashes.sh
##
## @brief Rebuild the content-addressed pool from existing repo and snapshot trees.
##
## Walks one or more managed repo directories under repos_root and ensures
## every regular file is hardlinked into the pool at
## pool_dir/by-hash/SHA256/<ab>/<cd>/<sha256>.  Files whose inode is
## already present in the pool are skipped (no re-hashing needed).  New
## files are SHA-256 hashed in batches and linked into the pool; pool
## entries with no remaining repo references (st_nlink == 1) are pruned.
##
## This is the implementation of ``mirror-dedupe --relink-pool``.  It is
## also useful for disaster recovery: if the pool is deleted or corrupted,
## running this script against the live repo tree reconstructs it without
## any upstream downloads.
##
## Processing is pipelined: a main loop feeds file paths into a FIFO
## and a background worker reads batches, runs sha256sum in parallel, and
## calls link_hashed() for each result.  This keeps CPU and I/O busy
## simultaneously without thrashing the disk with random seeks.
##
## Usage:
##   sync-hashes.sh [-v|-q] [-b batch_size] --include NAME ... <repos_root> <pool_dir>
##
## Options:
##   -v, --verbose      Enable verbose output (default: on).
##   -q, --quiet        Suppress verbose output.
##   -b, --batch-size   Number of files per sha256sum batch (default: 200).
##   --include NAME     Directory name under repos_root to process.
##                      May be specified multiple times.  At least one required.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

set -euo pipefail

verbose=1
batch_size=${BATCH_SIZE:-200}
include_names=()

while [[ $# -gt 0 ]]; do
  case $1 in
    -v|--verbose)
      verbose=1
      shift
      ;;
    -q|--quiet)
      verbose=0
      shift
      ;;
    -b|--batch-size)
      batch_size=$2
      shift 2
      ;;
    --include)
      if [[ -z "$2" || "$2" == -* ]]; then
        echo "ERROR: --include requires a name argument" >&2
        exit 2
      fi
      include_names+=("$2")
      shift 2
      ;;
    -*)
      echo "Unknown option: $1" >&2
      echo "usage: $0 [-v] [-b batch_size] --include NAME ... <repos_root> <pool_dir>" >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -ne 2 ]]; then
  echo "usage: $0 [-v] [-b batch_size] --include NAME ... <repos_root> <pool_dir>" >&2
  exit 2
fi

repos=$1
pool=$2
[[ -d "$repos" ]] || { echo "Missing repos root: $repos" >&2; exit 1; }
[[ -d "$pool" ]] || { echo "Missing pool dir: $pool" >&2; exit 1; }

if [[ ${#include_names[@]} -eq 0 ]]; then
  echo "ERROR: at least one --include NAME is required" >&2
  exit 2
fi

printf "  Checking repositories %s\n" "${include_names[*]}"

logfile=${LOGFILE:-/tmp/sync-hashes.log}
: >"$logfile"
exec 2>>"$logfile"

log() {
  local lvl="$1"
  shift
  if (( verbose )); then
    printf '  [%s] %s\n' "$lvl" "$*"
  fi
  printf '[%s] %s\n' "$lvl" "$*" >>"$logfile"
  return 0
}

## Ignore SIGPIPE so that writes to the FIFO do not kill the main process
## if the worker exits early.  pipefail is also disabled for the same reason:
## broken pipes in subshell pipelines are expected and handled explicitly.
trap '' PIPE
set +o pipefail

## link_hashed <repo_file> <sha256>
## Hardlink repo_file into the pool under by-hash/SHA256/<ab>/<cd>/<hash>.
## Direction depends on which end already exists:
##   pool entry exists  -> link pool -> repo_file  (repo_file gets pool inode)
##   pool entry absent  -> link repo_file -> pool  (pool entry gets repo inode)
## ln -f overwrites the destination if present; -n treats a destination
## symlink as a plain file rather than following it.
link_hashed() {
  local repo_file=$1
  local hash=$2

  if [[ ! $hash =~ ^[a-f0-9]{64}$ ]]; then
    log "ERROR" "Invalid hash: $hash for $repo_file"
    return 1
  fi

  ## Fan-out: first two hex chars -> subdir a, next two -> subdir b.
  ## Avoids millions of files in a single directory.
  local a=${hash:0:2}
  local b=${hash:2:2}
  local dest="$hash_root/$a/$b/$hash"

  if [[ -e "$dest" ]]; then
    log "LINK" "$repo_file <- pool (existing)"
    ln -fn "$dest" "$repo_file"
  else
    mkdir -p "$(dirname "$dest")"
    log "LINK" "$repo_file -> pool (new)"
    ln -fn "$repo_file" "$dest"
  fi
}

## Canonicalize paths so relative arguments work correctly from any CWD.
repos=$(cd "$repos" && pwd)
pool=$(cd "$pool" && pwd)
hash_root="$pool/by-hash/SHA256"
mkdir -pv "$hash_root"

## repo_seen:    inode -> 1  tracks inodes already queued this run so that
##               hardlinked duplicates within the repo tree are only hashed once.
## byhash_inode: inode -> 1  tracks inodes already present in the pool so
##               files that are already pooled can be skipped without re-hashing.
declare -A repo_seen byhash_inode
hash_done=0
skip_seen=0
skip_missing=0

## --- Build find-starting-points --------------------------------------------
find_roots=()
for name in "${include_names[@]}"; do
  root="$repos/$name"
  if [[ -d "$root" ]]; then
    find_roots+=("$root")
  else
    log "WARN" "Skipping --include '$name': not found under $repos"
  fi
done
if [[ ${#find_roots[@]} -eq 0 ]]; then
  log "ERROR" "No --include directories exist under $repos"
  exit 1
fi

## --- PHASE 1: Index existing pool ------------------------------------------
## Build an inode set for every file currently in the pool.  Files whose
## inode is already in the pool are skipped in phase 2 without re-hashing —
## a significant speedup on subsequent runs where most files are unchanged.
## stat -f is BSD (macOS); stat -c is GNU (Linux); the || chain handles both.
log "INFO" "Indexing existing pool..."
while IFS= read -r -d '' inode; do
  byhash_inode["$inode"]=1
done < <(
  find "$pool" -xdev -type f -print0 2>/dev/null | while IFS= read -r -d '' f; do
    stat -f '%i' "$f" 2>/dev/null || stat -c '%i' "$f" 2>/dev/null
    printf '\0'
  done
  ## 'true' prevents a non-zero exit from find/stat propagating to the
  ## outer while loop, which would terminate the read prematurely.
  true
)

## --- PHASE 2: Processing ---------------------------------------------------
## A named FIFO decouples the find/stat pipeline from the sha256sum worker.
## find and stat run in one process; sha256sum batching runs in another;
## both execute concurrently so CPU and disk I/O overlap.
##
## FIFO setup: mktemp gives a unique path on any filesystem, then we
## delete the regular file it created and replace it with a named pipe.
log "INFO" "Processing ${#include_names[@]} repos (${#find_roots[@]} directories)..."
set +e
work_fifo=$(mktemp)
rm -f "$work_fifo"
mkfifo "$work_fifo"

## --- Background worker subshell -------------------------------------------
## The worker runs as a background subshell (&).  It owns the read end of
## the FIFO and processes records in batches to amortise sha256sum startup.
(
  ## Errors inside the worker should not kill the parent via ERR trap.
  set +e
  trap - ERR

  ## Open the read end of the FIFO.  bash's {var}< syntax auto-assigns an
  ## available fd number >= 3 and stores it in w_fd.  Opening inside the
  ## subshell means only the worker holds this fd — the parent never sees it.
  exec {w_fd}<"$work_fifo"

  while :; do
    batch_repo_files=()

    ## Blocking read: wait for the first record of a new batch.
    ## A failed read (EOF or error) means the write end was closed — all
    ## work is done, exit cleanly.
    if ! IFS= read -r -d '' record <&$w_fd; then
      break
    fi
    ## Records are inode|path\0; strip the inode prefix to get the path.
    repo_file=${record#*|}
    batch_repo_files+=("$repo_file")

    ## Non-blocking drain: fill the batch with any records immediately
    ## available, without waiting.  -t 0.01 (10 ms timeout) prevents
    ## blocking on a slow feed while still building larger batches when
    ## records arrive in bursts, reducing sha256sum invocations.
    while (( ${#batch_repo_files[@]} < batch_size )); do
      if ! IFS= read -r -t 0.01 -d '' record <&$w_fd; then
        break
      fi
      repo_file=${record#*|}
      batch_repo_files+=("$repo_file")
    done

    ## Write the batch as a null-delimited list for xargs -0.
    ## Three temp files: tmp_list (input), tmp_out (sha256sum output),
    ## tmp_err (sha256sum errors).  The RETURN trap cleans them up even
    ## if sha256sum fails.
    idx=0
    tmp_out=$(mktemp)
    tmp_list=$(mktemp)
    tmp_err=$(mktemp)
    trap 'rm -f "$tmp_out" "$tmp_list" "$tmp_err"' RETURN
    printf '%s\0' "${batch_repo_files[@]}" >"$tmp_list"

    ## sha256sum --zero outputs null-delimited lines so filenames containing
    ## spaces or newlines are handled correctly.
    if ! xargs -0 sha256sum --zero >"$tmp_out" 2>"$tmp_err" <"$tmp_list"; then
      log "ERROR" "HASH FAIL batch count=${#batch_repo_files[@]}"
      exit 1
    fi

    ## sha256sum output format: "<hash>  <filename>\0"
    ## Strip everything from the first space onwards to isolate the hash.
    ## idx tracks which batch entry corresponds to each output line.
    while IFS= read -r -d '' line; do
      hash=${line%% *}
      r_file=${batch_repo_files[$idx]}
      link_hashed "$r_file" "$hash"
      ((hash_done++))
      ((idx++))
    done <"$tmp_out"
    rm -f "$tmp_out" "$tmp_list" "$tmp_err"
    trap - RETURN
  done
) &
worker_pid=$!

## Open TWO write file descriptors to the FIFO.
##
## wf_fd  — the working fd used to send records to the worker.
## wf_ka  — a keep-alive fd that holds the FIFO's write end open.
##
## A FIFO delivers EOF to its reader only when ALL writers have closed their
## end.  Closing wf_fd (at the end of the main loop) therefore does NOT
## signal EOF — wf_ka still holds the write end open, letting the worker
## drain any buffered records.  Closing wf_ka (immediately after) removes
## the last writer and delivers EOF, causing the worker to exit cleanly.
##
## Both opens must come AFTER the worker subshell starts: the worker's
## exec {w_fd}<"$work_fifo" (opening the read end) and the parent's write
## opens mutually unblock each other — FIFOs block until both ends are open.
exec {wf_ka}>"$work_fifo"
exec {wf_fd}>"$work_fifo"

## On INT/TERM: close write ends (unblocks worker's read with EOF), kill the
## worker, and exit 130 (standard convention for SIGINT termination).
trap 'exec {wf_fd}>&- 2>/dev/null; exec {wf_ka}>&- 2>/dev/null; kill $worker_pid 2>/dev/null; exit 130' INT TERM

## --- Main feed loop --------------------------------------------------------
## Process substitution runs find and stat in a pipeline; the outer while
## loop reads null-delimited inode\0path\0 pairs from it.
## find prunes .mirror-dedupe directories to exclude stats and lock files.
while IFS= read -r -d '' inode && IFS= read -r -d '' repo_file; do
  set +e

  ## ${array[$key]+x} is the safe way to test associative array key existence
  ## under 'set -u' — it expands to 'x' if the key is set, empty otherwise,
  ## without triggering an unbound variable error.
  ##
  ## Skip if already in pool (byhash_inode) or already queued this run
  ## (repo_seen) — hardlinks share an inode so duplicates must only be
  ## hashed once.
  if [[ ${byhash_inode[$inode]+x} ]]; then
    ((skip_seen++))
    continue
  fi
  if [[ ${repo_seen[$inode]+x} ]]; then
    ((skip_seen++))
    continue
  fi

  repo_seen["$inode"]=1

  if [[ ! -e "$repo_file" ]]; then
    ((skip_missing++))
    continue
  fi

  ## kill -0 sends no signal; it merely checks whether the worker process
  ## is still alive.  If the worker has exited (e.g. sha256sum error), stop
  ## feeding it rather than filling the FIFO indefinitely.
  if ! kill -0 "$worker_pid" 2>/dev/null; then
    break
  fi

  ## Send inode|path\0 to the worker.  The inode prefix is used by the
  ## worker to strip path for logging but is otherwise ignored — the
  ## batched sha256sum output is matched by position (idx), not by name.
  ## A failed write means the FIFO is broken (worker gone); break cleanly.
  if ! printf '%s|%s\0' "$inode" "$repo_file" >&$wf_fd 2>/dev/null; then
    break
  fi
done < <(
  find "${find_roots[@]}" -xdev \( -type d -name ".mirror-dedupe" -prune \) -o -type f -print0 2>/dev/null | while IFS= read -r -d '' f; do
    inode=$(stat -f '%i' "$f" 2>/dev/null || stat -c '%i' "$f" 2>/dev/null)
    printf '%s\0%s\0' "$inode" "$f"
  done
  ## 'true' swallows non-zero exit codes from find/stat so the outer
  ## while loop is not terminated prematurely by permission errors.
  true
)

set -e

## Close wf_fd first (stops new work), then wf_ka (delivers EOF to worker).
## Closing both in the wrong order would signal EOF before all records are
## drained.  See the two-fd explanation above.
exec {wf_fd}>&-
exec {wf_ka}>&-

## Block until the worker finishes processing remaining batched records.
wait "$worker_pid"
rm -f "$work_fifo"

## --- PHASE 3: Prune orphans ------------------------------------------------
## Pool files with st_nlink == 1 have no remaining hardlinks in any repo or
## snapshot tree — they are orphaned content.  Safe to delete.
## find -links 1 matches files with exactly one link (the pool entry itself).
log "INFO" "Pruning stale hashes (link count 1)..."
pruned=0
while IFS= read -r -d '' hfile; do
  rm -f "$hfile"
  ((pruned++))
done < <(find "$hash_root" -xdev -type f -links 1 -print0)

## Prune bottom-up (-depth) so parent directories are only removed after
## their children, avoiding rmdir failures on non-empty dirs.
log "INFO" "Pruning empty directories..."
while IFS= read -r d; do
  rmdir "$d" 2>/dev/null || true
done < <(find "$hash_root" -xdev -depth -type d -empty -print)

## --- Summary ----------------------------------------------------------------
total=$(( hash_done + skip_seen ))
if (( verbose )); then
  printf '\n'
  log "DONE" "Scanned $total files: $hash_done linked, $skip_seen skipped, $pruned pruned"
  if (( skip_missing > 0 )); then
    log "WARN" "$skip_missing files disappeared during processing"
  fi
fi