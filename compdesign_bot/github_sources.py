"""Curated official GitHub releases, with a persistent unauthenticated API budget."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from compdesign_bot.feeds import FetchReport, _plain_text, canonical_url
from compdesign_bot.models import Article, ArticleLink

MAX_REPOSITORIES = 10
MAX_CONFIG_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
MAX_CACHE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0
CACHE_TTL = timedelta(hours=6)
ERROR_COOLDOWN = timedelta(minutes=15)
RATE_LIMIT_COOLDOWN = timedelta(hours=1)
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "CompDesignBrief/1.0 (curated official releases)",
}


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str
    why: str
    topic: str
    docs_url: str = ""
    example_url: str = ""
    enabled: bool = True

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"


def _public_link(value: str) -> bool:
    """Links are operator-curated and never fetched by this collector."""
    if not isinstance(value, str) or len(value) > 1000 or not canonical_url(value):
        return False
    parts = urlsplit(value)
    return parts.scheme == "https" and parts.port in (None, 443)


def _validate_repository(repo: Repository) -> None:
    if not isinstance(repo.owner, str) or not _OWNER.fullmatch(repo.owner):
        raise ValueError("Repository owner is not a GitHub account name")
    if (
        not isinstance(repo.name, str)
        or not _NAME.fullmatch(repo.name)
        or repo.name in {".", ".."}
    ):
        raise ValueError("Repository name is not a GitHub repository name")
    for field_name in ("why", "topic"):
        value = getattr(repo, field_name)
        if not isinstance(value, str) or not value.strip() or len(value) > 400:
            raise ValueError(f"Repository {field_name} must be nonempty text of at most 400 characters")
    if type(repo.enabled) is not bool:
        raise ValueError("Repository enabled must be a boolean")
    for field_name in ("docs_url", "example_url"):
        value = getattr(repo, field_name)
        if not isinstance(value, str) or value and not _public_link(value):
            raise ValueError(f"Repository {field_name} must be a credential-free HTTPS URL")
        if (
            value and urlsplit(value).hostname == "github.com"
            and not urlsplit(value).path.lower().startswith(f"/{repo.full_name.lower()}/")
        ):
            raise ValueError(f"Repository {field_name} must belong to the configured repository")


def load_repositories(path: Path) -> list[Repository]:
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("Repository catalog is too large")
    data = json.loads(path.read_bytes())
    rows = data.get("repositories") if isinstance(data, dict) else data
    if not isinstance(rows, list) or len(rows) > MAX_REPOSITORIES:
        raise ValueError(f"repositories must be a list with at most {MAX_REPOSITORIES} entries")
    result: list[Repository] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each repository must be an object")  # noqa: TRY004 - invalid JSON data
        try:
            repo = Repository(**row)
        except TypeError as exc:
            raise ValueError("Invalid repository fields") from exc
        _validate_repository(repo)
        if repo.full_name.lower() in seen:
            raise ValueError("Repository catalog contains duplicate repositories")
        seen.add(repo.full_name.lower())
        result.append(repo)
    return result


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _changelog(body: str) -> str:
    """Remove Markdown transport noise without adding repository marketing text."""
    body = re.sub(r"```.*?```", "", body[:50000], flags=re.DOTALL)
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    body = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)
    lines = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"\**(?:full changelog|new contributors|contributors)\b", line, re.IGNORECASE):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^[0-9a-fA-F]{7,40}:\s+", "", line)
        line = re.sub(r"\s+by @[\w-]+\s+in\s+.*$", "", line)
        line = re.sub(r"https?://\S+", "", line)
        line = line.replace("`", "").replace("**", "")
        line = _plain_text(line, 1000)
        if line:
            lines.append(line if line[-1] in ".!?。！？" else f"{line}.")
    return " ".join(lines)[:6000]


class _FetchError(RuntimeError):
    def __init__(self, message: str, *, rate_limit: bool = False):
        super().__init__(message)
        self.rate_limit = rate_limit


async def _get_json(client: httpx.AsyncClient, path: str) -> object:
    # path is constructed only from locally validated owner/name, never API response links.
    try:
        async with (
            asyncio.timeout(REQUEST_TIMEOUT_SECONDS),
            client.stream(
                "GET", f"https://api.github.com/repos/{path}", headers=_HEADERS,
                timeout=15.0, follow_redirects=False,
            ) as response,
        ):
            if response.status_code != 200:
                raise _FetchError(
                    f"GitHub HTTP {response.status_code}",
                    rate_limit=response.status_code in {403, 429},
                )
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise _FetchError("GitHub response exceeds size limit")
        return json.loads(content)
    except TimeoutError:
        raise _FetchError("GitHub request timed out") from None
    except httpx.HTTPError as exc:
        raise _FetchError(f"GitHub network failure ({type(exc).__name__})") from None
    except (ValueError, UnicodeError):
        raise _FetchError("GitHub returned invalid JSON") from None


def _normalize_metadata(raw: object, repo: Repository) -> dict:
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("full_name"), str)
        or raw["full_name"].lower() != repo.full_name.lower()
    ):
        raise _FetchError("GitHub repository identity mismatch")
    if any(type(raw.get(field)) is not bool for field in ("archived", "disabled", "private")):
        raise _FetchError("GitHub repository state is missing")
    license_data = raw.get("license")
    license_id = license_data.get("spdx_id", "") if isinstance(license_data, dict) else ""
    if (
        not isinstance(license_id, str)
        or license_id in {"NOASSERTION", "NONE"}
        or not re.fullmatch(r"[A-Za-z0-9.+-]{1,60}", license_id)
    ):
        license_id = ""
    return {
        "full_name": repo.full_name,
        "archived": raw["archived"], "disabled": raw["disabled"], "private": raw["private"],
        "license": {"spdx_id": license_id},
    }


def _normalize_releases(raw: object, repo: Repository) -> list[dict]:
    if not isinstance(raw, list):
        raise _FetchError("GitHub releases response is not a list")
    releases: list[dict] = []
    for release in raw[:5]:
        if not isinstance(release, dict):
            continue
        if release.get("draft") is not False or release.get("prerelease") is not False:
            continue
        published = _timestamp(release.get("published_at"))
        title = release.get("name") or release.get("tag_name")
        body = release.get("body")
        link = release.get("html_url")
        if not published or not isinstance(title, str) or not isinstance(body, str):
            continue
        # Some maintainers leave the API prerelease flag false for explicitly named RCs.
        tag_name = release.get("tag_name", "")
        if not isinstance(tag_name, str):
            continue
        if re.search(
            r"(?:^|[\s._-])(?:alpha|beta|rc|pre|preview)(?:[\s._-]|\d|$)|release candidate",
            f"{title} {tag_name}", re.IGNORECASE,
        ):
            continue
        if not isinstance(link, str) or not _public_link(link):
            continue
        parts = urlsplit(link)
        expected_path = f"/{repo.full_name}/releases/tag/".lower()
        if (
            parts.hostname != "github.com"
            or not parts.path.lower().startswith(expected_path)
            or len(parts.path) <= len(expected_path)
        ):
            continue
        normalized_title = _plain_text(title, 240)
        if not normalized_title:
            continue
        releases.append({
            "name": normalized_title, "tag_name": tag_name[:240], "body": body[:6000],
            "html_url": canonical_url(link),
            "published_at": published.isoformat(), "draft": False, "prerelease": False,
        })
    return releases


def _articles(repo: Repository, metadata: dict, releases: list[dict]) -> list[Article]:
    if metadata["archived"] or metadata["disabled"] or metadata["private"]:
        return []
    links = [ArticleLink("GitHub 코드", repo.url)]
    if repo.docs_url:
        links.append(ArticleLink("공식 문서", repo.docs_url))
    if repo.example_url:
        links.append(ArticleLink("예제", repo.example_url))
    return [
        Article(
            title=f"{repo.full_name} release: {release['name']}"[:320],
            url=release["html_url"], source=f"GitHub · {repo.full_name}",
            summary=_changelog(release["body"]), published_at=_timestamp(release["published_at"]),
            kind="release", links=tuple(links), license=metadata["license"]["spdx_id"],
            topic_context=repo.topic, curator_note=repo.why,
        )
        for release in releases
    ]


def _read_cache(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            return {}
        data = json.loads(path.read_bytes())
        if data.get("version") != 2 or not isinstance(data.get("entries"), dict):
            return {}
        return data["entries"]
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _write_cache(path: Path, entries: dict) -> None:
    data = json.dumps({"version": 2, "entries": entries}, ensure_ascii=False).encode()
    if len(data) > MAX_CACHE_BYTES:
        raise OSError("GitHub cache exceeds size limit")
    name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        name = None
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


async def collect_releases(
    repositories: list[Repository], client: httpx.AsyncClient, cache_path: Path,
    now: datetime | None = None,
) -> FetchReport:
    """Only real published releases count as news; old repositories never get a new date."""
    if len(repositories) > MAX_REPOSITORIES:
        raise ValueError(f"At most {MAX_REPOSITORIES} repositories may be collected")
    for repo in repositories:
        _validate_repository(repo)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("Collection time must include a timezone")
    now = now.astimezone(UTC)
    report = FetchReport()
    enabled = list({repo.full_name.lower(): repo for repo in repositories if repo.enabled}.values())
    if not enabled:
        return report
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        lock = cache_path.with_suffix(cache_path.suffix + ".lock").open("a")
    except OSError:
        report.errors.append("GitHub: cache storage is unavailable")
        return report
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report.errors.append("GitHub: another release collection is running")
            return report
        existing = _read_cache(cache_path)
        entries: dict = {}
        semaphore = asyncio.Semaphore(2)

        async def collect(repo: Repository) -> FetchReport:
            key = repo.full_name.lower()
            cached = existing.get(key)
            if isinstance(cached, dict):
                checked = _timestamp(cached.get("checked_at"))
                retry_at = _timestamp(cached.get("retry_at"))
                if checked and checked <= now and retry_at and now < retry_at:
                    error = cached.get("error")
                    if isinstance(error, str) and error:
                        entries[key] = cached
                        return FetchReport(errors=[f"{repo.full_name}: GitHub retry cooldown active"])
                    if now - checked < CACHE_TTL:
                        try:
                            metadata = _normalize_metadata(cached.get("metadata"), repo)
                            releases = _normalize_releases(cached.get("releases"), repo)
                            entries[key] = cached
                            return FetchReport(articles=_articles(repo, metadata, releases))
                        except _FetchError:
                            pass
            try:
                async with semaphore:
                    metadata = _normalize_metadata(await _get_json(client, repo.full_name), repo)
                    if metadata["archived"] or metadata["disabled"] or metadata["private"]:
                        releases = []
                    else:
                        releases = _normalize_releases(
                            await _get_json(client, f"{repo.full_name}/releases?per_page=5"), repo,
                        )
                entries[key] = {
                    "checked_at": now.isoformat(), "retry_at": (now + CACHE_TTL).isoformat(),
                    "metadata": metadata, "releases": releases,
                }
                return FetchReport(articles=_articles(repo, metadata, releases))
            except _FetchError as exc:
                cooldown = RATE_LIMIT_COOLDOWN if exc.rate_limit else ERROR_COOLDOWN
                entries[key] = {
                    "checked_at": now.isoformat(), "retry_at": (now + cooldown).isoformat(),
                    "error": str(exc),
                }
                return FetchReport(errors=[f"{repo.full_name}: {exc}"])

        for result in await asyncio.gather(*(collect(repo) for repo in enabled)):
            report.articles.extend(result.articles)
            report.errors.extend(result.errors)
        try:
            _write_cache(cache_path, entries)
        except OSError:
            report.errors.append("GitHub: release cache could not be saved")
    return report
