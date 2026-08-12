"""SQLite-backed successful-delivery deduplication."""

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from .config import ModelConfig
from .model import ChineseSummary, NewsItem
from .summarizer import SummaryError, parse_summary

DELIVERED_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivered_items (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    url TEXT NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (source, item_id),
    UNIQUE (source, url)
)
"""

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chinese_summary_cache (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    item_url TEXT NOT NULL,
    model TEXT NOT NULL,
    protocol TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key_env TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    title_zh TEXT NOT NULL,
    bullets_zh_json TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (
        source, item_id, item_url, model, protocol, base_url, api_key_env, prompt_version
    )
)
"""


class SQLiteStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(DELIVERED_SCHEMA)
            connection.execute(CACHE_SCHEMA)

    def unseen(
        self, items: Iterable[NewsItem], *, read_only: bool = False
    ) -> list[NewsItem]:
        candidates = list(items)
        if not candidates:
            return []
        if read_only and not self.path.exists():
            return candidates
        if not read_only:
            self.initialize()

        connection = (
            self._connect_read_only() if read_only else sqlite3.connect(self.path)
        )
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
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executemany(
                "INSERT INTO delivered_items (source, item_id, url) VALUES (?, ?, ?)",
                ((item.source, item.item_id, item.url) for item in delivered),
            )

    def cached_summary(
        self,
        item: NewsItem,
        model: ModelConfig,
        prompt_version: str,
        *,
        read_only: bool = False,
    ) -> ChineseSummary | None:
        if read_only and not self.path.exists():
            return None
        if not read_only:
            self.initialize()
        connection = (
            self._connect_read_only() if read_only else sqlite3.connect(self.path)
        )
        try:
            if read_only and not _table_exists(connection, "chinese_summary_cache"):
                return None
            row = connection.execute(
                """
                SELECT title_zh, bullets_zh_json
                FROM chinese_summary_cache
                WHERE source = ? AND item_id = ? AND item_url = ?
                  AND model = ? AND protocol = ? AND base_url = ?
                  AND api_key_env = ? AND prompt_version = ?
                LIMIT 1
                """,
                _cache_key(item, model, prompt_version),
            ).fetchone()
            if row is None:
                return None
            try:
                return parse_summary(
                    json.dumps(
                        {"title_zh": row[0], "bullets_zh": json.loads(row[1])},
                        ensure_ascii=False,
                    )
                )
            except json.JSONDecodeError, SummaryError, TypeError:
                return None
        finally:
            connection.close()

    def cache_summary(
        self,
        item: NewsItem,
        model: ModelConfig,
        prompt_version: str,
        summary: ChineseSummary,
    ) -> None:
        self.initialize()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO chinese_summary_cache (
                    source, item_id, item_url, model, protocol, base_url,
                    api_key_env, prompt_version, title_zh, bullets_zh_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *_cache_key(item, model, prompt_version),
                    summary.title_zh,
                    json.dumps(summary.bullets_zh, ensure_ascii=False),
                ),
            )

    def _connect_read_only(self) -> sqlite3.Connection:
        return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)


def _table_exists(
    connection: sqlite3.Connection, table_name: str = "delivered_items"
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _cache_key(
    item: NewsItem, model: ModelConfig, prompt_version: str
) -> tuple[str, ...]:
    return (
        item.source,
        item.item_id,
        item.url,
        model.model,
        model.protocol,
        model.base_url,
        model.api_key_env,
        prompt_version,
    )
