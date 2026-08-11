"""RSS collection using only the Python standard library."""

from collections.abc import Callable
from datetime import UTC
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import logging
import re
from urllib.parse import urldefrag
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .config import NetworkConfig, SourceConfig
from .model import NewsItem


LOGGER = logging.getLogger(__name__)


class CollectionError(RuntimeError):
    """Raised when the feed as a whole cannot be collected."""


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
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class RSSCollector:
    """Collect the first configured number of items in RSS feed order."""

    def __init__(
        self,
        source: SourceConfig,
        network: NetworkConfig,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        self.source = source
        self.network = network
        self._opener = opener

    def collect(self) -> list[NewsItem]:
        request = Request(
            self.source.url,
            headers={"User-Agent": self.network.user_agent, "Accept": "application/rss+xml, application/xml;q=0.9"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.network.timeout_seconds) as response:
                payload = response.read(self.network.max_response_bytes + 1)
        except Exception as exc:
            raise CollectionError(f"failed to fetch RSS feed: {exc}") from exc

        if len(payload) > self.network.max_response_bytes:
            raise CollectionError(
                f"RSS response exceeds {self.network.max_response_bytes} byte limit"
            )
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise CollectionError(f"invalid RSS XML: {exc}") from exc

        entries = [element for element in root.iter() if _local_name(element.tag) == "item"]
        items: list[NewsItem] = []
        for index, element in enumerate(entries[: self.source.window_size], start=1):
            try:
                items.append(self._map_item(element))
            except ValueError as exc:
                LOGGER.warning("skipping invalid RSS item %d: %s", index, exc)
        return items

    def _map_item(self, element: ET.Element) -> NewsItem:
        title = clean_html(_required_text(element, "title"))
        url = _required_text(element, "link").strip()
        published = normalize_date(_required_text(element, "pubDate").strip())
        if not title:
            raise ValueError("empty title")
        if not url.startswith(("https://", "http://")):
            raise ValueError("link is not an HTTP(S) URL")

        guid = _optional_text(element, "guid").strip()
        item_id = guid or urldefrag(url).url
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
