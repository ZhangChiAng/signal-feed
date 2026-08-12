"""SignalFeed application orchestration."""

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .collector import RSSCollector
from .config import AppConfig, FeishuDeliveryConfig, ModelConfig
from .datetime_utils import BEIJING_TIMEZONE, beijing_date
from .filter import KeywordFilter
from .model import ChineseSummary, NewsItem
from .notifier import FeishuNotifier, build_digests
from .reader import JinaReader, ReaderError
from .storage import SQLiteStorage
from .summarizer import PROMPT_VERSION, ResponsesSummarizer, SummaryError

MAX_MODEL_CONCURRENCY = 500


def _now_beijing() -> datetime:
    return datetime.now(BEIJING_TIMEZONE)


@dataclass(frozen=True, slots=True)
class _GenerationResult:
    index: int
    item: NewsItem
    summary: ChineseSummary | None = None
    error: Exception | None = None


def run(
    config: AppConfig,
    model_config: ModelConfig,
    *,
    mode: str,
    database_path: str | Path,
    output: TextIO,
    api_key: str,
    feishu_delivery: FeishuDeliveryConfig | None = None,
    collector_factory: Callable[..., object] = RSSCollector,
    notifier_factory: Callable[..., object] = FeishuNotifier,
    reader_factory: Callable[..., object] = JinaReader,
    summarizer_factory: Callable[..., object] = ResponsesSummarizer,
    clock: Callable[[], datetime] = _now_beijing,
) -> int:
    if mode not in {"dry-run", "send"}:
        raise ValueError(f"unsupported mode: {mode}")
    if mode == "send" and feishu_delivery is None:
        raise ValueError("Feishu delivery configuration is required in --send mode")
    title = f"{beijing_date(clock())} · SignalFeed"

    collector = collector_factory(config.source, config.network)
    collected = collector.collect()  # type: ignore[attr-defined]
    matched = KeywordFilter(config.filter.keywords, config.filter.fields).select(
        collected
    )
    storage = SQLiteStorage(database_path)
    unseen = storage.unseen(matched, read_only=(mode == "dry-run"))
    if not unseen:
        print("No new matching items.", file=output)
        return 0

    summaries: list[ChineseSummary | None] = [None] * len(unseen)
    missing: list[tuple[int, NewsItem]] = []
    for index, item in enumerate(unseen):
        summary = storage.cached_summary(
            item,
            model_config,
            PROMPT_VERSION,
            read_only=(mode == "dry-run"),
        )
        if summary is None:
            missing.append((index, item))
        else:
            summaries[index] = summary

    first_error: Exception | None = None
    if missing:
        reader = reader_factory(config.network.user_agent)
        pending = [
            (index, item, reader.read(item.url))  # type: ignore[attr-defined]
            for index, item in missing
        ]
        summarizer = summarizer_factory(model_config, api_key)
        results = asyncio.run(_generate_summaries(pending, summarizer))
        for result in results:
            if result.error is not None:
                if first_error is None:
                    first_error = result.error
                continue
            if result.summary is None:
                if first_error is None:
                    first_error = RuntimeError("summary generation returned no result")
                continue
            summaries[result.index] = result.summary
            if mode == "send":
                storage.cache_summary(
                    result.item, model_config, PROMPT_VERSION, result.summary
                )
    if first_error is not None:
        raise first_error

    localized = [
        summary.apply_to(item)
        for item, summary in zip(unseen, summaries, strict=True)
        if summary is not None
    ]
    if len(localized) != len(unseen):
        raise RuntimeError("summary generation returned incomplete results")

    digests = build_digests(
        localized,
        title=title,
        max_payload_bytes=config.feishu.max_payload_bytes,
    )
    if mode == "dry-run":
        for digest in digests:
            print(digest.encoded.decode("utf-8"), file=output)
        return 0

    notifier = notifier_factory(feishu_delivery, config.network.timeout_seconds)
    sent_items = 0
    for digest in digests:
        notifier.send(digest)  # type: ignore[attr-defined]
        storage.record_delivered(digest.items)
        sent_items += len(digest.items)
    print(
        f"Sent {sent_items} item(s) in {len(digests)} message(s) to Feishu.",
        file=output,
    )
    return 0


async def _generate_summaries(
    pending: list[tuple[int, NewsItem, str]], summarizer: object
) -> list[_GenerationResult]:
    """Generate summaries while capping in-flight model calls at 500."""

    model_semaphore = asyncio.Semaphore(MAX_MODEL_CONCURRENCY)

    async def generate(index: int, item: NewsItem, article: str) -> _GenerationResult:
        try:
            async with model_semaphore:
                summary = await summarizer.summarize(  # type: ignore[attr-defined]
                    item, article
                )
            return _GenerationResult(index, item, summary=summary)
        except (ReaderError, SummaryError) as exc:
            return _GenerationResult(index, item, error=exc)

    results = list(
        await asyncio.gather(
            *(generate(index, item, article) for index, item, article in pending)
        )
    )
    close = getattr(summarizer, "close", None)
    if callable(close):
        try:
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result
        except SummaryError as exc:
            results.append(_GenerationResult(len(results), pending[0][1], error=exc))
    return results
