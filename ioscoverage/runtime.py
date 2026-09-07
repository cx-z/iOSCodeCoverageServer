"""Central immutable runtime-directory contract for the coverage platform."""

from dataclasses import dataclass
from pathlib import Path


class RuntimeConfigurationError(ValueError):
    """Reports an invalid operator-owned runtime root without exposing secrets."""


@dataclass(frozen=True)
class RuntimePaths:
    """Derives every mutable service path from one explicit runtime root."""

    root: Path

    @classmethod
    def from_root(cls, value: Path) -> "RuntimePaths":
        """Normalizes one explicit root and rejects files in its place."""

        root = Path(value).expanduser().resolve()
        if root == Path(root.anchor):
            raise RuntimeConfigurationError("runtime_root cannot be a filesystem root")
        if root.exists() and not root.is_dir():
            raise RuntimeConfigurationError("runtime_root must be a directory")
        return cls(root=root)

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def projects_file(self) -> Path:
        return self.config_dir / "projects.json"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def gitlab_token(self) -> Path:
        return self.state_dir / "gitlab_api_token"

    @property
    def operator_token(self) -> Path:
        return self.state_dir / "operator_token"

    @property
    def run_upload_token(self) -> Path:
        return self.state_dir / "run_upload_token"

    @property
    def instance_dir(self) -> Path:
        return self.root / "instance"

    @property
    def database(self) -> Path:
        return self.instance_dir / "coverage.sqlite3"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def analysis_dir(self) -> Path:
        return self.root / "analysis"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def server_log(self) -> Path:
        return self.logs_dir / "server.log"

    def prepare_directories(self) -> None:
        """Creates only the fixed runtime containers required by the service."""

        for path in (
            self.config_dir,
            self.state_dir,
            self.instance_dir,
            self.data_dir,
            self.analysis_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
