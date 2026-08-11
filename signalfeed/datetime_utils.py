"""Beijing-time helpers used at external business boundaries."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def to_beijing(value: datetime) -> datetime:
    """Return an aware Beijing datetime, treating naive input as UTC."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(BEIJING_TIMEZONE)


def beijing_isoformat(value: datetime) -> str:
    return to_beijing(value).isoformat(timespec="seconds")


def format_beijing_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO publication date: {value!r}") from exc
    return to_beijing(parsed).strftime("%Y-%m-%d %H:%M:%S")


def beijing_date(value: datetime) -> str:
    return to_beijing(value).date().isoformat()
