"""Small trusted-context fixtures shared by server tests."""

from pathlib import Path
from typing import Iterable
from types import SimpleNamespace

from ioscoverage.projects import GitLabProject, ProjectDefinition, ProjectRegistry


def project_registry(repository: Path, bundle_ids: Iterable[str] = ("com.example.coverage",),
                     project: str = "ExampleApp", host: str = "git.example.test",
                     project_path: str = None) -> ProjectRegistry:
    """Creates one strictly validated registry around an initialized temporary Git root."""

    return ProjectRegistry((ProjectDefinition(
        project=project,
        display_name=project,
        repository_path=repository.resolve(),
        gitlab=GitLabProject(host, project_path or "mobile/" + project),
        allowed_bundle_ids=tuple(bundle_ids),
    ),))


class FakeProjectRegistry:
    """Provides the registry protocol for tests that inject a fake repository."""

    def __init__(self, repository, project="ExampleApp", bundle_ids=("com.example.coverage",)):
        self._repository = repository
        self._definition = SimpleNamespace(
            project=project,
            display_name=project,
            gitlab=SimpleNamespace(host="git.example.test", project_path="mobile/" + project),
            allowed_bundle_ids=tuple(bundle_ids),
        )

    def require(self, project):
        if project != self._definition.project:
            from ioscoverage.projects import ProjectRegistryError
            raise ProjectRegistryError("project_not_registered", "Cycle project is not registered")
        return self._definition

    def repository(self, project):
        self.require(project)
        return self._repository
