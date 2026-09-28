"""Discover raw Hermes log sources and apply per-source retention rules.

Sources are <hermes_home>/logs and <hermes_home>/profiles/*/logs with the
approved globs agent.log*, errors.log*, gateway.log*. Each discovered file
yields a SourceFile with host/profile identity derived from its path.

Retention rules (filter_lines_for_source):
- errors.log*: keep every complete nonblank line.
- agent.log*/gateway.log*: keep WARNING/ERROR/CRITICAL (and WARN/ALERT/
  EMERGENCY) plus the traceback/exception continuation context of the last
  kept line; drop ordinary DEBUG/INFO/NOTICE chatter.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

RAW_LOG_BASENAMES = ("agent.log", "errors.log", "gateway.log")

_LEVEL_PREFIX = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s+)?"
    r"(?P<level>DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|ALERT|EMERGENCY)\b[ :]*",
    re.IGNORECASE,
)

# Levels always kept from agent/gateway logs.
KEPT_LEVELS = {"WARNING", "WARN", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"}
_EXCEPTION_CONTINUATION = re.compile(
    r"^(?:\s+File \".*\", line \d+|\s+\^|\s{2,}|Traceback \(|"
    r"(?:[A-Za-z_][\w.]*)(?:Error|Exception):)"
)


@dataclass(frozen=True)
class SourceFile:
    path: str
    source_name: str
    host: str
    profile: str


def default_hermes_patterns(hermes_home: str) -> list[str]:
    """The six approved raw-source globs under a Hermes home directory."""
    patterns: list[str] = []
    for base in RAW_LOG_BASENAMES:
        patterns.append(os.path.join(hermes_home, "logs", base + "*"))
    for base in RAW_LOG_BASENAMES:
        patterns.append(os.path.join(hermes_home, "profiles", "*", "logs", base + "*"))
    return patterns


def source_kind(basename: str) -> str:
    """Map a rotated variant name to its base source kind ('' if unknown)."""
    for base in RAW_LOG_BASENAMES:
        if basename == base or basename.startswith(base + ".") or basename.startswith(base + "-"):
            return base
    return ""


def derive_profile(path: str) -> str:
    """profiles/<name>/logs/... -> <name>; a root logs/ dir -> 'default'."""
    parts = os.path.abspath(path).split(os.sep)
    # layout: ... / profiles / <name> / logs / <basename>
    if len(parts) >= 4 and parts[-3] != "profiles" and parts[-2] == "logs" and parts[-4] == "profiles":
        return parts[-3]
    return "default"


def discover_raw_sources(
    patterns: list[str],
    host: str = "UNKNOWN",
) -> list[SourceFile]:
    seen: set[str] = set()
    sources: list[SourceFile] = []
    for pattern in patterns:
        for match in sorted(glob.glob(pattern)):
            if not os.path.isfile(match):
                continue
            real = os.path.realpath(match)
            if real in seen:
                continue
            seen.add(real)
            sources.append(
                SourceFile(
                    path=real,
                    source_name=os.path.basename(real),
                    host=host,
                    profile=derive_profile(real),
                )
            )
    return sources


def _level_of(text: str) -> str | None:
    match = _LEVEL_PREFIX.match(text)
    return match.group("level").upper() if match else None


def filter_lines_for_source(source_name: str, lines: list[str]) -> list[bool]:
    """Decide per line whether it is retained for this source kind.

    Unlevelled lines are continuation of the preceding levelled record
    (multi-line message or traceback/exception text): they are retained
    only while that record was retained, until the next levelled line.
    """
    kind = source_kind(source_name)
    if kind == "errors.log":
        return [bool(line) for line in lines]
    kept: list[bool] = []
    keep_context = False
    for line in lines:
        level = _level_of(line)
        if level is None:
            if _EXCEPTION_CONTINUATION.match(line):
                keep_context = True
            kept.append(keep_context)
            continue
        keep = level in KEPT_LEVELS
        kept.append(keep)
        keep_context = keep
    return kept
