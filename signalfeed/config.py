"""TOML configuration loading and validation."""

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


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
    max_payload_bytes: int


@dataclass(frozen=True, slots=True)
class FeishuDeliveryConfig:
    app_id: str
    app_secret: str
    receive_id_type: str
    receive_id: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    source: SourceConfig
    network: NetworkConfig
    filter: FilterConfig
    feishu: FeishuConfig


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str
    protocol: str
    base_url: str
    api_key_env: str


def load_config(path: str | Path = "config.toml") -> AppConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            raw = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"cannot load config {config_path}: {type(exc).__name__}"
        ) from exc

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
            user_agent=_nonempty_string(
                network_raw["user_agent"], "network.user_agent"
            ),
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
            max_payload_bytes=_positive_int(
                feishu_raw["max_payload_bytes"], "feishu.max_payload_bytes"
            ),
        )
    except KeyError as exc:
        raise ConfigError(f"missing config key: {exc.args[0]}") from exc
    except TypeError as exc:
        raise ConfigError(f"invalid config structure: {exc}") from exc

    if feishu.max_payload_bytes > 30 * 1024:
        raise ConfigError("feishu.max_payload_bytes must not exceed 30720")
    return AppConfig(source, network, filter_config, feishu)


def load_models_config(path: str | Path = "models.toml") -> ModelConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            raw = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"cannot load models config {config_path}: {type(exc).__name__}"
        ) from exc

    if set(raw) != {"models"}:
        raise ConfigError("models config must contain only the models array")
    models = raw.get("models")
    if (
        not isinstance(models, list)
        or len(models) != 1
        or not isinstance(models[0], dict)
    ):
        raise ConfigError("models config must contain exactly one model")

    model_raw = models[0]
    expected = {"model", "protocol", "base_url", "api_key_env"}
    if set(model_raw) != expected:
        raise ConfigError("model config must contain exactly four supported fields")

    model = _nonempty_string(model_raw["model"], "models.model")
    protocol = _nonempty_string(model_raw["protocol"], "models.protocol")
    if protocol != "openai_responses":
        raise ConfigError("models.protocol must be openai_responses")
    base_url = _safe_endpoint(model_raw["base_url"], "models.base_url")
    api_key_env = _nonempty_string(model_raw["api_key_env"], "models.api_key_env")
    if api_key_env != "SIGNALFEED_LLM_API_KEY":
        raise ConfigError("models.api_key_env must be SIGNALFEED_LLM_API_KEY")
    return ModelConfig(model, protocol, base_url, api_key_env)


def load_dotenv(
    path: str | Path = ".env",
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> None:
    """Load a small, non-interpolating dotenv file without overriding the process."""

    destination = os.environ if environ is None else environ
    env_path = Path(path)
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ConfigError(
            f"cannot load environment file: {type(exc).__name__}"
        ) from exc

    for line_number, original in enumerate(lines, start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not key.isidentifier():
            raise ConfigError(f"invalid environment entry on line {line_number}")
        if key in destination:
            continue
        destination[key] = _dotenv_value(raw_value.strip(), line_number)


def resolve_api_key(
    config: ModelConfig,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> str:
    source = os.environ if environ is None else environ
    value = source.get(config.api_key_env)
    if not value:
        raise ConfigError(f"{config.api_key_env} is required")
    return value


def resolve_feishu_delivery(
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> FeishuDeliveryConfig:
    source = os.environ if environ is None else environ
    names = (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_RECEIVE_ID_TYPE",
        "FEISHU_RECEIVE_ID",
    )
    values = {name: source.get(name, "").strip() for name in names}
    missing = [name for name in names if not values[name]]
    if missing:
        raise ConfigError(
            f"missing required Feishu environment variables: {', '.join(missing)}"
        )

    allowed_id_types = {"chat_id", "open_id", "union_id", "user_id", "email"}
    receive_id_type = values["FEISHU_RECEIVE_ID_TYPE"]
    if receive_id_type not in allowed_id_types:
        allowed = ", ".join(sorted(allowed_id_types))
        raise ConfigError(f"FEISHU_RECEIVE_ID_TYPE must be one of: {allowed}")

    return FeishuDeliveryConfig(
        app_id=values["FEISHU_APP_ID"],
        app_secret=values["FEISHU_APP_SECRET"],
        receive_id_type=receive_id_type,
        receive_id=values["FEISHU_RECEIVE_ID"],
    )


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _http_url(value: object, name: str) -> str:
    text = _nonempty_string(value, name)
    if not text.startswith(("https://", "http://")):
        raise ConfigError(f"{name} must be an HTTP(S) URL")
    return text


def _safe_endpoint(value: object, name: str) -> str:
    text = _http_url(value, name)
    parsed = urlsplit(text)
    if not parsed.hostname or parsed.username or parsed.password:
        raise ConfigError(f"{name} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{name} must not contain a query or fragment")
    return text.rstrip("/")


def _dotenv_value(value: str, line_number: int) -> str:
    if not value:
        return ""
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"invalid quoted environment value on line {line_number}"
            ) from exc
        if not isinstance(parsed, str):
            raise ConfigError(f"invalid environment value on line {line_number}")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ConfigError(f"invalid quoted environment value on line {line_number}")
        return value[1:-1]
    return value.split(" #", 1)[0].rstrip()


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
