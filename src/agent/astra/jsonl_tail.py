"""Read newly appended complete JSONL records using a durable cursor."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from astra.cursor import CursorStore


@dataclass(frozen=True)
class TailedRecord:
    record: dict[str, Any]
    source_path: str
    byte_start: int
    byte_end: int


@dataclass(frozen=True)
class TailError:
    source_path: str
    byte_start: int
    message: str


def _stat_ids(stat_result: os.stat_result) -> tuple[int, int, int]:
    return int(stat_result.st_ino), int(stat_result.st_dev), int(stat_result.st_size)


def read_new_complete_records(
    path: str, store: CursorStore
) -> tuple[list[TailedRecord], list[TailError]]:
    realpath = os.path.realpath(path)
    with open(realpath, "rb") as handle:
        stat_result = os.fstat(handle.fileno())
        inode, dev, size = _stat_ids(stat_result)
        previous = store.get(realpath)
        offset = 0
        if previous is not None:
            same_file = previous.get("inode") == inode and previous.get("dev") == dev
            if same_file and size >= int(previous.get("offset", 0)):
                offset = int(previous.get("offset", 0))
            # rotation (inode change) or truncation (size < offset) resets to 0
        handle.seek(offset)
        rows: list[TailedRecord] = []
        errors: list[TailError] = []
        consumed = offset
        while True:
            line_start = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break
            line_end = handle.tell()
            text = raw.decode("utf-8")
            if not text.strip():
                consumed = line_end
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                errors.append(
                    TailError(realpath, line_start, f"malformed JSON: {exc.msg}")
                )
                break
            if not isinstance(parsed, dict):
                errors.append(
                    TailError(realpath, line_start, "JSONL row must be an object")
                )
                break
            rows.append(
                TailedRecord(
                    record=parsed,
                    source_path=realpath,
                    byte_start=line_start,
                    byte_end=line_end,
                )
            )
            consumed = line_end
        store.put(realpath, inode=inode, dev=dev, offset=consumed, size=size)
        return rows, errors
