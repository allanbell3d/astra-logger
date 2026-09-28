"""Durable per-file byte/inode cursor state."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from typing import Any


class CursorStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._state: dict[str, Any] = {"files": {}}
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict) and isinstance(loaded.get("files"), dict):
                self._state = loaded

    def get(self, realpath: str) -> dict[str, Any] | None:
        entry = self._state["files"].get(realpath)
        return dict(entry) if isinstance(entry, dict) else None

    def put(self, realpath: str, inode: int, dev: int, offset: int, size: int) -> None:
        self._state["files"][realpath] = {"inode": inode, "dev": dev, "offset": offset, "size": size}

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    def restore(self, state: dict[str, Any]) -> None:
        self._state = copy.deepcopy(state) if isinstance(state, dict) and isinstance(state.get("files"), dict) else {"files": {}}

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".cursor.", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            if os.name != "nt":
                dirfd = os.open(directory, os.O_RDONLY)
                try: os.fsync(dirfd)
                finally: os.close(dirfd)
        except Exception:
            if os.path.exists(tmp): os.remove(tmp)
            raise
