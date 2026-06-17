#!/usr/bin/env bash
## @file list-codenames.sh
##
## @brief Print all unique Ubuntu and Debian release codenames (series).
##
## Fetches the distro-info-data CSV files for Ubuntu and Debian from the
## Debian project CDN and extracts the ``series`` column (short codename,
## e.g. ``noble``, ``bookworm``).  Output is sorted and deduplicated.
##
## Useful for generating scan candidate lists or verifying that a repo's
## discovered distributions match known release names.
##
## Usage:
##   bash scripts/list-codenames.sh
##
## Requires: curl, awk, sort (standard on all supported platforms).
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

set -euo pipefail

urls=(
 "https://debian.pages.debian.net/distro-info-data/ubuntu.csv"
 "https://debian.pages.debian.net/distro-info-data/debian.csv"
)

curl_opts=(-fSs --max-time 10 --parallel --parallel-immediate)

# Feed all curl output into a single awk process via one FIFO.
sort -u <(
  awk -F',' '$3 != "series" && $3 != "" {print $3}' <(
    curl "${curl_opts[@]}" "${urls[@]}"
  )
)
