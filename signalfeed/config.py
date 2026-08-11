"""TOML configuration loading and validation."""

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    """Raised when the application configuration is invalid."""


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    url: str
    window_size: int


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    timeout_seconds: float
    max_response_bytes: int
    user_agent: str


@dataclass(frozen=True, slots=True)
class FilterConfig:
    fields: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    title: str
    max_payload_bytes: int
    summary_max_chars: int


@dataclass(frozen=True, slots=True)
class AppConfig:
    source: SourceConfig
    network: NetworkConfig
    filter: FilterConfig
    feishu: FeishuConfig


def load_config(path: str | Path = "config.toml") -> AppConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            raw = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load config {config_path}: {exc}") from exc

    try:
        source_raw = raw["source"]
        network_raw = raw["network"]
        filter_raw = raw["filter"]
        feishu_raw = raw["feishu"]

        source = SourceConfig(
            name=_nonempty_string(source_raw["name"], "source.name"),
            url=_http_url(source_raw["url"], "source.url"),
            window_size=_positive_int(source_raw["window_size"], "source.window_size"),
        )
        network = NetworkConfig(
            timeout_seconds=_positive_number(
                network_raw["timeout_seconds"], "network.timeout_seconds"
            ),
            max_response_bytes=_positive_int(
                network_raw["max_response_bytes"], "network.max_response_bytes"
            ),
            user_agent=_nonempty_string(network_raw["user_agent"], "network.user_agent"),
        )

        fields = _string_tuple(filter_raw["fields"], "filter.fields")
        unsupported = set(fields) - {"title", "content"}
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ConfigError(f"filter.fields contains unsupported values: {names}")
        filter_config = FilterConfig(
            fields=fields,
            keywords=_string_tuple(filter_raw["keywords"], "filter.keywords"),
        )
        feishu = FeishuConfig(
            title=_nonempty_string(feishu_raw["title"], "feishu.title"),
            max_payload_bytes=_positive_int(
                feishu_raw["max_payload_bytes"], "feishu.max_payload_bytes"
            ),
            summary_max_chars=_positive_int(
                feishu_raw["summary_max_chars"], "feishu.summary_max_chars"
            ),
        )
    except KeyError as exc:
        raise ConfigError(f"missing config key: {exc.args[0]}") from exc
    except TypeError as exc:
        raise ConfigError(f"invalid config structure: {exc}") from exc

    if feishu.max_payload_bytes > 20 * 1024:
        raise ConfigError("feishu.max_payload_bytes must not exceed 20480")
    return AppConfig(source, network, filter_config, feishu)


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _http_url(value: object, name: str) -> str:
    text = _nonempty_string(value, name)
    if not text.startswith(("https://", "http://")):
        raise ConfigError(f"{name} must be an HTTP(S) URL")
    return text


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return float(value)


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must be a non-empty array")
    result = tuple(_nonempty_string(entry, name) for entry in value)
    return result
