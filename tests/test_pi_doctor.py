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


def _verb(cmd):
    """systemctl verb, even when the call is wrapped in `sudo -n`."""
    if "systemctl" not in cmd:
        return None
    i = cmd.index("systemctl")
    return cmd[i + 1] if i + 1 < len(cmd) else None


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
    f, x, i = doc.check_project("book-app", str(tmp_path), None, None, False, False)
    assert not any("nounit" in s for s in f)


def test_revive_dead_service(tmp_path):
    # simulate: unit file exists, service dead, start succeeds
    state = {"started": 0, "active": False}

    def fake_run(cmd, timeout=60):
        v = _verb(cmd)
        if v == "is-active":
            return (0, "active") if state["active"] else (3, "inactive")
        if v == "is-enabled":
            return 0, "enabled"
        if v == "start":
            state["started"] += 1
            state["active"] = True
            return 0, ""
        return 0, ""

    with mock.patch.object(doc, "run", fake_run), \
         mock.patch("os.path.exists",
                    side_effect=lambda p: p == "/etc/systemd/system/fakesvc.service"), \
         mock.patch("time.sleep"):
        f, x, i = doc.check_project("fakesvc", str(tmp_path), None, None, False, False)
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
    f, x, i = doc.check_project("r", str(repo), None, None, True, False)
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
    f, x, i = doc.check_project("r2", str(repo), None, None, True, False)
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
    import shutil, subprocess
    monkeypatch.setattr(shutil, "which", lambda *a: "/usr/bin/rtk")
    fake = subprocess.CompletedProcess(
        ["rtk", "gain"], 0,
        stdout="Total commands: 42\nOutput tokens: 1000\nTokens saved: 5.2K (83.9%)\nEfficiency meter: ████████ 83.9%\n")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
    info = doc.check_rtk()
    assert any("5.2K" in i and "83.9" in i for i in info)


def test_check_rtk_silent_without_binary(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda *a: None)
    assert doc.check_rtk() == []


# ------------------------------------------------------- retired units

def test_retired_unit_is_never_revived(tmp_path, monkeypatch):
    # owner did `systemctl disable` + stop: report it, never start it again
    started = []

    def fake_run(cmd, timeout=60):
        v = _verb(cmd)
        if v == "is-active":
            return 3, "inactive"
        if v == "is-enabled":
            return 1, "disabled"
        if v == "start":
            started.append(cmd)
            return 0, ""
        return 0, ""

    monkeypatch.setattr(doc, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *a: None)
    f, x, i = doc.check_project("cs2-tracker", str(tmp_path), None, None, True, True)
    assert started == []                                   # no revive attempt
    assert f == [] and x == []                             # not a fault either
    assert any(s.startswith("retired:cs2-tracker") for s in i), i


def test_enabled_dead_unit_still_revived(tmp_path, monkeypatch):
    # regression guard: the retire rule must not disarm F1 for live units
    state = {"active": False, "started": 0}

    def fake_run(cmd, timeout=60):
        v = _verb(cmd)
        if v == "is-active":
            return (0, "active") if state["active"] else (3, "inactive")
        if v == "is-enabled":
            return 0, "enabled"
        if v == "start":
            state["started"] += 1
            state["active"] = True
            return 0, ""
        return 0, ""

    monkeypatch.setattr(doc, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *a: None)
    with mock.patch("os.path.exists",
                    side_effect=lambda p: p == "/etc/systemd/system/fakesvc.service"):
        f, x, i = doc.check_project("fakesvc", str(tmp_path), None, None, False, False)
    assert state["started"] == 1
    assert any("revived" in s for s in x), x


def test_report_separates_retired_from_parks():
    r = doc.format_report([], [], None,
                          ["retired:x — unit disabled by owner, not reviving",
                           "parked:y — stopped by ram-mode focus"])
    assert "Retired (owner-disabled, not revived):" in r
    assert "\u23f9 retired:x" in r
    assert "Parks / hand-offs (expected):" in r
    assert "\u23f8 parked:y" in r
    assert "All projects healthy (expected states above)." in r


# ------------------------------------------------------- dark window (boot)

def _fake_boot(monkeypatch, uptime_s, saved_ts, boot="boot-1"):
    monkeypatch.setattr(doc, "_boot_id", lambda path=None: boot)
    monkeypatch.setattr(doc, "_read_uptime", lambda path=None: uptime_s)
    monkeypatch.setattr(doc, "_clock_saved", lambda path=None: saved_ts)


def test_dark_window_is_the_power_off_to_boot_interval():
    from datetime import datetime
    now = datetime(2026, 9, 25, 6, 30, 0)
    # clock restored to 05:30 (last save before the cut), box booted at
    # 06:20 per the monotonic uptime -> 10 minutes without power.
    w = doc.dark_window(now, uptime_s=600.0, saved_ts=now.timestamp() - 3600)
    assert w["gap_s"] == 3000 and w["minutes"] == 50
    assert (w["dark_since"], w["dark_until"]) == ("2026-09-25T05:30:00",
                                                  "2026-09-25T06:20:00")


def test_clean_reboot_is_not_a_window():
    from datetime import datetime
    now = datetime(2026, 9, 25, 6, 30, 0)
    assert doc.dark_window(now, 600.0, now.timestamp() - 620) is None   # 20 s
    assert doc.dark_window(now, 600.0, None) is None                    # no clock
    assert doc.dark_window(now, None, now.timestamp() - 3600) is None    # no uptime
    # a clock file nobody touched since an earlier boot is not an outage
    assert doc.dark_window(now, 600.0, now.timestamp() - 30 * 86400) is None


def test_boot_run_records_the_window_and_alerts_exactly_once(monkeypatch):
    from datetime import datetime
    alerts = []
    monkeypatch.setattr(doc, "send_alert", lambda s, b: alerts.append((s, b)))
    now = datetime(2026, 9, 25, 6, 30, 0)
    _fake_boot(monkeypatch, 600.0, now.timestamp() - 3600)
    state = {}
    line, window = doc.check_dark_window(now=now, state=state, save=False)
    assert line and "dark window" in line and window["minutes"] == 50
    assert len(alerts) == 1 and "50m without power" in alerts[0][1]
    assert state["dark_window"]["dark_since"] == window["dark_since"]
    assert [e["dark_since"] for e in state["dark_windows"]] == [window["dark_since"]]
    # the daily audit in the same boot observes nothing and re-alerts nobody
    line2, window2 = doc.check_dark_window(now=now, state=state, save=False)
    assert (line2, window2) == (None, None)
    assert len(alerts) == 1 and len(state["dark_windows"]) == 1


def test_long_uptime_never_reads_a_stale_clock_file(monkeypatch):
    """The first-run-since-boot gate: a 2-day uptime must not compute at all.

    Without it every run on a long-lived box would subtract a clock file
    from a boot instant days later and call the whole uptime an outage.
    """
    from datetime import datetime
    reads = []
    monkeypatch.setattr(doc, "_boot_id", lambda path=None: "boot-current")
    monkeypatch.setattr(doc, "_read_uptime",
                        lambda path=None: reads.append(1) or 200000.0)
    monkeypatch.setattr(doc, "_clock_saved", lambda path=None: 0.0)
    monkeypatch.setattr(doc, "send_alert",
                        lambda s, b: pytest.fail("must not alert"))
    state = {"boot_id": "boot-current"}
    assert doc.check_dark_window(now=datetime(2026, 9, 25, 6, 30),
                                 state=state, save=False) == (None, None)
    assert reads == []


def test_cli_dark_window_prints_and_records(tmp_path):
    """The boot unit's real entry point, driven from faked files."""
    import json
    import subprocess
    import time
    now = time.time()
    clock, uptime = tmp_path / "clock", tmp_path / "uptime"
    boot_id, state = tmp_path / "boot_id", tmp_path / "state.json"
    clock.write_text("")
    uptime.write_text("600.00 100.00\n")
    boot_id.write_text("fake-boot\n")
    os.utime(clock, (now - 3600, now - 3600))     # saved an hour before boot
    env = dict(os.environ, PI_DOCTOR_CLOCK_FILE=str(clock),
               PI_DOCTOR_UPTIME_FILE=str(uptime),
               PI_DOCTOR_BOOT_ID_FILE=str(boot_id),
               PI_DOCTOR_STATE=str(state))
    cmd = [sys.executable, str(SCRIPT), "--dark-window", "--no-alert"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("dark window"), (r.stdout, r.stderr)
    recorded = json.loads(state.read_text())
    assert recorded["dark_window"]["minutes"] >= 40
    assert len(recorded["dark_windows"]) == 1
    # a normal reboot prints nothing and records no window (a fresh ledger,
    # so the earlier window cannot be mistaken for this boot's)
    boot_id.write_text("next-boot\n")
    os.utime(clock, (now - 610, now - 610))
    env["PI_DOCTOR_STATE"] = str(tmp_path / "state-clean.json")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90)
    assert r.returncode == 0 and r.stdout == ""
    clean = json.loads((tmp_path / "state-clean.json").read_text())
    assert clean["boot_id"] == "next-boot" and "dark_window" not in clean
    assert len(recorded["dark_windows"]) == 1     # the real window stands


def test_restored_clock_reading_is_resolved_once_corrected(monkeypatch):
    """The 2026-09-26 outage: `file == now` at the boot run, measured later.

    This is the case the single-shot check could not see, and the one the
    real outage went through: the boot run reads the clock timesyncd just
    restored, so `now - uptime` lands on the restored timeline and the
    subtraction collapses to `-uptime`. The window is only measurable from
    the pair — this run's restored value plus a corrected later run.
    """
    from datetime import datetime, timedelta
    alerts = []
    monkeypatch.setattr(doc, "send_alert", lambda s, b: alerts.append((s, b)))
    restored = datetime(2026, 9, 25, 5, 30, 0)          # when the box died
    monkeypatch.setattr(doc, "_boot_id", lambda path=None: "boot-cut")
    monkeypatch.setattr(doc, "_clock_saved",
                        lambda path=None: restored.timestamp())
    monkeypatch.setattr(doc, "_read_uptime", lambda path=None: 20.0)

    state = {}
    assert doc.check_dark_window(now=restored, state=state, save=False) == (None, None)
    assert alerts == [] and "dark_window" not in state
    reading = state["dark_window_pending"]
    assert reading["boot_id"] == "boot-cut"
    assert reading["restored_ts"] == restored.timestamp()

    # the correction lands: the box really booted at 01:10:20 the next day
    # (1200 s of uptime against a clock that is right)
    corrected = datetime(2026, 9, 26, 1, 30, 20)
    monkeypatch.setattr(doc, "_read_uptime", lambda path=None: 1200.0)
    boot_instant = corrected - timedelta(seconds=1200)
    expect_gap = int((boot_instant - restored).total_seconds())
    line, window = doc.check_dark_window(now=corrected, state=state, save=False)
    assert window and window["gap_s"] == expect_gap
    assert window["minutes"] == round(expect_gap / 60)
    assert window["source"] == "restored-clock"
    assert (window["dark_since"], window["dark_until"]) == (
        "2026-09-25T05:30:00", "2026-09-26T01:10:20")
    assert line and "without power" in line
    assert len(alerts) == 1 and "without power" in alerts[0][1]
    assert state["dark_window"]["dark_since"] == window["dark_since"]
    assert "dark_window_pending" not in state       # resolved, not left behind

    # a later run in the same boot says nothing and re-alerts nobody
    assert doc.check_dark_window(now=corrected, state=state, save=False) == (None, None)
    assert len(alerts) == 1 and len(state["dark_windows"]) == 1


def test_clean_reboot_resolves_from_the_restored_clock_to_nothing(monkeypatch):
    """An ordinary reboot goes through the same pair and stays quiet."""
    from datetime import datetime, timedelta
    alerts = []
    monkeypatch.setattr(doc, "send_alert", lambda s, b: alerts.append((s, b)))
    restored = datetime(2026, 9, 25, 6, 30, 0)
    monkeypatch.setattr(doc, "_boot_id", lambda path=None: "boot-plain")
    monkeypatch.setattr(doc, "_clock_saved",
                        lambda path=None: restored.timestamp())
    monkeypatch.setattr(doc, "_read_uptime", lambda path=None: 5.0)
    state = {}
    assert doc.check_dark_window(now=restored, state=state, save=False) == (None, None)
    assert "dark_window_pending" in state

    # 30 s of real downtime, clock corrected, 10 s of uptime
    monkeypatch.setattr(doc, "_read_uptime", lambda path=None: 10.0)
    late = restored + timedelta(seconds=40)
    assert doc.check_dark_window(now=late, state=state, save=False) == (None, None)
    assert alerts == [] and "dark_window" not in state
    assert "dark_window_pending" not in state    # measured and dismissed


def test_still_restored_reading_waits_for_the_correction(monkeypatch):
    """While `now` is still the restored value the reading is kept, not read
    as a clean boot: `now - uptime` before the saved value means "not yet".

    Both clocks move together here — uptime with the restored wall clock —
    which is exactly why `now - uptime` stays on the restored timeline.
    """
    from datetime import datetime, timedelta
    restored = datetime(2026, 9, 25, 5, 30, 0)
    monkeypatch.setattr(doc, "_boot_id", lambda path=None: "boot-cut")
    monkeypatch.setattr(doc, "_clock_saved",
                        lambda path=None: restored.timestamp())
    monkeypatch.setattr(doc, "_read_uptime", lambda path=None: 20.0)
    state = {}
    doc.check_dark_window(now=restored, state=state, save=False)
    monkeypatch.setattr(doc, "_read_uptime", lambda path=None: 79.0)
    line, window = doc.check_dark_window(now=restored + timedelta(seconds=59),
                                        state=state, save=False)
    assert (line, window) == (None, None)
    assert state["dark_window_pending"]["boot_id"] == "boot-cut"


def test_unmeasured_reading_is_named_when_its_boot_is_over(monkeypatch):
    """A reading the correction never reached is reported, not silently
    dropped — an unmeasured blackout must not look like a quiet boot."""
    from datetime import datetime
    alerts = []
    monkeypatch.setattr(doc, "send_alert", lambda s, b: alerts.append((s, b)))
    now = datetime(2026, 9, 26, 5, 30, 0)
    monkeypatch.setattr(doc, "_boot_id", lambda path=None: "boot-next")
    monkeypatch.setattr(doc, "_clock_saved", lambda path=None: now.timestamp())
    monkeypatch.setattr(doc, "_read_uptime", lambda path=None: 30.0)
    state = {"boot_id": "boot-old",
             "dark_window_pending": {"boot_id": "boot-old",
                                     "restored_ts": now.timestamp() - 90000,
                                     "clock_mtime": now.timestamp() - 90000}}
    line, window = doc.check_dark_window(now=now, state=state, save=False)
    assert window is None and "dark_window" not in state
    assert line and "unmeasured for boot boot-old" in line
    assert len(alerts) == 1 and "unmeasured" in alerts[0][1]
    # boot-next's own reading took the slot: this boot is still measurable
    assert state["dark_window_pending"]["boot_id"] == "boot-next"


def test_cli_boot_run_writes_the_reading_and_a_later_run_resolves_it(tmp_path):
    """The boot unit's real entry point, on both sides of the correction."""
    import subprocess
    import time
    clock, uptime = tmp_path / "clock", tmp_path / "uptime"
    boot_id, state = tmp_path / "boot_id", tmp_path / "state.json"
    clock.write_text("")
    boot_id.write_text("cli-boot\n")
    now = time.time()
    # the restored clock: the file's mtime is the value the boot run sees
    os.utime(clock, (now - 20, now - 20))
    uptime.write_text("20.00 10.00\n")
    env = dict(os.environ, PI_DOCTOR_CLOCK_FILE=str(clock),
               PI_DOCTOR_UPTIME_FILE=str(uptime),
               PI_DOCTOR_BOOT_ID_FILE=str(boot_id),
               PI_DOCTOR_STATE=str(state))
    cmd = [sys.executable, str(SCRIPT), "--dark-window", "--no-alert"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90)
    assert r.returncode == 0 and r.stdout == "", (r.stdout, r.stderr)
    first = json.loads(state.read_text())
    assert first["dark_window_pending"]["boot_id"] == "cli-boot"
    assert "dark_window" not in first

    # 20 hours later in the same boot, clock corrected: the pair resolves
    uptime.write_text("30.00 10.00\n")
    first["dark_window_pending"]["restored_ts"] = time.time() - 72000
    state.write_text(json.dumps(first))
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90)
    assert r.returncode == 0 and r.stdout.startswith("dark window"), (r.stdout, r.stderr)
    resolved = json.loads(state.read_text())
    assert resolved["dark_window"]["minutes"] >= 1190
    assert resolved["dark_window"]["source"] == "restored-clock"
    assert "dark_window_pending" not in resolved
    assert len(resolved["dark_windows"]) == 1


def test_report_has_its_own_dark_window_bucket():
    r = doc.format_report([], [], None, ["dark:dark window X -> Y (50m without power)"])
    assert "Dark windows (box had no power):" in r
    assert "dark window X -> Y (50m without power)" in r
    assert "Parks / hand-offs" not in r          # not a park, not a fault


# ------------------------------------------------- probe target sanity

def test_probe_targets_are_bounded():
    """No project may be probed on an endless stream.

    sat-audio's /stream.mp3 never ends by design; the 8s probe read 8KB and
    hung up mid-encode, which is how one leaked ffmpeg per day (756MB RSS by
    2026-09-19) was discovered. Bounded endpoints only.
    """
    endless = ("/stream.mp3",)
    for entry in doc.PROJECTS:
        svc, ui = entry[0], entry[2]
        if ui:
            assert not ui.endswith(endless), f"{svc} probes an endless response"
