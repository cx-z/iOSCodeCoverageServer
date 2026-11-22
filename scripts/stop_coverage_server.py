"""Stop only this checkout's daily server through its coordinated SIGTERM path."""

import os
import ctypes
import struct
import re
from pathlib import Path
import signal
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LISTEN_PORT = 8765
SHUTDOWN_TIMEOUT_SECONDS = 60
INSPECTION_TIMEOUT_SECONDS = 3


def listener_pid():
    """Finds the unique local listener without trusting a stale PID file or health URL."""
    result = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-t", "-iTCP:{}".format(LISTEN_PORT), "-sTCP:LISTEN"],
        capture_output=True, text=True, check=False, timeout=INSPECTION_TIMEOUT_SECONDS,
    )
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RuntimeError("cannot inspect the local listening port")
    pids = set(result.stdout.split())
    if not pids:
        return None
    if len(pids) != 1 or not all(value.isdigit() for value in pids):
        raise RuntimeError("port {} has ambiguous ownership; no process was stopped".format(LISTEN_PORT))
    return int(pids.pop())


def process_identity(pid):
    """Reads a locale-stable birth time and full command for bounded PID-reuse checks."""
    result = subprocess.run(
        ["/bin/ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "lstart=", "-o", "command="],
        capture_output=True, text=True, check=False, env=dict(os.environ, LC_ALL="C"), timeout=INSPECTION_TIMEOUT_SECONDS,
    )
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return None
    if result.returncode != 0 or result.stderr.strip():
        raise RuntimeError("cannot inspect server process identity")
    parts = result.stdout.strip().split(None, 6)
    if len(parts) != 7:
        raise RuntimeError("cannot parse server process identity")
    state, birth, command = parts[0], tuple(parts[1:6]), parts[6]
    if state.startswith("Z"):
        return None
    return birth, command


def _process_arguments(pid):
    """Reads exact Darwin argv boundaries; never parses ps display text or exposes environment bytes."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    query = libc.sysctl
    query.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                      ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    query.restype = ctypes.c_int
    # CTL_KERN / KERN_PROCARGS2, defined by the macOS SDK sys/sysctl.h.
    mib = (ctypes.c_int * 3)(1, 49, pid)
    length = ctypes.c_size_t()
    if query(mib, 3, None, ctypes.byref(length), None, 0) != 0:
        raise RuntimeError("cannot inspect exact server arguments")
    if not 4 < length.value <= 4 * 1024 * 1024:
        raise RuntimeError("invalid process argument size")
    buffer = ctypes.create_string_buffer(length.value)
    if query(mib, 3, buffer, ctypes.byref(length), None, 0) != 0:
        raise RuntimeError("cannot inspect exact server arguments")
    return _decode_arguments(buffer.raw[:length.value])


def _decode_arguments(raw):
    """Decodes argc, executable path, padding and argv; ignores the following environment block."""
    if len(raw) < 4:
        raise RuntimeError("invalid process arguments")
    count = struct.unpack_from("=i", raw)[0]
    end = raw.find(b"\0", 4)
    if not 0 < count <= len(raw) or end < 0:
        raise RuntimeError("invalid process arguments")
    executable = os.fsdecode(raw[4:end])
    if not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(executable).name, re.IGNORECASE):
        raise RuntimeError("listener is not a Python executable; nothing was stopped")
    offset = end + 1
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    arguments = []
    for _ in range(count):
        end = raw.find(b"\0", offset)
        if end < 0:
            raise RuntimeError("incomplete process arguments")
        arguments.append(os.fsdecode(raw[offset:end]))
        offset = end + 1
    return arguments


def is_checkout_server(pid, identity):
    """Accepts exact native argv with a narrow interpreter-option allowlist before this script."""
    arguments = _process_arguments(pid)
    if not arguments or not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?",
                                          Path(arguments[0]).name, re.IGNORECASE):
        return False
    # All other options (including attached -cCODE/-mMODULE, -W and -X) fail closed.
    allowed_options = {"-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO"}
    script = None
    for index, argument in enumerate(arguments[1:], start=1):
        if argument == "--":
            script = arguments[index + 1] if index + 1 < len(arguments) else None
            break
        if argument.startswith("-"):
            if argument not in allowed_options:
                return False
            continue
        script = argument
        break
    if script is None:
        return False
    path = Path(script)
    if not path.is_absolute():
        result = subprocess.run(
            ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, check=False, timeout=INSPECTION_TIMEOUT_SECONDS,
        )
        directories = [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]
        if result.returncode != 0 or result.stderr.strip() or len(directories) != 1:
            return False
        path = Path(directories[0]) / path
    return path.resolve() == PROJECT_ROOT / "server.py"


def stop_server(timeout=SHUTDOWN_TIMEOUT_SECONDS):
    """Prints before the final identity check, then sends SIGTERM without intervening output."""
    pid = listener_pid()
    if pid is None:
        print("iOS coverage platform is not running on port {}".format(LISTEN_PORT), flush=True)
        return
    identity = process_identity(pid)
    if identity is None:
        return
    if not is_checkout_server(pid, identity):
        raise RuntimeError("port {} belongs to another process; nothing was stopped".format(LISTEN_PORT))
    print("Stopping iOS coverage platform (PID {})...".format(pid), flush=True)
    if process_identity(pid) != identity:
        raise RuntimeError("server process changed; nothing was stopped")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while process_identity(pid) == identity:
        if time.monotonic() >= deadline:
            raise RuntimeError("graceful shutdown timed out; no SIGKILL was sent and runtime locks were retained")
        time.sleep(0.2)
    print("iOS coverage platform stopped", flush=True)


def main():
    """Reports lifecycle errors without displaying process command lines or credentials."""
    try:
        stop_server()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print("error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
