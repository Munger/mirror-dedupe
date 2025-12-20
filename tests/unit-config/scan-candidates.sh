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

# Scanner command (can be overridden). By default we invoke the scan
# module via the current Python, which is typically the project's venv.
SCAN_CMD="${SCAN_CMD:-python -m mirror_dedupe.scan}"

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

  local name dest
  name=${fields[0]}
  dest=${fields[1]}

  # All remaining fields are passed through as scanner flags. Upstreams
  # are expected to be supplied explicitly via -U/--upstream/--upstreams
  # in this flags portion rather than as a positional argument.
  local -a extras=()
  if (( count > 2 )); then
    extras=(${fields[@]:2})
  fi

  # The new scanner no longer supports --gpg-key-path; strip any
  # occurrences from the extras list while preserving order of the
  # remaining arguments.
  if (( ${#extras[@]} > 0 )); then
    local -a filtered=()
    local skip_next=0
    for arg in "${extras[@]}"; do
      if (( skip_next )); then
        skip_next=0
        continue
      fi
      if [[ "${arg}" == "--gpg-key-path" ]]; then
        skip_next=1
        continue
      fi
      filtered+=("${arg}")
    done
    extras=("${filtered[@]}")
  fi

  echo "=== Scanning ${name} ===" >&2
  if ! ${SCAN_CMD} \
    --config "${CONFIG_DIR}" \
    --name "${name}" \
    --dest "${dest}" \
    "${extras[@]}"; then
    echo "ERROR: scan for ${name} failed (see above); continuing with next candidate" >&2
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
