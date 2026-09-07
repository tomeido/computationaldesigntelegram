"""Bounded, failure-isolated RSS/Atom collection from an explicit source list."""

from __future__ import annotations

import asyncio
import calendar
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import httpx

from compdesign_bot.models import ARTICLE_KINDS, Article
from compdesign_bot.resources import source_links

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ENTRIES_PER_SOURCE = 80
MAX_SOURCES = 40
MAX_SUMMARY_CHARS = 6000
FETCH_TIMEOUT_SECONDS = 20.0
USER_AGENT = "CompDesignBrief/1.0 (RSS reader; Korean computational design digest)"


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    enabled: bool = True
    topic: str = ""
    weight: float = 1.0
    discovery: bool = False
    kind: str = "news"


@dataclass
class FetchReport:
    articles: list[Article] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1
        elif tag in {"br", "p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _plain_text(value: str, limit: int) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return re.sub(r"\s+", " ", unescape("".join(parser.parts))).strip()[:limit]


def canonical_url(url: str) -> str:
    """Remove common tracking parameters without discarding meaningful queries."""
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        if parts.username or parts.password or any(c.isspace() for c in parts.netloc):
            return ""
        # Accessing port rejects malformed ports; preserve non-default ports.
        port = parts.port
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if (
            port
            and not (parts.scheme.lower() == "https" and port == 443)
            and not (parts.scheme.lower() == "http" and port == 80)
        ):
            host += f":{port}"
        tracking = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "igshid"}
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in tracking
        ]
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(query), ""))
    except (ValueError, TypeError):
        return ""


def load_sources(path: Path) -> list[Source]:
    """Read a JSON list or {"sources": [...]} and validate operator configuration."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(rows, list) or len(rows) > MAX_SOURCES:
        raise ValueError(f"sources must be a list with at most {MAX_SOURCES} entries")
    result: list[Source] = []
    names: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Source {index} must be an object")  # noqa: TRY004 - invalid JSON data
        try:
            source = Source(**row)
        except TypeError as exc:
            raise ValueError(f"Invalid fields in source {index}") from exc
        if not isinstance(source.name, str) or not source.name.strip() or source.name in names:
            raise ValueError(f"Source {index} needs a unique nonempty name")
        if not isinstance(source.url, str) or not canonical_url(source.url):
            raise ValueError(f"Source {index} needs an HTTP(S) URL without credentials")
        if type(source.enabled) is not bool or type(source.discovery) is not bool:
            raise ValueError(f"Source {index} enabled/discovery must be booleans")
        if not isinstance(source.topic, str):
            raise ValueError(f"Source {index} topic must be text")  # noqa: TRY004 - invalid JSON data
        if not isinstance(source.kind, str) or source.kind not in ARTICLE_KINDS:
            raise ValueError(f"Source {index} kind must be news, paper, funding, showcase or release")
        if isinstance(source.weight, bool) or not isinstance(source.weight, (int, float)):
            raise ValueError(f"Source {index} weight must be a finite nonnegative number")  # noqa: TRY004
        if not math.isfinite(source.weight) or source.weight < 0:
            raise ValueError(f"Source {index} weight must be a finite nonnegative number")
        names.add(source.name)
        result.append(source)
    return result


def _published_at(entry: feedparser.FeedParserDict) -> datetime | None:
    # Do not use updated/updated_parsed: editing an old post must not make it news.
    # Explicit membership avoids feedparser's backwards-compatible alias lookup.
    if "published_parsed" not in entry or not entry["published_parsed"]:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(entry["published_parsed"]), UTC)
    except (OverflowError, ValueError, TypeError):
        return None


def _parse_feed(content: bytes, source: Source, base_url: str) -> FetchReport:
    parsed = feedparser.parse(
        content, response_headers={"content-location": base_url, "content-type": "application/xml"}
    )
    report = FetchReport()
    if not parsed.get("version"):
        report.errors.append(f"{source.name}: response is not an RSS/Atom feed")
        return report
    if parsed.get("bozo"):
        report.errors.append(f"{source.name}: malformed feed; recovered valid entries only")
    for entry in parsed.entries[:MAX_ENTRIES_PER_SOURCE]:
        try:
            title = _plain_text(str(entry.get("title", "")), 500)
            link = entry.get("link", "")
            if not link:
                link = next(
                    (
                        item.get("href", "")
                        for item in entry.get("links", [])
                        if item.get("rel") == "alternate"
                    ),
                    "",
                )
            url = canonical_url(urljoin(base_url, str(link))) if link else ""
            if not title or not url:
                continue
            raw_summary = next(
                (item.get("value", "") for item in entry.get("content", []) if item.get("value")),
                entry.get("summary", ""),
            )
            # Search RSS descriptions repeat the headline; they are not article text.
            discovery = source.discovery or urlsplit(base_url).hostname == "news.google.com"
            summary = "" if discovery else _plain_text(str(raw_summary), MAX_SUMMARY_CHARS)
            # arXiv authors often put project/code URLs in their comments rather
            # than the abstract. Preserve those explicit links, without treating
            # author comments as research findings or verified publication data.
            resource_content = str(raw_summary)
            if source.kind == "paper" and isinstance(entry.get("arxiv_comment"), str):
                resource_content += "\n" + entry["arxiv_comment"]
            report.articles.append(
                Article(
                    title=title,
                    url=url,
                    source=source.name,
                    summary=summary,
                    published_at=_published_at(entry),
                    kind=source.kind,
                    links=() if discovery else source_links(resource_content, url),
                )
            )
        except (ValueError, TypeError, AttributeError):
            report.errors.append(f"{source.name}: skipped an invalid entry")
    return report


async def collect_articles(sources: list[Source], client: httpx.AsyncClient) -> FetchReport:
    """Collect sources concurrently; one source failure never loses other results."""
    if len(sources) > MAX_SOURCES:
        raise ValueError(f"At most {MAX_SOURCES} sources are supported")
    semaphore = asyncio.Semaphore(4)

    async def collect(source: Source) -> FetchReport:
        async with semaphore:
            try:
                async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
                    async with client.stream(
                        "GET",
                        source.url,
                        timeout=FETCH_TIMEOUT_SECONDS,
                        follow_redirects=True,
                        headers={
                            "User-Agent": USER_AGENT,
                            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9",
                        },
                    ) as response:
                        response.raise_for_status()
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > MAX_FEED_BYTES:
                                return FetchReport(
                                    errors=[f"{source.name}: feed exceeds {MAX_FEED_BYTES} bytes"]
                                )
                        return _parse_feed(bytes(content), source, str(response.url))
            except httpx.HTTPStatusError as exc:
                return FetchReport(errors=[f"{source.name}: HTTP {exc.response.status_code}"])
            except (httpx.HTTPError, TimeoutError) as exc:
                return FetchReport(errors=[f"{source.name}: {type(exc).__name__}"])
            except Exception as exc:  # noqa: BLE001 - one malformed source must not stop the digest
                # Parser bugs or unusual encodings are also isolated to the source.
                return FetchReport(errors=[f"{source.name}: {type(exc).__name__}"])

    batches = await asyncio.gather(*(collect(source) for source in sources if source.enabled))
    report = FetchReport()
    seen: set[str] = set()
    for batch in batches:
        report.errors.extend(batch.errors)
        for article in batch.articles:
            if article.url not in seen:
                seen.add(article.url)
                report.articles.append(article)
    return report
