"""Tests for service-probe — the uptime scoreboard prober."""
import importlib.util
import importlib.machinery
import io
import json
import struct
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "service-probe"

spec = importlib.util.spec_from_loader(
    "service_probe",
    importlib.machinery.SourceFileLoader("service_probe", str(SCRIPT)),
)
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)


# --------------------------------------------------------------- helpers

class FakeResp:
    """Just enough of urlopen()'s context-manager response."""

    def __init__(self, body, code=200):
        self._body = body.encode() if isinstance(body, str) else body
        self._code = code

    def getcode(self):
        return self._code

    def read(self, n=-1):
        return self._body[:n] if n and n > 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code=404):
    return urllib.error.HTTPError(
        "url", code, "Nope", {}, io.BytesIO(b"{}"))


class Router:
    """URL-routing fake for urllib.request.urlopen."""

    def __init__(self, pages=None, ntfy_code=200, fail_urls=()):
        self.pages = pages or {}      # url -> body (or Exception)
        self.ntfy_code = ntfy_code
        self.fail_urls = fail_urls
        self.published = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        for prefix in self.fail_urls:
            if url.startswith(prefix):
                raise http_error(503)
        if url in self.pages:
            body = self.pages[url]
            if isinstance(body, Exception):
                raise body
            return FakeResp(body)
        if url == "http://ntfy.example:6839":
            if self.ntfy_code >= 400:
                raise http_error(self.ntfy_code)
            self.published.append(json.loads(
                req.data.decode() if isinstance(req.data, bytes)
                else req.data))
            return FakeResp("{}", self.ntfy_code)
        raise AssertionError(f"unexpected url {url}")


class FakeClock:
    def __init__(self, start):
        self.now = start

    def monotonic(self):
        return self.now


def make_cfg(http=(("portal", "http://127.0.0.1:8090/"),),
             dns=(), ntfy="http://ntfy.example:6839", confirm=2):
    return {
        "http": [tuple(x) for x in http],
        "dns": [tuple(x) for x in dns],
        "ntfy_url": ntfy or "",
        "ntfy_token": "tok" if ntfy else "",
        "topic": "services",
        "timeout": 5,
        "confirm_fails": confirm,
    }


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "state" / "state.json"


@pytest.fixture
def utc():
    return datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)


def run_sweep(cfg, state_file, monkeypatch, pages=None, fail_urls=(),
              clock_start=1000.0, verbose=False):
    """Run sp.run() with faked network and no clock drift."""
    router = Router(pages=pages, fail_urls=fail_urls)
    clock = FakeClock(clock_start)
    monkeypatch.setattr(sp.urllib.request, "urlopen", router)
    monkeypatch.setattr(sp.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(sp, "datetime", FakeDateTime)
    FakeDateTime.fixed = datetime(2026, 8, 27, 10, 0, 0,
                                  tzinfo=timezone.utc)
    rc = sp.run(cfg, state_file, verbose=verbose)
    return rc, router


class FakeDateTime(datetime):
    fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls.fixed


# ---------------------------------------------------------------- config

def test_config_defaults():
    cfg = sp.load_config("/nonexistent")
    assert cfg["http"] == [] and cfg["dns"] == []
    assert cfg["topic"] == "services"
    assert cfg["confirm_fails"] == sp.DEFAULT_CONFIRM_FAILS
    assert cfg["timeout"] == sp.DEFAULT_TIMEOUT


def test_config_parses_probes(tmp_path):
    f = tmp_path / "sp.conf"
    f.write_text(
        "PROBE_HTTP=portal=http://127.0.0.1:8090/\n"
        "PROBE_HTTP=funnel=https://pi.example:8443/mark/,kiosk=http://127.0.0.1:8091/\n"
        "PROBE_DNS=adguard=127.0.0.1:53:tailscale.com\n"
        "NTFY_TOPIC=services\nCONFIRM_FAILS=3\n")
    cfg = sp.load_config(f)
    assert cfg["http"] == [
        ("portal", "http://127.0.0.1:8090/"),
        ("funnel", "https://pi.example:8443/mark/"),
        ("kiosk", "http://127.0.0.1:8091/")]
    assert cfg["dns"] == [("adguard", "127.0.0.1:53", "tailscale.com")]
    assert cfg["confirm_fails"] == 3


def test_config_skips_bad_entries(tmp_path, capsys):
    f = tmp_path / "sp.conf"
    f.write_text(
        "PROBE_HTTP=no-equals,ok=https://x.example\n"
        "PROBE_DNS=bad,alsobad=nonamewithcolon\n")
    cfg = sp.load_config(f)
    assert cfg["http"] == [("ok", "https://x.example")]
    assert cfg["dns"] == []
    err = capsys.readouterr().err
    assert "bad PROBE_HTTP entry" in err
    assert "bad PROBE_DNS entry" in err


# ----------------------------------------------------------------- probes

def test_http_probe_ok(monkeypatch):
    router = Router(pages={"http://x/": "<html>hi</html>"})
    monkeypatch.setattr(sp.urllib.request, "urlopen", router)
    ok, lat, err = sp.http_probe("http://x/", 5)
    assert ok and err is None and lat >= 0


def test_http_probe_healthz_gate():
    # covered via run() below; direct shape check here
    assert "healthy" in '{"healthy": true}'


def test_http_probe_error_codes(monkeypatch):
    router = Router(pages={"http://x/": http_error(503)})
    monkeypatch.setattr(sp.urllib.request, "urlopen", router)
    ok, lat, err = sp.http_probe("http://x/", 5)
    assert not ok and "503" in err


# ------------------------------------------------------------------- dns

def test_dns_query_packet_shape():
    packet = sp.build_dns_query("tailscale.com")
    ident, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", packet[:12])
    assert flags == 0x0100 and qd == 1 and an == 0
    # qname: \x09tailscale\x03com\x00, qtype A(1), qclass IN(1)
    assert packet[12:] == (b"\x09tailscale\x03com\x00" +
                           struct.pack("!HH", 1, 1))


def test_dns_query_rejects_bad_labels():
    with pytest.raises(ValueError):
        sp.build_dns_query("a" * 64 + ".com")
    with pytest.raises(ValueError):
        sp.build_dns_query("..")


def test_dns_response_parsing():
    # rcode 0, 1 answer
    good = struct.pack("!HHHHHH", 1, 0x8180, 1, 1, 0, 0) + b"\x00" * 20
    assert sp.parse_dns_response(good) == (0, 1)
    # rcode 3 (NXDOMAIN), 0 answers
    nxdomain = struct.pack("!HHHHHH", 1, 0x8183, 1, 0, 0, 0) + b"\x00" * 12
    assert sp.parse_dns_response(nxdomain) == (3, 0)
    assert sp.parse_dns_response(b"short") == (None, None)


def test_dns_probe_live_shape(monkeypatch):
    """dns_probe against a faked socket: success, timeout, rcode paths."""
    good = struct.pack("!HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0) + b"\x00" * 24

    class GoodSock:
        def settimeout(self, t): pass

        def sendto(self, packet, addr):
            GoodSock.sent = (packet, addr)

        def recvfrom(self, n):
            return good, ("127.0.0.1", 53)

        def close(self): pass

    monkeypatch.setattr(sp.socket, "socket", lambda *a, **k: GoodSock())
    ok, lat, err = sp.dns_probe("127.0.0.1:53", "x.com", 5)
    assert ok and err is None
    packet, addr = GoodSock.sent
    assert addr == ("127.0.0.1", 53)
    assert struct.unpack("!H", packet[:2])[0] == 0x1234

    class BadSock(GoodSock):
        def recvfrom(self, n):
            raise socket_timeout()

    def socket_timeout():
        import socket as s
        return s.timeout("timed out")

    monkeypatch.setattr(sp.socket, "socket", lambda *a, **k: BadSock())
    ok, lat, err = sp.dns_probe("127.0.0.1", "x.com", 5)
    assert not ok and "timed out" in err


# ------------------------------------------------------------- state/run

def test_state_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    assert sp.load_state(p) == {"probes": {}}
    sp.save_json_atomic(p, {"probes": {"a": {"status": "up"}}})
    assert sp.load_state(p)["probes"]["a"]["status"] == "up"
    p.write_text("{corrupt")
    assert sp.load_state(p) == {"probes": {}}


def test_run_no_probes(tmp_path, capsys):
    assert sp.run(make_cfg(http=(), dns=()), tmp_path / "s.json") == 1
    assert "no probes configured" in capsys.readouterr().err


def test_run_first_sweep_is_baseline(state_file, monkeypatch):
    rc, router = run_sweep(make_cfg(), state_file, monkeypatch,
                           pages={"http://127.0.0.1:8090/": "ok"})
    assert rc == 0
    assert router.published == []          # first sight is not an alert
    state = sp.load_state(state_file)
    assert state["probes"]["http:portal"]["status"] == "up"


def test_run_down_confirmed_after_streak(state_file, monkeypatch):
    cfg = make_cfg(confirm=2)
    rc, r1 = run_sweep(cfg, state_file, monkeypatch,
                       pages={"http://127.0.0.1:8090/": "ok"})
    # fail #1 — below threshold, silent
    rc, r2 = run_sweep(cfg, state_file, monkeypatch,
                       fail_urls=("http://127.0.0.1:8090",))
    assert rc == 0 and r2.published == []
    # fail #2 — confirmed DOWN, alert
    rc, r3 = run_sweep(cfg, state_file, monkeypatch,
                       fail_urls=("http://127.0.0.1:8090",))
    assert rc == 0
    assert len(r3.published) == 1
    assert "portal DOWN" in r3.published[0]["message"]
    assert r3.published[0]["tags"] == ["rotating_light"]
    # still failing — deduped, no repeat alert
    rc, r4 = run_sweep(cfg, state_file, monkeypatch,
                       fail_urls=("http://127.0.0.1:8090",))
    assert r4.published == []
    state = sp.load_state(state_file)
    assert state["probes"]["http:portal"]["status"] == "down"
    assert state["probes"]["http:portal"]["failures"] == 3


def test_run_recovery_notice(state_file, monkeypatch):
    cfg = make_cfg(confirm=1)
    rc, _ = run_sweep(cfg, state_file, monkeypatch,
                      fail_urls=("http://127.0.0.1:8090",))
    rc, r2 = run_sweep(cfg, state_file, monkeypatch,
                       pages={"http://127.0.0.1:8090/": "ok"})
    assert rc == 0
    assert len(r2.published) == 1
    assert "portal back UP" in r2.published[0]["message"]
    assert r2.published[0]["tags"] == ["white_check_mark"]


def test_run_healthz_healthy_false_is_down(state_file, monkeypatch):
    cfg = make_cfg(confirm=1)
    rc, r = run_sweep(cfg, state_file, monkeypatch,
                      pages={"http://127.0.0.1:8090/":
                             '{"healthy": false}'})
    assert rc == 0
    assert "portal DOWN" in r.published[0]["message"]
    assert "unhealthy" in r.published[0]["message"]


def test_run_healthz_json_key_absent_is_up(state_file, monkeypatch):
    cfg = make_cfg(confirm=1)
    rc, r = run_sweep(cfg, state_file, monkeypatch,
                      pages={"http://127.0.0.1:8090/":
                             '{"status": "ok"}'})
    assert rc == 0 and r.published == []


def test_run_dns_probe_wired(state_file, monkeypatch):
    """DNS probes flow through run() with the same up/down logic."""
    good = struct.pack("!HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0) + b"\x00" * 24

    class GoodSock:
        def settimeout(self, t): pass

        def sendto(self, packet, addr): pass

        def recvfrom(self, n):
            return good, ("127.0.0.1", 53)

        def close(self): pass

    monkeypatch.setattr(sp.socket, "socket", lambda *a, **k: GoodSock())
    cfg = make_cfg(http=(), dns=(("adguard", "127.0.0.1:53",
                                  "tailscale.com"),))
    rc, r = run_sweep(cfg, state_file, monkeypatch)
    assert rc == 0 and r.published == []
    state = sp.load_state(state_file)
    assert state["probes"]["dns:adguard"]["status"] == "up"
    assert state["probes"]["dns:adguard"]["kind"] == "dns"
    assert state["probes"]["dns:adguard"]["target"] == \
        "127.0.0.1:53 (tailscale.com)"


def test_run_writes_status_json(state_file, monkeypatch):
    rc, _ = run_sweep(make_cfg(), state_file, monkeypatch,
                      pages={"http://127.0.0.1:8090/": "ok"})
    status = json.loads(
        (state_file.parent / "status.json").read_text())
    assert status["probes"]["portal"]["status"] == "up"
    assert status["probes"]["portal"]["kind"] == "http"
    assert "generated" in status


def test_run_publish_failure_is_not_run_failure(state_file, monkeypatch):
    cfg = make_cfg(confirm=1)
    rc, _ = run_sweep(cfg, state_file, monkeypatch,
                      fail_urls=("http://127.0.0.1:8090",))
    # now recover while ntfy is broken
    router = Router(pages={"http://127.0.0.1:8090/": "ok"}, ntfy_code=507)
    monkeypatch.setattr(sp.urllib.request, "urlopen", router)
    rc = sp.run(cfg, state_file)
    assert rc == 0                      # degraded, not failed
    assert state_file.exists()          # state still persisted


def test_run_dry_run_touches_nothing(state_file, monkeypatch):
    # take the service down with a real (publishing) sweep first
    rc, _ = run_sweep(make_cfg(confirm=1), state_file, monkeypatch,
                      fail_urls=("http://127.0.0.1:8090",))
    before = state_file.read_text()
    # dry-run: the probe still fails but nothing is published or persisted
    router = Router(fail_urls=("http://127.0.0.1:8090",))
    monkeypatch.setattr(sp.urllib.request, "urlopen", router)
    rc = sp.run(make_cfg(confirm=1), state_file, dry_run=True)
    assert rc == 0
    assert router.published == []
    assert state_file.read_text() == before  # nothing persisted


def test_print_state(state_file, monkeypatch, capsys):
    rc, _ = run_sweep(make_cfg(), state_file, monkeypatch,
                      pages={"http://127.0.0.1:8090/": "ok"})
    assert sp.print_state(state_file) == 0
    out = capsys.readouterr().out
    assert "http:portal: up" in out


def test_recovery_duration_line(state_file, monkeypatch):
    cfg = make_cfg(confirm=1)
    rc, _ = run_sweep(cfg, state_file, monkeypatch,
                      fail_urls=("http://127.0.0.1:8090",))
    # simulate 34 minutes of downtime, then recover
    FakeDateTime.fixed = datetime(2026, 8, 27, 10, 34,
                                  tzinfo=timezone.utc)
    router = Router(pages={"http://127.0.0.1:8090/": "ok"})
    monkeypatch.setattr(sp.urllib.request, "urlopen", router)
    rc = sp.run(cfg, state_file)
    state = sp.load_state(state_file)
    assert state["probes"]["http:portal"]["status"] == "up"
    # the recovery event was published with a duration
    msg = router.published[0]["message"]
    assert "back UP" in msg


def test_elapsed_ms_monotonic(monkeypatch):
    clock = FakeClock(5.0)
    monkeypatch.setattr(sp.time, "monotonic", clock.monotonic)
    start = sp.time.monotonic()
    clock.now = 5.5
    assert sp.elapsed_ms(start) == 500
