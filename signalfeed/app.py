"""SignalFeed multi-source application orchestration."""

import asyncio
import hashlib
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .collector import (
    CollectionBatch,
    CollectionError,
    CollectionIssue,
    collect_source,
)
from .config import AppConfig, FeishuDeliveryConfig, ModelConfig, SourceConfig
from .datetime_utils import BEIJING_TIMEZONE, beijing_date
from .filter import KeywordFilter
from .model import ChineseSummary, NewsItem, canonicalize_url
from .notifier import (
    FeishuNotifier,
    build_digests,
    build_failure_digest,
)
from .reader import JinaReader
from .storage import (
    ARTICLE_RECOVERY_FAILURE_ITEM_KEY,
    BASELINE_FAILURE_ITEM_KEY,
    PRIORITY_RECOVERY_FAILURE_ITEM_KEY,
    RECONCILE_FAILURE_ITEM_KEY,
    SOURCE_FAILURE_ITEM_KEY,
    STATE_FAILURE_ITEM_KEY,
    UNSEEN_FAILURE_ITEM_KEY,
    SQLiteStorage,
)
from .summarizer import PROMPT_VERSION, ResponsesSummarizer

MAX_MODEL_CONCURRENCY = 500


def _now_beijing() -> datetime:
    return datetime.now(BEIJING_TIMEZONE)


@dataclass(frozen=True, slots=True)
class _Candidate:
    index: int
    source: SourceConfig
    item: NewsItem


@dataclass(frozen=True, slots=True)
class _GenerationResult:
    index: int
    item: NewsItem
    summary: ChineseSummary | None = None
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class _FailureEvent:
    source: str
    item_key: str
    article_title: str
    stage: str


@dataclass(slots=True)
class _RunStats:
    sent: int = 0
    previewed: int = 0
    failed: int = 0
    baseline: int = 0
    skipped: int = 0
    failure_events: set[tuple[str, str]] = field(default_factory=set)


@dataclass(slots=True)
class _LazyNotifier:
    factory: Callable[..., object]
    delivery: FeishuDeliveryConfig
    timeout_seconds: float
    instance: object | None = None
    initialization_error: Exception | None = None
    attempted: bool = False

    def send(self, digest: object) -> None:
        if not self.attempted:
            self.attempted = True
            try:
                self.instance = self.factory(self.delivery, self.timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - isolate notifier boundary
                self.initialization_error = exc
        if self.initialization_error is not None:
            raise self.initialization_error
        if self.instance is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("notifier initialization returned no instance")
        self.instance.send(digest)  # type: ignore[attr-defined]


def run(
    config: AppConfig,
    model_config: ModelConfig,
    *,
    mode: str,
    database_path: str | Path,
    output: TextIO,
    api_key: str,
    feishu_delivery: FeishuDeliveryConfig | None = None,
    collector_factory: Callable[..., object] | None = None,
    notifier_factory: Callable[..., object] = FeishuNotifier,
    reader_factory: Callable[..., object] = JinaReader,
    summarizer_factory: Callable[..., object] = ResponsesSummarizer,
    clock: Callable[[], datetime] = _now_beijing,
) -> int:
    """Run one ordered collection round.

    Source downloads and article delivery follow configuration order.  Model
    calls may overlap, but every successful summary, alert, and delivery is
    persisted independently so a bad article cannot hold back later articles.
    """

    if mode not in {"dry-run", "send"}:
        raise ValueError(f"unsupported mode: {mode}")
    if mode == "send" and feishu_delivery is None:
        raise ValueError("Feishu delivery configuration is required in --send mode")

    run_date = beijing_date(clock())
    storage = SQLiteStorage(database_path)
    notifier = None
    if mode == "send":
        # Construction is lazy: a baseline-only run neither needs nor touches
        # Feishu, while a broken client is isolated like any other send failure.
        assert feishu_delivery is not None
        notifier = _LazyNotifier(
            notifier_factory, feishu_delivery, config.network.timeout_seconds
        )
    stats = _RunStats()
    keyword_filter = KeywordFilter(config.filter.keywords, config.filter.fields)
    priority_keys: set[str] = set()
    candidates: list[_Candidate] = []

    for source in config.sources:
        try:
            batch = _collect(source, config, collector_factory)
        except Exception:  # noqa: BLE001 - isolate one source collector
            _report_failure(
                _FailureEvent(
                    source.name,
                    SOURCE_FAILURE_ITEM_KEY,
                    "（来源采集）",
                    "collection",
                ),
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue

        issue_events = tuple(_issue_event(source, issue) for issue in batch.issues)
        if mode == "send":
            # A successful source-level collection is the recovery boundary for
            # that source event.  Article events remain active until delivery.
            try:
                storage.clear_active_failure(source.name, SOURCE_FAILURE_ITEM_KEY)
                storage.reconcile_active_failure_namespace(
                    source.name,
                    "collection:",
                    {
                        event.item_key
                        for event in issue_events
                        if event.item_key.startswith("collection:")
                    },
                )
                storage.clear_active_failure(source.name, RECONCILE_FAILURE_ITEM_KEY)
            except Exception:  # noqa: BLE001 - isolate state reconciliation
                _report_failure(
                    _FailureEvent(
                        source.name,
                        RECONCILE_FAILURE_ITEM_KEY,
                        "（来源状态）",
                        "state",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )

        for event in issue_events:
            _report_failure(
                event,
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )

        source_items = _unique_source_items(batch.items, stats)
        try:
            initialized = storage.is_source_initialized(
                source.name, read_only=(mode == "dry-run")
            )
        except Exception:  # noqa: BLE001 - isolate one source state lookup
            _report_failure(
                _FailureEvent(
                    source.name,
                    STATE_FAILURE_ITEM_KEY,
                    "（来源状态）",
                    "state",
                ),
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            priority_keys.update(item.dedupe_key for item in source_items)
            continue
        if mode == "send":
            try:
                storage.clear_active_failure(source.name, STATE_FAILURE_ITEM_KEY)
            except Exception:  # noqa: BLE001 - isolate recovery accounting
                _report_failure(
                    _FailureEvent(
                        source.name,
                        STATE_FAILURE_ITEM_KEY,
                        "（来源状态）",
                        "state",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
            if initialized:
                # A baseline failure may have been persisted before source_state
                # committed.  Retry its recovery accounting on every later run
                # so a transient DELETE error cannot suppress future alerts.
                try:
                    storage.clear_active_failure(source.name, BASELINE_FAILURE_ITEM_KEY)
                except Exception:  # noqa: BLE001 - isolate recovery accounting
                    _report_failure(
                        _FailureEvent(
                            source.name,
                            BASELINE_FAILURE_ITEM_KEY,
                            "（首次基线）",
                            "baseline",
                        ),
                        mode=mode,
                        notifier=notifier,
                        storage=storage,
                        config=config,
                        run_date=run_date,
                        output=output,
                        stats=stats,
                    )
        if not initialized:
            if batch.issues:
                print(
                    f"Baseline deferred: {source.name}: "
                    f"{len(batch.issues)} collection issue(s)",
                    file=output,
                )
                priority_keys.update(item.dedupe_key for item in source_items)
                continue
            if mode == "dry-run":
                stats.baseline += len(source_items)
                _print_baseline_preview(source, source_items, output, dry_run=True)
                priority_keys.update(item.dedupe_key for item in source_items)
                continue
            try:
                created = storage.initialize_source_baseline(source.name, source_items)
            except Exception:  # noqa: BLE001 - isolate baseline transaction
                _report_failure(
                    _FailureEvent(
                        source.name,
                        BASELINE_FAILURE_ITEM_KEY,
                        "（首次基线）",
                        "baseline",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
                priority_keys.update(item.dedupe_key for item in source_items)
                continue
            try:
                storage.clear_active_failure(source.name, BASELINE_FAILURE_ITEM_KEY)
            except Exception:  # noqa: BLE001 - isolate recovery accounting
                _report_failure(
                    _FailureEvent(
                        source.name,
                        BASELINE_FAILURE_ITEM_KEY,
                        "（首次基线）",
                        "baseline",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
            if created:
                stats.baseline += len(source_items)
                _print_baseline_preview(source, source_items, output, dry_run=False)
                priority_keys.update(item.dedupe_key for item in source_items)
                continue

        prioritized: list[NewsItem] = []
        priority_skipped: list[NewsItem] = []
        for item in source_items:
            if item.dedupe_key in priority_keys:
                stats.skipped += 1
                priority_skipped.append(item)
                continue
            priority_keys.add(item.dedupe_key)
            prioritized.append(item)

        try:
            unseen = storage.unseen(prioritized, read_only=(mode == "dry-run"))
        except Exception:  # noqa: BLE001 - isolate one source state query
            _report_failure(
                _FailureEvent(
                    source.name,
                    UNSEEN_FAILURE_ITEM_KEY,
                    "（来源状态）",
                    "state",
                ),
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue
        persisted_priority_keys: frozenset[str] = frozenset()
        if mode == "send":
            try:
                persisted_priority_keys = storage.seen_dedupe_keys(priority_skipped)
                storage.clear_active_failure(
                    source.name, PRIORITY_RECOVERY_FAILURE_ITEM_KEY
                )
            except Exception:  # noqa: BLE001 - optional recovery reconciliation
                _report_failure(
                    _FailureEvent(
                        source.name,
                        PRIORITY_RECOVERY_FAILURE_ITEM_KEY,
                        "（跨来源恢复状态）",
                        "state",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
        if mode == "send":
            try:
                storage.clear_active_failure(source.name, UNSEEN_FAILURE_ITEM_KEY)
            except Exception:  # noqa: BLE001 - isolate recovery accounting
                _report_failure(
                    _FailureEvent(
                        source.name,
                        UNSEEN_FAILURE_ITEM_KEY,
                        "（来源状态）",
                        "state",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
        stats.skipped += len(prioritized) - len(unseen)
        unseen_keys = {item.dedupe_key for item in unseen}
        recovered_without_pipeline = {
            item.dedupe_key
            for item in prioritized
            if item.dedupe_key not in unseen_keys
        }
        recovered_without_pipeline.update(persisted_priority_keys)
        selected = keyword_filter.select(unseen) if source.filter else unseen
        stats.skipped += len(unseen) - len(selected)
        if mode == "send":
            # A previously malformed article may now parse successfully yet be
            # omitted by unseen() because it was already delivered or baselined.
            # That is a recovery only when the same key is not still an issue in
            # the current batch. Filtering and in-memory priority alone are not
            # recovery boundaries because neither proves successful delivery.
            current_issue_keys = {event.item_key for event in issue_events}
            recovered_without_pipeline.difference_update(current_issue_keys)
            try:
                storage.clear_active_failure_keys(
                    source.name, recovered_without_pipeline
                )
                storage.clear_active_failure(
                    source.name, ARTICLE_RECOVERY_FAILURE_ITEM_KEY
                )
            except Exception:  # noqa: BLE001 - isolate recovery accounting
                _report_failure(
                    _FailureEvent(
                        source.name,
                        ARTICLE_RECOVERY_FAILURE_ITEM_KEY,
                        "（文章恢复状态）",
                        "state",
                    ),
                    mode=mode,
                    notifier=notifier,
                    storage=storage,
                    config=config,
                    run_date=run_date,
                    output=output,
                    stats=stats,
                )
        candidates.extend(
            _Candidate(len(candidates), source, item) for item in selected
        )

    summaries, article_failures = _prepare_summaries(
        candidates,
        config=config,
        model_config=model_config,
        api_key=api_key,
        mode=mode,
        storage=storage,
        reader_factory=reader_factory,
        summarizer_factory=summarizer_factory,
    )

    for candidate in candidates:
        for stage in article_failures.get(candidate.index, ()):
            _report_item_failure(
                candidate,
                stage,
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
        summary = summaries.get(candidate.index)
        if summary is None:
            continue
        try:
            localized = summary.apply_to(candidate.item)
            (digest,) = build_digests(
                [localized],
                title=f"{run_date} · SignalFeed · {candidate.source.name}",
                max_payload_bytes=config.feishu.max_payload_bytes,
            )
        except Exception:  # noqa: BLE001 - isolate message construction
            _report_item_failure(
                candidate,
                "message",
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue

        if mode == "dry-run":
            print(digest.encoded.decode("utf-8"), file=output)
            stats.previewed += 1
            continue

        try:
            notifier.send(digest)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - isolate remote delivery
            _report_item_failure(
                candidate,
                "send",
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue

        stats.sent += 1
        try:
            storage.record_delivered([candidate.item])
        except Exception:  # noqa: BLE001 - isolate delivery accounting
            # The remote delivery succeeded, but without durable accounting the
            # round must still fail.  A compact alert is attempted just like any
            # other article failure; it is deliberately not retried recursively.
            _report_item_failure(
                candidate,
                "record",
                mode=mode,
                notifier=notifier,
                storage=storage,
                config=config,
                run_date=run_date,
                output=output,
                stats=stats,
            )
            continue

    if not candidates and not stats.failed and not stats.baseline:
        print("No new matching items.", file=output)
    print(
        "Summary: "
        f"sent={stats.sent} failed={stats.failed} baseline={stats.baseline} "
        f"skipped={stats.skipped} previewed={stats.previewed}",
        file=output,
    )
    return 1 if stats.failed else 0


def _collect(
    source: SourceConfig,
    config: AppConfig,
    collector_factory: Callable[..., object] | None,
) -> CollectionBatch:
    if collector_factory is None:
        batch = collect_source(source, config.network)
    else:
        collector_or_result = collector_factory(source, config.network)
        result = (
            collector_or_result.collect()  # type: ignore[attr-defined]
            if hasattr(collector_or_result, "collect")
            else collector_or_result
        )
        if isinstance(result, CollectionBatch):
            batch = result
        elif isinstance(result, Sequence) and not isinstance(result, str | bytes):
            batch = CollectionBatch(items=tuple(result), issues=())
        else:
            raise CollectionError("collector returned an unsupported result")
    if any(not isinstance(item, NewsItem) for item in batch.items):
        raise CollectionError("collector returned a non-NewsItem entry")
    if any(item.source != source.name for item in batch.items):
        raise CollectionError("collector returned an entry for the wrong source")
    if any(not isinstance(issue, CollectionIssue) for issue in batch.issues):
        raise CollectionError("collector returned an invalid CollectionIssue")
    return batch


def _unique_source_items(items: Sequence[NewsItem], stats: _RunStats) -> list[NewsItem]:
    result: list[NewsItem] = []
    seen: set[str] = set()
    for item in items:
        if item.dedupe_key in seen:
            stats.skipped += 1
            continue
        seen.add(item.dedupe_key)
        result.append(item)
    return result


def _print_baseline_preview(
    source: SourceConfig,
    items: Sequence[NewsItem],
    output: TextIO,
    *,
    dry_run: bool,
) -> None:
    label = "Baseline preview" if dry_run else "Baseline created"
    print(f"{label}: {source.name}: {len(items)} item(s)", file=output)
    if dry_run:
        for item in items:
            date = item.published_at or "日期未知"
            print(f"  - {date} · {item.title}", file=output)


def _prepare_summaries(
    candidates: Sequence[_Candidate],
    *,
    config: AppConfig,
    model_config: ModelConfig,
    api_key: str,
    mode: str,
    storage: SQLiteStorage,
    reader_factory: Callable[..., object],
    summarizer_factory: Callable[..., object],
) -> tuple[dict[int, ChineseSummary], dict[int, tuple[str, ...]]]:
    summaries: dict[int, ChineseSummary] = {}
    failures: dict[int, set[str]] = {}
    pending: list[tuple[int, NewsItem, str]] = []
    reader: object | None = None

    def fail(index: int, stage: str) -> None:
        failures.setdefault(index, set()).add(stage)

    for candidate in candidates:
        try:
            cached = storage.cached_summary(
                candidate.item,
                model_config,
                PROMPT_VERSION,
                read_only=(mode == "dry-run"),
            )
        except Exception:  # noqa: BLE001 - cache failure must not abort the round
            cached = None
            fail(candidate.index, "cache")
        if cached is not None:
            summaries[candidate.index] = cached
            continue

        try:
            if candidate.source.content_mode == "inline":
                article = candidate.item.content.strip()
                if not article:
                    raise ValueError("inline article content is empty")
            else:
                if reader is None:
                    reader = reader_factory(config.network.user_agent)
                article = _read_article(
                    reader, candidate.item.url, candidate.source.allowed_hosts
                )
                if not isinstance(article, str) or not article.strip():
                    raise ValueError("article reader returned empty content")
        except Exception:  # noqa: BLE001 - isolate article extraction
            fail(candidate.index, "content")
            continue
        pending.append((candidate.index, candidate.item, article))

    if not pending:
        return summaries, _ordered_failure_stages(failures)

    try:
        summarizer = summarizer_factory(model_config, api_key)
    except Exception:  # noqa: BLE001 - isolate summarizer construction
        for index, _item, _article in pending:
            fail(index, "summary")
        return summaries, _ordered_failure_stages(failures)

    cache_errors: set[int] = set()

    def cache_completed(result: _GenerationResult) -> None:
        if mode != "send" or result.summary is None:
            return
        try:
            storage.cache_summary(
                result.item, model_config, PROMPT_VERSION, result.summary
            )
        except Exception:  # noqa: BLE001 - isolate summary cache writes
            cache_errors.add(result.index)

    results, _close_error = asyncio.run(
        _generate_summaries(pending, summarizer, cache_completed)
    )
    for result in results:
        candidate = candidates[result.index]
        if result.error is not None or result.summary is None:
            fail(candidate.index, "summary")
            continue
        summaries[result.index] = result.summary
        if result.index in cache_errors:
            fail(candidate.index, "cache")

    return summaries, _ordered_failure_stages(failures)


def _ordered_failure_stages(
    failures: dict[int, set[str]],
) -> dict[int, tuple[str, ...]]:
    order = {"cache": 0, "content": 1, "summary": 2}
    return {
        index: tuple(sorted(stages, key=lambda stage: order.get(stage, 99)))
        for index, stages in failures.items()
    }


def _read_article(reader: object, url: str, allowed_hosts: tuple[str, ...]) -> str:
    return reader.read(  # type: ignore[attr-defined,no-any-return]
        url, allowed_hosts=allowed_hosts
    )


async def _generate_summaries(
    pending: list[tuple[int, NewsItem, str]],
    summarizer: object,
    on_complete: Callable[[_GenerationResult], None] | None = None,
) -> tuple[list[_GenerationResult], Exception | None]:
    """Generate summaries concurrently while retaining candidate order."""

    model_semaphore = asyncio.Semaphore(MAX_MODEL_CONCURRENCY)

    async def generate(index: int, item: NewsItem, article: str) -> _GenerationResult:
        try:
            async with model_semaphore:
                summary = await summarizer.summarize(  # type: ignore[attr-defined]
                    item, article
                )
            if not isinstance(summary, ChineseSummary):
                raise TypeError("summarizer returned an unsupported result")
            result = _GenerationResult(index, item, summary=summary)
            if on_complete is not None:
                on_complete(result)
            return result
        except Exception as exc:  # noqa: BLE001 - isolate one model task
            return _GenerationResult(index, item, error=exc)

    results = list(
        await asyncio.gather(
            *(generate(index, item, article) for index, item, article in pending)
        )
    )
    close_error: Exception | None = None
    close = getattr(summarizer, "close", None)
    if callable(close):
        try:
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result
        except Exception as exc:  # noqa: BLE001 - client cleanup is best effort
            close_error = exc
    return results, close_error


def _report_item_failure(
    candidate: _Candidate,
    stage: str,
    **kwargs: object,
) -> None:
    _report_failure(
        _FailureEvent(
            candidate.source.name,
            candidate.item.dedupe_key,
            candidate.item.title,
            stage,
        ),
        **kwargs,  # type: ignore[arg-type]
    )


def _issue_event(source: SourceConfig, issue: CollectionIssue) -> _FailureEvent:
    title = issue.title or f"（列表条目 {issue.index or '?'}）"
    issue_url = canonicalize_url(issue.url) if issue.url else ""
    if issue_url and issue_url != canonicalize_url(source.url):
        # A recognizable article keeps one identity while it moves from parse
        # to content, summary, message, or delivery failure.
        item_key = issue_url
    else:
        stable_label = " ".join(issue.title.split()).casefold()
        if not stable_label:
            stable_label = f"index:{issue.index or '?'}"
        identity = f"{source.name}\0{canonicalize_url(source.url)}\0{stable_label}"
        item_key = "collection:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return _FailureEvent(
        source.name,
        item_key,
        title,
        issue.stage or "collection",
    )


def _report_failure(
    event: _FailureEvent,
    *,
    mode: str,
    notifier: object | None,
    storage: SQLiteStorage,
    config: AppConfig,
    run_date: str,
    output: TextIO,
    stats: _RunStats,
) -> None:
    identity = (event.source, event.item_key)
    if identity in stats.failure_events:
        return
    stats.failure_events.add(identity)
    stats.failed += 1
    print(
        f"Failure: source={event.source} article={event.article_title} "
        f"stage={event.stage}",
        file=output,
    )
    if mode != "send" or notifier is None:
        return
    try:
        already_active = storage.has_active_failure(event.source, event.item_key)
    except Exception:  # noqa: BLE001 - alert state must not abort the round
        already_active = False
    if already_active:
        return
    try:
        digest = build_failure_digest(
            title=f"{run_date} · SignalFeed · 告警",
            source=event.source,
            article_title=event.article_title,
            stage=event.stage,
            max_payload_bytes=config.feishu.max_payload_bytes,
        )
        notifier.send(digest)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - alerts are best effort
        return
    try:
        storage.record_active_failure(event.source, event.item_key)
    except Exception:  # noqa: BLE001 - alert accounting is best effort
        # The alert was delivered but cannot be durably deduplicated.  Retrying
        # next run is safer than aborting unrelated articles.
        return
