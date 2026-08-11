"""SQLite-backed successful-delivery deduplication."""

from collections.abc import Iterable
from pathlib import Path
import sqlite3

from .model import NewsItem


SCHEMA = """
CREATE TABLE IF NOT EXISTS delivered_items (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    url TEXT NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (source, item_id),
    UNIQUE (source, url)
)
"""


class SQLiteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(SCHEMA)

    def unseen(self, items: Iterable[NewsItem], *, read_only: bool = False) -> list[NewsItem]:
        candidates = list(items)
        if not candidates:
            return []
        if read_only and not self.path.exists():
            return candidates
        if not read_only:
            self.initialize()

        connection = self._connect_read_only() if read_only else sqlite3.connect(self.path)
        try:
            if read_only and not _table_exists(connection):
                return candidates
            result: list[NewsItem] = []
            batch_item_keys: set[tuple[str, str]] = set()
            batch_url_keys: set[tuple[str, str]] = set()
            for item in candidates:
                item_key = (item.source, item.item_id)
                url_key = (item.source, item.url)
                if item_key in batch_item_keys or url_key in batch_url_keys:
                    continue
                row = connection.execute(
                    """
                    SELECT 1 FROM delivered_items
                    WHERE source = ? AND (item_id = ? OR url = ?)
                    LIMIT 1
                    """,
                    (item.source, item.item_id, item.url),
                ).fetchone()
                if row is None:
                    result.append(item)
                    batch_item_keys.add(item_key)
                    batch_url_keys.add(url_key)
            return result
        finally:
            connection.close()

    def record_delivered(self, items: Iterable[NewsItem]) -> None:
        delivered = list(items)
        if not delivered:
            return
        self.initialize()
        with sqlite3.connect(self.path) as connection:
            connection.executemany(
                "INSERT INTO delivered_items (source, item_id, url) VALUES (?, ?, ?)",
                ((item.source, item.item_id, item.url) for item in delivered),
            )

    def _connect_read_only(self) -> sqlite3.Connection:
        return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)


def _table_exists(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'delivered_items'"
        ).fetchone()
        is not None
    )
