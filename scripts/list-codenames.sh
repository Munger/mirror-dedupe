#!/usr/bin/env bash

# List all unique Ubuntu and Debian short codenames (series).

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
