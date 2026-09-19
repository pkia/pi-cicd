"""One retired list, three readers — see ../retired-units.

The 2026-09-15 retirements (cs2-tracker, cs2-dashboard, mark-site) were
learned separately by pi-doctor, service-probe and the unit-index test:
three places to forget.  All three read the shared list through
`retired_units.py` now, so retiring a unit is one edit, and a unit that is
retired *and* live is a contradiction caught here instead of a quiet
disagreement between two lists.

The last test drives the real service-probe with a probe row named after a
retired unit: the sweep must say so, and publish nothing.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from pathlib import Path

import retired_units

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "retired-units"
UNITS_DOC = REPO / "docs" / "units.md"
SYSTEMD = REPO / "systemd"

# Every reader of the one list. A new reader belongs here; a reader that
# grows its own copy of the retired names fails test_readers_use_the_one_file.
READERS = (
    REPO / "pi-doctor",
    REPO / "service-probe",
    REPO / "tests" / "test_units_doc.py",
)


def _live_unit_names() -> set:
    """The live set: index rows in docs/units.md plus systemd/ unit files."""
    names = set()
    for line in UNITS_DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cell = line.split("|")[1].strip().strip("`")
        if cell and not set(cell) <= set("-: "):
            names.add(cell)
    names |= {p.name.split(".")[0] for p in SYSTEMD.glob("*.service*")}
    return names


def test_config_carries_the_retired_units_and_every_spelling():
    r = retired_units.load(CONFIG)
    assert {"cs2-tracker", "cs2-dashboard", "mark-site"} <= set(r.units)
    assert {"cs2-dash", "cs2trk"} <= set(r.tokens)  # probe-row spellings
    assert r.is_retired("cs2-dashboard")
    assert not r.is_retired("project-hub")


def test_readers_use_the_one_file():
    for path in READERS:
        src = path.read_text(encoding="utf-8")
        assert re.search(r"\bretired_units\b", src), (
            f"{path.name} carries its own idea of what is retired — read "
            "retired-units instead")
    stale = re.search(r"RETIRED_UNITS\s*=\s*\{",
                      (REPO / "tests" / "test_units_doc.py").read_text(encoding="utf-8"))
    assert not stale, "the index test is back to a private copy of the list"


def test_retired_and_live_is_a_contradiction():
    r = retired_units.load(CONFIG)
    overlap = r.conflict(_live_unit_names())
    assert overlap == [], f"retired and live at once: {overlap}"


def test_conflict_predicate_fires_on_an_overlap(tmp_path):
    cfg = tmp_path / "retired-units"
    cfg.write_text("# comment\n\ncs2-tracker cs2trk\n", encoding="utf-8")
    r = retired_units.load(cfg)
    assert r.conflict({"portal", "cs2trk"}) == ["cs2trk"]
    assert r.conflict({"portal", "mission-control"}) == []


def test_missing_file_fails_open(tmp_path):
    """An unreadable list must not make every unit look retired."""
    r = retired_units.load(tmp_path / "gone")
    assert not r
    assert not r.is_retired("cs2-tracker")
    assert r.conflict({"cs2-tracker"}) == []


def _service_probe():
    loader = importlib.machinery.SourceFileLoader(
        "service_probe_retired_probe", str(REPO / "service-probe"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_service_probe_nags_a_probe_row_for_a_retired_unit(
        tmp_path, monkeypatch, capsys):
    """A retired unit still in PROBE_HTTP is named at once, not in 1000 sweeps."""
    sp = _service_probe()
    monkeypatch.setattr(sp, "http_probe", lambda url, timeout: (True, 1, None))
    cfg = tmp_path / "service-probe.conf"
    cfg.write_text("PROBE_HTTP=cs2-dash=http://127.0.0.1:9/healthz\n",
                   encoding="utf-8")
    rc = sp.main(["--config", str(cfg), "--state", str(tmp_path / "state.json"),
                  "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "retired unit" in out and "cs2-dash" in out
    assert "would publish" not in out  # a config fact is not an outage
