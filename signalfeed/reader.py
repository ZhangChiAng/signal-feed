"""Restricted Jina Reader client for OpenAI articles."""

import re
from collections.abc import Callable
from contextlib import suppress
from urllib.error import HTTPError
from urllib.parse import urldefrag, urlsplit
from urllib.request import Request, urlopen

JINA_READER_BASE = "https://r.jina.ai/"
READER_TIMEOUT_SECONDS = 45.0
MAX_READER_BYTES = 1024 * 1024
MAX_READER_TOKENS = 6000


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

    def read(self, article_url: str) -> str:
        safe_url = _allowed_article_url(article_url)
        request = Request(
            JINA_READER_BASE + safe_url,
            headers={
                "Accept": "text/markdown",
                "User-Agent": self.user_agent,
                "X-Respond-With": "markdown",
                "X-Retain-Links": "text",
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
        content = _remove_images_and_links(markdown)
        if not content:
            raise ReaderError("Jina Reader returned empty article content")
        return content


def _allowed_article_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ReaderError("article URL is invalid") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or (host != "openai.com" and not host.endswith(".openai.com"))
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ReaderError("article URL must be HTTPS on openai.com")
    return urldefrag(value).url


def _remove_images_and_links(markdown: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^\n)]*\)", "", markdown)
    text = re.sub(r"\[([^\]]+)\]\([^\n)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"(?m)^[ \t]*\[[^\]]+\]:[ \t]*\S+.*$", "", text)
    text = re.sub(r"<https?://[^>]+>", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"(?m)^[ \t]*URL Source:[ \t]*.*$", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
