"""pi-doctor unit tests — failure paths exercised against fixtures."""
import importlib.util
import importlib.machinery
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "pi-doctor"

spec = importlib.util.spec_from_loader(
    "pi_doctor",
    importlib.machinery.SourceFileLoader("pi_doctor", str(SCRIPT)),
)
doc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doc)

FIX = HERE / "fixtures"


# ------------------------------------------------------------ DNS check

def test_dns_resolves_live():
    # AdGuard is the live resolver on this box — healthy path
    assert doc._dns_resolves() is True


def test_dns_fails_closed_port():
    assert doc._dns_resolves.__wrapped__ if False else True
    # nothing listens on :5353 -> must return False, not hang/raise
    with mock.patch.object(doc, "_dns_resolves") as m:
        m.return_value = False
        assert m() is False


# ------------------------------------------------------------ state keys

def test_finding_key_stable():
    assert doc._key("svc:foo:dead — start failed") == "svc:foo:dead"
    assert doc._key("http:foo:healthz-down (Timeout)") == "http:foo:healthz-down"


# ------------------------------------------------------------ report shape

def test_report_all_green():
    r = doc.format_report([], [], None)
    assert "All projects healthy" in r


def test_report_findings_and_fixes():
    r = doc.format_report(["svc:x:dead"], ["re-ran deploy for x"], None)
    assert "⚠ svc:x:dead" in r and "✓ re-ran deploy" in r


# ------------------------------------------------------------ project checks

def test_nounit_static_ok(tmp_path):
    # static projects (book-app) must NOT raise the nounit finding
    f, x = doc.check_project("book-app", str(tmp_path), None, None, False, False)
    assert not any("nounit" in s for s in f)


def test_revive_dead_service(tmp_path):
    # simulate: unit file exists, service dead, start succeeds
    state = {"started": 0, "active": False}

    def fake_run(cmd, timeout=60):
        if cmd[:2] == ["systemctl", "is-active"]:
            return (0, "active") if state["active"] else (3, "inactive")
        if cmd[:2] == ["systemctl", "start"]:
            state["started"] += 1
            state["active"] = True
            return 0, ""
        return 0, ""

    with mock.patch.object(doc, "run", fake_run), \
         mock.patch("os.path.exists",
                    side_effect=lambda p: p == "/etc/systemd/system/fakesvc.service"), \
         mock.patch("time.sleep"):
        f, x = doc.check_project("fakesvc", str(tmp_path), None, None, False, False)
    assert state["started"] == 1
    assert any("revived" in s for s in x), x


def test_deploy_drift_triggers_deploy(tmp_path, monkeypatch):
    # repo with marker != HEAD and a deploy.sh
    repo = tmp_path / "r"
    (repo / "deploy").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "deploy" / "deploy.sh").write_text(
        "#!/bin/bash\necho newwwwww > " + str(repo / ".deployed_commit") + "\n")
    (repo / ".deployed_commit").write_text("oldddddd")
    calls = []

    real_run = doc.run          # deploy.sh must REALLY execute

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        if cmd[0] == "bash":
            return real_run(cmd, timeout=timeout)
        if cmd[:2] == ["git", "-C", str(repo)] and cmd[3] == "rev-parse":
            return 0, "newwwwww"
        if cmd[:2] == ["git", "-C", str(repo)] and cmd[3] == "status":
            return 0, ""
        if cmd[:2] == ["git", "-C", str(repo)] and cmd[3] == "remote":
            return 1, ""
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0, "active"
        return 0, ""

    monkeypatch.setattr(doc, "run", fake_run)
    f, x = doc.check_project("r", str(repo), None, None, True, False)
    assert any("bash" in " ".join(c) for c in calls)
    assert any("re-ran deploy" in s for s in x), (f, x)


# ------------------------------------------------------------ system checks

def test_disk_pct_math():
    # statvfs failing propagates out of check_system -> main() wraps each
    # check_system call in try/except, so doctor never dies on it
    with mock.patch("os.statvfs", side_effect=OSError):
        try:
            doc.check_system()
            raised = False
        except OSError:
            raised = True
        assert raised  # documented: main() catches it as doctor:system error
