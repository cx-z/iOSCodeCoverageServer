#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly LISTEN_HOST="0.0.0.0"
readonly LISTEN_PORT="8765"

if [[ $# -eq 1 && ("$1" == "-h" || "$1" == "--help") ]]; then
  echo "Usage: ${0##*/}"
  echo "Recompiles Python sources and restarts the background server using config/server.json."
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: ${0##*/}" >&2
  exit 2
fi

cd "${PROJECT_ROOT}"
readonly LOCAL_BASE_URL="http://127.0.0.1:${LISTEN_PORT}"
# Compile and validate log configuration first; failure leaves the old server untouched.
echo "Recompiling Python sources..."
/usr/bin/python3 -m compileall -q -f \
  "${PROJECT_ROOT}/server.py" "${PROJECT_ROOT}/ioscoverage" \
  "${PROJECT_ROOT}/scripts/stop_coverage_server.py"
CONSOLE_LOG="$(/usr/bin/python3 - <<'PY'
from ioscoverage.server.deployment import ServerDeploymentConfiguration
try:
    paths = ServerDeploymentConfiguration.load().runtime_paths
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    # Both files must be appendable before the old service is stopped.
    for log_file in (paths.server_log, paths.logs_dir / "server-console.log"):
        with log_file.open("a", encoding="utf-8"):
            pass
    print(paths.logs_dir / "server-console.log")
except (OSError, ValueError) as error:
    raise SystemExit(str(error))
PY
)"
readonly CONSOLE_LOG
# Check write permission before stopping the previous service.
: >> "${CONSOLE_LOG}"
"${PROJECT_ROOT}/stop_coverage_server.sh"

# Bound process inspection as well as HTTP polling; failure is never treated as readiness.
listener_owned() {
  /usr/bin/python3 - "$1" 2>/dev/null <<'PYCODE'
import sys
import subprocess
from scripts.stop_coverage_server import listener_pid
try:
    raise SystemExit(0 if listener_pid() == int(sys.argv[1]) else 1)
except (OSError, RuntimeError, subprocess.TimeoutExpired):
    raise SystemExit(1)
PYCODE
}

# Use an independent session/process group, not just a background job in this shell's group.
server_pid="$(/usr/bin/python3 - "${PROJECT_ROOT}" "${CONSOLE_LOG}" "${LISTEN_HOST}" "${LISTEN_PORT}" <<'PYCODE'
from pathlib import Path
import signal
import subprocess
import sys
root = Path(sys.argv[1])
signal.signal(signal.SIGHUP, signal.SIG_IGN)
with open(sys.argv[2], "ab", buffering=0) as console:
    process = subprocess.Popen(
        [sys.executable, "-u", str(root / "server.py"), "--host", sys.argv[3], "--port", sys.argv[4]],
        cwd=str(root), stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True,
    )
print(process.pid)
PYCODE
)"
readonly server_pid
readonly ready_deadline=$((SECONDS + 60))
while (( SECONDS < ready_deadline )); do
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    wait "${server_pid}" || true
    echo "error: server exited during startup; see ${CONSOLE_LOG}" >&2
    exit 1
  fi
  # Require this child to own the socket; another process's health response is insufficient.
  if listener_owned "${server_pid}" \
    && /usr/bin/curl --noproxy '*' --fail --silent --max-time 1 "${LOCAL_BASE_URL}/health" >/dev/null 2>&1 \
    && kill -0 "${server_pid}" 2>/dev/null; then
    echo "iOS coverage platform is running in the background (PID ${server_pid})"
    echo "Cycles: ${LOCAL_BASE_URL}/cycles/"
    /usr/bin/python3 - <<'PYCODE'
import subprocess
for interface in ("en0", "en1"):
    try:
        result = subprocess.run(["/usr/sbin/ipconfig", "getifaddr", interface],
                                capture_output=True, text=True, timeout=1, check=False)
    except (OSError, subprocess.TimeoutExpired):
        continue
    if result.returncode == 0 and result.stdout.strip():
        print("LAN cycles: http://{}:8765/cycles/".format(result.stdout.strip()))
        break
PYCODE
    echo "Log: ${CONSOLE_LOG%/*}/server.log"
    echo "Console/errors: ${CONSOLE_LOG}"
    echo "Stop: ${PROJECT_ROOT}/stop_coverage_server.sh"
    exit 0
  fi
  sleep 0.5
done
# Startup cleanup may still be running; do not interrupt it with a forced signal.
echo "error: server is not ready after 60 seconds; PID ${server_pid} remains in the background; see ${CONSOLE_LOG}" >&2
exit 1
