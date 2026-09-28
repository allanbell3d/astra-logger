"""Direct bounded raw-source ingestion into ASTRA enrichment and groups."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import tempfile
from typing import Any, Callable

from astra.classify import classify_event
from astra.cursor import CursorStore
from astra.enrich import enrich_record
from astra.freshness import (
    FreshnessCounters,
    attach_child,
    stage_parent_from_journald,
    stage_parent_from_line,
)
from astra.group import GroupIndex
from astra.journald_source import (
    JournaldEntry,
    journalctl_reader,
    read_journald_entries,
    save_journald_cursor,
)
from astra.raw_sources import default_hermes_patterns, discover_raw_sources, filter_lines_for_source
from astra.raw_tail import RawLine, read_new_raw_lines
from astra.severity import SeverityPolicy
from astra.storage import EnrichedStore, apply_retention, scan_enriched_segments
from astra.timestamps import canonical_now
from astra.compressed import CompressedStream
from astra.llm_batch import build_llm_batches

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[.,]\d+)?")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _line_ts(text: str) -> str:
    match = _TS.match(text)
    return match.group(1).replace(" ", "T") if match else "UNKNOWN"


def _journal_ts(fields: dict[str, Any]) -> str:
    try:
        return datetime.fromtimestamp(
            int(fields["__REALTIME_TIMESTAMP"]) / 1_000_000, tz=timezone.utc
        ).isoformat(timespec="seconds")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return "UNKNOWN"


def _atomic_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".astra-raw.", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _append(path: str, rows: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _rebuild_groups(enriched_path: str, grouped_path: str) -> None:
    groups = GroupIndex()
    if os.path.isfile(enriched_path):
        with open(enriched_path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    groups.add(json.loads(line))
    groups.save(grouped_path)


def _restore_transaction(journal_path: str, enriched_path: str, grouped_path: str,
                         store: CursorStore, journal_state_path: str,
                         compressed_path: str | None = None) -> None:
    if not os.path.isfile(journal_path):
        return
    with open(journal_path, encoding="utf-8") as handle:
        txn = json.load(handle)
    if os.path.isfile(enriched_path):
        with open(enriched_path, "r+b") as handle:
            handle.truncate(int(txn["enriched_size"]))
            handle.flush()
            os.fsync(handle.fileno())
    if compressed_path and os.path.isfile(compressed_path) and txn.get("compressed_size") is not None:
        with open(compressed_path, "r+b") as handle:
            handle.truncate(int(txn["compressed_size"]))
            handle.flush()
            os.fsync(handle.fileno())
    store.restore(txn["file_state"])
    store.save()
    prior = txn.get("journal_state")
    if prior is None:
        if os.path.exists(journal_state_path):
            os.remove(journal_state_path)
    else:
        _atomic_json(journal_state_path, prior)
    _rollback_store(txn, txn.get("enriched_size"))
    _rollback_batches(txn)
    _rebuild_groups(enriched_path, grouped_path)
    os.remove(journal_path)


def _store_day_snapshot(store_dir: str | None) -> dict[str, int]:
    """Per-day plaintext byte sizes of store segments before this run."""
    if not store_dir:
        return {}
    sizes: dict[str, int] = {}
    for segment in scan_enriched_segments(store_dir):
        sizes[segment.path] = segment.size
    return sizes


def _batch_file_snapshot(batch_dir: str | None) -> dict[str, int]:
    if not batch_dir or not os.path.isdir(batch_dir):
        return {}
    return {
        name: os.path.getsize(os.path.join(batch_dir, name))
        for name in os.listdir(batch_dir)
        if name.endswith(".json")
    }


def _rollback_store(txn: dict[str, Any], enriched_size: int) -> None:
    """Truncate store segments back to their pre-run sizes on recovery."""
    sizes = txn.get("store_days") or {}
    for path, size in sizes.items():
        if not os.path.isfile(path):
            continue
        current = os.path.getsize(path)
        if current < int(size):
            continue  # segment was rotated away or replaced; leave it
        if current == int(size):
            continue
        with open(path, "r+b") as handle:
            handle.truncate(int(size))
            handle.flush()
            os.fsync(handle.fileno())


def _rollback_batches(txn: dict[str, Any]) -> None:
    """Rewind batch files to pre-run contents (or drop new files)."""
    sizes = txn.get("batch_files") or {}
    directory = os.path.dirname(next(iter(sizes), "")) if sizes else None
    if directory and os.path.isdir(directory):
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if name.endswith(".json") and name not in sizes:
                os.remove(path)
    for name, size in sizes.items():
        path = os.path.join(directory, name) if directory else name
        if os.path.isfile(path) and os.path.getsize(path) > int(size):
            with open(path, "r+b") as handle:
                handle.truncate(int(size))
                handle.flush()
                os.fsync(handle.fileno())


def _write_llm_batches(batch_dir: str, groups: "GroupIndex", host: str,
                       observed_at: str) -> None:
    """Regenerate deterministic batch files for the current window.

    A crash mid-write leaves a truncated JSON file, which the next run's
    regeneration overwrites; the recovery journal rewinds to the snapshot.
    """
    os.makedirs(batch_dir, exist_ok=True)
    window = (groups._window_start, groups._window_end)
    day = (window[1] or observed_at)[:10].replace("-", "")
    batches = build_llm_batches(groups.records(), host=host, window=window)
    for batch in batches:
        path = os.path.join(batch_dir, f"llm-batch-{day}-{batch['batch_index']:03d}.json")
        fd, tmp = tempfile.mkstemp(prefix=".llmbatch.", suffix=".json", dir=batch_dir, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(batch, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise


def _compact_add(bucket: OrderedDict[tuple[str, ...], dict[str, Any]], key: tuple[str, ...],
                 record: dict[str, Any]) -> None:
    existing = bucket.get(key)
    if existing is None:
        record["occurrence_count"] = 1
        record["first_seen"] = record.get("ts") or "UNKNOWN"
        record["last_seen"] = record.get("ts") or "UNKNOWN"
        record["duplicate_compaction"] = "exact-host-profile-source-content"
        bucket[key] = record
        return
    existing["occurrence_count"] += 1
    existing["last_seen"] = record.get("ts") or existing["last_seen"]
    if "byte_end" in record:
        existing["byte_end"] = record["byte_end"]


def _is_continuation_text(text: str) -> bool:
    from astra.raw_sources import _EXCEPTION_CONTINUATION

    return bool(_EXCEPTION_CONTINUATION.match(text or ""))


def _source_is_initial(store: CursorStore, path: str, initial_discovery: bool | None) -> bool:
    """A source is on initial semantics when discovery is forced or no
    committed cursor exists yet (first sight / rotation / truncation reset).
    Must be consulted before the first tail read commits a cursor for path."""
    if initial_discovery is not None:
        return initial_discovery
    return store.get(path) is None


def _raw_record(host: str, profile: str, line: RawLine, captured_at: str) -> dict[str, Any]:
    return {
        "ts": _line_ts(line.text), "captured_at": captured_at,
        "host": host, "profile": profile, "file": line.source_name,
        "source_type": "hermes_log", "text": line.text,
        "text_truncated": line.text_truncated,
        "original_text_bytes": line.original_text_bytes,
        "source_path": line.source_path,
        "source_identity": {"inode": line.inode, "dev": line.dev, "content_hash": line.content_hash},
        "byte_start": line.byte_start,
        "byte_end": line.byte_end,
    }


def _journal_record(host: str, entry: JournaldEntry, captured_at: str) -> dict[str, Any]:
    fields = entry.retained()
    message = str(fields.get("MESSAGE") or "")
    encoded = message.encode("utf-8")
    fields["MESSAGE"] = message[:800]
    unit = fields.get("_SYSTEMD_UNIT") or fields.get("_SYSTEMD_USER_UNIT") \
        or fields.get("SYSLOG_IDENTIFIER") or fields.get("_COMM") or "kernel-or-unknown"
    return {
        "ts": _journal_ts(fields), "captured_at": captured_at,
        "host": host, "profile": f"system:{unit}",
        "file": f"journald(p{fields.get('PRIORITY', '?')})",
        "source_type": "journald", "text": message[:800],
        "text_truncated": len(message) > 800,
        "original_text_bytes": len(encoded), "journal": fields,
    }


def run_raw_pipeline(
    hermes_home: str,
    host: str,
    enriched_path: str,
    grouped_path: str,
    state_path: str,
    policy_path: str | None = None,
    work_ceiling_per_file: int = 2_000_000,
    journal_reader: Callable[..., list[JournaldEntry]] | None = journalctl_reader,
    journal_batch_limit: int = 500,
    now: str | None = None,
    initial_discovery: bool | None = None,
    store_dir: str | None = None,
    llm_batch_dir: str | None = None,
    compressed_path: str | None = None,
) -> dict[str, Any]:
    """Process one bounded increment from raw Hermes logs and journald.

    ``now`` (canonical UTC or ISO-8601) sets the freshness reference time so
    runs are testable and replayable; it defaults to the current time.
    ``initial_discovery`` forces first-run semantics for every source; by
    default only sources without a committed cursor are treated as initial.
    ``store_dir`` enables the rotating enriched-evidence store (10 MiB /
    UTC-day rotation, gzip closed segments, 72h + 250 MiB retention).
    ``llm_batch_dir`` enables deterministic bounded LLM-ready batch files.
    ``compressed_path`` enables the Output 2 chronological compressed stream.
    """
    observed_at = canonical_now(now)
    policy = SeverityPolicy.load(policy_path) if policy_path else SeverityPolicy.default()
    store = CursorStore(state_path)
    journal_state_path = state_path + ".journald"
    journal_path = state_path + ".journal"
    _restore_transaction(journal_path, enriched_path, grouped_path, store, journal_state_path, compressed_path)
    committed_state = store.snapshot()
    prior_journal_state = None
    if os.path.isfile(journal_state_path):
        try:
            with open(journal_state_path, encoding="utf-8") as handle:
                prior_journal_state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            prior_journal_state = None

    compacted: OrderedDict[tuple[str, ...], dict[str, Any]] = OrderedDict()
    counters = FreshnessCounters()
    source_records = 0
    backlog_bytes = 0
    errors: list[str] = []
    staged_parents: list[Any] = []
    sources = discover_raw_sources(default_hermes_patterns(hermes_home), host=host)

    def _stamp(record: dict[str, Any]) -> dict[str, Any]:
        record["record_id"] = hashlib.sha256(
            json.dumps(
                [record.get("source_path") or "journald", record.get("byte_start"),
                 record.get("byte_end"), record.get("text"),
                 (record.get("journal") or {}).get("__CURSOR"),
                 record.get("ts")],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        record["record_role"] = ""
        record["parent_id"] = None
        return record

    for source in sources:
        try:
            mode = "initial" if _source_is_initial(store, os.path.realpath(source.path), initial_discovery) else "incremental"
            result = read_new_raw_lines(source.path, store, work_ceiling=work_ceiling_per_file)
        except OSError as exc:
            errors.append(f"{source.path}: {exc}")
            continue
        backlog_bytes += result.backlog_bytes
        flags = filter_lines_for_source(source.source_name, [line.text for line in result.lines])
        current_parent: Any = None
        for line, keep in zip(result.lines, flags):
            if not keep:
                current_parent = None
                continue
            record = _stamp(_raw_record(host, source.profile, line, observed_at))
            if record["ts"] == "UNKNOWN" and _is_continuation_text(line.text):
                if current_parent is not None:
                    attach_child(record, current_parent, counters)
                    continue
                counters.dropped_missing_timestamp += 1
                continue
            source_records += 1
            staged = stage_parent_from_line(record, observed_at, mode, counters)
            if staged is None:
                current_parent = None
                continue
            record["record_role"] = staged.role
            staged_parents.append(staged)
            current_parent = staged

    journal_result = None
    if journal_reader is not None:
        try:
            journal_result = read_journald_entries(
                journal_reader, journal_state_path, batch_limit=journal_batch_limit, persist=False
            )
            for entry in journal_result.entries:
                record = _stamp(_journal_record(host, entry, observed_at))
                source_records += 1
                staged = stage_parent_from_journald(record, entry.retained(), observed_at, counters)
                if staged is not None:
                    record["record_role"] = staged.role
                    staged_parents.append(staged)
        except (OSError, RuntimeError) as exc:
            errors.append(f"journald: {exc}")

    accepted: list[dict[str, Any]] = [staged.record for staged in staged_parents]
    for staged in staged_parents:
        record = staged.record
        key = (
            host, record.get("profile") or "", record.get("source_path") or "journald",
            record.get("record_id") or record.get("ts"),
        )
        compacted[key] = record

    enriched_rows: list[dict[str, Any]] = []
    for record in compacted.values():
        classification = classify_event(record)
        provenance_path = str(record.get("source_path") or "journald")
        enriched_rows.append(enrich_record(
            record, classification, policy, provenance_path,
            int(record.get("byte_start") or 0), int(record.get("byte_end") or 0),
        ))

    if not enriched_rows:
        store.save()
        if journal_result and journal_result.cursor:
            save_journald_cursor(journal_state_path, journal_result.cursor)
        return {
            "processed": 0, "source_records": source_records,
            "inputs": len(sources), "backlog_bytes": backlog_bytes, "errors": errors,
            "dropped_old": counters.dropped_old,
            "dropped_missing_timestamp": counters.dropped_missing_timestamp,
            "dropped_malformed_timestamp": counters.dropped_malformed_timestamp,
            "captured_at": observed_at,
        }

    enriched_size = os.path.getsize(enriched_path) if os.path.isfile(enriched_path) else 0
    compressed_size = os.path.getsize(compressed_path) if compressed_path and os.path.isfile(compressed_path) else 0
    _atomic_json(journal_path, {
        "enriched_size": enriched_size, "file_state": committed_state,
        "journal_state": prior_journal_state,
        "store_days": _store_day_snapshot(store_dir),
        "batch_files": _batch_file_snapshot(llm_batch_dir),
        "compressed_size": compressed_size,
    })
    if store_dir is None:
        _append(enriched_path, enriched_rows)
    else:
        evidence_store = EnrichedStore(store_dir, now=observed_at)
        evidence_store.append_many(enriched_rows)
        retention = apply_retention(store_dir, now=observed_at)
    if compressed_path is not None:
        compressed_stream = CompressedStream(compressed_path)
        compressed_stream.append_many(enriched_rows)
        compressed_stream.prune_retention(now=observed_at)
    groups = GroupIndex.load(grouped_path, now=observed_at)
    for row in enriched_rows:
        groups.add(row)
    groups.save(grouped_path)
    if llm_batch_dir is not None:
        _write_llm_batches(llm_batch_dir, groups, host, observed_at)
    store.save()
    if journal_result and journal_result.cursor:
        save_journald_cursor(journal_state_path, journal_result.cursor)
    os.remove(journal_path)
    return {
        "processed": len(enriched_rows), "source_records": source_records,
        "inputs": len(sources), "backlog_bytes": backlog_bytes, "errors": errors,
        "dropped_old": counters.dropped_old,
        "dropped_missing_timestamp": counters.dropped_missing_timestamp,
        "dropped_malformed_timestamp": counters.dropped_malformed_timestamp,
        "captured_at": observed_at,
        **({"retention": retention} if store_dir is not None else {}),
    }
