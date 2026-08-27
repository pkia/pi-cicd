"""Tests for loop-heartbeat — the loop's dead-man's switch.

Fixtures under fixtures/ are scrubbed snapshots of REAL `hermes cron
list` / `hermes cron runs` output captured on the Pi (phone number
redacted), so the parsers are tested against the exact CLI shapes in
production, not a guess.
"""
import importlib.util
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "loop-heartbeat"

spec = importlib.util.spec_from_loader(
    "loop_heartbeat",
    importlib.machinery.SourceFileLoader("loop_heartbeat", str(SCRIPT)),
)
lh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lh)

FIX = HERE / "fixtures"


# ------------------------------------------------------------ parsers

def test_parse_cron_list_finds_all_jobs():
    jobs = lh.parse_cron_list((FIX / "cron_list.txt").read_text())
    assert len(jobs) == 10
    assert jobs["Daily devlog post"] == ("4daccc802319", "active")
    assert jobs["Radar implementer — self-improvement loop"] == ("50517c2e700c", "active")
    assert jobs["Pi health watchdog"] == ("db50209537ca", "active")


def test_parse_cron_list_ignores_box_drawing_and_indented_noise():
    jobs = lh.parse_cron_list((FIX / "cron_list.txt").read_text())
    # every parsed name came from a real "Name:" line
    assert all("Name" not in n and "[" not in n for n in jobs)


def test_parse_runs_extracts_statuses_and_naive_datetimes():
    entries = lh.parse_runs((FIX / "runs_streak.txt").read_text())
    assert entries, "fixture should yield entries"
    assert entries[0]["status"] == "running"
    assert {e["status"] for e in entries} >= {"failed", "unknown"}
    assert all(e["ts"].tzinfo is None for e in entries)


def test_parse_runs_ignores_indented_continuation_lines():
    entries = lh.parse_runs((FIX / "runs_streak.txt").read_text())
    for e in entries:
        assert len(e["id"]) == 32


# ----------------------------------------------------------- schedules

def test_parse_schedule_interval():
    assert lh.parse_schedule("every 30m") == ("interval", 30)
    assert lh.parse_schedule("every 60m") == ("interval", 60)
    assert lh.parse_schedule("every 2h") == ("interval", 120)


def test_parse_schedule_cron_daily():
    kind, fields = lh.parse_schedule("0 1 * * *")
    assert kind == "cron"
    assert fields == [[0], [1], None, None, None]


def test_parse_schedule_cron_with_dow():
    kind, fields = lh.parse_schedule("30 13 * * 0")
    assert kind == "cron"
    assert fields[4] == [0]


def test_parse_schedule_rejects_unsupported():
    assert lh.parse_schedule("*/5 * * * *") is None
    assert lh.parse_schedule("garbage") is None
    assert lh.parse_schedule("") is None


def test_last_expected_fire_daily_one_o_clock():
    sched = lh.parse_schedule("0 1 * * *")
    now = datetime(2026, 8, 23, 4, 0)
    assert lh.last_expected_fire(sched, now) == datetime(2026, 8, 23, 1, 0)


def test_last_expected_fire_returns_none_for_interval():
    sched = lh.parse_schedule("every 30m")
    assert lh.last_expected_fire(sched, now=datetime(2026, 8, 23)) is None


# ------------------------------------------------------------- checks

def make_cfg(**over):
    cfg = {
        "watch_jobs": [], "services": [], "timers": {},
        "send_target": "", "grace_min": 30, "streak_threshold": 2,
        "zombie_hours": 6, "renotify_min": 360,
        "ntfy_url": "", "ntfy_topic": "loop-heartbeat", "ntfy_token": "",
    }
    cfg.update(over)
    return cfg


def test_check_job_streak_alerts_on_real_implementer_history(monkeypatch):
    entries = lh.parse_runs((FIX / "runs_streak.txt").read_text())
    monkeypatch.setattr(lh, "run_cmd", lambda a, timeout=60: (0, (FIX / "runs_streak.txt").read_text()))
    # newest entry in fixture is 2026-08-23T04:00 'running'; stale enough to be a zombie
    now = entries[0]["ts"] + timedelta(hours=7)
    alerts = lh.check_job("Radar implementer — self-improvement loop", "50517c2e700c",
                          "active", "0 4 * * *", now, make_cfg())
    keys = [a["key"] for a in alerts]
    assert any(k.endswith(":streak") for k in keys), keys
    assert any(k.endswith(":zombie") for k in keys), keys


def test_check_job_silent_when_never_ran_after_expected_fire(monkeypatch):
    # healthy cron fixture: last devlog run 2026-08-23T01:19, now is the 24th 04:00
    monkeypatch.setattr(lh, "run_cmd", lambda a, timeout=60: (0, (FIX / "runs_healthy_cron.txt").read_text()))
    now = datetime(2026, 8, 24, 4, 0)
    alerts = lh.check_job("Daily devlog post", "4daccc802319", "active", "0 1 * * *",
                          now, make_cfg())
    assert [a["key"] for a in alerts] == ["job:Daily devlog post:silent"]


def test_check_job_green_when_freshly_run(monkeypatch):
    monkeypatch.setattr(lh, "run_cmd", lambda a, timeout=60: (0, (FIX / "runs_healthy_cron.txt").read_text()))
    now = datetime(2026, 8, 23, 4, 0)  # 3h after the 01:19 run; next fire is tomorrow 01:00
    alerts = lh.check_job("Daily devlog post", "4daccc802319", "active", "0 1 * * *",
                          now, make_cfg())
    assert alerts == []


def test_check_job_interval_silence(monkeypatch):
    # watchdog every 30m: last entry 03:59; now 05:10 -> overdue beyond grace
    text = (FIX / "runs_healthy_interval.txt").read_text()
    monkeypatch.setattr(lh, "run_cmd", lambda a, timeout=60: (0, text))
    now = datetime(2026, 8, 23, 5, 10)
    alerts = lh.check_job("Pi health watchdog", "db50209537ca", "active", "every 30m",
                          now, make_cfg())
    assert [a["key"] for a in alerts] == ["job:Pi health watchdog:silent"]


def test_check_job_interval_green(monkeypatch):
    text = (FIX / "runs_healthy_interval.txt").read_text()
    monkeypatch.setattr(lh, "run_cmd", lambda a, timeout=60: (0, text))
    now = datetime(2026, 8, 23, 4, 10)  # 11 min after 03:59 run
    alerts = lh.check_job("Pi health watchdog", "db50209537ca", "active", "every 30m",
                          now, make_cfg())
    assert alerts == []


def test_check_job_missing_history(monkeypatch):
    monkeypatch.setattr(lh, "run_cmd", lambda a, timeout=60: (0, ""))
    alerts = lh.check_job("X", "abc123def456", "active", "every 30m",
                          datetime(2026, 8, 23), make_cfg())
    assert [a["key"] for a in alerts] == ["job:X:no-history"]


# ------------------------------------------------------------- config

def test_load_config_defaults_without_file(tmp_path):
    cfg = lh.load_config(tmp_path / "nonexistent.conf")
    assert cfg["watch_jobs"] == lh.DEFAULT_WATCH_JOBS
    assert cfg["send_target"] == ""
    assert cfg["grace_min"] == 30 and cfg["streak_threshold"] == 2


def test_load_config_reads_env_file(tmp_path):
    conf = tmp_path / "lh.conf"
    conf.write_text(
        "# comment\n"
        "WATCH_JOBS=Daily devlog post, Radar implementer — self-improvement loop\n"
        "SERVICES=AdGuardHome\n"
        "TIMERS=project-guard:10\n"
        "SEND_TARGET=whatsapp:REDACTED@lid\n"
        "GRACE_MIN=15\n"
    )
    cfg = lh.load_config(conf)
    assert cfg["watch_jobs"] == ["Daily devlog post",
                                 "Radar implementer — self-improvement loop"]
    assert cfg["services"] == ["AdGuardHome"]
    assert cfg["timers"] == {"project-guard": 10}
    assert cfg["send_target"] == "whatsapp:REDACTED@lid"
    assert cfg["grace_min"] == 15


# --------------------------------------------------------- state/dedupe

def test_deliver_dedupes_and_renotifies(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    alert = [{"key": "job:X:streak", "text": "X failing"}]
    cfg = make_cfg(send_target="whatsapp:REDACTED@lid", renotify_min=360)
    sends = []
    monkeypatch.setattr(lh, "send",
                        lambda target, subject, body, dry: sends.append(body) or True)
    sent, sup, rec = lh.deliver(alert, cfg, state_path, dry_run=False,
                                now=datetime(2026, 8, 23, 4))
    assert sent == ["job:X:streak"] and rec == []
    # immediate second call: same condition, inside the renotify window -> suppressed
    sent2, sup2, _ = lh.deliver(alert, cfg, state_path, dry_run=False,
                                now=datetime(2026, 8, 23, 5))
    assert sent2 == [] and sup2 == ["job:X:streak"]
    # after the renotify window it fires again
    sent3, _, _ = lh.deliver(alert, cfg, state_path, dry_run=False,
                             now=datetime(2026, 8, 23, 11))
    assert sent3 == ["job:X:streak"]
    assert len(sends) == 2


def test_deliver_notices_recovery(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(
        {"job:X:streak": {"last_sent": "2026-08-23T04:00:00"}}))
    cfg = make_cfg(send_target="whatsapp:REDACTED@lid")
    sent, sup, rec = lh.deliver([], cfg, state_path, dry_run=True, now=datetime(2026, 8, 23, 5))
    assert rec == ["job:X:streak"] and sent == []


def test_deliver_writes_state_only_on_success(tmp_path):
    state_path = tmp_path / "state.json"
    alert = [{"key": "job:X:streak", "text": "X failing"}]
    cfg = make_cfg(send_target="")  # empty target -> send() returns False
    sent, sup, rec = lh.deliver(alert, cfg, state_path, dry_run=False, now=datetime(1926, 8, 23, 4))
    assert sent == [] and sup == ["job:X:streak"]
    assert not state_path.exists()


# --------------------------------------------------------------- ntfy

class Resp:
    def __init__(self, code):
        self._code = code

    def getcode(self):
        return self._code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_send_uses_ntfy_when_configured(monkeypatch):
    calls = []

    def fake_post(url, headers, payload, timeout=15):
        calls.append((url, headers, payload))
        return True

    monkeypatch.setattr(lh, "ntfy_post", fake_post)
    cfg = make_cfg(send_target="", ntfy_url="http://100.122.94.33:6839",
                   ntfy_topic="loop-heartbeat", ntfy_token="tk_test")
    assert lh.send(cfg, "⚠ loop-heartbeat", "X down", dry_run=False) is True
    url, headers, payload = calls[0]
    assert url == "http://100.122.94.33:6839"
    assert headers["Authorization"] == "Bearer tk_test"
    assert payload["topic"] == "loop-heartbeat"
    assert payload["title"] == "⚠ loop-heartbeat"
    assert payload["message"] == "X down"
    assert payload["tags"] == ["warning"]


def test_send_recovery_notice_uses_check_mark_tag(monkeypatch):
    calls = []
    monkeypatch.setattr(lh, "ntfy_post",
                        lambda u, h, p, timeout=15: calls.append(p) or True)
    cfg = make_cfg(send_target="", ntfy_url="http://n:6839",
                   ntfy_topic="loop-heartbeat", ntfy_token="tk")
    assert lh.send(cfg, "✓ loop-heartbeat", "resolved", dry_run=False) is True
    assert calls[0]["tags"] == ["white_check_mark"]


def test_send_both_channels_and_ntfy_failure_shortcircuit(monkeypatch, capsys):
    # hermes ok, ntfy down -> alert still counts as delivered
    monkeypatch.setattr(lh, "send_hermes", lambda t, s, b: True)
    monkeypatch.setattr(lh, "ntfy_post", lambda u, h, p, timeout=15: False)
    cfg = make_cfg(send_target="whatsapp:REDACTED@lid",
                   ntfy_url="http://n:6839", ntfy_token="tk")
    assert lh.send(cfg, "⚠ loop-heartbeat", "X down", dry_run=False) is True
    assert "ntfy publish failed" not in capsys.readouterr().err


def test_send_all_channels_failed(monkeypatch, capsys):
    monkeypatch.setattr(lh, "send_hermes", lambda t, s, b: False)
    monkeypatch.setattr(lh, "ntfy_post", lambda u, h, p, timeout=15: False)
    cfg = make_cfg(send_target="whatsapp:REDACTED@lid",
                   ntfy_url="http://n:6839", ntfy_token="tk")
    assert lh.send(cfg, "⚠ loop-heartbeat", "X down", dry_run=False) is False
    assert "not delivered" in capsys.readouterr().err


def test_send_dry_run_names_channels(monkeypatch, capsys):
    monkeypatch.setattr(lh, "ntfy_post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                            "dry-run must not POST")))
    cfg = make_cfg(send_target="", ntfy_url="http://n:6839", ntfy_token="tk")
    assert lh.send(cfg, "⚠ loop-heartbeat", "X", dry_run=True) is True
    assert "via ntfy" in capsys.readouterr().out


def test_ntfy_post_returns_true_on_2xx(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return Resp(200)

    monkeypatch.setattr(lh.urllib.request, "urlopen", fake_urlopen)
    assert lh.ntfy_post("http://n:6839", {}, {"topic": "t"}) is True


def test_ntfy_post_returns_false_on_connection_error(monkeypatch, capsys):
    def fake_urlopen(req, timeout=None):
        raise lh.urllib.error.URLError("refused")

    monkeypatch.setattr(lh.urllib.request, "urlopen", fake_urlopen)
    assert lh.ntfy_post("http://n:6839", {}, {"topic": "t"}) is False
    assert "refused" in capsys.readouterr().err


def test_load_config_reads_ntfy_keys(tmp_path):
    conf = tmp_path / "lh.conf"
    conf.write_text(
        "NTFY_URL=http://100.122.94.33:6839/\n"
        "NTFY_TOPIC=loop-heartbeat\n"
        "NTFY_TOKEN=tk_test\n"
    )
    cfg = lh.load_config(conf)
    assert cfg["ntfy_url"] == "http://100.122.94.33:6839"  # trailing / stripped
    assert cfg["ntfy_topic"] == "loop-heartbeat"
    assert cfg["ntfy_token"] == "tk_test"


def test_load_config_ntfy_defaults(tmp_path):
    cfg = lh.load_config(tmp_path / "nonexistent.conf")
    assert cfg["ntfy_url"] == "" and cfg["ntfy_token"] == ""
    assert cfg["ntfy_topic"] == "loop-heartbeat"


def test_end_to_end_dry_run_with_ntfy_configured(monkeypatch, tmp_path, capsys):
    listing = (FIX / "cron_list.txt").read_text()
    streak = (FIX / "runs_streak.txt").read_text()

    def fake_run(argv, timeout=60):
        if "list" in argv:
            return (0, listing)
        if "runs" in argv:
            return (0, streak)
        return (0, "")

    monkeypatch.setattr(lh, "run_cmd", fake_run)
    posts = []
    monkeypatch.setattr(lh, "ntfy_post",
                        lambda u, h, p, timeout=15: posts.append(p) or True)
    monkeypatch.setattr(lh, "send_hermes", lambda t, s, b: True)
    conf = tmp_path / "lh.conf"
    conf.write_text(
        "WATCH_JOBS=Radar implementer — self-improvement loop\n"
        "SEND_TARGET=whatsapp:REDACTED@lid\n"
        "NTFY_URL=http://100.122.94.33:6839\n"
        "NTFY_TOKEN=tk_test\n"
    )
    rc = lh.main(["--config", str(conf), "--state", str(tmp_path / "s.json")],
                 now=datetime(2026, 8, 23, 11, 0))
    out = capsys.readouterr().out
    assert rc == 0
    assert "silently dead" in out
    assert posts, "alerts must be published to ntfy when configured"
    assert all(p["topic"] == "loop-heartbeat" for p in posts)


# ------------------------------------------------------------- systemd

def test_check_service_down(monkeypatch):
    monkeypatch.setattr(lh, "run_cmd", lambda a, timeout=60: (3, ""))
    alerts = lh.check_service("AdGuardHome")
    assert alerts[0]["key"] == "svc:AdGuardHome:down"


def test_check_timer_stale(monkeypatch):
    # craft LastTriggerUSec output older than 1.5x10m + 5m
    orig = lh.run_cmd
    def fake_run(argv, timeout=60):
        if "is-active" in argv:
            return (0, "")
        if "LastTriggerUSec" in argv:
            return (0, "Sun 2026-08-23 03:00:00 IST\n")
        return orig(argv, timeout)
    monkeypatch.setattr(lh, "run_cmd", fake_run)
    alerts = lh.check_timer("project-guard", 10, datetime(2026, 8, 23, 4, 15))
    assert [a["key"] for a in alerts] == ["timer:project-guard:stale"]


def test_check_timer_fresh(monkeypatch):
    orig = lh.run_cmd
    def fake_run(argv, timeout=60):
        if "is-active" in argv:
            return (0, "")
        if "LastTriggerUSec" in argv:
            return (0, "Sun 2026-08-23 04:04:39 IST\n")
        return orig(argv, timeout)
    monkeypatch.setattr(lh, "run_cmd", fake_run)
    alerts = lh.check_timer("project-guard", 10, datetime(2026, 8, 23, 4, 15))
    assert alerts == []


# ------------------------------------------------------------- end2end

def test_end_to_end_dry_run_green(monkeypatch, tmp_path, capsys):
    listing = (FIX / "cron_list.txt").read_text()
    healthy = (FIX / "runs_healthy_cron.txt").read_text()
    monkeypatch.setattr(lh, "run_cmd",
                        lambda a, timeout=60: (0, listing if "list" in a else healthy))
    conf = tmp_path / "lh.conf"
    conf.write_text("WATCH_JOBS=Daily devlog post\nSEND_TARGET=\n")
    rc = lh.main(["--config", str(conf), "--state", str(tmp_path / "s.json"),
                 "--dry-run"], now=datetime(2026, 8, 23, 4, 0))
    out = capsys.readouterr().out
    assert rc == 0
    assert "all green" in out
    assert "would send" not in out


def test_end_to_end_detects_implementer_streak(monkeypatch, tmp_path, capsys):
    listing = (FIX / "cron_list.txt").read_text()
    streak = (FIX / "runs_streak.txt").read_text()
    def fake_run(argv, timeout=60):
        if "list" in argv:
            return (0, listing)
        if "runs" in argv:
            return (0, streak)
        return (0, "")
    monkeypatch.setattr(lh, "run_cmd", fake_run)
    conf = tmp_path / "lh.conf"
    conf.write_text("WATCH_JOBS=Radar implementer — self-improvement loop\nSEND_TARGET=\n")
    rc = lh.main(["--config", str(conf), "--state", str(tmp_path / "s.json"),
                 "--dry-run"], now=datetime(2026, 8, 23, 11, 0))
    out = capsys.readouterr().out
    assert rc == 0
    assert "silently dead" in out


# ---------------------------------------------------------- no secrets

def test_fixtures_contain_no_phone_numbers():
    for f in FIX.iterdir():
        if f.is_file():
            text = f.read_text()
            # fixtures were scrubbed: any real delivery target would show as
            # whatsapp:<digits>; redacted ones are whatsapp:REDACTED@lid
            assert re.search(r"whatsapp:\d{7,}", text) is None, f"{f.name} leaks a JID"
            assert re.search(r"\+353\d{8,}", text) is None, f"{f.name} leaks +353 number"


# ----------------------------------------------------------- healthz checks

def test_parse_healthz():
    _parse_healthz = lh._parse_healthz
    assert _parse_healthz("") == []
    assert _parse_healthz("http://x:1/h") == [("http://x:1/h", "x:1")]
    assert _parse_healthz("http://x:1/h=cs2, http://y:2/=other") == [
        ("http://x:1/h", "cs2"), ("http://y:2/", "other")]


def test_check_healthz_ok():
    # hermetic local server: the healthy path must produce no alerts
    # (the previous live version probed :8092, which only exists on the Pi
    # and made CI red — see run 32978462285)
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"healthy":true}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        alerts = lh.check_healthz(
            f"http://127.0.0.1:{srv.server_port}/healthz", "fake")
        assert alerts == []
    finally:
        srv.shutdown()


def test_check_healthz_down():
    alerts = lh.check_healthz("http://127.0.0.1:59999/healthz", "nope")
    assert alerts and alerts[0]["key"] == "healthz:nope:down"


def test_check_healthz_unhealthy_flag():
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"healthy":false}')
        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        alerts = lh.check_healthz(
            f"http://127.0.0.1:{srv.server_port}/healthz", "fake")
        assert alerts and alerts[0]["key"] == "healthz:fake:unhealthy"
    finally:
        srv.shutdown()
