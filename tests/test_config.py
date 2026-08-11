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
)


class ConfigTests(unittest.TestCase):
    def test_repository_config_loads(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.toml")
        self.assertEqual(config.source.window_size, 20)
        self.assertEqual(config.filter.fields, ("title", "content"))
        self.assertIn("ChatGPT", config.filter.keywords)
        self.assertLess(config.feishu.max_payload_bytes, 20 * 1024)

    def test_missing_config_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config("definitely-missing.toml")

    def test_legacy_feishu_fields_are_accepted_but_ignored(self) -> None:
        repository_config = Path(__file__).parents[1] / "config.toml"
        content = repository_config.read_text(encoding="utf-8").replace(
            "max_payload_bytes = 18432",
            'max_payload_bytes = 1024\ntitle = 42\nsummary_max_chars = "unused"',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(content, encoding="utf-8")
            config = load_config(path)

        self.assertEqual(config.feishu.max_payload_bytes, 1024)

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
                "FEISHU_WEBHOOK_URL",
                "SIGNALFEED_DB_PATH",
            },
        )
        self.assertEqual(environ["SIGNALFEED_DB_PATH"], "data/signalfeed-zh.sqlite3")

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
