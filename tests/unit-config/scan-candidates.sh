#!/usr/bin/env bash
#
# scan-candidates.sh : Local-only scanner tests for mirror-dedupe
#
# This script is NOT installed or shipped. It is intended for interactive
# experimentation only. It uses the tests/unit-config tree as its
# configuration directory and writes configs into:
#   tests/unit-config/repos-available/
#
# No data is synced by this script; it only runs mirror-dedupe-scan.
# You can then run mirror-dedupe --config tests/unit-config --test NAME
# to validate each generated config.
#
# Many repositories will auto-detect their GPG key. For those that do not,
# you can update the commands below to add:
#   --gpg-key-url  ...
#   --gpg-key-path ...
# once you have identified the correct locations.
#
# Usage:
#   From the repo root:
#     bash tests/unit-config/scan-candidates.sh
#

set -uo pipefail

CONFIG_DIR="tests/unit-config"
CANDIDATES_FILE="${CONFIG_DIR}/candidates.conf"

# Scanner command (can be overridden, e.g. SCAN_CMD="python3 -m mirror_dedupe.scan")
SCAN_CMD="${SCAN_CMD:-mirror-dedupe-scan}"

scan_line() {
  local line="$1"

  # Strip leading/trailing whitespace
  line="${line##+([[:space:]])}"
  line="${line%%+([[:space:]])}"

  # Skip empty or comment lines
  [[ -z "${line}" ]] && return 0
  [[ "${line}" =~ ^[[:space:]]*# ]] && return 0

  # Split into fields
  local -a fields=()
  read -r -a fields <<<"${line}"
  local count=${#fields[@]}
  if (( count < 3 )); then
    echo "Skipping malformed line (need at least name dest upstream): ${line}" >&2
    return 0
  fi

  local name dest upstream
  name=${fields[0]}
  dest=${fields[1]}
  upstream=${fields[2]}

  # Extra args are everything after upstream
  local -a extras=()
  if (( count > 3 )); then
    extras=(${fields[@]:3})
  fi

  echo "=== Scanning ${name} (${upstream}) ===" >&2
  if (( ${#extras[@]} > 0 )); then
    if ! ${SCAN_CMD} \
      --config "${CONFIG_DIR}" \
      --name "${name}" \
      --dest "${dest}" \
      "${extras[@]}" \
      "${upstream}"; then
      echo "ERROR: scan for ${name} failed (see above); continuing with next candidate" >&2
    fi
  else
    if ! ${SCAN_CMD} \
      --config "${CONFIG_DIR}" \
      --name "${name}" \
      --dest "${dest}" \
      "${upstream}"; then
      echo "ERROR: scan for ${name} failed (see above); continuing with next candidate" >&2
    fi
  fi
  echo "" >&2
}

if [[ ! -f "${CANDIDATES_FILE}" ]]; then
  echo "ERROR: Candidates file not found: ${CANDIDATES_FILE}" >&2
  exit 1
fi

while IFS= read -r line; do
  scan_line "${line}"
done < "${CANDIDATES_FILE}"

cat <<EOF >&2
All candidate scans completed.

Next steps for each NAME above:
  mirror-dedupe --config tests/unit-config --test NAME

If a repository is missing a GPG key in the generated config, identify the
correct key URL/path and re-run mirror-dedupe-scan for that NAME with
  --gpg-key-url / --gpg-key-path, then re-test.
EOF
