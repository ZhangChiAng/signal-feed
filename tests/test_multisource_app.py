import asyncio
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

from signalfeed.app import run
from signalfeed.collector import CollectionBatch, CollectionError, CollectionIssue
from signalfeed.config import (
    AppConfig,
    FeishuConfig,
    FeishuDeliveryConfig,
    FilterConfig,
    ModelConfig,
    NetworkConfig,
    SourceConfig,
)
from signalfeed.model import ChineseSummary, NewsItem
from signalfeed.notifier import NotificationError
from signalfeed.reader import ReaderError
from signalfeed.storage import SOURCE_FAILURE_ITEM_KEY, SQLiteStorage
from signalfeed.summarizer import PROMPT_VERSION, SummaryError

MODEL_CONFIG = ModelConfig(
    "test-model",
    "openai_responses",
    "https://api.example.com/v1",
    "SIGNALFEED_LLM_API_KEY",
)
DELIVERY = FeishuDeliveryConfig("cli_test", "secret", "chat_id", "oc_test")
RUN_DATE = "2026-08-12"


def source_config(
    name: str,
    *,
    content_mode: str = "article",
    apply_filter: bool = False,
    max_age_days: int | None = None,
) -> SourceConfig:
    slug = name.lower().replace(" ", "-")
    return SourceConfig(
        name=name,
        url=f"https://official.example/{slug}",
        collector="rss",
        transport="direct",
        content_mode=content_mode,
        window_size=20,
        allowed_hosts=("official.example",),
        filter=apply_filter,
        max_age_days=max_age_days,
    )


def app_config(*sources: SourceConfig, max_payload_bytes: int = 28 * 1024) -> AppConfig:
    return AppConfig(
        tuple(sources),
        NetworkConfig(15, 1024 * 1024, "SignalFeed/Test"),
        FilterConfig(("title", "content"), ("GPT",)),
        FeishuConfig(max_payload_bytes),
    )


def item(
    source: SourceConfig,
    slug: str,
    *,
    title: str | None = None,
    url: str | None = None,
    content: str = "Inline official release details.",
    dedupe_key: str = "",
    published_at: str = "2026-08-10T12:00:00+08:00",
) -> NewsItem:
    target = url or f"https://official.example/articles/{slug}"
    return NewsItem(
        source=source.name,
        item_id=f"{source.name}:{slug}",
        title=title or f"GPT release {slug}",
        content=content,
        url=target,
        published_at=published_at,
        author=source.name,
        category="Release",
        guid=f"{source.name}:{slug}",
        dedupe_key=dedupe_key,
    )


def summary_for(news_item: NewsItem) -> ChineseSummary:
    return ChineseSummary(
        f"中文标题 {news_item.item_id}",
        (
            f"第一条中文要点对应 {news_item.item_id}。",
            "第二条中文要点保留完整条件。",
            "第三条中文要点保留完整数字。",
        ),
    )


def collector_factory_for(
    outcomes: dict[
        str,
        CollectionBatch | BaseException | Callable[[], CollectionBatch | BaseException],
    ],
    constructed: list[str] | None = None,
) -> Callable[..., object]:
    class FakeCollector:
        def __init__(self, source: SourceConfig) -> None:
            self.source = source

        def collect(self) -> CollectionBatch:
            outcome = outcomes[self.source.name]
            if callable(outcome):
                outcome = outcome()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    def factory(source: SourceConfig, network: NetworkConfig) -> object:
        if constructed is not None:
            constructed.append(source.name)
        return FakeCollector(source)

    return factory


def reader_factory_for(
    *,
    failures: set[str] | None = None,
    calls: list[tuple[str, tuple[str, ...]]] | None = None,
) -> Callable[..., object]:
    failed_urls = set() if failures is None else failures

    class FakeReader:
        def read(self, url: str, allowed_hosts: tuple[str, ...] | None = None) -> str:
            if calls is not None:
                calls.append((url, tuple(allowed_hosts or ())))
            if url in failed_urls:
                raise ReaderError("reader failed")
            return f"Full article body for {url}"

    def factory(user_agent: str) -> object:
        return FakeReader()

    return factory


def summarizer_factory_for(
    *,
    failures: set[str] | None = None,
    oversized: set[str] | None = None,
    calls: list[str] | None = None,
) -> Callable[..., object]:
    failed_ids = set() if failures is None else failures
    oversized_ids = set() if oversized is None else oversized

    class FakeSummarizer:
        async def summarize(self, news_item: NewsItem, article: str) -> ChineseSummary:
            if calls is not None:
                calls.append(news_item.item_id)
            if news_item.item_id in failed_ids:
                raise SummaryError("summary failed")
            if news_item.item_id in oversized_ids:
                return ChineseSummary(
                    f"超大中文标题 {news_item.item_id}",
                    ("超大但不可截断的完整要点。" * 1000, "第二条。", "第三条。"),
                )
            return summary_for(news_item)

        async def close(self) -> None:
            return None

    def factory(model: ModelConfig, api_key: str) -> object:
        return FakeSummarizer()

    return factory


def notifier_factory_for(
    calls: list[Any],
    *,
    fail_item_ids: set[str] | None = None,
    fail_alerts: bool = False,
    before_send: Callable[[Any], None] | None = None,
) -> Callable[..., object]:
    failed_ids = set() if fail_item_ids is None else fail_item_ids

    class FakeNotifier:
        def send(self, digest: Any) -> None:
            if before_send is not None:
                before_send(digest)
            calls.append(digest)
            if not digest.items and fail_alerts:
                raise NotificationError("alert rejected")
            if digest.items and digest.items[0].item_id in failed_ids:
                raise NotificationError("content rejected")

    def factory(delivery: FeishuDeliveryConfig, timeout: float) -> object:
        return FakeNotifier()

    return factory


def initialize_sources(path: Path, sources: tuple[SourceConfig, ...]) -> None:
    storage = SQLiteStorage(path)
    for source in sources:
        storage.initialize_source_baseline(source.name, ())


def assert_summary_counts(
    case: unittest.TestCase,
    output: str,
    *,
    sent: int,
    failed: int,
    baseline: int,
    skipped: int,
) -> None:
    labels = {
        "sent": (sent, r"(?:sent|发送)"),
        "failed": (failed, r"(?:failed|failures?|失败)"),
        "baseline": (baseline, r"(?:baselined?|基线)"),
        "skipped": (skipped, r"(?:skipped?|跳过)"),
    }
    for name, (count, label) in labels.items():
        with case.subTest(summary_field=name):
            case.assertRegex(output.lower(), rf"{label}[^\d]*{count}\b")


class MultiSourceAppTests(unittest.TestCase):
    def run_app(
        self,
        config: AppConfig,
        path: Path,
        outcomes: dict[
            str,
            CollectionBatch
            | BaseException
            | Callable[[], CollectionBatch | BaseException],
        ],
        *,
        mode: str = "send",
        output: StringIO | None = None,
        collector_factory: Callable[..., object] | None = None,
        reader_factory: Callable[..., object] | None = None,
        summarizer_factory: Callable[..., object] | None = None,
        notifier_factory: Callable[..., object] | None = None,
    ) -> tuple[int, StringIO]:
        destination = StringIO() if output is None else output
        result = run(
            config,
            MODEL_CONFIG,
            mode=mode,
            database_path=path,
            output=destination,
            api_key="test-key",
            feishu_delivery=DELIVERY if mode == "send" else None,
            collector_factory=(
                collector_factory_for(outcomes)
                if collector_factory is None
                else collector_factory
            ),
            reader_factory=(
                reader_factory_for() if reader_factory is None else reader_factory
            ),
            summarizer_factory=(
                summarizer_factory_for()
                if summarizer_factory is None
                else summarizer_factory
            ),
            notifier_factory=(
                notifier_factory_for([])
                if notifier_factory is None
                else notifier_factory
            ),
            clock=lambda: datetime(2026, 8, 11, 16, 30, tzinfo=UTC),
        )
        return result, destination

    def test_empty_database_send_atomically_baselines_every_source_without_sending(
        self,
    ) -> None:
        alpha = source_config("Alpha News", apply_filter=True)
        beta = source_config("Beta Engineering")
        filtered_out = item(alpha, "unmatched", title="Unrelated company update")
        alpha_match = item(alpha, "matched")
        beta_item = item(beta, "engineering")
        outcomes = {
            alpha.name: CollectionBatch((filtered_out, alpha_match)),
            beta.name: CollectionBatch((beta_item,)),
        }
        notifier_calls: list[Any] = []
        reader_calls: list[tuple[str, tuple[str, ...]]] = []
        summary_calls: list[str] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            result, output = self.run_app(
                app_config(alpha, beta),
                path,
                outcomes,
                reader_factory=reader_factory_for(calls=reader_calls),
                summarizer_factory=summarizer_factory_for(calls=summary_calls),
                notifier_factory=notifier_factory_for(notifier_calls),
            )

            storage = SQLiteStorage(path)
            self.assertEqual(result, 0)
            self.assertTrue(storage.is_source_initialized(alpha.name, read_only=True))
            self.assertTrue(storage.is_source_initialized(beta.name, read_only=True))
            self.assertEqual(
                storage.baseline_items(alpha.name, read_only=True),
                frozenset({filtered_out.dedupe_key, alpha_match.dedupe_key}),
            )
            self.assertEqual(
                storage.baseline_items(beta.name, read_only=True),
                frozenset({beta_item.dedupe_key}),
            )

        self.assertEqual(notifier_calls, [])
        self.assertEqual(reader_calls, [])
        self.assertEqual(summary_calls, [])
        assert_summary_counts(
            self,
            output.getvalue(),
            sent=0,
            failed=0,
            baseline=3,
            skipped=0,
        )

    def test_dry_run_previews_each_new_source_baseline_with_zero_database_writes(
        self,
    ) -> None:
        alpha = source_config("Alpha News")
        beta = source_config("Beta Engineering")
        outcomes = {
            alpha.name: CollectionBatch((item(alpha, "one"),)),
            beta.name: CollectionBatch((item(beta, "two"),)),
        }

        def forbidden_factory(*args: object) -> object:
            raise AssertionError("baseline preview must not process or notify articles")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.sqlite3"
            result, output = self.run_app(
                app_config(alpha, beta),
                path,
                outcomes,
                mode="dry-run",
                reader_factory=forbidden_factory,
                summarizer_factory=forbidden_factory,
                notifier_factory=forbidden_factory,
            )
            self.assertEqual(result, 0)
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

        rendered = output.getvalue()
        self.assertIn(alpha.name, rendered)
        self.assertIn(beta.name, rendered)
        assert_summary_counts(self, rendered, sent=0, failed=0, baseline=2, skipped=0)

    def test_sources_and_items_send_in_order_with_cross_source_url_deduplication(
        self,
    ) -> None:
        alpha = source_config("Alpha Engineering")
        beta = source_config("Beta Research")
        shared_alpha = item(
            alpha,
            "shared-alpha",
            url="https://official.example/articles/shared?utm_source=alpha#top",
        )
        alpha_second = item(alpha, "alpha-second")
        shared_beta = item(
            beta,
            "shared-beta",
            url="https://official.example/articles/shared#details",
        )
        beta_last = item(beta, "beta-last")
        outcomes = {
            alpha.name: CollectionBatch((shared_alpha, alpha_second)),
            beta.name: CollectionBatch((shared_beta, beta_last)),
        }
        constructed: list[str] = []
        summary_calls: list[str] = []
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(alpha, beta)
            initialize_sources(path, config.sources)
            storage = SQLiteStorage(path)

            def assert_immediate_state(digest: Any) -> None:
                if not digest.items:
                    return
                current = digest.items[0]
                self.assertIsNotNone(
                    storage.cached_summary(
                        current, MODEL_CONFIG, PROMPT_VERSION, read_only=True
                    )
                )
                if current.item_id == alpha_second.item_id:
                    self.assertTrue(storage.is_delivered(shared_alpha, read_only=True))
                if current.item_id == beta_last.item_id:
                    self.assertTrue(storage.is_delivered(alpha_second, read_only=True))

            result, output = self.run_app(
                config,
                path,
                outcomes,
                collector_factory=collector_factory_for(outcomes, constructed),
                summarizer_factory=summarizer_factory_for(calls=summary_calls),
                notifier_factory=notifier_factory_for(
                    notifier_calls, before_send=assert_immediate_state
                ),
            )

            self.assertEqual(result, 0)
            self.assertTrue(storage.is_delivered(beta_last, read_only=True))

        content_messages = [call for call in notifier_calls if call.items]
        self.assertEqual(constructed, [alpha.name, beta.name])
        self.assertEqual(
            [message.items[0].item_id for message in content_messages],
            [shared_alpha.item_id, alpha_second.item_id, beta_last.item_id],
        )
        self.assertTrue(all(len(message.items) == 1 for message in content_messages))
        self.assertEqual(
            summary_calls,
            [shared_alpha.item_id, alpha_second.item_id, beta_last.item_id],
        )
        self.assertEqual(
            [
                message.payload["content"]["zh_cn"]["title"]
                for message in content_messages
            ],
            [
                f"{RUN_DATE} · SignalFeed · {alpha.name}",
                f"{RUN_DATE} · SignalFeed · {alpha.name}",
                f"{RUN_DATE} · SignalFeed · {beta.name}",
            ],
        )
        assert_summary_counts(
            self, output.getvalue(), sent=3, failed=0, baseline=0, skipped=1
        )

    def test_article_stage_failures_do_not_block_later_items_and_return_one(
        self,
    ) -> None:
        source = source_config("Alpha Engineering")
        read_failure = item(source, "read-failure", title="Read Failure")
        summary_failure = item(source, "summary-failure", title="Summary Failure")
        oversized = item(source, "oversized", title="Oversized Message")
        send_failure = item(source, "send-failure", title="Send Failure")
        success = item(source, "success", title="Later Success")
        news_items = (
            read_failure,
            summary_failure,
            oversized,
            send_failure,
            success,
        )
        outcomes = {source.name: CollectionBatch(news_items)}
        reader_calls: list[tuple[str, tuple[str, ...]]] = []
        summary_calls: list[str] = []
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source, max_payload_bytes=4 * 1024)
            initialize_sources(path, config.sources)
            result, output = self.run_app(
                config,
                path,
                outcomes,
                reader_factory=reader_factory_for(
                    failures={read_failure.url}, calls=reader_calls
                ),
                summarizer_factory=summarizer_factory_for(
                    failures={summary_failure.item_id},
                    oversized={oversized.item_id},
                    calls=summary_calls,
                ),
                notifier_factory=notifier_factory_for(
                    notifier_calls, fail_item_ids={send_failure.item_id}
                ),
            )

            storage = SQLiteStorage(path)
            self.assertEqual(result, 1)
            self.assertTrue(storage.is_delivered(success, read_only=True))
            for failed in news_items[:-1]:
                self.assertFalse(storage.is_delivered(failed, read_only=True))
            for summarized in (oversized, send_failure, success):
                self.assertIsNotNone(
                    storage.cached_summary(
                        summarized, MODEL_CONFIG, PROMPT_VERSION, read_only=True
                    )
                )
            for unsummarized in (read_failure, summary_failure):
                self.assertIsNone(
                    storage.cached_summary(
                        unsummarized, MODEL_CONFIG, PROMPT_VERSION, read_only=True
                    )
                )

        content_attempts = [call for call in notifier_calls if call.items]
        alerts = [call for call in notifier_calls if not call.items]
        self.assertEqual(
            [call.items[0].item_id for call in content_attempts],
            [send_failure.item_id, success.item_id],
        )
        self.assertEqual(len(alerts), 4)
        alert_text = "\n".join(call.encoded.decode("utf-8") for call in alerts)
        for failed in news_items[:-1]:
            self.assertIn(failed.title, alert_text)
        self.assertEqual(
            summary_calls,
            [
                summary_failure.item_id,
                oversized.item_id,
                send_failure.item_id,
                success.item_id,
            ],
        )
        self.assertTrue(
            all(allowed == source.allowed_hosts for _, allowed in reader_calls)
        )
        assert_summary_counts(
            self, output.getvalue(), sent=1, failed=4, baseline=0, skipped=0
        )

    def test_same_article_failure_alerts_once_even_when_its_stage_changes(self) -> None:
        source = source_config("Alpha Engineering")
        failed_item = item(source, "flaky", title="Flaky Article")
        outcomes = {source.name: CollectionBatch((failed_item,))}
        phase = 0
        notifier_calls: list[Any] = []

        class StageChangingReader:
            def read(
                self, url: str, allowed_hosts: tuple[str, ...] | None = None
            ) -> str:
                if phase == 0:
                    raise ReaderError("first failure")
                return "Recovered article body"

        class StageChangingSummarizer:
            async def summarize(
                self, news_item: NewsItem, article: str
            ) -> ChineseSummary:
                raise SummaryError("second-stage failure")

            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            initialize_sources(path, config.sources)
            storage = SQLiteStorage(path)

            for current_phase in (0, 1):
                phase = current_phase
                result, _ = self.run_app(
                    config,
                    path,
                    outcomes,
                    reader_factory=lambda user_agent: StageChangingReader(),
                    summarizer_factory=lambda model, api_key: StageChangingSummarizer(),
                    notifier_factory=notifier_factory_for(notifier_calls),
                )
                self.assertEqual(result, 1)

            self.assertTrue(
                storage.has_active_failure(
                    source.name, failed_item.dedupe_key, read_only=True
                )
            )

        alerts = [call for call in notifier_calls if not call.items]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Flaky Article", alerts[0].encoded.decode("utf-8"))

    def test_known_url_issue_defers_new_source_baseline_then_recovery_rearms_alert(
        self,
    ) -> None:
        source = source_config("Alpha Engineering")
        known_url = "https://official.example/articles/known"
        recovered_item = item(source, "known", title="Known Article", url=known_url)
        issue = CollectionIssue(
            source=source.name,
            stage="date parsing",
            title="Known Article",
            url=f"{known_url}?utm_source=index#card",
            message="invalid date",
            index=1,
        )
        broken_batch = CollectionBatch((), (issue,))
        current = broken_batch
        outcomes = {source.name: lambda: current}
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            storage = SQLiteStorage(path)

            first_result, first_output = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(first_result, 1)
            self.assertIn("Baseline deferred", first_output.getvalue())
            self.assertFalse(storage.is_source_initialized(source.name, read_only=True))
            self.assertTrue(
                storage.has_active_failure(
                    source.name, recovered_item.dedupe_key, read_only=True
                )
            )

            current = CollectionBatch((recovered_item,))
            recovered_result, recovered_output = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(recovered_result, 0)
            self.assertIn("Baseline created", recovered_output.getvalue())
            self.assertTrue(storage.is_source_initialized(source.name, read_only=True))
            self.assertEqual(
                storage.baseline_items(source.name, read_only=True),
                frozenset({recovered_item.dedupe_key}),
            )
            self.assertFalse(
                storage.has_active_failure(
                    source.name, recovered_item.dedupe_key, read_only=True
                )
            )

            current = broken_batch
            failed_again, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(failed_again, 1)
            self.assertTrue(
                storage.has_active_failure(
                    source.name, recovered_item.dedupe_key, read_only=True
                )
            )

        alerts = [call for call in notifier_calls if not call.items]
        self.assertEqual(len(alerts), 2)
        self.assertTrue(
            all("Known Article" in call.encoded.decode() for call in alerts)
        )

    def test_known_url_collection_issue_to_content_failure_does_not_alert_twice(
        self,
    ) -> None:
        source = source_config("Alpha Engineering")
        known_url = "https://official.example/articles/known"
        known_item = item(source, "known", title="Known Article", url=known_url)
        issue = CollectionIssue(
            source=source.name,
            stage="entry parsing",
            title=known_item.title,
            url=f"{known_url}?utm_campaign=release#details",
            message="malformed entry",
            index=1,
        )
        current = CollectionBatch((), (issue,))
        outcomes = {source.name: lambda: current}
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            initialize_sources(path, config.sources)
            storage = SQLiteStorage(path)

            issue_result, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(issue_result, 1)
            self.assertTrue(
                storage.has_active_failure(
                    source.name, known_item.dedupe_key, read_only=True
                )
            )

            current = CollectionBatch((known_item,))
            content_result, _ = self.run_app(
                config,
                path,
                outcomes,
                reader_factory=reader_factory_for(failures={known_item.url}),
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(content_result, 1)
            self.assertTrue(
                storage.has_active_failure(
                    source.name, known_item.dedupe_key, read_only=True
                )
            )

        alerts = [call for call in notifier_calls if not call.items]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Known Article", alerts[0].encoded.decode())

    def test_delivered_article_parse_recovery_rearms_a_later_issue_alert(
        self,
    ) -> None:
        source = source_config("Alpha Engineering")
        known_item = item(source, "known", title="Known Article")
        issue = CollectionIssue(
            source=source.name,
            stage="entry parsing",
            title=known_item.title,
            url=f"{known_item.url}?utm_source=index#card",
            message="malformed entry",
            index=1,
        )
        current = CollectionBatch((), (issue,))
        outcomes = {source.name: lambda: current}
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            initialize_sources(path, config.sources)
            storage = SQLiteStorage(path)
            storage.record_delivered([known_item])

            first_failure, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(first_failure, 1)
            self.assertTrue(
                storage.has_active_failure(
                    source.name, known_item.dedupe_key, read_only=True
                )
            )

            current = CollectionBatch((known_item,))
            recovered, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(recovered, 0)
            self.assertFalse(
                storage.has_active_failure(
                    source.name, known_item.dedupe_key, read_only=True
                )
            )

            current = CollectionBatch((), (issue,))
            failed_again, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(failed_again, 1)

        alerts = [call for call in notifier_calls if not call.items]
        self.assertEqual(len(alerts), 2)

    def test_persisted_priority_duplicate_recovery_rearms_lower_source_alert(
        self,
    ) -> None:
        higher = source_config("Higher News")
        lower = source_config("Lower News")
        shared_url = "https://official.example/articles/shared"
        higher_item = item(higher, "shared", url=shared_url)
        lower_item = item(lower, "shared", title="Shared Article", url=shared_url)
        issue = CollectionIssue(
            source=lower.name,
            stage="entry parsing",
            title=lower_item.title,
            url=f"{shared_url}?utm_source=lower#card",
            message="malformed entry",
            index=1,
        )
        higher_batch = CollectionBatch(())
        lower_batch = CollectionBatch((), (issue,))
        outcomes = {
            higher.name: lambda: higher_batch,
            lower.name: lambda: lower_batch,
        }
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(higher, lower)
            initialize_sources(path, config.sources)
            storage = SQLiteStorage(path)
            storage.record_delivered([higher_item])

            failed, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(failed, 1)

            higher_batch = CollectionBatch((higher_item,))
            lower_batch = CollectionBatch((lower_item,))
            recovered, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(recovered, 0)
            self.assertFalse(
                storage.has_active_failure(
                    lower.name, lower_item.dedupe_key, read_only=True
                )
            )

            lower_batch = CollectionBatch((), (issue,))
            failed_again, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(failed_again, 1)

        alerts = [call for call in notifier_calls if not call.items]
        self.assertEqual(len(alerts), 2)

    def test_filter_skip_does_not_rearm_an_undelivered_article_failure(self) -> None:
        source = source_config("Alpha News", apply_filter=True)
        selected_item = item(source, "flaky", title="GPT Flaky Article")
        filtered_item = item(source, "flaky", title="Unrelated Article")
        current = CollectionBatch((selected_item,))
        outcomes = {source.name: lambda: current}
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            initialize_sources(path, config.sources)
            storage = SQLiteStorage(path)

            failed, _ = self.run_app(
                config,
                path,
                outcomes,
                reader_factory=reader_factory_for(failures={selected_item.url}),
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(failed, 1)

            current = CollectionBatch((filtered_item,))
            skipped, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(skipped, 0)
            self.assertTrue(
                storage.has_active_failure(
                    source.name, selected_item.dedupe_key, read_only=True
                )
            )

            current = CollectionBatch((selected_item,))
            failed_again, _ = self.run_app(
                config,
                path,
                outcomes,
                reader_factory=reader_factory_for(failures={selected_item.url}),
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(failed_again, 1)

        alerts = [call for call in notifier_calls if not call.items]
        self.assertEqual(len(alerts), 1)

    def test_completed_summary_is_cached_while_another_model_task_is_pending(
        self,
    ) -> None:
        source = source_config("Alpha Engineering")
        fast_item = item(source, "fast")
        slow_item = item(source, "slow")
        outcomes = {source.name: CollectionBatch((fast_item, slow_item))}
        notifier_calls: list[Any] = []
        observed_cache_before_slow_returned = False

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            initialize_sources(path, config.sources)
            storage = SQLiteStorage(path)

            class CoordinatedSummarizer:
                def __init__(self) -> None:
                    self.slow_started: asyncio.Event | None = None

                async def summarize(
                    self, news_item: NewsItem, article: str
                ) -> ChineseSummary:
                    nonlocal observed_cache_before_slow_returned
                    if self.slow_started is None:
                        self.slow_started = asyncio.Event()
                    if news_item.item_id == fast_item.item_id:
                        await self.slow_started.wait()
                        return summary_for(news_item)

                    self.slow_started.set()
                    for _ in range(200):
                        await asyncio.sleep(0)
                        cached = storage.cached_summary(
                            fast_item,
                            MODEL_CONFIG,
                            PROMPT_VERSION,
                            read_only=True,
                        )
                        if cached is not None:
                            observed_cache_before_slow_returned = True
                            return summary_for(news_item)
                    raise AssertionError(
                        "fast summary was not cached while slow task was pending"
                    )

                async def close(self) -> None:
                    return None

            result, _ = self.run_app(
                config,
                path,
                outcomes,
                summarizer_factory=lambda model, api_key: CoordinatedSummarizer(),
                notifier_factory=notifier_factory_for(notifier_calls),
            )

            self.assertEqual(result, 0)
            self.assertTrue(observed_cache_before_slow_returned)
            self.assertIsNotNone(
                storage.cached_summary(
                    fast_item, MODEL_CONFIG, PROMPT_VERSION, read_only=True
                )
            )
            self.assertIsNotNone(
                storage.cached_summary(
                    slow_item, MODEL_CONFIG, PROMPT_VERSION, read_only=True
                )
            )

        content = [call for call in notifier_calls if call.items]
        self.assertEqual(
            [call.items[0].item_id for call in content],
            [fast_item.item_id, slow_item.item_id],
        )

    def test_source_recovery_clears_failure_and_allows_a_later_alert(self) -> None:
        source = source_config("Alpha Engineering")
        current: CollectionBatch | BaseException = CollectionError("offline")
        outcomes = {source.name: lambda: current}
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            initialize_sources(path, config.sources)
            storage = SQLiteStorage(path)

            for expected in (1, 1):
                result, _ = self.run_app(
                    config,
                    path,
                    outcomes,
                    notifier_factory=notifier_factory_for(notifier_calls),
                )
                self.assertEqual(result, expected)
            self.assertTrue(
                storage.has_active_failure(
                    source.name, SOURCE_FAILURE_ITEM_KEY, read_only=True
                )
            )

            current = CollectionBatch(())
            recovered, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(recovered, 0)
            self.assertFalse(
                storage.has_active_failure(
                    source.name, SOURCE_FAILURE_ITEM_KEY, read_only=True
                )
            )

            current = CollectionError("offline again")
            failed_again, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(failed_again, 1)

        alerts = [call for call in notifier_calls if not call.items]
        self.assertEqual(len(alerts), 2)

    def test_failed_alert_is_not_recorded_and_is_retried_next_run(self) -> None:
        source = source_config("Alpha Engineering")
        outcomes = {source.name: CollectionError("offline")}
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            initialize_sources(path, config.sources)
            storage = SQLiteStorage(path)

            for _ in range(2):
                result, _ = self.run_app(
                    config,
                    path,
                    outcomes,
                    notifier_factory=notifier_factory_for(
                        notifier_calls, fail_alerts=True
                    ),
                )
                self.assertEqual(result, 1)
                self.assertFalse(
                    storage.has_active_failure(
                        source.name, SOURCE_FAILURE_ITEM_KEY, read_only=True
                    )
                )

        self.assertEqual(len(notifier_calls), 2)
        self.assertTrue(all(not call.items for call in notifier_calls))

    def test_collection_issues_and_source_failure_are_isolated_and_counted(
        self,
    ) -> None:
        alpha = source_config("Alpha Engineering")
        beta = source_config("Beta Research")
        gamma = source_config("Gamma Releases", content_mode="inline")
        alpha_item = item(alpha, "valid-alpha")
        gamma_item = item(gamma, "valid-gamma")
        issue = CollectionIssue(
            source=alpha.name,
            stage="date parsing",
            title="Broken Alpha Entry",
            url="https://official.example/articles/broken",
            message="invalid date",
            index=2,
        )
        outcomes = {
            alpha.name: CollectionBatch((alpha_item,), (issue,)),
            beta.name: CollectionError("source unavailable"),
            gamma.name: CollectionBatch((gamma_item,)),
        }
        constructed: list[str] = []
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(alpha, beta, gamma)
            initialize_sources(path, config.sources)
            result, output = self.run_app(
                config,
                path,
                outcomes,
                collector_factory=collector_factory_for(outcomes, constructed),
                notifier_factory=notifier_factory_for(notifier_calls),
            )

            storage = SQLiteStorage(path)
            self.assertEqual(result, 1)
            self.assertTrue(storage.is_delivered(alpha_item, read_only=True))
            self.assertTrue(storage.is_delivered(gamma_item, read_only=True))
            self.assertTrue(
                storage.has_active_failure(
                    beta.name, SOURCE_FAILURE_ITEM_KEY, read_only=True
                )
            )

        content = [call for call in notifier_calls if call.items]
        alerts = [call for call in notifier_calls if not call.items]
        self.assertEqual(constructed, [alpha.name, beta.name, gamma.name])
        self.assertEqual(
            [call.items[0].item_id for call in content],
            [alpha_item.item_id, gamma_item.item_id],
        )
        self.assertEqual(len(alerts), 2)
        alert_text = "\n".join(call.encoded.decode("utf-8") for call in alerts)
        self.assertIn("Broken Alpha Entry", alert_text)
        self.assertIn(beta.name, alert_text)
        assert_summary_counts(
            self, output.getvalue(), sent=2, failed=2, baseline=0, skipped=0
        )

    def test_titled_collection_issue_identity_survives_index_shifts(self) -> None:
        source = source_config("API Changelog", content_mode="inline")
        issue_index = 1
        outcomes = {
            source.name: lambda: CollectionBatch(
                (),
                (
                    CollectionIssue(
                        source=source.name,
                        stage="date parsing",
                        title="Broken dated entry",
                        url=source.url,
                        message="invalid date",
                        index=issue_index,
                    ),
                ),
            )
        }
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            initialize_sources(path, config.sources)

            for shifted_index in (1, 2):
                issue_index = shifted_index
                failed, _ = self.run_app(
                    config,
                    path,
                    outcomes,
                    notifier_factory=notifier_factory_for(notifier_calls),
                )
                self.assertEqual(failed, 1)

            outcomes[source.name] = CollectionBatch(())
            recovered, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(recovered, 0)

            issue_index = 3
            outcomes[source.name] = lambda: CollectionBatch(
                (),
                (
                    CollectionIssue(
                        source=source.name,
                        stage="date parsing",
                        title="Broken dated entry",
                        url=source.url,
                        message="invalid date",
                        index=issue_index,
                    ),
                ),
            )
            failed_again, _ = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )
            self.assertEqual(failed_again, 1)

        alerts = [call for call in notifier_calls if not call.items]
        self.assertEqual(len(alerts), 2)

    def test_url_shape_change_does_not_repush_baselined_articles(self) -> None:
        """Replay of the 2026-08-17 incident: a site relists baseline articles
        under new URL shapes (for example a locale prefix) while the list keeps
        stable per-article ids.  The baseline secondary identities must keep
        every relisted article unseen-blocked."""

        source = source_config("Kimi Research")
        original_urls = [
            item(source, "kimi-k2", url="https://official.example/blog/kimi-k2"),
            item(
                source, "agent-swarm", url="https://official.example/blog/agent-swarm"
            ),
            item(source, "worldvqa", url="https://official.example/blog/worldvqa"),
        ]
        relisted_urls = [
            item(source, "kimi-k2", url="https://official.example/en/blog/kimi-k2"),
            item(
                source,
                "agent-swarm",
                url="https://official.example/en/blog/agent-swarm",
            ),
            item(source, "worldvqa", url="https://official.example/en/blog/worldvqa"),
        ]
        genuinely_new = item(
            source, "kimi-k3", url="https://official.example/en/blog/kimi-k3"
        )
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)

            _, baseline_output = self.run_app(
                config, path, {source.name: CollectionBatch(tuple(original_urls))}
            )
            assert_summary_counts(
                self,
                baseline_output.getvalue(),
                sent=0,
                failed=0,
                baseline=3,
                skipped=0,
            )

            outcomes = {source.name: CollectionBatch((*relisted_urls, genuinely_new))}
            result, output = self.run_app(
                config,
                path,
                outcomes,
                notifier_factory=notifier_factory_for(notifier_calls),
            )

            self.assertEqual(result, 0)
            self.assertEqual(len(notifier_calls), 1)
            self.assertEqual(
                tuple(delivered.url for delivered in notifier_calls[0].items),
                (genuinely_new.url,),
            )
            assert_summary_counts(
                self, output.getvalue(), sent=1, failed=0, baseline=0, skipped=3
            )

    def test_age_gate_blocks_stale_articles_and_alerts_once(self) -> None:
        source = source_config("Kimi Research", max_age_days=30)
        fresh = item(source, "fresh")
        stale_full = item(
            source, "stale-full", published_at="2026-01-05T09:00:00+08:00"
        )
        stale_date = item(source, "stale-date", published_at="2025-12-20")
        stale_month = item(source, "stale-month", published_at="2026-01")
        boundary_month = item(source, "boundary-month", published_at="2026-07")
        unparseable = item(source, "unparseable", published_at="Jul 20")
        batch = CollectionBatch(
            (fresh, stale_full, stale_date, stale_month, boundary_month, unparseable)
        )
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            initialize_sources(path, config.sources)

            result, output = self.run_app(
                config,
                path,
                {source.name: batch},
                notifier_factory=notifier_factory_for(notifier_calls),
            )

            # The clock is 2026-08-12 Beijing, so the 30-day cutoff is
            # 2026-07-13: month precision stays fresh until the whole month
            # expires, and unparseable dates are treated conservatively.
            self.assertEqual(result, 1)
            self.assertEqual(len(notifier_calls), 4)
            alerts = [call for call in notifier_calls if not call.items]
            self.assertEqual(len(alerts), 1)
            alert_text = alerts[0].encoded.decode("utf-8")
            self.assertIn("3 篇超过 30 天", alert_text)
            assert_summary_counts(
                self, output.getvalue(), sent=3, failed=1, baseline=0, skipped=3
            )

            # The stale articles reappear next round: still skipped, still one
            # CLI failure, but no repeated alert while the event is active.
            repeat_calls: list[Any] = []
            result_again, output_again = self.run_app(
                config,
                path,
                {source.name: CollectionBatch((stale_full, stale_date, stale_month))},
                notifier_factory=notifier_factory_for(repeat_calls),
            )
            self.assertEqual(result_again, 1)
            self.assertEqual(repeat_calls, [])
            assert_summary_counts(
                self, output_again.getvalue(), sent=0, failed=1, baseline=0, skipped=3
            )

            # A round without stale articles clears the active age event.
            storage = SQLiteStorage(path)
            self.assertTrue(
                storage.has_active_failure(source.name, "__age__", read_only=True)
            )
            recovered, _ = self.run_app(
                config,
                path,
                {source.name: CollectionBatch((fresh,))},
                notifier_factory=notifier_factory_for([]),
            )
            self.assertEqual(recovered, 0)
            self.assertFalse(
                storage.has_active_failure(source.name, "__age__", read_only=True)
            )

    def test_age_gate_off_without_configuration(self) -> None:
        source = source_config("Kimi Research")
        ancient = item(source, "ancient", published_at="2025-01-20")
        notifier_calls: list[Any] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            config = app_config(source)
            initialize_sources(path, config.sources)

            result, output = self.run_app(
                config,
                path,
                {source.name: CollectionBatch((ancient,))},
                notifier_factory=notifier_factory_for(notifier_calls),
            )

            self.assertEqual(result, 0)
            self.assertEqual(len(notifier_calls), 1)
            assert_summary_counts(
                self, output.getvalue(), sent=1, failed=0, baseline=0, skipped=0
            )


if __name__ == "__main__":
    unittest.main()
