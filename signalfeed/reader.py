"""Restricted Jina Reader client for configured official article hosts."""

import re
from collections.abc import Callable, Collection
from contextlib import suppress
from urllib.error import HTTPError
from urllib.parse import urldefrag, urlsplit
from urllib.request import Request, urlopen

JINA_READER_BASE = "https://r.jina.ai/"
READER_TIMEOUT_SECONDS = 45.0
MAX_READER_BYTES = 1024 * 1024
MAX_READER_TOKENS = 6000
DEFAULT_ALLOWED_HOSTS = ("openai.com",)


class ReaderError(RuntimeError):
    """Raised when an article cannot be safely extracted."""


class JinaReader:
    def __init__(
        self,
        user_agent: str,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        self.user_agent = user_agent
        self._opener = opener

    def read(
        self,
        article_url: str,
        allowed_hosts: Collection[str] | None = None,
        *,
        retain_links: bool = False,
    ) -> str:
        """Read an HTTPS page after checking it against a source allowlist.

        Omitting ``allowed_hosts`` preserves the original OpenAI-only behavior.
        Article content drops images and link targets by default. Index callers
        can retain Markdown links while still dropping images.
        """

        hosts = DEFAULT_ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts
        safe_url = _allowed_article_url(article_url, hosts)
        request = Request(
            JINA_READER_BASE + safe_url,
            headers={
                "Accept": "text/markdown",
                "User-Agent": self.user_agent,
                "X-Respond-With": "markdown",
                "X-Retain-Links": "all" if retain_links else "text",
                "X-Retain-Images": "none",
                "X-With-Links-Summary": "false",
                "X-Token-Budget": str(MAX_READER_TOKENS),
                "X-Max-Tokens": str(MAX_READER_TOKENS),
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=READER_TIMEOUT_SECONDS) as response:
                payload = response.read(MAX_READER_BYTES + 1)
        except Exception as exc:
            if isinstance(exc, HTTPError):
                with suppress(OSError):
                    exc.close()
            raise ReaderError(
                f"Jina Reader request failed: {type(exc).__name__}"
            ) from exc
        if len(payload) > MAX_READER_BYTES:
            raise ReaderError("Jina Reader response exceeds 1048576 byte limit")
        try:
            markdown = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReaderError("Jina Reader returned non-UTF-8 content") from exc
        content = _clean_markdown(markdown, retain_links=retain_links)
        if not content:
            raise ReaderError("Jina Reader returned empty article content")
        return content


def _allowed_article_url(
    value: str, allowed_hosts: Collection[str] = DEFAULT_ALLOWED_HOSTS
) -> str:
    hosts = _normalize_allowed_hosts(allowed_hosts)
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ReaderError("article URL is invalid") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not any(host == allowed or host.endswith(f".{allowed}") for allowed in hosts)
    ):
        raise ReaderError("article URL must be HTTPS on an allowed host")
    return urldefrag(value).url


def _normalize_allowed_hosts(allowed_hosts: Collection[str]) -> tuple[str, ...]:
    if isinstance(allowed_hosts, str) or not allowed_hosts:
        raise ReaderError("allowed_hosts must be a non-empty collection of hostnames")
    hosts: list[str] = []
    for value in allowed_hosts:
        if not isinstance(value, str):
            raise ReaderError("allowed_hosts must contain only hostnames")
        host = value.strip().lower()
        try:
            parsed = urlsplit(f"//{host}")
            port = parsed.port
        except ValueError as exc:
            raise ReaderError("allowed_hosts contains an invalid hostname") from exc
        if (
            not host
            or parsed.hostname != host
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or any(character in host for character in "/?#")
            or not _is_hostname(host)
        ):
            raise ReaderError("allowed_hosts contains an invalid hostname")
        hosts.append(host)
    return tuple(hosts)


def _is_hostname(value: str) -> bool:
    if len(value) > 253:
        return False
    labels = value.split(".")
    return all(
        label
        and len(label) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    )


def _clean_markdown(markdown: str, *, retain_links: bool) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^\n)]*\)", "", markdown)
    text = re.sub(r"!\[[^\]]*\]\[[^\]]*\]", "", text)
    text = re.sub(r"(?is)<img\b[^>]*>", "", text)
    if not retain_links:
        text = _remove_links(text)
    text = re.sub(r"(?m)^[ \t]*URL Source:[ \t]*.*$", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _remove_links(markdown: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^\n)]*\)", r"\1", markdown)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"(?m)^[ \t]*\[[^\]]+\]:[ \t]*\S+.*$", "", text)
    text = re.sub(r"<https?://[^>]+>", "", text)
    text = re.sub(r"https?://\S+", "", text)
    return text


def _remove_images_and_links(markdown: str) -> str:
    """Backward-compatible helper for article Markdown cleanup."""

    return _clean_markdown(markdown, retain_links=False)
