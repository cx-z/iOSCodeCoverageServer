"""Registry-bounded, read-only GitLab merge-request lookup."""

import json
import re
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ioscoverage.projects import ProjectDefinition, ProjectRegistry, ProjectRegistryError

GITLAB_RESPONSE_LIMIT = 1024 * 1024
GIT_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class GitLabMRClientError(Exception):
    """Carries one safe URL, credential, or upstream error."""

    def __init__(self, code: str, message: str, http_status: int):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class NormalizedMRURL:
    project: ProjectDefinition
    iid: int


@dataclass(frozen=True)
class GitLabMergeRequest:
    project: str
    project_display_name: str
    iid: int
    web_url: str
    state: str
    source_branch: str
    target_branch: str
    base_sha: str
    start_sha: str
    head_sha: str


GitLabTransport = Callable[[Request, float], Tuple[int, bytes]]


class GitLabMRClient:
    """Performs only a registry-approved MR GET using one local credential."""

    def __init__(self, token_path: Path, project_registry: ProjectRegistry,
                 transport: GitLabTransport = None, timeout_seconds: float = 10.0):
        self.token_path = token_path.resolve()
        self.project_registry = project_registry
        self.transport = transport or self._default_transport
        self.timeout_seconds = timeout_seconds

    def fetch(self, mr_url: object) -> GitLabMergeRequest:
        normalized = normalize_mr_url(mr_url, self.project_registry)
        definition = normalized.project
        request = Request(
            "https://{}/api/v4/projects/{}/merge_requests/{}".format(
                definition.gitlab.host, quote(definition.gitlab.project_path, safe=""), normalized.iid
            ),
            method="GET",
            headers={"PRIVATE-TOKEN": self._load_token(), "Accept": "application/json"},
        )
        try:
            status, body = self.transport(request, self.timeout_seconds)
        except (TimeoutError, socket.timeout):
            raise GitLabMRClientError("gitlab_timeout", "GitLab MR lookup timed out", 504)
        except (URLError, OSError):
            raise GitLabMRClientError("gitlab_unavailable", "GitLab MR lookup is unavailable", 502)
        if status in (401, 403):
            raise GitLabMRClientError("gitlab_auth_failed", "GitLab credential was rejected", 502)
        if status == 404:
            raise GitLabMRClientError("mr_not_found", "merge request was not found", 404)
        if status != 200:
            raise GitLabMRClientError("gitlab_request_failed", "GitLab MR lookup failed", 502)
        return self._parse_response(normalized, body)

    def fetch_version_refs(self, mr_url: object, version_id: str):
        """Binds a page-selected numeric diff ID to its immutable three refs."""

        if not isinstance(version_id, str) or re.fullmatch(r"[1-9][0-9]{0,18}", version_id) is None:
            raise GitLabMRClientError("invalid_page_version", "page diff version is invalid", 400)
        normalized = normalize_mr_url(mr_url, self.project_registry)
        definition = normalized.project
        request = Request(
            "https://{}/api/v4/projects/{}/merge_requests/{}/versions?per_page=1".format(
                definition.gitlab.host, quote(definition.gitlab.project_path, safe=""), normalized.iid),
            method="GET", headers={"PRIVATE-TOKEN": self._load_token(), "Accept": "application/json"})
        try:
            status, body = self.transport(request, self.timeout_seconds)
        except (TimeoutError, socket.timeout, URLError, OSError) as error:
            raise GitLabMRClientError("gitlab_unavailable", "GitLab version lookup is unavailable", 502) from error
        if status != 200 or len(body) > GITLAB_RESPONSE_LIMIT:
            raise GitLabMRClientError("page_version_unavailable", "GitLab page version cannot be verified", 502)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError) as error:
            raise GitLabMRClientError("gitlab_invalid_response", "GitLab version is invalid", 502) from error
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise GitLabMRClientError("gitlab_invalid_response", "GitLab version identity is invalid", 502)
        payload = payload[0]
        if str(payload.get("id")) != version_id:
            return None
        return {name + "_sha": self._required_sha(payload, name + "_commit_sha")
                for name in ("base", "start", "head")}

    def _load_token(self) -> str:
        try:
            info = self.token_path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not regular")
            token = self.token_path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            raise GitLabMRClientError("gitlab_token_unavailable", "GitLab credential is unavailable", 503)
        if len(token) < 16 or any(character.isspace() for character in token):
            raise GitLabMRClientError("gitlab_token_invalid", "GitLab credential is invalid", 503)
        return token

    def _parse_response(self, requested: NormalizedMRURL, body: bytes) -> GitLabMergeRequest:
        if len(body) > GITLAB_RESPONSE_LIMIT:
            raise GitLabMRClientError("gitlab_invalid_response", "GitLab response is too large", 502)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise GitLabMRClientError("gitlab_invalid_response", "GitLab response is not valid JSON", 502)
        if not isinstance(payload, dict):
            raise GitLabMRClientError("gitlab_invalid_response", "GitLab response must be an object", 502)
        if payload.get("iid") != requested.iid:
            raise GitLabMRClientError("gitlab_project_mismatch", "GitLab MR identity does not match the request", 502)
        try:
            response = normalize_mr_url(payload.get("web_url"), self.project_registry)
        except GitLabMRClientError:
            response = None
        if response is None or response.iid != requested.iid or response.project.project != requested.project.project:
            raise GitLabMRClientError("gitlab_project_mismatch", "GitLab MR project does not match the registered project", 502)
        state = self._required_text(payload, "state")
        if state != "opened":
            raise GitLabMRClientError("mr_not_open", "merge request is not open for testing", 409)
        diff_refs = payload.get("diff_refs")
        if not isinstance(diff_refs, dict):
            raise GitLabMRClientError("mr_diff_refs_not_ready", "merge request diff refs are not ready", 409)
        definition = requested.project
        return GitLabMergeRequest(
            project=definition.project,
            project_display_name=definition.display_name,
            iid=requested.iid,
            web_url="https://{}/{}/merge_requests/{}".format(
                definition.gitlab.host, definition.gitlab.project_path, requested.iid
            ),
            state=state,
            source_branch=self._required_text(payload, "source_branch"),
            target_branch=self._required_text(payload, "target_branch"),
            base_sha=self._required_sha(diff_refs, "base_sha"),
            start_sha=self._required_sha(diff_refs, "start_sha"),
            head_sha=self._required_sha(diff_refs, "head_sha"),
        )

    def _required_text(self, payload, field):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise GitLabMRClientError("gitlab_invalid_response", "GitLab response is missing required fields", 502)
        return value

    def _required_sha(self, payload, field):
        value = payload.get(field)
        if not isinstance(value, str) or not GIT_SHA_PATTERN.fullmatch(value):
            raise GitLabMRClientError("mr_diff_refs_not_ready", "merge request diff refs are not ready", 409)
        return value

    @staticmethod
    def _default_transport(request, timeout_seconds):
        try:
            with build_opener(_NoRedirectHandler()).open(request, timeout=timeout_seconds) as response:
                return response.status, response.read(GITLAB_RESPONSE_LIMIT + 1)
        except HTTPError as error:
            return error.code, error.read(GITLAB_RESPONSE_LIMIT + 1)


def normalize_mr_url(value: object, registry: ProjectRegistry) -> NormalizedMRURL:
    """Maps one safe MR page URL to exactly one registered project and IID."""

    if not isinstance(value, str) or not value or len(value) > 2048:
        raise GitLabMRClientError("invalid_mr_url", "mr_url is invalid", 400)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise GitLabMRClientError("invalid_mr_url", "mr_url is invalid", 400)
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or port is not None or not parsed.hostname
            or parsed.netloc.lower() != parsed.hostname.lower()):
        raise GitLabMRClientError("invalid_mr_url", "mr_url is not an allowed GitLab MR", 400)
    if not parsed.path.startswith("/") or "//" in parsed.path:
        raise GitLabMRClientError("invalid_mr_url", "mr_url is not an allowed GitLab MR", 400)
    parts = parsed.path.strip("/").split("/")
    if parts and parts[-1] in ("diffs", "commits"):
        if len(parts) < 5 or parts[-3] != "merge_requests":
            raise GitLabMRClientError("invalid_mr_url", "mr_url is not an allowed GitLab MR", 400)
        iid_text, project_parts = parts[-2], parts[:-3]
    else:
        if len(parts) < 4 or parts[-2] != "merge_requests":
            raise GitLabMRClientError("invalid_mr_url", "mr_url is not an allowed GitLab MR", 400)
        iid_text, project_parts = parts[-1], parts[:-2]
    if project_parts and project_parts[-1] == "-":
        project_parts = project_parts[:-1]
    if (not iid_text.isdigit() or int(iid_text) <= 0 or not project_parts
            or any(part in ("", ".", "..") for part in project_parts)):
        raise GitLabMRClientError("invalid_mr_url", "mr_url is not an allowed GitLab MR", 400)
    try:
        definition = registry.match_gitlab(parsed.hostname.lower(), "/".join(project_parts))
    except ProjectRegistryError as error:
        raise GitLabMRClientError(error.code, error.message, 400) from error
    return NormalizedMRURL(definition, int(iid_text))
