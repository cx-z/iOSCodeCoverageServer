"""Daily script safety tests; never signal or restart the deployed service."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import signal
import re
import struct
import sys
import time
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import stop_coverage_server as control


class ServerControlTests(unittest.TestCase):
    """Verifies exact process ownership, bounded stop and compile-before-stop ordering."""

    def test_no_listener_is_idempotent(self):
        """Stopping an absent service succeeds without sending any signal."""
        with patch.object(control, "listener_pid", return_value=None), patch.object(control.os, "kill") as kill, redirect_stdout(io.StringIO()):
            control.stop_server()
            kill.assert_not_called()

    def test_ownership_matches_exact_checkout(self):
        """Uses exact native argv, including quoted/space-containing paths and narrow options."""
        target = str(control.PROJECT_ROOT / "server.py")
        for arguments, expected in [
            (["python3", target], True), (["python-helper", target], False), (["python3", "-u", target], True),
            (["python3", "--", target], True), (["python3", "-B", "-u", target], True),
            (["python3", "-c", "code", target], False),
            (["python3", "-c__import__('http.server').test(port=8765)", target], False),
            (["python3", "-mhttp.server", target], False),
            (["python3", "-umhttp.server", target], False),
            (["python3", "-X", target], False), (["python3", "-Wignore", target], False),
            (["python3", "/another/server.py"], False), (["python3", "--"], False),
        ]:
            with self.subTest(arguments=arguments), patch.object(control, "_process_arguments", return_value=arguments):
                self.assertEqual(control.is_checkout_server(123, ((), "untrusted display")), expected)
        cwd = subprocess.CompletedProcess([], 0, stdout="p123\nn{}\n".format(control.PROJECT_ROOT), stderr="")
        with patch.object(control, "_process_arguments", return_value=["python3", "-u", "server.py"]), patch.object(control.subprocess, "run", return_value=cwd):
            self.assertTrue(control.is_checkout_server(123, ((), "display")))
        root = Path("/example/path with 'quotes' and spaces")
        with patch.object(control, "PROJECT_ROOT", root), patch.object(control, "_process_arguments", return_value=["/example/Python", str(root / "server.py")]):
            self.assertTrue(control.is_checkout_server(123, ((), "lossy display")))

    def test_native_argv_does_not_parse_environment_or_shell_quotes(self):
        """Keeps exact argc boundaries, empty arguments and literal quote characters."""
        args = [b"python3", b"/path with 'quotes'/server.py", b"", b"--port", b"8765"]
        raw = struct.pack("=i", len(args)) + b"/usr/bin/python3\0\0\0" + b"\0".join(args) + b"\0SECRET=not-an-argument\0"
        self.assertEqual(control._decode_arguments(raw), [os.fsdecode(arg) for arg in args])
        for damaged in [b"", struct.pack("=i", -1) + b"x\0", raw[:20]]:
            with self.assertRaises(RuntimeError):
                control._decode_arguments(damaged)
        spoofed = struct.pack("=i", 2) + b"/usr/bin/not-python\0\0python3\0/server.py\0"
        with self.assertRaises(RuntimeError):
            control._decode_arguments(spoofed)
        self.assertTrue(control._process_arguments(os.getpid()))

    def test_native_attached_execution_is_rejected(self):
        """An actual Python -cCODE process cannot claim ownership through a positional server.py."""
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            code = "import pathlib,time; pathlib.Path({!r}).touch(); time.sleep(2)".format(str(ready))
            process = subprocess.Popen([sys.executable, "-c" + code, str(control.PROJECT_ROOT / "server.py")])
            try:
                deadline = time.monotonic() + 5
                while not ready.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail("owned argv fixture did not initialize")
                    time.sleep(0.01)
                self.assertFalse(control.is_checkout_server(process.pid, ((), "display")))
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)

    def test_unrelated_listener_is_never_signalled(self):
        """Port ownership alone is insufficient authority to kill a process."""
        with patch.object(control, "_process_arguments", return_value=["python3", "/other/server.py"]), patch.object(control, "listener_pid", return_value=123), patch.object(control, "process_identity", return_value=((), "python3 /other/server.py")), patch.object(control.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "another process"):
                control.stop_server()
            kill.assert_not_called()

    def test_sigterm_once_and_reused_pid_not_signalled(self):
        """Waits for the original identity without signalling a process that later reuses its PID."""
        original, replacement = (("birth",), "python3 server.py"), (("later",), "python3 other.py")
        with patch.object(control, "listener_pid", return_value=123), patch.object(control, "process_identity", side_effect=[original, original, original, replacement]), patch.object(control, "is_checkout_server", return_value=True), patch.object(control.os, "kill") as kill, patch.object(control.time, "sleep"), redirect_stdout(io.StringIO()):
            control.stop_server()
            kill.assert_called_once_with(123, signal.SIGTERM)

    def test_timeout_never_escalates(self):
        """A hung graceful stop fails without SIGKILL or runtime-lock removal."""
        with patch.object(control, "listener_pid", return_value=123), patch.object(control, "process_identity", return_value=((), "python3 server.py")), patch.object(control, "is_checkout_server", return_value=True), patch.object(control.os, "kill") as kill, redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                control.stop_server(timeout=0)
            kill.assert_called_once_with(123, signal.SIGTERM)

    def test_identity_recheck_prevents_wrong_signal(self):
        """A process change before signalling aborts the request."""
        with patch.object(control, "listener_pid", return_value=123), patch.object(control, "process_identity", side_effect=[("old", "cmd"), ("new", "cmd")]), patch.object(control, "is_checkout_server", return_value=True), patch.object(control.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "changed"):
                control.stop_server()
            kill.assert_not_called()

    def test_listener_and_process_inspection_fail_closed(self):
        """Ambiguous listeners or inspection errors cannot silently become an absent server."""
        for output, error, code in [("123\n456\n", "", 0), ("", "permission denied", 1), ("", "", 2)]:
            with self.subTest(output=output, code=code), patch.object(control.subprocess, "run", return_value=subprocess.CompletedProcess([], code, stdout=output, stderr=error)):
                with self.assertRaises(RuntimeError):
                    control.listener_pid()
        command = "S Sun Nov 22 12:00:00 2026 python3 /path/server.py --port 8765\n"
        with patch.object(control.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=command, stderr="")):
            birth, invocation = control.process_identity(123)
            self.assertEqual(birth, ("Sun", "Nov", "22", "12:00:00", "2026"))
            self.assertEqual(invocation, "python3 /path/server.py --port 8765")

    def test_inspection_timeouts_fail_without_signalling(self):
        """Inspection has a bounded timeout and never converts an inspection stall to permission to kill."""
        def blocked(arguments, **options):
            """Models a hung native inspection command and checks the configured bound."""
            self.assertEqual(options.get("timeout"), control.INSPECTION_TIMEOUT_SECONDS)
            raise subprocess.TimeoutExpired(arguments, control.INSPECTION_TIMEOUT_SECONDS)

        with patch.object(control.subprocess, "run", side_effect=blocked), patch.object(control.os, "kill") as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                control.stop_server()
            kill.assert_not_called()

    def test_chinese_terminal_does_not_shift_process_command(self):
        """Forces stable ps dates even when the calling terminal uses a Chinese locale."""
        executable = "/Applications/Xcode263.app/Contents/MacOS/Python"
        command = executable + " -u " + str(control.PROJECT_ROOT / "server.py") + " --port 8765"

        def localized_ps(arguments, **options):
            """Models actual macOS ps field layouts observed under C and Chinese locales."""
            locale = options.get("env", {}).get("LC_ALL", os.environ.get("LC_ALL"))
            date = "Sun Nov 22 21:13:13 2026" if locale == "C" else "日 11月/22 21:13:13 2026"
            return subprocess.CompletedProcess(arguments, 0, stdout="S " + date + " " + command + "\n", stderr="")

        with patch.object(control, "_process_arguments", return_value=[executable, "-u", str(control.PROJECT_ROOT / "server.py")]), patch.dict(os.environ, {"LC_ALL": "zh_CN.UTF-8"}), patch.object(control.subprocess, "run", side_effect=localized_ps):
            identity = control.process_identity(123)
            self.assertEqual(identity[1], command)
            self.assertTrue(control.is_checkout_server(123, identity))
        with patch.object(control, "_process_arguments", return_value=[executable, "/unrelated/server.py"]):
            self.assertFalse(control.is_checkout_server(123, ((), "display")))

    def test_background_start_preserves_failure_order_and_detaches_output(self):
        """Runs isolated copied scripts with fake socket probes and an owned dummy service."""
        source = control.PROJECT_ROOT / "scripts/start_coverage_server.sh"
        for failure in ("compile", "log", "log-permission", "console-permission", "stop", "process", "notready", None):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "scripts").mkdir()
                (root / "ioscoverage/server").mkdir(parents=True)
                (root / "ioscoverage/__init__.py").touch()
                (root / "ioscoverage/server/__init__.py").touch()
                (root / "scripts/stop_coverage_server.py").write_text("# fixture\n")
                (root / "ioscoverage/module.py").write_text("def broken(:\n" if failure == "compile" else "VALUE = 1\n")
                (root / "ioscoverage/server/deployment.py").write_text(
                    "from pathlib import Path\nfrom types import SimpleNamespace\n"
                    "class ServerDeploymentConfiguration:\n"
                    "    @staticmethod\n    def load():\n"
                    "        root = Path(__file__).resolve().parents[2]\n"
                    "        return SimpleNamespace(runtime_paths=SimpleNamespace(logs_dir=root/'runtime/logs', server_log=root/'runtime/logs/server.log'))\n"
                )
                if failure == "log":
                    (root / "runtime/logs/server.log").mkdir(parents=True)
                existing_log = None
                blocked_log = None
                if failure in ("log-permission", "console-permission"):
                    name = "server.log" if failure == "log-permission" else "server-console.log"
                    blocked_log = root / "runtime/logs" / name
                    blocked_log.parent.mkdir(parents=True, exist_ok=True)
                    existing_log = blocked_log.open("a")
                    blocked_log.chmod(0o400)
                stop = root / "stop_coverage_server.sh"
                stop.write_text("#!/bin/bash\necho STOP_MARKER\n" + ("exit 1\n" if failure == "stop" else ""))
                stop.chmod(0o755)
                ready = root / "ready"
                probe = root / "socket-probe"
                probe.write_text('#!/bin/bash\ntest -f "{}"\n'.format(ready))
                probe.chmod(0o755)
                health = root / "health-probe"
                health.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "' + str(root / "health-args") + '"\nexit 0\n')
                health.chmod(0o755)
                script = root / "scripts/start_coverage_server.sh"
                script.write_text(source.read_text().replace('listener_owned "${server_pid}"', str(probe)).replace("/usr/bin/curl", str(health)).replace("SECONDS + 60", "SECONDS + 2"))
                program = "import time,sys\nfrom pathlib import Path\nprint('NEW_CODE_LOADED',flush=True)\nprint('RAW_ERROR_OUTPUT',file=sys.stderr,flush=True)\n"
                if failure == "process":
                    program += "raise SystemExit(3)\n"
                else:
                    if failure != "notready":
                        program += "Path({!r}).touch()\n".format(str(ready))
                    program += "while True: time.sleep(0.1)\n"
                (root / "server.py").write_text(program)
                pid = None
                try:
                    result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, timeout=10,
                                            env=dict(os.environ, PYTHONPYCACHEPREFIX=str(root / "pycache")))
                    match = re.search(r"PID (\d+)", result.stdout + result.stderr)
                    pid = int(match.group(1)) if match else None
                    if failure in ("compile", "log", "log-permission", "console-permission"):
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn("STOP_MARKER", result.stdout)
                        if existing_log is not None:
                            existing_log.write("old writer remains active\n")
                            existing_log.flush()
                    elif failure == "stop":
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("STOP_MARKER", result.stdout)
                        self.assertFalse(ready.exists())
                    elif failure in ("process", "notready"):
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("see ", result.stderr)
                        if failure == "notready":
                            self.assertIsNotNone(pid)
                            os.kill(pid, 0)
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIsNotNone(pid)
                        os.kill(pid, 0)
                        self.assertEqual(os.getsid(pid), pid)
                        os.kill(pid, signal.SIGHUP)
                        os.kill(pid, 0)
                        self.assertIn("background", result.stdout)
                        self.assertNotIn("NEW_CODE_LOADED", result.stdout)
                        self.assertIn("--noproxy\n*\n", (root / "health-args").read_text())
                    if failure not in ("compile", "log", "log-permission", "console-permission", "stop"):
                        console = (root / "runtime/logs/server-console.log").read_text()
                        self.assertIn("NEW_CODE_LOADED", console)
                        self.assertIn("RAW_ERROR_OUTPUT", console)
                finally:
                    if existing_log is not None:
                        existing_log.close()
                        blocked_log.chmod(0o600)
                    if pid is not None:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
