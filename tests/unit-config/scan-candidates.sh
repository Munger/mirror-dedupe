#!/usr/bin/env bash
#
# scan-candidates.sh : Local-only scanner tests for mirror-dedupe
#
# This script is NOT installed or shipped. It is intended for interactive
# experimentation only.
#
# Usage:
#   bash tests/unit-config/scan-candidates.sh --root <path>
#
# The --root argument specifies a test filesystem root. The script creates
# the following tree under <root>:
#   <root>/etc/mirror-dedupe/mirror-dedupe.conf
#   <root>/etc/mirror-dedupe/repos-available/
#   <root>/etc/mirror-dedupe/repos-enabled/
#   <root>/mirror/repos/
#   <root>/mirror/pool/
#
# Candidate definitions are read from scan_candidates/*.yaml adjacent to
# this script. No data is synced by this script; it only runs
# mirror-dedupe-scan.
#
set -uo pipefail

ROOT=""

usage() {
  cat <<EOF >&2
Usage: $(basename "$0") --root <path>

Required:
  --root <path>   Test filesystem root (created if it does not exist)

Candidate definitions are loaded from scan_candidates/*.yaml relative
to the script's location.
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --root requires an argument" >&2
        usage
      fi
      ROOT="$2"
      shift 2
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage
      ;;
  esac
done

if [[ -z "${ROOT}" ]]; then
  echo "ERROR: --root is required" >&2
  usage
fi

ROOT="$(cd "$ROOT" 2>/dev/null && pwd -P || echo "${ROOT}")"

CONFIG_DIR="${ROOT}/etc/mirror-dedupe"
REPOS_DIR="${ROOT}/mirror/repos"
POOL_DIR="${ROOT}/mirror/pool"
REPOS_AVAILABLE="${CONFIG_DIR}/repos-available"
REPOS_ENABLED="${CONFIG_DIR}/repos-enabled"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
CANDIDATES_DIR="${SCRIPT_DIR}/scan_candidates"

mkdir -p "${CONFIG_DIR}" "${REPOS_DIR}" "${POOL_DIR}" "${REPOS_AVAILABLE}" "${REPOS_ENABLED}"

if [[ ! -f "${CONFIG_DIR}/mirror-dedupe.conf" ]]; then
  cat > "${CONFIG_DIR}/mirror-dedupe.conf" <<CONF
repo_root: ${REPOS_DIR}

pool_root: ${POOL_DIR}

architectures: ['amd64']

collapse_distributions: false

buffer_size: 1048576

parallel_downloads: 10

curl_timeout: 900

max_retries: 3

progress_interval: 1000
CONF
  echo "Created ${CONFIG_DIR}/mirror-dedupe.conf" >&2
fi

SCAN_CMD="${SCAN_CMD:-python3 -m mirror_dedupe.scan}"

yaml_to_args() {
  local file="$1"
  python3 -c "
import sys, yaml
with open('${file}') as f:
    data = yaml.safe_load(f)
if not data:
    sys.exit(0)
args = ['--name', data['name'], '--dest', data['dest']]
for u in data.get('upstreams', []):
    args.extend(['-U', u])
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

  extra_args="$(yaml_to_args "${yaml_file}")"
  if [[ -z "${extra_args}" ]]; then
    echo "WARNING: ${yaml_file} produced no args; skipping" >&2
    continue
  fi

  if ! ${SCAN_CMD} --config "${CONFIG_DIR}" ${extra_args}; then
    echo "ERROR: scan for ${name} failed (see above); continuing with next candidate" >&2
  fi
  echo "" >&2
done

cat <<EOF >&2
All candidate scans completed.

Config directory: ${CONFIG_DIR}
Output:          ${REPOS_AVAILABLE}/
Link:            ${REPOS_ENABLED}/  (create symlinks here to activate)

Next steps for each NAME above:
  mirror-dedupe --config ${CONFIG_DIR} --test NAME
EOF
