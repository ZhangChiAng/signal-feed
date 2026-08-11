"""Feishu custom-bot rich-text notification support."""

from dataclasses import dataclass
import json
from collections.abc import Callable, Sequence
from urllib.request import Request, urlopen

from .model import NewsItem


class NotificationError(RuntimeError):
    """Raised when Feishu does not accept a notification."""


@dataclass(frozen=True, slots=True)
class Digest:
    payload: dict[str, object]
    items: tuple[NewsItem, ...]
    encoded: bytes


def build_digest(
    items: Sequence[NewsItem],
    *,
    title: str,
    max_payload_bytes: int,
    summary_max_chars: int,
) -> Digest:
    if not items:
        raise ValueError("cannot build an empty digest")

    paragraphs: list[list[dict[str, str]]] = []
    included: list[NewsItem] = []
    encoded = b""
    payload: dict[str, object] = {}
    for item in items:
        candidate_paragraphs = paragraphs + _item_paragraphs(item, summary_max_chars)
        candidate_payload = _payload(title, candidate_paragraphs)
        candidate_encoded = encode_payload(candidate_payload)
        if len(candidate_encoded) > max_payload_bytes:
            break
        paragraphs = candidate_paragraphs
        payload = candidate_payload
        encoded = candidate_encoded
        included.append(item)

    if not included:
        raise NotificationError(
            f"newest item cannot fit within {max_payload_bytes} byte payload limit"
        )
    return Digest(payload=payload, items=tuple(included), encoded=encoded)


def encode_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _payload(title: str, paragraphs: list[list[dict[str, str]]]) -> dict[str, object]:
    return {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {"title": title, "content": paragraphs}}},
    }


def _item_paragraphs(item: NewsItem, summary_max_chars: int) -> list[list[dict[str, str]]]:
    summary = _truncate(item.content or "（无摘要）", summary_max_chars)
    title = _truncate(item.title, 240)
    return [
        [{"tag": "text", "text": f"{title}\n"}],
        [
            {
                "tag": "text",
                "text": f"来源：{item.source} · UTC：{item.published_at}\n{summary}\n",
            },
            {"tag": "a", "text": "查看原文", "href": item.url},
        ],
    ]


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars == 1:
        return "…"
    return value[: max_chars - 1].rstrip() + "…"


class FeishuNotifier:
    def __init__(
        self,
        webhook_url: str,
        timeout_seconds: float,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds
        self._opener = opener

    def send(self, digest: Digest) -> None:
        request = Request(
            self.webhook_url,
            data=digest.encoded,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                response_body = response.read(65_537)
        except Exception as exc:
            raise NotificationError(f"Feishu webhook request failed: {type(exc).__name__}") from exc

        if len(response_body) > 65_536:
            raise NotificationError("Feishu webhook returned an oversized response")
        try:
            result = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotificationError("Feishu webhook returned a non-JSON response") from exc
        if not isinstance(result, dict) or result.get("code") != 0:
            code = result.get("code") if isinstance(result, dict) else None
            raise NotificationError(f"Feishu rejected the message (code={code!r})")
