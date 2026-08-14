import logging
import unittest
from io import StringIO

from signalfeed.collector import (
    COLLECTOR_REGISTRY,
    CollectionBatch,
    CollectionError,
    MarkdownCardsCollector,
    MarkdownChangelogCollector,
    MarkdownIndexCollector,
    NextDataIndexCollector,
    RSSCollector,
    clean_html,
    collect_source,
    normalize_date,
)
from signalfeed.config import NetworkConfig, SourceConfig
from signalfeed.model import NewsItem, canonicalize_url
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

MARKDOWN_INDEX = b"""Title: Engineering
URL Source: https://example.com/engineering
Published Time: Fri, 14 Aug 2026 04:45:23 GMT

Markdown Content:
[![Image 1: Featured containment](https://cdn.example.net/one.svg) Featured ## Featured containment A description without a date.](https://example.com/engineering/featured)
[![Image 2: How we built agents](https://cdn.example.net/two.svg) ### How we built agents Jul 20](https://example.com/engineering/agents?utm_source=feed)[![Image 3: Duplicate](https://cdn.example.net/three.svg) ### Duplicate Jul 20](https://example.com/engineering/agents?fbclid=tracking)
### [Kimi \xe5\xbc\x80\xe6\x94\xbe\xe5\xb9\xb3\xe5\x8f\xb0](https://example.com/engineering/kimi)
2025\xe5\xb9\xb411\xe6\x9c\x8807\xe6\x97\xa5
### [Unsafe target](https://evil.example/article)
2026-08-01
"""

MARKDOWN_CHANGELOG = b"""# Changelog

## August, 2026

### Aug 13

Announcement

Released a new API mode.

### Aug 7

#### Two changes on the same date

* First change
* Second change

## July, 2026

### Jul 30

Updated model snapshots.
"""

MARKDOWN_CARDS = b"""# Models

#### Jul. 31, 2026

<Card title="MiniMax H3" href="https://www.example.com/news/h3?utm_campaign=x">
  A multimodal video model.<br /><br />Learn more.
</Card>

#### Apr. 2026

<Card title="Music-2.6" href="/news/music-26">
  Cover reborn.
</Card>

<Card title="Broken" href="https://evil.example/broken">Bad host.</Card>
"""

NEXT_DATA = b"""<!doctype html><html><body>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"articleList":{"items":[
  {"id":"kimi-k3","title":"Kimi K3","description":"Research", "href":"/blog/kimi-k3","date":"2026/07/16"},
  {"id":"duplicate","title":"Duplicate","href":"/blog/kimi-k3?utm_medium=x","date":"2026/07/16"},
  {"id":"project","title":"Project","href":"https://github.com/moonshotai/project","date":"2026/06/01"},
  {"id":"bad","title":"Bad","href":"https://evil.example/bad","date":"not-a-date"}
]}}}}
</script></body></html>"""


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
        self.assertEqual(first.published_at, "2026-08-10T12:30:00+08:00")
        self.assertEqual(first.author, "Example Author")
        self.assertEqual(first.category, "Research, AI")
        self.assertEqual(second.item_id, "https://example.com/two")
        self.assertEqual(second.guid, "")
        self.assertEqual(second.content, "Second summary")
        self.assertEqual(len(items.issues), 1)
        self.assertEqual(items.issues[0].title, "Broken entry")
        self.assertEqual(first.dedupe_key, "https://example.com/one")
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

    def test_inline_rss_entries_remain_ordinary_url_deduplication_items(self) -> None:
        source = SourceConfig(
            name="GLM Model Releases",
            url="https://example.com/rss",
            collector="rss",
            content_mode="inline",
            allowed_hosts=("example.com",),
            filter=False,
        )
        original = RSSCollector(
            source,
            self.network,
            lambda request, timeout: FakeResponse(RSS),
        ).collect()
        changed = RSSCollector(
            source,
            self.network,
            lambda request, timeout: FakeResponse(
                RSS.replace(b"Hello <b>world</b>.", b"Updated inline content.")
            ),
        ).collect()

        self.assertEqual(original.items[0].dedupe_key, "https://example.com/one")
        self.assertEqual(original.items[0].dedupe_key, changed.items[0].dedupe_key)

    def test_rejects_oversized_and_invalid_xml_responses(self) -> None:
        tiny = NetworkConfig(15.0, 4, "test")
        with self.assertRaisesRegex(CollectionError, "exceeds"):
            RSSCollector(
                self.source, tiny, lambda request, timeout: FakeResponse(b"12345")
            ).collect()
        with self.assertRaisesRegex(CollectionError, "invalid RSS XML"):
            RSSCollector(
                self.source,
                self.network,
                lambda request, timeout: FakeResponse(b"<rss>"),
            ).collect()

    def test_clean_html_and_naive_date(self) -> None:
        self.assertEqual(clean_html("<p>A&nbsp; B</p><style>hidden</style> C"), "A B C")
        self.assertEqual(
            normalize_date("10 Aug 2026 04:00:00"),
            "2026-08-10T12:00:00+08:00",
        )
        self.assertEqual(
            normalize_date("10 Aug 2026 04:00:00 -0700"),
            "2026-08-10T19:00:00+08:00",
        )

    def test_markdown_index_handles_joined_cards_dates_duplicates_and_issues(
        self,
    ) -> None:
        source = SourceConfig(
            name="Engineering",
            url="https://example.com/engineering",
            collector="markdown_index",
            transport="jina",
            content_mode="article",
            allowed_hosts=("example.com",),
            filter=False,
        )
        seen: dict[str, object] = {}

        def opener(request: object, *, timeout: float) -> FakeResponse:
            seen["request"] = request
            return FakeResponse(MARKDOWN_INDEX)

        batch = MarkdownIndexCollector(source, self.network, opener).collect()
        self.assertIsInstance(batch, CollectionBatch)
        self.assertEqual(
            [item.title for item in batch.items],
            ["Featured containment", "How we built agents", "Kimi 开放平台"],
        )
        self.assertEqual(batch.items[0].published_at, "")
        self.assertEqual(batch.items[1].published_at, "2026-07-20")
        self.assertEqual(batch.items[2].published_at, "2025-11-07")
        self.assertEqual(len(batch.issues), 1)
        self.assertIn("not allowed", batch.issues[0].message)
        request = seen["request"]
        self.assertTrue(request.full_url.startswith("https://r.jina.ai/"))  # type: ignore[union-attr]
        self.assertEqual(request.get_header("X-retain-links"), "all")  # type: ignore[union-attr]

    def test_markdown_changelog_preserves_precision_and_content_signatures(
        self,
    ) -> None:
        source = SourceConfig(
            name="API Changelog",
            url="https://example.com/changelog",
            window_size=2,
            collector="markdown_changelog",
            content_mode="inline",
            allowed_hosts=("example.com",),
            filter=False,
        )
        batch = MarkdownChangelogCollector(
            source,
            self.network,
            lambda request, timeout: FakeResponse(MARKDOWN_CHANGELOG),
        ).collect()
        self.assertEqual(
            [item.published_at for item in batch], ["2026-08-13", "2026-08-07"]
        )
        self.assertEqual(len({item.item_id for item in batch}), 2)
        self.assertTrue(all(item.dedupe_key.startswith("changelog:") for item in batch))
        self.assertIn("First change", batch.items[1].content)
        changed = MARKDOWN_CHANGELOG.replace(b"First change", b"Changed first entry")
        changed_batch = MarkdownChangelogCollector(
            source,
            self.network,
            lambda request, timeout: FakeResponse(changed),
        ).collect()
        self.assertNotEqual(
            batch.items[1].dedupe_key, changed_batch.items[1].dedupe_key
        )

    def test_markdown_changelog_ignores_bad_entries_outside_raw_window(self) -> None:
        source = SourceConfig(
            name="API Changelog",
            url="https://example.com/changelog",
            window_size=2,
            collector="markdown_changelog",
            content_mode="inline",
            allowed_hosts=("example.com",),
            filter=False,
        )
        markdown = MARKDOWN_CHANGELOG + b"\n### Feb 30, 2020\nBroken history.\n"
        batch = MarkdownChangelogCollector(
            source,
            self.network,
            lambda request, timeout: FakeResponse(markdown),
        ).collect()

        self.assertEqual(
            [item.published_at for item in batch.items],
            ["2026-08-13", "2026-08-07"],
        )
        self.assertEqual(batch.issues, ())

    def test_markdown_changelog_preserves_month_only_section_precision(self) -> None:
        source = SourceConfig(
            name="Monthly API Changelog",
            url="https://example.com/changelog",
            collector="markdown_changelog",
            content_mode="inline",
            allowed_hosts=("example.com",),
            filter=False,
        )
        markdown = b"""# Changelog

## April 2026

Monthly API compatibility update.

## March 2026

Older monthly update.
"""
        batch = MarkdownChangelogCollector(
            source,
            self.network,
            lambda request, timeout: FakeResponse(markdown),
        ).collect()

        self.assertEqual(
            [item.published_at for item in batch.items], ["2026-04", "2026-03"]
        )
        self.assertEqual(batch.issues, ())

    def test_markdown_cards_parses_month_precision_and_bad_card(self) -> None:
        source = SourceConfig(
            name="Model Releases",
            url="https://platform.example.com/models",
            collector="markdown_cards",
            content_mode="article",
            allowed_hosts=("example.com",),
            filter=False,
        )
        batch = MarkdownCardsCollector(
            source,
            self.network,
            lambda request, timeout: FakeResponse(MARKDOWN_CARDS),
        ).collect()
        self.assertEqual([item.title for item in batch], ["MiniMax H3", "Music-2.6"])
        self.assertEqual(
            [item.published_at for item in batch], ["2026-07-31", "2026-04"]
        )
        self.assertEqual(
            batch.items[0].content, "A multimodal video model. Learn more."
        )
        self.assertEqual(len(batch.issues), 1)

    def test_markdown_cards_reports_unclosed_card_and_keeps_later_card(self) -> None:
        source = SourceConfig(
            name="Model Releases",
            url="https://platform.example.com/models",
            collector="markdown_cards",
            content_mode="article",
            allowed_hosts=("example.com",),
            filter=False,
        )
        markdown = b"""# Models

#### Jul. 31, 2026

<Card title="Broken" href="/news/broken">
Missing close.

<Card title="Recovered" href="/news/recovered">
Valid later card.
</Card>
"""
        batch = MarkdownCardsCollector(
            source,
            self.network,
            lambda request, timeout: FakeResponse(markdown),
        ).collect()

        self.assertEqual([item.title for item in batch.items], ["Recovered"])
        self.assertEqual(batch.issues[0].title, "Broken")
        self.assertIn("closing tag", batch.issues[0].message)

    def test_invalid_markdown_dates_are_entry_issues_not_source_failures(self) -> None:
        index_source = SourceConfig(
            name="Engineering",
            url="https://example.com/engineering",
            collector="markdown_index",
            transport="jina",
            content_mode="article",
            allowed_hosts=("example.com",),
            filter=False,
        )
        index_batch = MarkdownIndexCollector(
            index_source,
            self.network,
            lambda request, timeout: FakeResponse(
                MARKDOWN_INDEX.replace(b"Jul 20", b"Feb 30, 2026", 1)
            ),
        ).collect()
        self.assertIn("Kimi 开放平台", [item.title for item in index_batch.items])
        self.assertTrue(
            any(
                "invalid page publication date" in issue.message
                for issue in index_batch.issues
            )
        )

        changelog_source = SourceConfig(
            name="API Changelog",
            url="https://example.com/changelog",
            collector="markdown_changelog",
            content_mode="inline",
            allowed_hosts=("example.com",),
            filter=False,
        )
        changelog_batch = MarkdownChangelogCollector(
            changelog_source,
            self.network,
            lambda request, timeout: FakeResponse(
                MARKDOWN_CHANGELOG.replace(b"Aug 13", b"Feb 30, 2026")
            ),
        ).collect()
        self.assertEqual(
            [item.published_at for item in changelog_batch.items],
            ["2026-08-07", "2026-07-30"],
        )
        self.assertEqual(changelog_batch.issues[0].title, "Feb 30, 2026")

        cards_source = SourceConfig(
            name="Model Releases",
            url="https://platform.example.com/models",
            collector="markdown_cards",
            content_mode="article",
            allowed_hosts=("example.com",),
            filter=False,
        )
        cards_batch = MarkdownCardsCollector(
            cards_source,
            self.network,
            lambda request, timeout: FakeResponse(
                MARKDOWN_CARDS.replace(b"Jul. 31, 2026", b"Feb. 30, 2026")
            ),
        ).collect()
        self.assertEqual([item.title for item in cards_batch.items], ["Music-2.6"])
        self.assertTrue(
            any(issue.title == "MiniMax H3" for issue in cards_batch.issues)
        )

    def test_malformed_item_urls_are_isolated_without_secondary_join_errors(
        self,
    ) -> None:
        index_source = SourceConfig(
            name="Engineering",
            url="https://example.com/engineering",
            collector="markdown_index",
            transport="jina",
            content_mode="article",
            allowed_hosts=("example.com",),
            filter=False,
        )
        index_batch = MarkdownIndexCollector(
            index_source,
            self.network,
            lambda request, timeout: FakeResponse(
                MARKDOWN_INDEX.replace(b"https://evil.example/article", b"http://[")
            ),
        ).collect()
        self.assertEqual(len(index_batch.items), 3)
        self.assertTrue(any(issue.url == "http://[" for issue in index_batch.issues))

        cards_source = SourceConfig(
            name="Model Releases",
            url="https://platform.example.com/models",
            collector="markdown_cards",
            content_mode="article",
            allowed_hosts=("example.com",),
            filter=False,
        )
        cards_batch = MarkdownCardsCollector(
            cards_source,
            self.network,
            lambda request, timeout: FakeResponse(
                MARKDOWN_CARDS.replace(b"https://evil.example/broken", b"http://[")
            ),
        ).collect()
        self.assertEqual(len(cards_batch.items), 2)
        self.assertTrue(any(issue.url == "http://[" for issue in cards_batch.issues))

        next_source = SourceConfig(
            name="Kimi Research",
            url="https://www.example.com/blog/",
            collector="next_data_index",
            content_mode="article",
            allowed_hosts=("example.com", "github.com"),
            filter=False,
        )
        next_batch = NextDataIndexCollector(
            next_source,
            self.network,
            lambda request, timeout: FakeResponse(
                NEXT_DATA.replace(b"https://github.com/moonshotai/project", b"http://[")
            ),
        ).collect()
        self.assertEqual([item.title for item in next_batch.items], ["Kimi K3"])
        self.assertTrue(any(issue.url == "http://[" for issue in next_batch.issues))

    def test_next_data_index_uses_fixed_structure_and_allow_list(self) -> None:
        source = SourceConfig(
            name="Kimi Research",
            url="https://www.example.com/blog/",
            collector="next_data_index",
            content_mode="article",
            allowed_hosts=("example.com", "github.com"),
            filter=False,
        )
        batch = NextDataIndexCollector(
            source,
            self.network,
            lambda request, timeout: FakeResponse(NEXT_DATA),
        ).collect()
        self.assertEqual([item.title for item in batch], ["Kimi K3", "Project"])
        self.assertEqual(batch.items[0].published_at, "2026-07-16")
        self.assertEqual(batch.items[1].url, "https://github.com/moonshotai/project")
        self.assertEqual(len(batch.issues), 1)

    def test_next_data_non_object_entry_is_reported_as_an_issue(self) -> None:
        source = SourceConfig(
            name="Kimi Research",
            url="https://www.example.com/blog/",
            collector="next_data_index",
            content_mode="article",
            allowed_hosts=("example.com", "github.com"),
            filter=False,
        )
        malformed = NEXT_DATA.replace(b'"items":[', b'"items":[null,', 1)
        batch = NextDataIndexCollector(
            source,
            self.network,
            lambda request, timeout: FakeResponse(malformed),
        ).collect()

        self.assertEqual([item.title for item in batch], ["Kimi K3", "Project"])
        self.assertTrue(
            any("must be an object" in issue.message for issue in batch.issues)
        )

    def test_next_data_requires_string_title_href_and_date_fields(self) -> None:
        source = SourceConfig(
            name="Kimi Research",
            url="https://www.example.com/blog/",
            collector="next_data_index",
            content_mode="article",
            allowed_hosts=("example.com", "github.com"),
            filter=False,
        )
        cases = (
            (b'"title":"Kimi K3"', b'"title":{"text":"Kimi K3"}', "title"),
            (b'"href":"/blog/kimi-k3"', b'"href":123', "href"),
            (b'"date":"2026/07/16"', b'"date":[2026,7,16]', "date"),
        )
        for original, replacement, field in cases:
            with self.subTest(field=field):
                response_body = NEXT_DATA.replace(original, replacement, 1)
                batch = NextDataIndexCollector(
                    source,
                    self.network,
                    lambda request, timeout, body=response_body: FakeResponse(body),
                ).collect()
                self.assertIn("Project", [item.title for item in batch.items])
                self.assertTrue(
                    any(
                        f"{field} must be a string" in issue.message
                        for issue in batch.issues
                    )
                )

    def test_registry_covers_all_collectors_and_canonicalizes_tracking(self) -> None:
        self.assertEqual(
            set(COLLECTOR_REGISTRY),
            {
                "rss",
                "markdown_index",
                "markdown_changelog",
                "markdown_cards",
                "next_data_index",
            },
        )
        source = SourceConfig("RSS", "https://example.com/rss")
        batch = collect_source(
            source,
            self.network,
            lambda request, timeout: FakeResponse(RSS),
        )
        self.assertEqual(len(batch), 2)
        self.assertEqual(
            canonicalize_url(
                "HTTPS://Example.COM:443/one?utm_source=x&b=2&a=1#section"
            ),
            "https://example.com/one?a=1&b=2",
        )
        item = NewsItem(
            "Source", "id", "title", "", "https://example.com/a#one", "", "", "", ""
        )
        self.assertEqual(item.dedupe_key, "https://example.com/a")

    def test_all_text_collectors_enforce_response_limit(self) -> None:
        tiny = NetworkConfig(15.0, 4, "test")
        cases = (
            ("markdown_index", MarkdownIndexCollector),
            ("markdown_changelog", MarkdownChangelogCollector),
            ("markdown_cards", MarkdownCardsCollector),
            ("next_data_index", NextDataIndexCollector),
        )
        for collector_name, collector_type in cases:
            with self.subTest(collector=collector_name):
                source = SourceConfig(
                    "Source",
                    "https://example.com/source",
                    collector=collector_name,
                    allowed_hosts=("example.com",),
                )
                with self.assertRaisesRegex(CollectionError, "exceeds"):
                    collector_type(
                        source,
                        tiny,
                        lambda request, timeout: FakeResponse(b"12345"),
                    ).collect()


if __name__ == "__main__":
    unittest.main()
