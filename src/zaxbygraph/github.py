from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from typing import Protocol
from urllib.parse import quote

from zaxbygraph.repo import validate_slug


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _status_from_stderr(stderr: str) -> int | None:
    text = stderr.lower()
    if "429" in text or "rate limit" in text or "secondary rate" in text:
        return 429
    if "403" in text or "forbidden" in text:
        return 403
    if "404" in text or "not found" in text:
        return 404
    if "401" in text or "unauthorized" in text:
        return 401
    return None


class GitHubSource(Protocol):
    def list_issues(self, since: str | None) -> Iterator[dict]: ...
    def get_pull(self, number: int) -> dict: ...
    def list_issue_comments(self, number: int) -> list[dict]: ...
    def list_reviews(self, number: int) -> list[dict]: ...
    def list_review_comments(self, number: int) -> list[dict]: ...
    def list_pr_files(self, number: int) -> list[dict]: ...
    def list_releases(self) -> list[dict]: ...


class GhApiSource:
    """Talks to GitHub through the `gh` CLI. No tokens stored here."""

    def __init__(self, owner: str, repo: str, gh_bin: str = "gh") -> None:
        slug = validate_slug(f"{owner}/{repo}")
        self.owner, self.repo = slug.split("/", 1)
        self.gh_bin = gh_bin
        self.slug = slug

    def _api(self, path: str, paginate: bool = False) -> object:
        if ".." in path or path.startswith("/"):
            raise GitHubError(f"refusing API path: {path!r}")
        cmd = [self.gh_bin, "api", path]
        if paginate:
            cmd.append("--paginate")
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise GitHubError(
                "gh CLI not found. Install GitHub CLI and authenticate with gh auth login."
            ) from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "gh api failed").strip()
            raise GitHubError(err, status=_status_from_stderr(err))
        text = proc.stdout.strip()
        if not text:
            return []
        if paginate and text.startswith("["):
            chunks: list[object] = []
            decoder = json.JSONDecoder()
            idx = 0
            while idx < len(text):
                while idx < len(text) and text[idx].isspace():
                    idx += 1
                if idx >= len(text):
                    break
                obj, end = decoder.raw_decode(text, idx)
                chunks.append(obj)
                idx = end
            merged: list[object] = []
            for chunk in chunks:
                if isinstance(chunk, list):
                    merged.extend(chunk)
                else:
                    merged.append(chunk)
            return merged
        return json.loads(text)

    def list_issues(self, since: str | None) -> Iterator[dict]:
        qs = "state=all&per_page=100&sort=updated&direction=asc"
        if since:
            qs += f"&since={quote(str(since), safe=':-')}"
        data = self._api(f"repos/{self.slug}/issues?{qs}", paginate=True)
        if not isinstance(data, list):
            raise GitHubError("unexpected issues payload")
        for item in data:
            if isinstance(item, dict):
                yield item

    def get_pull(self, number: int) -> dict:
        data = self._api(f"repos/{self.slug}/pulls/{int(number)}")
        if not isinstance(data, dict):
            raise GitHubError(f"unexpected pull payload for #{number}")
        return data

    def list_issue_comments(self, number: int) -> list[dict]:
        data = self._api(
            f"repos/{self.slug}/issues/{int(number)}/comments?per_page=100",
            paginate=True,
        )
        return list(data) if isinstance(data, list) else []

    def list_reviews(self, number: int) -> list[dict]:
        data = self._api(
            f"repos/{self.slug}/pulls/{int(number)}/reviews?per_page=100",
            paginate=True,
        )
        return list(data) if isinstance(data, list) else []

    def list_review_comments(self, number: int) -> list[dict]:
        data = self._api(
            f"repos/{self.slug}/pulls/{int(number)}/comments?per_page=100",
            paginate=True,
        )
        return list(data) if isinstance(data, list) else []

    def list_pr_files(self, number: int) -> list[dict]:
        data = self._api(
            f"repos/{self.slug}/pulls/{int(number)}/files?per_page=100",
            paginate=True,
        )
        return list(data) if isinstance(data, list) else []

    def list_releases(self) -> list[dict]:
        data = self._api(
            f"repos/{self.slug}/releases?per_page=100",
            paginate=True,
        )
        return list(data) if isinstance(data, list) else []
