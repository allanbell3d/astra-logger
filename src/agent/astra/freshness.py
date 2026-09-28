"""Record-level timestamp assignment, freshness gating, and run counters."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from astra.timestamps import canonical_now, fresh, from_journald, parse_leading

PARENT = "parent"
CONTINUATION = "continuation"
STANDALONE = "standalone"

# Accepted explicit bases; anything else means the record has no usable ts.
PARSED_BASES = ("text", "journald")


@dataclass
class FreshnessCounters:
    dropped_old: int = 0
    dropped_missing_timestamp: int = 0
    dropped_malformed_timestamp: int = 0
    kept: int = 0
    basis_new_append: int = 0
    basis_inherited: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "dropped_old": self.dropped_old,
            "dropped_missing_timestamp": self.dropped_missing_timestamp,
            "dropped_malformed_timestamp": self.dropped_malformed_timestamp,
        }


@dataclass
class StagedRecord:
    record: dict
    role: str
    parent_id: str | None = None
    event_ts: str | None = None
    basis: str | None = None
    dropped: str | None = None
    pending_children: list["StagedRecord"] = field(default_factory=list)


def stage_parent_from_line(
    record: dict,
    observed_at: str,
    mode: str,
    counters: FreshnessCounters,
) -> StagedRecord | None:
    """Stage a timestamped-parent-capable record from one raw line.

    mode is 'initial' (freshness gate applies; untimestamped dropped) or
    'incremental' (new standalone lines may use observed time).
    Returns None when the record is discarded; the reason lands in counters.
    """
    canonical, status = parse_leading(record.get("text") or "")
    if status == "ok":
        basis = "text"
    else:
        if mode == "initial":
            counters.dropped_malformed_timestamp += status == "malformed"
            counters.dropped_missing_timestamp += status == "missing"
            return None
        record["event_ts"] = observed_at
        record["timestamp_basis"] = "new_append"
        counters.basis_new_append += 1
        return StagedRecord(record=record, role=STANDALONE, event_ts=observed_at, basis="new_append")
    if not fresh(canonical, _as_datetime(observed_at)):
        counters.dropped_old += 1
        return None
    record["event_ts"] = canonical
    record["timestamp_basis"] = basis
    counters.kept += 1
    return StagedRecord(record=record, role=PARENT, event_ts=canonical, basis=basis)


def stage_parent_from_journald(
    record: dict,
    fields: dict,
    observed_at: str,
    counters: FreshnessCounters,
) -> StagedRecord | None:
    """Stage a journald record; the native journald timestamp is authoritative."""
    canonical, status = from_journald(fields)
    if status != "ok":
        counters.dropped_malformed_timestamp += status == "malformed"
        counters.dropped_missing_timestamp += status == "missing"
        return None
    if not fresh(canonical, _as_datetime(observed_at)):
        counters.dropped_old += 1
        return None
    record["event_ts"] = canonical
    record["timestamp_basis"] = "journald"
    counters.kept += 1
    return StagedRecord(record=record, role=PARENT, event_ts=canonical, basis="journald")


def attach_child(child: dict, parent: StagedRecord, counters: FreshnessCounters) -> StagedRecord:
    """Attach a continuation to its parent: inherit timestamp and fold text in.

    Continuations are not independent events. Their text is appended to the
    parent (still capped at 800 characters) so grouped/LLM output never
    receives a timestamp-less traceback row.
    """
    child["event_ts"] = parent.event_ts
    child["timestamp_basis"] = "inherited"
    child["parent_id"] = parent.record["record_id"]
    extra = str(child.get("text") or "")
    current = str(parent.record.get("text") or "")
    combined = f"{current}\n{extra}" if current else extra
    if len(combined) > 8000:
        parent.record["text"] = combined[:8000]
        parent.record["text_truncated"] = True
    else:
        parent.record["text"] = combined
        parent.record["text_truncated"] = False
    parent.record["original_text_bytes"] = int(parent.record.get("original_text_bytes") or 0) + int(
        child.get("original_text_bytes") or len(extra.encode("utf-8"))
    )
    if "byte_end" in child and child["byte_end"] is not None:
        parent.record["byte_end"] = child["byte_end"]
    counters.basis_inherited += 1
    staged = StagedRecord(
        record=child, role=CONTINUATION, parent_id=parent.record["record_id"],
        event_ts=parent.event_ts, basis="inherited",
    )
    parent.pending_children.append(staged)
    return staged


def finalize_counters(counters: FreshnessCounters, now: datetime | None = None) -> dict:
    payload = counters.as_dict()
    payload["captured_at"] = canonical_now(now)
    return payload


def _as_datetime(observed_at: str) -> datetime:
    from astra.timestamps import as_utc

    return as_utc(observed_at)
