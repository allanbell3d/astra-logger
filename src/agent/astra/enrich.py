"""Assemble lossless enriched records with namespaced ASTRA fields."""
from __future__ import annotations

from typing import Any

from astra.fingerprint import build_fingerprint
from astra.severity import SeverityPolicy, apply_severity

NAMESPACED = (
    "astra.classification",
    "astra.fingerprint",
    "astra.severity",
    "astra.provenance",
)


def _set_namespaced(enriched: dict[str, Any], original: dict[str, Any], key: str, value: Any) -> None:
    if key in original:
        bucket = enriched.setdefault("astra.pipeline", {})
        bucket[key] = value
        return
    enriched[key] = value


def enrich_record(
    raw: dict[str, Any],
    classification: dict[str, Any],
    policy: SeverityPolicy,
    source_path: str,
    byte_start: int,
    byte_end: int,
) -> dict[str, Any]:
    classification_view = {
        key: value
        for key, value in classification.items()
        if key not in ("text",)
    }
    if raw.get("event_ts"):
        classification_view["event_ts"] = raw["event_ts"]
    severity = apply_severity(classification, policy)
    fingerprint = build_fingerprint(
        classification,
        host=raw.get("host"),
        profile=raw.get("profile"),
        text=raw.get("text", ""),
    )
    provenance = {
        "source_path": source_path,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "rule_version": classification.get("rule_version"),
        "tier0_sig_original": raw.get("tier0_sig_original", raw.get("sig")),
        "severity_policy_version": severity.get("policy_version"),
    }
    enriched = dict(raw)
    _set_namespaced(enriched, raw, "astra.classification", classification_view)
    _set_namespaced(enriched, raw, "astra.fingerprint", fingerprint)
    _set_namespaced(enriched, raw, "astra.severity", severity)
    _set_namespaced(enriched, raw, "astra.provenance", provenance)
    return enriched
