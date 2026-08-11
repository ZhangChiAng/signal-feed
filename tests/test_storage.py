import tempfile
import unittest
from pathlib import Path

from signalfeed.config import ModelConfig
from signalfeed.model import ChineseSummary
from signalfeed.storage import SQLiteStorage
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
            self.assertIsNone(
                storage.cached_summary(
                    news_item(item_id="other"), self.model, "prompt-v1", read_only=True
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


if __name__ == "__main__":
    unittest.main()
