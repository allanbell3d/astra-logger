"""Deterministic bounded LLM-ready batches from grouped windows.

Ordering is severity (needs-attention > watch > ignore), then recency
(latest event first), then count (descending). Each batch carries a
metadata header (host, window, group/occurrence counts, severity totals,
batch numbering — per batch and for the whole window) and is capped at
100 groups AND 128 KiB serialized. Batches contain compact grouped rows
only — never raw lines or full enriched records — with text samples cut
to the grouped 128-char cap. Deterministic: identical input yields
identical bytes regardless of input order.
"""
from __future__ import annotations

import json
from typing import Any

MAX_BATCH_GROUPS = 100
MAX_BATCH_BYTES = 128 * 1024
TEXT_LIMIT = 128

SEVERITY_ORDER = {"needs-attention": 0, "watch": 1, "tracked": 2, "ignore": 3}
BATCH_GROUP_FIELDS = (
    "fingerprint", "n", "host", "profile", "event", "cause", "domain", "kind",
    "provider", "model", "platform", "component", "tool", "target", "operation",
    "http_status", "provider_code", "rpc_code", "errno", "exit_code", "signal",
    "events", "causes", "exceptions", "severity", "text", "text_truncated",
    "normalized_text", "first_ts", "last_ts", "source_files",
    "window_start", "window_end",
)


def _order(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Severity, then recency (newest first), then count (descending).

    Three stable passes: later passes dominate, earlier passes break ties.
    """
    ordered = [dict(group) for group in groups]
    ordered.sort(key=lambda group: -int(group.get("n") or 0))
    ordered.sort(key=lambda group: str(group.get("last_ts") or ""), reverse=True)
    ordered.sort(key=lambda group: SEVERITY_ORDER.get(str(group.get("severity") or "watch"), 1))
    return ordered


def _compact_for_llm(group: dict[str, Any]) -> dict[str, Any]:
    compact = {field: group.get(field) for field in BATCH_GROUP_FIELDS}
    text = compact.get("text")
    if isinstance(text, str) and len(text) > TEXT_LIMIT:
        compact["text"] = text[:TEXT_LIMIT]
        compact["text_truncated"] = True
    return compact


def _severity_totals(groups: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for group in groups:
        label = str(group.get("severity") or "watch")
        totals[label] = totals.get(label, 0) + 1
    return dict(sorted(totals.items()))


def build_llm_batches(
    groups: list[dict[str, Any]],
    host: str,
    window: tuple[str | None, str | None] | None = None,
    now: str | None = None,
    max_groups: int = MAX_BATCH_GROUPS,
    max_bytes: int = MAX_BATCH_BYTES,
) -> list[dict[str, Any]]:
    """Order, cap, and pack compact groups into deterministic batches."""
    ordered = _order(groups)
    if not ordered:
        return []
    window_start, window_end = window or (None, None)
    compacts = [_compact_for_llm(group) for group in ordered]
    sizes = [len(json.dumps(compact, ensure_ascii=False)) for compact in compacts]

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for compact, size in zip(compacts, sizes):
        if current and (len(current) + 1 > max_groups or current_bytes + size > max_bytes):
            chunks.append(current)
            current, current_bytes = [], 0
        current.append(compact)
        current_bytes += size
    if current:
        chunks.append(current)

    window_occurrences = sum(int(group.get("n") or 0) for group in ordered)
    window_totals = _severity_totals(ordered)
    batches: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        chunk_occurrences = sum(int(item.get("n") or 0) for item in chunk)
        batches.append({
            "host": host,
            "window_start": window_start,
            "window_end": window_end,
            "group_count": len(chunk),
            "occurrence_count": chunk_occurrences,
            "severity_totals": _severity_totals(chunk),
            "window_group_count": len(ordered),
            "window_occurrence_count": window_occurrences,
            "window_severity_totals": window_totals,
            "batch_index": index + 1,
            "batch_count": len(chunks),
            "groups": chunk,
        })
    return batches
