#!/usr/bin/env bash
## @file batch-scan.sh
##
## @brief Run mirror-dedupe --scan against all YAML candidates in scan_candidates/.
##
## Iterates every YAML file under tests/unit-config/scan_candidates/, converts
## each to CLI arguments via a small Python snippet, and calls
## ``mirror-dedupe --scan`` to perform live HTTP discovery against the upstream
## URL defined in the YAML.  Results are written to ``<outdir>/<name>.conf``
## and ``<outdir>/<name>.json``.
##
## This script is NOT installed or shipped.  It is intended for interactive
## testing of the scanner against real upstream repositories.  No repos,
## pool, or data directories are needed — scanning is purely in-memory HTTP
## discovery.
##
## Usage:
##   bash tests/unit-config/batch-scan.sh --outdir <dir> [--config <path>]
##
## Options:
##   --outdir <dir>    Output directory for scan results (created if absent).
##   --config <path>   Path to mirror-dedupe.conf.  If omitted a minimal
##                     config is generated at <outdir>/mirror-dedupe.conf.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT
set -uo pipefail

trap 'echo "" >&2; echo "Aborted by user." >&2; exit 130' INT

CONFIG=""
OUTDIR=""
NO_FILTER=false
EMIT_JSON=false

usage() {
  cat <<EOF >&2
Usage: $(basename "$0") --outdir <dir> [--config <path>] [--no-filter] [--emit-json]

Required:
  --outdir <dir>    Output directory (created if it does not exist)

Optional:
  --config <path>   Path to mirror-dedupe.conf. If omitted, a minimal config
                    is generated at <outdir>/mirror-dedupe.conf.
  --no-filter       Discover every distribution and architecture from each
                    upstream, ignoring filters in the YAML candidate and
                    architecture restrictions in mirror-dedupe.conf.
                    Skips "releases:" and "components:" from the YAML file.
  --emit-json       Generate a JSON snapshot file alongside each YAML config
                    (default: only YAML configs are emitted).
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --config requires an argument" >&2
        usage
      fi
      CONFIG="$2"
      shift 2
      ;;
    --outdir)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --outdir requires an argument" >&2
        usage
      fi
      OUTDIR="$2"
      shift 2
      ;;
    --no-filter)
      NO_FILTER=true
      shift
      ;;
    --emit-json)
      EMIT_JSON=true
      shift
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage
      ;;
  esac
done

if [[ -z "${OUTDIR}" ]]; then
  echo "ERROR: --outdir is required" >&2
  usage
fi

OUTDIR="$(cd "$OUTDIR" 2>/dev/null && pwd -P || echo "${OUTDIR}")"

mkdir -p "${OUTDIR}"

# Generate minimal config file when --config was not provided
if [[ -z "${CONFIG}" ]]; then
  CONFIG="${OUTDIR}/mirror-dedupe.conf"
  if [[ ! -f "${CONFIG}" ]]; then
    cat > "${CONFIG}" <<CONF
repo_root: /tmp/scan-root/repos
pool_root: /tmp/scan-root/pool
architectures: ['amd64']
collapse_distributions: true
CONF
    echo "Created ${CONFIG}" >&2
  fi
fi

SCAN_CMD="${SCAN_CMD:-mirror-dedupe --scan}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
CANDIDATES_DIR="${SCRIPT_DIR}/scan_candidates"

yaml_to_args() {
  local file="$1"
  local no_filter="${2:-false}"
  python3 -c "
import sys, yaml
with open('${file}') as f:
    data = yaml.safe_load(f)
if not data:
    sys.exit(0)
args = ['--name', data['name'], '--dest', data['dest']]
for u in data.get('upstreams', []):
    args.extend(['-U', u])
no_filter = '${no_filter}' == 'true'
if not no_filter:
    for r in data.get('releases', []):
        args.extend(['--release', r])
    comps = data.get('components')
    if comps:
        args.extend(['--components', ' '.join(comps)])
gpg = data.get('gpg_key_url')
if gpg:
    args.extend(['--gpg-key-url', gpg])
print(' '.join(args))
"
}

if [[ ! -d "${CANDIDATES_DIR}" ]]; then
  echo "ERROR: Candidates directory not found: ${CANDIDATES_DIR}" >&2
  exit 1
fi

shopt -s nullglob
for yaml_file in "${CANDIDATES_DIR}"/*.yaml; do
  name="$(basename "${yaml_file}" .yaml)"
  echo "=== Scanning ${name} ===" >&2

  extra_args="$(yaml_to_args "${yaml_file}" "${NO_FILTER}")"
  if [[ -z "${extra_args}" ]]; then
    echo "WARNING: ${yaml_file} produced no args; skipping" >&2
    continue
  fi

  scan_flags=()
  if ${NO_FILTER}; then
    scan_flags+=(--no-filter)
  fi
  if ${EMIT_JSON}; then
    scan_flags+=(--emit-json)
  fi

  if ! ${SCAN_CMD} --config "${CONFIG}" --out "${OUTDIR}" ${scan_flags[@]+"${scan_flags[@]}"} ${extra_args}; then
    echo "ERROR: scan for ${name} failed (see above); continuing with next candidate" >&2
  fi
  echo "" >&2
done

cat <<EOF >&2

Scans complete.

Output directory: ${OUTDIR}
Config files:    ${OUTDIR}/*.conf

Next steps for each NAME above:
  mirror-dedupe --config ${CONFIG} --test NAME
EOF
