"""Chronological compressed event stream with durable archive rotation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
import json
import os
import re
import tempfile
from typing import Any

from astra.timestamps import as_utc, canonical_now, to_datetime

DEFAULT_RETENTION_HOURS = 144
TEXT_LIMIT = 300


def _default_archive_dir(path: str) -> str:
    parent = os.path.dirname(os.path.abspath(path))
    root = os.path.dirname(parent) if os.path.basename(parent) == "compressed" else parent
    return os.path.join(root, "archive", "compressed")


def _safe_stamp(value: str) -> str:
    return re.sub(r"[^0-9A-Za-zTZ+.-]", "_", value)


def _archive_target(archive_dir: str, first_ts: str, last_ts: str) -> str:
    os.makedirs(archive_dir, exist_ok=True)
    stem = f"compressed-events-{_safe_stamp(first_ts)}-to-{_safe_stamp(last_ts)}"
    target = os.path.join(archive_dir, stem + ".jsonl.gz")
    if not os.path.exists(target):
        return target
    index = 1
    while os.path.exists(os.path.join(archive_dir, f"{stem}.part-{index}.jsonl.gz")):
        index += 1
    return os.path.join(archive_dir, f"{stem}.part-{index}.jsonl.gz")


def _write_gzip_rows(path: str, rows: list[dict[str, Any]]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".compressed-archive.", suffix=".tmp", dir=os.path.dirname(path))
    os.close(fd)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
        with open(tmp, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                json.loads(line)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _extract_compressed_record(enriched: dict[str, Any]) -> dict[str, Any] | None:
    """Format an enriched row into a clean, rich, chronological compressed record.

    Returns None if the record should be ignored (e.g. labeled ignore or a raw continuation).
    """
    severity_info = enriched.get("astra.severity") or {}
    severity = severity_info.get("label") or "watch"
    if severity == "ignore":
        return None
    if enriched.get("record_role") == "continuation":
        return None

    classification = enriched.get("astra.classification") or {}
    provenance = enriched.get("astra.provenance") or {}

    ts = enriched.get("event_ts") or classification.get("event_ts") or enriched.get("ts")
    if not ts or ts == "UNKNOWN":
        return None

    text = enriched.get("text")
    text_snippet = str(text)[:TEXT_LIMIT] if text is not None else ""

    record: dict[str, Any] = {
        "ts": str(ts),
        "host": enriched.get("host"),
        "profile": enriched.get("profile"),
        "domain": classification.get("domain") or "system",
        "kind": classification.get("kind") or "incident",
        "event": classification.get("event"),
        "cause": classification.get("cause"),
        "severity": severity,
        "severity_rule_id": severity_info.get("rule_id"),
        "source_path": provenance.get("source_path") or enriched.get("source_path"),
        "byte_start": provenance.get("byte_start", enriched.get("byte_start", 0)),
        "byte_end": provenance.get("byte_end", enriched.get("byte_end", 0)),
        "text": text_snippet,
    }

    # Include specific operational fields only when present
    for key in ("component", "tool", "target", "operation", "provider", "model", "http_status", "exit_code", "errno", "signal"):
        val = classification.get(key)
        if val is not None and val != "":
            record[key] = val

    exceptions = classification.get("exceptions")
    if exceptions:
        record["exceptions"] = exceptions

    # Clean out empty strings and None
    return {k: v for k, v in record.items() if v is not None and v != "" and v != []}


class CompressedStream:
    """Maintains a bounded active stream and durable gzip archives."""

    def __init__(self, path: str, retention_hours: int = DEFAULT_RETENTION_HOURS,
                 archive_dir: str | None = None) -> None:
        self.path = path
        self.retention_hours = retention_hours
        self.archive_dir = archive_dir or _default_archive_dir(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        os.makedirs(self.archive_dir, exist_ok=True)

    def append_many(self, enriched_rows: list[dict[str, Any]]) -> int:
        """Append enriched records that pass the noise filter to the active stream."""
        to_append: list[dict[str, Any]] = []
        for row in enriched_rows:
            compressed = _extract_compressed_record(row)
            if compressed is not None:
                to_append.append(compressed)

        if not to_append:
            return 0

        to_append.sort(key=lambda r: str(r.get("ts") or ""))
        with open(self.path, "a", encoding="utf-8") as handle:
            for item in to_append:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return len(to_append)

    def rotate_retention(self, now: str | None = None) -> int:
        """Move expired rows to gzip, then rewrite only the active window.

        The rows and their source byte pointers are preserved verbatim in the
        archive.  No event is discarded; the active file is merely rotated so
        the inspect reader remains bounded.
        """
        if not os.path.isfile(self.path):
            return 0

        moment = as_utc(canonical_now(now))
        horizon = moment - timedelta(hours=self.retention_hours)
        kept: list[dict[str, Any]] = []
        expired: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    row = json.loads(line_str)
                    ts_str = row.get("ts")
                    event_dt = to_datetime(ts_str) if ts_str else None
                    if event_dt is not None and event_dt.tzinfo is None:
                        event_dt = event_dt.replace(tzinfo=timezone.utc)
                    (expired if event_dt is not None and event_dt < horizon else kept).append(row)
                except (json.JSONDecodeError, ValueError):
                    kept.append({"raw": line_str})

        if not expired:
            return 0
        expired.sort(key=lambda row: str(row.get("ts") or ""))
        first_ts = str(expired[0].get("ts") or "unknown")
        last_ts = str(expired[-1].get("ts") or "unknown")
        archive_path = _archive_target(self.archive_dir, first_ts, last_ts)
        _write_gzip_rows(archive_path, expired)

        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".compressed.", suffix=".jsonl", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for row in kept:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
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
        return len(expired)

    def prune_retention(self, now: str | None = None) -> int:
        """Compatibility alias: rotate expired rows into gzip, never delete them."""
        return self.rotate_retention(now=now)

    @classmethod
    def read_tail(
        cls,
        path: str,
        window_minutes: int | None = 60,
        since_ts: str | None = None,
        max_bytes: int = 500_000,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read the recent tail of the compressed stream for Free Triage LLM evaluation.

        If since_ts is provided, returns all events with ts > since_ts.
        Otherwise, returns events within window_minutes from now (or the latest event).
        """
        if not os.path.isfile(path):
            return []

        if max_bytes <= 0:
            return []
        rows: list[dict[str, Any]] = []
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            # One preceding byte distinguishes a record boundary from a partial row.
            handle.seek(max(0, start - 1))
            boundary = start == 0 or handle.read(1) == b"\n"
            payload = handle.read(max_bytes)
            if not boundary:
                _, _, payload = payload.partition(b"\n")
            # A writer's incomplete last row is not valid evidence yet.
            if payload and not payload.endswith(b"\n"):
                payload = payload.rpartition(b"\n")[0]
            for line in payload.splitlines():
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass

        if not rows:
            return []

        if since_ts:
            return [r for r in rows if str(r.get("ts") or "") > since_ts]

        if window_minutes is not None:
            if now:
                ref_dt = as_utc(canonical_now(now))
            else:
                last_ts_str = rows[-1].get("ts")
                try:
                    ref_dt = to_datetime(last_ts_str) if last_ts_str else datetime.now(timezone.utc)
                    if ref_dt.tzinfo is None:
                        ref_dt = ref_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    ref_dt = datetime.now(timezone.utc)

            cutoff = ref_dt - timedelta(minutes=window_minutes)
            filtered = []
            for r in rows:
                t_str = r.get("ts")
                if t_str:
                    try:
                        e_dt = to_datetime(t_str)
                        if e_dt.tzinfo is None:
                            e_dt = e_dt.replace(tzinfo=timezone.utc)
                        if e_dt >= cutoff:
                            filtered.append(r)
                    except Exception:
                        pass
            return filtered

        return rows
