"""Tests for ntfy_lib — the shared publish layer (mute + timeouts).

The two guarantees every ntfy publisher on this box inherits:
 1. a global mute file that suppresses publishes without touching the
    network (storm kill switch), reported as success so jobs keep
    flowing;
 2. a finite timeout on every request, whatever the caller passes.
"""
import json
import urllib.error
from pathlib import Path

import ntfy_lib
from ntfy_lib import DEFAULT_TIMEOUT


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    """Captures requests; returns canned responses or raises."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append((req, timeout))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return FakeResponse(r)


HEADERS = {"Authorization": "Bearer tk_test"}
PAYLOAD = {"topic": "radar", "title": "t", "message": "m"}


# ------------------------------------------------------------- publish

def test_publish_posts_json_with_auth_and_content_type():
    fake = FakeUrlopen([200])
    ok = ntfy_lib.publish("http://n:6839", HEADERS, PAYLOAD,
                          _urlopen=fake)
    assert ok is True
    req, timeout = fake.calls[0]
    assert req.full_url == "http://n:6839"
    assert req.get_header("Authorization") == "Bearer tk_test"
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data.decode()) == PAYLOAD


def test_publish_returns_true_on_2xx_false_otherwise():
    fake = FakeUrlopen([200, 204, 500])
    assert ntfy_lib.publish("http://n", HEADERS, PAYLOAD, _urlopen=fake) is True
    assert ntfy_lib.publish("http://n", HEADERS, PAYLOAD, _urlopen=fake) is True
    assert ntfy_lib.publish("http://n", HEADERS, PAYLOAD, _urlopen=fake) is False


# ----------------------------------------------------------- timeouts

def test_publish_never_runs_without_finite_timeout():
    """The hang path: timeout=None/0/negative must never reach urlopen."""
    fake = FakeUrlopen([200, 200, 200])
    ntfy_lib.publish("http://n", HEADERS, PAYLOAD, timeout=None, _urlopen=fake)
    ntfy_lib.publish("http://n", HEADERS, PAYLOAD, timeout=0, _urlopen=fake)
    ntfy_lib.publish("http://n", HEADERS, PAYLOAD, timeout=-5, _urlopen=fake)
    assert all(t == DEFAULT_TIMEOUT for _, t in fake.calls)


def test_publish_passes_caller_timeout_through_when_sane():
    fake = FakeUrlopen([200])
    ntfy_lib.publish("http://n", HEADERS, PAYLOAD, timeout=7, _urlopen=fake)
    assert fake.calls[0][1] == 7


# --------------------------------------------------------------- mute

def test_publish_muted_suppresses_without_network(tmp_path, capsys):
    """The storm path: mute file present -> no request, reported True."""
    mute = tmp_path / "mute"
    mute.write_text("storm drill 2026-08-29\nsecond line ignored\n")

    def boom(req, timeout=None):
        raise AssertionError("network must not be touched while muted")

    ok = ntfy_lib.publish("http://n", HEADERS, PAYLOAD,
                          mute_file=mute, _urlopen=boom)
    assert ok is True  # suppressed counts as delivered, not failed
    err = capsys.readouterr().err
    assert "muted" in err and "storm drill 2026-08-29" in err


def test_publish_unmuted_when_file_absent(tmp_path):
    mute = tmp_path / "mute"          # never created
    fake = FakeUrlopen([200])
    ok = ntfy_lib.publish("http://n", HEADERS, PAYLOAD,
                          mute_file=mute, _urlopen=fake)
    assert ok is True and len(fake.calls) == 1


def test_publish_mute_fails_open_on_unreadable_path(tmp_path):
    """An unreadable mute path must not silence notifications."""
    fake = FakeUrlopen([200])
    ok = ntfy_lib.publish("http://n", HEADERS, PAYLOAD,
                          mute_file=tmp_path,  # a directory: read() fails
                          _urlopen=fake)
    assert ok is True and len(fake.calls) == 1


def test_mute_default_path_is_patchable(monkeypatch, tmp_path):
    """Consumers must be testable by patching ntfy_lib.DEFAULT_MUTE_FILE."""
    mute = tmp_path / "mute"
    mute.write_text("storm\n")
    monkeypatch.setattr(ntfy_lib, "DEFAULT_MUTE_FILE", str(mute))
    fake = FakeUrlopen([200])
    # no explicit mute_file: the patched default must apply at call time
    ok = ntfy_lib.publish("http://n", HEADERS, PAYLOAD, _urlopen=fake)
    assert ok is True and fake.calls == []
    assert ntfy_lib.muted() == (True, "storm")


def test_muted_reads_reason_from_first_line(tmp_path):
    mute = tmp_path / "mute"
    mute.write_text("reason here\n")
    assert ntfy_lib.muted(mute) == (True, "reason here")
    assert ntfy_lib.muted(tmp_path / "absent") == (False, "")


# ------------------------------------------------------------- errors

def test_publish_http_error_reported_not_raised(capsys):
    fake = FakeUrlopen([urllib.error.HTTPError(
        "http://n", 403, "forbidden", {}, None)])
    assert ntfy_lib.publish("http://n", HEADERS, PAYLOAD, _urlopen=fake) is False
    assert "403" in capsys.readouterr().err


def test_publish_url_error_reported_not_raised(capsys):
    fake = FakeUrlopen([urllib.error.URLError("refused")])
    assert ntfy_lib.publish("http://n", HEADERS, PAYLOAD, _urlopen=fake) is False
    assert "refused" in capsys.readouterr().err


def test_publish_requires_url():
    assert ntfy_lib.publish("", HEADERS, PAYLOAD, _urlopen=FakeUrlopen([])) is False
