"""Command-line entry point for ``python -m signalfeed``."""

import argparse
import logging
import os
import sys
from pathlib import Path

from .app import run
from .collector import CollectionError
from .config import (
    ConfigError,
    load_config,
    load_dotenv,
    load_models_config,
    resolve_api_key,
    resolve_feishu_delivery,
)
from .notifier import NotificationError
from .reader import ReaderError
from .summarizer import SummaryError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect OpenAI RSS signals for Feishu"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print all planned payload batches without local writes",
    )
    mode.add_argument(
        "--send", action="store_true", help="send all planned Feishu digest batches"
    )
    parser.add_argument("--config", default="config.toml", help="TOML config path")
    parser.add_argument(
        "--models-config", default="models.toml", help="model TOML config path"
    )
    args = parser.parse_args(argv)

    selected_mode = "send" if args.send else "dry-run"
    try:
        load_dotenv(Path.cwd() / ".env")
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    database_path = Path(
        os.environ.get("SIGNALFEED_DB_PATH", "data/signalfeed.sqlite3")
    )
    try:
        config = load_config(args.config)
        model_config = load_models_config(args.models_config)
        api_key = resolve_api_key(model_config)
        feishu_delivery = resolve_feishu_delivery() if selected_mode == "send" else None
        return run(
            config,
            model_config,
            mode=selected_mode,
            database_path=database_path,
            output=sys.stdout,
            api_key=api_key,
            feishu_delivery=feishu_delivery,
        )
    except (
        CollectionError,
        ConfigError,
        NotificationError,
        OSError,
        ReaderError,
        SummaryError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
