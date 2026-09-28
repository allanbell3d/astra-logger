"""Expand glob patterns into unique real paths, preserving first-seen order."""
from __future__ import annotations

import glob
import os
from collections.abc import Iterable


def discover_input_files(patterns: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    found: list[str] = []
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if not matches and os.path.isfile(pattern):
            matches = [pattern]
        for match in sorted(matches):
            if not os.path.isfile(match):
                continue
            real = os.path.realpath(match)
            if real in seen:
                continue
            seen.add(real)
            found.append(real)
    return found
