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
    window_size: int = 20
    collector: str = "rss"
    transport: str = "direct"
    content_mode: str = "article"
    allowed_hosts: tuple[str, ...] = ()
    filter: bool = True

    def __post_init__(self) -> None:
        """Validate programmatic construction as strictly as TOML loading."""

        name = _nonempty_string(self.name, "sources.name")
        url = _source_url(self.url, "sources.url")
        window_size = _positive_int(self.window_size, "sources.window_size")
        collector = _enum_value(
            self.collector,
            "sources.collector",
            {
                "rss",
                "markdown_index",
                "markdown_changelog",
                "markdown_cards",
                "next_data_index",
            },
        )
        transport = _enum_value(self.transport, "sources.transport", {"direct", "jina"})
        content_mode = _enum_value(
            self.content_mode, "sources.content_mode", {"article", "inline"}
        )
        if not isinstance(self.filter, bool):
            raise ConfigError("sources.filter must be a boolean")

        if self.allowed_hosts:
            allowed_hosts = _host_tuple(self.allowed_hosts, "sources.allowed_hosts")
        else:
            # Programmatic three-argument construction was the public API before
            # multi-source support.  Deriving its single host preserves that API;
            # new TOML entries are still required to declare the allow-list.
            host = urlsplit(url).hostname
            if host is None:  # pragma: no cover - guarded by _source_url
                raise ConfigError("sources.url must contain a host")
            allowed_hosts = (host.lower(),)

        source_host = urlsplit(url).hostname
        if source_host is None or not _host_is_allowed(source_host, allowed_hosts):
            raise ConfigError("sources.allowed_hosts must allow the source URL host")

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "window_size", window_size)
        object.__setattr__(self, "collector", collector)
        object.__setattr__(self, "transport", transport)
        object.__setattr__(self, "content_mode", content_mode)
        object.__setattr__(self, "allowed_hosts", allowed_hosts)


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


@dataclass(frozen=True, slots=True, init=False)
class AppConfig:
    sources: tuple[SourceConfig, ...]
    network: NetworkConfig
    filter: FilterConfig
    feishu: FeishuConfig

    def __init__(
        self,
        sources: tuple[SourceConfig, ...] | SourceConfig | None = None,
        network: NetworkConfig | None = None,
        filter: FilterConfig | None = None,
        feishu: FeishuConfig | None = None,
        *,
        source: SourceConfig | None = None,
    ) -> None:
        """Build an application config, accepting the legacy ``source=`` alias."""

        if source is not None:
            if sources is not None:
                raise ConfigError("configure either source or sources, not both")
            normalized_sources = (source,)
        elif isinstance(sources, SourceConfig):
            # Also preserve the old positional AppConfig(source, ...) spelling.
            normalized_sources = (sources,)
        elif isinstance(sources, tuple) and sources:
            normalized_sources = sources
        else:
            raise ConfigError("sources must contain at least one source")
        if not all(isinstance(entry, SourceConfig) for entry in normalized_sources):
            raise ConfigError("sources must contain only SourceConfig values")
        if network is None or filter is None or feishu is None:
            raise ConfigError("network, filter, and feishu configurations are required")

        _validate_unique_source_names(normalized_sources)
        object.__setattr__(self, "sources", normalized_sources)
        object.__setattr__(self, "network", network)
        object.__setattr__(self, "filter", filter)
        object.__setattr__(self, "feishu", feishu)

    @property
    def source(self) -> SourceConfig:
        """The first source, retained for callers migrating from single-source."""

        return self.sources[0]


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
        network_raw = raw["network"]
        filter_raw = raw["filter"]
        feishu_raw = raw["feishu"]
        sources = _load_sources(raw)
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
    return AppConfig(sources, network, filter_config, feishu)


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


def _load_sources(raw: dict[str, object]) -> tuple[SourceConfig, ...]:
    has_legacy = "source" in raw
    has_array = "sources" in raw
    if has_legacy == has_array:
        if has_legacy:
            raise ConfigError("configure either [source] or [[sources]], not both")
        raise ConfigError("missing config key: sources")

    if has_legacy:
        source_raw = raw["source"]
        if not isinstance(source_raw, dict):
            raise ConfigError("invalid config structure: source must be a table")
        try:
            source = SourceConfig(
                name=_nonempty_string(source_raw["name"], "source.name"),
                url=_source_url(source_raw["url"], "source.url"),
                window_size=_positive_int(
                    source_raw.get("window_size", 20), "source.window_size"
                ),
            )
        except KeyError as exc:
            raise ConfigError(f"missing config key: source.{exc.args[0]}") from exc
        return (source,)

    source_array = raw["sources"]
    if not isinstance(source_array, list) or not source_array:
        raise ConfigError("sources must be a non-empty array of tables")
    sources: list[SourceConfig] = []
    required = {
        "name",
        "url",
        "collector",
        "transport",
        "content_mode",
        "allowed_hosts",
        "filter",
    }
    supported = required | {"window_size"}
    for index, source_raw in enumerate(source_array, start=1):
        prefix = f"sources[{index}]"
        if not isinstance(source_raw, dict):
            raise ConfigError(f"{prefix} must be a table")
        missing = required - set(source_raw)
        if missing:
            names = ", ".join(sorted(missing))
            raise ConfigError(f"{prefix} is missing required fields: {names}")
        unsupported = set(source_raw) - supported
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ConfigError(f"{prefix} contains unsupported fields: {names}")
        sources.append(
            SourceConfig(
                name=_nonempty_string(source_raw["name"], f"{prefix}.name"),
                url=_source_url(source_raw["url"], f"{prefix}.url"),
                window_size=_positive_int(
                    source_raw.get("window_size", 20), f"{prefix}.window_size"
                ),
                collector=_enum_value(
                    source_raw["collector"],
                    f"{prefix}.collector",
                    {
                        "rss",
                        "markdown_index",
                        "markdown_changelog",
                        "markdown_cards",
                        "next_data_index",
                    },
                ),
                transport=_enum_value(
                    source_raw["transport"],
                    f"{prefix}.transport",
                    {"direct", "jina"},
                ),
                content_mode=_enum_value(
                    source_raw["content_mode"],
                    f"{prefix}.content_mode",
                    {"article", "inline"},
                ),
                allowed_hosts=_host_tuple(
                    source_raw["allowed_hosts"], f"{prefix}.allowed_hosts"
                ),
                filter=_boolean(source_raw["filter"], f"{prefix}.filter"),
            )
        )
    result = tuple(sources)
    _validate_unique_source_names(result)
    return result


def _validate_unique_source_names(sources: tuple[SourceConfig, ...]) -> None:
    seen: set[str] = set()
    for source in sources:
        normalized = source.name.casefold()
        if normalized in seen:
            raise ConfigError(f"source names must be unique: {source.name}")
        seen.add(normalized)


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _http_url(value: object, name: str) -> str:
    text = _nonempty_string(value, name)
    if not text.startswith(("https://", "http://")):
        raise ConfigError(f"{name} must be an HTTP(S) URL")
    return text


def _source_url(value: object, name: str) -> str:
    text = _http_url(value, name)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{name} is not a valid URL") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ConfigError(f"{name} must contain a host and no credentials")
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


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value


def _enum_value(value: object, name: str, allowed: set[str]) -> str:
    text = _nonempty_string(value, name)
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ConfigError(f"{name} must be one of: {choices}")
    return text


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must be a non-empty array")
    result = tuple(_nonempty_string(entry, name) for entry in value)
    return result


def _host_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not value:
        raise ConfigError(f"{name} must be a non-empty array")
    result: list[str] = []
    seen: set[str] = set()
    for entry in value:
        host = _nonempty_string(entry, name).lower().rstrip(".")
        if (
            "*" in host
            or "://" in host
            or any(character in host for character in "/?#@")
        ):
            raise ConfigError(f"{name} entries must be host names without wildcards")
        try:
            parsed = urlsplit(f"//{host}")
            port = parsed.port
        except ValueError as exc:
            raise ConfigError(f"{name} contains an invalid host") from exc
        if (
            not parsed.hostname
            or parsed.hostname.lower().rstrip(".") != host
            or port
            or not _is_hostname(host)
        ):
            raise ConfigError(f"{name} contains an invalid host")
        if host not in seen:
            seen.add(host)
            result.append(host)
    return tuple(result)


def _is_hostname(value: str) -> bool:
    if len(value) > 253:
        return False
    labels = value.split(".")
    return all(
        label
        and len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        and label.isascii()
        for label in labels
    )


def _host_is_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(
        normalized == allowed or normalized.endswith(f".{allowed}")
        for allowed in allowed_hosts
    )
