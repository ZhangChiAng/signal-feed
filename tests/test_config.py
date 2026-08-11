from pathlib import Path
import unittest

from signalfeed.config import ConfigError, load_config


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


if __name__ == "__main__":
    unittest.main()
