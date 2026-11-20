"""Server lifecycle, polling and missing-object repair regressions."""

import io
import logging
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from ioscoverage.integrations.git_sync import RepositorySyncError
from ioscoverage.integrations.gitlab_mr import GitLabMRClientError
from ioscoverage.projects import ProjectRegistry
from ioscoverage.runtime import RuntimePaths
from ioscoverage.server.bootstrap import create_server
from ioscoverage.server.http.routes.builds import BuildRoutesMixin
from ioscoverage.server.http.server import ProbeHTTPServer
from ioscoverage.server.repository_sync import RepositorySyncService
from ioscoverage.storage.coverage_store import CoverageStore, CoverageStoreError
from tests.integrations.test_git_sync import GitSyncWorkspace


class RepositorySyncServiceTests(unittest.TestCase):
    """Exercises real Git and SQLite through the server-owned background worker."""

    def setUp(self):
        """Creates one stale registered project with a persisted testing Cycle."""

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = GitSyncWorkspace(self.root)
        self.store = CoverageStore(self.root / "db.sqlite3", self.root / "data", self.workspace.registry, None)
        self.cycle = self.store.create_cycle({
            "schema_version": 1, "project": "app-a", "mr_iid": "42",
            "mr_url": self.workspace.mr.web_url, "target_branch": "main",
            "base_sha": self.workspace.base, "head_sha": self.workspace.base,
        })
        self.gitlab = SimpleNamespace(fetch=lambda url: self.workspace.mr)
        logger = logging.getLogger("sync-tests")
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        self.service = RepositorySyncService(self.store, self.gitlab, logger, interval_seconds=0.1)

    def tearDown(self):
        """Stops the repository owner before removing its temporary inputs."""

        self.service.shutdown()
        self.temporary.cleanup()

    def test_startup_and_periodic_fetch_work_without_any_browser_request(self):
        """Finds existing Cycles after startup and later picks up a target-only push."""

        self.service.start()
        self.wait_until(lambda: self.workspace.has_commit(self.workspace.head))
        self.workspace.git(self.workspace.developer, "checkout", "-q", "main")
        target = self.workspace.commit("base\ntarget only\n")
        self.workspace.git(self.workspace.developer, "push", "-q", "origin", "main")
        self.wait_until(lambda: self.workspace.has_commit(target))
        after = self.store.get_cycle(self.cycle["id"])
        for key in ("head_sha", "base_sha", "updated_at"):
            self.assertEqual(after[key], self.cycle[key])

    def test_failed_mr_is_retried_on_a_later_sweep(self):
        """Keeps the worker alive after GitLab failure and recovers on a later sweep."""

        calls = []
        def fetch(url):
            """Fails the first lookup and then returns a valid registered identity."""

            calls.append(url)
            if len(calls) == 1:
                raise GitLabMRClientError("gitlab_timeout", "private upstream data", 504)
            return self.workspace.mr
        self.service.gitlab_client = SimpleNamespace(fetch=fetch)
        self.service.start()
        self.wait_until(lambda: self.workspace.has_commit(self.workspace.head))
        self.assertGreaterEqual(len(calls), 2)

    def test_removed_project_cycle_does_not_block_healthy_project_discovery(self):
        """Keeps historical orphaned Cycles without aborting startup or periodic discovery."""

        other = GitSyncWorkspace(self.root, "app-b")
        registry = ProjectRegistry((*self.workspace.registry.projects, *other.registry.projects))
        expanded = CoverageStore(self.store.database_path, self.store.data_dir, registry, None)
        orphan = expanded.create_cycle({
            "schema_version": 1, "project": "app-b", "mr_iid": "42",
            "mr_url": other.mr.web_url, "target_branch": "main",
            "base_sha": other.base, "head_sha": other.base,
        })
        # self.store models the next startup, whose registry no longer contains B.
        # The existing user-facing list must retain its explicit rejection behavior.
        with self.assertRaises(CoverageStoreError) as caught:
            self.store.list_cycles()
        self.assertEqual(caught.exception.code, "project_not_registered")
        fetch = Mock(return_value=self.workspace.mr)
        self.service.gitlab_client = SimpleNamespace(fetch=fetch)
        self.service.start()
        self.wait_until(lambda: self.workspace.has_commit(self.workspace.head))
        with self.assertRaises(RepositorySyncError) as caught:
            self.service.request_cycle(orphan["id"]).result(timeout=3)
        self.assertEqual(caught.exception.code, "project_not_registered")
        self.assertFalse(other.has_commit(other.head))
        self.assertTrue(all(call.args == (self.workspace.mr.web_url,) for call in fetch.call_args_list))
        self.workspace.git(self.workspace.developer, "checkout", "-q", "main")
        target = self.workspace.commit("base\ntarget after orphan\n")
        self.workspace.git(self.workspace.developer, "push", "-q", "origin", "main")
        self.wait_until(lambda: self.workspace.has_commit(target))

    def test_identical_requests_coalesce_and_shutdown_cancels_queued_waiters(self):
        """Shares running work without holding the condition during Git/network work."""

        entered, release = threading.Event(), threading.Event()
        def fetch(url):
            """Holds one lookup so concurrent enqueue and shutdown can be observed."""

            entered.set()
            release.wait(timeout=5)
            return self.workspace.mr
        self.service.gitlab_client = SimpleNamespace(fetch=fetch)
        first = self.service.request_cycle(self.cycle["id"])
        self.service.start()
        self.assertTrue(entered.wait(timeout=3))
        self.assertIs(first, self.service.request_cycle(self.cycle["id"]))
        queued = self.service.request_cycle(self.cycle["id"], (self.workspace.head,))
        self.service._cancellation.set()
        release.set()
        self.service.shutdown()
        with self.assertRaises(RepositorySyncError):
            queued.result(timeout=1)
        with self.assertRaises(RepositorySyncError):
            self.service.request_cycle(self.cycle["id"])
        self.assertFalse(self.workspace.has_commit(self.workspace.head))

    def test_mismatched_cycle_identity_cannot_synchronize_another_project(self):
        """Checks the Cycle against the validated MR before starting Git writes."""

        self.service.gitlab_client = SimpleNamespace(fetch=lambda url: replace(self.workspace.mr, iid=99))
        future = self.service.request_cycle(self.cycle["id"])
        self.service.start()
        with self.assertRaises(RepositorySyncError) as caught:
            future.result(timeout=3)
        self.assertEqual(caught.exception.code, "repository_sync_mr_mismatch")
        self.assertFalse(self.workspace.has_commit(self.workspace.head))

    def test_closed_mr_does_not_fetch_and_reopening_can_be_observed(self):
        """Skips a closed MR without changing the Cycle or permanently forgetting it."""

        self.service.gitlab_client = SimpleNamespace(fetch=Mock(side_effect=GitLabMRClientError("mr_not_open", "closed", 409)))
        future = self.service.request_cycle(self.cycle["id"])
        self.service.start()
        with self.assertRaises(RepositorySyncError):
            future.result(timeout=3)
        self.assertFalse(self.workspace.has_commit(self.workspace.head))
        self.service.gitlab_client = self.gitlab
        self.service.request_cycle(self.cycle["id"]).result(timeout=3)
        self.assertTrue(self.workspace.has_commit(self.workspace.head))

    @staticmethod
    def wait_until(predicate, timeout=5):
        """Waits for observable Git state with a bounded fixture deadline."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("background synchronization did not complete")


class BuildRegistrationSyncTests(unittest.TestCase):
    """Checks repair at the authorized HTTP boundary rather than inside storage."""

    def handler(self, store, synchronizer):
        """Builds a narrow handler fixture with stable multipart streams and response capture."""

        handler = BuildRoutesMixin()
        manifest = {"cycle_id": str(uuid.uuid4()), "git_sha": "a"*40,
                    "target_sha_at_build": "b"*40, "merge_base_sha": "c"*40}
        handler.config = SimpleNamespace(max_build_artifact_bytes=1024,
            coverage_store=store, repository_sync_service=synchronizer)
        handler._read_multipart_body = Mock(return_value=({"manifest": "{}"}, {
            "main_executable": SimpleNamespace(filename="App", file=io.BytesIO(b"artifact"))}))
        handler._decode_json_field = Mock(return_value=manifest)
        handler._log_event = Mock()
        handler._send_json = Mock()
        handler._send_domain_error = Mock()
        handler._request_analysis_safely = Mock()
        return handler, manifest

    def test_missing_objects_sync_once_then_retry_with_original_manifest_and_stream(self):
        """Preserves immutable Build inputs and does not request a synthetic analysis."""

        store, synchronizer = Mock(), Mock()
        build = {"id": str(uuid.uuid4()), "cycle_id": str(uuid.uuid4()),
                 "artifact_state": "ready", "pending_runs_promoted": 0}
        store.register_build.side_effect = [CoverageStoreError("git_object_not_found", "missing"), (build, False)]
        handler, manifest = self.handler(store, synchronizer)
        handler._handle_build_register()
        synchronizer.ensure_cycle.assert_called_once_with(manifest["cycle_id"], ("a"*40, "b"*40, "c"*40))
        self.assertEqual(store.register_build.call_args_list[0], store.register_build.call_args_list[1])
        self.assertEqual(handler._send_json.call_args.args[0], 201)
        handler._request_analysis_safely.assert_not_called()

    def test_sync_failure_returns_safe_503_without_retry_or_success(self):
        """Keeps upstream failures distinct from invalid artifacts or Git snapshot mismatches."""

        store, synchronizer = Mock(), Mock()
        store.register_build.side_effect = CoverageStoreError("git_object_not_found", "missing")
        synchronizer.ensure_cycle.side_effect = RepositorySyncError("repository_sync_failed", "safe failure")
        handler, _ = self.handler(store, synchronizer)
        handler._handle_build_register()
        self.assertEqual(store.register_build.call_count, 1)
        self.assertEqual(handler._send_json.call_args.args[0], 503)

    def test_repair_normalizes_sha_arguments_without_mutating_original_manifest(self):
        """Accepts the same case-insensitive snapshots as the storage validation path."""

        store, synchronizer = Mock(), Mock()
        build = {"id": str(uuid.uuid4()), "cycle_id": str(uuid.uuid4()),
                 "artifact_state": "ready", "pending_runs_promoted": 0}
        store.register_build.side_effect = [CoverageStoreError("git_object_not_found", "missing"), (build, False)]
        handler, manifest = self.handler(store, synchronizer)
        manifest.update(git_sha="A"*40, target_sha_at_build="bB"*20, merge_base_sha="C"*40)
        original = dict(manifest)
        handler._handle_build_register()
        synchronizer.ensure_cycle.assert_called_once_with(manifest["cycle_id"], ("a"*40, "b"*40, "c"*40))
        self.assertEqual(manifest, original)
        self.assertEqual(store.register_build.call_args_list[0], store.register_build.call_args_list[1])

    def test_other_validation_errors_never_trigger_fetch(self):
        """Does not hide artifact failures or merge-base conflicts behind network retries."""

        for code in ("merge_base_mismatch", "invalid_git_sha", "artifact_uuid_mismatch"):
            store, synchronizer = Mock(), Mock()
            store.register_build.side_effect = CoverageStoreError(code, "invalid")
            handler, _ = self.handler(store, synchronizer)
            handler._handle_build_register()
            synchronizer.ensure_cycle.assert_not_called()
            handler._send_domain_error.assert_called_once()


class RepositorySyncLifecycleTests(unittest.TestCase):
    """Protects worker ownership when HTTP construction or coordinated close fails."""

    def test_create_server_starts_sync_for_persisted_cycles_by_default(self):
        """Exercises production composition without installing a service into the config manually."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = GitSyncWorkspace(root)
            paths = RuntimePaths.from_root(root / "deployment")
            store = CoverageStore(paths.database, paths.data_dir, workspace.registry, None)
            store.create_cycle({
                "schema_version": 1, "project": "app-a", "mr_iid": "42",
                "mr_url": workspace.mr.web_url, "target_branch": "main",
                "base_sha": workspace.base, "head_sha": workspace.base,
            })
            client = SimpleNamespace(fetch=lambda url: workspace.mr)
            server = create_server("127.0.0.1", 0, "fixture-operator", paths, workspace.registry,
                                   gitlab_mr_client=client, start_analysis_scheduler=False,
                                   repository_sync_interval_seconds=0.1)
            try:
                self.assertIsInstance(server.config.repository_sync_service, RepositorySyncService)
                RepositorySyncServiceTests.wait_until(lambda: workspace.has_commit(workspace.head))
            finally:
                server.server_close()
            self.assertFalse(server.config.repository_sync_service._worker.is_alive())

    def test_sync_close_failure_still_stops_analysis_and_remains_an_error(self):
        """Does not publish successful service close while Git execution is unresolved."""

        synchronizer, scheduler = Mock(), Mock()
        synchronizer.shutdown.side_effect = RepositorySyncError("repository_sync_termination_failed", "still active")
        scheduler.shutdown.return_value = True
        config = SimpleNamespace(repository_sync_service=synchronizer,
                                 analysis_scheduler=scheduler, runtime_cleanup_manager=None)
        server = ProbeHTTPServer(("127.0.0.1", 0), config)
        with self.assertRaises(RepositorySyncError):
            server.server_close()
        scheduler.shutdown.assert_called_once_with(timeout=30.0)
        self.assertEqual(server.fileno(), -1)

    def test_sync_start_failure_closes_its_owner_and_existing_analysis_worker(self):
        """Rolls back all started workers if sync startup cannot complete."""

        synchronizer, scheduler = Mock(), Mock()
        synchronizer.start.side_effect = RepositorySyncError("repository_sync_failed", "cannot start")
        scheduler.shutdown.return_value = True
        config = SimpleNamespace(repository_sync_service=synchronizer,
                                 analysis_scheduler=scheduler, runtime_cleanup_manager=None)
        with self.assertRaises(RepositorySyncError):
            ProbeHTTPServer(("127.0.0.1", 0), config)
        synchronizer.shutdown.assert_called_once()
        scheduler.shutdown.assert_called_once_with(timeout=30.0)
