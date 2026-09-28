"""Chronological enriched-evidence storage with 12-hour buckets.

Active buckets remain plain JSONL for the current 72-hour working window.
Expired or cap-displaced buckets are compressed into the sibling
``logs-watch/archive/enriched`` directory instead of being discarded.  The
archive is the durable record; the active directory stays bounded for capture
and review.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
import gzip
import json
import os
import re
import shutil
import tempfile

from astra.timestamps import as_utc, canonical_now, system_timezone, to_datetime

BUCKET_HOURS = 12
RETENTION_HOURS = 72
MAX_TOTAL_BYTES = 250 * 1024 * 1024
ACTIVE_SUFFIX = ".jsonl"
GZIP_SUFFIX = ".jsonl.gz"


@dataclass(frozen=True)
class Segment:
    path: str
    day: str
    mtime: float
    size: int
    active: bool
    record_bytes: int = 0


def _timezone_or_system(timezone_: tzinfo | None) -> tzinfo:
    return timezone_ or system_timezone()


def _utcnow(now: str | None, timezone_: tzinfo | None = None) -> datetime:
    return as_utc(canonical_now(now, timezone_=timezone_)) if now else datetime.now(timezone.utc)


def _day_stamp(moment: datetime, timezone_: tzinfo | None = None) -> str:
    return moment.astimezone(_timezone_or_system(timezone_)).strftime("%Y%m%d")


def _event_moment(record: dict, fallback: datetime, timezone_: tzinfo | None = None) -> datetime:
    value = record.get("event_ts")
    if isinstance(value, str) and value:
        try:
            moment = to_datetime(value)
            return moment if moment.tzinfo else moment.replace(tzinfo=_timezone_or_system(timezone_))
        except (TypeError, ValueError):
            pass
    return fallback


def _bucket_path(store_dir: str, moment: datetime, timezone_: tzinfo | None = None) -> str:
    local = moment.astimezone(_timezone_or_system(timezone_))
    hour = 0 if local.hour < BUCKET_HOURS else BUCKET_HOURS
    return os.path.join(store_dir, f"enriched-{local.strftime('%Y%m%d')}-{hour:02d}{ACTIVE_SUFFIX}")


def _record_key(record: dict) -> tuple[str, str, str, int, str]:
    """Stable event-time ordering, with provenance as deterministic tie-breakers."""
    return (
        str(record.get("event_ts") or ""),
        str(record.get("captured_at") or ""),
        str(record.get("source_path") or "journald"),
        int(record.get("byte_start") or 0),
        str(record.get("record_id") or ""),
    )


def _read_rows(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _atomic_rows(path: str, rows: list[dict]) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".astra-enriched.", suffix=".jsonl", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            dirfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _default_archive_dir(store_dir: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(store_dir)), "archive", "enriched")


def _archive_target(archive_dir: str, filename: str) -> str:
    os.makedirs(archive_dir, exist_ok=True)
    candidate = os.path.join(archive_dir, filename)
    if not os.path.exists(candidate):
        return candidate
    stem, suffix = os.path.splitext(candidate)
    index = 1
    while os.path.exists(f"{stem}.part-{index}{suffix}"):
        index += 1
    return f"{stem}.part-{index}{suffix}"


def _archive_file(path: str, archive_dir: str) -> str:
    """Durably gzip one segment, then remove only the archived source copy."""
    name = os.path.basename(path)
    if name.endswith(ACTIVE_SUFFIX):
        name += ".gz"
    target = _archive_target(archive_dir, name)
    fd, tmp = tempfile.mkstemp(prefix=".astra-archive.", suffix=".tmp", dir=archive_dir)
    os.close(fd)
    try:
        if path.endswith(GZIP_SUFFIX):
            shutil.copyfile(path, tmp)
        else:
            with open(path, "rb") as source, gzip.open(tmp, "wb", compresslevel=6) as archive:
                shutil.copyfileobj(source, archive)
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        # Validate the archive before removing the active copy.
        with gzip.open(target, "rb") as archive:
            while archive.read(1024 * 1024):
                pass
        os.remove(path)
        return target
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _archive_rows(rows: list[dict], archive_dir: str, filename: str) -> str:
    """Write boundary rows to a durable gzip archive before rewriting a bucket."""
    target = _archive_target(archive_dir, filename)
    fd, tmp = tempfile.mkstemp(prefix=".astra-archive.", suffix=".tmp", dir=archive_dir)
    os.close(fd)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as archive:
            for row in rows:
                archive.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                archive.write("\n")
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        with gzip.open(target, "rt", encoding="utf-8") as archive:
            for line in archive:
                json.loads(line)
        return target
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


class EnrichedStore:
    """Store events in readable, chronological 12-hour host-local buckets."""

    def __init__(self, store_dir: str, now: str | None = None, timezone_: tzinfo | None = None) -> None:
        self.store_dir = store_dir
        os.makedirs(store_dir, exist_ok=True)
        self._now = now
        self._timezone = _timezone_or_system(timezone_)

    def set_now(self, now: str | None) -> None:
        self._now = now

    def append(self, record: dict) -> None:
        self.append_many([record])

    def append_many(self, records: list[dict]) -> None:
        """Merge one pipeline increment into each affected bucket atomically."""
        by_path: dict[str, list[dict]] = {}
        fallback = _utcnow(self._now, timezone_=self._timezone)
        for record in records:
            path = _bucket_path(self.store_dir, _event_moment(record, fallback, timezone_=self._timezone), timezone_=self._timezone)
            by_path.setdefault(path, []).append(record)
        for path, additions in by_path.items():
            rows = _read_rows(path)
            rows.extend(additions)
            rows.sort(key=_record_key)
            _atomic_rows(path, rows)

    @property
    def active_path(self) -> str:
        return _bucket_path(self.store_dir, _utcnow(self._now, timezone_=self._timezone), timezone_=self._timezone)

    def rotate_closed(self) -> None:
        """Compatibility no-op: time buckets replace size/day rotation."""


def _gzip_record_bytes(path: str) -> int:
    try:
        with open(path, "rb") as handle:
            handle.seek(-4, os.SEEK_END)
            return int.from_bytes(handle.read(4), "little")
    except (OSError, ValueError):
        return 0


def _day_of(name: str) -> str:
    stem = name[len("enriched-"):] if name.startswith("enriched-") else name
    return stem.split("-", 1)[0].split(".", 1)[0]


def scan_enriched_segments(store_dir: str, now: str | None = None) -> list[Segment]:
    """List current and retired segments oldest-first.

    A segment from today's UTC day is active; prior buckets are closed for
    retention purposes, even though delayed-but-fresh source records can be
    rewritten into their appropriate historical bucket.
    """
    if not os.path.isdir(store_dir):
        return []
    today = _day_stamp(_utcnow(now))
    segments: list[Segment] = []
    for name in sorted(os.listdir(store_dir)):
        if not name.startswith("enriched-"):
            continue
        path = os.path.join(store_dir, name)
        if name.endswith(GZIP_SUFFIX):
            size = os.path.getsize(path)
            segments.append(Segment(path, _day_of(name), os.path.getmtime(path), size, False,
                                    record_bytes=_gzip_record_bytes(path)))
        elif name.endswith(ACTIVE_SUFFIX):
            size = os.path.getsize(path)
            day = _day_of(name)
            segments.append(Segment(path, day, os.path.getmtime(path), size, day == today,
                                    record_bytes=size))
    return sorted(segments, key=lambda segment: (segment.day, segment.path))


def close_stale_plaintext(store_dir: str, now: str | None = None) -> None:
    """Retained as a compatibility no-op; current evidence stays readable."""


def _close_time(segment: Segment) -> datetime:
    try:
        day = datetime.strptime(segment.day, "%Y%m%d").replace(tzinfo=timezone.utc)
        return day + timedelta(days=1)
    except ValueError:
        return datetime.fromtimestamp(segment.mtime, tz=timezone.utc)


def _is_current_bucket(path: str) -> bool:
    return bool(re.fullmatch(r"enriched-\d{8}-(?:00|12)\.jsonl", os.path.basename(path)))


def _prune_expired_rows(path: str, horizon: datetime, archive_dir: str) -> bool:
    """Archive expired rows before rewriting a boundary bucket."""
    rows = _read_rows(path)
    kept: list[dict] = []
    expired_rows: list[dict] = []
    for row in rows:
        value = row.get("event_ts")
        try:
            expired = isinstance(value, str) and as_utc(value) < horizon
        except (TypeError, ValueError):
            expired = False
        (expired_rows if expired else kept).append(row)
    if not expired_rows:
        return False

    stamp = horizon.strftime("%Y%m%dT%H%M%SZ")
    _archive_rows(expired_rows, archive_dir, f"{os.path.splitext(os.path.basename(path))[0]}-expired-before-{stamp}.jsonl.gz")
    if not kept:
        os.remove(path)
        return True
    kept.sort(key=_record_key)
    _atomic_rows(path, kept)
    return False


def apply_retention(store_dir: str, now: str | None = None,
                    max_total_bytes: int = MAX_TOTAL_BYTES,
                    archive_dir: str | None = None) -> dict[str, int]:
    """Keep the working store bounded without discarding evidence.

    Closed segments and cap-displaced segments are validated, gzip-compressed,
    and moved under the sibling archive directory.  Only the archived active
    copy is removed from the working directory.
    """
    moment = _utcnow(now)
    horizon = moment - timedelta(hours=RETENTION_HOURS)
    archive_dir = archive_dir or _default_archive_dir(store_dir)
    archived_old = 0
    for segment in scan_enriched_segments(store_dir, now=now):
        if not segment.active and _close_time(segment) <= horizon:
            _archive_file(segment.path, archive_dir)
            archived_old += 1

    # A 12-hour bucket can straddle the rolling horizon. Archive only its
    # expired rows, retaining the fresh rows in the active working bucket.
    archived_boundary = 0
    for segment in scan_enriched_segments(store_dir, now=now):
        if not _is_current_bucket(segment.path):
            continue
        before = os.path.exists(segment.path)
        removed_file = _prune_expired_rows(segment.path, horizon, archive_dir)
        if before and (removed_file or not os.path.exists(segment.path)):
            archived_boundary += 1

    remaining = [s for s in scan_enriched_segments(store_dir, now=now) if not s.active]
    total = sum(s.record_bytes for s in remaining)
    archived_cap = 0
    for segment in sorted(remaining, key=lambda s: (s.day, s.mtime, s.path)):
        if total <= max_total_bytes:
            break
        _archive_file(segment.path, archive_dir)
        total -= segment.record_bytes
        archived_cap += 1
    return {
        "removed_old": 0,
        "removed_cap": 0,
        "archived_old": archived_old,
        "archived_boundary": archived_boundary,
        "archived_cap": archived_cap,
    }


def total_closed_bytes(store_dir: str) -> int:
    return sum(segment.size for segment in scan_enriched_segments(store_dir) if not segment.active)
