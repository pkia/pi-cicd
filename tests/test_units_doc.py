"""docs/units.md is the index of the live system — keep it honest.

Fails if an expected unit disappears from the index, a row loses a
required cell, a systemd unit in systemd/ is not indexed, or a layer
section vanishes from docs/layers.md. Docs that map the running system
need a test, or they rot.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNITS_DOC = REPO / "docs" / "units.md"
LAYERS_DOC = REPO / "docs" / "layers.md"
SYSTEMD = REPO / "systemd"

# Every operational unit that must appear in the index.
EXPECTED_UNITS = {
    "project-guard",
    "deploy.sh (per service)",
    "pipeline-check",
    "pi-doctor",
    "loop-heartbeat",
    "ntfy-notify",
    "ntfy server",
    "pi-backup",
    "pi-backup-drill",
    "release-watch",
    "service-probe",
    "chaos-drill",
}

REQUIRED_COLUMNS = {"Unit", "Kind", "Schedule", "Config", "State", "Topic", "Verify"}

# Every ## section docs/layers.md must carry.
EXPECTED_LAYERS = {
    "Deploy",
    "project-guard",
    "pipeline-check",
    "pi-doctor",
    "loop-heartbeat",
    "Notifications",
    "Backup",
    "Release watching",
    "Service probing",
    "Chaos drills",
}


def _table_rows(text):
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if all(set(cell) <= set("-: ") for cell in cells):  # separator row
            continue
        rows.append(cells)
    return rows


def test_index_lists_every_expected_unit_with_full_rows():
    rows = _table_rows(UNITS_DOC.read_text())
    assert rows, "units.md index table is empty"
    headers = rows[0]
    for col in REQUIRED_COLUMNS:
        assert col in headers, f"index missing column {col!r}"
    indexed = {row[0] for row in rows[1:]}
    missing = EXPECTED_UNITS - indexed
    assert not missing, f"units not indexed in units.md: {sorted(missing)}"
    for row in rows[1:]:
        for col in REQUIRED_COLUMNS:
            assert row[headers.index(col)], f"row {row[0]!r} has empty {col!r} cell"


def test_index_covers_every_systemd_unit():
    indexed = {row[0] for row in _table_rows(UNITS_DOC.read_text())[1:]}
    for unit in SYSTEMD.glob("*.service"):
        stem = unit.name[: -len(".service")]
        assert any(stem in name for name in indexed), (
            f"{unit.name} not covered by units.md index"
        )
        assert (unit.with_suffix(".timer")).exists(), f"{stem} missing its timer"


def test_layers_doc_has_every_layer_section():
    text = LAYERS_DOC.read_text()
    for layer in EXPECTED_LAYERS:
        assert f"## {layer}" in text, f"layers.md missing section '## {layer}'"
