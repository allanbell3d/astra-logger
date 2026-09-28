"""Canonical event-timestamp parsing, normalization, and 72-hour freshness.

Canonical form is UTC ISO-8601 with a ``Z`` suffix and seconds precision.
Every accepted ASTRA record carries ``event_ts`` (canonical or None) plus
``timestamp_basis``: ``text`` (explicit parsed timestamp), ``journald``
(native monotonic-free realtime timestamp), ``new_append`` (observed time
for a truly newly appended untimestamped line), or ``inherited`` (a
continuation attached to a timestamped parent).
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
import re
from typing import Any

FRESHNESS_WINDOW_HOURS = 72

_LEADING_TS = re.compile(
    r"^\s*(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[.,]\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)


def to_datetime(canonical: str) -> datetime:
    return datetime.fromisoformat(canonical.replace("Z", "+00:00"))


def system_timezone() -> tzinfo:
    """Return the operating system's current local timezone."""
    return datetime.now().astimezone().tzinfo


def _timezone_or_system(timezone_: tzinfo | None) -> tzinfo:
    return timezone_ or system_timezone()


def canonicalize(text: str, timezone_: tzinfo | None = None) -> str:
    """Render an instant in the host-local timezone, preserving its offset."""
    moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    zone = _timezone_or_system(timezone_)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    rendered = moment.astimezone(zone).isoformat(timespec="seconds")
    return rendered.replace("+00:00", "Z")


def parse_leading(text: str, timezone_: tzinfo | None = None) -> tuple[str | None, str]:
    """Return (canonical_ts | None, status) for a leading textual timestamp."""
    match = _LEADING_TS.match(text or "")
    if not match:
        if not (text or "").strip():
            return None, "missing"
        if re.search(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", text):
            return None, "malformed"
        return None, "missing"
    raw = match.group("ts").replace("T", " ").replace(",", ".")
    tz = match.group("tz")
    if tz == "Z":
        tz = "+00:00"
    try:
        moment = datetime.fromisoformat(f"{raw}{tz or ''}")
    except ValueError:
        return None, "malformed"
    zone = _timezone_or_system(timezone_)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return canonicalize(moment.isoformat(), timezone_=zone), "ok"


def from_journald(fields: dict[str, Any], timezone_: tzinfo | None = None) -> tuple[str | None, str]:
    """Return (canonical_ts | None, status) using the journald native timestamp."""
    value = fields.get("__REALTIME_TIMESTAMP")
    if value in (None, ""):
        return None, "missing"
    try:
        moment = datetime.fromtimestamp(int(value) / 1_000_000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None, "malformed"
    return canonicalize(moment.isoformat(), timezone_=timezone_), "ok"


def as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def fresh(event_ts: str | None, now: datetime) -> bool:
    """True when event_ts is within (now - 72h, now], inclusive at the boundary."""
    if not event_ts:
        return False
    try:
        moment = as_utc(event_ts)
    except ValueError:
        return False
    horizon = as_utc(now) - _timedelta_hours(FRESHNESS_WINDOW_HOURS)
    return horizon <= moment <= as_utc(now)


def _timedelta_hours(hours: int):
    from datetime import timedelta

    return timedelta(hours=hours)


def canonical_now(now: datetime | str | None = None, timezone_: tzinfo | None = None) -> str:
    zone = _timezone_or_system(timezone_)
    if now is None:
        return canonicalize(datetime.now(zone).isoformat(), timezone_=zone)
    if isinstance(now, str):
        return canonicalize(now, timezone_=zone)
    return canonicalize(now.isoformat(), timezone_=zone)
