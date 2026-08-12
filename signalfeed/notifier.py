"""Feishu application-bot rich-text notification support."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from .config import FeishuDeliveryConfig
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
        "content": {"zh_cn": {"title": title, "content": paragraphs}},
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
        delivery: FeishuDeliveryConfig,
        timeout_seconds: float,
        client_factory: Callable[[FeishuDeliveryConfig, float], object] | None = None,
    ) -> None:
        self.delivery = delivery
        factory = _build_client if client_factory is None else client_factory
        try:
            self._client = factory(delivery, timeout_seconds)
        except Exception as exc:
            raise NotificationError(
                f"Feishu OpenAPI client initialization failed: {type(exc).__name__}"
            ) from exc

    def send(self, digest: Digest) -> None:
        try:
            content = json.dumps(
                digest.payload["content"], ensure_ascii=False, separators=(",", ":")
            )
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(self.delivery.receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(self.delivery.receive_id)
                    .msg_type("post")
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)  # type: ignore[attr-defined]
            success = response.success()
        except Exception as exc:
            raise NotificationError(
                f"Feishu OpenAPI request failed: {type(exc).__name__}"
            ) from exc

        if not success:
            code = getattr(response, "code", None)
            if not isinstance(code, int):
                code = None
            raise NotificationError(f"Feishu rejected the message (code={code!r})")


def _build_client(delivery: FeishuDeliveryConfig, timeout_seconds: float) -> object:
    return (
        lark.Client.builder()
        .app_id(delivery.app_id)
        .app_secret(delivery.app_secret)
        .timeout(timeout_seconds)
        .build()
    )
