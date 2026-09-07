"""Strict immutable registry of trusted local iOS projects."""

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Mapping, Tuple

from ioscoverage.integrations.git_repository import GitRepository, GitRepositoryError


PROJECT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")


class ProjectRegistryError(Exception):
    """Carries a stable configuration or lookup failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class GitLabProject:
    host: str
    project_path: str


@dataclass(frozen=True)
class ProjectDefinition:
    project: str
    display_name: str
    repository_path: Path
    gitlab: GitLabProject
    allowed_bundle_ids: Tuple[str, ...]


class ProjectRegistry:
    """Loads trusted project definitions once and resolves immutable contexts."""

    def __init__(self, definitions: Iterable[ProjectDefinition]):
        values = tuple(definitions)
        if not values:
            raise ProjectRegistryError("project_registry_empty", "at least one project is required")
        by_project: Dict[str, ProjectDefinition] = {}
        by_gitlab: Dict[Tuple[str, str], ProjectDefinition] = {}
        repositories: Dict[str, GitRepository] = {}
        for definition in values:
            if definition.project in by_project:
                raise ProjectRegistryError("duplicate_project", "project keys must be unique")
            gitlab_key = (definition.gitlab.host, definition.gitlab.project_path)
            if gitlab_key in by_gitlab:
                raise ProjectRegistryError("duplicate_gitlab_project", "GitLab project addresses must be unique")
            try:
                repositories[definition.project] = GitRepository(definition.repository_path)
            except GitRepositoryError as error:
                raise ProjectRegistryError(error.code, error.message) from error
            by_project[definition.project] = definition
            by_gitlab[gitlab_key] = definition
        self._definitions = values
        self._by_project = by_project
        self._by_gitlab = by_gitlab
        self._repositories = repositories

    @classmethod
    def load(cls, path: Path) -> "ProjectRegistry":
        """Loads and strictly validates schema version 1 JSON."""

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ProjectRegistryError("project_registry_missing", "projects.json is missing") from error
        except (OSError, UnicodeError, ValueError) as error:
            raise ProjectRegistryError("project_registry_invalid", "projects.json is invalid") from error
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "projects"}:
            raise ProjectRegistryError("project_registry_invalid", "projects.json has unknown or missing fields")
        if payload["schema_version"] != 1 or not isinstance(payload["projects"], list):
            raise ProjectRegistryError("project_registry_invalid", "projects.json schema_version must be 1")
        return cls(cls._parse_definition(item) for item in payload["projects"])

    @staticmethod
    def _parse_definition(value: object) -> ProjectDefinition:
        if not isinstance(value, dict) or set(value) != {
            "project", "display_name", "repository_path", "gitlab", "allowed_bundle_ids"
        }:
            raise ProjectRegistryError("project_registry_invalid", "project definition has unknown or missing fields")
        project = value["project"]
        display_name = value["display_name"]
        repository_text = value["repository_path"]
        gitlab = value["gitlab"]
        bundle_ids = value["allowed_bundle_ids"]
        if not isinstance(project, str) or not PROJECT_KEY_PATTERN.fullmatch(project):
            raise ProjectRegistryError("project_registry_invalid", "project key is invalid")
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 128:
            raise ProjectRegistryError("project_registry_invalid", "display_name is invalid")
        if not isinstance(repository_text, str) or not Path(repository_text).is_absolute() or ".." in Path(repository_text).parts:
            raise ProjectRegistryError("project_registry_invalid", "repository_path must be an absolute path without traversal")
        if not isinstance(gitlab, dict) or set(gitlab) != {"host", "project_path"}:
            raise ProjectRegistryError("project_registry_invalid", "gitlab definition is invalid")
        host = gitlab["host"]
        project_path = gitlab["project_path"]
        if not isinstance(host, str) or not HOST_PATTERN.fullmatch(host) or host.lower() != host or ":" in host:
            raise ProjectRegistryError("project_registry_invalid", "GitLab host is invalid")
        if not isinstance(project_path, str) or not project_path or project_path.startswith("/"):
            raise ProjectRegistryError("project_registry_invalid", "GitLab project_path is invalid")
        project_parts = PurePosixPath(project_path).parts
        if len(project_parts) < 2 or any(part in ("", ".", "..") for part in project_parts):
            raise ProjectRegistryError("project_registry_invalid", "GitLab project_path is invalid")
        if not isinstance(bundle_ids, list) or not bundle_ids or any(
            not isinstance(item, str) or not item.strip() or len(item) > 255 for item in bundle_ids
        ) or len(set(bundle_ids)) != len(bundle_ids):
            raise ProjectRegistryError("project_registry_invalid", "allowed_bundle_ids must be non-empty and unique")
        return ProjectDefinition(
            project=project,
            display_name=display_name.strip(),
            repository_path=Path(repository_text).resolve(),
            gitlab=GitLabProject(host=host, project_path=project_path),
            allowed_bundle_ids=tuple(bundle_ids),
        )

    @property
    def projects(self) -> Tuple[ProjectDefinition, ...]:
        return self._definitions

    def require(self, project: object) -> ProjectDefinition:
        if not isinstance(project, str) or project not in self._by_project:
            raise ProjectRegistryError("project_not_registered", "Cycle project is not registered")
        return self._by_project[project]

    def match_gitlab(self, host: str, project_path: str) -> ProjectDefinition:
        definition = self._by_gitlab.get((host, project_path))
        if definition is None:
            raise ProjectRegistryError("project_not_registered", "GitLab project is not registered")
        return definition

    def repository(self, project: object) -> GitRepository:
        definition = self.require(project)
        return self._repositories[definition.project]
