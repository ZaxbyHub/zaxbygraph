from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import quote

from zaxbygraph.repo import validate_slug

#: Issues listing page size (also the "is there another page" threshold).
ISSUES_PAGE_SIZE = 100

#: Seconds before a hung `gh` subprocess is killed instead of stalling the
#: sync (which holds the whole-run lock) indefinitely.
GH_TIMEOUT_S = 300

_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _status_from_stderr(stderr: str) -> int | None:
    text = stderr.lower()
    # Digit matches are word-boundary anchored: a bare "404"/"403"/"429"
    # inside a larger number (an issue number, a URL, a request id) must not
    # drive classification — the 404/410 verdicts durably flip items.state.
    if "429" in text or "rate limit" in text or "secondary rate" in text:
        return 429
    if re.search(r"\b403\b", text) or "forbidden" in text:
        return 403
    if re.search(r"\b404\b", text) or "not found" in text:
        return 404
    if re.search(r"\b410\b", text) or re.search(r"\bgone\b", text):
        return 410
    if re.search(r"\b401\b", text) or "unauthorized" in text:
        return 401
    return None


class GitHubSource(Protocol):
    def list_issues(self, since: str | None) -> Iterator[list[dict] | dict]: ...
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

    def _api(self, path: str, paginate: bool = False, _enrich_rate: bool = True) -> object:
        if ".." in path or path.startswith(("/", "-")):
            raise GitHubError(f"refusing API path: {path!r}")
        cmd = [self.gh_bin, "api", path]
        if paginate:
            cmd.append("--paginate")
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=GH_TIMEOUT_S,
            )
        except FileNotFoundError as exc:
            raise GitHubError(
                "gh CLI not found. Install GitHub CLI and authenticate with gh auth login."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(
                f"gh api timed out after {GH_TIMEOUT_S}s: {path}"
            ) from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "gh api failed").strip()
            exc = GitHubError(err, status=_status_from_stderr(err))
            if _enrich_rate:
                exc = self._with_rate_budget(exc)
            raise exc
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

    def _with_rate_budget(self, exc: GitHubError) -> GitHubError:
        """Attach the machine-readable budget to a truly exhausted core limit
        (issue #5): the stderr classifier puts every rate-limit flavor in the
        429 class, so only a probe-reported `remaining == 0` qualifies — a
        non-zero budget means the failure is not the core limit and stays
        unenriched, and plain 403s (permissions, SSO) never get here. The
        `rate_limit` endpoint is exempt from the REST budget; a probe failure
        keeps the original error (fail-fast as before)."""
        if exc.status != 429:
            return exc
        try:
            data = self._api("rate_limit", _enrich_rate=False)
        except GitHubError:
            return exc
        core = data.get("resources", {}).get("core") if isinstance(data, dict) else None
        if not isinstance(core, dict):
            return exc
        remaining = core.get("remaining")
        reset = core.get("reset")
        # bool is an int subclass and NaN compares unequal to itself; a
        # hostile/buggy value must never crash the enrichment path (that
        # would replace the original 429), so anything unrepresentable keeps
        # the original error.
        if (
            remaining != 0
            or isinstance(reset, bool)
            or not isinstance(reset, (int, float))
            or reset != reset
        ):
            return exc
        try:
            exc.rate_limit_remaining = 0
            exc.rate_limit_reset = datetime.fromtimestamp(
                float(reset), tz=timezone.utc
            ).strftime(_ISO_Z)
        except (OverflowError, OSError, ValueError):
            return exc
        return exc

    def list_issues(self, since: str | None) -> Iterator[list[dict]]:
        """Stream the issues listing one page per `gh` call (issue #5 AC1).

        The sync layer commits each page before the next is fetched, so a
        failure mid-listing keeps everything already delivered instead of
        discarding the whole buffered listing. Yields pages (lists of raw
        item dicts); the item-or-page yield contract is documented in the
        GitHubSource protocol."""
        qs = "state=all&per_page=100&sort=updated&direction=asc"
        if since:
            qs += f"&since={quote(str(since), safe=':-')}"
        page = 1
        while True:
            data = self._api(f"repos/{self.slug}/issues?{qs}&page={page}")
            if not isinstance(data, list):
                raise GitHubError("unexpected issues payload")
            items = [item for item in data if isinstance(item, dict)]
            if items:
                yield items
            if len(data) < ISSUES_PAGE_SIZE:
                return
            page += 1

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
