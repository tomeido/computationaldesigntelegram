"""Keep useful links actually present in a source; never guess a project's repository."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from compdesign_bot.models import ArticleLink

MAX_SOURCE_CHARS = 60_000
MAX_LINK_CANDIDATES = 80
MAX_SOURCE_LINKS = 3
_HIDDEN_TAGS = {"script", "style", "noscript", "template", "iframe", "svg"}
_TRACKING_KEYS = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "igshid"}
_GITHUB_NAVIGATION = {
    "about", "apps", "collections", "contact", "enterprise", "explore", "features",
    "issues", "login", "marketplace", "new", "notifications", "orgs", "pricing",
    "pulls", "search", "security", "settings", "site", "sponsors", "topics", "users",
}
_DEMO = re.compile(r"\b(?:live\s+demo|interactive\s+demo|demo|playground)\b|데모|체험", re.IGNORECASE)
_PROJECT = re.compile(
    r"\bproject\s+(?:page|website|site|homepage)\b|^\s*project\s*:?\s*$|프로젝트\s*(?:페이지|사이트)",
    re.IGNORECASE,
)
_DOCS = re.compile(r"\b(?:documentation|docs)\b|사용\s*문서|개발자\s*문서", re.IGNORECASE)
_PLAIN_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)


def public_resource_url(url: str, base_url: str = "") -> str:
    """Validate a public web link without resolving or requesting it.

    This is deliberately narrower than a general URL parser. Resource links do
    not need credentials, IP literals, private host names, or unusual ports.
    It is not an SSRF allowlist: callers must not fetch arbitrary returned URLs.
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        return ""
    url = unescape(url).strip()
    if not url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        return ""
    if re.search(r"[\\<>\"'`]|%(?:0[0-9a-f]|1[0-9a-f]|7f|5c)", url, re.IGNORECASE):
        return ""
    try:
        if base_url and not urlsplit(url).scheme:
            base = public_resource_url(base_url)
            if not base:
                return ""
            url = urljoin(base, url)
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        if parts.username is not None or parts.password is not None or "%" in parts.netloc:
            return ""
        if parts.port not in {None, 80, 443}:
            return ""
        host = parts.hostname.encode("idna").decode("ascii").lower()
        host = host.removesuffix(".")
        if len(host) > 253 or "." not in host:
            return ""
        if host.rsplit(".", 1)[-1] in {
            "localhost", "local", "internal", "test", "invalid", "onion", "home", "lan", "corp",
        }:
            return ""
        labels = host.split(".")
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
            return ""
        if len(labels[-1]) < 2 or not re.search(r"[a-z]", labels[-1]):
            return ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return ""
        scheme = parts.scheme.lower()
        if parts.port and not (scheme == "http" and parts.port == 80) and not (
            scheme == "https" and parts.port == 443
        ):
            host += f":{parts.port}"
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
        ]
        return urlunsplit((scheme, host, parts.path or "/", urlencode(query), ""))
    except (ValueError, UnicodeError):
        return ""


def _github_code(url: str) -> bool:
    parts = urlsplit(url)
    if parts.hostname not in {"github.com", "www.github.com"}:
        return False
    path = parts.path.strip("/").split("/")
    if len(path) < 2 or path[0].lower() in _GITHUB_NAVIGATION:
        return False
    if any(unquote(segment) in {".", ".."} or "/" in unquote(segment) for segment in path):
        return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", path[0]):
        return False
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", path[1]) or path[1] in {".", ".."}:
        return False
    # Files and directories are useful code links. Issues, PRs, account pages,
    # actions, settings and login paths do not establish a repository resource.
    return len(path) == 2 or (len(path) >= 4 and path[2] in {"tree", "blob"})


def _label(url: str, description: str) -> str:
    if _github_code(url):
        return "코드 (원문 링크)"
    host = urlsplit(url).hostname or ""
    if host in {"github.com", "www.github.com"} or "github.com" in host:
        return ""
    if _DEMO.search(description):
        return "데모 (원문 링크)"
    if _PROJECT.search(description):
        return "프로젝트 (원문 링크)"
    if _DOCS.search(description):
        return "문서 (원문 링크)"
    return ""


@dataclass
class _Anchor:
    href: str
    text: list[str] = field(default_factory=list)


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[_Anchor] = []
        self.text: list[str] = []
        self.current: _Anchor | None = None
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self.hidden_depth += 1
        if self.hidden_depth:
            return
        if tag == "a" and len(self.links) < MAX_LINK_CANDIDATES:
            self.current = _Anchor(dict(attrs).get("href") or "")
            self.links.append(self.current)
        if tag in {"p", "div", "br", "li"}:
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
        if self.hidden_depth:
            return
        if tag == "a":
            self.current = None
        if tag in {"p", "div", "li"}:
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        self.text.append(data)
        if self.current:
            self.current.text.append(data)


def source_links(raw_html: str, base_url: str) -> tuple[ArticleLink, ...]:
    """Extract at most three explicit code/demo/project links from feed content.

    Labels describe the source's link, not a verified author/repository match.
    The source's own article URL and unrelated navigation links are omitted.
    """
    if not isinstance(raw_html, str):
        return ()
    parser = _LinkExtractor()
    parser.feed(raw_html[:MAX_SOURCE_CHARS])
    parser.close()
    candidates = [(anchor.href, " ".join(anchor.text)) for anchor in parser.links]
    plain = "".join(parser.text)
    for match in list(_PLAIN_URL.finditer(plain))[:MAX_LINK_CANDIDATES]:
        href = match.group(0).rstrip(".,;:!?)]}")
        # For a plain non-GitHub URL, require an explicit adjacent resource label.
        prefix = plain[max(0, match.start() - 65):match.start()]
        prefix = re.split(r"[\n.!?]", prefix)[-1]
        candidates.append((href, prefix))
    original = public_resource_url(base_url).rstrip("/")
    found: list[ArticleLink] = []
    seen: set[str] = set()
    for href, description in candidates:
        url = public_resource_url(href, base_url)
        identity = url.rstrip("/")
        if not url or identity == original or identity in seen:
            continue
        label = _label(url, description)
        if not label:
            continue
        seen.add(identity)
        found.append(ArticleLink(label=label, url=url))
    # Code and runnable demos are most useful when a feed includes many links.
    order = {"코드 (원문 링크)": 0, "데모 (원문 링크)": 1, "프로젝트 (원문 링크)": 2, "문서 (원문 링크)": 3}
    found.sort(key=lambda link: order[link.label])
    return tuple(found[:MAX_SOURCE_LINKS])
