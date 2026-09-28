"""Apply an editable JSON severity policy to classified events."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

DEFAULT_POLICY: dict[str, Any] = {
    "version": "v4.1-table",
    "default": "watch",
    "rules": [
        {"id": "continuation", "when": {"kind": ["continuation"]}, "label": "ignore"},
        {"id": "observation", "when": {"kind": ["observation"]}, "label": "ignore"},
        {
            "id": "typing-rate-limit",
            "when": {"event": ["platform.operation_rate_limited"]},
            "label": "ignore",
        },
        {
            "id": "severe-causes",
            "when": {
                "causes_any": [
                    "usage_quota_exhausted",
                    "credit_balance_exhausted",
                    "allocation_quota_exhausted",
                    "database_corrupt",
                    "memory_exhausted",
                    "oom_killed_process",
                    "permission_denied",
                    "missing_access",
                    "auth_invalid",
                    "oauth_refresh_invalid",
                ]
            },
            "label": "needs-attention",
        },
        {
            "id": "oom-events",
            "when": {"event": ["system.oom_invoked", "service.oom_killed", "process.oom_killed"]},
            "label": "needs-attention",
        },
        {
            "id": "critical-log-level",
            "when": {"log_level": ["CRITICAL", "ALERT", "EMERGENCY"]},
            "label": "needs-attention",
        },
        {
            "id": "error-stream-or-network",
            "when": {
                "log_level": ["ERROR"],
                "cause": ["stream_stalled", "dns_resolution_failed", "connection_refused"],
            },
            "label": "needs-attention",
        },
    ],
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _rule_matches(when: dict[str, Any], classification: dict[str, Any]) -> bool:
    for key, expected in when.items():
        wanted = _as_list(expected)
        if key == "causes_any":
            have = set(_as_list(classification.get("causes")))
            if classification.get("cause") is not None:
                have.add(classification.get("cause"))
            if not have.intersection(wanted):
                return False
            continue
        actual = classification.get(key)
        if actual not in wanted:
            return False
    return True


@dataclass(frozen=True)
class SeverityPolicy:
    version: str
    default_label: str
    rules: tuple[dict[str, Any], ...]

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "SeverityPolicy":
        if not isinstance(data, dict):
            raise ValueError("severity policy must be a JSON object")
        rules = data.get("rules")
        if not isinstance(rules, list):
            raise ValueError("severity policy.rules must be a list")
        default_label = data.get("default", "watch")
        if default_label not in ("ignore", "watch", "needs-attention"):
            raise ValueError(f"unsupported default severity: {default_label}")
        return cls(
            version=str(data.get("version") or "unversioned"),
            default_label=str(default_label),
            rules=tuple(rules),
        )

    @classmethod
    def default(cls) -> "SeverityPolicy":
        return cls.from_mapping(DEFAULT_POLICY)

    @classmethod
    def load(cls, path: str) -> "SeverityPolicy":
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return cls.from_mapping(data)

    def to_mapping(self) -> dict[str, Any]:
        return {"version": self.version, "default": self.default_label, "rules": list(self.rules)}


def apply_severity(classification: dict[str, Any], policy: SeverityPolicy) -> dict[str, Any]:
    for index, rule in enumerate(policy.rules):
        when = rule.get("when") or {}
        if not isinstance(when, dict):
            continue
        if _rule_matches(when, classification):
            label = rule.get("label", policy.default_label)
            return {
                "label": label,
                "rule_id": rule.get("id") or f"rule-{index}",
                "policy_version": policy.version,
            }
    return {
        "label": policy.default_label,
        "rule_id": "default",
        "policy_version": policy.version,
    }


def write_sample_policy(path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(DEFAULT_POLICY, handle, indent=2)
        handle.write("\n")
