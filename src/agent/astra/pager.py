"""Deterministic pager helpers: alarms, bookmarks, source slices."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INSPECT_PROMPT_RULES = (
    "Required on every mentioned event: agent (Hermes profile; `default` = root ~/.hermes), "
    "function (main, aux, title, delegation, approvals, moa, fallback, tool, plugin, gateway — "
    "derive from component/operation), provider and model when present, worst signature, "
    "burst vs one-off, escalate yes/no. Never omit agent or function. "
    "Include source pointers when present. Bullets, no tables."
)

RCA_PROMPT_RULES = (
    "Proposal only — do not implement, do not give shell commands to run unattended. "
    "Use the exact source slices. Return: root cause, blast radius, proposed repair, stop-for-approval. "
    "Name agent (profile) and function (main/aux/title/delegation/approvals/...) so the owner "
    "knows which config slot to open. Bullets, no tables."
)

_FUNCTION_BY_COMPONENT = {
    "agent.conversation_loop": "main",
    "agent.chat_completion": "main",
    "agent.auxiliary": "aux",
    "agent.title_generator": "title",
    "agent.moa_loop": "moa",
    "agent.credential": "credential",
    "tools.approval": "approvals",
    "agent.tool_executor": "tool",
    "hermes_cli.plugins": "plugin",
}


def agent_label(row: dict[str, Any]) -> str:
    profile = str(row.get("profile") or row.get("agent") or "").strip()
    return profile or "unknown"


def model_function(row: dict[str, Any]) -> str:
    existing = str(row.get("function") or "").strip()
    if existing:
        return existing
    op = str(row.get("operation") or "")
    tool = str(row.get("tool") or "")
    if tool == "delegate_task" or op in ("subagent_delegate", "delegation"):
        return "delegation"
    if op == "fallback":
        return "fallback"
    if op in ("title_generation", "title"):
        return "title"
    if op in ("aux", "auxiliary"):
        return "aux"
    comp = str(row.get("component") or "")
    if comp in _FUNCTION_BY_COMPONENT:
        return _FUNCTION_BY_COMPONENT[comp]
    for prefix, name in _FUNCTION_BY_COMPONENT.items():
        if comp == prefix or comp.startswith(prefix + "."):
            return name
    if comp.startswith("gateway."):
        return "gateway"
    if op:
        return op
    if comp.startswith("agent.") and "." in comp:
        return comp.split(".", 1)[1]
    if comp:
        return comp
    return "unknown"


def annotate_report_fields(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["agent"] = agent_label(row)
    out["function"] = model_function(row)
    return out


def format_job_error(
    *,
    agent: str,
    function: str,
    message: str,
    provider: str = "",
    model: str = "",
    host: str = "",
    config: str = "",
    now: str | None = None,
) -> str:
    ts = now or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    parts = [ts, f"agent={agent}", f"function={function}"]
    if host:
        parts.append(f"host={host}")
    if provider:
        parts.append(f"provider={provider}")
    if model:
        parts.append(f"model={model}")
    if config:
        parts.append(f"config={config}")
    parts.append(message)
    return " ".join(parts)


def attention_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("severity") == "needs-attention"]


def event_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("host") or ""),
            str(row.get("profile") or ""),
            str(row.get("event") or ""),
            str(row.get("cause") or ""),
            str(row.get("source_path") or ""),
            str(row.get("byte_start") or ""),
        ]
    )


def unseen_rows(rows: list[dict[str, Any]], seen: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        key = event_key(row)
        if key not in seen:
            out.append(row)
    return out


def pointer(row: dict[str, Any]) -> str:
    path = str(row.get("source_path") or "?")
    start = row.get("byte_start")
    end = row.get("byte_end")
    if start is None or end is None:
        return path
    return f"{path}:{start}-{end}"


def format_alarm(host: str, rows: list[dict[str, Any]], limit: int = 8) -> str:
    if not rows:
        return ""
    lines = [f"**ALARM {host}**", f"{len(rows)} new needs-attention event(s)"]
    for row in rows[:limit]:
        event = row.get("event") or "unclassified"
        agent = agent_label(row)
        function = model_function(row)
        cause = row.get("cause") or "?"
        ts = row.get("ts") or "?"
        provider = row.get("provider") or ""
        model = row.get("model") or ""
        text = str(row.get("text") or "").replace("\n", " ")[:180]
        extra = ""
        if provider or model:
            extra = f" provider=`{provider or '?'}` model=`{model or '?'}`"
        lines.append(
            f"- `{event}` agent=`{agent}` function=`{function}` cause=`{cause}` ts=`{ts}`{extra}"
        )
        lines.append(f"  ptr `{pointer(row)}`")
        if text:
            lines.append(f"  {text}")
    if len(rows) > limit:
        lines.append(f"- … {len(rows) - limit} more")
    return "\n".join(lines) + "\n"


def load_seen(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    items = data.get("seen") or []
    return {str(item) for item in items}


def save_seen(path: Path, seen: set[str], keep: int = 4000) -> None:
    ordered = sorted(seen)
    if len(ordered) > keep:
        ordered = ordered[-keep:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seen": ordered}, indent=2) + "\n", encoding="utf-8")


def slice_source(path: str, byte_start: int, byte_end: int, limit: int = 8000) -> str:
    start = max(int(byte_start), 0)
    end = max(int(byte_end), start)
    width = min(end - start, limit)
    with open(path, "rb") as handle:
        handle.seek(start)
        data = handle.read(width)
    return data.decode("utf-8", errors="replace")


def parse_escalate(text: str, attention: int) -> bool:
    lowered = text.lower()
    if "escalate: no" in lowered or "escalate no" in lowered or "escalate:no" in lowered:
        return False
    if "escalate: yes" in lowered or "escalate yes" in lowered or "escalate:yes" in lowered:
        return True
    return attention > 0


def append_observation(
    path: Path,
    *,
    host: str,
    model: str,
    last_ts: str,
    summary: str,
    escalate: bool,
    pointers: list[dict[str, Any]],
) -> dict[str, Any]:
    rec = {
        "id": uuid.uuid4().hex,
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "host": host,
        "model": model,
        "last_ts": last_ts,
        "summary": summary[:2000],
        "escalate": bool(escalate),
        "pointers": pointers,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return rec


def write_escalate_request(path: Path, observation: dict[str, Any]) -> None:
    payload = {
        "observation_id": observation["id"],
        "host": observation.get("host"),
        "last_ts": observation.get("last_ts"),
        "summary": observation.get("summary"),
        "pointers": observation.get("pointers") or [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def source_pointers(rows: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not row.get("source_path"):
            continue
        out.append(
            {
                "source_path": row.get("source_path"),
                "byte_start": row.get("byte_start"),
                "byte_end": row.get("byte_end"),
                "event": row.get("event"),
                "profile": row.get("profile"),
                "agent": agent_label(row),
                "function": model_function(row),
                "component": row.get("component"),
                "operation": row.get("operation"),
                "provider": row.get("provider"),
                "model": row.get("model"),
                "ts": row.get("ts"),
            }
        )
    return out
