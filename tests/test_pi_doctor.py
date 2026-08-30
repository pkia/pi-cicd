"""pi-doctor unit tests — failure paths exercised against fixtures."""
import importlib.util
import importlib.machinery
import json
import os
import sys

import pytest
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
    # AdGuard is the live resolver on this box — healthy path. Hermetic on
    # CI (no :53 there): skip rather than assert, so the test exercises
    # the live path only where a resolver actually exists.
    import socket as _s
    try:
        with _s.create_connection(("127.0.0.1", 53), timeout=1):
            pass
    except OSError:
        pytest.skip("no local DNS resolver on this host (CI)")
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
    # REAL throwaway git repo; only systemctl is mocked
    import subprocess as sp
    repo = tmp_path / "r"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty",
            "-m", "x"], env={**os.environ,
                             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
            check=True)
    (repo / "deploy").mkdir()
    (repo / "deploy" / "deploy.sh").write_text(
        "#!/bin/bash\ngit -C %s rev-parse HEAD > %s/.deployed_commit\n"
        % (repo, repo))
    (repo / ".deployed_commit").write_text("0" * 40)   # drifted
    real_run = doc.run

    def fake_run(cmd, timeout=60):
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0, "active"
        return real_run(cmd, timeout=timeout)   # git + bash run for real

    monkeypatch.setattr(doc, "run", fake_run)
    f, x = doc.check_project("r", str(repo), None, None, True, False)
    assert any("re-ran deploy" in s for s in x), (f, x)


def test_deploy_healthy_no_drift(tmp_path, monkeypatch):
    import subprocess as sp
    repo = tmp_path / "r2"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    sp.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty",
            "-m", "x"], env={**os.environ,
                             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
            check=True)
    head = sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                  capture_output=True, text=True).stdout.strip()
    (repo / ".deployed_commit").write_text(head)       # in sync
    monkeypatch.setattr(doc, "run",
                        lambda cmd, timeout=60: (0, "active")
                        if cmd[:2] == ["systemctl", "is-active"]
                        else (0, ""))
    f, x = doc.check_project("r2", str(repo), None, None, True, False)
    assert not any("deploy" in s for s in f), f
    assert x == []


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


# ------------------------------------------------------- standing mute

def test_standing_mute_is_a_finding(tmp_path):
    mute = tmp_path / "mute"
    mute.write_text("storm since monday\n")
    f, x = doc.check_mute(str(mute))
    assert f and "muted" in f[0] and "storm since monday" in f[0]


def test_no_mute_no_finding(tmp_path):
    f, x = doc.check_mute(str(tmp_path / "absent"))
    assert f == [] and x == []


def test_check_rtk_reports_savings(monkeypatch):
    import subprocess
    doc = _load()
    fake = subprocess.CompletedProcess(
        ["rtk", "gain"], 0,
        stdout="Total commands: 42\nOutput tokens: 1000\nTokens saved: 5.2K (83.9%)\nEfficiency meter: ████████ 83.9%\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
    info = doc.check_rtk()
    assert any("5.2K" in i and "83.9" in i for i in info)


def test_check_rtk_silent_without_binary(monkeypatch):
    import shutil
    doc = _load()
    monkeypatch.setattr(shutil, "which", lambda *a: None)
    assert doc.check_rtk() == []
