from io import StringIO
from pathlib import Path
import tempfile
import unittest

from signalfeed.app import run
from signalfeed.config import (
    AppConfig,
    FeishuConfig,
    FilterConfig,
    NetworkConfig,
    SourceConfig,
)
from signalfeed.notifier import NotificationError

from tests.helpers import news_item


def test_config() -> AppConfig:
    return AppConfig(
        source=SourceConfig("OpenAI News", "https://example.com/rss", 20),
        network=NetworkConfig(15, 5 * 1024 * 1024, "test"),
        filter=FilterConfig(("title", "content"), ("GPT",)),
        feishu=FeishuConfig("Signals", 18 * 1024, 200),
    )


class FixedCollector:
    def __init__(self, source: object, network: object) -> None:
        pass

    def collect(self) -> list[object]:
        return [news_item()]


class AppTests(unittest.TestCase):
    def test_send_requires_webhook_before_collecting(self) -> None:
        class ForbiddenCollector:
            def __init__(self, source: object, network: object) -> None:
                raise AssertionError("collector should not be created")

        with self.assertRaisesRegex(ValueError, "FEISHU_WEBHOOK_URL"):
            run(
                test_config(),
                mode="send",
                database_path="unused.sqlite3",
                output=StringIO(),
                collector_factory=ForbiddenCollector,
            )

    def test_dry_run_does_not_call_webhook_or_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "state.sqlite3"
            output = StringIO()

            def forbidden_notifier(*args: object) -> object:
                raise AssertionError("dry-run created a notifier")

            result = run(
                test_config(),
                mode="dry-run",
                database_path=path,
                output=output,
                collector_factory=FixedCollector,
                notifier_factory=forbidden_notifier,
            )
            self.assertEqual(result, 0)
            self.assertIn('"msg_type":"post"', output.getvalue())
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    def test_success_is_deduplicated_on_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            sent: list[object] = []

            class RecordingNotifier:
                def __init__(self, webhook: str, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    sent.append(digest)

            for _ in range(2):
                run(
                    test_config(),
                    mode="send",
                    database_path=path,
                    output=StringIO(),
                    webhook_url="https://example.com/hook",
                    collector_factory=FixedCollector,
                    notifier_factory=RecordingNotifier,
                )
            self.assertEqual(len(sent), 1)

    def test_failed_send_is_not_recorded_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            attempts = 0

            class FailingNotifier:
                def __init__(self, webhook: str, timeout: float) -> None:
                    pass

                def send(self, digest: object) -> None:
                    nonlocal attempts
                    attempts += 1
                    raise NotificationError("rejected")

            for _ in range(2):
                with self.assertRaises(NotificationError):
                    run(
                        test_config(),
                        mode="send",
                        database_path=path,
                        output=StringIO(),
                        webhook_url="https://example.com/hook",
                        collector_factory=FixedCollector,
                        notifier_factory=FailingNotifier,
                    )
            self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
