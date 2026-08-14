"""Deterministic collectors for the supported official source formats."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, overload
from urllib.parse import unquote, urldefrag, urljoin, urlsplit
from urllib.request import Request, urlopen

from .config import NetworkConfig, SourceConfig
from .datetime_utils import beijing_isoformat
from .model import NewsItem, canonicalize_url

LOGGER = logging.getLogger(__name__)
JINA_READER_BASE = "https://r.jina.ai/"


class CollectionError(RuntimeError):
    """Raised when an entire source cannot be downloaded or parsed."""


@dataclass(frozen=True, slots=True)
class CollectionIssue:
    """A malformed entry that does not invalidate the rest of its source."""

    source: str
    stage: str
    title: str = ""
    url: str = ""
    message: str = ""
    index: int | None = None

    @property
    def item_title(self) -> str:
        return self.title

    @property
    def error(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class CollectionBatch(Sequence[NewsItem]):
    """Valid items plus independent entry-level parsing issues."""

    items: tuple[NewsItem, ...]
    issues: tuple[CollectionIssue, ...] = ()

    def __iter__(self) -> Iterator[NewsItem]:
        return iter(self.items)

    @overload
    def __getitem__(self, index: int) -> NewsItem: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[NewsItem, ...]: ...

    def __getitem__(self, index: int | slice) -> NewsItem | tuple[NewsItem, ...]:
        return self.items[index]

    def __len__(self) -> int:
        return len(self.items)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag.lower() in {"br", "p", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag.lower() in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def clean_html(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def normalize_date(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid publication date: {value!r}") from exc
    if parsed is None:
        raise ValueError(f"invalid publication date: {value!r}")
    return beijing_isoformat(parsed)


class _BaseCollector:
    accept = "*/*"
    response_name = "source"
    retain_images = "none"

    def __init__(
        self,
        source: SourceConfig,
        network: NetworkConfig,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        self.source = source
        self.network = network
        self._opener = opener

    def _fetch_bytes(self) -> bytes:
        target = (
            self.source.url
            if self.source.transport == "direct"
            else JINA_READER_BASE + self.source.url
        )
        headers = {
            "User-Agent": self.network.user_agent,
            "Accept": self.accept,
        }
        if self.source.transport == "jina":
            headers.update(
                {
                    "X-Respond-With": "markdown",
                    "X-Retain-Links": "all",
                    "X-Retain-Images": self.retain_images,
                    "X-With-Links-Summary": "false",
                }
            )
        request = Request(target, headers=headers, method="GET")
        try:
            with self._opener(
                request, timeout=self.network.timeout_seconds
            ) as response:
                payload = response.read(self.network.max_response_bytes + 1)
        except Exception as exc:
            raise CollectionError(
                f"failed to fetch {self.response_name}: {type(exc).__name__}"
            ) from exc
        if len(payload) > self.network.max_response_bytes:
            raise CollectionError(
                f"{self.response_name} response exceeds "
                f"{self.network.max_response_bytes} byte limit"
            )
        return payload

    def _fetch_text(self) -> str:
        payload = self._fetch_bytes()
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CollectionError(
                f"{self.response_name} response is not valid UTF-8"
            ) from exc

    def _issue(
        self,
        index: int,
        error: Exception | str,
        *,
        title: str = "",
        url: str = "",
        stage: str = "parse",
    ) -> CollectionIssue:
        message = str(error)
        LOGGER.warning(
            "skipping invalid %s item %d: %s", self.response_name, index, message
        )
        return CollectionIssue(
            source=self.source.name,
            stage=stage,
            title=title,
            url=url,
            message=message,
            index=index,
        )


class RSSCollector(_BaseCollector):
    """Collect the first configured number of items in RSS feed order."""

    accept = "application/rss+xml, application/xml;q=0.9"
    response_name = "RSS"

    def collect(self) -> CollectionBatch:
        payload = self._fetch_bytes()
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise CollectionError(f"invalid RSS XML: {exc}") from exc

        entries = [
            element for element in root.iter() if _local_name(element.tag) == "item"
        ]
        if not entries:
            raise CollectionError("invalid RSS XML: feed contains no item elements")
        items: list[NewsItem] = []
        issues: list[CollectionIssue] = []
        dedupe_keys: set[str] = set()
        for index, element in enumerate(entries[: self.source.window_size], start=1):
            try:
                item = self._map_item(element)
            except ValueError as exc:
                issues.append(
                    self._issue(
                        index,
                        exc,
                        title=clean_html(_optional_text(element, "title")),
                        url=_optional_text(element, "link").strip(),
                    )
                )
                continue
            if item.dedupe_key in dedupe_keys:
                continue
            dedupe_keys.add(item.dedupe_key)
            items.append(item)
        return CollectionBatch(tuple(items), tuple(issues))

    def _map_item(self, element: ET.Element) -> NewsItem:
        title = clean_html(_required_text(element, "title"))
        url = _allowed_item_url(
            _required_text(element, "link").strip(),
            self.source.url,
            self.source.allowed_hosts,
        )
        published = normalize_date(_required_text(element, "pubDate").strip())
        if not title:
            raise ValueError("empty title")

        guid = _optional_text(element, "guid").strip()
        item_id = guid or canonicalize_url(url)
        if not item_id:
            raise ValueError("missing stable ID")

        description = _optional_text(element, "description")
        if not description:
            description = _optional_text(element, "encoded")
        author = _optional_text(element, "creator") or _optional_text(element, "author")
        categories = [
            clean_html(child.text or "")
            for child in element
            if _local_name(child.tag) == "category" and clean_html(child.text or "")
        ]
        return NewsItem(
            source=self.source.name,
            item_id=item_id,
            title=title,
            content=clean_html(description),
            url=url,
            published_at=published,
            author=clean_html(author),
            category=", ".join(categories),
            guid=guid,
        )


@dataclass(frozen=True, slots=True)
class _MarkdownLink:
    label: str
    target: str
    start: int
    end: int


class MarkdownIndexCollector(_BaseCollector):
    """Extract article cards and heading links from a Jina Markdown index."""

    accept = "text/markdown, text/plain;q=0.9"
    response_name = "Markdown index"
    retain_images = "all"

    def collect(self) -> CollectionBatch:
        markdown = self._fetch_text()
        all_links = list(_markdown_links(markdown))
        title_hints = _index_title_hints(markdown, all_links, self.source.url)
        image_titles = _markdown_image_titles(markdown)
        links = [
            link
            for link in all_links
            if _is_index_link(markdown, link, self.source.url)
        ]
        if not links:
            raise CollectionError("invalid Markdown index: no article links found")

        reference_year = _jina_reference_year(markdown)
        inferred_year = reference_year
        previous_month: int | None = None
        items: list[NewsItem] = []
        issues: list[CollectionIssue] = []
        dedupe_keys: set[str] = set()
        considered = 0
        for link_index, link in enumerate(links):
            if considered >= self.source.window_size:
                break
            considered += 1
            raw_target = link.target
            title = ""
            fallback_title = _index_title(link.label, "")
            next_start = (
                links[link_index + 1].start
                if link_index + 1 < len(links)
                else len(markdown)
            )
            trailing = markdown[link.end : min(link.end + 100, next_start)]
            raw_for_date = _remove_markdown_images(link.label) + trailing
            try:
                published_at, month, has_year = _find_page_date(
                    raw_for_date, default_year=inferred_year
                )
                if month is not None and not has_year and inferred_year is not None:
                    if (
                        previous_month is not None
                        and previous_month <= 2
                        and month >= 10
                    ):
                        inferred_year -= 1
                        published_at, month, has_year = _find_page_date(
                            raw_for_date, default_year=inferred_year
                        )
                    previous_month = month
                elif month is not None:
                    previous_month = month
                    if has_year and published_at:
                        inferred_year = int(published_at[:4])

                hinted_url = canonicalize_url(urljoin(self.source.url, link.target))
                image_title = _matching_image_title(link.label, image_titles)
                parsed_title = _index_title(link.label, published_at)
                if re.search(r"####\s+", _remove_markdown_images(link.label)):
                    parsed_title = _title_from_url_slug(parsed_title, link.target)
                hint = title_hints.get(hinted_url, "")
                title = image_title or min(
                    (candidate for candidate in (hint, parsed_title) if candidate),
                    key=len,
                    default="",
                )
                if not title:
                    raise ValueError("empty article title")
                url = _allowed_item_url(
                    raw_target, self.source.url, self.source.allowed_hosts
                )
            except ValueError as exc:
                issues.append(
                    self._issue(
                        considered,
                        exc,
                        title=title or fallback_title,
                        url=_safe_issue_url(self.source.url, raw_target),
                    )
                )
                continue
            item = NewsItem(
                source=self.source.name,
                item_id=canonicalize_url(url),
                title=title,
                content=_index_description(link.label, title, published_at),
                url=url,
                published_at=published_at,
                author="",
                category="",
                guid="",
            )
            if item.dedupe_key in dedupe_keys:
                continue
            dedupe_keys.add(item.dedupe_key)
            items.append(item)
        return CollectionBatch(tuple(items), tuple(issues))


@dataclass(frozen=True, slots=True)
class _ChangelogSection:
    raw_date: str
    published_at: str
    heading: str
    content: str
    url: str


@dataclass(frozen=True, slots=True)
class _MalformedChangelogSection:
    title: str
    message: str


class MarkdownChangelogCollector(_BaseCollector):
    """Split a rolling Markdown page into stable date-level entries."""

    accept = "text/markdown, text/plain;q=0.9"
    response_name = "Markdown changelog"

    def collect(self) -> CollectionBatch:
        markdown = self._fetch_text()
        entries = _changelog_sections(markdown, self.source.url)
        if not entries:
            raise CollectionError("invalid Markdown changelog: no dated sections found")

        items: list[NewsItem] = []
        issues: list[CollectionIssue] = []
        for index, entry in enumerate(entries[: self.source.window_size], start=1):
            if isinstance(entry, _MalformedChangelogSection):
                issues.append(
                    self._issue(
                        index,
                        entry.message,
                        title=entry.title,
                        url=self.source.url,
                    )
                )
                continue
            section = entry
            try:
                content = section.content.strip()
                if not content:
                    raise ValueError("dated changelog section has no content")
                title = _section_title(content, self.source.name, section.published_at)
                key = changelog_dedupe_key(
                    self.source.name, section.published_at, content
                )
                url = _allowed_item_url(
                    section.url, self.source.url, self.source.allowed_hosts
                )
                items.append(
                    NewsItem(
                        source=self.source.name,
                        item_id=key,
                        title=title,
                        content=content,
                        url=url,
                        published_at=section.published_at,
                        author="",
                        category="Changelog",
                        guid="",
                        dedupe_key=key,
                    )
                )
            except ValueError as exc:
                issues.append(
                    self._issue(
                        index,
                        exc,
                        title=section.heading,
                        url=section.url,
                    )
                )
        return CollectionBatch(tuple(items), tuple(issues))


@dataclass(frozen=True, slots=True)
class _MarkdownCard:
    start: int
    attrs: str
    body: str


@dataclass(frozen=True, slots=True)
class _MalformedMarkdownCard:
    start: int
    attrs: str
    message: str


class MarkdownCardsCollector(_BaseCollector):
    """Parse official Markdown ``<Card>`` release entries."""

    accept = "text/markdown, text/plain;q=0.9"
    response_name = "Markdown cards"

    def collect(self) -> CollectionBatch:
        markdown = self._fetch_text()
        cards = _markdown_card_entries(markdown)
        if not cards:
            raise CollectionError("invalid Markdown cards: no Card elements found")
        date_headings = _dated_heading_positions(markdown)

        items: list[NewsItem] = []
        issues: list[CollectionIssue] = []
        dedupe_keys: set[str] = set()
        for index, card in enumerate(cards[: self.source.window_size], start=1):
            attrs = _html_attributes(card.attrs)
            title = clean_html(attrs.get("title", ""))
            raw_url = attrs.get("href", "")
            if isinstance(card, _MalformedMarkdownCard):
                issues.append(
                    self._issue(
                        index,
                        card.message,
                        title=title,
                        url=_safe_issue_url(self.source.url, raw_url),
                    )
                )
                continue
            try:
                if not title:
                    raise ValueError("Card is missing title")
                if not raw_url:
                    raise ValueError("Card is missing href")
                published_at = _nearest_date(date_headings, card.start)
                if not published_at:
                    raise ValueError("Card has no preceding date heading")
                url = _allowed_item_url(
                    raw_url, self.source.url, self.source.allowed_hosts
                )
                item = NewsItem(
                    source=self.source.name,
                    item_id=canonicalize_url(url),
                    title=title,
                    content=_clean_markdown_text(card.body),
                    url=url,
                    published_at=published_at,
                    author="",
                    category="Model release",
                    guid="",
                )
            except ValueError as exc:
                issues.append(
                    self._issue(
                        index,
                        exc,
                        title=title,
                        url=_safe_issue_url(self.source.url, raw_url),
                    )
                )
                continue
            if item.dedupe_key in dedupe_keys:
                continue
            dedupe_keys.add(item.dedupe_key)
            items.append(item)
        return CollectionBatch(tuple(items), tuple(issues))


class _ScriptExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[dict[str, str], str]] = []
        self._attrs: dict[str, str] | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._attrs = {name.lower(): value or "" for name, value in attrs}
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._attrs is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._attrs is not None:
            self.scripts.append((self._attrs, "".join(self._parts)))
            self._attrs = None
            self._parts = []


class NextDataIndexCollector(_BaseCollector):
    """Read Kimi's fixed ``articleList.items`` Next.js data shape."""

    accept = "text/html, application/xhtml+xml;q=0.9"
    response_name = "Next.js index"

    def collect(self) -> CollectionBatch:
        document = self._fetch_text()
        raw_items, found_container = _next_article_items(document)
        if not found_container:
            raise CollectionError(
                "invalid Next.js index: articleList.items was not found"
            )

        items: list[NewsItem] = []
        issues: list[CollectionIssue] = []
        dedupe_keys: set[str] = set()
        for index, raw in enumerate(raw_items[: self.source.window_size], start=1):
            if not isinstance(raw, dict):
                issues.append(self._issue(index, "articleList item must be an object"))
                continue
            title = ""
            raw_url = ""
            try:
                raw_title = raw.get("title")
                raw_target = raw.get("href") or raw.get("url") or raw.get("link")
                raw_date = (
                    raw.get("date") or raw.get("publishedAt") or raw.get("published_at")
                )
                if not isinstance(raw_title, str):
                    raise TypeError("articleList item title must be a string")
                if not isinstance(raw_target, str):
                    raise TypeError("articleList item href must be a string")
                if not isinstance(raw_date, str):
                    raise TypeError("articleList item date must be a string")
                title = clean_html(raw_title)
                raw_url = raw_target
                if not title:
                    raise ValueError("articleList item is missing title")
                if not raw_url:
                    raise ValueError("articleList item is missing href")
                published_at = _normalize_page_date(raw_date)
                if not published_at:
                    raise ValueError("articleList item has an invalid date")
                url = _allowed_item_url(
                    raw_url, self.source.url, self.source.allowed_hosts
                )
                item_id = str(raw.get("id") or "").strip() or canonicalize_url(url)
                item = NewsItem(
                    source=self.source.name,
                    item_id=item_id,
                    title=title,
                    content=clean_html(str(raw.get("description") or "")),
                    url=url,
                    published_at=published_at,
                    author="",
                    category="Research",
                    guid="",
                )
            except (TypeError, ValueError) as exc:
                issues.append(
                    self._issue(
                        index,
                        exc,
                        title=title,
                        url=_safe_issue_url(self.source.url, raw_url),
                    )
                )
                continue
            if item.dedupe_key in dedupe_keys:
                continue
            dedupe_keys.add(item.dedupe_key)
            items.append(item)
        return CollectionBatch(tuple(items), tuple(issues))


def changelog_dedupe_key(source: str, published_at: str, content: str) -> str:
    """Build a stable identity for one dated entry on a rolling page."""

    normalized_content = re.sub(r"\s+", " ", content).strip()
    content_signature = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
    material = f"{source}\0{published_at}\0{content_signature}".encode()
    return "changelog:" + hashlib.sha256(material).hexdigest()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _optional_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name:
            return "".join(child.itertext())
    return ""


def _required_text(element: ET.Element, name: str) -> str:
    value = _optional_text(element, name)
    if not value.strip():
        raise ValueError(f"missing {name}")
    return value


def _allowed_item_url(
    target: str, base_url: str, allowed_hosts: tuple[str, ...]
) -> str:
    value = html.unescape(target.strip()).strip("<>")
    if not value or value.startswith("#"):
        raise ValueError("link is not an article HTTP(S) URL")
    try:
        url = urljoin(base_url, value)
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("link is not a valid HTTP(S) URL") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError("link is not a valid HTTP(S) URL")
    if not any(
        host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts
    ):
        raise ValueError(f"link host is not allowed: {host}")
    return url


def _safe_issue_url(base_url: str, target: str) -> str:
    """Return diagnostic URL text without letting malformed input escape."""

    try:
        return urljoin(base_url, target)
    except ValueError:
        return target[:2000]


def _markdown_links(markdown: str) -> Iterator[_MarkdownLink]:
    """Yield inline Markdown links, including cards with a nested image."""

    index = 0
    length = len(markdown)
    while index < length:
        start = markdown.find("[", index)
        if start < 0:
            return
        if start > 0 and markdown[start - 1] == "!":
            index = start + 1
            continue
        depth = 1
        cursor = start + 1
        escaped = False
        while cursor < length and depth:
            character = markdown[cursor]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
            cursor += 1
        if depth or cursor >= length or markdown[cursor] != "(":
            index = start + 1
            continue
        label_end = cursor - 1
        destination_start = cursor + 1
        paren_depth = 1
        cursor = destination_start
        escaped = False
        while cursor < length and paren_depth:
            character = markdown[cursor]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "(":
                paren_depth += 1
            elif character == ")":
                paren_depth -= 1
            cursor += 1
        if paren_depth:
            index = start + 1
            continue
        destination = markdown[destination_start : cursor - 1].strip()
        if destination.startswith("<") and ">" in destination:
            destination = destination[1 : destination.index(">")]
        else:
            destination = re.split(r"\s+[\"']", destination, maxsplit=1)[0]
        yield _MarkdownLink(markdown[start + 1 : label_end], destination, start, cursor)
        index = cursor


def _is_index_link(markdown: str, link: _MarkdownLink, source_url: str) -> bool:
    target = html.unescape(link.target.strip())
    if not target or target.startswith(("#", "mailto:", "javascript:")):
        return False
    if (
        target.lower()
        .split("?", 1)[0]
        .endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"))
    ):
        return False
    line_start = markdown.rfind("\n", 0, link.start) + 1
    prefix = markdown[line_start : link.start]
    has_card_image = "![" in link.label
    has_heading = bool(re.search(r"(?:^|\s)#{2,6}\s*$", prefix)) or bool(
        re.search(r"#{2,6}\s+", _remove_markdown_images(link.label))
    )
    try:
        _, detected_month, _ = _find_page_date(_remove_markdown_images(link.label))
    except ValueError:
        # Keep a date-shaped card in the candidate list so the collector can
        # return a localized CollectionIssue instead of failing the source.
        has_date = True
    else:
        has_date = detected_month is not None
    if not (has_card_image or has_heading or has_date):
        return False
    try:
        urljoin(source_url, target)
    except ValueError:
        # Keep a malformed target in the ordered item stream so it becomes a
        # CollectionIssue instead of disappearing during candidate discovery.
        return True
    return _is_index_article_target(target, source_url)


def _is_index_article_target(target: str, source_url: str) -> bool:
    try:
        resolved = urlsplit(urljoin(source_url, html.unescape(target.strip())))
        source = urlsplit(source_url)
    except ValueError:
        return False
    if resolved.scheme not in {"http", "https"} or not resolved.hostname:
        return False
    if resolved.hostname.lower() != (source.hostname or "").lower():
        return True
    base_path = source.path.rstrip("/")
    return bool(base_path) and resolved.path.startswith(base_path + "/")


def _is_descendant_article_url(target: str, source_url: str) -> bool:
    try:
        resolved = urlsplit(urljoin(source_url, html.unescape(target.strip())))
        source = urlsplit(source_url)
    except ValueError:
        return False
    if resolved.scheme not in {"http", "https"} or not resolved.hostname:
        return False
    if resolved.hostname.lower() != (source.hostname or "").lower():
        return False
    base_path = source.path.rstrip("/")
    return bool(base_path) and resolved.path.startswith(base_path + "/")


def _index_title_hints(
    markdown: str, links: list[_MarkdownLink], source_url: str
) -> dict[str, str]:
    """Use repeated sidebar titles to disambiguate flattened visual cards."""

    result: dict[str, str] = {}
    for link in links:
        if not _is_descendant_article_url(link.target, source_url):
            continue
        label = _strip_markdown(_remove_markdown_images(link.label))
        if not label or re.search(r"#{2,6}\s+", link.label):
            continue
        line_start = markdown.rfind("\n", 0, link.start) + 1
        if not re.search(r"(?:^|\s)[-*+]\s*$", markdown[line_start : link.start]):
            continue
        try:
            published_at = _find_page_date(label)[0]
        except ValueError:
            # The corresponding card is validated in the ordered item loop.
            published_at = ""
        if published_at:
            label = _strip_date_text(label, published_at)
            label = re.sub(
                r"^(?:Announcements?|Product|Policy|Research|Company|Safety|"
                r"Societal\s+Impacts?)\s+",
                "",
                label,
                flags=re.IGNORECASE,
            )
        if not label:
            continue
        key = canonicalize_url(urljoin(source_url, link.target))
        previous = result.get(key)
        if previous is None or len(label) < len(previous):
            result[key] = label
    return result


def _markdown_image_titles(markdown: str) -> tuple[str, ...]:
    result: list[str] = []
    for match in re.finditer(r"!\[([^\]]+)\]", markdown):
        title = re.sub(r"^Image\s+\d+\s*:\s*", "", match.group(1)).strip()
        title = _strip_markdown(title)
        if title and title not in result:
            result.append(title)
    return tuple(result)


def _matching_image_title(label: str, image_titles: tuple[str, ...]) -> str:
    plain = _strip_date_text(_strip_markdown(_remove_markdown_images(label)), "")
    plain = re.sub(r"^(?:Featured\s+)?#{2,6}\s+", "", plain, flags=re.IGNORECASE)
    matches = [
        title for title in image_titles if plain.casefold().startswith(title.casefold())
    ]
    return max(matches, key=len, default="")


def _title_from_url_slug(title_and_description: str, target: str) -> str:
    slug = unquote(urlsplit(target).path.rstrip("/").rsplit("/", 1)[-1])
    slug_tokens = re.findall(r"[a-z0-9]+", slug.casefold())
    if not slug_tokens:
        return title_and_description
    token_index = 0
    for match in re.finditer(r"[a-z0-9]+", title_and_description.casefold()):
        if match.group() != slug_tokens[token_index]:
            continue
        token_index += 1
        if token_index == len(slug_tokens):
            return title_and_description[: match.end()].strip()
    return title_and_description


def _remove_markdown_images(value: str) -> str:
    return re.sub(r"!\[[^\]]*\]\([^\n)]*\)", " ", value)


def _strip_markdown(value: str) -> str:
    text = _remove_markdown_images(value)
    text = re.sub(r"\[([^\]]+)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"[`*_~]", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _index_title(label: str, published_at: str) -> str:
    without_images = _remove_markdown_images(label)
    image = re.search(r"!\[([^\]]+)\]", label)
    image_title = ""
    if image:
        image_title = re.sub(r"^Image\s+\d+\s*:\s*", "", image.group(1)).strip()
    if image_title and not published_at:
        return _strip_markdown(image_title)
    heading = re.search(r"#{2,6}\s+(.+)", without_images, flags=re.DOTALL)
    if heading:
        candidate = _strip_date_text(_strip_markdown(heading.group(1)), published_at)
        if candidate:
            return candidate
    if image_title:
        return _strip_markdown(image_title)
    candidate = _strip_date_text(_strip_markdown(without_images), published_at)
    if "\n" in candidate:
        candidate = candidate.splitlines()[0]
    return candidate.strip(" -·|")


def _index_description(label: str, title: str, published_at: str) -> str:
    text = _strip_date_text(
        _strip_markdown(_remove_markdown_images(label)), published_at
    )
    text = re.sub(r"^#{2,6}\s*", "", text)
    text = text.removeprefix(title)
    return text.strip(" -·|")


def _strip_date_text(value: str, published_at: str) -> str:
    text = value
    patterns = (
        r"\b\d{4}[年/-]\d{1,2}(?:[月/-]\d{1,2}日?)?\b",
        (
            r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
            r"Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|"
            r"Dec(?:ember)?)\.?\s+\d{1,2}(?:,?\s+\d{4})?\b"
        ),
    )
    for pattern in patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    if published_at:
        text = text.replace(published_at, " ")
    return re.sub(r"\s+", " ", text).strip()


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?"
)


def _find_page_date(
    value: str, *, default_year: int | None = None, default_month: int | None = None
) -> tuple[str, int | None, bool]:
    text = _strip_markdown(value)
    numeric = re.search(r"(?<!\d)(20\d{2})[年/-](\d{1,2})(?:[月/-](\d{1,2})日?)?", text)
    if numeric:
        year = int(numeric.group(1))
        month = int(numeric.group(2))
        day = int(numeric.group(3)) if numeric.group(3) else None
        return _validated_date(year, month, day), month, True
    english_day = re.search(
        rf"\b({_MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:,?\s+(20\d{{2}}))?\b",
        text,
        flags=re.IGNORECASE,
    )
    if english_day:
        month = _MONTHS[english_day.group(1).lower().rstrip(".")]
        year_text = english_day.group(3)
        year = int(year_text) if year_text else default_year
        if year is None:
            return "", month, False
        return (
            _validated_date(year, month, int(english_day.group(2))),
            month,
            bool(year_text),
        )
    english_month = re.search(
        rf"\b({_MONTH_PATTERN})\.?,?\s+(20\d{{2}})\b",
        text,
        flags=re.IGNORECASE,
    )
    if english_month:
        month = _MONTHS[english_month.group(1).lower().rstrip(".")]
        return f"{int(english_month.group(2)):04d}-{month:02d}", month, True
    return "", None, False


def _normalize_page_date(
    value: str, *, default_year: int | None = None, default_month: int | None = None
) -> str:
    return _find_page_date(
        value, default_year=default_year, default_month=default_month
    )[0]


def _validated_date(year: int, month: int, day: int | None) -> str:
    try:
        if day is None:
            date(year, month, 1)
            return f"{year:04d}-{month:02d}"
        value = date(year, month, day)
    except ValueError as exc:
        raise ValueError("invalid page publication date") from exc
    return value.isoformat()


def _jina_reference_year(markdown: str) -> int | None:
    match = re.search(r"(?m)^Published Time:\s*(.+)$", markdown)
    if not match:
        return None
    try:
        parsed = parsedate_to_datetime(match.group(1).strip())
    except TypeError, ValueError:
        return None
    return parsed.year if parsed is not None else None


def _changelog_sections(
    markdown: str, source_url: str
) -> list[_ChangelogSection | _MalformedChangelogSection]:
    heading_pattern = re.compile(r"(?m)^(#{2,4})[ \t]+(.+?)[ \t]*$")
    context_year: int | None = None
    context_month: int | None = None
    current: tuple[str, str, str, int, str] | None = None
    entries: list[_ChangelogSection | _MalformedChangelogSection] = []

    def finish(end: int, *, discard_month: bool = False) -> None:
        nonlocal current
        if current is None:
            return
        raw_date, published_at, heading, start, url = current
        content = markdown[start:end].strip(" \n*-")
        if len(published_at) == 7 and (discard_month or not content):
            current = None
            return
        entries.append(
            _ChangelogSection(
                raw_date,
                published_at,
                heading,
                content,
                url,
            )
        )
        current = None

    for match in heading_pattern.finditer(markdown):
        raw_heading = match.group(2).strip()
        heading_text = _strip_markdown(raw_heading).replace("\u200b", "").strip()
        try:
            published_at, month, _ = _find_page_date(
                heading_text, default_year=context_year, default_month=context_month
            )
        except ValueError:
            finish(match.start(), discard_month=True)
            entries.append(
                _MalformedChangelogSection(
                    heading_text,
                    f"invalid changelog date heading: {heading_text}",
                )
            )
            continue
        if not published_at:
            if re.match(r"(?i)^date\s*:", heading_text):
                finish(match.start(), discard_month=True)
                entries.append(
                    _MalformedChangelogSection(
                        heading_text,
                        f"invalid changelog date heading: {heading_text}",
                    )
                )
            continue
        if len(published_at) == 7:
            finish(match.start())
            context_year = int(published_at[:4])
            context_month = int(published_at[5:7])
            current = (
                heading_text,
                published_at,
                heading_text,
                match.end(),
                _heading_url(raw_heading, source_url, published_at),
            )
            continue
        finish(match.start(), discard_month=True)
        context_year = int(published_at[:4])
        context_month = month
        heading_url = _heading_url(raw_heading, source_url, published_at)
        current = (
            heading_text,
            published_at,
            heading_text,
            match.end(),
            heading_url,
        )
    finish(len(markdown))
    return entries


def _heading_url(raw_heading: str, source_url: str, published_at: str) -> str:
    for link in _markdown_links(raw_heading):
        if link.target.startswith("#"):
            return urldefrag(source_url).url + link.target
        try:
            resolved = urljoin(source_url, link.target)
        except ValueError:
            continue
        if canonicalize_url(resolved) == canonicalize_url(source_url):
            return resolved
    slug = re.sub(r"[^a-z0-9]+", "-", published_at.lower()).strip("-")
    return urldefrag(source_url).url + f"#{slug}"


def _section_title(content: str, source: str, published_at: str) -> str:
    heading = re.search(r"(?m)^#{2,6}\s+(.+)$", content)
    if heading:
        title = _strip_markdown(heading.group(1)).replace("\u200b", "").strip()
        if title and not _normalize_page_date(title):
            return title
    return f"{source} · {published_at}"


def _dated_heading_positions(markdown: str) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    context_year: int | None = None
    context_month: int | None = None
    for match in re.finditer(r"(?m)^#{2,6}\s+(.+?)\s*$", markdown):
        try:
            published_at, month, _ = _find_page_date(
                match.group(1), default_year=context_year, default_month=context_month
            )
        except ValueError:
            # An empty marker prevents following cards from inheriting the
            # previous valid heading; each affected card becomes its own issue.
            result.append((match.start(), ""))
            continue
        if not published_at:
            continue
        context_year = int(published_at[:4])
        context_month = month
        result.append((match.start(), published_at))
    return result


def _nearest_date(headings: list[tuple[int, str]], position: int) -> str:
    result = ""
    for heading_position, published_at in headings:
        if heading_position >= position:
            break
        result = published_at
    return result


def _markdown_card_entries(
    markdown: str,
) -> list[_MarkdownCard | _MalformedMarkdownCard]:
    """Scan Card blocks while retaining malformed entries in page order."""

    marker_pattern = re.compile(r"<Card\b", flags=re.IGNORECASE)
    opening_pattern = re.compile(r"<Card\b(?P<attrs>[^>\r\n]*)>", flags=re.IGNORECASE)
    closing_pattern = re.compile(r"</Card\s*>", flags=re.IGNORECASE)
    entries: list[_MarkdownCard | _MalformedMarkdownCard] = []
    cursor = 0
    while marker := marker_pattern.search(markdown, cursor):
        opening = opening_pattern.match(markdown, marker.start())
        if opening is None:
            line_end = markdown.find("\n", marker.end())
            if line_end < 0:
                line_end = len(markdown)
            entries.append(
                _MalformedMarkdownCard(
                    marker.start(),
                    markdown[marker.end() : line_end],
                    "Card has a malformed opening tag",
                )
            )
            cursor = marker.end()
            continue

        closing = closing_pattern.search(markdown, opening.end())
        next_opening = marker_pattern.search(markdown, opening.end())
        if closing is None or (
            next_opening is not None and next_opening.start() < closing.start()
        ):
            entries.append(
                _MalformedMarkdownCard(
                    marker.start(),
                    opening.group("attrs"),
                    "Card is missing a closing tag",
                )
            )
            cursor = next_opening.start() if next_opening is not None else len(markdown)
            continue

        entries.append(
            _MarkdownCard(
                marker.start(),
                opening.group("attrs"),
                markdown[opening.end() : closing.start()],
            )
        )
        cursor = closing.end()
    return entries


def _html_attributes(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(
        r"([A-Za-z_:][\w:.-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", value
    ):
        result[match.group(1).lower()] = html.unescape(
            match.group(2) if match.group(2) is not None else match.group(3)
        )
    return result


def _clean_markdown_text(value: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^\n)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"[`*_~]", "", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _next_article_items(document: str) -> tuple[list[Any], bool]:
    parser = _ScriptExtractor()
    parser.feed(document)
    parser.close()
    values: list[Any] = []
    flight_strings: list[str] = []
    for attrs, script in parser.scripts:
        if attrs.get("id") == "__NEXT_DATA__":
            try:
                values.append(json.loads(script))
            except json.JSONDecodeError:
                continue
        for argument in _javascript_call_arguments(script, "self.__next_f.push"):
            try:
                decoded = json.loads(argument)
            except json.JSONDecodeError:
                continue
            values.append(decoded)
            flight_strings.extend(_all_strings(decoded))

    items: list[Any] = []
    found = False
    for value in values:
        discovered, containers = _article_lists_in_value(value)
        found = found or discovered
        items.extend(containers)
    # Flight data wraps the server payload in strings prefixed with row IDs.  A
    # single list may also span adjacent push calls, so inspect both forms.
    for payload in [*flight_strings, "".join(flight_strings)]:
        discovered, containers = _article_lists_in_text(payload)
        found = found or discovered
        items.extend(containers)

    unique: list[Any] = []
    seen: set[str] = set()
    for entry in items:
        marker = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        if marker not in seen:
            seen.add(marker)
            unique.append(entry)
    return unique, found


def _article_lists_in_value(value: Any) -> tuple[bool, list[Any]]:
    found = False
    result: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "articleList":
                child_found, child_items = _article_list_container(child)
                found = found or child_found
                result.extend(child_items)
            nested_found, nested_items = _article_lists_in_value(child)
            found = found or nested_found
            result.extend(nested_items)
    elif isinstance(value, list):
        for child in value:
            nested_found, nested_items = _article_lists_in_value(child)
            found = found or nested_found
            result.extend(nested_items)
    return found, result


def _article_list_container(value: Any) -> tuple[bool, list[Any]]:
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        return True, list(value["items"])
    if isinstance(value, list):
        return True, list(value)
    return False, []


def _article_lists_in_text(value: str) -> tuple[bool, list[Any]]:
    found = False
    result: list[Any] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r'["\']articleList["\']\s*:', value):
        start = match.end()
        while start < len(value) and value[start].isspace():
            start += 1
        try:
            decoded, _ = decoder.raw_decode(value, start)
        except json.JSONDecodeError:
            continue
        child_found, child_items = _article_list_container(decoded)
        found = found or child_found
        result.extend(child_items)
    return found, result


def _javascript_call_arguments(script: str, call_name: str) -> Iterator[str]:
    marker = call_name + "("
    start = 0
    while True:
        call = script.find(marker, start)
        if call < 0:
            return
        cursor = call + len(marker)
        argument_start = cursor
        depth = 1
        quote = ""
        escaped = False
        while cursor < len(script) and depth:
            character = script[cursor]
            if quote:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = ""
            elif character in {'"', "'"}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            cursor += 1
        if not depth:
            yield script[argument_start : cursor - 1]
            start = cursor
        else:
            return


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for child in value for text in _all_strings(child)]
    if isinstance(value, dict):
        return [text for child in value.values() for text in _all_strings(child)]
    return []


CollectorType = type[
    RSSCollector
    | MarkdownIndexCollector
    | MarkdownChangelogCollector
    | MarkdownCardsCollector
    | NextDataIndexCollector
]

COLLECTOR_REGISTRY: dict[str, CollectorType] = {
    "rss": RSSCollector,
    "markdown_index": MarkdownIndexCollector,
    "markdown_changelog": MarkdownChangelogCollector,
    "markdown_cards": MarkdownCardsCollector,
    "next_data_index": NextDataIndexCollector,
}


def create_collector(
    source: SourceConfig,
    network: NetworkConfig,
    opener: Callable[..., object] = urlopen,
) -> (
    RSSCollector
    | MarkdownIndexCollector
    | MarkdownChangelogCollector
    | MarkdownCardsCollector
    | NextDataIndexCollector
):
    """Instantiate the deterministic collector selected by ``source``."""

    try:
        collector_type = COLLECTOR_REGISTRY[source.collector]
    except KeyError as exc:  # Defensive for programmatically forged configs.
        raise CollectionError(f"unsupported collector: {source.collector}") from exc
    return collector_type(source, network, opener)


def collect_source(
    source: SourceConfig,
    network: NetworkConfig,
    opener: Callable[..., object] = urlopen,
) -> CollectionBatch:
    """Collect one configured source through the public registry seam."""

    return create_collector(source, network, opener).collect()
