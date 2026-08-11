"""Command-line entry point for ``python -m signalfeed``."""

import argparse
import logging
import os
from pathlib import Path
import sys

from .app import run
from .collector import CollectionError
from .config import ConfigError, load_config
from .notifier import NotificationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect OpenAI RSS signals for Feishu")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print payload without writes")
    mode.add_argument("--send", action="store_true", help="send one Feishu digest")
    parser.add_argument("--config", default="config.toml", help="TOML config path")
    args = parser.parse_args(argv)

    selected_mode = "send" if args.send else "dry-run"
    webhook_url = os.environ.get("FEISHU_WEBHOOK_URL")
    if selected_mode == "send" and not webhook_url:
        parser.error("FEISHU_WEBHOOK_URL is required in --send mode")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    database_path = Path(os.environ.get("SIGNALFEED_DB_PATH", "data/signalfeed.sqlite3"))
    try:
        config = load_config(args.config)
        return run(
            config,
            mode=selected_mode,
            database_path=database_path,
            output=sys.stdout,
            webhook_url=webhook_url,
        )
    except (CollectionError, ConfigError, NotificationError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
