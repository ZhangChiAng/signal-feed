"""Beijing-time helpers used at external business boundaries.

Feed timestamps are normalized to an ISO timestamp by the RSS collector.  A
number of documentation changelogs only publish a day or a month, though.  The
display helper deliberately keeps that precision instead of inventing a time.
"""

import re
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
    value = value.strip()
    if not value:
        return "日期未知"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        # Curated pages sometimes expose locale-formatted dates (for example
        # ``Jul 20`` or ``2025年11月6日``).  They are already validated at the
        # collector boundary; displaying the source precision is safer than
        # manufacturing a timestamp.
        return value
    return to_beijing(parsed).strftime("%Y-%m-%d %H:%M:%S")


def beijing_date(value: datetime) -> str:
    return to_beijing(value).date().isoformat()
