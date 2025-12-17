#!/usr/bin/env bash

# List all unique Ubuntu and Debian short codenames (series) from
# distro-info-data's ubuntu.csv and debian.csv.
#
# Usage:
#   scripts/list-codenames.sh
#
# This fetches both CSVs in parallel via curl, strips the header row
# from each, extracts the third column (series), and prints a
# de-duplicated, sorted list.

set -euo pipefail

ubuntu_csv="https://debian.pages.debian.net/distro-info-data/ubuntu.csv"
debian_csv="https://debian.pages.debian.net/distro-info-data/debian.csv"

# Common curl options: fail on HTTP errors, be quiet, and avoid hanging
# indefinitely if GitHub or the network is slow.
curl_opts=(
  -fS        # fail on HTTP errors, show errors
  -s         # silent (no progress)
  --max-time 10  # overall timeout in seconds
)

cut -d',' -f3 \
  <(curl "${curl_opts[@]}" "${ubuntu_csv}" | tail -n +2) \
  <(curl "${curl_opts[@]}" "${debian_csv}" | tail -n +2) \
| sort -u
