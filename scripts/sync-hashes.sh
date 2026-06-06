#!/usr/bin/env bash
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
trap '' PIPE
set +o pipefail

link_hashed() {
  local repo_file=$1
  local hash=$2

  if [[ ! $hash =~ ^[a-f0-9]{64}$ ]]; then
    log "ERROR" "Invalid hash: $hash for $repo_file"
    return 1
  fi

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

# Canonicalize paths
repos=$(cd "$repos" && pwd)
pool=$(cd "$pool" && pwd)
hash_root="$pool/by-hash/SHA256"
mkdir -pv "$hash_root"

declare -A repo_seen byhash_inode
hash_done=0
skip_seen=0
skip_missing=0

# --- Build find-starting-points --------------------------------------------
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

# --- PHASE 1: Index existing pool ------------------------------------------
log "INFO" "Indexing existing pool..."
while IFS= read -r -d '' inode; do
  byhash_inode["$inode"]=1
done < <(
  find "$pool" -xdev -type f -print0 2>/dev/null | while IFS= read -r -d '' f; do
    stat -f '%i' "$f" 2>/dev/null || stat -c '%i' "$f" 2>/dev/null
    printf '\0'
  done
  true
)

# --- PHASE 2: Processing ---------------------------------------------------
log "INFO" "Processing ${#include_names[@]} repos (${#find_roots[@]} directories)..."
set +e
work_fifo=$(mktemp)
rm -f "$work_fifo"
mkfifo "$work_fifo"

(
  set +e
  trap - ERR
  exec {w_fd}<"$work_fifo"

  while :; do
    batch_repo_files=()
    if ! IFS= read -r -d '' record <&$w_fd; then
      break
    fi
    repo_file=${record#*|}
    batch_repo_files+=("$repo_file")

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
    if ! xargs -0 sha256sum --zero >"$tmp_out" 2>"$tmp_err" <"$tmp_list"; then
      log "ERROR" "HASH FAIL batch count=${#batch_repo_files[@]}"
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

exec {wf_ka}>"$work_fifo"
exec {wf_fd}>"$work_fifo"

trap 'exec {wf_fd}>&- 2>/dev/null; exec {wf_ka}>&- 2>/dev/null; kill $worker_pid 2>/dev/null; exit 130' INT TERM

# find excludes .mirror-dedupe dirs via -prune, only emits files
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

  if ! printf '%s|%s\0' "$inode" "$repo_file" >&$wf_fd 2>/dev/null; then
    break
  fi
done < <(
  find "${find_roots[@]}" -xdev \( -type d -name ".mirror-dedupe" -prune \) -o -type f -print0 2>/dev/null | while IFS= read -r -d '' f; do
    inode=$(stat -f '%i' "$f" 2>/dev/null || stat -c '%i' "$f" 2>/dev/null)
    printf '%s\0%s\0' "$inode" "$f"
  done
  true
)

set -e
exec {wf_fd}>&-
exec {wf_ka}>&-

wait "$worker_pid"
rm -f "$work_fifo"

# --- PHASE 3: Prune orphans -------------------------------------------------
log "INFO" "Pruning stale hashes (link count 1)..."
pruned=0
while IFS= read -r -d '' hfile; do
  rm -f "$hfile"
  ((pruned++))
done < <(find "$hash_root" -xdev -type f -links 1 -print0)

log "INFO" "Pruning empty directories..."
while IFS= read -r d; do
  rmdir "$d" 2>/dev/null || true
done < <(find "$hash_root" -xdev -depth -type d -empty -print)

# --- Summary ----------------------------------------------------------------
total=$(( hash_done + skip_seen ))
if (( verbose )); then
  printf '\n'
  log "DONE" "Scanned $total files: $hash_done linked, $skip_seen skipped, $pruned pruned"
  if (( skip_missing > 0 )); then
    log "WARN" "$skip_missing files disappeared during processing"
  fi
fi