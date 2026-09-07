"""ProbeRoutesMixin for the stable HTTP boundary."""

import cgi
import hashlib
import html
import json
import logging
import os
import re
import secrets
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from ioscoverage.analysis.scheduler import AnalysisSchedulerError
from ioscoverage.integrations.gitlab_mr import GitLabMRClientError
from ioscoverage.integrations.xcode_artifacts import ArtifactValidationError
from ioscoverage.storage.analysis_state import AnalysisStateStoreError
from ioscoverage.storage.coverage_store import CoverageStoreError
from ioscoverage.storage.reports import CoverageReportError
from ioscoverage.server.config import *
from ioscoverage.web.pages.cycles import render_cycle_list_page
from ioscoverage.web.pages.report import render_coverage_report_page


class ProbeRoutesMixin:
    def _handle_probe_upload(self) -> None:
        """Validates multipart size and SHA-256 before atomically saving a probe."""

        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._log_event(
                "upload_rejected",
                level=logging.WARNING,
                reason="multipart_form_data_required",
            )
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "multipart_form_data_required"},
            )
            return

        content_length = self._parse_content_length()
        maximum_request_bytes = self.config.max_file_bytes + MULTIPART_OVERHEAD_BYTES
        if content_length is None:
            self._log_event(
                "upload_rejected", level=logging.WARNING, reason="content_length_required"
            )
            self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "content_length_required"})
            return
        if content_length <= 0 or content_length > maximum_request_bytes:
            self._log_event(
                "upload_rejected",
                level=logging.WARNING,
                reason="request_too_large",
                content_length=content_length,
                max_request_bytes=maximum_request_bytes,
            )
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {
                    "error": "request_too_large",
                    "max_file_bytes": self.config.max_file_bytes,
                },
            )
            return

        body = self.rfile.read(content_length)
        self._log_event(
            "upload_body_received",
            content_length=content_length,
            received_bytes=len(body),
        )
        try:
            fields, filename, payload = self._parse_multipart(content_type, body)
        except ValueError as error:
            self._log_event(
                "upload_rejected", level=logging.WARNING, reason=str(error)
            )
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        declared_sha256 = fields.get("sha256", "").lower()
        if not SHA256_PATTERN.fullmatch(declared_sha256):
            self._log_event("upload_rejected", level=logging.WARNING, reason="invalid_sha256")
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_sha256"})
            return
        if not payload:
            self._log_event("upload_rejected", level=logging.WARNING, reason="empty_file")
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "empty_file"})
            return
        if len(payload) > self.config.max_file_bytes:
            self._log_event(
                "upload_rejected",
                level=logging.WARNING,
                reason="file_too_large",
                file_size=len(payload),
                max_file_bytes=self.config.max_file_bytes,
            )
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {
                    "error": "file_too_large",
                    "max_file_bytes": self.config.max_file_bytes,
                },
            )
            return

        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if not secrets.compare_digest(declared_sha256, actual_sha256):
            self._log_event(
                "upload_rejected",
                level=logging.WARNING,
                reason="sha256_mismatch",
                file_size=len(payload),
                declared_sha256=declared_sha256,
                actual_sha256=actual_sha256,
            )
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "error": "sha256_mismatch",
                    "declared_sha256": declared_sha256,
                    "actual_sha256": actual_sha256,
                },
            )
            return

        metadata = self._save_probe(filename, payload, actual_sha256)
        self._log_event(
            "upload_accepted",
            probe_id=metadata["probe_id"],
            original_filename=metadata["original_filename"],
            file_size=metadata["size"],
            sha256=metadata["sha256"],
        )
        self._send_json(
            HTTPStatus.CREATED,
            {
                "status": "accepted",
                "probe_id": metadata["probe_id"],
                "size": metadata["size"],
                "sha256": metadata["sha256"],
                "sha256_matches": True,
                "received_at": metadata["received_at"],
            },
        )
    def _save_probe(self, filename: str, payload: bytes, sha256: str) -> Dict[str, object]:
        """Writes payload and metadata under a server-generated ID using atomic renames."""

        probe_id = str(uuid.uuid4())
        received_at = datetime.now(timezone.utc).isoformat()
        safe_display_name = Path(filename).name[:255] or "upload.bin"
        metadata: Dict[str, object] = {
            "probe_id": probe_id,
            "received_at": received_at,
            "source_ip": self.client_address[0],
            "original_filename": safe_display_name,
            "size": len(payload),
            "sha256": sha256,
        }

        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.config.data_dir / (probe_id + ".bin"), payload)
        self._atomic_write(
            self.config.data_dir / (probe_id + ".json"),
            (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return metadata
    def _atomic_write(self, destination: Path, data: bytes) -> None:
        """Persists one file completely before exposing its final path."""

        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=str(destination.parent), prefix=".upload-", delete=False
            ) as temporary_file:
                temporary_name = temporary_file.name
                temporary_file.write(data)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, destination)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)
    def _load_recent_probes(self, limit: int = 20) -> List[Dict[str, object]]:
        """Reads recent immutable metadata files for the diagnostic HTML page."""

        if not self.config.data_dir.exists():
            return []
        metadata_files = sorted(
            self.config.data_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True
        )
        probes: List[Dict[str, object]] = []
        for metadata_file in metadata_files[:limit]:
            try:
                probes.append(json.loads(metadata_file.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return probes
