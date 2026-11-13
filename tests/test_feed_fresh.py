"""Tests for feed-fresh — the devlog's heartbeat tripwire.

Two layers, both executed:

* the measurement, driven through the real module (`check`/`newest_item_date`)
  against fixtures built here at a pinned instant, so no test depends on the
  wall clock or on the live site;
* the contract, driven through the real CLI in a subprocess: exit code and
  which stream carries the line — because that is what the timer and
  pi-doctor actually see.

The stale check is negative-controlled *by its own semantics*: every stale
fixture carries a fresh `lastBuildDate`, so a checker that read the feed's
build stamp instead of its newest item would pass them. `newest_item_date`
must prefer `pubDate`, and the stale tests fail if it does not.
"""
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
SCRIPT = REPO / "feed-fresh"

spec = importlib.util.spec_from_loader(
    "feed_fresh",
    importlib.machinery.SourceFileLoader("feed_fresh", str(SCRIPT)),
)
ff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ff)

NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)


def rss(items, build=None):
    """Build an RSS document. `items` are (pubDate, title) pairs."""
    build = build or (NOW - timedelta(hours=1))
    body = "\n".join(
        f"    <item>\n      <title>{title}</title>\n"
        f"      <link>https://pkia.github.io/blog/x.html</link>\n"
        f"      <pubDate>{pub}</pubDate>\n"
        "    </item>"
        for pub, title in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        "    <title>EV — Devlog</title>\n"
        f"    <lastBuildDate>{ff.email.utils.format_datetime(build)}</lastBuildDate>\n"
        f"{body}\n"
        "  </channel>\n</rss>\n"
    )


def rfc822(dt):
    return ff.email.utils.format_datetime(dt)


def run_cli(*args, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60, env=env,
    )


# ------------------------------------------------------------- measurement

def test_fresh_feed_passes():
    result = ff.check(rss([(rfc822(NOW - timedelta(hours=6)), "Cycle 75")]), NOW, 36)
    assert result["state"] == "fresh"
    assert result["age_hours"] == 6.0
    assert result["title"] == "Cycle 75"


def test_pinned_stale_feed_names_the_gap():
    xml = rss([(rfc822(NOW - timedelta(hours=61.5)), "Cycle 74")])
    result = ff.check(xml, NOW, 36)
    assert result["state"] == "stale"
    assert round(result["age_hours"], 1) == 61.5
    line = ff.format_line(result)
    assert "STALE" in line and "61.5 h" in line and "limit 36 h" in line


def test_last_build_date_is_not_the_measurement():
    """The feed rebuilt an hour ago around a 60-hour-old post is stale.

    This is the whole point of the check: a generator can refresh the feed
    without a new post, so `lastBuildDate` must not make it look alive.
    """
    xml = rss([(rfc822(NOW - timedelta(hours=60)), "Cycle 74")],
              build=NOW - timedelta(hours=1))
    assert "lastBuildDate" in xml
    assert ff.check(xml, NOW, 36)["state"] == "stale"


def test_newest_item_wins_over_list_order():
    xml = rss([
        (rfc822(NOW - timedelta(hours=60)), "old first"),
        (rfc822(NOW - timedelta(hours=3)), "newest second"),
    ])
    result = ff.check(xml, NOW, 36)
    assert result["state"] == "fresh"
    assert result["title"] == "newest second"


def test_offset_pubdate_is_read_as_aware_time():
    # The live feed stamps +0100; 08:00 +0100 is 07:00 UTC, i.e. 5 h before NOW.
    result = ff.check(rss([("Fri, 13 Nov 2026 08:00:00 +0100", "Cycle 75")]), NOW, 36)
    assert result["state"] == "fresh"
    assert result["age_hours"] == 5.0


def test_iso8601_and_atom_fallback():
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><title>Cycle 75</title><published>2026-11-13T06:00:00Z</published></entry>"
        "</feed>"
    )
    result = ff.check(xml, NOW, 36)
    assert result["state"] == "fresh"
    assert result["title"] == "Cycle 75"
    assert result["age_hours"] == 6.0


def test_future_dated_item_reports_zero_hours():
    result = ff.check(rss([(rfc822(NOW + timedelta(hours=5)), "tomorrow")]), NOW, 36)
    assert result["state"] == "fresh"
    assert result["age_hours"] == 0.0
    assert result["reason"] == "newest item is dated in the future"


def test_limit_is_strict():
    assert ff.check(rss([(rfc822(NOW - timedelta(hours=36)), "edge")]), NOW, 36)["state"] == "fresh"
    late = ff.check(rss([(rfc822(NOW - timedelta(hours=36, minutes=6)), "late")]), NOW, 36)
    assert late["state"] == "stale"


def test_unmeasurable_is_never_fresh():
    for xml in ("not xml at all", "<rss><channel></channel></rss>", ""):
        result = ff.check(xml, NOW, 36)
        assert result["state"] == "error", xml
        assert result["reason"]
        assert ff.format_line(result).startswith("feed-fresh: UNMEASURABLE — ")


# ------------------------------------------------------------------ CLI

def test_cli_fresh_exit_zero_and_ok_on_stdout(tmp_path):
    feed = tmp_path / "feed.xml"
    feed.write_text(rss([(rfc822(NOW - timedelta(hours=2)), "Cycle 75")]))
    proc = run_cli("--file", str(feed), "--now", NOW.isoformat())
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout and "2.0 h old" in proc.stdout
    assert proc.stderr == ""


def test_cli_stale_exit_one_and_fault_on_stderr(tmp_path):
    feed = tmp_path / "feed.xml"
    feed.write_text(rss([(rfc822(NOW - timedelta(hours=61.5)), "Cycle 74")]))
    proc = run_cli("--file", str(feed), "--now", NOW.isoformat())
    assert proc.returncode == 1
    assert "STALE" in proc.stderr and "61.5 h" in proc.stderr
    assert "64" not in proc.stderr  # no rounded-up arithmetic sneaking in
    assert proc.stdout == ""


def test_cli_unmeasurable_exit_two_for_bad_feed_missing_file_and_dead_url(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<rss><channel/></rss>")
    for args in (
        ("--file", str(bad), "--now", NOW.isoformat()),
        ("--file", str(tmp_path / "absent.xml"), "--now", NOW.isoformat()),
        # refused locally, so no network in CI and no third-party flake
        ("--url", "http://127.0.0.1:1/feed.xml", "--now", NOW.isoformat()),
    ):
        proc = run_cli(*args)
        assert proc.returncode == 2, (args, proc.stdout, proc.stderr)
        assert "UNMEASURABLE" in proc.stderr


def test_cli_fetches_over_http(tmp_path):
    feed = tmp_path / "feed.xml"
    feed.write_text(rss([(rfc822(NOW - timedelta(hours=4)), "Cycle 75")]))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmp_path), **kw)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/feed.xml"
        proc = run_cli("--url", url, "--now", NOW.isoformat())
    finally:
        server.shutdown()
        server.server_close()
    assert proc.returncode == 0, proc.stderr
    assert "4.0 h old" in proc.stdout


def test_cli_json_shape(tmp_path):
    feed = tmp_path / "feed.xml"
    feed.write_text(rss([(rfc822(NOW - timedelta(hours=61.5)), "Cycle 74")]))
    proc = run_cli("--file", str(feed), "--now", NOW.isoformat(), "--json")
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["state"] == "stale"
    assert payload["age_hours"] == 61.5
    assert payload["max_hours"] == 36.0
    assert payload["newest"] == (NOW - timedelta(hours=61.5)).isoformat()
    assert payload["title"] == "Cycle 74"


def test_cli_json_stdout_stays_parseable_on_a_fresh_feed(tmp_path):
    """--json must not also print the human line on stdout (10-12 lesson)."""
    feed = tmp_path / "feed.xml"
    feed.write_text(rss([(rfc822(NOW - timedelta(hours=2)), "Cycle 75")]))
    proc = run_cli("--file", str(feed), "--now", NOW.isoformat(), "--json")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["state"] == "fresh"
    assert proc.stderr == ""


# ------------------------------------------------------------- stateless

def test_source_writes_nothing():
    """The acceptance says it keeps no state beyond its own log line."""
    src = SCRIPT.read_text(encoding="utf-8")
    for banned in ("write_text", "write_bytes", "mkdir", "makedirs", "touch(",
                   "unlink(", "remove(", "rename(", "shutil"):
        assert banned not in src, f"source carries {banned!r}"
    # no open() in a writing mode (the only open is the read of --file)
    assert not re.search(r"open\([^\n]*['\"][wax]", src), "source opens a file for writing"
    assert "tempfile" not in src


def test_a_run_leaves_the_tree_byte_identical(tmp_path):
    feed = tmp_path / "feed.xml"
    feed.write_text(rss([(rfc822(NOW - timedelta(hours=1)), "Cycle 75")]))

    def snapshot():
        return {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir()}

    before = snapshot()
    assert run_cli("--file", str(feed), "--now", NOW.isoformat()).returncode == 0
    assert run_cli("--file", str(feed), "--now", NOW.isoformat(), "--json").returncode == 0
    assert snapshot() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["feed.xml"]


# ------------------------------------------------------------- wiring

def test_tool_is_executable_and_wired():
    assert os.access(SCRIPT, os.X_OK), "feed-fresh needs its exec bit (install.sh links it)"
    unit = (REPO / "systemd" / "feed-fresh.service").read_text()
    timer = (REPO / "systemd" / "feed-fresh.timer").read_text()
    assert "ExecStart=/home/ev/.local/bin/feed-fresh" in unit
    assert "--url https://pkia.github.io/feed.xml" in unit
    assert "--max-hours 36" in unit
    assert "User=ev" in unit
    assert "%h" not in "\n".join(
        ln for ln in unit.splitlines() if not ln.lstrip().startswith("#")
    )  # system units resolve %h from the manager, not User=
    assert "OnUnitActiveSec=1h" in timer
    install = (REPO / "install.sh").read_text()
    assert "feed-fresh" in install
