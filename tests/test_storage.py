import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from signalfeed.config import ModelConfig
from signalfeed.model import ChineseSummary
from signalfeed.storage import (
    BASELINE_FAILURE_ITEM_KEY,
    SOURCE_FAILURE_ITEM_KEY,
    SQLiteStorage,
)
from tests.helpers import news_item


class StorageTests(unittest.TestCase):
    model = ModelConfig(
        "model-a",
        "openai_responses",
        "https://api.example.com/v1",
        "SIGNALFEED_LLM_API_KEY",
    )

    def test_persists_across_reopen_and_deduplicates_same_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "feed.sqlite3"
            first = news_item()
            SQLiteStorage(path).record_delivered([first])

            reopened = SQLiteStorage(path)
            self.assertEqual(reopened.unseen([first]), [])
            same_url_new_guid = news_item(
                item_id="different-guid", guid="different-guid"
            )
            self.assertEqual(reopened.unseen([same_url_new_guid]), [])
            self.assertEqual(
                reopened.unseen(
                    [
                        news_item(
                            item_id="guid-2", guid="guid-2", url="https://example.com/2"
                        )
                    ]
                ),
                [
                    news_item(
                        item_id="guid-2", guid="guid-2", url="https://example.com/2"
                    )
                ],
            )

    def test_read_only_missing_database_does_not_create_anything(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "feed.sqlite3"
            item = news_item()
            self.assertEqual(SQLiteStorage(path).unseen([item], read_only=True), [item])
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    def test_deduplicates_repeated_ids_and_urls_within_one_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "feed.sqlite3")
            first = news_item()
            duplicate_id = news_item(url="https://example.com/other")
            duplicate_url = news_item(item_id="other-guid", guid="other-guid")
            self.assertEqual(
                storage.unseen([first, duplicate_id, duplicate_url]),
                [first],
            )

    def test_chinese_cache_key_includes_item_model_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            storage = SQLiteStorage(path)
            item = news_item()
            summary = ChineseSummary(
                "中文标题", ("第一条要点", "第二条要点", "第三条要点")
            )
            storage.cache_summary(item, self.model, "prompt-v1", summary)

            self.assertEqual(
                storage.cached_summary(item, self.model, "prompt-v1", read_only=True),
                summary,
            )
            self.assertIsNone(
                storage.cached_summary(item, self.model, "prompt-v2", read_only=True)
            )
            changed_model = ModelConfig(
                "model-b",
                self.model.protocol,
                self.model.base_url,
                self.model.api_key_env,
            )
            self.assertIsNone(
                storage.cached_summary(item, changed_model, "prompt-v1", read_only=True)
            )
            tracking_variant = news_item(
                item_id="other-feed-id",
                guid="other-feed-id",
                url=f"{item.url}?utm_source=retry#section",
            )
            self.assertEqual(
                storage.cached_summary(
                    tracking_variant, self.model, "prompt-v1", read_only=True
                ),
                summary,
            )
            self.assertIsNone(
                storage.cached_summary(
                    news_item(
                        item_id="other",
                        url="https://example.com/news/other",
                    ),
                    self.model,
                    "prompt-v1",
                    read_only=True,
                )
            )

    def test_read_only_cache_miss_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "feed.sqlite3"
            result = SQLiteStorage(path).cached_summary(
                news_item(), self.model, "prompt-v1", read_only=True
            )
            self.assertIsNone(result)
            self.assertFalse(path.parent.exists())

    def test_legacy_raw_url_cache_is_reused_by_canonical_dedupe_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            storage = SQLiteStorage(path)
            storage.initialize()
            original = news_item(
                url="https://example.com/news/1?utm_source=old#section"
            )
            summary = ChineseSummary(
                "旧缓存中文标题", ("第一条要点", "第二条要点", "第三条要点")
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO chinese_summary_cache (
                        source, item_id, item_url, model, protocol, base_url,
                        api_key_env, prompt_version, title_zh, bullets_zh_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        original.source,
                        original.item_id,
                        original.url,
                        self.model.model,
                        self.model.protocol,
                        self.model.base_url,
                        self.model.api_key_env,
                        "prompt-v1",
                        summary.title_zh,
                        '["第一条要点", "第二条要点", "第三条要点"]',
                    ),
                )

            retry = news_item(
                item_id="changed-feed-id",
                guid="changed-feed-id",
                url="https://example.com/news/1?fbclid=new#different",
            )
            self.assertEqual(
                storage.cached_summary(retry, self.model, "prompt-v1", read_only=True),
                summary,
            )

    def test_delivery_dedupe_key_is_global_across_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "feed.sqlite3")
            delivered = news_item(
                url="https://Example.com/news/1?utm_source=rss&edition=cn#top"
            )
            storage.record_delivered([delivered])

            duplicate = news_item(
                source="Anthropic Newsroom",
                item_id="anthropic-guid",
                guid="anthropic-guid",
                url="https://example.com/news/1?edition=cn",
            )
            self.assertTrue(storage.is_delivered(duplicate, read_only=True))
            self.assertEqual(storage.unseen([duplicate], read_only=True), [])

    def test_changelog_keys_allow_multiple_entries_on_one_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            storage = SQLiteStorage(path)
            first = news_item(
                source="API Changelog",
                item_id="same-page-id",
                guid="same-page-id",
                url="https://example.com/changelog",
                dedupe_key="changelog:first",
            )
            second = news_item(
                source="API Changelog",
                item_id="same-page-id",
                guid="same-page-id",
                url="https://example.com/changelog",
                dedupe_key="changelog:second",
            )

            self.assertEqual(storage.unseen([first, second]), [first, second])
            storage.record_delivered([first, second])

            self.assertTrue(storage.is_delivered(first, read_only=True))
            self.assertTrue(storage.is_delivered(second, read_only=True))
            with closing(sqlite3.connect(path)) as connection, connection:
                count = connection.execute(
                    "SELECT count(*) FROM delivered_items"
                ).fetchone()[0]
            self.assertEqual(count, 2)

    def test_source_baseline_is_atomic_idempotent_and_globally_seen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            storage = SQLiteStorage(path)
            items = [
                news_item(item_id=f"item-{index}", guid=f"item-{index}", url=url)
                for index, url in enumerate(
                    ("https://example.com/1", "https://example.com/2"), start=1
                )
            ]

            self.assertTrue(storage.initialize_source_baseline("OpenAI News", items))
            self.assertTrue(storage.is_source_initialized("OpenAI News"))
            self.assertEqual(
                storage.baseline_items("OpenAI News", read_only=True),
                frozenset(item.dedupe_key for item in items),
            )
            self.assertEqual(storage.unseen(items, read_only=True), [])
            cross_source_duplicate = news_item(
                source="Anthropic Newsroom",
                item_id="duplicate",
                guid="duplicate",
                url=items[0].url,
            )
            self.assertTrue(
                storage.is_baseline_item(cross_source_duplicate, read_only=True)
            )
            self.assertEqual(
                storage.unseen([cross_source_duplicate], read_only=True), []
            )

            later = news_item(
                item_id="later", guid="later", url="https://example.com/later"
            )
            self.assertFalse(storage.initialize_source_baseline("OpenAI News", [later]))
            self.assertNotIn(
                later.dedupe_key,
                storage.baseline_items("OpenAI News", read_only=True),
            )
            self.assertEqual(storage.unseen([later], read_only=True), [later])

    def test_empty_window_still_initializes_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "feed.sqlite3")
            self.assertTrue(storage.initialize_source_baseline("Empty Source", []))
            self.assertTrue(storage.is_source_initialized("Empty Source"))
            self.assertEqual(storage.baseline_items("Empty Source"), frozenset())

    def test_baseline_atomically_recovers_item_and_baseline_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "feed.sqlite3")
            item = news_item(source="New Source")
            storage.record_active_failure(item.source, item.dedupe_key)
            storage.record_active_failure(item.source, BASELINE_FAILURE_ITEM_KEY)

            self.assertTrue(storage.initialize_source_baseline(item.source, [item]))

            self.assertFalse(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )
            self.assertFalse(
                storage.has_active_failure(
                    item.source, BASELINE_FAILURE_ITEM_KEY, read_only=True
                )
            )

    def test_invalid_baseline_does_not_partially_initialize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "feed.sqlite3"
            storage = SQLiteStorage(path)
            wrong_source = news_item(source="Different Source")

            with self.assertRaisesRegex(ValueError, "belong to the source"):
                storage.initialize_source_baseline("Expected Source", [wrong_source])

            self.assertFalse(path.exists())

    def test_old_database_migrates_without_losing_delivery_or_timestamp(self) -> None:
        old_schema = """
        CREATE TABLE delivered_items (
            source TEXT NOT NULL,
            item_id TEXT NOT NULL,
            url TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            PRIMARY KEY (source, item_id),
            UNIQUE (source, url)
        )
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(old_schema)
                connection.execute(
                    """
                    INSERT INTO delivered_items
                        (source, item_id, url, delivered_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "OpenAI News",
                        "old-guid",
                        "https://EXAMPLE.com/old?utm_source=rss#section",
                        "2025-01-02T03:04:05Z",
                    ),
                )

            storage = SQLiteStorage(path)
            storage.initialize()

            with closing(sqlite3.connect(path)) as connection, connection:
                row = connection.execute(
                    """
                    SELECT source, item_id, url, dedupe_key, delivered_at
                    FROM delivered_items
                    """
                ).fetchone()
                table_names = {
                    value[0]
                    for value in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                unique_indexes = [
                    index
                    for index in connection.execute(
                        "PRAGMA index_list(delivered_items)"
                    )
                    if index[2]
                ]
            self.assertEqual(
                row,
                (
                    "OpenAI News",
                    "old-guid",
                    "https://EXAMPLE.com/old?utm_source=rss#section",
                    "https://example.com/old",
                    "2025-01-02T03:04:05Z",
                ),
            )
            self.assertTrue(
                {"source_state", "baseline_items", "active_failures"} <= table_names
            )
            self.assertEqual(unique_indexes, [])
            self.assertTrue(storage.is_source_initialized("OpenAI News"))
            self.assertTrue(
                storage.is_delivered(
                    news_item(
                        source="Other Source",
                        item_id="new-id",
                        guid="new-id",
                        url="https://example.com/old",
                    ),
                    read_only=True,
                )
            )

    def test_read_only_old_database_uses_delivery_as_openai_state_without_migration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE delivered_items (
                        source TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        url TEXT NOT NULL,
                        delivered_at TEXT NOT NULL,
                        PRIMARY KEY (source, item_id),
                        UNIQUE (source, url)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO delivered_items
                        (source, item_id, url, delivered_at)
                    VALUES ('OpenAI News', 'old', 'https://example.com/old', 'old')
                    """
                )
            before = path.read_bytes()

            storage = SQLiteStorage(path)
            self.assertTrue(
                storage.is_source_initialized("OpenAI News", read_only=True)
            )
            self.assertFalse(
                storage.is_source_initialized("Anthropic Newsroom", read_only=True)
            )
            self.assertEqual(
                storage.baseline_items("OpenAI News", read_only=True), set()
            )
            self.assertFalse(
                storage.has_active_failure(
                    "OpenAI News", SOURCE_FAILURE_ITEM_KEY, read_only=True
                )
            )
            self.assertEqual(path.read_bytes(), before)

    def test_all_read_only_state_queries_leave_missing_database_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "feed.sqlite3"
            storage = SQLiteStorage(path)
            item = news_item()

            self.assertEqual(storage.unseen([item], read_only=True), [item])
            self.assertFalse(storage.is_delivered(item, read_only=True))
            self.assertFalse(storage.is_source_initialized(item.source, read_only=True))
            self.assertEqual(
                storage.baseline_items(item.source, read_only=True), frozenset()
            )
            self.assertFalse(storage.is_baseline_item(item, read_only=True))
            self.assertFalse(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )
            self.assertFalse(path.parent.exists())

    def test_active_failure_deduplicates_until_the_event_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.sqlite3"
            storage = SQLiteStorage(path)
            item = news_item()

            self.assertTrue(storage.record_active_failure(item.source, item.dedupe_key))
            self.assertFalse(
                storage.record_active_failure(item.source, item.dedupe_key)
            )
            self.assertTrue(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )

            storage.record_active_failure(item.source, SOURCE_FAILURE_ITEM_KEY)
            self.assertEqual(
                storage.clear_active_failure(item.source, SOURCE_FAILURE_ITEM_KEY), 1
            )
            self.assertTrue(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )

            storage.record_delivered([item])
            self.assertFalse(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )
            self.assertTrue(storage.record_active_failure(item.source, item.dedupe_key))

    def test_collection_failure_namespace_can_recover_without_clearing_articles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = SQLiteStorage(Path(directory) / "feed.sqlite3")
            item = news_item()
            storage.record_active_failure(item.source, "collection:recovered-entry")
            storage.record_active_failure(item.source, "collection:still-broken")
            storage.record_active_failure(item.source, item.dedupe_key)

            self.assertEqual(
                storage.reconcile_active_failure_namespace(
                    item.source, "collection:", {"collection:still-broken"}
                ),
                1,
            )
            self.assertFalse(
                storage.has_active_failure(
                    item.source, "collection:recovered-entry", read_only=True
                )
            )
            self.assertTrue(
                storage.has_active_failure(
                    item.source, "collection:still-broken", read_only=True
                )
            )
            self.assertTrue(
                storage.has_active_failure(item.source, item.dedupe_key, read_only=True)
            )
            self.assertEqual(
                storage.clear_active_failures_with_prefix(item.source, "collection:"),
                1,
            )


if __name__ == "__main__":
    unittest.main()
