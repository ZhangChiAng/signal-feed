import unittest
from urllib.error import HTTPError

from signalfeed.reader import (
    MAX_READER_BYTES,
    JinaReader,
    ReaderError,
)
from tests.helpers import FakeResponse


class ReaderTests(unittest.TestCase):
    def test_restricted_request_limits_and_markdown_cleanup(self) -> None:
        seen: dict[str, object] = {}
        body = b"""Title: Test
URL Source: https://openai.com/test

![image](https://cdn.example/image.png)
[linked words](https://example.com/path) and https://example.com/bare
"""

        def opener(request: object, *, timeout: float) -> FakeResponse:
            seen["request"] = request
            seen["timeout"] = timeout
            return FakeResponse(body)

        content = JinaReader("SignalFeed/Test", opener).read(
            "https://openai.com/index/test#section"
        )
        request = seen["request"]
        self.assertEqual(
            request.full_url,
            "https://r.jina.ai/https://openai.com/index/test",  # type: ignore[union-attr]
        )
        self.assertEqual(request.get_method(), "GET")  # type: ignore[union-attr]
        self.assertEqual(request.get_header("X-token-budget"), "6000")  # type: ignore[union-attr]
        self.assertEqual(request.get_header("X-max-tokens"), "6000")  # type: ignore[union-attr]
        self.assertEqual(request.get_header("X-retain-links"), "text")  # type: ignore[union-attr]
        self.assertEqual(request.get_header("X-retain-images"), "none")  # type: ignore[union-attr]
        self.assertEqual(seen["timeout"], 45.0)
        self.assertIn("linked words", content)
        self.assertNotIn("http", content)
        self.assertNotIn("image", content)

    def test_allows_only_https_openai_articles(self) -> None:
        forbidden = [
            "http://openai.com/test",
            "https://example.com/test",
            "https://openai.com.evil.test/test",
            "https://user:secret@openai.com/test",
        ]
        for url in forbidden:
            with self.subTest(url=url), self.assertRaises(ReaderError):
                JinaReader("test", lambda request, timeout: None).read(url)

    def test_response_limit_and_network_errors_are_sanitized(self) -> None:
        with self.assertRaisesRegex(ReaderError, "1048576"):
            JinaReader(
                "test",
                lambda request, timeout: FakeResponse(b"x" * (MAX_READER_BYTES + 1)),
            ).read("https://openai.com/test")

        def failing_opener(request: object, *, timeout: float) -> FakeResponse:
            raise HTTPError("https://r.jina.ai/secret-token", 500, "bad", {}, None)

        with self.assertRaises(ReaderError) as raised:
            JinaReader("test", failing_opener).read("https://openai.com/test")
        self.assertNotIn("secret-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
