"""Feishu custom-bot rich-text notification support."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.request import Request, urlopen

from .datetime_utils import format_beijing_timestamp
from .model import NewsItem


class NotificationError(RuntimeError):
    """Raised when Feishu does not accept a notification."""


@dataclass(frozen=True, slots=True)
class Digest:
    payload: dict[str, object]
    items: tuple[NewsItem, ...]
    encoded: bytes


def build_digests(
    items: Sequence[NewsItem],
    *,
    title: str,
    max_payload_bytes: int,
) -> tuple[Digest, ...]:
    if not items:
        raise ValueError("cannot build empty digests")

    rendered = [(item, _item_paragraphs(item)) for item in items]
    for item, item_paragraphs in rendered:
        encoded = encode_payload(_payload(title, item_paragraphs))
        if len(encoded) > max_payload_bytes:
            raise NotificationError(
                f"item {item.item_id!r} cannot fit within "
                f"{max_payload_bytes} byte payload limit without truncation"
            )

    digests: list[Digest] = []
    paragraphs: list[list[dict[str, object]]] = []
    included: list[NewsItem] = []
    for item, item_paragraphs in rendered:
        candidate_paragraphs = paragraphs + item_paragraphs
        candidate_payload = _payload(title, candidate_paragraphs)
        candidate_encoded = encode_payload(candidate_payload)
        if len(candidate_encoded) > max_payload_bytes:
            digests.append(_digest(title, paragraphs, included))
            paragraphs = item_paragraphs
            included = [item]
            continue
        paragraphs = candidate_paragraphs
        included.append(item)

    digests.append(_digest(title, paragraphs, included))
    return tuple(digests)


def encode_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest(
    title: str,
    paragraphs: list[list[dict[str, object]]],
    items: list[NewsItem],
) -> Digest:
    payload = _payload(title, paragraphs)
    return Digest(payload=payload, items=tuple(items), encoded=encode_payload(payload))


def _payload(
    title: str, paragraphs: list[list[dict[str, object]]]
) -> dict[str, object]:
    return {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {"title": title, "content": paragraphs}}},
    }


def _item_paragraphs(item: NewsItem) -> list[list[dict[str, object]]]:
    summary = item.content or "（无摘要）"
    published_at = format_beijing_timestamp(item.published_at)
    return [
        [{"tag": "a", "text": f"{item.title}\n", "href": item.url}],
        [
            {
                "tag": "text",
                "text": (
                    f"来源：{item.source} · 北京时间：{published_at}\n{summary}\n"
                ),
            }
        ],
    ]


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
            raise NotificationError(
                f"Feishu webhook request failed: {type(exc).__name__}"
            ) from exc

        if len(response_body) > 65_536:
            raise NotificationError("Feishu webhook returned an oversized response")
        try:
            result = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotificationError(
                "Feishu webhook returned a non-JSON response"
            ) from exc
        if not isinstance(result, dict) or result.get("code") != 0:
            code = result.get("code") if isinstance(result, dict) else None
            raise NotificationError(f"Feishu rejected the message (code={code!r})")
