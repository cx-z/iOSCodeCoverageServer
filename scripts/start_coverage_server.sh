#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly LISTEN_HOST="0.0.0.0"
readonly LISTEN_PORT="8765"

if [[ $# -eq 1 && ("$1" == "-h" || "$1" == "--help") ]]; then
  echo "Usage: ${0##*/}"
  echo "Starts the iOS coverage platform using config/server.json."
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: ${0##*/}" >&2
  exit 2
fi

readonly LOCAL_BASE_URL="http://127.0.0.1:${LISTEN_PORT}"
if /usr/bin/curl --fail --silent --show-error --max-time 1 "${LOCAL_BASE_URL}/health" >/dev/null 2>&1; then
  echo "iOS coverage platform is already running at ${LOCAL_BASE_URL}"
  exit 0
fi
if /usr/sbin/lsof -nP -iTCP:"${LISTEN_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "error: port ${LISTEN_PORT} is already used" >&2
  exit 1
fi

lan_ip=""
for interface_name in en0 en1; do
  lan_ip="$(/usr/sbin/ipconfig getifaddr "${interface_name}" 2>/dev/null || true)"
  [[ -n "${lan_ip}" ]] && break
done
echo "Starting iOS coverage platform"
echo "Runtime configuration: ${PROJECT_ROOT}/config/server.json"
echo "Cycles: ${LOCAL_BASE_URL}/cycles/"
[[ -n "${lan_ip}" ]] && echo "LAN cycles: http://${lan_ip}:${LISTEN_PORT}/cycles/"
echo "Stop: press Control-C"

cd "${PROJECT_ROOT}"
exec /usr/bin/python3 "${PROJECT_ROOT}/server.py" \
  --host "${LISTEN_HOST}" \
  --port "${LISTEN_PORT}"
