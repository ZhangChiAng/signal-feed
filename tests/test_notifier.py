import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from signalfeed.config import FeishuDeliveryConfig
from signalfeed.notifier import (
    FeishuNotifier,
    NotificationError,
    _build_client,
    build_digests,
)
from tests.helpers import news_item

DELIVERY = FeishuDeliveryConfig(
    app_id="cli_test",
    app_secret="super-secret",
    receive_id_type="chat_id",
    receive_id="oc_private",
)


class NotifierTests(unittest.TestCase):
    def test_builds_linked_complete_post_with_beijing_time(self) -> None:
        long_title = "中文长标题" * 70
        long_summary = "完整摘要。" * 100
        first = news_item(title=long_title, content=long_summary)
        second = news_item(item_id="2", guid="2", url="https://example.com/2")
        (digest,) = build_digests(
            [first, second],
            title="2026-08-12",
            max_payload_bytes=18 * 1024,
        )
        self.assertEqual(digest.payload["msg_type"], "post")
        self.assertLessEqual(len(digest.encoded), 18 * 1024)
        self.assertEqual(digest.items, (first, second))
        post = digest.payload["content"]["zh_cn"]  # type: ignore[index]
        self.assertEqual(post["title"], "2026-08-12")
        title_node = post["content"][0][0]
        self.assertEqual(
            title_node,
            {
                "tag": "a",
                "text": f"{long_title}\n",
                "href": "https://example.com/news/1",
            },
        )
        body = digest.encoded.decode("utf-8")
        self.assertIn(long_title, body)
        self.assertIn(long_summary, body)
        self.assertNotIn("…", body)
        self.assertIn("OpenAI News", body)
        self.assertIn("北京时间：2026-08-10 12:00:00", body)
        self.assertIn("https://example.com/news/1", body)
        self.assertNotIn("查看原文", body)

    def test_greedily_builds_all_bounded_batches_in_original_order(self) -> None:
        items = [
            news_item(item_id=str(i), guid=str(i), url=f"https://example.com/{i}")
            for i in range(3)
        ]
        single_sizes = [
            len(
                build_digests([item], title="2026-08-12", max_payload_bytes=5000)[
                    0
                ].encoded
            )
            for item in items
        ]
        limit = max(single_sizes)
        digests = build_digests(
            items,
            title="2026-08-12",
            max_payload_bytes=limit,
        )
        self.assertGreater(len(digests), 1)
        self.assertEqual(
            [item.item_id for digest in digests for item in digest.items],
            ["0", "1", "2"],
        )
        self.assertTrue(all(len(digest.encoded) <= limit for digest in digests))
        self.assertTrue(
            all(
                digest.payload["content"]["zh_cn"]["title"]  # type: ignore[index]
                == "2026-08-12"
                for digest in digests
            )
        )

    def test_default_28_kib_limit_splits_complete_items(self) -> None:
        items = [
            news_item(
                item_id=str(index),
                guid=str(index),
                url=f"https://example.com/{index}",
                content="完整中文摘要。" * 800,
            )
            for index in range(3)
        ]
        digests = build_digests(
            items,
            title="2026-08-12",
            max_payload_bytes=28 * 1024,
        )
        self.assertGreater(len(digests), 1)
        self.assertTrue(all(len(digest.encoded) <= 28 * 1024 for digest in digests))
        self.assertEqual(
            [item.item_id for digest in digests for item in digest.items],
            ["0", "1", "2"],
        )

    def test_rejects_any_individually_oversized_item_during_preflight(self) -> None:
        first = news_item()
        oversized = news_item(
            item_id="large",
            guid="large",
            url="https://example.com/large",
            content="完整内容" * 1000,
        )
        with self.assertRaisesRegex(NotificationError, "large.*without truncation"):
            build_digests(
                [first, oversized],
                title="2026-08-12",
                max_payload_bytes=1000,
            )

    def test_openapi_request_contains_target_post_and_string_content(self) -> None:
        (digest,) = build_digests(
            [news_item()],
            title="2026-08-12",
            max_payload_bytes=5000,
        )
        seen: dict[str, object] = {}

        class MessageService:
            def create(self, request: object) -> object:
                seen["request"] = request
                return SimpleNamespace(code=0, success=lambda: True)

        client = SimpleNamespace(
            im=SimpleNamespace(v1=SimpleNamespace(message=MessageService()))
        )

        def client_factory(delivery: FeishuDeliveryConfig, timeout: float) -> object:
            seen["delivery"] = delivery
            seen["timeout"] = timeout
            return client

        FeishuNotifier(DELIVERY, 7.0, client_factory).send(digest)
        request = seen["request"]
        self.assertEqual(request.receive_id_type, "chat_id")  # type: ignore[union-attr]
        body = request.request_body  # type: ignore[union-attr]
        self.assertEqual(body.receive_id, "oc_private")
        self.assertEqual(body.msg_type, "post")
        self.assertIsInstance(body.content, str)
        self.assertEqual(json.loads(body.content), digest.payload["content"])
        self.assertEqual(seen["delivery"], DELIVERY)
        self.assertEqual(seen["timeout"], 7.0)

    def test_sdk_client_uses_app_credentials_and_network_timeout(self) -> None:
        calls: list[tuple[str, object]] = []
        built_client = object()

        class Builder:
            def app_id(self, value: str) -> Builder:
                calls.append(("app_id", value))
                return self

            def app_secret(self, value: str) -> Builder:
                calls.append(("app_secret", value))
                return self

            def timeout(self, value: float) -> Builder:
                calls.append(("timeout", value))
                return self

            def build(self) -> object:
                return built_client

        class Client:
            @staticmethod
            def builder() -> Builder:
                return Builder()

        with patch("signalfeed.notifier.lark.Client", Client):
            result = _build_client(DELIVERY, 9.5)

        self.assertIs(result, built_client)
        self.assertEqual(
            calls,
            [
                ("app_id", "cli_test"),
                ("app_secret", "super-secret"),
                ("timeout", 9.5),
            ],
        )

    def test_rejects_business_error_and_sdk_exception_without_secrets(self) -> None:
        (digest,) = build_digests(
            [news_item()],
            title="2026-08-12",
            max_payload_bytes=5000,
        )

        class MessageService:
            def __init__(self, response: object = None, error: Exception | None = None):
                self.response = response
                self.error = error

            def create(self, request: object) -> object:
                if self.error is not None:
                    raise self.error
                return self.response

        cases = (
            MessageService(SimpleNamespace(code=19001, success=lambda: False)),
            MessageService(error=RuntimeError("super-secret oc_private")),
        )
        for service in cases:
            client = SimpleNamespace(
                im=SimpleNamespace(v1=SimpleNamespace(message=service))
            )
            with (
                self.subTest(service=service),
                self.assertRaises(NotificationError) as raised,
            ):
                FeishuNotifier(
                    DELIVERY,
                    5,
                    lambda delivery, timeout, current_client=client: current_client,
                ).send(digest)
            self.assertNotIn("super-secret", str(raised.exception))
            self.assertNotIn("oc_private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
