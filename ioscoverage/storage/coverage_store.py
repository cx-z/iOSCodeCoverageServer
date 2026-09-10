#!/usr/bin/env python3
"""Unified facade over responsibility-focused coverage persistence mixins."""

import threading
from pathlib import Path

from ioscoverage.projects import ProjectRegistry, ProjectRegistryError
from ioscoverage.integrations.xcode_artifacts import XcodeArtifactValidator
from ioscoverage.storage.artifact_files import ArtifactFileMixin
from ioscoverage.storage.builds import BuildStoreMixin
from ioscoverage.storage.common import *
from ioscoverage.storage.cycles import CycleStoreMixin
from ioscoverage.storage.database import DatabaseMixin
from ioscoverage.storage.runs import RunStoreMixin
from ioscoverage.storage.schema import SchemaMixin
from ioscoverage.storage.validation import ValidationMixin
from ioscoverage.storage.retained_evidence import RetainedBuildEvidenceStore


class CoverageStore(
    CycleStoreMixin,
    BuildStoreMixin,
    RunStoreMixin,
    SchemaMixin,
    ArtifactFileMixin,
    DatabaseMixin,
    ValidationMixin,
):
    """Owns SQLite state and corresponding immutable payloads through one API."""
    def __init__(
        self,
        database_path: Path,
        data_dir: Path,
        project_registry: ProjectRegistry,
        artifact_validator: XcodeArtifactValidator,
        retained_evidence_store: RetainedBuildEvidenceStore = None,
    ):
        """Initializes schema and the process-local mutation lock for one store."""

        self.database_path = database_path.resolve()
        self.data_dir = data_dir.resolve()
        self.project_registry = project_registry
        self.artifact_validator = artifact_validator
        self.retained_evidence_store = retained_evidence_store
        self._mutation_lock = threading.RLock()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def project_definition(self, project: object):
        """Returns one trusted registered project as a stable storage error."""

        try:
            return self.project_registry.require(project)
        except ProjectRegistryError as error:
            raise CoverageStoreError(error.code, error.message) from error
