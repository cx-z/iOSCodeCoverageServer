"""Architecture compatibility checks for the packaged implementation."""

import importlib
import os
import pkgutil
import subprocess
import unittest
from pathlib import Path

import ioscoverage
SOURCE_ROOT = Path(__file__).resolve().parents[1]


class ArchitectureCompatibilityTests(unittest.TestCase):
    """Protects package discoverability, thin roots, and stable installation paths."""

    def test_all_package_modules_import(self) -> None:
        """Imports every source module without relying on root-level aliases."""

        names = sorted(
            item.name
            for item in pkgutil.walk_packages(
                ioscoverage.__path__, prefix="ioscoverage."
            )
        )
        self.assertGreater(len(names), 30)
        for name in names:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_legacy_package_is_absent_from_paths_and_imports(self) -> None:
        """Prevents the unpublished former package name from remaining as an alias."""

        legacy_package = "".join(("k", "kcoverage"))
        self.assertFalse((SOURCE_ROOT / legacy_package).exists())
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("import " + legacy_package, source)
            self.assertNotIn("from " + legacy_package, source)

    def test_root_compatibility_entry_help(self) -> None:
        """Keeps every documented root command available after packaging."""

        commands = [
            "server.py",
            "analyze_single_build.py",
            "analyze_single_build_diff.py",
            "analyze_cycle_coverage.py",
            "analyze_cycle_latest.py",
            "cleanup_runtime_data.py",
            "manage_users.py",
        ]
        environment = dict(os.environ)
        environment["PYTHONPYCACHEPREFIX"] = "/private/tmp/ios-coverage-tests-pycache"
        for command in commands:
            with self.subTest(command=command):
                completed = subprocess.run(
                    ["python3", str(SOURCE_ROOT / command), "--help"],
                    cwd=str(SOURCE_ROOT),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

        start_help = subprocess.run(
            [str(SOURCE_ROOT / "start_coverage_server.sh"), "--help"],
            cwd=str(SOURCE_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(start_help.returncode, 0, start_help.stderr)
        self.assertIn("config/server.json", start_help.stdout)

    def test_start_script_rejects_legacy_runtime_argument(self) -> None:
        """Keeps daily startup zero-argument and prevents configuration ambiguity."""

        completed = subprocess.run(
            [str(SOURCE_ROOT / "start_coverage_server.sh"), "/tmp/legacy-runtime"],
            cwd=str(SOURCE_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("usage: start_coverage_server.sh", completed.stderr)

    def test_local_runtime_pointer_is_ignored_and_example_is_versionable(self) -> None:
        """Separates the developer's machine path from the committed configuration example."""

        ignore_rules = (SOURCE_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/config/server.json", ignore_rules)
        example = (SOURCE_ROOT / "config/server.example.json").read_text(encoding="utf-8")
        self.assertIn('"runtime_root": ""', example)

    def test_runtime_results_are_ignored_without_hiding_analysis_source(self) -> None:
        """Keeps deployment output private while preserving analyzer source visibility."""

        ignored = subprocess.run(
            ["git", "-C", str(SOURCE_ROOT), "check-ignore", "analysis/result.json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        source = subprocess.run(
            ["git", "-C", str(SOURCE_ROOT), "check-ignore", "ioscoverage/analysis/orchestrator.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertEqual(source.returncode, 1, source.stderr)

    def test_source_tree_contains_no_mutable_runtime_directories(self) -> None:
        """Keeps deployment state outside the independently movable source tree."""

        for name in ("state", "instance", "data", "analysis", "logs"):
            self.assertFalse((SOURCE_ROOT / name).exists())

    def test_root_python_files_are_thin_compatibility_entries(self) -> None:
        """Prevents business logic from drifting back into documented root commands."""

        for name in (
            "server.py",
            "analyze_single_build.py",
            "analyze_single_build_diff.py",
            "analyze_cycle_coverage.py",
            "analyze_cycle_latest.py",
            "cleanup_runtime_data.py",
            "manage_users.py",
        ):
            with self.subTest(name=name):
                lines = (SOURCE_ROOT / name).read_text(encoding="utf-8").splitlines()
                self.assertLessEqual(len(lines), 15)

    def test_web_projection_has_no_storage_or_token_dependency(self) -> None:
        """Keeps templates and browser projection independent of persistence secrets."""

        for path in sorted((SOURCE_ROOT / "ioscoverage/web").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("sqlite3", source)
            self.assertNotIn("TokenStore", source)
            self.assertNotIn("coverage_store", source)

    def test_production_source_has_no_single_project_or_installation_path_guess(self) -> None:
        """Prevents regressions to a source-adjacent repository or fixed product artifacts."""

        forbidden = (
            "/path/to/ExampleApp",
            'INSTALLATION_ROOT.parent / "ExampleApp"',
            "mobile/example-app",
            '"ExampleApp.debug.dylib"',
            '"ExampleApp.app.dSYM.zip"',
        )
        for path in sorted((SOURCE_ROOT / "ioscoverage").rglob("*")):
            if path.suffix not in (".py", ".html", ".js", ".css"):
                continue
            source = path.read_text(encoding="utf-8")
            for value in forbidden:
                with self.subTest(path=path, value=value):
                    self.assertNotIn(value, source)

    def test_analysis_subprocess_boundaries_propagate_runtime_lease(self) -> None:
        """Keeps parent-crash ownership on every controlled Git and LLVM process."""

        boundaries = (
            "ioscoverage/analysis/process_orchestrator.py",
            "ioscoverage/analysis/command_runner.py",
            "ioscoverage/integrations/git_repository.py",
            "ioscoverage/integrations/xcode_artifacts.py",
        )
        for relative_path in boundaries:
            with self.subTest(path=relative_path):
                source = (SOURCE_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("pass_fds=inherited_descriptors", source)


if __name__ == "__main__":
    unittest.main()
