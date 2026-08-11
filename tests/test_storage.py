from pathlib import Path
import tempfile
import unittest

from signalfeed.storage import SQLiteStorage

from tests.helpers import news_item


class StorageTests(unittest.TestCase):
    def test_persists_across_reopen_and_deduplicates_same_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "feed.sqlite3"
            first = news_item()
            SQLiteStorage(path).record_delivered([first])

            reopened = SQLiteStorage(path)
            self.assertEqual(reopened.unseen([first]), [])
            same_url_new_guid = news_item(item_id="different-guid", guid="different-guid")
            self.assertEqual(reopened.unseen([same_url_new_guid]), [])
            self.assertEqual(
                reopened.unseen([news_item(item_id="guid-2", guid="guid-2", url="https://example.com/2")]),
                [news_item(item_id="guid-2", guid="guid-2", url="https://example.com/2")],
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


if __name__ == "__main__":
    unittest.main()
