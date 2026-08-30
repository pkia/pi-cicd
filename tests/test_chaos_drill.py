"""Tests for chaos-drill — the nightly deliberate-failure drills.

Hermetic: no network beyond loopback, no real /etc reads, no ntfy.
The dead-port drill runs the REAL service-probe pipeline (imported from
the sibling script) against a shadow config and throwaway servers on
127.0.0.1 ephemeral ports.
"""
import importlib.util
import importlib.machinery
import io
import json
import socket
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "chaos-drill"

spec = importlib.util.spec_from_loader(
    "chaos_drill",
    importlib.machinery.SourceFileLoader("chaos_drill", str(SCRIPT)),
)
cd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cd)

BASE_CFG = {"ntfy_url": "http://ntfy.test", "ntfy_token": "tok",
            "topic": "chaos", "timeout": 3, "sub_token": "",
            "heal_target": "http://127.0.0.1:1/", "allowed":
            {"backups", "chaos", "releases", "services", "radar"},
            "enabled": set()}


def make_ctx(**over):
    ctx = {"cfg": dict(BASE_CFG), "probe_conf": "/nonexistent.conf",
           "topic": "chaos", "dry_run": False,
           "heal_target": "http://127.0.0.1:1/", "retarget_topic": "chaos",
           "tokens": {}}
    ctx.update(over)
    return ctx


# --------------------------------------------------------------- config

def test_load_config_defaults(tmp_path):
    conf = tmp_path / "chaos-drill.conf"
    conf.write_text("NTFY_URL=http://x.test/\nNTFY_TOKEN=t\n# comment\n\n")
    cfg = cd.load_config(conf)
    assert cfg["ntfy_url"] == "http://x.test"  # trailing slash stripped
    assert cfg["topic"] == "chaos"
    assert cfg["timeout"] == 15
    assert cfg["sub_token"] == ""
    assert cfg["heal_target"] == "http://127.0.0.1:8090/"
    assert cfg["enabled"] == set()


def test_load_config_missing_file_gives_empty():
    cfg = cd.load_config("/nonexistent/chaos-drill.conf")
    assert cfg["ntfy_url"] == ""
    assert cfg["enabled"] == set()


def test_load_config_values_and_drill_filter(tmp_path):
    conf = tmp_path / "c.conf"
    conf.write_text("NTFY_URL=http://n.test\nNTFY_TOKEN=k\n"
                    "TIMEOUT_SECS=4\n"
                    "NTFY_SUB_TOKEN=/etc/ntfy/tokens/subscriber.txt\n"
                    "SUBSCRIBER_ALLOWED_TOPICS=chaos, services\n"
                    "HEAL_TARGET=http://127.0.0.1:8090/\n"
                    "DRILL=ntfy-auth\nDRILL=probe-timer-alive\n")
    cfg = cd.load_config(conf)
    assert cfg["timeout"] == 4
    assert cfg["sub_token"] == "/etc/ntfy/tokens/subscriber.txt"
    assert cfg["allowed"] == {"chaos", "services"}
    assert cfg["enabled"] == {"ntfy-auth", "probe-timer-alive"}


def test_load_config_bad_timeout_falls_back(tmp_path):
    conf = tmp_path / "c.conf"
    conf.write_text("TIMEOUT_SECS=nonsense\n")
    assert cd.load_config(conf)["timeout"] == 15


def test_load_config_negative_timeout_sanitised(tmp_path):
    conf = tmp_path / "c.conf"
    conf.write_text("TIMEOUT_SECS=-5\n")
    assert cd.load_config(conf)["timeout"] == 15


# ------------------------------------------------------------- rotation

def test_pick_rotation_covers_all_drills():
    names = [n for n, _ in cd.MANIFEST]
    picks = set()
    day = datetime(2026, 8, 30, tzinfo=timezone.utc)
    for i in range(3 * len(names)):
        picks.add(cd.pick_rotation(names, day + timedelta(days=i)))
    assert picks == set(names)


def test_pick_rotation_stable_within_a_day():
    names = [n for n, _ in cd.MANIFEST]
    a = datetime(2026, 8, 30, 4, 45, tzinfo=timezone.utc)
    b = datetime(2026, 8, 30, 23, 59, tzinfo=timezone.utc)
    assert cd.pick_rotation(names, a) == cd.pick_rotation(names, b)


# ------------------------------------------------------------ plumbing

def test_http_request_returns_none_status_on_refused():
    # nothing listens on 127.0.0.1:1 in test envs (privileged port)
    status, body = cd.http_request("http://127.0.0.1:1/x", timeout=2)
    assert status is None
    assert isinstance(body, str)


class TinyHTTP:
    """A one-shot or persistent HTTP server on an ephemeral port."""

    def __init__(self, persistent=False):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        if persistent:
            self._t.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.recv(1024)
                conn.sendall(b"HTTP/1.0 200 OK\r\n"
                             b"Content-Length: 2\r\n\r\nok")
            except OSError:
                pass
            finally:
                conn.close()

    def answer_once(self):
        self._t.start()

    def close(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def test_http_request_gets_real_answer():
    srv = TinyHTTP(persistent=True)
    try:
        status, body = cd.http_request(f"http://127.0.0.1:{srv.port}/",
                                       timeout=5)
    finally:
        srv.close()
    assert status == 200
    assert "ok" in body


def test_read_sub_token_direct_read(tmp_path):
    f = tmp_path / "sub.txt"
    f.write_text("tok_sub\n")
    assert cd.read_sub_token(f) == "tok_sub"


def test_read_sub_token_unreadable_and_no_sudo(monkeypatch):
    def deny(*cmd):
        return 1, "sudo: a password is required"
    monkeypatch.setattr(cd, "run_cmd", deny)
    assert cd.read_sub_token("/nonexistent/sub.txt") == ""


# ------------------------------------------------------ dead-port drill

class LiveServer(TinyHTTP):
    """Answers HTTP 200 — the probe target that stays up."""


def make_probe_conf(path, up_port, confirm_fails=1):
    path.write_text(
        "NTFY_URL=\nNTFY_TOKEN=\nNTFY_TOPIC=services\n"
        f"PROBE_HTTP=real-up=http://127.0.0.1:{up_port}/\n"
        f"CONFIRM_FAILS={confirm_fails}\n")


@pytest.fixture
def shadow_env(tmp_path):
    """Live-ish probe conf with one real up target + the heal server."""
    heal = LiveServer(persistent=True)
    base_conf = tmp_path / "service-probe.conf"
    make_probe_conf(base_conf, heal.port)
    yield base_conf, heal
    heal.close()


def test_dead_port_drill_passes_end_to_end(shadow_env):
    base_conf, heal = shadow_env
    ctx = make_ctx(probe_conf=str(base_conf),
                   heal_target=f"http://127.0.0.1:{heal.port}/")
    result, detail = cd.drill_service_probe_dead_port(ctx)
    assert result == "pass", detail
    assert "DOWN detected by real pipeline" in detail
    assert "recovery noticed after heal" in detail


def test_dead_port_drill_skips_when_no_conf(tmp_path):
    ctx = make_ctx(probe_conf="/nonexistent/probe.conf")
    result, detail = cd.drill_service_probe_dead_port(ctx)
    assert result == "skip"
    assert "cannot read" in detail


def test_dead_port_drill_fails_when_pipeline_blind(shadow_env):
    """CONFIRM_FAILS beyond MAX_SWEEPS -> no DOWN alert -> FAIL (the
    drill exists to catch exactly this kind of blindness)."""
    base_conf, heal = shadow_env
    make_probe_conf(base_conf, heal.port, confirm_fails=99)
    ctx = make_ctx(probe_conf=str(base_conf),
                   heal_target=f"http://127.0.0.1:{heal.port}/")
    result, detail = cd.drill_service_probe_dead_port(ctx)
    assert result == "fail"
    assert "never went DOWN" in detail


def test_build_shadow_config_shape():
    base = ("NTFY_URL=http://n\nNTFY_TOKEN=k\nNTFY_TOPIC=services\n"
            "PROBE_HTTP=a=http://127.0.0.1:1/\nCONFIRM_FAILS=2\n")
    out = cd.build_shadow_config(base, "http://127.0.0.1:59999/", "chaos")
    lines = out.splitlines()
    assert "NTFY_TOPIC=services" in lines
    assert "NTFY_TOPIC=chaos" in lines
    assert lines.index("NTFY_TOPIC=services") < lines.index("NTFY_TOPIC=chaos")
    assert "PROBE_HTTP=chaos-dead-port=http://127.0.0.1:59999/" in lines
    assert "PROBE_HTTP=a=http://127.0.0.1:1/" in lines
    assert "CONFIRM_FAILS=2" in lines
    # and service-probe's own parser agrees: last NTFY_TOPIC wins,
    # repeated PROBE_HTTP accumulate, the drill probe parses as http
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
        f.write(out)
        path = f.name
    parsed = cd.load_service_probe().load_config(path)
    assert parsed["topic"] == "chaos"
    assert ("chaos-dead-port", "http://127.0.0.1:59999/") in parsed["http"]
    assert ("a", "http://127.0.0.1:1/") in parsed["http"]


# ---------------------------------------------------------- ntfy drill

def test_ntfy_auth_drill_passes_fail_closed(monkeypatch):
    """Anonymous denied (403) + publisher accepted -> pass, with the
    read-back check added when a subscriber token resolves."""
    calls = {}

    def fake_publish(url, headers, payload, timeout=None, mute_file=None,
                     _urlopen=None):
        calls["publish"] = payload
        return True

    monkeypatch.setattr(cd.ntfy_lib, "publish", fake_publish)

    marker_seq = []

    def fake_http(url, method="GET", headers=None, data=None, timeout=15):
        if method == "POST":
            return 403, "denied"
        # capture the marker the drill published, serve it back in the
        # read-back poll (ntfy JSON stream: one object per line)
        if "publish" in calls:
            marker_seq.append(calls["publish"]["message"])
        body = "\n".join(json.dumps({"message": m}) for m in marker_seq)
        return 200, body

    monkeypatch.setattr(cd, "http_request", fake_http)
    monkeypatch.setattr(cd, "read_sub_token", lambda p: "tok_sub")

    ctx = make_ctx()
    ctx["cfg"] = dict(BASE_CFG, sub_token="/etc/ntfy/tokens/subscriber.txt")
    result, detail = cd.drill_ntfy_auth(ctx)
    assert result == "pass", detail
    assert "anonymous publish denied" in detail
    assert "publisher publish accepted" in detail
    assert "subscriber read-back of receipt" in detail
    assert calls["publish"]["topic"] == "chaos"


def test_ntfy_auth_drill_fails_when_server_open(monkeypatch):
    def fake_publish(url, headers, payload, **kw):
        return True

    monkeypatch.setattr(cd.ntfy_lib, "publish", fake_publish)

    def fake_http(url, method="GET", headers=None, data=None, timeout=15):
        return 200, "open server"  # anonymous publish NOT denied

    monkeypatch.setattr(cd, "http_request", fake_http)
    ctx = make_ctx()
    result, detail = cd.drill_ntfy_auth(ctx)
    assert result == "fail"
    assert "anonymous publish denied" in detail
    assert "OPEN" in detail


def test_ntfy_auth_drill_fails_when_publisher_rejected(monkeypatch):
    def fake_publish(url, headers, payload, **kw):
        return False

    monkeypatch.setattr(cd.ntfy_lib, "publish", fake_publish)

    def fake_http(url, method="GET", headers=None, data=None, timeout=15):
        return 403, ""

    monkeypatch.setattr(cd, "http_request", fake_http)
    ctx = make_ctx()
    result, detail = cd.drill_ntfy_auth(ctx)
    assert result == "fail"
    assert "publisher publish accepted" in detail


def test_ntfy_auth_drill_skips_without_config():
    ctx = make_ctx()
    ctx["cfg"] = dict(BASE_CFG, ntfy_url="", ntfy_token="")
    result, detail = cd.drill_ntfy_auth(ctx)
    assert result == "skip"
    assert "not configured" in detail


def test_ntfy_auth_drill_skips_on_unreadable_sub_token(monkeypatch):
    def fake_publish(url, headers, payload, **kw):
        return True

    monkeypatch.setattr(cd.ntfy_lib, "publish", fake_publish)
    monkeypatch.setattr(cd, "http_request",
                        lambda *a, **k: (403, ""))
    monkeypatch.setattr(cd, "read_sub_token", lambda p: "")
    ctx = make_ctx()
    ctx["cfg"] = dict(BASE_CFG, sub_token="/unreadable/token")
    result, detail = cd.drill_ntfy_auth(ctx)
    assert result == "skip"
    assert "unreadable" in detail


# --------------------------------------------------------- timer drill

def test_probe_timer_drill_passes_when_recent(monkeypatch, tmp_path):
    def fake_run_cmd(*cmd):
        if "is-active" in cmd:
            return 0, "active"
        if "ExecMainExitTimestampMonotonic" in cmd:
            # monotonic us at boot+99700 on a box up 100000 s
            # -> last sweep 300 s (5 m) ago
            return 0, str(int((100000 - 300) * 1e6))
        return 1, ""

    monkeypatch.setattr(cd, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(cd, "read_uptime", lambda path="/proc/uptime":
                        100000.0)
    ctx = make_ctx()
    result, detail = cd.drill_probe_timer_alive(ctx)
    assert result == "pass", detail
    assert "last sweep 5m ago" in detail


def test_probe_timer_drill_fails_when_timer_inactive(monkeypatch):
    monkeypatch.setattr(cd, "run_cmd",
                        lambda *cmd: (1, "inactive"))
    ctx = make_ctx()
    result, detail = cd.drill_probe_timer_alive(ctx)
    assert result == "fail"
    assert "not active" in detail


def test_probe_timer_drill_fails_on_stale_sweep(monkeypatch):
    def fake_run_cmd(*cmd):
        if "is-active" in cmd:
            return 0, "active"
        # sweep at boot+3600 on a box up 7200 s -> 3600 s (60 m) ago
        return 0, str(int(3600 * 1e6))

    monkeypatch.setattr(cd, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(cd, "read_uptime", lambda path="/proc/uptime":
                        7200.0)
    ctx = make_ctx()
    result, detail = cd.drill_probe_timer_alive(ctx)
    assert result == "fail"
    assert "sweep 60m ago" in detail


def test_probe_timer_drill_skips_never_ran(monkeypatch):
    def fake_run_cmd(*cmd):
        if "is-active" in cmd:
            return 0, "active"
        return 0, "0"  # no last-run timestamp recorded yet

    monkeypatch.setattr(cd, "run_cmd", fake_run_cmd)
    ctx = make_ctx()
    result, detail = cd.drill_probe_timer_alive(ctx)
    assert result == "fail"
    assert "timestamp" in detail.lower()


# ------------------------------------------------------------------ run

def test_run_publishes_receipt_and_persists(tmp_path, monkeypatch):
    published = {}

    def fake_publish(url, headers, payload, timeout=None, mute_file=None,
                     _urlopen=None):
        published.update(payload)
        return True

    monkeypatch.setattr(cd.ntfy_lib, "publish", fake_publish)

    def ok_drill(ctx):
        return "pass", "all good"

    monkeypatch.setattr(cd, "MANIFEST", [("always-ok", ok_drill)])
    cfg = dict(BASE_CFG)
    state = tmp_path / "state.json"
    rc = cd.run(cfg, state, drills=["always-ok"])
    assert rc == 0
    assert published["topic"] == "chaos"
    assert "1 pass" in published["title"]
    assert "[PASS] always-ok" in published["message"]
    data = json.loads(state.read_text())
    assert data["history"][-1]["drills"][0]["result"] == "pass"
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["drills"]["always-ok"]["result"] == "pass"
    assert "last_run" in status


def test_run_returns_1_on_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(cd.ntfy_lib, "publish",
                        lambda *a, **k: True)

    def bad_drill(ctx):
        return "fail", "broken detection"

    monkeypatch.setattr(cd, "MANIFEST", [("always-bad", bad_drill)])
    rc = cd.run(dict(BASE_CFG), tmp_path / "state.json",
                drills=["always-bad"])
    assert rc == 1


def test_run_skip_is_not_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cd.ntfy_lib, "publish",
                        lambda *a, **k: True)

    def skip_drill(ctx):
        return "skip", "not configured"

    monkeypatch.setattr(cd, "MANIFEST", [("always-skip", skip_drill)])
    rc = cd.run(dict(BASE_CFG), tmp_path / "state.json",
                drills=["always-skip"])
    assert rc == 0
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["drills"]["always-skip"]["result"] == "skip"


def test_run_unknown_drill_rejected(tmp_path):
    rc = cd.run(dict(BASE_CFG), tmp_path / "state.json",
                drills=["no-such-drill"])
    assert rc == 1
    assert not (tmp_path / "state.json").exists()


def test_run_respects_drill_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(cd.ntfy_lib, "publish", lambda *a, **k: True)
    seen = []

    def d1(ctx):
        seen.append(1)
        return "pass", "one"

    def d2(ctx):
        seen.append(2)
        return "pass", "two"

    monkeypatch.setattr(cd, "MANIFEST",
                        [("drill-one", d1), ("drill-two", d2)])
    cfg = dict(BASE_CFG, enabled={"drill-one"})
    rc = cd.run(cfg, tmp_path / "state.json", drills=["drill-one"])
    assert rc == 0
    assert seen == [1]


def test_run_dry_run_executes_nothing(tmp_path, monkeypatch):
    def boom(ctx):
        raise AssertionError("dry-run must not execute drills")

    monkeypatch.setattr(cd, "MANIFEST", [("boom-drill", boom)])
    cfg = dict(BASE_CFG)
    state = tmp_path / "state.json"
    rc = cd.run(cfg, state, drills=["boom-drill"], dry_run=True)
    assert rc == 0
    assert not state.exists()


def test_run_crashed_drill_is_fail_not_hang(tmp_path, monkeypatch):
    monkeypatch.setattr(cd.ntfy_lib, "publish", lambda *a, **k: True)

    def crashy(ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cd, "MANIFEST", [("crashy", crashy)])
    rc = cd.run(dict(BASE_CFG), tmp_path / "state.json",
                drills=["crashy"])
    assert rc == 1
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["drills"]["crashy"]["result"] == "fail"
    assert "kaboom" in status["drills"]["crashy"]["detail"]


def test_run_state_history_capped_at_50(tmp_path, monkeypatch):
    monkeypatch.setattr(cd.ntfy_lib, "publish", lambda *a, **k: True)
    monkeypatch.setattr(cd, "MANIFEST",
                        [("ok", lambda ctx: ("pass", "fine"))])
    state = tmp_path / "state.json"
    for _ in range(55):
        assert cd.run(dict(BASE_CFG), state, drills=["ok"]) == 0
    data = json.loads(state.read_text())
    assert len(data["history"]) == 50


def test_publish_receipt_title_on_fail(monkeypatch):
    receipts = [{"drill": "x", "result": "fail", "detail": "no alert",
                 "at": "t"},
                {"drill": "y", "result": "pass", "detail": "ok",
                 "at": "t"}]
    captured = {}

    def fake_publish(url, headers, payload, timeout=None, mute_file=None,
                     _urlopen=None):
        captured.update(payload)
        return True

    monkeypatch.setattr(cd.ntfy_lib, "publish", fake_publish)
    cd.publish_receipt(dict(BASE_CFG), receipts,
                       [r for r in receipts if r["result"] == "fail"])
    assert "FAIL" in captured["title"]
    assert "x" in captured["title"]
    assert "[FAIL] x" in captured["message"]
    assert "[PASS] y" in captured["message"]


# ----------------------------------------------------------------- mute

def test_receipt_inherits_global_mute(tmp_path, monkeypatch):
    """A standing mute must suppress the receipt without touching the
    network — inherited from ntfy_lib, proven here end to end."""
    mute = tmp_path / "mute"
    mute.write_text("chaos drill test\n")
    # ntfy_lib reads the env var at import; patch the resolved default
    # the module actually uses at call time.
    monkeypatch.setattr(cd.ntfy_lib, "DEFAULT_MUTE_FILE", str(mute))

    def network_must_not_run(*a, **k):
        raise AssertionError("muted publish must not reach the network")

    published = cd.ntfy_lib.publish(
        "http://x", {}, {"topic": "chaos", "message": "m"},
        _urlopen=network_must_not_run)
    assert published is True  # muted counts as delivered


# ----------------------------------------------------------------- list

def test_list_prints_manifest_and_receipts(tmp_path, capsys):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "history": [{"at": "2026-08-30T04:45:00+00:00",
                     "drills": [{"drill": "ntfy-auth", "result": "pass",
                                 "detail": "denied+accepted",
                                 "at": "x"}],
                     "rotation": ["a"], "picked": ["ntfy-auth"]}]}))
    rc = cd.print_list(state)
    out = capsys.readouterr().out
    assert rc == 0
    assert "manifest" in out
    assert "ntfy-auth: pass" in out
    assert "never run" in out


def test_load_state_missing_or_corrupt(tmp_path):
    assert cd.load_state(tmp_path / "none.json") == {"history": []}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cd.load_state(bad) == {"history": []}
    worse = tmp_path / "worse.json"
    worse.write_text(json.dumps({"nothistory": 1}))
    assert cd.load_state(worse) == {"history": []}
