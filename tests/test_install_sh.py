"""install.sh must make every runnable repo tool available on PATH.

Regression test for the 2026-09-03 find: pipeline-check and pi-doctor were
never symlinked by install.sh (and no systemd unit schedules them), so the
self-healer's heal ledger could never be written on the live box — the
09-01 ledger ship was repo-only. A tool that lives in the repo but is not
linked by the installer is dead code by accident; this test keeps the
installer honest.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = (REPO / "install.sh").read_text()

# Every standalone tool script at the repo root. Keep in sync with the
# repo layout; a tool added to the repo without appearing here fails.
TOOLS = [
    "chaos-drill",
    "loop-heartbeat",
    "new-project",
    "ntfy-notify",
    "pi-backup",
    "pi-doctor",
    "pipeline-check",
    "project-guard",
    "release-watch",
    "service-probe",
]

LINK_RE = re.compile(r'ln -sf "\$REPO_DIR/(\S+)"\s+"\$BIN_DIR/\1"')
LINKED = {m.group(1) for m in LINK_RE.finditer(INSTALL_SH)}


def test_every_tool_is_linked_by_install_sh():
    missing = [t for t in TOOLS if t not in LINKED]
    assert not missing, (
        f"install.sh never links: {missing}. A tool that is not on PATH "
        "cannot run its timer-less job; add each to install.sh's ln -sf block."
    )


def test_no_stray_links():
    """install.sh links exactly the repo tools, nothing invented."""
    stray = LINKED - set(TOOLS)
    assert not stray, f"install.sh links unknown tools: {stray}"
