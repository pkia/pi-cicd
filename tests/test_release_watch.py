"""Tests for release-watch — the upstream release watcher."""
import importlib.util
import importlib.machinery
import io
import json
import urllib.error
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "release-watch"

spec = importlib.util.spec_from_loader(
    "release_watch",
    importlib.machinery.SourceFileLoader("release_watch", str(SCRIPT)),
)
rw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rw)


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


def make_cfg(github=("jvde-github/AIS-catcher",), pages=(),
             ntfy="http://ntfy.example:6839", streak=3):
    return {
        "github": list(github),
        "pages": [("AISstatus", "https://status.example/feed")
                  if p == "AISstatus" else p for p in pages],
        "ntfy_url": ntfy or "",
        "ntfy_token": "tok" if ntfy else "",
        "topic": "releases",
        "timeout": 5,
        "streak_alert": streak,
    }


class Router:
    """URL-routing fake for urllib.request.urlopen."""

    def __init__(self, releases=None, pages=None, ntfy_code=200,
                 fail_urls=()):
        self.releases = releases or {}   # slug -> json body (or Exception)
        self.pages = pages or {}         # url -> body (or Exception)
        self.ntfy_code = ntfy_code
        self.fail_urls = fail_urls
        self.published = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        for prefix in self.fail_urls:
            if url.startswith(prefix):
                raise http_error(503)
        if url.startswith("https://api.github.com/repos/"):
            parts = url.split("/")
            slug = f"{parts[4]}/{parts[5]}"
            body = self.releases[slug]
            if isinstance(body, Exception):
                raise body
            return FakeResp(body)
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


def release_body(tag="v1.2.3", published="2026-08-01T00:00:00Z"):
    return json.dumps({
        "tag_name": tag,
        "html_url": f"https://github.com/x/releases/tag/{tag}",
        "published_at": published,
    })


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "state" / "state.json"


# ---------------------------------------------------------------- config

def test_config_defaults():
    cfg = rw.load_config("/nonexistent")
    assert cfg["github"] == [] and cfg["pages"] == []
    assert cfg["topic"] == "releases"
    assert cfg["timeout"] == rw.DEFAULT_TIMEOUT
    assert cfg["streak_alert"] == rw.DEFAULT_STREAK


def test_config_parses_sources(tmp_path):
    f = tmp_path / "rw.conf"
    f.write_text(
        "WATCH_GITHUB=binwiederhier/ntfy, jvde-github/AIS-catcher\n"
        "WATCH_URLS=AISstatus=https://status.example/feed\n"
        "NTFY_TOPIC=releases\n")
    cfg = rw.load_config(f)
    assert cfg["github"] == ["binwiederhier/ntfy",
                             "jvde-github/AIS-catcher"]
    assert cfg["pages"] == [("AISstatus", "https://status.example/feed")]


def test_config_skips_bad_entries(tmp_path, capsys):
    f = tmp_path / "rw.conf"
    f.write_text(
        "WATCH_GITHUB=not-a-slug,AdguardTeam/AdGuardHome\n"
        "WATCH_URLS=no-equals-sign,ok=https://x.example\n"
        "ERROR_STREAK_ALERT=bogus\n")
    cfg = rw.load_config(f)
    assert cfg["github"] == ["AdguardTeam/AdGuardHome"]
    assert cfg["pages"] == [("ok", "https://x.example")]
    assert cfg["streak_alert"] == rw.DEFAULT_STREAK  # bad int -> default
    err = capsys.readouterr().err
    assert "not-a-slug" in err
    assert "no-equals-sign" in err


# -------------------------------------------------------------- fetchers

def test_fetch_github_latest_parses_release(monkeypatch):
    router = Router(releases={"o/r": release_body("v9.9.9")})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    version, url, released = rw.fetch_github_latest("o/r", 5)
    assert version == "v9.9.9"
    assert url.endswith("/tag/v9.9.9")
    assert released == "2026-08-01"


def test_fetch_github_latest_http_error(monkeypatch):
    router = Router(releases={"o/r": http_error(404)})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    version, err, released = rw.fetch_github_latest("o/r", 5)
    assert version is None and released is None
    assert err == "HTTP 404"


def test_fetch_github_latest_bad_json(monkeypatch):
    router = Router(releases={"o/r": "<html>not json</html>"})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    version, err, _ = rw.fetch_github_latest("o/r", 5)
    assert version is None
    assert "unparseable" in err


def test_fetch_page_sha(monkeypatch):
    router = Router(pages={"https://x.example": "hello"})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    import hashlib
    sha, url, err = rw.fetch_page_sha("https://x.example", 5)
    assert err is None and url == "https://x.example"
    assert sha == hashlib.sha256(b"hello").hexdigest()


def test_ntfy_post_ok(monkeypatch):
    router = Router()
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    cfg = make_cfg()
    assert rw.ntfy_post(cfg, "t", "m", ["arrow_up"], 5) is True
    assert len(router.published) == 1
    msg = router.published[0]
    assert msg["topic"] == "releases" and msg["title"] == "t"


def test_ntfy_post_http_fail(monkeypatch, capsys):
    router = Router(ntfy_code=500)
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    assert rw.ntfy_post(make_cfg(), "t", "m", [], 5) is False
    assert "HTTP 500" in capsys.readouterr().err


def test_ntfy_post_no_config(capsys):
    assert rw.ntfy_post(make_cfg(ntfy=None), "t", "m", [], 5) is False
    assert "not configured" in capsys.readouterr().err


# ----------------------------------------------------------------- state

def test_state_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    assert rw.load_state(p) == {"sources": {}}
    assert rw.save_state(p, {"sources": {"github:o/r": {"version": "v1"}}})
    assert rw.load_state(p)["sources"]["github:o/r"]["version"] == "v1"


def test_state_corrupt_recovers(tmp_path, capsys):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert rw.load_state(p) == {"sources": {}}
    assert "corrupt" in capsys.readouterr().err


def test_state_bad_shape_recovers(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"sources": 42}))
    assert rw.load_state(p) == {"sources": {}}


def test_state_save_failure(tmp_path, capsys):
    # a directory where the file should be -> OSError on write
    d = tmp_path / "dir"
    d.mkdir()
    assert rw.save_state(d, {"sources": {}}) is False
    assert "cannot write state" in capsys.readouterr().err


# ------------------------------------------------------------------- run

def test_run_no_sources():
    assert rw.run(make_cfg(github=(), pages=()), "/tmp/whatever.json") == 1


def test_run_first_observation_is_baseline(monkeypatch, state_file):
    router = Router(releases={"o/r": release_body("v1.0.0")})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    rc = rw.run(make_cfg(github=("o/r",)), state_file)
    assert rc == 0
    state = rw.load_state(state_file)
    assert state["sources"]["github:o/r"]["version"] == "v1.0.0"
    # baseline IS delivered (visibility), tagged as such
    assert len(router.published) == 1
    assert "baselined at v1.0.0" in router.published[0]["message"]


def test_run_silent_when_unchanged(monkeypatch, state_file, capsys):
    router = Router(releases={"o/r": release_body("v1.0.0")})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    rw.run(make_cfg(github=("o/r",)), state_file)
    router.published.clear()
    rc = rw.run(make_cfg(github=("o/r",)), state_file)
    assert rc == 0
    assert router.published == []
    assert "unchanged" in capsys.readouterr().out


def test_run_detects_new_release(monkeypatch, state_file):
    router = Router(releases={"o/r": release_body("v1.0.0")})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    cfg = make_cfg(github=("o/r",))
    rw.run(cfg, state_file)
    router.published.clear()
    router.releases["o/r"] = release_body("v1.1.0")
    rc = rw.run(cfg, state_file)
    assert rc == 0
    assert len(router.published) == 1
    assert "o/r v1.0.0 → v1.1.0" in router.published[0]["message"]
    assert rw.load_state(state_file)["sources"]["github:o/r"][
        "version"] == "v1.1.0"


def test_run_detects_page_change(monkeypatch, state_file):
    url = "https://status.example/feed"
    router = Router(pages={url: "page-a"})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    cfg = make_cfg(github=(), pages=(("AISstatus", url),))
    rw.run(cfg, state_file)
    router.published.clear()
    router.pages[url] = "page-b"
    rc = rw.run(cfg, state_file)
    assert rc == 0
    assert len(router.published) == 1
    assert "AISstatus page changed" in router.published[0]["message"]


def test_run_error_streak_escalates_then_recovers(
        monkeypatch, state_file):
    url = "https://status.example/feed"
    router = Router(pages={url: "ok"}, fail_urls=(url,))
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    cfg = make_cfg(github=(), pages=(("AISstatus", url),), streak=3)
    # two failures: counted, no digest
    assert rw.run(cfg, state_file) == 1  # only source failed
    assert rw.run(cfg, state_file) == 1
    entry = rw.load_state(state_file)["sources"]["page:AISstatus"]
    assert entry["consecutive_errors"] == 2
    # third failure hits the streak threshold -> still rc 1 (sole source),
    # but state carries the escalation
    assert rw.run(cfg, state_file) == 1
    entry = rw.load_state(state_file)["sources"]["page:AISstatus"]
    assert entry["consecutive_errors"] == 3
    assert "failing 3 runs" in entry["last_error"] or \
        entry["consecutive_errors"] == 3
    # recovery: source works again -> counter resets
    router.fail_urls = ()
    rc = rw.run(cfg, state_file)
    assert rc == 0
    entry = rw.load_state(state_file)["sources"]["page:AISstatus"]
    assert entry["consecutive_errors"] == 0
    assert entry["sha256"]


def test_run_streak_digest_published_alongside_change(
        monkeypatch, state_file):
    router = Router(
        releases={"o/r": release_body("v1.0.0")},
        fail_urls=("https://status.example",))
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    cfg = make_cfg(github=("o/r",), pages=(("AISstatus",
                                             "https://status.example/feed"),))
    rw.run(cfg, state_file)          # baseline + error 1
    rw.run(cfg, state_file)          # error 2
    router.published.clear()
    router.releases["o/r"] = release_body("v2.0.0")
    rc = rw.run(cfg, state_file)     # change + error 3 = streak, one digest
    assert rc == 0
    assert len(router.published) == 1
    body = router.published[0]["message"]
    assert "o/r v1.0.0 → v2.0.0" in body
    assert "page:AISstatus failing 3 runs" in body


def test_run_all_sources_failed(monkeypatch, state_file, capsys):
    router = Router(releases={"o/r": http_error(500)})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    rc = rw.run(make_cfg(github=("o/r",)), state_file)
    assert rc == 1
    assert "every source failed" in capsys.readouterr().err
    # error counters still persisted for the streak logic
    assert rw.load_state(state_file)["sources"]["github:o/r"][
        "consecutive_errors"] == 1


def test_run_publish_failure_is_not_run_failure(
        monkeypatch, state_file, capsys):
    router = Router(releases={"o/r": release_body("v1.0.0")},
                    ntfy_code=507)
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    cfg = make_cfg(github=("o/r",))
    assert rw.run(cfg, state_file) == 0        # baseline, publish fails
    router.releases["o/r"] = release_body("v1.1.0")
    assert rw.run(cfg, state_file) == 0        # change, publish fails again
    err = capsys.readouterr().err
    assert "NOT delivered" in err
    # state moved on regardless — documented behaviour
    assert rw.load_state(state_file)["sources"]["github:o/r"][
        "version"] == "v1.1.0"


def test_run_dry_run_touches_nothing(monkeypatch, state_file, capsys):
    router = Router(releases={"o/r": release_body("v1.0.0")})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    rc = rw.run(make_cfg(github=("o/r",)), state_file, dry_run=True)
    assert rc == 0
    assert router.published == []
    assert "[dry-run]" in capsys.readouterr().out
    assert not state_file.exists()


def test_run_state_unwritable_fails(monkeypatch, tmp_path, capsys):
    router = Router(releases={"o/r": release_body("v1.0.0")})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    blocker = tmp_path / "state.json"
    blocker.mkdir()  # a directory: save_state will OSError
    rc = rw.run(make_cfg(github=("o/r",)), blocker)
    assert rc == 1


# ---------------------------------------------------------------- digest

def test_build_digest_singular_plural():
    title, msg = rw.build_digest(["one thing"], [])
    assert title.endswith("1 update") and "1 upstream change:" in msg
    title, msg = rw.build_digest(["a", "b"], [])
    assert title.endswith("2 updates") and "2 upstream changes:" in msg
    title, msg = rw.build_digest([], ["src failing 3 runs"])
    assert "1 watch source failing:" in msg
    assert "- src failing 3 runs" in msg


def test_print_state_empty(tmp_path, capsys):
    assert rw.print_state(tmp_path / "none.json") == 0
    assert "nothing watched yet" in capsys.readouterr().out


def test_print_state_lists_sources(state_file, capsys):
    rw.save_state(state_file, {"sources": {
        "github:o/r": {"version": "v1.2.3"},
        "page:P": {"sha256": "abcdef1234567890", "consecutive_errors": 2},
    }})
    assert rw.print_state(state_file) == 0
    out = capsys.readouterr().out
    assert "github:o/r: v1.2.3" in out
    assert "page:P: sha abcdef123456" in out
    assert "[errors: 2]" in out


# ------------------------------------------------------------------ main

def test_main_list_subcommand(tmp_path, capsys, monkeypatch):
    router = Router(releases={"o/r": release_body("v1.0.0")})
    monkeypatch.setattr(rw.urllib.request, "urlopen", router)
    state = tmp_path / "s.json"
    conf = tmp_path / "rw.conf"
    conf.write_text("WATCH_GITHUB=o/r\n")
    assert rw.main(["--config", str(conf), "--state", str(state)]) == 0
    assert rw.main(["--state", str(state), "--list"]) == 0
    out = capsys.readouterr().out
    assert "github:o/r: v1.0.0" in out


def test_main_state_path_expands_home(tmp_path):
    assert rw.main(["--state", "~/nowhere-x/state.json", "--list"]) == 0


# ---------------------------------------- shared ntfy_lib delegation

def test_ntfy_post_suppressed_when_muted(monkeypatch, tmp_path, capsys):
    mute = tmp_path / "mute"
    mute.write_text("storm drill\n")
    monkeypatch.setattr(rw.ntfy_lib, "DEFAULT_MUTE_FILE", str(mute))

    def boom(req, timeout=None):
        raise AssertionError("network must not be touched while muted")

    monkeypatch.setattr(rw.urllib.request, "urlopen", boom)
    assert rw.ntfy_post(make_cfg(), "t", "m", [], 5) is True
    assert "muted" in capsys.readouterr().err
