"""Tests for the immutable multi-project deployment registry."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ioscoverage.projects import ProjectRegistry, ProjectRegistryError
from ioscoverage.runtime import RuntimeConfigurationError, RuntimePaths
from ioscoverage.storage.coverage_store import CoverageStore
from ioscoverage.integrations.git_repository import GitRepositoryError
from ioscoverage.analysis.single_build.analyzer import AnalysisConfiguration


class RuntimeAndProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        for repository in (self.repo_a, self.repo_b):
            subprocess.run(["git", "init", "-q", str(repository)], check=True)

    def tearDown(self):
        self.temporary.cleanup()

    def payload(self):
        return {
            "schema_version": 1,
            "projects": [
                {
                    "project": "app-a", "display_name": "App A",
                    "repository_path": str(self.repo_a),
                    "gitlab": {"host": "git.example.test", "project_path": "mobile/app-a"},
                    "allowed_bundle_ids": ["com.example.a"],
                },
                {
                    "project": "app-b", "display_name": "App B",
                    "repository_path": str(self.repo_b),
                    "gitlab": {"host": "git.example.test", "project_path": "mobile/app-b"},
                    "allowed_bundle_ids": ["com.example.b"],
                },
            ],
        }

    def write(self, payload):
        path = self.root / "projects.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_runtime_root_alone_derives_every_mutable_path(self):
        paths = RuntimePaths.from_root(self.root / "deployment" / ".." / "deployment")
        self.assertTrue(paths.root.is_absolute())
        self.assertEqual(paths.database, paths.root / "instance" / "coverage.sqlite3")
        self.assertEqual(paths.operator_token, paths.root / "state" / "operator_token")
        self.assertEqual(paths.run_upload_token, paths.root / "state" / "run_upload_token")
        self.assertEqual(paths.gitlab_token, paths.root / "state" / "gitlab_api_token")
        self.assertEqual(paths.server_log, paths.root / "logs" / "server.log")

    def test_filesystem_root_cannot_be_used_as_runtime_root(self):
        with self.assertRaises(RuntimeConfigurationError):
            RuntimePaths.from_root(Path("/"))

    def test_two_projects_resolve_distinct_repositories_and_gitlab_addresses(self):
        registry = ProjectRegistry.load(self.write(self.payload()))
        self.assertEqual(registry.repository("app-a").path, self.repo_a.resolve())
        self.assertEqual(registry.repository("app-b").path, self.repo_b.resolve())
        self.assertEqual(registry.match_gitlab("git.example.test", "mobile/app-b").project, "app-b")
        self.assertEqual(registry.require("app-a").allowed_bundle_ids, ("com.example.a",))

    def test_unknown_fields_duplicates_paths_and_bundle_ids_are_rejected(self):
        cases = []
        unknown = self.payload(); unknown["extra"] = True; cases.append(unknown)
        duplicate_project = self.payload(); duplicate_project["projects"][1]["project"] = "app-a"; cases.append(duplicate_project)
        duplicate_address = self.payload(); duplicate_address["projects"][1]["gitlab"] = dict(duplicate_address["projects"][0]["gitlab"]); cases.append(duplicate_address)
        relative = self.payload(); relative["projects"][0]["repository_path"] = "repo-a"; cases.append(relative)
        missing_repo = self.payload(); missing_repo["projects"][0]["repository_path"] = str(self.root / "missing"); cases.append(missing_repo)
        empty_bundle = self.payload(); empty_bundle["projects"][0]["allowed_bundle_ids"] = []; cases.append(empty_bundle)
        duplicate_bundle = self.payload(); duplicate_bundle["projects"][0]["allowed_bundle_ids"] = ["x", "x"]; cases.append(duplicate_bundle)
        traversal = self.payload(); traversal["projects"][0]["repository_path"] = str(self.root / "x" / ".." / "repo-a"); cases.append(traversal)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ProjectRegistryError):
                ProjectRegistry.load(self.write(payload))

    def test_unknown_project_fails_without_repository_guessing(self):
        registry = ProjectRegistry.load(self.write(self.payload()))
        with self.assertRaises(ProjectRegistryError) as raised:
            registry.repository("missing")
        self.assertEqual(raised.exception.code, "project_not_registered")

    def test_same_mr_iid_is_unique_within_project_not_across_projects(self):
        registry = ProjectRegistry.load(self.write(self.payload()))
        store = CoverageStore(self.root / "runtime" / "instance" / "coverage.sqlite3",
                              self.root / "runtime" / "data", registry, object())
        base = {
            "schema_version": 1, "mr_iid": "56", "target_branch": "main",
            "base_sha": "a" * 40, "head_sha": "b" * 40,
        }
        first, replay = store.create_or_get_cycle(dict(base, project="app-a"))
        same, replay_same = store.create_or_get_cycle(dict(base, project="app-a"))
        other, replay_other = store.create_or_get_cycle(dict(base, project="app-b"))
        self.assertFalse(replay)
        self.assertTrue(replay_same)
        self.assertFalse(replay_other)
        self.assertEqual(first["id"], same["id"])
        self.assertNotEqual(first["id"], other["id"])
        listed = {item["project"]: item for item in store.list_cycles()}
        self.assertEqual(listed["app-a"]["project_display_name"], "App A")
        self.assertEqual(listed["app-a"]["gitlab_host"], "git.example.test")
        self.assertEqual(listed["app-a"]["gitlab_project_path"], "mobile/app-a")

    def test_analysis_context_and_git_objects_are_project_specific(self):
        registry = ProjectRegistry.load(self.write(self.payload()))
        shas = {}
        for name, repository in (("app-a", self.repo_a), ("app-b", self.repo_b)):
            subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.test"], check=True)
            (repository / "source.txt").write_text(name, encoding="utf-8")
            subprocess.run(["git", "-C", str(repository), "add", "source.txt"], check=True)
            subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", name], check=True)
            shas[name] = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"], check=True,
                stdout=subprocess.PIPE, text=True
            ).stdout.strip()
        configuration = AnalysisConfiguration(
            self.root / "runtime" / "instance" / "coverage.sqlite3",
            self.root / "runtime" / "data",
            self.root / "runtime" / "analysis" / "single-build",
            project_registry=registry,
        )
        self.assertEqual(configuration.project_repository_path("app-b"), self.repo_b.resolve())
        with self.assertRaises(GitRepositoryError) as raised:
            registry.repository("app-a").validate_build_snapshot(
                shas["app-b"], shas["app-a"], shas["app-a"]
            )
        self.assertEqual(raised.exception.code, "git_object_not_found")


if __name__ == "__main__":
    unittest.main()
