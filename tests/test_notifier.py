import json
from urllib.error import HTTPError
import unittest

from signalfeed.notifier import (
    FeishuNotifier,
    NotificationError,
    build_digest,
)

from tests.helpers import FakeResponse, news_item


class NotifierTests(unittest.TestCase):
    def test_builds_bounded_post_and_truncates_summary(self) -> None:
        first = news_item(content="x" * 500)
        second = news_item(item_id="2", guid="2", url="https://example.com/2")
        digest = build_digest(
            [first, second], title="Signals", max_payload_bytes=18 * 1024, summary_max_chars=40
        )
        self.assertEqual(digest.payload["msg_type"], "post")
        self.assertLessEqual(len(digest.encoded), 18 * 1024)
        self.assertEqual(digest.items, (first, second))
        body = digest.encoded.decode()
        self.assertIn("xxx…", body)
        self.assertIn("OpenAI News", body)
        self.assertIn("2026-08-10T04:00:00Z", body)
        self.assertIn("https://example.com/news/1", body)

    def test_newest_items_are_a_prefix_when_payload_fills(self) -> None:
        items = [
            news_item(item_id=str(i), guid=str(i), url=f"https://example.com/{i}")
            for i in range(3)
        ]
        one = build_digest(items[:1], title="Signals", max_payload_bytes=5000, summary_max_chars=40)
        digest = build_digest(
            items,
            title="Signals",
            max_payload_bytes=len(one.encoded) + 10,
            summary_max_chars=40,
        )
        self.assertEqual(digest.items, (items[0],))

    def test_post_method_headers_json_and_code_zero(self) -> None:
        digest = build_digest(
            [news_item()], title="Signals", max_payload_bytes=5000, summary_max_chars=100
        )
        seen: dict[str, object] = {}

        def opener(request: object, *, timeout: float) -> FakeResponse:
            seen["request"] = request
            seen["timeout"] = timeout
            return FakeResponse(b'{"code":0,"msg":"success"}')

        FeishuNotifier("https://example.com/hook/secret", 7.0, opener).send(digest)
        request = seen["request"]
        self.assertEqual(request.get_method(), "POST")  # type: ignore[union-attr]
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")  # type: ignore[union-attr]
        self.assertEqual(json.loads(request.data), digest.payload)  # type: ignore[union-attr]
        self.assertEqual(seen["timeout"], 7.0)

    def test_rejects_business_error_http_error_and_non_json(self) -> None:
        digest = build_digest(
            [news_item()], title="Signals", max_payload_bytes=5000, summary_max_chars=100
        )
        cases = [
            lambda request, timeout: FakeResponse(b'{"code":19001,"msg":"bad"}'),
            lambda request, timeout: FakeResponse(b"not json"),
            lambda request, timeout: (_ for _ in ()).throw(
                HTTPError("https://redacted.invalid", 500, "error", {}, None)
            ),
        ]
        for opener in cases:
            with self.subTest(opener=opener), self.assertRaises(NotificationError) as raised:
                FeishuNotifier("https://example.com/hook/super-secret", 5, opener).send(digest)
            self.assertNotIn("super-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
