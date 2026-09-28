"""Tail raw Hermes text log files with a durable inode/dev/byte cursor.

Reads only complete newline-terminated lines. First sight of a file starts
at the last 200,000 bytes aligned forward past the first newline, so an
initial baseline never ingests an entire history. Later runs resume from
the committed cursor. Rotation (inode/dev change) is treated as a new file
(first-sight baseline again); truncation (size < offset) rewinds to byte 0.
The cursor never advances past bytes actually processed: a per-run work
ceiling leaves the remainder as a reported backlog for the next run.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from astra.cursor import CursorStore

FIRST_SIGHT_WINDOW_BYTES = 200_000

RAW_LINE_FIELDS = ("text", "text_truncated", "original_text_bytes")


@dataclass(frozen=True)
class RawLine:
    text: str
    source_path: str
    source_name: str
    byte_start: int
    byte_end: int
    text_truncated: bool = False
    original_text_bytes: int = 0
    content_hash: str = ""
    inode: int | None = None
    dev: int | None = None


@dataclass(frozen=True)
class RawTailResult:
    lines: list[RawLine] = field(default_factory=list)
    backlog_bytes: int = 0
    truncated: bool = False
    rotated: bool = False


def _stat_ids(stat_result: os.stat_result) -> tuple[int, int, int]:
    return int(stat_result.st_ino), int(stat_result.st_dev), int(stat_result.st_size)


def first_sight_start(handle: Any, baseline: int, size: int) -> int:
    """Align the baseline forward past the first newline (or to EOF)."""
    if baseline <= 0:
        return 0
    handle.seek(baseline)
    chunk = handle.readline()
    if chunk.endswith(b"\n"):
        return baseline + len(chunk)
    return size


def read_new_raw_lines(
    path: str,
    store: CursorStore,
    work_ceiling: int | None = None,
    text_limit: int = 800,
) -> RawTailResult:
    realpath = os.path.realpath(path)
    truncated = False
    rotated = False
    with open(realpath, "rb") as handle:
        inode, dev, size = _stat_ids(os.fstat(handle.fileno()))
        previous = store.get(realpath)
        start = 0
        if previous is not None:
            same_file = previous.get("inode") == inode and previous.get("dev") == dev
            prev_offset = int(previous.get("offset", 0))
            if same_file:
                if size < prev_offset:
                    truncated = True  # same inode shrank: rewind to start
                else:
                    start = prev_offset
            else:
                rotated = True  # different inode/dev: brand-new file
        if previous is None or rotated or truncated:
            baseline = max(0, size - FIRST_SIGHT_WINDOW_BYTES)
            start = first_sight_start(handle, baseline, size)

        handle.seek(start)
        lines: list[RawLine] = []
        consumed = start
        while True:
            if work_ceiling is not None and consumed - start >= work_ceiling:
                break
            line_start = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break  # incomplete trailing line: never consumed
            line_end = handle.tell()
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if text:
                original_bytes = len(raw)
                lines.append(
                    RawLine(
                        text=text[:text_limit],
                        source_path=realpath,
                        source_name=os.path.basename(realpath),
                        byte_start=line_start,
                        byte_end=line_end,
                        text_truncated=len(text) > text_limit,
                        original_text_bytes=original_bytes,
                        content_hash=hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest(),
                        inode=inode, dev=dev,
                    )
                )
            consumed = line_end
        eof = handle.seek(0, os.SEEK_END)
        backlog = max(0, eof - consumed)
        store.put(realpath, inode=inode, dev=dev, offset=consumed, size=size)
        return RawTailResult(
            lines=lines,
            backlog_bytes=backlog,
            truncated=truncated,
            rotated=rotated,
        )
