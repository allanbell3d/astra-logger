"""Host/profile-preserving semantic fingerprints for grouping."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SEMANTIC_FIELDS = (
    "provider",
    "model",
    "fallback_provider",
    "fallback_model",
    "platform",
    "component",
    "domain",
    "tool",
    "target",
    "event",
    "events",
    "cause",
    "causes",
    "exception",
    "exceptions",
    "http_status",
    "provider_code",
    "rpc_code",
    "errno",
    "exit_code",
    "signal",
    "operation",
    "stream_stage",
    "service_result",
    "device",
    "kind",
)

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{15,}\b", re.I),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s,;]+"),
]

LEADING_TS = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s*"
)
ISO_TS = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
SYSLOG_TS = re.compile(r"\b[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b")
TB_FRAME = re.compile(r'File "[^"]*", line(?:\s+\d+)?')
WINDOWS_PATH_BASE = re.compile(r"\b[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)+([^\\/:*?\"<>|\r\n]+)")
POSIX_PATH_BASE = re.compile(r"(?<!:)\/(?:[^\/\s'\"<>|]+\/)+([^\/\s'\"<>|]+)")
WINDOWS_PATH_FULL = re.compile(r"\b[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]+")
POSIX_PATH_FULL = re.compile(r"(?<!:)\/(?:[^\/\s'\"<>|]+\/)*[^\/\s'\"<>|]+")
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.I)
SESSION_ID = re.compile(r"\[\d{8}_\d{6}_[0-9a-fA-F]+\]")
# Cron job identity is stable; only the execution suffix is volatile.
CRON_RUN = re.compile(r"\[(cron_[A-Za-z0-9_-]+)_\d{8}_\d{6}\]")
COOLDOWN_EXPIRY = re.compile(r"(Subsequent auxiliary calls will skip it until )\d{2}:\d{2}:\d{2}")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
LONG_HEX = re.compile(r"\b(?:0x[0-9a-fA-F]{6,}|[0-9a-fA-F]{16,})\b")
DURATION = re.compile(
    r"\b(?:\d+(?:\.\d+)?\s*(?:ms|msec|s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours)|"
    r"in\s+\d+(?:\.\d+)?s|after\s+\d+\s+chunks?)\b",
    re.I,
)
TOKEN_COUNT = re.compile(r"\b\d[\d,]*(?:\s+(?:request|response|input|output|total))?\s+tokens?\b", re.I)
CHAR_COUNT = re.compile(r"\b\d[\d,]*\s+chars?\b", re.I)
PID = re.compile(r"\b(?:pid|PID|process id|ProcessId)\s*[:=]?\s*\d+\b")
SESSION_WORD = re.compile(
    r"\b(?:session|conversation|trace|span|request|run)[_-]?id\s*[:=]\s*['\"]?[A-Za-z0-9_.:-]{8,}\b",
    re.I,
)

HOST_ALIASES = {
    "pi": "RpiClaude",
    "rpiclaude": "RpiClaude",
    "dell": "dell",
}


def canonical_host(raw_host: Any) -> str:
    if not raw_host:
        return "UNKNOWN"
    host = str(raw_host).strip()
    return HOST_ALIASES.get(host.lower(), host)


def normalize_template(text: Any, is_system: bool | None = None) -> str:
    t = "" if text is None else str(text)
    for rx in SECRET_PATTERNS:
        if rx.pattern.startswith("(?i)\\b(api"):
            t = rx.sub(lambda m: f"{m.group(1)}=<REDACTED>", t)
        else:
            t = rx.sub("<REDACTED>", t)

    urls: list[str] = []

    def protect_url(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"__URL_{len(urls) - 1}__"

    t = URL.sub(protect_url, t)
    t = LEADING_TS.sub("", t)
    t = TB_FRAME.sub('File "<F>", line', t)
    if is_system is False:
        t = WINDOWS_PATH_FULL.sub("", t)
        t = POSIX_PATH_FULL.sub("", t)
    else:
        t = WINDOWS_PATH_BASE.sub(r"\1", t)
        t = POSIX_PATH_BASE.sub(r"\1", t)
    t = CRON_RUN.sub(r"[\1_<RUN>]", t)
    t = COOLDOWN_EXPIRY.sub(r"\1<TIME>", t)
    t = SESSION_ID.sub("[<SESSION_ID>]", t)
    t = SESSION_WORD.sub(lambda m: re.sub(r"[:=]\s*['\"]?.*$", "=<ID>", m.group(0)), t)
    t = UUID.sub("<UUID>", t)
    t = LONG_HEX.sub("<HEX>", t)
    t = ISO_TS.sub("<TIMESTAMP>", t)
    t = SYSLOG_TS.sub("<TIMESTAMP>", t)
    t = DURATION.sub("<DURATION>", t)
    t = TOKEN_COUNT.sub("<TOKENS>", t)
    t = CHAR_COUNT.sub("<CHARS>", t)
    t = PID.sub(lambda m: re.sub(r"\d+", "<PID>", m.group(0)), t)
    for idx, url in enumerate(urls):
        t = t.replace(f"__URL_{idx}__", url)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _list_field(classification: dict[str, Any], scalar: str, plural: str) -> list[Any]:
    values = classification.get(plural)
    if isinstance(values, list):
        return values
    value = classification.get(scalar)
    return [value] if value is not None else []


def build_fingerprint(
    classification: dict[str, Any],
    host: Any = None,
    profile: Any = None,
    text: Any = None,
) -> dict[str, Any]:
    host_name = canonical_host(host)
    profile_name = str(profile or "UNKNOWN")
    is_system = classification.get("domain") == "system" or profile_name.startswith("system:")
    source_text = text if text is not None else classification.get("text", "")
    normalized = normalize_template(source_text, is_system=bool(is_system))
    semantic: dict[str, Any] = {}
    for field in SEMANTIC_FIELDS:
        if field == "events":
            semantic[field] = _list_field(classification, "event", "events")
        elif field == "causes":
            semantic[field] = _list_field(classification, "cause", "causes")
        elif field == "exceptions":
            semantic[field] = _list_field(classification, "exception", "exceptions")
        else:
            semantic[field] = classification.get(field)
    # Paths in diagnostic messages identify affected resources. The display
    # template may collapse paths, but the key must not merge two databases or
    # devices merely because they share an exception. Exclude traceback frames.
    resource_text = TB_FRAME.sub('', str(source_text))
    for pattern in SECRET_PATTERNS:
        resource_text = pattern.sub('<REDACTED>', resource_text)
    resources = sorted(set(WINDOWS_PATH_FULL.findall(resource_text) + POSIX_PATH_FULL.findall(resource_text)))
    material = {
        "resources": resources,
        "host": host_name,
        "profile": profile_name,
        "text": normalized,
        "semantic": semantic,
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return {
        "key": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "host": host_name,
        "profile": profile_name,
        "normalized_text": normalized,
        "event": classification.get("event"),
        "events": semantic["events"],
        "cause": classification.get("cause"),
        "causes": semantic["causes"],
        "provider": classification.get("provider"),
        "model": classification.get("model"),
        "domain": classification.get("domain"),
        "kind": classification.get("kind"),
        "platform": classification.get("platform"),
        "component": classification.get("component"),
        "tool": classification.get("tool"),
        "target": classification.get("target"),
        "operation": classification.get("operation"),
    }
