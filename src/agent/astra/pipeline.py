"""Run the incremental ASTRA enrich+group pipeline."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any, Iterable

from astra.classify import classify_event
from astra.cursor import CursorStore
from astra.enrich import enrich_record
from astra.group import GroupIndex
from astra.inputs import discover_input_files
from astra.jsonl_tail import TailError, read_new_complete_records
from astra.severity import SeverityPolicy, apply_severity


class PipelineCollisionError(ValueError):
    """Raised when an output path would overwrite an input file."""


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _assert_no_collision(inputs: list[str], outputs: list[str]) -> None:
    input_keys = {_real(path) for path in inputs}
    for output in outputs:
        if _real(output) in input_keys:
            raise PipelineCollisionError(f"Refusing to overwrite input file with output: {output}")


def _append_jsonl(path: str, rows: Iterable[dict[str, Any]]) -> int:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    written = 0
    with open(path, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
        handle.flush()
        os.fsync(handle.fileno())
    return written


def _write_journal(path: str, enriched_size: int, cursor_state: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".journal.", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"enriched_size": enriched_size, "cursor_state": cursor_state}, handle, sort_keys=True)
            handle.write("\n")
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


def _recover(journal_path: str, enriched_path: str, grouped_path: str, store: CursorStore, policy: SeverityPolicy) -> None:
    if not os.path.isfile(journal_path):
        return
    with open(journal_path, encoding="utf-8") as handle:
        journal = json.load(handle)
    size = int(journal.get("enriched_size", 0))
    cursor_state = journal.get("cursor_state") or {"files": {}}
    # A surviving journal means the transaction did not commit. Always roll
    # the append back to its pre-transaction byte boundary and restore the
    # last committed cursor. The next run then replays the source rows safely.
    if os.path.isfile(enriched_path):
        with open(enriched_path, "r+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())
    store.restore(cursor_state)
    store.save()
    _rebuild_groups(enriched_path, grouped_path, policy)
    os.remove(journal_path)


def _generated(row: dict[str, Any], key: str) -> dict[str, Any]:
    bucket = row.get("astra.pipeline") or {}
    value = bucket.get(key, row.get(key))
    return value if isinstance(value, dict) else {}


def _rebuild_groups(enriched_path: str, grouped_path: str, policy: SeverityPolicy) -> GroupIndex:
    groups = GroupIndex()
    if os.path.isfile(enriched_path):
        with open(enriched_path, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                classification = _generated(row, "astra.classification")
                severity = apply_severity(classification, policy)
                bucket = row.get("astra.pipeline")
                if "astra.severity" in row and isinstance(bucket, dict):
                    bucket["astra.severity"] = severity
                else:
                    row["astra.severity"] = severity
                groups.add(row)
    if groups.records() or os.path.isfile(grouped_path):
        groups.save(grouped_path)
    return groups


def run_pipeline(
    patterns: Iterable[str], enriched_path: str, grouped_path: str, state_path: str,
    policy_path: str | None = None,
) -> dict[str, Any]:
    inputs = discover_input_files(patterns)
    journal_path = state_path + ".journal"
    _assert_no_collision(inputs, [enriched_path, grouped_path, state_path, journal_path])
    policy = SeverityPolicy.load(policy_path) if policy_path else SeverityPolicy.default()
    store = CursorStore(state_path)
    _recover(journal_path, enriched_path, grouped_path, store, policy)
    enriched_size = os.path.getsize(enriched_path) if os.path.isfile(enriched_path) else 0
    committed_cursor_state = store.snapshot()
    processed = 0
    errors: list[TailError] = []
    new_rows: list[dict[str, Any]] = []
    for source in inputs:
        records, source_errors = read_new_complete_records(source, store)
        errors.extend(source_errors)
        for tailed in records:
            classification = classify_event(tailed.record)
            enriched = enrich_record(tailed.record, classification, policy, tailed.source_path, tailed.byte_start, tailed.byte_end)
            new_rows.append(enriched)
            processed += 1
    _write_journal(journal_path, enriched_size, committed_cursor_state)
    _append_jsonl(enriched_path, new_rows)
    _rebuild_groups(enriched_path, grouped_path, policy)
    store.save()
    os.remove(journal_path)
    return {"processed": processed, "inputs": inputs, "errors": [{"source_path": err.source_path, "byte_start": err.byte_start, "message": err.message} for err in errors]}
