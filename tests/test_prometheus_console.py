"""prometheus/consoles/cluster.html — the cluster numbers as a console page.

The board's acceptance: the page renders real numbers from a running
scrape, and an empty store renders its empty state without erroring.
The structural tests are hermetic; the two drills run the real
prometheus binary (skipped where it is not installed, like the promtool
config test in test_prometheus_config.py) against a canned /metrics
stand-in for the coordinator, so a scrape-job change that breaks the
page reddens here before it blinds the kiosk.
"""

import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CONSOLE = REPO / "prometheus" / "consoles" / "cluster.html"
DROPIN = REPO / "prometheus" / "prometheus-consoles.conf"
BIND_LOCAL = REPO / "prometheus" / "prometheus-bind-local.conf"
INSTALL = REPO / "install.sh"
DOC = REPO / "docs" / "prometheus.md"
EXAMPLE_LIBS = Path("/usr/share/doc/prometheus/examples/console_libraries")

METRIC_NAMES = (
    "cluster_queue_depth",
    "cluster_units",
    "cluster_jobs",
    "cluster_workers_recent",
    "cluster_stale_after_seconds",
    "cluster_worker_last_seen_seconds",
    "cluster_worker_healthy",
    "cluster_worker_units_done_total",
)

# A faithful stand-in for the coordinator's GET /metrics exposition
# (cluster/metrics.py render_metrics): the real status vocabulary
# (units are queued/running/succeeded/dead, jobs
# queued/running/complete/failed in cluster/store.py) and the real
# worker hostnames. queued + running == queue depth, as /health counts
# it. raspberrypi is deliberately past the 180s stale threshold so the
# unhealthy branches of the page have something to render.
FAKE_METRICS = """# HELP cluster_queue_depth Units queued or claimed-and-running: unfinished work the cluster owes
# TYPE cluster_queue_depth gauge
cluster_queue_depth 7
# HELP cluster_units Units by status
# TYPE cluster_units gauge
cluster_units{status="dead"} 3
cluster_units{status="queued"} 5
cluster_units{status="running"} 2
cluster_units{status="succeeded"} 411
# HELP cluster_jobs Jobs by status
# TYPE cluster_jobs gauge
cluster_jobs{status="complete"} 33
cluster_jobs{status="failed"} 1
cluster_jobs{status="queued"} 1
cluster_jobs{status="running"} 1
# HELP cluster_workers_recent Workers whose last heartbeat is inside the stale threshold
# TYPE cluster_workers_recent gauge
cluster_workers_recent 1
# HELP cluster_stale_after_seconds The silence threshold /health and the worker_healthy verdicts use (max(60, lease*3))
# TYPE cluster_stale_after_seconds gauge
cluster_stale_after_seconds 180
# HELP cluster_worker_last_seen_seconds Seconds since the worker's last heartbeat
# TYPE cluster_worker_last_seen_seconds gauge
cluster_worker_last_seen_seconds{worker="raspberrypi"} 3700
cluster_worker_last_seen_seconds{worker="switchlab"} 12.5
# HELP cluster_worker_healthy 1 when the worker is inside the silence threshold, else 0 (mirrors /health)
# TYPE cluster_worker_healthy gauge
cluster_worker_healthy{worker="raspberrypi"} 0
cluster_worker_healthy{worker="switchlab"} 1
# HELP cluster_worker_units_done_total Units the worker has completed
# TYPE cluster_worker_units_done_total counter
cluster_worker_units_done_total{worker="raspberrypi"} 23
cluster_worker_units_done_total{worker="switchlab"} 411
"""


def _needs_prometheus():
    return shutil.which("prometheus") is None or not EXAMPLE_LIBS.is_dir()


# ----------------------------------------------------------------
# structural: the page, the drop-in, the wiring, the docs
# ----------------------------------------------------------------


def test_console_exists_and_names_the_cluster_metrics():
    text = CONSOLE.read_text()
    for metric in METRIC_NAMES:
        assert metric in text, f"console never renders {metric}"


def test_console_carries_an_explicit_empty_state():
    """The acceptance's empty-store half: a named empty state, not a 500."""
    text = CONSOLE.read_text()
    assert "No cluster data yet" in text
    # the no-data rows the tables themselves fall back to
    assert "no units scraped yet" in text
    assert "no workers have been scraped yet" in text
    # the empty-vector footgun the drills exist to catch
    assert "index . 0" not in text


def test_console_leaks_no_private_addresses():
    """The site's own house rule, applied to the new page: loopback
    targets are fine (the scrape is 127.0.0.1:8800 by design), nothing
    else is."""
    text = CONSOLE.read_text()
    for pattern in (
        r"\b(?:10|192\.168)\.\d+\.\d+\b",
        r"\b172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+\b",
        r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b",
        r"\.ts\.net",
    ):
        assert not re.search(pattern, text), f"private address matched {pattern}"


def test_console_is_styled_by_the_package_templates():
    text = CONSOLE.read_text()
    assert '{{ template "head" . }}' in text
    assert '{{ template "tail" }}' in text
    assert '{{ template "prom_query_drilldown"' in text


def test_dropin_repeats_every_loopback_flag_when_adding_console_flags():
    """consoles.conf replaces bind-local.conf's ExecStart wholesale (the
    later drop-in wins), so its line must carry every flag bind-local
    sets plus the two console paths — pinned here so the repeat cannot
    rot."""
    def execstart(path):
        for line in path.read_text().splitlines():
            if line.startswith("ExecStart=/usr/bin/prometheus"):
                return line.split()
        raise AssertionError(f"no ExecStart line in {path}")

    base = execstart(BIND_LOCAL)
    console = execstart(DROPIN)
    missing = [tok for tok in base if tok not in console]
    assert not missing, f"consoles.conf dropped flags from bind-local.conf: {missing}"
    for flag in ("--web.console.templates=", "--web.console.libraries="):
        assert any(tok.startswith(flag) for tok in console), f"missing {flag}"
    assert any("--web.console.templates=/etc/prometheus/consoles" in tok
               for tok in console)


def test_install_sh_wires_the_console():
    script = INSTALL.read_text()
    assert "/etc/prometheus/consoles/cluster.html" in script
    assert "console_libraries" in script and "prom.lib" in script
    assert "consoles.conf" in script
    # a reload cannot pick up new flags — the unit must be restarted
    assert "try-restart prometheus" in script


def test_docs_name_the_console_page():
    doc = DOC.read_text()
    assert "/consoles/cluster.html" in doc
    assert "consoles/cluster.html" in doc


# ----------------------------------------------------------------
# drills: the real prometheus binary over the real chain
# ----------------------------------------------------------------


class _MetricsHandler(BaseHTTPRequestHandler):
    body = FAKE_METRICS.encode()

    def do_GET(self):
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(self.body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # silence the drill
        pass


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return res.status, res.read().decode()


def _stage_prometheus(tmp_path, scrape_port):
    """Staged consoles/libs/yml + the prometheus subprocess. Returns
    (proc, web_port, log_path) — the caller terminates proc."""
    consoles = tmp_path / "consoles"
    libs = tmp_path / "console_libraries"
    consoles.mkdir()
    libs.mkdir()
    shutil.copy(CONSOLE, consoles / "cluster.html")
    for name in ("prom.lib", "menu.lib"):
        shutil.copy(EXAMPLE_LIBS / name, libs / name)
    yml = tmp_path / "prometheus.yml"
    yml.write_text(
        "global:\n"
        "  scrape_interval: 1s\n"
        "  evaluation_interval: 1s\n"
        "scrape_configs:\n"
        "  - job_name: cluster\n"
        "    metrics_path: /metrics\n"
        f"    static_configs:\n      - targets: ['127.0.0.1:{scrape_port}']\n"
    )
    web_port = _free_port()
    log_path = tmp_path / "prometheus.log"
    log = log_path.open("wb")
    proc = subprocess.Popen(
        [
            "prometheus",
            "--config.file", str(yml),
            "--storage.tsdb.path", str(tmp_path / "tsdb"),
            "--web.listen-address", f"127.0.0.1:{web_port}",
            "--web.console.templates", str(consoles),
            "--web.console.libraries", str(libs),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return proc, web_port, log_path


def _wait_ready(base, deadline_s=20):
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            status, _ = _get(f"{base}/-/ready", timeout=2)
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.4)
    raise AssertionError("prometheus never became ready")


def _wait_cluster_series(base, expr, deadline_s=25):
    """Poll the query API until expr has a result — proves the scrape
    landed before the page assertions run, so a render failure is never
    mistaken for a scrape failure."""
    end = time.time() + deadline_s
    last = ""
    while time.time() < end:
        try:
            _, body = _get(f"{base}/api/v1/query?query={expr}", timeout=3)
            data = json.loads(body)
            if data.get("data", {}).get("result"):
                return data["data"]["result"]
            last = body
        except (OSError, ValueError) as exc:
            last = str(exc)
        time.sleep(0.5)
    raise AssertionError(f"no series for {expr!r} after {deadline_s}s: {last}")


@pytest.mark.skipif(_needs_prometheus(), reason="prometheus/examples not installed")
def test_console_renders_real_numbers_from_a_running_scrape(tmp_path):
    """The acceptance, executed: a real Prometheus scrapes a canned
    /metrics and the console page carries the real numbers — queue
    depth, statuses, per-worker ages and the unhealthy verdict."""
    server = HTTPServer(("127.0.0.1", 0), _MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proc = None
    try:
        proc, web_port, log_path = _stage_prometheus(tmp_path, server.server_port)
        base = f"http://127.0.0.1:{web_port}"
        _wait_ready(base)
        result = _wait_cluster_series(base, "cluster_queue_depth")
        assert result[0]["value"][1] == "7"

        status, page = _get(f"{base}/consoles/cluster.html")
        assert status == 200
        # the real numbers, rendered: queue depth, a status row, the
        # worker table, the healthy and the unhealthy verdicts
        assert "<strong>7</strong>" in page
        assert 'status="succeeded"' not in page  # labels render as text
        assert "succeeded" in page and "411" in page
        assert "switchlab" in page
        assert "raspberrypi" in page
        assert "<strong>NO</strong>" in page  # raspberrypi, 3700s > 180s
        assert "Silent workers" in page
        assert "3700" in page
        assert ">yes<" in page  # switchlab, 12.5s < 180s
        assert "180.0" in page  # stale-after drilldown, printf.1f
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(_needs_prometheus(), reason="prometheus/examples not installed")
def test_empty_store_renders_its_empty_state_without_erroring(tmp_path):
    """The other half of the acceptance: with the cluster target
    refusing connections (the coordinator not up — today's live state
    until the owner installs the token), the page must still render:
    HTTP 200, the named empty state, no worker rows, no 5xx."""
    # reserve the port so the refusal is instant and deterministic
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    dead_port = blocker.getsockname()[1]
    proc = None
    try:
        proc, web_port, log_path = _stage_prometheus(tmp_path, dead_port)
        base = f"http://127.0.0.1:{web_port}"
        _wait_ready(base)
        # give one scrape interval for the (failing) scrape to register
        time.sleep(2.5)
        status, page = _get(f"{base}/consoles/cluster.html")
        assert status == 200
        assert "No cluster data yet" in page
        assert "no workers have been scraped yet" in page
        assert "no units scraped yet" in page
        assert "switchlab" not in page  # nothing was ever scraped
        assert "/etc/cluster-metrics.token" in page  # names the owner step
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        blocker.close()
