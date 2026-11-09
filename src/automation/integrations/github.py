"""GitHub REST API: merged pull requests of a repository since a date.

Uses ``GET /repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc`` (100 per page, following the
``Link: rel="next"`` header) and keeps the pull requests whose ``merged_at`` is on or after ``since``. API version
``2026-03-10`` (the latest on 2026-10-01; the default for requests without the header is ``2022-11-28``).
Without ``GITHUB_TOKEN`` only public repositories work, at 60 requests an hour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import httpx
from pydantic import SecretStr

from automation.http import HttpClient

API_URL = "https://api.github.com"
API_VERSION = "2026-03-10"
MAX_PAGES = 10


class GitHubError(Exception):
    pass


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    author: str
    merged_at: datetime


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class GitHub:
    def __init__(self, http: HttpClient, token: SecretStr | None = None, base_url: str = API_URL) -> None:
        self._http = http
        self._token = token
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        if self._token is not None and self._token.get_secret_value():
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        return headers

    def _get(self, url: str, params: dict[str, str | int] | None = None) -> httpx.Response:
        response = self._http.get(url, params=params, headers=self._headers(), raise_for_status=False)
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubError("GitHub API rate limit reached; set GITHUB_TOKEN for a higher limit")
        if response.status_code == 404:
            raise GitHubError(f"not found: {url} (private repository without a token, or a typo?)")
        if response.status_code >= 400:
            raise GitHubError(f"GitHub API returned HTTP {response.status_code}: {response.text[:200]}")
        return response

    def merged_pull_requests(self, repo: str, since: datetime) -> list[PullRequest]:
        """Pull requests of ``owner/name`` merged at or after ``since``, newest first."""
        url: str | None = f"{self.base_url}/repos/{repo}/pulls"
        params: dict[str, str | int] | None = {
            "state": "closed",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100,
        }
        merged: list[PullRequest] = []
        for _page in range(MAX_PAGES):
            if url is None:
                break
            response = self._get(url, params)
            older_page = False
            for pr in response.json():
                if _parse(pr["updated_at"]) < since:
                    older_page = True  # sorted by update time: everything after this is older
                    break
                if pr.get("merged_at") and _parse(pr["merged_at"]) >= since:
                    merged.append(
                        PullRequest(
                            repo=repo,
                            number=int(pr["number"]),
                            title=pr["title"],
                            url=pr["html_url"],
                            author=(pr.get("user") or {}).get("login", "unknown"),
                            merged_at=_parse(pr["merged_at"]),
                        )
                    )
            if older_page:
                break
            url = response.links.get("next", {}).get("url")
            params = None  # the next link already carries the query
        return sorted(merged, key=lambda p: p.merged_at, reverse=True)
