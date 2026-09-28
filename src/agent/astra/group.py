"""Compact fingerprint-grouped records in daily 72-hour windows.

Grouped storage is windowed, not lifetime-cumulative: each row records the
retention window it was assembled under (``window_start``/``window_end``),
groups whose latest event falls outside the window are dropped on reload,
and rows never carry blank ``first_ts``/``last_ts``. Records without an
event timestamp are omitted from grouped storage (they remain in the
enriched evidence).
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from astra.timestamps import as_utc, fresh

COMPACT_FIELDS = (
    "fingerprint", "n", "host", "profile", "event", "cause", "domain", "kind",
    "provider", "model", "fallback_provider", "fallback_model", "platform", "component",
    "tool", "target", "operation", "stream_stage", "http_status", "provider_code",
    "rpc_code", "errno", "exit_code", "signal", "events", "causes", "exceptions",
    "matched_rule_ids", "conflicts", "ts_quality", "text", "text_truncated", "source_files",
    "capture_sig", "severity", "severity_rule_id", "severity_policy_version",
    "normalized_text", "first_ts", "last_ts", "latest_source_ref", "latest_text",
    "window_start", "window_end",
)

DEFAULT_WINDOW_HOURS = 72


def _metadata(enriched: dict[str, Any], key: str) -> dict[str, Any]:
    """Use generated values from the internal bucket when originals collide."""
    bucket = enriched.get("astra.pipeline") or {}
    value = bucket.get(key, enriched.get(key))
    return value if isinstance(value, dict) else {}


def _event_ts(enriched: dict[str, Any]) -> str | None:
    classification = _metadata(enriched, "astra.classification")
    ts = classification.get("event_ts") or enriched.get("event_ts") or enriched.get("ts")
    if ts in (None, "", "UNKNOWN"):
        return None
    return str(ts)


def grouped_record(enriched: dict[str, Any], n: int | None = None) -> dict[str, Any]:
    if n is None:
        n = max(1, int(enriched.get("occurrence_count") or 1))
    fingerprint = _metadata(enriched, "astra.fingerprint")
    classification = _metadata(enriched, "astra.classification")
    severity = _metadata(enriched, "astra.severity")
    provenance = _metadata(enriched, "astra.provenance")
    ts = _event_ts(enriched)
    text = enriched.get("text")
    text = str(text) if text is not None else None
    text_limit = 128
    matched = classification.get("matched_rules") or []

    source_path = provenance.get("source_path") or enriched.get("source_path")
    byte_start = provenance.get("byte_start") if provenance.get("byte_start") is not None else enriched.get("byte_start")
    byte_end = provenance.get("byte_end") if provenance.get("byte_end") is not None else enriched.get("byte_end")
    if source_path and byte_start is not None and byte_end is not None:
        latest_source_ref = f"{source_path}:{byte_start}-{byte_end}"
    else:
        latest_source_ref = source_path or None
    latest_text = text[:250] if text is not None else None

    result = {
        "fingerprint": fingerprint.get("key"), "n": n,
        "host": fingerprint.get("host") or enriched.get("host"),
        "profile": fingerprint.get("profile") or enriched.get("profile"),
        "event": fingerprint.get("event") or classification.get("event"),
        "cause": fingerprint.get("cause") or classification.get("cause"),
        "domain": fingerprint.get("domain") or classification.get("domain"),
        "kind": fingerprint.get("kind") or classification.get("kind"),
        "provider": classification.get("provider"), "model": classification.get("model"),
        "fallback_provider": classification.get("fallback_provider"),
        "fallback_model": classification.get("fallback_model"),
        "platform": classification.get("platform"), "component": classification.get("component"),
        "tool": classification.get("tool"), "target": classification.get("target"),
        "operation": classification.get("operation"), "stream_stage": classification.get("stream_stage"),
        "http_status": classification.get("http_status"), "provider_code": classification.get("provider_code"),
        "rpc_code": classification.get("rpc_code"), "errno": classification.get("errno"),
        "exit_code": classification.get("exit_code"), "signal": classification.get("signal"),
        "events": classification.get("events") or [], "causes": classification.get("causes") or [],
        "exceptions": classification.get("exceptions") or [],
        "matched_rule_ids": [item.get("id") for item in matched if isinstance(item, dict) and item.get("id")],
        "conflicts": classification.get("conflicts") or [],
        "ts_quality": {"basis": classification.get("event_ts_basis")} if classification.get("event_ts_basis") else None,
        "text": text[:text_limit] if text is not None else None,
        "text_truncated": bool(text is not None and len(text) > text_limit),
        "source_files": [provenance.get("source_path")] if provenance.get("source_path") else [],
        "capture_sig": provenance.get("tier0_sig_original", enriched.get("sig")),
        "severity": severity.get("label"), "severity_rule_id": severity.get("rule_id"),
        "severity_policy_version": severity.get("policy_version"),
        "normalized_text": fingerprint.get("normalized_text"), "first_ts": ts, "last_ts": ts,
        "latest_source_ref": latest_source_ref, "latest_text": latest_text,
    }
    if text is None:
        result["text"] = None
        result["text_truncated"] = False
    else:
        # Short samples are kept verbatim; only long ones are cut to the cap.
        result["text"] = text[:text_limit]
        result["text_truncated"] = len(text) > text_limit
    return result


class GroupIndex:
    """Fingerprint-keyed compaction over a rolling retention window."""

    def __init__(self, window_hours: int = DEFAULT_WINDOW_HOURS, now: str | None = None) -> None:
        self.window_hours = window_hours
        self.now = now
        self._groups: dict[str, dict[str, Any]] = {}
        self._window_start: str | None = None
        self._window_end: str | None = None
        if now is not None:
            self._window_start, self._window_end = self._window_bounds()

    def _stamp_window(self, row: dict[str, Any]) -> dict[str, Any]:
        if self._window_start:
            row["window_start"] = self._window_start
            row["window_end"] = self._window_end
        return row

    def _set_window(self, row: dict[str, Any]) -> None:
        start = row.get("window_start")
        end = row.get("window_end")
        if isinstance(start, str) and isinstance(end, str):
            self._window_start = start
            self._window_end = end

    def _window_bounds(self) -> tuple[str, str]:
        """Canonical [now-72h, now] bounds for the current window."""
        if self.now is None:
            return "", ""
        from astra.timestamps import to_datetime
        from datetime import timedelta
        end = as_utc(self.now) if isinstance(self.now, str) else self.now
        start = end - timedelta(hours=self.window_hours)
        return (
            start.isoformat(timespec="seconds").replace("+00:00", "Z"),
            end.astimezone(start.tzinfo).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )

    def add(self, enriched: dict[str, Any]) -> None:
        incoming = grouped_record(enriched)
        key = incoming["fingerprint"]
        if not key:
            raise ValueError("enriched record is missing astra.fingerprint.key")
        ts = incoming.get("last_ts")
        if not ts:
            # No event timestamp: never store a blank-timestamp group.
            return
        if self.now is not None and not fresh(ts, as_utc(self.now) if isinstance(self.now, str) else self.now):
            return
        self._set_window(incoming)
        self._stamp_window(incoming)
        existing = self._groups.get(key)
        if existing is None:
            self._groups[key] = incoming
            return
        existing["n"] += incoming["n"]
        if incoming["first_ts"] and (existing.get("first_ts") is None or incoming["first_ts"] < existing["first_ts"]):
            existing["first_ts"] = incoming["first_ts"]
        if incoming["last_ts"] and (existing.get("last_ts") is None or incoming["last_ts"] >= existing["last_ts"]):
            existing["last_ts"] = incoming["last_ts"]
            if incoming.get("latest_source_ref"):
                existing["latest_source_ref"] = incoming["latest_source_ref"]
            if incoming.get("latest_text"):
                existing["latest_text"] = incoming["latest_text"]
        elif not existing.get("latest_source_ref") and incoming.get("latest_source_ref"):
            existing["latest_source_ref"] = incoming["latest_source_ref"]
        if not existing.get("latest_text") and incoming.get("latest_text"):
            existing["latest_text"] = incoming["latest_text"]
        if not existing.get("source_files"):
            existing["source_files"] = []
        existing["source_files"] = sorted(set(existing["source_files"]) | set(incoming.get("source_files", [])))

    def records(self) -> list[dict[str, Any]]:
        return [self._groups[key] for key in sorted(self._groups)]

    @classmethod
    def load(cls, path: str, window_hours: int = DEFAULT_WINDOW_HOURS,
             now: str | None = None) -> "GroupIndex":
        index = cls(window_hours=window_hours, now=now)
        if not os.path.isfile(path):
            return index
        horizon_seen = False
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                key = row.get("fingerprint")
                if not key:
                    continue
                if now is not None:
                    last = row.get("last_ts")
                    if not last or not fresh(last, as_utc(now) if isinstance(now, str) else now):
                        # Stale beyond the retention window: evict on reload.
                        horizon_seen = True
                        continue
                loaded = {field: row[field] for field in COMPACT_FIELDS if field in row}
                if "events" not in loaded and loaded.get("event"):
                    loaded["events"] = [loaded["event"]]
                if "causes" not in loaded and loaded.get("cause"):
                    loaded["causes"] = [loaded["cause"]]
                loaded["n"] = int(row.get("n") or 0)
                index._groups[key] = loaded
                index._set_window(row)
        return index

    def save(self, path: str) -> None:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".grouped.", suffix=".jsonl", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for row in self.records():
                    cleaned = {k: v for k, v in row.items() if v is not None}
                    if cleaned.get("events") == [cleaned.get("event")] or cleaned.get("events") == []:
                        cleaned.pop("events", None)
                    if cleaned.get("causes") == [cleaned.get("cause")] or cleaned.get("causes") == []:
                        cleaned.pop("causes", None)
                    if cleaned.get("capture_sig"):
                        cleaned.pop("capture_sig", None)
                    handle.write(json.dumps(cleaned, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            if os.name != "nt":
                dirfd = os.open(directory, os.O_RDONLY)
                try: os.fsync(dirfd)
                finally: os.close(dirfd)
        except Exception:
            if os.path.exists(tmp): os.remove(tmp)
            raise
