import json
import unittest
from urllib.error import HTTPError

from signalfeed.notifier import (
    FeishuNotifier,
    NotificationError,
    build_digests,
)
from tests.helpers import FakeResponse, news_item


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
        post = digest.payload["content"]["post"]["zh_cn"]  # type: ignore[index]
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
                digest.payload["content"]["post"]["zh_cn"]["title"]  # type: ignore[index]
                == "2026-08-12"
                for digest in digests
            )
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

    def test_post_method_headers_json_and_code_zero(self) -> None:
        (digest,) = build_digests(
            [news_item()],
            title="2026-08-12",
            max_payload_bytes=5000,
        )
        seen: dict[str, object] = {}

        def opener(request: object, *, timeout: float) -> FakeResponse:
            seen["request"] = request
            seen["timeout"] = timeout
            return FakeResponse(b'{"code":0,"msg":"success"}')

        FeishuNotifier("https://example.com/hook/secret", 7.0, opener).send(digest)
        request = seen["request"]
        self.assertEqual(request.get_method(), "POST")  # type: ignore[union-attr]
        self.assertEqual(
            request.get_header("Content-type"), "application/json; charset=utf-8"
        )  # type: ignore[union-attr]
        self.assertEqual(json.loads(request.data), digest.payload)  # type: ignore[union-attr]
        self.assertEqual(seen["timeout"], 7.0)

    def test_rejects_business_error_http_error_and_non_json(self) -> None:
        (digest,) = build_digests(
            [news_item()],
            title="2026-08-12",
            max_payload_bytes=5000,
        )
        cases = [
            lambda request, timeout: FakeResponse(b'{"code":19001,"msg":"bad"}'),
            lambda request, timeout: FakeResponse(b"not json"),
            lambda request, timeout: (_ for _ in ()).throw(
                HTTPError("https://redacted.invalid", 500, "error", {}, None)
            ),
        ]
        for opener in cases:
            with (
                self.subTest(opener=opener),
                self.assertRaises(NotificationError) as raised,
            ):
                FeishuNotifier("https://example.com/hook/super-secret", 5, opener).send(
                    digest
                )
            self.assertNotIn("super-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
