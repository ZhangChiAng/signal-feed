"""SignalFeed application orchestration."""

from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .collector import RSSCollector
from .config import AppConfig
from .filter import KeywordFilter
from .notifier import FeishuNotifier, build_digest
from .storage import SQLiteStorage


def run(
    config: AppConfig,
    *,
    mode: str,
    database_path: str | Path,
    output: TextIO,
    webhook_url: str | None = None,
    collector_factory: Callable[..., object] = RSSCollector,
    notifier_factory: Callable[..., object] = FeishuNotifier,
) -> int:
    if mode not in {"dry-run", "send"}:
        raise ValueError(f"unsupported mode: {mode}")
    if mode == "send" and not webhook_url:
        raise ValueError("FEISHU_WEBHOOK_URL is required in --send mode")

    collector = collector_factory(config.source, config.network)
    collected = collector.collect()  # type: ignore[attr-defined]
    matched = KeywordFilter(config.filter.keywords, config.filter.fields).select(collected)
    storage = SQLiteStorage(database_path)
    unseen = storage.unseen(matched, read_only=(mode == "dry-run"))
    if not unseen:
        print("No new matching items.", file=output)
        return 0

    digest = build_digest(
        unseen,
        title=config.feishu.title,
        max_payload_bytes=config.feishu.max_payload_bytes,
        summary_max_chars=config.feishu.summary_max_chars,
    )
    if mode == "dry-run":
        print(digest.encoded.decode("utf-8"), file=output)
        return 0

    notifier = notifier_factory(webhook_url, config.network.timeout_seconds)
    notifier.send(digest)  # type: ignore[attr-defined]
    storage.record_delivered(digest.items)
    print(f"Sent {len(digest.items)} item(s) to Feishu.", file=output)
    return 0
