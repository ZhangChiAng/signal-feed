import tempfile
import unittest
from pathlib import Path

from signalfeed.config import (
    ConfigError,
    ModelConfig,
    load_config,
    load_dotenv,
    load_models_config,
    resolve_api_key,
    resolve_feishu_delivery,
)


class ConfigTests(unittest.TestCase):
    def test_repository_config_loads(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.toml")
        self.assertEqual(len(config.sources), 14)
        self.assertEqual(config.sources[0].window_size, 20)
        self.assertEqual(config.sources[1].collector, "markdown_index")
        self.assertEqual(config.sources[1].transport, "jina")
        self.assertFalse(config.sources[1].filter)
        self.assertEqual(config.sources[7].allowed_hosts[-1], "moonshotai.github.io")
        self.assertEqual([source.max_age_days for source in config.sources], [30] * 14)
        self.assertEqual(config.filter.fields, ("title", "content"))
        self.assertIn("ChatGPT", config.filter.keywords)
        self.assertIn("智能体", config.filter.keywords)
        self.assertEqual(config.feishu.max_payload_bytes, 28 * 1024)

    def test_source_arrays_validate_names_enums_allow_list_and_boolean(self) -> None:
        repository = (Path(__file__).parents[1] / "config.toml").read_text(
            encoding="utf-8"
        )
        cases = {
            "duplicate": repository.replace(
                'name = "OpenAI Developer Blog"', 'name = "openai news"'
            ),
            "collector": repository.replace(
                'collector = "markdown_index"', 'collector = "llm_parser"', 1
            ),
            "transport": repository.replace(
                'transport = "jina"', 'transport = "proxy"', 1
            ),
            "content mode": repository.replace(
                'content_mode = "article"', 'content_mode = "full_page"', 1
            ),
            "allow list": repository.replace(
                'allowed_hosts = ["openai.com"]',
                'allowed_hosts = ["https://openai.com"]',
                1,
            ),
            "empty hostname label": repository.replace(
                'allowed_hosts = ["openai.com"]',
                'allowed_hosts = ["openai.com", "bad..host"]',
                1,
            ),
            "hostname edge hyphen": repository.replace(
                'allowed_hosts = ["openai.com"]',
                'allowed_hosts = ["openai.com", "-bad.example"]',
                1,
            ),
            "source host": repository.replace(
                'allowed_hosts = ["openai.com"]', 'allowed_hosts = ["example.com"]', 1
            ),
            "filter": repository.replace("filter = true", 'filter = "true"', 1),
            "zero max age": repository.replace(
                "max_age_days = 30", "max_age_days = 0", 1
            ),
            "non-integer max age": repository.replace(
                "max_age_days = 30", 'max_age_days = "30"', 1
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for name, content in cases.items():
                with self.subTest(name=name):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_config(path)

    def test_missing_config_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config("definitely-missing.toml")

    def test_feishu_rejects_unknown_fields(self) -> None:
        repository_config = Path(__file__).parents[1] / "config.toml"
        content = repository_config.read_text(encoding="utf-8").replace(
            "max_payload_bytes = 28672",
            'max_payload_bytes = 1024\ntitle = 42\nsummary_max_chars = "unused"',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_legacy_source_table_is_rejected(self) -> None:
        content = """[source]
name = "OpenAI News"
url = "https://openai.com/news/rss.xml"

[network]
timeout_seconds = 15
max_response_bytes = 1000
user_agent = "test"

[filter]
fields = ["title"]
keywords = ["model"]

[feishu]
max_payload_bytes = 1024
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, r"\[source\]"):
                load_config(path)

    def test_example_model_config_is_strict_and_loadable(self) -> None:
        config = load_models_config(Path(__file__).parents[1] / "models.example.toml")
        self.assertEqual(config.protocol, "openai_responses")
        self.assertEqual(config.api_key_env, "SIGNALFEED_LLM_API_KEY")
        self.assertEqual(config.base_url, "https://api.example.com/v1")

    def test_model_config_requires_one_model_and_exact_fields(self) -> None:
        cases = [
            "models = []\n",
            """[[models]]
model = "one"
protocol = "openai_responses"
base_url = "https://api.example.com/v1"
api_key_env = "SIGNALFEED_LLM_API_KEY"
extra = true
""",
            """[[models]]
model = "one"
protocol = "chat_completions"
base_url = "https://api.example.com/v1"
api_key_env = "SIGNALFEED_LLM_API_KEY"
""",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.toml"
            for content in cases:
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_models_config(path)

    def test_environment_file_does_not_override_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                'SIGNALFEED_LLM_API_KEY="file-key"\nSIGNALFEED_DB_PATH=data/test.sqlite3\n',
                encoding="utf-8",
            )
            environ = {"SIGNALFEED_LLM_API_KEY": "process-key"}
            load_dotenv(path, environ=environ)
            self.assertEqual(environ["SIGNALFEED_LLM_API_KEY"], "process-key")
            self.assertEqual(environ["SIGNALFEED_DB_PATH"], "data/test.sqlite3")
            config = ModelConfig(
                "model",
                "openai_responses",
                "https://api.example.com/v1",
                "SIGNALFEED_LLM_API_KEY",
            )
            self.assertEqual(resolve_api_key(config, environ=environ), "process-key")

    def test_repository_environment_example_is_loadable(self) -> None:
        environ: dict[str, str] = {}
        load_dotenv(Path(__file__).parents[1] / ".env.example", environ=environ)
        self.assertEqual(
            set(environ),
            {
                "SIGNALFEED_LLM_API_KEY",
                "FEISHU_APP_ID",
                "FEISHU_APP_SECRET",
                "FEISHU_RECEIVE_ID_TYPE",
                "FEISHU_RECEIVE_ID",
                "SIGNALFEED_DB_PATH",
            },
        )
        self.assertEqual(environ["SIGNALFEED_DB_PATH"], "data/signalfeed-zh.sqlite3")

    def test_feishu_delivery_environment_loads_all_fields(self) -> None:
        environ = {
            "FEISHU_APP_ID": " cli_test ",
            "FEISHU_APP_SECRET": " app-secret ",
            "FEISHU_RECEIVE_ID_TYPE": " chat_id ",
            "FEISHU_RECEIVE_ID": " oc_test ",
        }
        delivery = resolve_feishu_delivery(environ=environ)
        self.assertEqual(delivery.app_id, "cli_test")
        self.assertEqual(delivery.app_secret, "app-secret")
        self.assertEqual(delivery.receive_id_type, "chat_id")
        self.assertEqual(delivery.receive_id, "oc_test")

    def test_feishu_delivery_reports_all_missing_fields_without_values(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            resolve_feishu_delivery(environ={})
        message = str(raised.exception)
        for name in (
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_RECEIVE_ID_TYPE",
            "FEISHU_RECEIVE_ID",
        ):
            self.assertIn(name, message)

    def test_feishu_delivery_id_type_is_restricted_and_sanitized(self) -> None:
        allowed = ("chat_id", "open_id", "union_id", "user_id", "email")
        for receive_id_type in allowed:
            with self.subTest(receive_id_type=receive_id_type):
                delivery = resolve_feishu_delivery(
                    environ={
                        "FEISHU_APP_ID": "cli_test",
                        "FEISHU_APP_SECRET": "super-secret",
                        "FEISHU_RECEIVE_ID_TYPE": receive_id_type,
                        "FEISHU_RECEIVE_ID": "private-recipient",
                    }
                )
                self.assertEqual(delivery.receive_id_type, receive_id_type)

        with self.assertRaises(ConfigError) as raised:
            resolve_feishu_delivery(
                environ={
                    "FEISHU_APP_ID": "cli_test",
                    "FEISHU_APP_SECRET": "super-secret",
                    "FEISHU_RECEIVE_ID_TYPE": "secret-invalid-type",
                    "FEISHU_RECEIVE_ID": "private-recipient",
                }
            )
        message = str(raised.exception)
        self.assertNotIn("super-secret", message)
        self.assertNotIn("private-recipient", message)
        self.assertNotIn("secret-invalid-type", message)

    def test_feishu_payload_limit_accepts_30_kib_boundary(self) -> None:
        repository_config = Path(__file__).parents[1] / "config.toml"
        original = repository_config.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                original.replace(
                    "max_payload_bytes = 28672", "max_payload_bytes = 30720"
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_config(path).feishu.max_payload_bytes, 30 * 1024)

            path.write_text(
                original.replace(
                    "max_payload_bytes = 28672", "max_payload_bytes = 30721"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "30720"):
                load_config(path)

    def test_model_config_errors_do_not_echo_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.toml"
            path.write_text(
                """[[models]]
model = "one"
protocol = "openai_responses"
base_url = "https://user:super-secret@example.com/v1"
api_key_env = "SIGNALFEED_LLM_API_KEY"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as raised:
                load_models_config(path)
            self.assertNotIn("super-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
