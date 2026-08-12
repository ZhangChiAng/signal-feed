import asyncio
import json
import tempfile
import unittest
from collections import Counter
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from signalfeed.app import MAX_MODEL_CONCURRENCY, run
from signalfeed.config import (
    AppConfig,
    FeishuConfig,
    FeishuDeliveryConfig,
    FilterConfig,
    ModelConfig,
    NetworkConfig,
    SourceConfig,
)
from signalfeed.model import ChineseSummary
from signalfeed.notifier import NotificationError, build_digests
from signalfeed.storage import SQLiteStorage
from signalfeed.summarizer import PROMPT_VERSION, SummaryError
from tests.helpers import news_item

MODEL_CONFIG = ModelConfig(
    "test-model",
    "openai_responses",
    "https://api.example.com/v1",
    "SIGNALFEED_LLM_API_KEY",
)
SUMMARY = ChineseSummary(
    "GPT 中文发布", ("第一条中文要点。", "第二条中文要点。", "第三条中文要点。")
)
DELIVERY = FeishuDeliveryConfig("cli_test", "app-secret", "chat_id", "oc_private")


def test_config(max_payload_bytes: int = 28 * 1024) -> AppConfig:
    return AppConfig(
        source=SourceConfig("OpenAI News", "https://example.com/rss", 20),
        network=NetworkConfig(15, 5 * 1024 * 1024, "test"),
        filter=FilterConfig(("title", "content"), ("GPT",)),
        feishu=FeishuConfig(max_payload_bytes),
    )


class FixedCollector:
    def __init__(self, source: object, network: object) -> None:
        pass

    def collect(self) -> list[object]:
        return [news_item()]


class FixedReader:
    calls = 0

    def __init__(self, user_agent: str) -> None:
        pass

    def read(self, url: str) -> str:
        type(self).calls += 1
        return "Full article body"


class FixedSummarizer:
    calls = 0

    def __init__(self, model: ModelConfig, api_key: str) -> None:
        pass

    async def summarize(self, item: object, article: str) -> ChineseSummary:
        type(self).calls += 1
        return SUMMARY


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        FixedReader.calls = 0
        FixedSummarizer.calls = 0

    def run_app(self, **overrides: object) -> int:
        kwargs: dict[str, object] = {
            "mode": "dry-run",
            "database_path": "unused.sqlite3",
            "output": StringIO(),
            "api_key": "test-key",
            "collector_factory": FixedCollector,
            "reader_factory": FixedReader,
            "summarizer_factory": FixedSummarizer,
            "clock": lambda: datetime(2026, 8, 12, 12, tzinfo=UTC),
        }
        kwargs.update(overrides)
        config = kwargs.pop("config", test_config())
        return run(config, MODEL_CONFIG, **kwargs)  # type: ignore[arg-type]

    def test_send_requires_feishu_delivery_before_collecting(self) -> None:
        class ForbiddenCollector:
            def __init__(self, source: object, network: object) -> None:
                raise AssertionError("collector should not be created")

        with self.assertRaisesRegex(ValueError, "Feishu delivery configuration"):
            self.run_app(mode="send", collector_factory=ForbiddenCollector)

    def test_dry_run_generates_chinese_without_feishu_config_or_database_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "state.sqlite3"
            output = StringIO()

            def forbidden_notifier(*args: object) -> object:
                raise AssertionError("dry-run created a notifier")

            for _ in range(2):
                result = self.run_app(
                    database_path=path,
                    output=output,
                    notifier_factory=forbidden_notifier,
                )
                self.assertEqual(result, 0)
            self.assertIn('"msg_type":"post"', output.getvalue())
            self.assertIn("GPT 中文发布", output.getvalue())
            self.assertEqual(FixedSummarizer.calls, 2)
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    def test_beijing_date_is_computed_once_and_shared_by_all_dry_run_batches(
        self,
    ) -> None:
        items = [
            news_item(
                item_id=f"guid-{index}",
                guid=f"guid-{index}",
                url=f"https://example.com/news/{index}",
            )
            for index in range(3)
        ]

        class ThreeCollector:
            def __init__(self, source: object, network: object) -> None:
                pass

            def collect(self) -> list[object]:
                return items

        localized = [SUMMARY.apply_to(item) for item in items]
        limit = max(
            len(
                build_digests(
                    [item],
                    title="2026-08-12 · SignalFeed",
                    max_payload_bytes=100_000,
                )[0].encoded
            )
            for item in localized
        )
        clock_calls = 0

        def clock() -> datetime:
            nonlocal clock_calls
            clock_calls += 1
            return datetime(2026, 8, 11, 16, 30, tzinfo=UTC)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.sqlite3"
            output = StringIO()
            self.run_app(
                config=test_config(limit),
                database_path=path,
                output=output,
                collector_factory=ThreeCollector,
                clock=clock,
            )
            self.assertFalse(path.parent.exists())

        payloads = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertGreater(len(payloads), 1)
        self.assertEqual(clock_calls, 1)
        self.assertTrue(
            all(
                payload["content"]["zh_cn"]["title"] == "2026-08-12 · SignalFeed"
                for payload in payloads
            )
        )

    def test_success_is_deduplicated_on_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            sent: list[object] = []

            class RecordingNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)

            outputs = []
            for _ in range(2):
                output = StringIO()
                self.run_app(
                    mode="send",
                    database_path=path,
                    output=output,
                    feishu_delivery=DELIVERY,
                    notifier_factory=RecordingNotifier,
                )
                outputs.append(output.getvalue())
            self.assertEqual(len(sent), 1)
            self.assertEqual(FixedSummarizer.calls, 1)
            self.assertIn("No new matching items.", outputs[1])

    def test_failed_send_is_not_delivered_and_reuses_cached_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            attempts = 0

            class FailingNotifier:
                def __init__(self, delivery: object, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    nonlocal attempts
                    attempts += 1
                    raise NotificationError("rejected")

            for _ in range(2):
                with self.assertRaises(NotificationError):
                    self.run_app(
                        mode="send",
                        database_path=path,
                        feishu_delivery=DELIVERY,
                        notifier_factory=FailingNotifier,
                    )
            self.assertEqual(attempts, 2)
            self.assertEqual(FixedSummarizer.calls, 1)
            self.assertEqual(SQLiteStorage(path).unseen([news_item()]), [news_item()])
            self.assertEqual(
                SQLiteStorage(path).cached_summary(
                    news_item(), MODEL_CONFIG, PROMPT_VERSION, read_only=True
                ),
                SUMMARY,
            )

    def test_second_batch_failure_records_first_and_retry_sends_only_remaining(
        self,
    ) -> None:
        items = [
            news_item(
                item_id=f"guid-{index}",
                guid=f"guid-{index}",
                url=f"https://example.com/news/{index}",
            )
            for index in range(1, 4)
        ]

        class ThreeCollector:
            def __init__(self, source: object, network: object) -> None:
                pass

            def collect(self) -> list[object]:
                return items

        limit = max(
            len(
                build_digests(
                    [SUMMARY.apply_to(item)],
                    title="2026-08-12 · SignalFeed",
                    max_payload_bytes=100_000,
                )[0].encoded
            )
            for item in items
        )
        attempts: list[tuple[str, ...]] = []

        class FailSecondNotifier:
            def __init__(self, delivery: object, timeout: float) -> None:
                pass

            def send(self, digest: object) -> None:
                attempts.append(tuple(item.item_id for item in digest.items))  # type: ignore[attr-defined]
                if len(attempts) == 2:
                    raise NotificationError("second batch failed")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self.assertRaisesRegex(NotificationError, "second batch"):
                self.run_app(
                    config=test_config(limit),
                    mode="send",
                    database_path=path,
                    feishu_delivery=DELIVERY,
                    collector_factory=ThreeCollector,
                    notifier_factory=FailSecondNotifier,
                )
            self.assertEqual(SQLiteStorage(path).unseen(items), items[1:])

            self.run_app(
                config=test_config(limit),
                mode="send",
                database_path=path,
                feishu_delivery=DELIVERY,
                collector_factory=ThreeCollector,
                notifier_factory=FailSecondNotifier,
            )
            self.assertEqual(SQLiteStorage(path).unseen(items), [])

        self.assertEqual(
            attempts,
            [("guid-1",), ("guid-2",), ("guid-2",), ("guid-3",)],
        )
        self.assertEqual(FixedSummarizer.calls, 3)

    def test_oversized_later_item_is_rejected_before_any_send(self) -> None:
        first = news_item()
        second = news_item(
            item_id="guid-large",
            guid="guid-large",
            url="https://example.com/news/large",
        )

        class TwoCollector:
            def __init__(self, source: object, network: object) -> None:
                pass

            def collect(self) -> list[object]:
                return [first, second]

        normal = SUMMARY.apply_to(first)
        limit = len(
            build_digests(
                [normal],
                title="2026-08-12 · SignalFeed",
                max_payload_bytes=100_000,
            )[0].encoded
        )

        class OversizedSummarizer:
            def __init__(self, model: ModelConfig, api_key: str) -> None:
                pass

            async def summarize(self, item: object, article: str) -> ChineseSummary:
                if item.item_id == "guid-large":  # type: ignore[attr-defined]
                    return ChineseSummary(
                        "超长完整标题",
                        ("完整句子。" * 500, "第二条完整句子。", "第三条完整句子。"),
                    )
                return SUMMARY

        def forbidden_notifier(*args: object) -> object:
            raise AssertionError("notifier must not be created before preflight")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self.assertRaisesRegex(NotificationError, "guid-large"):
                self.run_app(
                    config=test_config(limit),
                    mode="send",
                    database_path=path,
                    feishu_delivery=DELIVERY,
                    collector_factory=TwoCollector,
                    summarizer_factory=OversizedSummarizer,
                    notifier_factory=forbidden_notifier,
                )
            self.assertEqual(
                SQLiteStorage(path).unseen([first, second]), [first, second]
            )

    def test_partial_generation_is_cached_but_round_is_not_sent(self) -> None:
        first = news_item()
        second = news_item(
            item_id="guid-2", guid="guid-2", url="https://example.com/news/2"
        )

        class TwoCollector:
            def __init__(self, source: object, network: object) -> None:
                pass

            def collect(self) -> list[object]:
                return [first, second]

        calls: list[str] = []

        class FlakySummarizer:
            def __init__(self, model: ModelConfig, api_key: str) -> None:
                pass

            async def summarize(self, item: object, article: str) -> ChineseSummary:
                calls.append(item.item_id)  # type: ignore[attr-defined]
                if item.item_id == "guid-2" and calls.count("guid-2") == 1:  # type: ignore[attr-defined]
                    raise SummaryError("temporary failure")
                return SUMMARY

        sent: list[object] = []

        class RecordingNotifier:
            def __init__(self, delivery: object, timeout: float) -> None:
                pass

            def send(self, digest: object) -> None:
                sent.append(digest)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with self.assertRaises(SummaryError):
                self.run_app(
                    mode="send",
                    database_path=path,
                    feishu_delivery=DELIVERY,
                    collector_factory=TwoCollector,
                    summarizer_factory=FlakySummarizer,
                    notifier_factory=RecordingNotifier,
                )
            self.assertEqual(sent, [])
            self.assertEqual(
                SQLiteStorage(path).unseen([first, second]), [first, second]
            )

            self.run_app(
                mode="send",
                database_path=path,
                feishu_delivery=DELIVERY,
                collector_factory=TwoCollector,
                summarizer_factory=FlakySummarizer,
                notifier_factory=RecordingNotifier,
            )
            self.assertEqual(Counter(calls), Counter({"guid-1": 1, "guid-2": 2}))
            self.assertEqual(len(sent), 1)

    def test_model_calls_run_in_parallel_with_a_hard_limit_of_500(self) -> None:
        items = [
            news_item(
                item_id=f"guid-{index}",
                guid=f"guid-{index}",
                title=f"GPT item {index}",
                url=f"https://example.com/news/{index}",
            )
            for index in range(MAX_MODEL_CONCURRENCY + 1)
        ]

        class ManyCollector:
            def __init__(self, source: object, network: object) -> None:
                pass

            def collect(self) -> list[object]:
                return items

        class CappedSummarizer:
            active = 0
            max_active = 0
            release: asyncio.Event | None = None
            closed = False

            def __init__(self, model: ModelConfig, api_key: str) -> None:
                pass

            async def summarize(self, item: object, article: str) -> ChineseSummary:
                if type(self).release is None:
                    type(self).release = asyncio.Event()
                type(self).active += 1
                type(self).max_active = max(type(self).max_active, type(self).active)
                if type(self).active == MAX_MODEL_CONCURRENCY:
                    type(self).release.set()
                try:
                    await asyncio.wait_for(type(self).release.wait(), timeout=5)
                    return ChineseSummary(
                        f"中文标题 {item.item_id}",  # type: ignore[attr-defined]
                        ("第一条要点", "第二条要点", "第三条要点"),
                    )
                finally:
                    type(self).active -= 1

            async def close(self) -> None:
                type(self).closed = True

        with tempfile.TemporaryDirectory() as directory:
            output = StringIO()
            result = self.run_app(
                database_path=Path(directory) / "state.sqlite3",
                output=output,
                collector_factory=ManyCollector,
                summarizer_factory=CappedSummarizer,
            )

        self.assertEqual(result, 0)
        self.assertEqual(CappedSummarizer.max_active, MAX_MODEL_CONCURRENCY)
        self.assertTrue(CappedSummarizer.closed)
        payload = output.getvalue()
        self.assertLess(
            payload.index("中文标题 guid-0"), payload.index("中文标题 guid-1")
        )
        self.assertLess(
            payload.index("中文标题 guid-1"), payload.index("中文标题 guid-2")
        )


if __name__ == "__main__":
    unittest.main()
