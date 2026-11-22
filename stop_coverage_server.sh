#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ $# -eq 1 && ("$1" == "-h" || "$1" == "--help") ]]; then
  echo "Usage: ${0##*/}"
  echo "Gracefully stops this checkout's daily server on port 8765."
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: ${0##*/}" >&2
  exit 2
fi
exec /usr/bin/python3 "${PROJECT_ROOT}/scripts/stop_coverage_server.py"
