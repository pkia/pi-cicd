"""One retired list, three readers — see ./retired-units.

pi-doctor, service-probe and the unit-index tests each learned about a
retirement on their own (the 2026-09-15 retirements of cs2-tracker,
cs2-dashboard and mark-site landed in three places), which is three
places to forget.  They all read this module now, so retiring a unit is
one edit to the config file, and a name that is retired *and* live at
once is a contradiction CI catches instead of a silent disagreement
(tests/test_retired_units.py).

The list is config, not code: it lives next to the tools, is overridable
with $RETIRED_UNITS_FILE (tests and probes use that), and a missing file
reads as "nothing retired" — an unreadable config must never make the
doctor or the prober behave as if every unit were gone.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().with_name("retired-units")
ENV_VAR = "RETIRED_UNITS_FILE"


class Retired:
    """Parsed retired list: canonical units plus every known spelling."""

    def __init__(self, units, tokens, path):
        self.units = frozenset(units)
        self.tokens = frozenset(tokens)
        self.path = Path(path)

    def __bool__(self) -> bool:
        return bool(self.units)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Retired({sorted(self.units)}, {self.path})"

    def is_retired(self, name) -> bool:
        return str(name).strip() in self.tokens

    def hits(self, text) -> list:
        """Every retired token occurring in `text` (a probe row, a URL…)."""
        text = str(text)
        return sorted(t for t in self.tokens if t in text)

    def conflict(self, live) -> list:
        """Tokens that are retired and live at the same time — a bug."""
        return sorted({str(x).strip() for x in live} & set(self.tokens))


def load(path=None) -> Retired:
    """Load the retired list (argument, then $RETIRED_UNITS_FILE, then repo)."""
    if path is None:
        path = os.environ.get(ENV_VAR) or DEFAULT_PATH
    units: list[str] = []
    tokens: set[str] = set()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return Retired((), (), path)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        units.append(parts[0])
        tokens.update(parts)
    return Retired(units, tokens, path)


# The tools want a question, not a config object: `retired_units.is_retired(svc)`.
# The list is re-read when the path changes (a test or a probe pointing
# $RETIRED_UNITS_FILE elsewhere) and is tiny, so the cache is per-path.
_CACHE: dict = {}


def _default() -> Retired:
    path = os.environ.get(ENV_VAR) or DEFAULT_PATH
    key = str(path)
    if key not in _CACHE:
        _CACHE[key] = load(path)
    return _CACHE[key]


def is_retired(name) -> bool:
    """Is `name` (or one of its spellings) on the shared retired list?"""
    return _default().is_retired(name)


def hits(text) -> list:
    """Retired tokens occurring in `text` (a probe row, a URL, a unit key)."""
    return _default().hits(text)
