"""Journald source: priority-gated entries with a durable cursor.

Reads warning-and-above (PRIORITY <= 4) entries through an injectable
reader (the real one shells out to journalctl; tests inject fakes), keeps
exactly the approved field subset, and persists the last consumed
__CURSOR. When no valid cursor exists the reader is given a fallback
ISO-8601 window (default 24h) instead of replaying all history. Batches
are bounded and the loop stops when a batch makes no cursor progress.
"""
from __future__ import annotations

import datetime as _dt
import inspect
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable

JOURNALD_FIELDS = (
    "MESSAGE", "PRIORITY", "__REALTIME_TIMESTAMP", "__CURSOR",
    "_SYSTEMD_UNIT", "_SYSTEMD_USER_UNIT", "SYSLOG_IDENTIFIER",
    "_COMM", "_EXE", "_CMDLINE", "_PID", "_UID", "_GID", "_HOSTNAME",
    "_BOOT_ID", "CODE_FILE", "CODE_LINE", "CODE_FUNC", "ERRNO",
    "RESULT", "UNIT", "USER_UNIT", "INVOCATION_ID", "SYSLOG_FACILITY",
)

WARNING_PRIORITY = 4

Reader = Callable[..., list["JournaldEntry"]]


def priority_of(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 99


@dataclass(frozen=True)
class JournaldEntry:
    fields: dict[str, str]
    cursor: str

    def retained(self) -> dict[str, str]:
        return {k: v for k, v in self.fields.items() if k in JOURNALD_FIELDS}


@dataclass
class JournaldReadResult:
    entries: list[JournaldEntry] = field(default_factory=list)
    cursor: str | None = None
    lost_cursor: bool = False
    backlog: int = 0


def _load_cursor(path: str) -> tuple[str | None, bool]:
    """Return (cursor, lost) — lost means state existed but was unusable."""
    if not os.path.isfile(path):
        return None, False
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        cursor = data.get("cursor")
        if isinstance(cursor, str) and cursor:
            return cursor, False
    except (json.JSONDecodeError, OSError):
        pass
    return None, True


def _save_cursor(path: str, cursor: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".jcursor.", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"cursor": cursor}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _reader_takes_since(reader: Reader) -> bool:
    try:
        params = inspect.signature(reader).parameters
    except (TypeError, ValueError):
        return False
    return "since" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def _iso_window(hours: int) -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - _dt.timedelta(hours=hours)).isoformat(timespec="seconds")


def save_journald_cursor(path: str, cursor: str) -> None:
    _save_cursor(path, cursor)


def journalctl_reader(after_cursor: str | None, limit: int, since: str | None = None) -> list[JournaldEntry]:
    """Stream the next bounded warning-or-worse page from the local journal."""
    args = ["journalctl", "-p", "warning", "-o", "json", "--no-pager"]
    if after_cursor:
        args.append(f"--after-cursor={after_cursor}")
    elif since:
        args.append(f"--since={since}")
    process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    entries: list[JournaldEntry] = []
    capped = False
    assert process.stdout is not None
    try:
        for blob in process.stdout:
            try:
                fields = json.loads(blob)
            except json.JSONDecodeError:
                continue
            cursor = fields.get("__CURSOR")
            if isinstance(cursor, str) and cursor:
                entries.append(JournaldEntry(fields=fields, cursor=cursor))
            if len(entries) >= limit:
                capped = True
                process.terminate()
                break
    finally:
        process.stdout.close()
        returncode = process.wait(timeout=60)
    if not capped and returncode != 0:
        raise RuntimeError(f"journalctl exited {returncode}")
    return entries


def read_journald_entries(
    reader: Reader,
    state_path: str,
    batch_limit: int = 500,
    max_batches: int = 20,
    fallback_window_hours: int = 24,
    persist: bool = True,
) -> JournaldReadResult:
    cursor, lost = _load_cursor(state_path)
    since = None
    if cursor is None:
        since = _iso_window(fallback_window_hours)
    collected: list[JournaldEntry] = []
    seen_cursors: set[str] = set()
    batches = 0
    while batches < max_batches:
        if since is not None and _reader_takes_since(reader):
            entries = reader(cursor, batch_limit, since=since)
        else:
            entries = reader(cursor, batch_limit)
        batches += 1
        if not entries:
            break
        progressed = False
        for entry in entries:
            if not entry.cursor or priority_of(entry.fields.get("PRIORITY")) > WARNING_PRIORITY:
                continue
            if entry.cursor in seen_cursors:
                continue
            seen_cursors.add(entry.cursor)
            collected.append(entry)
            cursor = entry.cursor
            progressed = True
        if not progressed:
            break
        # A short page is terminal for this invocation. Calling the reader
        # again with a cursor is only needed when it filled the requested
        # page; this also avoids replaying simplistic/inclusive readers.
        if len(entries) < batch_limit:
            break
    result = JournaldReadResult(entries=collected, cursor=cursor, lost_cursor=lost, backlog=0)
    if persist and cursor and cursor != _load_cursor(state_path)[0]:
        _save_cursor(state_path, cursor)
    return result
