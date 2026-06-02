#!/usr/bin/env bash
set -euo pipefail

verbose=0
batch_size=${BATCH_SIZE:-200}

while [[ $# -gt 0 ]]; do
  case $1 in
    -v|--verbose)
      verbose=1
      shift
      ;;
    -b|--batch-size)
      batch_size=$2
      shift 2
      ;;
    -*)
      echo "Unknown option: $1" >&2
      echo "usage: $0 [-v] [-b batch_size] <repos_dir> <pool_dir>" >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -ne 2 ]]; then
  echo "usage: $0 [-v] [-b batch_size] <repos_dir> <pool_dir>" >&2
  exit 2
fi

logfile=${LOGFILE:-/tmp/sync-hashes.log}
: >"$logfile"
exec 2>>"$logfile"

log() {
  if (( verbose )); then
    printf '%s\n' "$*"
  fi
  printf '%s\n' "$*" >>"$logfile"
  return 0
}
trap '' PIPE
set +o pipefail

link_hashed() {
  local repo_file=$1
  local hash=$2

  # Validation: Ensure hash is exactly 64 hex characters
  if [[ ! $hash =~ ^[a-f0-9]{64}$ ]]; then
    log "ERROR: Invalid hash detected: $hash for $repo_file"
    return 1
  fi

  local a=${hash:0:2}
  local b=${hash:2:2}
  local dest="$hash_root/$a/$b/$hash"

  if [[ -e "$dest" ]]; then
    log "Link repo to existing hash: $repo_file <- $dest"
    ln -fn "$dest" "$repo_file"
  else
    mkdir -p "$(dirname "$dest")"
    log "Link hash: $repo_file -> $dest"
    ln -fn "$repo_file" "$dest"
  fi
}

repos=$1
pool=$2
[[ -d "$repos" ]] || { echo "Missing repos dir: $repos" >&2; exit 1; }

# Canonicalize paths to absolute
repos=$(cd "$repos" && pwd)
pool=$(cd "$pool" && pwd)

hash_root="$pool/by-hash/SHA256"
mkdir -pv "$hash_root"

declare -A repo_seen byhash_inode
hash_done=0
skip_seen=0
skip_missing=0

# --- PHASE 1: Index existing pool ---
log "Indexing existing pool..."
while IFS= read -r -d '' inode; do
  byhash_inode["$inode"]=1
done < <(find "$pool" -xdev -type f -printf '%i\0' 2>/dev/null || true)

# --- PHASE 2: Processing ---
log "Processing repositories..."
set +e
work_fifo=$(mktemp)
rm -f "$work_fifo"
mkfifo "$work_fifo"

(
  set +e
  trap - ERR
  # Worker opens for reading ONLY.
  exec {w_fd}<"$work_fifo"

  while :; do
    batch_repo_files=()
    # Read first record (blocking).
    if ! IFS= read -r -d '' record <&$w_fd; then
      break
    fi
    repo_file=${record#*|}
    batch_repo_files+=("$repo_file")

    # Read more records (non-blocking) to fill the batch.
    while (( ${#batch_repo_files[@]} < batch_size )); do
      if ! IFS= read -r -t 0.01 -d '' record <&$w_fd; then
        break
      fi
      repo_file=${record#*|}
      batch_repo_files+=("$repo_file")
    done

    idx=0
    tmp_out=$(mktemp)
    tmp_list=$(mktemp)
    tmp_err=$(mktemp)
    trap 'rm -f "$tmp_out" "$tmp_list" "$tmp_err"' RETURN
    printf '%s\0' "${batch_repo_files[@]}" >"$tmp_list"
    log "DEBUG: Batch contains ${#batch_repo_files[@]} files, first few: ${batch_repo_files[0]} ${batch_repo_files[1]} ${batch_repo_files[2]}"
    # Use xargs -0 to avoid long argv and for compatibility (no --files0-from support here).
    if ! xargs -0 sha256sum --zero >"$tmp_out" 2>"$tmp_err" <"$tmp_list"; then
      log "worker: HASH FAIL batch count=${#batch_repo_files[@]} stderr: $(<"$tmp_err")"
      log "DEBUG: tmp_list contents: $(cat "$tmp_list" | tr '\0' '\n' | head -5)"
      exit 1
    fi
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

# Main process opens for writing.
# We open it twice: once for the 'keep-alive' and once for the 'writer'.
exec {wf_ka}>"$work_fifo"
exec {wf_fd}>"$work_fifo"

trap 'exec {wf_fd}>&- 2>/dev/null; exec {wf_ka}>&- 2>/dev/null; kill $worker_pid 2>/dev/null; exit 130' INT TERM

while IFS= read -r -d '' inode && IFS= read -r -d '' repo_file; do
  set +e
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

  if ! kill -0 "$worker_pid" 2>/dev/null; then
    break
  fi

  log "DEBUG: Enqueuing inode=$inode file=$repo_file"
  if ! printf '%s|%s\0' "$inode" "$repo_file" >&$wf_fd 2>/dev/null; then
    break
  fi
done < <(find "$repos" -xdev -type f -printf '%i\0%p\0' 2>/dev/null || true)

set -e
# Close BOTH descriptors to send EOF to the worker.
exec {wf_fd}>&-
exec {wf_ka}>&-

wait "$worker_pid"
rm -f "$work_fifo"

log "Pruning stale hashes..."
while IFS= read -r -d '' hfile; do
  log "Remove stale hash: $hfile"
  rm -f "$hfile"
done < <(find "$hash_root" -xdev -type f -links 1 -print0)

log "Pruning empty directories..."
while IFS= read -r d; do
  log "Remove dir: $d"
  rmdir "$d"
done < <(find "$hash_root" -xdev -depth -type d -empty -print)

echo "Done."