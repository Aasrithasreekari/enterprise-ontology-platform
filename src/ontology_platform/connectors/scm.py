"""Source-code management connectors: GitHub, GitLab, Bitbucket Cloud.

Each lists the repositories of an org/group/workspace and fetches a configurable set of
well-known files (Backstage ``catalog-info.yaml``, ``README.md``, OpenAPI specs) used by the
extractors. Not verified against live tenants — see ARCHITECTURE.md §12.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import datetime
from urllib.parse import quote

from pydantic import BaseModel, Field

from ontology_platform.connectors.base import Connector, RecordKind, RepositoryPayload, SourceRecord, register_connector
from ontology_platform.connectors.http import RetryingHttpClient, parse_link_next

DEFAULT_FILES = ["catalog-info.yaml", "README.md", "openapi.yaml", "openapi.json"]


class _ScmConfig(BaseModel):
    files: list[str] = Field(default_factory=lambda: list(DEFAULT_FILES))
    include_archived: bool = False
    max_repos: int | None = Field(default=None, ge=1)


def _max_ts(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if current is None:
        return candidate
    return max(current, candidate, key=lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")))


def _newer(ts: str | None, since: str | None) -> bool:
    if since is None or ts is None:
        return True
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) > datetime.fromisoformat(since.replace("Z", "+00:00"))


# ----------------------------------------------------------------------------- GitHub
class GitHubConfig(_ScmConfig):
    org: str = Field(min_length=1)
    api_url: str = "https://api.github.com"


@register_connector
class GitHubConnector(Connector):
    type = "github"
    source_system = "github"
    config_model = GitHubConfig
    supports_incremental = True
    config: GitHubConfig

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        http = RetryingHttpClient(
            self.config.api_url,
            self.settings,
            headers={"Authorization": f"Bearer {self.secret}", "Accept": "application/vnd.github+json"},
            transport=self._transport,
        )
        try:
            url: str | None = f"/orgs/{quote(self.config.org)}/repos"
            params: dict | None = {"per_page": 100, "type": "all", "sort": "updated", "direction": "desc"}
            count = 0
            self._cursor = since
            while url:
                resp = await http.request("GET", url, params=params)
                params = None  # the next link already carries query params
                for repo in resp.json():
                    updated = repo.get("pushed_at") or repo.get("updated_at")
                    if not _newer(updated, since):
                        return  # sorted by updated desc: everything after is older
                    if repo.get("archived") and not self.config.include_archived:
                        continue
                    files = {}
                    for path in self.config.files:
                        text = await http.get_text(
                            f"/repos/{repo['full_name']}/contents/{quote(path)}",
                            params={"ref": repo.get("default_branch") or "main"},
                            headers={"Accept": "application/vnd.github.raw"},
                        )
                        if text is not None:
                            files[path] = text
                    payload = RepositoryPayload(
                        provider="github",
                        path=repo["full_name"],
                        name=repo["name"],
                        url=repo.get("html_url"),
                        description=repo.get("description"),
                        topics=repo.get("topics") or [],
                        default_branch=repo.get("default_branch"),
                        language=repo.get("language"),
                        archived=bool(repo.get("archived")),
                        files=files,
                    )
                    self._cursor = _max_ts(self._cursor, updated)
                    yield SourceRecord(
                        kind=RecordKind.REPOSITORY,
                        external_id=f"github:{repo['full_name']}",
                        source_system="github",
                        source_url=repo.get("html_url"),
                        payload=payload.model_dump(),
                    )
                    count += 1
                    if self.config.max_repos and count >= self.config.max_repos:
                        return
                url = parse_link_next(resp.headers.get("Link"))
        finally:
            await http.aclose()


# ----------------------------------------------------------------------------- GitLab
class GitLabConfig(_ScmConfig):
    group: str = Field(min_length=1)
    base_url: str = "https://gitlab.com"


@register_connector
class GitLabConnector(Connector):
    type = "gitlab"
    source_system = "gitlab"
    config_model = GitLabConfig
    supports_incremental = True
    config: GitLabConfig

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        http = RetryingHttpClient(
            self.config.base_url, self.settings, headers={"PRIVATE-TOKEN": self.secret or ""}, transport=self._transport
        )
        try:
            page: str | None = "1"
            count = 0
            self._cursor = since
            while page:
                params = {"include_subgroups": "true", "per_page": 100, "page": page, "order_by": "last_activity_at"}
                if since:
                    params["last_activity_after"] = since
                resp = await http.request("GET", f"/api/v4/groups/{quote(self.config.group, safe='')}/projects", params=params)
                for proj in resp.json():
                    if proj.get("archived") and not self.config.include_archived:
                        continue
                    branch = proj.get("default_branch") or "main"
                    files = {}
                    for path in self.config.files:
                        text = await http.get_text(
                            f"/api/v4/projects/{proj['id']}/repository/files/{quote(path, safe='')}/raw", params={"ref": branch}
                        )
                        if text is not None:
                            files[path] = text
                    payload = RepositoryPayload(
                        provider="gitlab",
                        path=proj["path_with_namespace"],
                        name=proj["path"],
                        url=proj.get("web_url"),
                        description=proj.get("description"),
                        topics=proj.get("topics") or proj.get("tag_list") or [],
                        default_branch=branch,
                        archived=bool(proj.get("archived")),
                        files=files,
                    )
                    self._cursor = _max_ts(self._cursor, proj.get("last_activity_at"))
                    yield SourceRecord(
                        kind=RecordKind.REPOSITORY,
                        external_id=f"gitlab:{proj['path_with_namespace']}",
                        source_system="gitlab",
                        source_url=proj.get("web_url"),
                        payload=payload.model_dump(),
                    )
                    count += 1
                    if self.config.max_repos and count >= self.config.max_repos:
                        return
                page = resp.headers.get("X-Next-Page") or None
        finally:
            await http.aclose()


# ----------------------------------------------------------------------------- Bitbucket Cloud
class BitbucketConfig(_ScmConfig):
    workspace: str = Field(min_length=1)
    api_url: str = "https://api.bitbucket.org"


@register_connector
class BitbucketConnector(Connector):
    """Secret: an access token, or ``username:app_password`` for basic auth."""

    type = "bitbucket"
    source_system = "bitbucket"
    config_model = BitbucketConfig
    supports_incremental = True
    config: BitbucketConfig

    def _auth_headers(self) -> dict[str, str]:
        secret = self.secret or ""
        if ":" in secret:
            return {"Authorization": "Basic " + base64.b64encode(secret.encode()).decode()}
        return {"Authorization": f"Bearer {secret}"}

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        http = RetryingHttpClient(self.config.api_url, self.settings, headers=self._auth_headers(), transport=self._transport)
        try:
            url: str | None = f"/2.0/repositories/{quote(self.config.workspace)}"
            params: dict | None = {"pagelen": 100}
            if since:
                params["q"] = f'updated_on > "{since}"'
            count = 0
            self._cursor = since
            while url:
                data = await http.get_json(url, params=params)
                params = None
                for repo in data.get("values", []):
                    branch = (repo.get("mainbranch") or {}).get("name") or "main"
                    files = {}
                    for path in self.config.files:
                        text = await http.get_text(f"/2.0/repositories/{repo['full_name']}/src/{quote(branch)}/{quote(path)}")
                        if text is not None:
                            files[path] = text
                    html = ((repo.get("links") or {}).get("html") or {}).get("href")
                    payload = RepositoryPayload(
                        provider="bitbucket",
                        path=repo["full_name"],
                        name=repo.get("slug") or repo["name"],
                        url=html,
                        description=repo.get("description"),
                        default_branch=branch,
                        language=repo.get("language"),
                        files=files,
                    )
                    self._cursor = _max_ts(self._cursor, repo.get("updated_on"))
                    yield SourceRecord(
                        kind=RecordKind.REPOSITORY,
                        external_id=f"bitbucket:{repo['full_name']}",
                        source_system="bitbucket",
                        source_url=html,
                        payload=payload.model_dump(),
                    )
                    count += 1
                    if self.config.max_repos and count >= self.config.max_repos:
                        return
                url = data.get("next")
        finally:
            await http.aclose()
