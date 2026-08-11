from io import StringIO
import logging
import unittest

from signalfeed.collector import CollectionError, RSSCollector, clean_html, normalize_date
from signalfeed.config import NetworkConfig, SourceConfig

from tests.helpers import FakeResponse


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <item>
      <title>  GPT &amp; agents </title>
      <link>https://example.com/one#section</link>
      <guid>stable-one</guid>
      <description><![CDATA[<p>Hello&nbsp; <strong>world</strong>.</p><script>bad()</script>]]></description>
      <pubDate>Sun, 10 Aug 2026 12:30:00 +0800</pubDate>
      <dc:creator> Example Author </dc:creator>
      <category>Research</category><category>AI</category>
    </item>
    <item>
      <title>Broken entry</title>
      <link>https://example.com/broken</link>
      <pubDate>not a date</pubDate>
    </item>
    <item>
      <title>Fallback ID</title>
      <link>https://example.com/two#fragment</link>
      <content:encoded><![CDATA[<div>Second<br>summary</div>]]></content:encoded>
      <pubDate>Sun, 10 Aug 2026 04:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = SourceConfig("OpenAI News", "https://example.com/rss", 20)
        self.network = NetworkConfig(15.0, 5 * 1024 * 1024, "SignalFeed/Test")

    def test_maps_rss_cleans_html_normalizes_time_and_skips_bad_item(self) -> None:
        seen: dict[str, object] = {}

        def opener(request: object, *, timeout: float) -> FakeResponse:
            seen["request"] = request
            seen["timeout"] = timeout
            return FakeResponse(RSS)

        log_output = StringIO()
        handler = logging.StreamHandler(log_output)
        logger = logging.getLogger("signalfeed.collector")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            items = RSSCollector(self.source, self.network, opener).collect()
        finally:
            logger.removeHandler(handler)

        self.assertEqual(len(items), 2)
        first, second = items
        self.assertEqual(first.item_id, "stable-one")
        self.assertEqual(first.title, "GPT & agents")
        self.assertEqual(first.content, "Hello world.")
        self.assertEqual(first.published_at, "2026-08-10T04:30:00Z")
        self.assertEqual(first.author, "Example Author")
        self.assertEqual(first.category, "Research, AI")
        self.assertEqual(second.item_id, "https://example.com/two")
        self.assertEqual(second.guid, "")
        self.assertEqual(second.content, "Second summary")
        self.assertIn("skipping invalid RSS item 2", log_output.getvalue())
        self.assertEqual(seen["timeout"], 15.0)
        request = seen["request"]
        self.assertEqual(request.get_method(), "GET")  # type: ignore[union-attr]
        self.assertEqual(request.get_header("User-agent"), "SignalFeed/Test")  # type: ignore[union-attr]

    def test_limits_feed_order_before_mapping(self) -> None:
        collector = RSSCollector(
            SourceConfig("Source", "https://example.com/rss", 1),
            self.network,
            lambda request, timeout: FakeResponse(RSS),
        )
        self.assertEqual([item.item_id for item in collector.collect()], ["stable-one"])

    def test_rejects_oversized_and_invalid_xml_responses(self) -> None:
        tiny = NetworkConfig(15.0, 4, "test")
        with self.assertRaisesRegex(CollectionError, "exceeds"):
            RSSCollector(self.source, tiny, lambda request, timeout: FakeResponse(b"12345")).collect()
        with self.assertRaisesRegex(CollectionError, "invalid RSS XML"):
            RSSCollector(
                self.source,
                self.network,
                lambda request, timeout: FakeResponse(b"<rss>"),
            ).collect()

    def test_clean_html_and_naive_date(self) -> None:
        self.assertEqual(clean_html("<p>A&nbsp; B</p><style>hidden</style> C"), "A B C")
        self.assertEqual(normalize_date("10 Aug 2026 04:00:00"), "2026-08-10T04:00:00Z")


if __name__ == "__main__":
    unittest.main()
