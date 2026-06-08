#!/usr/bin/env bash
## @file build-repo-paths.sh
##
## @brief Build per-repo pre-sync path lists under /tmp/mirror-dedupe/.
##
## Runs a single GNU find pass over all managed repo directories and
## routes each file's relative path and inode number into a per-repo
## output file at /tmp/mirror-dedupe/<repo_name>.paths.
##
## Each output file contains null-delimited ``path\0inode\0`` pairs in
## the format consumed by ``Inventory.from_path_file()`` in Python.
## Files are overwritten on every run so crashed runs leave no permanent
## litter in tmpfs.  Python opens each file and unlinks it immediately
## on read, so the file vanishes even if the process subsequently crashes.
##
## The find expression embeds the destination file path directly in its
## ``-printf`` output so the routing loop needs no conditional logic: it
## reads three null-delimited fields (outfile, relative_path, inode) and
## writes the latter two to the file descriptor already open for that
## outfile.  Using persistent file descriptors avoids repeated open/close
## overhead in the inner loop.
##
## Call this script once before starting any sync workers so the disk
## scan completes while the disk is quiescent and does not compete with
## the concurrent hardlink and download I/O that follows.  On systems
## where /tmp is tmpfs (RAM-backed) all file I/O here is in-memory.
##
## Usage:
##   build-repo-paths.sh <repo_root> <dest_name>...
##
## Arguments:
##   repo_root    Root of the repository tree (e.g. /srv/mirror/repos).
##   dest_name    One or more first-level directory names under repo_root
##                to scan (e.g. postgresql test).
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

set -euo pipefail
trap '' PIPE

## --- argument validation --------------------------------------------------

if [[ $# -lt 2 ]]; then
    printf 'Usage: %s <repo_root> <dest_name>...\n' "$(basename "$0")" >&2
    exit 2
fi

repo_root="$1"; shift
dest_names=("$@")

## --- resolve GNU find and GNU awk (macOS requires Homebrew versions) ------
## BSD find lacks -printf; BSD awk does not support RS="\0".
## Both are available via: brew install findutils gawk

if [[ "$(uname -s)" == "Darwin" ]]; then
    FIND=$(command -v gfind 2>/dev/null) || {
        printf 'ERROR: gfind not found.  Install with: brew install findutils\n' >&2
        exit 1
    }
    AWK=$(command -v gawk 2>/dev/null) || {
        printf 'ERROR: gawk not found.  Install with: brew install gawk\n' >&2
        exit 1
    }
else
    FIND=find
    AWK=awk
fi

## --- destination directory -----------------------------------------------
## Fixed path — no PID suffix.  If a previous run crashed and left files
## behind the next run overwrites them silently.

dest_dir="/tmp/mirror-dedupe"
mkdir -p "$dest_dir"

## --- build the find expression --------------------------------------------
## One -path/-printf clause per repo separated by -o (logical OR).
##
## %P — path relative to the find starting point (repo_root), which
##      includes the repo name prefix and matches the paths that
##      Node.sync() discards from stale_paths during sync.
## %i — inode number, used to cross-reference the pool inventory.
##
## The outfile path is embedded as the first field so the awk router
## needs no lookup table: it reads outfile\0rel\0inode\0 per file and
## routes directly.
##
## .mirror-dedupe directories (stats files, lock files) are excluded
## via -prune so they never appear in any repo's path list.

find_expr=( "(" "-type" "d" "-name" ".mirror-dedupe" "-prune" ")" "-o" )

first=1
for name in "${dest_names[@]}"; do
    outfile="${dest_dir}/${name}.paths"
    (( first )) || find_expr+=("-o")
    find_expr+=(
        "("
            "-type" "f"
            "-path"   "${repo_root}/${name}/*"
            "-printf" "${outfile}\\0%P\\0%i\\0"
        ")"
    )
    first=0
done

## --- single find pass with awk routing ------------------------------------
## awk reads null-delimited triples (RS="\0"): outfile, relative path, inode.
## For each triple it writes rel\0inode\0 to the indicated output file.
##
## awk's > operator truncates on first open and holds the file descriptor
## open for the rest of the run — no repeated open/close overhead per record,
## and any file left from a previous run is silently overwritten.
##
## %c with value 0 outputs a literal null byte portably across awk versions.
## ORS="" prevents awk appending its own output record separator.
## RS="\0" requires GNU awk (gawk); BSD awk does not support null separators.

"${FIND}" "${repo_root}" -xdev "${find_expr[@]}" | \
"${AWK}" '
    BEGIN { RS="\0"; ORS="" }
    (NR%3)==1 { dest=$0; next }
    (NR%3)==2 { rel=$0;  next }
              { printf "%s%c%s%c", rel, 0, $0, 0 > dest }
' || true   ## tolerate SIGPIPE if caller exits early
