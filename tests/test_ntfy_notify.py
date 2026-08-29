"""Tests for ntfy-notify — the ntfy backbone publisher helper."""
import importlib.util
import importlib.machinery
import json
import urllib.error
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "ntfy-notify"

spec = importlib.util.spec_from_loader(
    "ntfy_notify",
    importlib.machinery.SourceFileLoader("ntfy_notify", str(SCRIPT)),
)
nn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nn)


# ------------------------------------------------------------- config

def test_load_config_reads_env_file(tmp_path):
    conf = tmp_path / "nn.conf"
    conf.write_text(
        "# comment\n"
        "NTFY_URL=http://pi.example.ts.net:6839/\n"
        "NTFY_TOKEN=tk_test\n"
        "NTFY_TOPIC=radar\n"
    )
    cfg = nn.load_config(conf)
    assert cfg == {"url": "http://pi.example.ts.net:6839",
                   "token": "tk_test", "topic": "radar"}


def test_load_config_missing_file_gives_empty(tmp_path):
    cfg = nn.load_config(tmp_path / "nonexistent.conf")
    assert cfg == {"url": "", "token": "", "topic": ""}


# ------------------------------------------------------------ publish

class FakeResponse:
    def __init__(self, status):
        self.status = status
        self._body = b"{}"

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    """Captures the request; returns a canned response or raises."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return FakeResponse(r)


def test_notify_posts_json_payload_with_auth(monkeypatch):
    fake = FakeUrlopen([200])
    monkeypatch.setattr(nn.urllib.request, "urlopen", fake)
    cfg = {"url": "http://ntfy.local:6839", "token": "tk_test", "topic": "x"}
    ok = nn.notify(cfg, "radar", "shipped", "idea X done", ["rocket"], 4)
    assert ok is True
    req = fake.calls[0]
    assert req.full_url == "http://ntfy.local:6839"
    assert req.get_header("Authorization") == "Bearer tk_test"
    assert req.get_header("Content-type") == "application/json"
    payload = json.loads(req.data.decode())
    assert payload == {"topic": "radar", "title": "shipped",
                       "message": "idea X done", "tags": ["rocket"],
                       "priority": 4}


def test_notify_requires_url_token_and_topic():
    cfg = {"url": "", "token": "", "topic": ""}
    assert nn.notify(cfg, "radar", "", "m", [], None) is False
    assert nn.notify({"url": "http://x", "token": "", "topic": ""}, "", "", "m",
                     [], None) is False
    assert nn.notify({"url": "http://x", "token": "tk", "topic": ""}, "", "",
                     "m", [], None) is False


def test_notify_http_error_is_reported_not_raised(monkeypatch, capsys):
    fake = FakeUrlopen([urllib.error.HTTPError(
        "http://x", 403, "forbidden", {}, None)])  # noqa: E501  (no fp on py3.11)
    monkeypatch.setattr(nn.urllib.request, "urlopen", fake)
    cfg = {"url": "http://x", "token": "tk", "topic": "radar"}
    ok = nn.notify(cfg, "radar", "", "m", [], None)
    assert ok is False
    assert "403" in capsys.readouterr().err


def test_notify_connection_error_is_reported_not_raised(monkeypatch, capsys):
    fake = FakeUrlopen([urllib.error.URLError("refused")])
    monkeypatch.setattr(nn.urllib.request, "urlopen", fake)
    cfg = {"url": "http://x", "token": "tk", "topic": "radar"}
    assert nn.notify(cfg, "radar", "", "m", [], None) is False
    assert "refused" in capsys.readouterr().err


def test_notify_dry_run_publishes_nothing(monkeypatch, capsys):
    def boom(req, timeout=None):
        raise AssertionError("network must not be touched in dry-run")
    monkeypatch.setattr(nn.urllib.request, "urlopen", boom)
    cfg = {"url": "http://x", "token": "tk", "topic": "radar"}
    assert nn.notify(cfg, "radar", "t", "m", [], None, dry_run=True) is True
    out = capsys.readouterr().out
    assert "would publish" in out and "radar" in out


# ---------------------------------------------------------------- cli

def test_cli_publishes_to_config_topic(monkeypatch, tmp_path, capsys):
    fake = FakeUrlopen([200])
    monkeypatch.setattr(nn.urllib.request, "urlopen", fake)
    conf = tmp_path / "nn.conf"
    conf.write_text("NTFY_URL=http://n:6839\nNTFY_TOKEN=tk\nNTFY_TOPIC=radar\n")
    rc = nn.main(["--config", str(conf), "radar shipped idea X"])
    assert rc == 0
    payload = json.loads(fake.calls[0].data.decode())
    assert payload["topic"] == "radar"
    assert payload["message"] == "radar shipped idea X"
    assert "✓ published" in capsys.readouterr().out


def test_cli_topic_flag_overrides_config(monkeypatch, tmp_path):
    fake = FakeUrlopen([200])
    monkeypatch.setattr(nn.urllib.request, "urlopen", fake)
    conf = tmp_path / "nn.conf"
    conf.write_text("NTFY_URL=http://n:6839\nNTFY_TOKEN=tk\nNTFY_TOPIC=radar\n")
    rc = nn.main(["--config", str(conf), "-t", "backups", "borg ran"])
    assert rc == 0
    payload = json.loads(fake.calls[0].data.decode())
    assert payload["topic"] == "backups"


def test_cli_priority_names_map_to_numbers(monkeypatch, tmp_path):
    fake = FakeUrlopen([200])
    monkeypatch.setattr(nn.urllib.request, "urlopen", fake)
    conf = tmp_path / "nn.conf"
    conf.write_text("NTFY_URL=http://n:6839\nNTFY_TOKEN=tk\nNTFY_TOPIC=radar\n")
    rc = nn.main(["--config", str(conf), "-p", "high", "m"])
    assert rc == 0
    payload = json.loads(fake.calls[0].data.decode())
    assert payload["priority"] == 4


def test_cli_fails_cleanly_when_unconfigured(monkeypatch, tmp_path, capsys):
    # no config file at all -> notify() refuses, main returns 1
    conf = tmp_path / "absent.conf"
    rc = nn.main(["--config", str(conf), "-t", "radar", "m"])
    assert rc == 1
    assert "required" in capsys.readouterr().err


def test_cli_dry_run(monkeypatch, tmp_path, capsys):
    conf = tmp_path / "nn.conf"
    conf.write_text("NTFY_URL=http://n:6839\nNTFY_TOKEN=tk\nNTFY_TOPIC=radar\n")
    rc = nn.main(["--config", str(conf), "--dry-run", "-T", "radar", "note"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would publish" in out
    assert "note" in out


# ---------------------------------------------------------- conventions

def test_default_config_path_points_at_etc():
    assert nn.DEFAULT_CONFIG == "/etc/ntfy-notify.conf"


# ------------------------------------------------- shared-library mute

def test_notify_suppressed_when_global_mute_file_present(
        monkeypatch, tmp_path, capsys):
    """Storm path: mute file present -> no network, reported delivered."""
    mute = tmp_path / "mute"
    mute.write_text("storm drill\n")
    monkeypatch.setattr(nn.ntfy_lib, "DEFAULT_MUTE_FILE", str(mute))

    def boom(req, timeout=None):
        raise AssertionError("network must not be touched while muted")

    monkeypatch.setattr(nn.urllib.request, "urlopen", boom)
    cfg = {"url": "http://x", "token": "tk", "topic": "radar"}
    assert nn.notify(cfg, "radar", "t", "m", [], None) is True
    assert "muted" in capsys.readouterr().err


def test_notify_timeout_always_finite(monkeypatch):
    """Hang path: a bogus timeout must be sanitised by ntfy_lib."""
    seen = {}

    def fake(req, timeout=None):
        seen["timeout"] = timeout
        return FakeResponse(200)

    monkeypatch.setattr(nn.urllib.request, "urlopen", fake)
    cfg = {"url": "http://x", "token": "tk", "topic": "radar"}
    assert nn.notify(cfg, "radar", "t", "m", [], None, timeout=None) is True
    assert seen["timeout"] == nn.ntfy_lib.DEFAULT_TIMEOUT


# ---------------------------------------------------------- mute CLI

def test_cli_reports_suppressed_not_published(monkeypatch, tmp_path, capsys):
    mute = tmp_path / "mute"
    mute.write_text("storm\n")
    monkeypatch.setattr(nn.ntfy_lib, "DEFAULT_MUTE_FILE", str(mute))
    cfg = tmp_path / "nn.conf"
    cfg.write_text("NTFY_URL=http://n:6839\nNTFY_TOKEN=tk\nNTFY_TOPIC=radar\n")

    def boom(req, timeout=None):
        raise AssertionError("network must not be touched while muted")

    monkeypatch.setattr(nn.urllib.request, "urlopen", boom)
    assert nn.main(["--config", str(cfg), "m"]) == 0
    out = capsys.readouterr()
    assert "suppressed" in out.out and "storm" in out.out
    assert "✓ published" not in out.out


def test_cli_mute_writes_reason_file(monkeypatch, tmp_path, capsys):
    mute = tmp_path / "mute"
    monkeypatch.setattr(nn, "MUTE_FILE", str(mute))
    rc = nn.main(["--mute", "ais-catcher storm", "unused"])
    assert rc == 0
    assert mute.read_text().strip() == "ais-catcher storm"
    assert "muted" in capsys.readouterr().out.lower()


def test_cli_mute_without_reason_still_mutes(monkeypatch, tmp_path):
    mute = tmp_path / "mute"
    monkeypatch.setattr(nn, "MUTE_FILE", str(mute))
    assert nn.main(["--mute"]) == 0
    assert mute.exists()


def test_cli_unmute_removes_file(monkeypatch, tmp_path, capsys):
    mute = tmp_path / "mute"
    mute.write_text("storm\n")
    monkeypatch.setattr(nn, "MUTE_FILE", str(mute))
    rc = nn.main(["--unmute", "unused"])
    assert rc == 0
    assert not mute.exists()


def test_cli_unmute_when_not_muted_is_fine(monkeypatch, tmp_path):
    monkeypatch.setattr(nn, "MUTE_FILE", str(tmp_path / "mute"))
    assert nn.main(["--unmute", "unused"]) == 0


def test_cli_mute_status_reports_state(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(nn, "MUTE_FILE", str(tmp_path / "mute"))
    assert nn.main(["--mute-status", "unused"]) == 1   # not muted
    Path(str(tmp_path / "mute")).write_text("storm\n")
    assert nn.main(["--mute-status", "unused"]) == 0   # muted
    assert "storm" in capsys.readouterr().out

