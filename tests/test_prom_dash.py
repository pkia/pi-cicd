"""prom-dash must render the pinned panels from a Prometheus range API.

Hermetic: a fake Prometheus (stdlib http.server on an ephemeral port)
answers /api/v1/query_range, so the tests never touch the live box.
Binds the panels the devlogs quote (CPU temperature, load, active/failed
systemd units) to the code: the fake server must see exactly the queries
in prom-dash's PANELS list, and the rendered HTML must carry each
panel's data.
"""
import http.server
import importlib.machinery
import importlib.util
import json
import threading
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_loader = importlib.machinery.SourceFileLoader("prom_dash", str(REPO / "prom-dash"))
_spec = importlib.util.spec_from_loader("prom_dash", _loader)
prom_dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prom_dash)

STEP = 900
START = 1_700_000_000


def _matrix(values):
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {}, "values": [[START + i * STEP, f"{v:.6f}"]
                                          for i, v in enumerate(values)]}
            ],
        },
    }


def _canned(query):
    """Distinct, assertable series per pinned query."""
    if "temp" in query:
        vals = [40 + i * (20 / 95) for i in range(96)]  # ramps to 60.0
    elif "node_load1" in query:
        vals = [0.5] * 96
    elif 'state="active"' in query:
        vals = [980 + i * (5 / 95) for i in range(96)]  # ramps to 985.0
    else:  # failed units — the honest headline: zero
        vals = [0] * 96
    return _matrix(vals)


class FakeProm(http.server.BaseHTTPRequestHandler):
    hits = []
    empty = False

    def do_GET(self):
        type(self).hits.append(self.path)
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query).get("query", [""])[0]
        body = json.dumps({"status": "success", "data": {"resultType": "matrix",
                                                         "result": []}}
                          if type(self).empty else _canned(query)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def _serve(empty=False):
    FakeProm.hits = []
    FakeProm.empty = empty
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeProm)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _run(url, out, **kw):
    argv = ["--url", url, "--out", str(out), "--hours", "24"]
    return prom_dash.main(argv)


def test_dashboard_renders_every_pinned_panel_with_data(tmp_path):
    srv, url = _serve()
    out = tmp_path / "dashboard.html"
    try:
        rc = _run(url, out)
    finally:
        srv.shutdown()
    assert rc == 0
    page = out.read_text()
    for panel in prom_dash.PANELS:  # titles and data for every pinned graph
        assert panel["name"] in page
        assert panel["query"] in page  # the query is printed on the card
    # last values from the canned series appear in the HTML
    assert "60.0" in page and "0.5" in page
    assert "985.0" in page and "0.0" in page
    assert page.count("<polyline") == len(prom_dash.PANELS)


def test_fake_prometheus_sees_exactly_the_pinned_queries(tmp_path):
    srv, url = _serve()
    out = tmp_path / "dashboard.html"
    try:
        _run(url, out)
    finally:
        srv.shutdown()
    seen = {
        urllib.parse.parse_qs(urllib.parse.urlparse(h).query).get("query", [""])[0]
        for h in FakeProm.hits
    }
    assert seen == {p["query"] for p in prom_dash.PANELS}
    assert any("step=900" in h for h in FakeProm.hits)


def test_empty_result_renders_no_data_not_crash(tmp_path):
    srv, url = _serve(empty=True)
    out = tmp_path / "dashboard.html"
    try:
        rc = _run(url, out)
    finally:
        srv.shutdown()
    assert rc == 0
    page = out.read_text()
    assert page.count("no data in range") == len(prom_dash.PANELS)


def test_unreachable_prometheus_exits_1_and_leaves_dashboard_untouched(tmp_path):
    out = tmp_path / "dashboard.html"
    out.write_text("previous good dashboard")
    rc = prom_dash.main(["--url", "http://127.0.0.1:1", "--out", str(out)])
    assert rc == 1
    assert out.read_text() == "previous good dashboard"  # never overwritten
