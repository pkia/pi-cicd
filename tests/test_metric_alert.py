"""Tests for metric-alert — stdlib rule-check alerting on Prometheus.

Hermetic: no live Prometheus, no live ntfy. query() is driven through an
injected _urlopen, run() through a stubbed query + captured ntfy_post.
"""
import importlib.machinery
import importlib.util
import json
import os
import urllib.error
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "metric-alert"
TEMPLATE = HERE.parent / "templates" / "metric-alert.conf.example"

spec = importlib.util.spec_from_loader(
    "metric_alert",
    importlib.machinery.SourceFileLoader("metric_alert", str(SCRIPT)),
)
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)


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


def prom_body(values, metric=None):
    """Prometheus instant-query JSON with one or more series."""
    result = []
    if isinstance(values, (int, float)):
        values = [values]
    for i, value in enumerate(values):
        m = dict(metric or ({} if len(values) == 1 else {"zone": f"z{i}"}))
        m.setdefault("__name__", "node_thermal_zone_temp")
        result.append({"metric": m, "value": [1700000000.0, str(value)]})
    return json.dumps({"status": "success", "data": {"result": result}})


def prom_ok(values):
    """Stub for ma.query: always returns these series."""
    return lambda url, expr, timeout, _urlopen=None: (
        json.loads(prom_body(values))["data"]["result"], None)


def prom_error(msg="boom"):
    return lambda url, expr, timeout, _urlopen=None: (None, msg)


def make_cfg(rules, confirm=2):
    return {"rules": rules, "prom_url": "http://127.0.0.1:9090",
            "ntfy_url": "http://ntfy.invalid:6839", "ntfy_token": "pt",
            "topic": "services", "timeout": 5, "confirm_fails": confirm}


RULE = {"name": "CpuTempC", "op": ">", "threshold": 80.0,
        "expr": "node_thermal_zone_temp"}


class Capture:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, cfg, title, message, tags, timeout):
        self.calls.append({"title": title, "message": message, "tags": tags,
                           "topic": cfg["topic"]})
        return self.ok


# ----------------------------------------------------------------- rules

def test_parse_rule_keeps_promql_verbatim():
    rule = ma.parse_rule(
        'DiskRootPct > 90 = 100 - (node_filesystem_avail_bytes'
        '{mountpoint="/",fstype="ext4"} / node_filesystem_size_bytes) * 100')
    assert rule["name"] == "DiskRootPct"
    assert rule["op"] == ">" and rule["threshold"] == 90.0
    assert rule["expr"].startswith("100 - (node_filesystem_avail_bytes")
    assert '{mountpoint="/",fstype="ext4"}' in rule["expr"]


@pytest.mark.parametrize("op", [">", ">=", "<", "<="])
def test_parse_rule_accepts_every_operator(op):
    assert ma.parse_rule(f"R {op} 1 = up")["op"] == op


@pytest.mark.parametrize("bad", [
    "NoEquals",                       # no expression
    "Name ~ 5 = up",                  # unsupported operator
    "Name > five = up",               # non-numeric threshold
    "Name > 5 =",                     # empty expression
    "Too many tokens > 5 = up",       # four head fields
])
def test_parse_rule_rejects_malformed(bad):
    assert ma.parse_rule(bad) is None


def test_load_config_defaults_and_bad_rule_skipped(tmp_path, capsys):
    conf = tmp_path / "metric-alert.conf"
    conf.write_text(
        "# comment\nNTFY_URL=http://ntfy:6839/\nNTFY_TOKEN=t\n"
        "PROM_URL=http://127.0.0.1:9090/\n"
        "RULE=CpuTempC > 80 = node_thermal_zone_temp\n"
        "RULE=broken-line\n"
        "CONFIRM_FAILS=3\nTIMEOUT_SECS=4\n")
    cfg = ma.load_config(str(conf))
    assert [r["name"] for r in cfg["rules"]] == ["CpuTempC"]
    assert cfg["ntfy_url"] == "http://ntfy:6839"       # trailing slash gone
    assert cfg["prom_url"] == "http://127.0.0.1:9090"
    assert cfg["topic"] == "services"                  # pinned default
    assert cfg["confirm_fails"] == 3 and cfg["timeout"] == 4
    assert "bad RULE" in capsys.readouterr().err


def test_missing_config_is_no_rules_not_a_crash(tmp_path, capsys):
    cfg = ma.load_config(str(tmp_path / "nope.conf"))
    assert cfg["rules"] == [] and cfg["topic"] == "services"
    assert "no config at" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_unreadable_config_says_so(tmp_path, capsys):
    """The live trap: root-created config, timer runs as ev."""
    conf = tmp_path / "metric-alert.conf"
    conf.write_text("RULE=CpuTempC > 80 = node_thermal_zone_temp\n")
    conf.chmod(0o000)
    cfg = ma.load_config(str(conf))
    assert cfg["rules"] == []
    assert "not readable by this user" in capsys.readouterr().err


def test_shipped_template_parses_cleanly(capsys):
    """The example config is the real starting point — it must be valid."""
    cfg = ma.load_config(str(TEMPLATE))
    names = [r["name"] for r in cfg["rules"]]
    assert names == ["DiskRootPct", "CpuTempC", "MemUsedPct", "FailedUnits",
                     "ScrapeTargetDown"]
    assert capsys.readouterr().err == ""               # nothing skipped
    assert {r["op"] for r in cfg["rules"]} == {">", "<"}  # both directions


# --------------------------------------------------------------- query()

def test_query_urlencodes_expression_and_parses_series():
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return FakeResp(prom_body([42.5]))

    result, error = ma.query("http://127.0.0.1:9090",
                             'up{job="ntfy"} / 2', 5, _urlopen=fake_urlopen)
    assert error is None and len(result) == 1
    assert "/api/v1/query?query=up%7Bjob%3D%22ntfy%22%7D+%2F+2" in seen["url"]


@pytest.mark.parametrize("boom,expected", [
    (urllib.error.HTTPError("u", 500, "err", {}, None), "HTTP 500"),
    (urllib.error.URLError("refused"), "refused"),
    (OSError("timeout"), "timeout"),
])
def test_query_transport_errors_are_reported_not_raised(boom, expected):
    def fake_urlopen(req, timeout=None):
        raise boom

    result, error = ma.query("http://127.0.0.1:9090", "up", 5,
                             _urlopen=fake_urlopen)
    assert result is None and expected in error


def test_query_reports_bad_json_and_prometheus_error():
    bad, err = ma.query("http://p", "up", 5,
                        _urlopen=lambda r, timeout=None: FakeResp("nope"))
    assert bad is None and "bad JSON" in err
    body = json.dumps({"status": "error", "error": "parse error"})
    bad, err = ma.query("http://p", "up", 5,
                        _urlopen=lambda r, timeout=None: FakeResp(body))
    assert bad is None and "parse error" in err


# --------------------------------------------------------------- worst()

def test_worst_picks_extreme_firing_series_and_names_it():
    result = json.loads(prom_body([71.0, 84.5], metric={}))["data"]["result"]
    value, label, count, seen = ma.worst(result, ">", 80.0)
    assert (value, count, seen) == (84.5, 1, 2)
    assert label == "zone=z1" or "z1" in label


def test_worst_for_less_than_and_no_data():
    result = json.loads(prom_body([1.0, 0.0], metric={}))["data"]["result"]
    value, _label, count, seen = ma.worst(result, "<", 1.0)
    assert (value, count, seen) == (0.0, 1, 2)
    assert ma.worst([], ">", 1.0) == (None, "", 0, 0)


def test_no_data_is_never_an_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(ma, "query", prom_ok([]))
    state = tmp_path / "state.json"
    assert ma.run(make_cfg([RULE]), state) == 0
    assert ma.load_state(state)["rules"]["CpuTempC"]["status"] == "nodata"


# ------------------------------------------------------------------ run

def test_breach_confirms_then_alerts_once_and_goes_quiet(tmp_path, monkeypatch):
    capture = Capture()
    monkeypatch.setattr(ma, "ntfy_post", capture)
    monkeypatch.setattr(ma, "query", prom_ok(95.0))
    state = tmp_path / "state.json"

    assert ma.run(make_cfg([RULE]), state) == 0          # sweep 1: streak 1
    assert capture.calls == []
    entry = ma.load_state(state)["rules"]["CpuTempC"]
    assert entry["status"] == "ok" and entry["breaches"] == 1

    assert ma.run(make_cfg([RULE]), state) == 0          # sweep 2: alert
    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert "CpuTempC > 80: 95" in call["message"]
    assert call["title"].startswith("metric-alert: 1 metric")
    assert call["tags"] == ["rotating_light"] and call["topic"] == "services"
    assert ma.load_state(state)["rules"]["CpuTempC"]["status"] == "firing"

    assert ma.run(make_cfg([RULE]), state) == 0          # sweep 3: quiet
    assert len(capture.calls) == 1, "a standing breach must not re-alert"
    assert ma.load_state(state)["rules"]["CpuTempC"]["breaches"] == 3


def test_recovery_alerts_once_and_resets(tmp_path, monkeypatch):
    capture = Capture()
    monkeypatch.setattr(ma, "ntfy_post", capture)
    state = tmp_path / "state.json"

    monkeypatch.setattr(ma, "query", prom_ok(95.0))
    ma.run(make_cfg([RULE]), state)
    ma.run(make_cfg([RULE]), state)                      # firing
    monkeypatch.setattr(ma, "query", prom_ok(60.0))
    ma.run(make_cfg([RULE]), state)

    assert len(capture.calls) == 2
    assert capture.calls[1]["message"] == "CpuTempC back in range (60)"
    assert capture.calls[1]["tags"] == ["white_check_mark"]
    entry = ma.load_state(state)["rules"]["CpuTempC"]
    assert entry["status"] == "ok" and entry["breaches"] == 0
    ma.run(make_cfg([RULE]), state)                      # still fine: silence
    assert len(capture.calls) == 2


def test_query_error_holds_state_and_never_alerts(tmp_path, monkeypatch):
    capture = Capture()
    monkeypatch.setattr(ma, "ntfy_post", capture)
    monkeypatch.setattr(ma, "query", prom_error("connection refused"))
    state = tmp_path / "state.json"
    assert ma.run(make_cfg([RULE]), state, verbose=True) == 1   # all failed
    assert capture.calls == []
    entry = ma.load_state(state)["rules"]["CpuTempC"]
    assert entry["status"] == "error" and "refused" in entry["error"]


def test_one_dead_query_does_not_fail_or_alert_the_run(tmp_path, monkeypatch):
    calls = []

    def flaky(url, expr, timeout, _urlopen=None):
        if expr == "broken":
            return None, "prometheus: parse error"
        calls.append(expr)
        return json.loads(prom_body(10.0))["data"]["result"], None

    capture = Capture()
    monkeypatch.setattr(ma, "ntfy_post", capture)
    monkeypatch.setattr(ma, "query", flaky)
    rules = [RULE, {"name": "Broken", "op": ">", "threshold": 1.0,
                    "expr": "broken"}]
    state = tmp_path / "state.json"
    assert ma.run(make_cfg(rules), state) == 0
    assert calls == ["node_thermal_zone_temp"]
    assert capture.calls == []


def test_state_and_status_written_atomically_and_listed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ma, "query", prom_ok(95.0))
    monkeypatch.setattr(ma, "ntfy_post", Capture(ok=False))
    state = tmp_path / "sub" / "state.json"
    assert ma.run(make_cfg([RULE]), state, dry_run=True) == 0
    assert not state.exists(), "dry-run must not persist"

    ma.run(make_cfg([RULE]), state)
    ma.run(make_cfg([RULE]), state)                      # fires (undelivered)
    assert state.exists() and (tmp_path / "sub" / "status.json").exists()
    doc = json.loads((tmp_path / "sub" / "status.json").read_text())
    assert doc["rules"]["CpuTempC"]["status"] == "firing"
    assert doc["rules"]["CpuTempC"]["threshold"] == 80.0
    assert "generated" in doc
    assert "NOT delivered" in capsys.readouterr().err

    assert ma.print_state(state) == 0
    out = capsys.readouterr().out
    assert "CpuTempC: firing 95 > 80" in out
    assert ma.print_state(tmp_path / "absent.json") == 0


def test_no_rules_configured_is_a_broken_run(tmp_path):
    assert ma.run(make_cfg([]), tmp_path / "state.json") == 1


def test_ntfy_post_routes_through_shared_lib(tmp_path, monkeypatch):
    """The mute gap closure: alerts go through ntfy_lib, so the global
    mute covers them like every other publisher."""
    seen = {}

    def fake_publish(url, headers, payload, timeout=None, mute_file=None,
                     _urlopen=None):
        seen.update({"url": url, "headers": headers, "payload": payload,
                     "timeout": timeout})
        return True

    monkeypatch.setattr(ma.ntfy_lib, "publish", fake_publish)
    cfg = make_cfg([RULE])
    assert ma.ntfy_post(cfg, "t", "m", ["rotating_light"], 5) is True
    assert seen["url"] == "http://ntfy.invalid:6839"
    assert seen["headers"]["Authorization"] == "Bearer pt"
    assert seen["payload"]["topic"] == "services"
    assert seen["timeout"] == 5
    cfg["ntfy_token"] = ""
    assert ma.ntfy_post(cfg, "t", "m", [], 5) is False
