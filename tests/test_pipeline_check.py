"""pipeline-check heal ledger: self-heals must land in status.json.

Hermetic end-to-end: a real stranded-commit push heal is driven through
the REAL pipeline-check script against a throwaway HOME_DIR with a local
bare remote (no network, no gh, no systemd involved).
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "pipeline-check"


def _git(cwd, *args, env=None):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True, env=env)


def _commit(proj, msg="init"):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    _git(proj, "commit", "-q", "-m", msg, env=env)


def _setup(home, origin, push_after_commit):
    proj = Path(home) / "myproj"
    proj.mkdir()
    (proj / "app.py").write_text("print('hi')\n")
    _git(proj, "init", "-q", "-b", "master")
    _git(proj, "add", ".")
    _commit(proj)
    _git(proj, "remote", "add", "origin", str(origin))
    if push_after_commit:
        _git(proj, "push", "-q", "origin", "master")
    return proj


def _run_check(home):
    env = dict(os.environ, GUARD_HOME=home, HOME=home)
    r = subprocess.run([str(SCRIPT)], capture_output=True, text=True,
                       timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return r


def test_stranded_commit_heal_is_recorded():
    with tempfile.TemporaryDirectory() as home, \
            tempfile.TemporaryDirectory() as origin_dir:
        origin = Path(origin_dir) / "origin_repo"
        _git(origin_dir, "init", "-q", "--bare", "origin_repo")
        _setup(home, origin, push_after_commit=False)  # one commit stranded
        _run_check(home)

        status = Path(home) / ".local/state/pipeline-check/status.json"
        assert status.exists(), "no status.json written for a real heal"
        data = json.loads(status.read_text(encoding="utf-8"))
        assert len(data["heals"]) == 1
        heal = data["heals"][0]
        assert heal["what"] == "push-stranded-commits"
        assert "myproj" in heal["detail"] and "master" in heal["detail"]
        assert heal["ts"]
        assert data["updated"] == heal["ts"]


def test_no_heal_means_no_status_file():
    with tempfile.TemporaryDirectory() as home, \
            tempfile.TemporaryDirectory() as origin_dir:
        origin = Path(origin_dir) / "origin_repo"
        _git(origin_dir, "init", "-q", "--bare", "origin_repo")
        _setup(home, origin, push_after_commit=True)  # already clean
        r = _run_check(home)
        assert r.stdout == ""  # silent when green AND nothing fixed
        assert not (Path(home) / ".local/state/pipeline-check/status.json").exists()
