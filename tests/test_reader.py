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
            "https://developers.openai.com/index/test#section",
            allowed_hosts=("openai.com",),
        )
        request = seen["request"]
        self.assertEqual(
            request.full_url,
            "https://r.jina.ai/https://developers.openai.com/index/test",  # type: ignore[union-attr]
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

    def test_source_allowlist_accepts_exact_hosts_and_subdomains(self) -> None:
        requested: list[str] = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            requested.append(request.full_url)  # type: ignore[union-attr]
            return FakeResponse(b"article")

        reader = JinaReader("test", opener)
        reader.read("https://anthropic.com/news/test", allowed_hosts=("anthropic.com",))
        reader.read(
            "https://www.anthropic.com/engineering/test",
            allowed_hosts=("anthropic.com",),
        )

        self.assertEqual(
            requested,
            [
                "https://r.jina.ai/https://anthropic.com/news/test",
                "https://r.jina.ai/https://www.anthropic.com/engineering/test",
            ],
        )

    def test_rejects_urls_outside_the_source_allowlist(self) -> None:
        forbidden = [
            "http://anthropic.com/test",
            "https://example.com/test",
            "https://anthropic.com.evil.test/test",
            "https://user:secret@anthropic.com/test",
        ]
        for url in forbidden:
            with self.subTest(url=url), self.assertRaises(ReaderError):
                JinaReader("test", lambda request, timeout: None).read(
                    url, allowed_hosts=("anthropic.com",)
                )

    def test_rejects_empty_or_malformed_host_allowlists(self) -> None:
        invalid = [
            (),
            ("https://anthropic.com",),
            ("anthropic.com:443",),
            ("anthropic com",),
        ]
        for allowed_hosts in invalid:
            with (
                self.subTest(allowed_hosts=allowed_hosts),
                self.assertRaises(ReaderError),
            ):
                JinaReader("test", lambda request, timeout: None).read(
                    "https://anthropic.com/test", allowed_hosts=allowed_hosts
                )

    def test_index_mode_retains_links_but_removes_images(self) -> None:
        seen: dict[str, object] = {}
        body = b"""Title: Index
URL Source: https://www.anthropic.com/news

![cover](https://cdn.example/cover.png)
[Claude news](https://www.anthropic.com/news/claude)
"""

        def opener(request: object, *, timeout: float) -> FakeResponse:
            seen["request"] = request
            return FakeResponse(body)

        content = JinaReader("test", opener).read(
            "https://www.anthropic.com/news",
            allowed_hosts=("anthropic.com",),
            retain_links=True,
        )

        request = seen["request"]
        self.assertEqual(request.get_header("X-retain-links"), "all")  # type: ignore[union-attr]
        self.assertEqual(request.get_header("X-retain-images"), "none")  # type: ignore[union-attr]
        self.assertIn("[Claude news](https://www.anthropic.com/news/claude)", content)
        self.assertNotIn("cover", content)
        self.assertNotIn("URL Source:", content)

    def test_response_limit_and_network_errors_are_sanitized(self) -> None:
        with self.assertRaisesRegex(ReaderError, "1048576"):
            JinaReader(
                "test",
                lambda request, timeout: FakeResponse(b"x" * (MAX_READER_BYTES + 1)),
            ).read("https://openai.com/test", allowed_hosts=("openai.com",))

        def failing_opener(request: object, *, timeout: float) -> FakeResponse:
            raise HTTPError("https://r.jina.ai/secret-token", 500, "bad", {}, None)

        with self.assertRaises(ReaderError) as raised:
            JinaReader("test", failing_opener).read(
                "https://openai.com/test", allowed_hosts=("openai.com",)
            )
        self.assertNotIn("secret-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
