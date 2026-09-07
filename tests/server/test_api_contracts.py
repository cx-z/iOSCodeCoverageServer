#!/usr/bin/env python3
"""HTTP component tests for Cycle, Build, and Run contracts."""

import hashlib
import http.client
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import uuid
import zipfile
from pathlib import Path
from ioscoverage.runtime import RuntimePaths
from tests.helpers import project_registry
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from server import create_server


class APIContractTests(unittest.TestCase):
    """Exercises authenticated protocol behavior and durable state transitions."""

    @classmethod
    def setUpClass(cls) -> None:
        """Builds reusable real Mach-O and dSYM fixtures with Xcode 26.3."""

        cls.artifact_fixture_directory = tempfile.TemporaryDirectory()
        cls.artifact_root = Path(cls.artifact_fixture_directory.name)
        cls.xcode_environment = dict(os.environ)
        cls.xcode_environment["DEVELOPER_DIR"] = "/Applications/Xcode263.app/Contents/Developer"
        main_source = cls.artifact_root / "main.c"
        debug_source = cls.artifact_root / "debug.c"
        main_source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        debug_source.write_text(
            "int coverage_debug_symbol(void) { return 1; }\n", encoding="utf-8"
        )
        cls.main_path = cls.artifact_root / "ExampleApp"
        cls.debug_path = cls.artifact_root / "ExampleApp.debug.dylib"
        cls._xcode("xcrun", "clang", "-g", "-o", str(cls.main_path), str(main_source))
        cls._xcode(
            "xcrun",
            "clang",
            "-g",
            "-dynamiclib",
            "-install_name",
            "@rpath/ExampleApp.debug.dylib",
            "-o",
            str(cls.debug_path),
            str(debug_source),
        )
        cls.main_dsym_path = cls.artifact_root / "ExampleApp.app.dSYM"
        cls.debug_dsym_path = cls.artifact_root / "ExampleApp.debug.dylib.dSYM"
        cls._xcode("xcrun", "dsymutil", str(cls.main_path), "-o", str(cls.main_dsym_path))
        cls._xcode("xcrun", "dsymutil", str(cls.debug_path), "-o", str(cls.debug_dsym_path))
        cls.main_uuids = cls._fixture_uuids(cls.main_path)
        cls.debug_uuids = cls._fixture_uuids(cls.debug_path)
        cls.main_bytes = cls.main_path.read_bytes()
        cls.debug_bytes = cls.debug_path.read_bytes()
        cls.main_dsym_zip = cls._create_dsym_zip("main-only.zip", [cls.main_dsym_path])
        cls.combined_dsym_zip = cls._create_dsym_zip(
            "main-and-debug.zip", [cls.main_dsym_path, cls.debug_dsym_path]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        """Removes the shared Xcode-generated artifact fixtures."""

        cls.artifact_fixture_directory.cleanup()

    @classmethod
    def _xcode(cls, *arguments: str) -> str:
        """Runs one fixture command with the mandated Xcode 26.3 developer directory."""

        result = subprocess.run(
            list(arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cls.xcode_environment,
        )
        return result.stdout.strip()

    @classmethod
    def _fixture_uuids(cls, path: Path) -> List[str]:
        """Extracts canonical UUIDs from one generated fixture using dwarfdump."""

        output = cls._xcode("xcrun", "dwarfdump", "--uuid", str(path))
        values = []
        for line in output.splitlines():
            match = re.match(r"^UUID:\s+([0-9A-Fa-f-]{36})(?:\s|$)", line.strip())
            if match:
                values.append(str(uuid.UUID(match.group(1))))
        if not values:
            raise AssertionError("fixture has no Mach-O UUID")
        return sorted(set(values))

    @classmethod
    def _create_dsym_zip(cls, filename: str, bundles: Sequence[Path]) -> bytes:
        """Creates one deterministic-shape ZIP containing the requested real dSYM bundles."""

        archive_path = cls.artifact_root / filename
        with zipfile.ZipFile(str(archive_path), mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for bundle in bundles:
                for path in sorted(bundle.rglob("*")):
                    archive.write(str(path), str(path.relative_to(cls.artifact_root)))
        return archive_path.read_bytes()

    def setUp(self) -> None:
        """Starts an isolated server with distinct probe and artifact size limits."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_dir = self.root / "data"
        self.database = self.root / "instance" / "coverage.sqlite3"
        self.repository = self.root / "ExampleApp"
        self.base_sha, self.target_sha, self.head_sha = self.create_git_history()
        self.token = "api-component-test-token"
        self.run_upload_token = "restricted-run-test-token"
        self.logger = logging.getLogger("ios_coverage_p1_test_{}".format(id(self)))
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.logger.addHandler(logging.NullHandler())
        self.server = create_server(
            "127.0.0.1",
            0,
            self.token,
            RuntimePaths.from_root(self.root),
            project_registry(self.repository),
            max_file_bytes=32,
            logger=self.logger,
            max_run_bytes=2048,
            max_build_artifact_bytes=8 * 1024 * 1024,
            start_analysis_scheduler=False,
            run_upload_token=self.run_upload_token,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cycle_id = self.create_cycle()

    def tearDown(self) -> None:
        """Stops the server and removes isolated metadata and artifacts."""

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def git(self, *arguments: str) -> str:
        """Runs one deterministic Git command inside the isolated test repository."""

        result = subprocess.run(
            ["git", "-C", str(self.repository)] + list(arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    def create_git_history(self) -> Tuple[str, str, str]:
        """Creates diverged target and feature commits with one known merge-base."""

        self.repository.mkdir()
        subprocess.run(
            ["git", "init", str(self.repository)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.git("config", "user.email", "coverage-tests@example.invalid")
        self.git("config", "user.name", "Coverage Tests")
        (self.repository / "base.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "base.txt")
        self.git("commit", "-m", "base")
        base_sha = self.git("rev-parse", "HEAD")
        self.target_branch = self.git("branch", "--show-current")
        self.git("branch", "feature")
        (self.repository / "target.txt").write_text("target\n", encoding="utf-8")
        self.git("add", "target.txt")
        self.git("commit", "-m", "target")
        target_sha = self.git("rev-parse", "HEAD")
        self.git("checkout", "feature")
        (self.repository / "feature.txt").write_text("feature\n", encoding="utf-8")
        self.git("add", "feature.txt")
        self.git("commit", "-m", "feature")
        head_sha = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("merge-base", target_sha, head_sha), base_sha)
        return base_sha, target_sha, head_sha

    def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        headers: Dict[str, str] = None,
    ) -> Tuple[int, Dict[str, object]]:
        """Sends one authenticated-by-default request and parses its JSON response."""

        request_headers = {"Authorization": "Bearer " + self.token}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def json_request(
        self, method: str, path: str, payload: Mapping[str, object]
    ) -> Tuple[int, Dict[str, object]]:
        """Sends one canonical JSON API request."""

        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self.request(
            method,
            path,
            body,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
        )

    def multipart(
        self, text_fields: Mapping[str, str], file_fields: Mapping[str, Tuple[str, bytes]]
    ) -> Tuple[bytes, str]:
        """Builds a deterministic multipart body for streaming endpoint tests."""

        return self.multipart_parts(
            list(text_fields.items()),
            [(name, value[0], value[1]) for name, value in file_fields.items()],
        )

    def multipart_parts(
        self,
        text_fields: Sequence[Tuple[str, str]],
        file_fields: Sequence[Tuple[str, str, bytes]],
    ) -> Tuple[bytes, str]:
        """Builds multipart bytes while allowing duplicate-field rejection tests."""

        boundary = "ios-coverage-test-api-" + uuid.uuid4().hex
        body = bytearray()
        for name, value in text_fields:
            body.extend(("--{}\r\n".format(boundary)).encode("ascii"))
            body.extend(
                ('Content-Disposition: form-data; name="{}"\r\n\r\n'.format(name)).encode(
                    "ascii"
                )
            )
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")
        for name, filename, payload in file_fields:
            body.extend(("--{}\r\n".format(boundary)).encode("ascii"))
            body.extend(
                (
                    'Content-Disposition: form-data; name="{}"; filename="{}"\r\n'
                    "Content-Type: application/octet-stream\r\n\r\n"
                ).format(name, filename).encode("ascii")
            )
            body.extend(payload)
            body.extend(b"\r\n")
        body.extend(("--{}--\r\n".format(boundary)).encode("ascii"))
        return bytes(body), "multipart/form-data; boundary=" + boundary

    def create_cycle(self) -> str:
        """Creates and returns the stable Cycle used by a test."""

        status, payload = self.json_request(
            "POST",
            "/api/v1/cycles",
            {
                "schema_version": 1,
                "project": "ExampleApp",
                "mr_iid": "123",
                "mr_url": "https://git.example.com/mobile/example-app/merge_requests/123",
                "target_branch": self.target_branch,
                "base_sha": self.base_sha,
                "head_sha": self.head_sha,
            },
        )
        self.assertEqual(status, 201)
        return str(payload["cycle"]["id"])

    def build_material(
        self,
        build_id: str = None,
        coverage_binary: str = "main_executable",
        include_debug_dylib: bool = False,
        dsym_includes_debug: Optional[bool] = None,
    ) -> Tuple[Dict[str, object], Dict[str, Tuple[str, bytes]]]:
        """Creates a valid manifest and real Xcode-generated artifact set."""

        include_debug_in_dsym = (
            include_debug_dylib if dsym_includes_debug is None else dsym_includes_debug
        )
        dsym_bytes = self.combined_dsym_zip if include_debug_in_dsym else self.main_dsym_zip
        files = {
            "main_executable": ("ExampleApp", self.main_bytes),
            "dsym": ("ExampleApp.app.dSYM.zip", dsym_bytes),
        }
        if include_debug_dylib:
            files["debug_dylib"] = ("ExampleApp.debug.dylib", self.debug_bytes)
        artifacts: Dict[str, Dict[str, object]] = {}
        for name, (filename, content) in files.items():
            descriptor: Dict[str, object] = {
                "filename": filename,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
            if name == "dsym":
                descriptor["archive_format"] = "zip"
            artifacts[name] = descriptor
        manifest = {
            "schema_version": 1,
            "build_id": build_id or str(uuid.uuid4()),
            "cycle_id": self.cycle_id,
            "git_sha": self.head_sha,
            "target_sha_at_build": self.target_sha,
            "merge_base_sha": self.base_sha,
            "coverage_binary": coverage_binary,
            "bundle_id": "com.example.coverage",
            "app_version": "1.0.0",
            "bundle_version": "100",
            "xcode_version": "26.3",
            "swift_version": "6.2",
            "macho_uuids": {
                "main_executable": list(self.main_uuids),
                "dsym": sorted(
                    set(self.main_uuids)
                    | (set(self.debug_uuids) if include_debug_in_dsym else set())
                ),
            },
            "artifacts": artifacts,
        }
        if include_debug_dylib:
            manifest["macho_uuids"]["debug_dylib"] = list(self.debug_uuids)  # type: ignore[index]
        return manifest, files

    def register_build(
        self, manifest: Mapping[str, object], files: Mapping[str, Tuple[str, bytes]]
    ) -> Tuple[int, Dict[str, object]]:
        """Uploads one Build request."""

        body, content_type = self.multipart(
            {"manifest": json.dumps(manifest, separators=(",", ":"))}, files
        )
        return self.request(
            "POST",
            "/api/v1/builds",
            body,
            {"Content-Type": content_type, "Content-Length": str(len(body))},
        )

    def test_project_specific_bundle_and_product_independent_filenames(self) -> None:
        """Accepts safe product filenames but enforces the Cycle project's Bundle ID."""

        manifest, files = self.build_material()
        renamed = {
            "main_executable": ("Other App", files["main_executable"][1]),
            "dsym": ("Other App.app.dSYM.zip", files["dsym"][1]),
        }
        manifest["artifacts"]["main_executable"]["filename"] = "Other App"
        manifest["artifacts"]["dsym"]["filename"] = "Other App.app.dSYM.zip"
        status, payload = self.register_build(manifest, renamed)
        self.assertEqual(status, 201, payload)
        build_dir = self.data_dir / "cycles" / self.cycle_id / "builds" / manifest["build_id"]
        self.assertTrue((build_dir / "artifacts" / "main_executable.bin").is_file())
        self.assertTrue((build_dir / "artifacts" / "dsym.zip").is_file())

        rejected, rejected_files = self.build_material()
        rejected["bundle_id"] = "com.example.not-allowed"
        status, payload = self.register_build(rejected, rejected_files)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "bundle_id_not_allowed")

    def run_metadata(self, run_id: str, build_id: str) -> Dict[str, object]:
        """Creates stable Run metadata for upload and replay tests."""

        return {
            "schema_version": 1,
            "run_id": run_id,
            "cycle_id": self.cycle_id,
            "build_id": build_id,
            "device_id": str(uuid.uuid4()),
            "process_id": 4321,
            "launch_id": str(uuid.uuid4()),
            "captured_at": "2026-08-31T16:00:00+08:00",
            "app_state": "active",
            "trigger_reason": "user_upload",
        }

    def upload_run(
        self,
        metadata: Mapping[str, object],
        profile: bytes,
        declared_sha256: str = None,
        authorization_token: str = None,
    ) -> Tuple[int, Dict[str, object]]:
        """Uploads one profraw using the stable three-field contract."""

        body, content_type = self.multipart(
            {
                "metadata": json.dumps(metadata, separators=(",", ":")),
                "sha256": declared_sha256 or hashlib.sha256(profile).hexdigest(),
            },
            {"profile": (str(metadata["run_id"]) + ".profraw", profile)},
        )
        headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        if authorization_token is not None:
            headers["Authorization"] = "Bearer " + authorization_token
        return self.request(
            "POST",
            "/api/v1/runs",
            body,
            headers,
        )

    def test_restricted_token_uploads_run_but_cannot_read_resources(self) -> None:
        """Allows the iOS token on Run POST while denying every management/read route."""

        manifest, files = self.build_material()
        self.assertEqual(self.register_build(manifest, files)[0], 201)
        run_id = str(uuid.uuid4())
        status, payload = self.upload_run(
            self.run_metadata(run_id, str(manifest["build_id"])),
            b"restricted-profraw",
            authorization_token=self.run_upload_token,
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["run_id"], run_id)
        protected_paths = (
            "/api/v1/cycles",
            "/api/v1/cycles/" + self.cycle_id,
            "/api/v1/builds/" + str(manifest["build_id"]),
            "/api/v1/runs/" + run_id,
            "/api/v1/cycles/" + self.cycle_id + "/analysis",
            "/api/v1/cycles/" + self.cycle_id + "/report",
        )
        for path in protected_paths:
            with self.subTest(path=path):
                status, denied = self.request(
                    "GET", path, headers={"Authorization": "Bearer " + self.run_upload_token}
                )
                self.assertEqual(status, 403)
                self.assertEqual(denied["error"], "insufficient_scope")
        status, operator_result = self.request("GET", "/api/v1/runs/" + run_id)
        self.assertEqual(status, 200)
        self.assertEqual(operator_result["run"]["id"], run_id)

    def test_cycle_id_and_base_are_immutable_while_head_can_change(self) -> None:
        """Confirms the API and SQLite both protect Cycle identity and base SHA."""

        new_head = "3" * 40
        status, payload = self.json_request(
            "PATCH",
            "/api/v1/cycles/" + self.cycle_id,
            {"schema_version": 1, "head_sha": new_head},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["cycle"]["head_sha"], new_head)
        self.assertEqual(payload["cycle"]["base_sha"], self.base_sha)

        status, payload = self.json_request(
            "PATCH",
            "/api/v1/cycles/" + self.cycle_id,
            {"schema_version": 1, "head_sha": new_head, "base_sha": "4" * 40},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "unknown_field")
        with sqlite3.connect(str(self.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE coverage_cycles SET base_sha = ? WHERE id = ?",
                    ("4" * 40, self.cycle_id),
                )

    def test_main_coverage_without_debug_dylib_is_persisted_and_idempotent(self) -> None:
        """Registers the Archive shape without creating a debug dylib placeholder."""

        manifest, files = self.build_material()
        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 201)
        self.assertEqual(payload["artifact_state"], "ready")
        self.assertFalse(payload["idempotent_replay"])
        status, detail = self.request("GET", "/api/v1/builds/" + str(manifest["build_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(detail["build"]["git_sha"], self.head_sha)
        self.assertEqual(detail["build"]["target_sha_at_build"], self.target_sha)
        self.assertEqual(detail["build"]["merge_base_sha"], self.base_sha)
        self.assertEqual(detail["build"]["coverage_binary"], "main_executable")
        self.assertEqual(set(detail["build"]["artifacts"]), {"main_executable", "dsym"})
        self.assertEqual(set(detail["build"]["macho_uuids"]), {"main_executable", "dsym"})
        self.assertNotIn("debug_dylib_sha256", detail["build"])
        self.assertNotIn("debug_dylib_uuids", detail["build"])
        expected_manifest_sha = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        self.assertEqual(detail["build"]["manifest_sha256"], expected_manifest_sha)
        build_dir = self.data_dir / "cycles" / self.cycle_id / "builds" / str(manifest["build_id"])
        self.assertEqual((build_dir / "artifacts" / "main_executable.bin").read_bytes(), files["main_executable"][1])
        self.assertFalse((build_dir / "artifacts" / "debug_dylib.bin").exists())
        self.assertEqual(
            (build_dir / "artifacts" / "dsym.zip").read_bytes(), files["dsym"][1]
        )

        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 200)
        self.assertTrue(payload["idempotent_replay"])

    def test_main_coverage_with_real_debug_dylib_keeps_main_as_subject(self) -> None:
        """Stores a declared optional dylib without changing the analysis subject."""

        manifest, files = self.build_material(include_debug_dylib=True)
        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 201)
        status, detail = self.request("GET", "/api/v1/builds/" + str(manifest["build_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(detail["build"]["coverage_binary"], "main_executable")
        self.assertIn("debug_dylib", detail["build"]["artifacts"])
        self.assertEqual(detail["build"]["debug_dylib_uuids"], self.debug_uuids)

    def test_debug_dylib_coverage_with_real_dylib_succeeds(self) -> None:
        """Registers the ordinary Debug shape with the dylib as analysis subject."""

        manifest, files = self.build_material(
            coverage_binary="debug_dylib", include_debug_dylib=True
        )
        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 201)
        status, detail = self.request("GET", "/api/v1/builds/" + str(manifest["build_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(detail["build"]["coverage_binary"], "debug_dylib")
        self.assertEqual(set(detail["build"]["artifacts"]), set(files))

    def test_build_registration_requires_comparison_snapshot_fields(self) -> None:
        """Returns one stable code when the Build comparison snapshot is incomplete."""

        manifest, files = self.build_material()
        manifest.pop("target_sha_at_build")
        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "missing_build_field")

    def test_debug_dylib_subject_without_dylib_is_rejected(self) -> None:
        """Rejects a dylib analysis subject when no real dylib is declared or uploaded."""

        manifest, files = self.build_material(coverage_binary="debug_dylib")
        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "coverage_binary_artifact_missing")

    def test_invalid_coverage_binary_is_rejected(self) -> None:
        """Accepts only the two explicitly supported analysis subjects."""

        missing_manifest, missing_files = self.build_material()
        missing_manifest.pop("coverage_binary")
        status, payload = self.register_build(missing_manifest, missing_files)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "missing_build_field")

        manifest, files = self.build_material()
        manifest["coverage_binary"] = "copied_placeholder"
        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_coverage_binary")

    def test_main_executable_and_dsym_are_always_required(self) -> None:
        """Rejects either missing mandatory multipart artifact."""

        for missing_field in ("main_executable", "dsym"):
            manifest, files = self.build_material()
            files.pop(missing_field)
            status, payload = self.register_build(manifest, files)
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"], "invalid_multipart_fields")

    def test_manifest_and_multipart_dylib_sets_must_match(self) -> None:
        """Rejects both a missing declared dylib and an undeclared uploaded dylib."""

        declared_manifest, declared_files = self.build_material(include_debug_dylib=True)
        declared_files.pop("debug_dylib")
        status, payload = self.register_build(declared_manifest, declared_files)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "artifact_set_mismatch")

        undeclared_manifest, undeclared_files = self.build_material(include_debug_dylib=True)
        undeclared_manifest["artifacts"].pop("debug_dylib")  # type: ignore[union-attr]
        undeclared_manifest["macho_uuids"].pop("debug_dylib")  # type: ignore[union-attr]
        status, payload = self.register_build(undeclared_manifest, undeclared_files)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "artifact_set_mismatch")

    def test_unknown_and_duplicate_multipart_fields_are_rejected(self) -> None:
        """Rejects protocol drift and duplicate field ambiguity at the HTTP boundary."""

        manifest, files = self.build_material()
        files["unknown_artifact"] = ("unknown.bin", b"unknown")
        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_multipart_fields")

        manifest, files = self.build_material()
        body, content_type = self.multipart_parts(
            [("manifest", json.dumps(manifest, separators=(",", ":")))],
            [
                ("main_executable", files["main_executable"][0], files["main_executable"][1]),
                ("main_executable", files["main_executable"][0], files["main_executable"][1]),
                ("dsym", files["dsym"][0], files["dsym"][1]),
            ],
        )
        status, payload = self.request(
            "POST",
            "/api/v1/builds",
            body,
            {"Content-Type": content_type, "Content-Length": str(len(body))},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "duplicate_multipart_field")

    def test_build_size_and_sha_mismatch_are_rejected(self) -> None:
        """Rejects declared Build bytes that do not match the actual upload."""

        size_manifest, files = self.build_material()
        size_manifest["artifacts"]["main_executable"]["size"] += 1  # type: ignore[index,operator]
        status, payload = self.register_build(size_manifest, files)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "size_mismatch")

        sha_manifest, files = self.build_material()
        sha_manifest["artifacts"]["main_executable"]["sha256"] = "0" * 64  # type: ignore[index]
        status, payload = self.register_build(sha_manifest, files)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "sha256_mismatch")

    def test_main_and_debug_macho_uuid_mismatch_are_rejected(self) -> None:
        """Validates declared UUIDs against both received Mach-O files."""

        main_manifest, files = self.build_material()
        main_manifest["macho_uuids"]["main_executable"] = [str(uuid.uuid4())]  # type: ignore[index]
        status, payload = self.register_build(main_manifest, files)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "artifact_uuid_mismatch")

        debug_manifest, files = self.build_material(include_debug_dylib=True)
        debug_manifest["macho_uuids"]["debug_dylib"] = [str(uuid.uuid4())]  # type: ignore[index]
        status, payload = self.register_build(debug_manifest, files)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "artifact_uuid_mismatch")

    def test_dsym_uuid_and_coverage_subject_relationship_are_validated(self) -> None:
        """Rejects a false dSYM UUID list and a dSYM that omits the analysis subject."""

        dsym_manifest, files = self.build_material()
        dsym_manifest["macho_uuids"]["dsym"] = [str(uuid.uuid4())]  # type: ignore[index]
        status, payload = self.register_build(dsym_manifest, files)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "artifact_uuid_mismatch")

        coverage_manifest, files = self.build_material(
            coverage_binary="debug_dylib",
            include_debug_dylib=True,
            dsym_includes_debug=False,
        )
        status, payload = self.register_build(coverage_manifest, files)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "coverage_binary_dsym_uuid_mismatch")

    def test_unsafe_dsym_zip_is_rejected_and_cleaned(self) -> None:
        """Prevents ZIP path traversal and removes its staging directory on failure."""

        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, mode="w") as archive:
            archive.writestr("../escape", b"not allowed")
        manifest, files = self.build_material()
        unsafe_zip = archive_buffer.getvalue()
        files["dsym"] = ("ExampleApp.app.dSYM.zip", unsafe_zip)
        manifest["artifacts"]["dsym"]["size"] = len(unsafe_zip)  # type: ignore[index]
        manifest["artifacts"]["dsym"]["sha256"] = hashlib.sha256(unsafe_zip).hexdigest()  # type: ignore[index]
        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "unsafe_dsym_zip")
        self.assertFalse((self.data_dir / "escape").exists())
        self.assertEqual(list((self.data_dir / ".staging").iterdir()), [])

    def test_build_registration_rejects_unknown_git_object_and_wrong_merge_base(self) -> None:
        """Distinguishes missing repository objects from an incorrect declared merge-base."""

        missing_manifest, files = self.build_material()
        missing_manifest["target_sha_at_build"] = "f" * 40
        status, payload = self.register_build(missing_manifest, files)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "git_object_not_found")

        mismatch_manifest, files = self.build_material()
        mismatch_manifest["merge_base_sha"] = self.target_sha
        status, payload = self.register_build(mismatch_manifest, files)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "merge_base_mismatch")

    def test_build_id_conflict_includes_comparison_snapshot_fields(self) -> None:
        """Treats a changed valid comparison snapshot as different Build content."""

        manifest, files = self.build_material()
        self.assertEqual(self.register_build(manifest, files)[0], 201)
        changed_manifest = json.loads(json.dumps(manifest))
        changed_manifest["target_sha_at_build"] = self.head_sha
        changed_manifest["merge_base_sha"] = self.head_sha
        status, payload = self.register_build(changed_manifest, files)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "build_id_conflict")

    def test_build_id_conflict_rejects_coverage_subject_or_artifact_set_change(self) -> None:
        """Prevents an existing Build from gaining a dylib or changing analysis subject."""

        build_id = str(uuid.uuid4())
        manifest, files = self.build_material(build_id=build_id)
        self.assertEqual(self.register_build(manifest, files)[0], 201)
        changed_manifest, changed_files = self.build_material(
            build_id=build_id,
            coverage_binary="debug_dylib",
            include_debug_dylib=True,
        )
        status, payload = self.register_build(changed_manifest, changed_files)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "build_id_conflict")
        with sqlite3.connect(str(self.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE builds SET coverage_binary = 'debug_dylib' WHERE id = ?",
                    (build_id,),
                )

    def test_target_branch_advance_does_not_modify_registered_build(self) -> None:
        """Keeps the persisted comparison snapshot immutable after the target branch advances."""

        manifest, files = self.build_material()
        self.assertEqual(self.register_build(manifest, files)[0], 201)
        self.git("checkout", self.target_branch)
        (self.repository / "target-next.txt").write_text("target next\n", encoding="utf-8")
        self.git("add", "target-next.txt")
        self.git("commit", "-m", "target next")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), self.target_sha)

        status, payload = self.request("GET", "/api/v1/builds/" + str(manifest["build_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(payload["build"]["target_sha_at_build"], self.target_sha)
        self.assertEqual(payload["build"]["merge_base_sha"], self.base_sha)
        with sqlite3.connect(str(self.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE builds SET target_sha_at_build = ? WHERE id = ?",
                    (self.git("rev-parse", "HEAD"), manifest["build_id"]),
                )

    def test_build_manifest_cannot_choose_repository_or_git_command(self) -> None:
        """Rejects request-controlled repository and command fields."""

        manifest, files = self.build_material()
        manifest["repository_path"] = "/tmp/other"
        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "unknown_field")

    def test_artifact_filename_rejects_cross_platform_path_separators(self) -> None:
        """Accepts product basenames but never path-shaped artifact names."""

        for field_name, filename in (
            ("main_executable", "folder/App"),
            ("main_executable", "folder\\App"),
            ("dsym", "folder\\App.app.dSYM.zip"),
        ):
            with self.subTest(field_name=field_name, filename=filename):
                manifest, files = self.build_material()
                manifest["build_id"] = str(uuid.uuid4())
                manifest["artifacts"][field_name]["filename"] = filename
                status, payload = self.register_build(manifest, files)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "invalid_manifest")

    def test_registered_run_replay_succeeds_and_different_content_conflicts(self) -> None:
        """Confirms run_id protects exact metadata and profile content."""

        manifest, files = self.build_material()
        self.assertEqual(self.register_build(manifest, files)[0], 201)
        run_id = str(uuid.uuid4())
        metadata = self.run_metadata(run_id, str(manifest["build_id"]))
        profile = b"profraw" + b"x" * 128
        status, payload = self.upload_run(metadata, profile)
        self.assertEqual(status, 201)
        self.assertTrue(payload["analyzable"])
        self.assertFalse(payload["idempotent_replay"])
        self.assertEqual(payload["sha256"], hashlib.sha256(profile).hexdigest())
        self.assertEqual(
            (
                self.data_dir
                / "cycles"
                / self.cycle_id
                / "runs"
                / run_id
                / "payload.profraw"
            ).read_bytes(),
            profile,
        )

        status, payload = self.upload_run(metadata, profile)
        self.assertEqual(status, 200)
        self.assertTrue(payload["idempotent_replay"])

        status, payload = self.upload_run(metadata, profile + b"different")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "run_id_conflict")

        status, payload = self.upload_run(
            metadata, profile + b"different", hashlib.sha256(profile).hexdigest()
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "sha256_mismatch")

    def test_unknown_build_run_is_pending_then_promoted_by_registration(self) -> None:
        """Confirms unknown Builds retain Runs without claiming analyzability."""

        build_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        metadata = self.run_metadata(run_id, build_id)
        status, payload = self.upload_run(metadata, b"pending-profraw")
        self.assertEqual(status, 202)
        self.assertEqual(payload["run_state"], "pending_build")
        self.assertFalse(payload["build_registered"])
        self.assertFalse(payload["analyzable"])

        manifest, files = self.build_material(build_id)
        status, payload = self.register_build(manifest, files)
        self.assertEqual(status, 201)
        self.assertEqual(payload["pending_runs_promoted"], 1)

        status, payload = self.request("GET", "/api/v1/runs/" + run_id)
        self.assertEqual(status, 200)
        self.assertEqual(payload["run"]["state"], "uploaded")
        self.assertTrue(payload["run"]["analyzable"])

    def test_run_limit_is_independent_from_smaller_probe_limit(self) -> None:
        """Confirms a valid profraw may exceed the probe's 32-byte test limit."""

        manifest, files = self.build_material()
        self.assertEqual(self.register_build(manifest, files)[0], 201)
        profile = b"p" * 1024
        metadata = self.run_metadata(str(uuid.uuid4()), str(manifest["build_id"]))
        status, payload = self.upload_run(metadata, profile)
        self.assertEqual(status, 201)
        self.assertEqual(payload["size"], 1024)

    def test_hash_mismatch_leaves_no_final_or_staging_run(self) -> None:
        """Confirms failed integrity checks publish neither final nor temporary Run data."""

        run_id = str(uuid.uuid4())
        metadata = self.run_metadata(run_id, str(uuid.uuid4()))
        status, payload = self.upload_run(metadata, b"actual", hashlib.sha256(b"other").hexdigest())
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "sha256_mismatch")
        self.assertFalse((self.data_dir / "cycles" / self.cycle_id / "runs" / run_id).exists())
        staging = self.data_dir / ".staging"
        self.assertEqual(list(staging.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
