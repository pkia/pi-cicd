"""Tests for pi-backup — borg wrapper with restore drill.

Uses a real borg binary (Debian package) against temp repositories:
create/prune/list/check/restore and the byte-compare drill all run for
real. Only the ntfy publish layer is fake, via a patched ntfy_post.
CI installs borgbackup alongside pytest (see .github/workflows/ci.yml).
"""
import hashlib
import importlib.util
import importlib.machinery
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "pi-backup"

spec = importlib.util.spec_from_loader(
    "pi_backup",
    importlib.machinery.SourceFileLoader("pi_backup", str(SCRIPT)),
)
pb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pb)

try:
    subprocess.run(["borg", "--version"], capture_output=True, check=True)
    HAVE_BORG = True
except Exception:
    HAVE_BORG = False

pytestmark = pytest.mark.skipif(
    not HAVE_BORG, reason="borg binary not available")


# ------------------------------------------------------------- config

def write_config(tmp_path, repo, paths, extra=""):
    conf = tmp_path / "pi-backup.conf"
    conf.write_text(
        f"REPO={repo}\n"
        f"PASSPHRASE=test-passphrase-123\n"
        f"BACKUP_PATHS={' '.join(paths)}\n"
        f"{extra}\n")
    return conf


def make_sources(tmp_path):
    src = tmp_path / "src"
    (src / "etc" / "ntfy").mkdir(parents=True)
    (src / "etc" / "ntfy" / "server.yml").write_text("listen-http: 1.2.3.4:6839\n")
    (src / "etc" / "ntfy" / "user.db").write_bytes(b"fakedb" * 100)
    (src / "etc" / "pi-backup.conf").write_text("REPO=/nope\n")
    return src


def cfg_from(tmp_path, conf):
    cfg = pb.load_config(conf)
    assert pb.validate(cfg) == []
    return cfg


def test_load_config_defaults(tmp_path):
    conf = tmp_path / "c.conf"
    conf.write_text(
        "# comment\n"
        "REPO=/tmp/r\n"
        "PASSPHRASE=secret\n"
        "BACKUP_PATHS=/etc/ntfy /etc/loop-heartbeat.conf\n"
        "EXCLUDE=*.db-wal\n"
        "PRUNE_KEEP_DAILY=3\n"
        "DRILL_MAX_FILES=5\n")
    cfg = pb.load_config(conf)
    assert cfg["repo"] == "/tmp/r"
    assert cfg["passphrase"] == "secret"
    assert cfg["paths"] == ["/etc/ntfy", "/etc/loop-heartbeat.conf"]
    assert cfg["exclude"] == ["*.db-wal"]
    assert cfg["keep_daily"] == 3
    assert cfg["keep_weekly"] == 4          # default
    assert cfg["keep_monthly"] == 6         # default
    assert cfg["prefix"] == "pi"            # default
    assert cfg["drill_max_files"] == 5
    assert cfg["ntfy_topic"] == "backups"   # default


def test_validate_reports_all_problems():
    cfg = pb.load_config("/nonexistent/pi-backup.conf")
    problems = pb.validate(cfg)
    assert "REPO not set" in problems
    assert "PASSPHRASE not set" in problems
    assert "BACKUP_PATHS empty" in problems
    assert pb.main(["--config", "/nonexistent", "run"]) == 2


def test_archive_name_uses_prefix_and_timestamp():
    cfg = {"prefix": "pi"}
    now = datetime(2026, 8, 25, 4, 30, 1)
    assert pb.archive_name(cfg, now) == "pi-2026-08-25T043001"


# ------------------------------------------------------------ create

def test_init_and_create_roundtrip(tmp_path):
    src = make_sources(tmp_path)
    repo = tmp_path / "repo"
    conf = write_config(tmp_path, repo, [str(src / "etc")])
    cfg = cfg_from(tmp_path, conf)

    ok, err = pb.init_repo(cfg)
    assert ok, err
    assert pb.repo_initialized(cfg)
    # idempotent
    ok2, _ = pb.init_repo(cfg)
    assert ok2

    ok, name, stats = pb.create_archive(cfg, datetime.now())
    assert ok, stats
    assert stats["nfiles"] == 3
    assert stats["original_size"] > 0
    assert name.startswith("pi-")

    # identical second archive dedups to (almost) nothing. Fixed `now`
    # (1s apart): two datetime.now() calls can land in the same second
    # and collide on the second-resolution archive name (CI flake,
    # run 33231581403).
    ok2, name2, stats2 = pb.create_archive(
        cfg, datetime.now() + timedelta(seconds=1))
    assert ok2, stats2
    assert stats2["deduplicated_size"] < stats["original_size"]


def test_create_failure_reports_last_stderr_line(tmp_path):
    src = make_sources(tmp_path)
    repo = tmp_path / "repo"
    conf = write_config(tmp_path, repo, [str(src / "etc")])
    cfg = cfg_from(tmp_path, conf)
    ok, err = pb.init_repo(cfg)
    assert ok

    # break the source mid-flight: path gone
    cfg["paths"] = ["/nonexistent-backup-path"]
    ok, name, stats = pb.create_archive(cfg, datetime.now())
    assert not ok
    assert stats["error"]


# ------------------------------------------------------------- drill

def test_drill_passes_on_consistent_repo(tmp_path):
    src = make_sources(tmp_path)
    repo = tmp_path / "repo"
    conf = write_config(tmp_path, repo, [str(src / "etc")])
    cfg = cfg_from(tmp_path, conf)
    ok, err = pb.init_repo(cfg)
    assert ok, err

    ok, lines = pb.run_drill(cfg, datetime.now())
    assert ok, lines
    assert any("PASS" in ln for ln in lines)
    assert any("byte-compared 3" in ln for ln in lines)


def test_drill_detects_drift_between_archive_and_source(tmp_path):
    """The compare layer is what catches drift: archive, tamper the
    source, extract the OLD archive — restored bytes must differ."""
    src = make_sources(tmp_path)
    repo = tmp_path / "repo"
    conf = write_config(tmp_path, repo, [str(src / "etc")])
    cfg = cfg_from(tmp_path, conf)
    ok, err = pb.init_repo(cfg)
    assert ok, err
    ok, name, _ = pb.create_archive(cfg, datetime.now())
    assert ok

    (src / "etc" / "ntfy" / "server.yml").write_text("tampered\n")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        code, _, err = pb.run_cmd(
            ["borg", "extract", f"{cfg['repo']}::{name}"], cfg, cwd=tmp)
        assert code == 0, err
        ok, report = pb.compare_restored(tmp, limit=10)
        assert not ok
        assert report["mismatched"], report


def test_compare_restored_detects_missing_source(tmp_path):
    # compare maps a restored rel path back to '/' + rel; mirror the
    # tmp_path layout inside a fake restore root so sources exist
    root = tmp_path / "restored"
    rel_a = str(tmp_path.relative_to("/") / "a.txt")
    rel_b = str(tmp_path.relative_to("/") / "b.txt")
    (root / rel_a).parent.mkdir(parents=True)
    (root / rel_a).write_text("same")
    (root / rel_b).write_text("orphan")
    (tmp_path / "a.txt").write_text("same")
    # '/…/b.txt' does not exist at source
    ok, report = pb.compare_restored(root, limit=10)
    assert not ok
    assert report["missing"] == [rel_b]
    assert report["mismatched"] == []


def test_sample_files_picks_evenly():
    files = [f"f{i:03d}" for i in range(10)]
    picked = pb.sample_files(files, 3)
    assert picked == ["f000", "f004", "f009"]
    assert pb.sample_files(files, 99) == files


def test_file_hash_matches_sha256sum(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello pi-backup")
    out = subprocess.run(["sha256sum", str(p)], capture_output=True,
                         text=True, check=True).stdout
    assert pb.file_hash(p) == out.split()[0]


# ------------------------------------------------------------ actions

def test_act_run_full_cycle_notifies_and_prunes(tmp_path, capsys, monkeypatch):
    src = make_sources(tmp_path)
    repo = tmp_path / "repo"
    conf = write_config(
        tmp_path, repo, [str(src / "etc")],
        extra="PRUNE_KEEP_DAILY=1\nPRUNE_KEEP_WEEKLY=1\nPRUNE_KEEP_MONTHLY=1")
    cfg = cfg_from(tmp_path, conf)
    cfg["ntfy_url"] = "http://ntfy.invalid"
    cfg["ntfy_token"] = "tk_test"

    sent = []

    def fake_post(url, headers, payload, timeout=15):
        sent.append((url, headers, payload))
        return True

    monkeypatch.setattr(pb, "ntfy_post", fake_post)
    rc = pb.act_run(cfg, datetime.now())
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "✓" in out
    assert len(sent) == 1
    url, headers, payload = sent[0]
    assert url == "http://ntfy.invalid"
    assert headers["Authorization"] == "Bearer tk_test"
    assert payload["topic"] == "backups"
    assert payload["title"] == "pi-backup ok"
    assert "files" in payload["message"]

    # second run prunes: fake 'now' two days on so the first archive
    # falls outside every keep window
    from datetime import timedelta
    rc = pb.act_run(cfg, datetime.now() + timedelta(days=2))
    assert rc == 0
    rc = pb.act_list(cfg)
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "pi-" in out_text


def test_act_run_failure_pages_via_ntfy(tmp_path, capsys, monkeypatch):
    src = make_sources(tmp_path)
    repo = tmp_path / "repo"
    conf = write_config(tmp_path, repo, ["/nonexistent-backup-path"])
    cfg = cfg_from(tmp_path, conf)
    cfg["ntfy_url"] = "http://ntfy.invalid"
    cfg["ntfy_token"] = "tk_test"

    sent = []

    def fake_post(url, headers, payload, timeout=15):
        sent.append(payload)
        return True

    monkeypatch.setattr(pb, "ntfy_post", fake_post)
    rc = pb.act_run(cfg, datetime.now())
    assert rc == 1
    assert capsys.readouterr().err
    assert sent and sent[0]["title"] == "pi-backup FAILED"
    assert sent[0]["priority"] == 4


def test_act_run_notifies_without_ntfy_configured(tmp_path, capsys):
    # no NTFY_URL/TOKEN: must still succeed silently (best-effort notify)
    src = make_sources(tmp_path)
    repo = tmp_path / "repo"
    conf = write_config(tmp_path, repo, [str(src / "etc")])
    cfg = cfg_from(tmp_path, conf)
    rc = pb.act_run(cfg, datetime.now())
    assert rc == 0
    assert "✓" in capsys.readouterr().out


def test_act_restore_extracts_to_dest(tmp_path, capsys):
    src = make_sources(tmp_path)
    repo = tmp_path / "repo"
    conf = write_config(tmp_path, repo, [str(src / "etc")])
    cfg = cfg_from(tmp_path, conf)
    ok, err = pb.init_repo(cfg)
    assert ok, err
    ok, name, _ = pb.create_archive(cfg, datetime.now())
    assert ok

    dest = tmp_path / "restore-dest"
    rc = pb.act_restore(cfg, name, None, dest)
    assert rc == 0
    # borg stores absolute source paths minus the leading '/', so the
    # restore lands under dest/tmp/pytest-.../src/etc/...
    inner = dest / src.relative_to("/")
    assert (inner / "etc" / "ntfy" / "server.yml").is_file()
    assert pb.file_hash(inner / "etc" / "ntfy" / "server.yml") == \
        pb.file_hash(src / "etc" / "ntfy" / "server.yml")
    assert (inner / "etc" / "pi-backup.conf").is_file()


def test_act_check_green_on_fresh_repo(tmp_path, capsys):
    src = make_sources(tmp_path)
    repo = tmp_path / "repo"
    conf = write_config(tmp_path, repo, [str(src / "etc")])
    cfg = cfg_from(tmp_path, conf)
    ok, err = pb.init_repo(cfg)
    assert ok, err
    ok, name, _ = pb.create_archive(cfg, datetime.now())
    assert ok
    rc = pb.act_check(cfg)
    assert rc == 0
    assert "repository consistent" in capsys.readouterr().out


# ------------------------------------------------------------- notify

def test_notify_dry_run_prints_and_succeeds(tmp_path, capsys):
    cfg = {"ntfy_url": "http://x", "ntfy_token": "t",
           "ntfy_topic": "backups"}
    assert pb.notify(cfg, "pi-backup ok", "3 files", dry_run=True)
    out = capsys.readouterr().out
    assert "backups" in out and "pi-backup ok" in out


def test_notify_skipped_when_unconfigured():
    assert pb.notify({}, "t", "m") is False


def test_notify_wraps_url_errors(monkeypatch):
    cfg = {"ntfy_url": "http://x", "ntfy_token": "t",
           "ntfy_topic": "backups"}

    def boom(req, timeout=15):
        raise OSError("net down")

    monkeypatch.setattr(pb.urllib.request, "urlopen", boom)
    assert pb.notify(cfg, "t", "m") is False


def test_human_sizes():
    assert pb.human(512) == "512 B"
    assert pb.human(2048) == "2.0 KB"
    assert pb.human(3 * 1024 * 1024) == "3.0 MB"


# --------------------------------------------------------------- cli

def test_main_run_happy_path(tmp_path, capsys, monkeypatch):
    src = make_sources(tmp_path)
    repo = tmp_path / "repo"
    conf = write_config(tmp_path, repo, [str(src / "etc")])
    monkeypatch.setattr(pb, "ntfy_post", lambda *a, **k: True)
    cfg = pb.load_config(conf)  # validated inside main
    rc = pb.main(["--config", str(conf), "run"])
    assert rc == 0
    assert "✓" in capsys.readouterr().out


def test_main_list_ignores_missing_config_gracefully(tmp_path, capsys):
    rc = pb.main(["--config", "/nonexistent", "list"])
    assert rc == 1
    assert capsys.readouterr().err


# ---------------------------------------- shared ntfy_lib delegation

def test_ntfy_post_suppressed_when_muted(monkeypatch, tmp_path, capsys):
    mute = tmp_path / "mute"
    mute.write_text("storm drill\n")
    monkeypatch.setattr(pb.ntfy_lib, "DEFAULT_MUTE_FILE", str(mute))

    def boom(req, timeout=None):
        raise AssertionError("network must not be touched while muted")

    monkeypatch.setattr(pb.urllib.request, "urlopen", boom)
    assert pb.ntfy_post("http://n", {"Authorization": "Bearer x"},
                        {"topic": "backups"}) is True
    assert "muted" in capsys.readouterr().err


# ------------------------------------------------------ live-DB snapshots

def make_sqlite(path, rows=3):
    """A real SQLite DB, the way the host's state.db is real: WAL, so a
    reader never blocks on the writer this test keeps open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("pragma journal_mode=wal")
    con.execute("create table t (id integer primary key, v text)")
    con.executemany("insert into t (v) values (?)",
                    [(f"row-{i}",) for i in range(rows)])
    con.commit()
    con.close()
    return path


def borg_listing(repo, name, cfg):
    code, out, err = pb.run_cmd(
        ["borg", "list", "--short", f"{repo}::{name}"], cfg)
    assert code == 0, err
    return [line.lstrip("/") for line in out.split()]


def test_is_sqlite_file_reads_the_magic(tmp_path):
    assert pb.is_sqlite_file(make_sqlite(tmp_path / "state.db"))
    plain = tmp_path / "notes.txt"
    plain.write_text("not a database\n")
    assert not pb.is_sqlite_file(plain)
    assert not pb.is_sqlite_file(tmp_path / "missing.db")


def test_snapshot_dir_defaults_and_is_configurable(tmp_path):
    conf = write_config(tmp_path, tmp_path / "repo", [str(tmp_path)],
                        extra=f"SNAPSHOT_DIR={tmp_path}/snaps")
    assert pb.load_config(conf)["snapshot_dir"] == f"{tmp_path}/snaps"
    assert pb.load_config("/nonexistent")["snapshot_dir"] == \
        pb.DEFAULT_SNAPSHOT_DIR
    assert "SNAPSHOT_DIR not absolute: snaps" in pb.validate(
        {**pb.load_config(conf), "snapshot_dir": "snaps"})


def test_create_archive_snapshots_live_db_and_excludes_it(tmp_path):
    """The nightly run must never hand borg a file that is being written
    (borg aborts: "file changed while we backed it up"): the DB goes in
    as a consistent snapshot and the live file is left out."""
    src = tmp_path / "src"
    live = make_sqlite(src / "hermes" / "state.db", rows=3)
    snaps = tmp_path / "snaps"
    repo = tmp_path / "repo"
    conf = write_config(tmp_path, repo, [str(src / "hermes")],
                        extra=f"SNAPSHOT_DIR={snaps}")
    cfg = cfg_from(tmp_path, conf)
    assert pb.init_repo(cfg)[0]

    writer = sqlite3.connect(live)
    writer.execute("insert into t (v) values ('committed-before-backup')")
    writer.commit()
    inflight = sqlite3.connect(live)          # a transaction still open
    inflight.execute("insert into t (v) values ('in-flight')")

    ok, name, stats = pb.create_archive(cfg, datetime.now())
    assert ok, stats
    assert stats["snapshots"] == [str(snaps / str(live).lstrip("/"))]

    listing = borg_listing(repo, name, cfg)
    assert str(live).lstrip("/") not in listing           # live DB excluded
    assert stats["snapshots"][0].lstrip("/") in listing   # snapshot archived

    ex = tmp_path / "ex"
    ex.mkdir()
    code, _, err = pb.run_cmd(
        ["borg", "extract", f"{repo}::{name}"], cfg, cwd=ex)
    assert code == 0, err
    restored = ex / str(stats["snapshots"][0]).lstrip("/")
    con = sqlite3.connect(restored)
    assert con.execute("pragma integrity_check").fetchone()[0] == "ok"
    rows = [r[0] for r in con.execute("select v from t").fetchall()]
    con.close()
    assert len(rows) == 4 and "committed-before-backup" in rows
    assert "in-flight" not in rows            # uncommitted work not captured

    inflight.rollback()
    writer.close()
    inflight.close()


def test_stale_snapshots_do_not_accumulate(tmp_path):
    src = tmp_path / "src"
    make_sqlite(src / "a.db")
    gone = make_sqlite(src / "b.db")
    snaps = tmp_path / "snaps"
    conf = write_config(tmp_path, tmp_path / "repo", [str(src)],
                        extra=f"SNAPSHOT_DIR={snaps}")
    cfg = cfg_from(tmp_path, conf)
    ok, live, dumps, err = pb.snapshot_live_dbs(cfg)
    assert ok, err
    assert len(dumps) == 2

    gone.unlink()                             # a DB disappears
    ok, live, dumps, err = pb.snapshot_live_dbs(cfg)
    assert ok, err
    assert dumps == [str(snaps / str(src / "a.db").lstrip("/"))]


def test_create_archive_fails_loudly_when_a_db_cannot_be_snapshotted(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "torn.db").write_bytes(pb.SQLITE_MAGIC + b"garbage" * 20)
    conf = write_config(tmp_path, tmp_path / "repo", [str(src)],
                        extra=f"SNAPSHOT_DIR={tmp_path}/snaps")
    cfg = cfg_from(tmp_path, conf)
    assert pb.init_repo(cfg)[0]
    ok, name, stats = pb.create_archive(cfg, datetime.now())
    assert not ok
    assert "sqlite snapshot" in stats["error"]
