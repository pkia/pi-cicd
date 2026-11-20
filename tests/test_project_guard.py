"""project-guard adopt deny-list: an explicit filter, visible in the log.

Hermetic end-to-end: the REAL project-guard script runs against a
throwaway HOME with no gh auth (HOME is the temp dir, so the guard's
`gh auth status` probe finds no config and adopts locally only). No
network, no systemd.
"""
import os
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "project-guard"

IDENTITY = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _project(home, name, sub=""):
    d = Path(home) / sub / name
    d.mkdir(parents=True)
    (d / "app.py").write_text("print('hi')\n")
    return d


def _deny_list(home, text):
    f = Path(home) / ".config/project-guard/deny-list"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def _run(home):
    env = dict(os.environ, GUARD_HOME=home, HOME=home, **IDENTITY)
    r = subprocess.run([str(SCRIPT)], capture_output=True, text=True,
                       timeout=120, env=env)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    return r.stdout


def _log(home):
    f = Path(home) / ".local/state/project-guard.log"
    return f.read_text() if f.exists() else ""   # guard writes nothing when green


def test_denied_directory_is_left_untouched_and_reported():
    with tempfile.TemporaryDirectory() as home:
        keep = _project(home, "myproj")
        skip = _project(home, "vendor-src")
        _deny_list(home, "# scratch trees we pull, not code we own\n"
                          "\n"
                          "  vendor-*   # trailing comment\n")

        out = _run(home)

        assert (keep / ".git").is_dir(), "a normal project must still be adopted"
        assert not (skip / ".git").exists(), "denied directory must stay unversioned"
        assert not (skip / ".gitignore").exists()
        assert "adopt vendor-src SKIPPED: deny-list pattern 'vendor-*' matched" in _log(home)
        assert "discovered unversioned project" in _log(home)   # only for myproj
        assert out.count("adopt vendor-src SKIPPED") == 1


def test_skip_is_reported_once_not_every_sweep():
    with tempfile.TemporaryDirectory() as home:
        _project(home, "vendor-src")
        _deny_list(home, "vendor-*\n")

        _run(home)
        _run(home)
        _run(home)

        assert _log(home).count("adopt vendor-src SKIPPED") == 1
        state = Path(home) / ".local/state/project-guard-denied.state"
        assert state.read_text().splitlines() == [f"{home}/vendor-src|vendor-*"]


def test_no_deny_list_changes_nothing():
    with tempfile.TemporaryDirectory() as home:
        keep = _project(home, "myproj")
        assert _run(home)
        assert (keep / ".git").is_dir()
        assert "SKIPPED" not in _log(home)


def test_path_patterns_and_nested_projects():
    with tempfile.TemporaryDirectory() as home:
        skip = _project(home, "thing", sub="apps")
        keep = _project(home, "other", sub="apps")
        _deny_list(home, f"{home}/apps/thing\n")

        _run(home)

        assert not (skip / ".git").exists()
        assert (keep / ".git").is_dir()
        assert "deny-list pattern" in _log(home)


def test_read_only_upstream_is_skipped_not_called_diverged(tmp_path):
    """A mirror of someone else's repo cannot be pushed to.

    Before this, every sweep logged a false "diverged from origin? - needs a
    human" for it (12k lines by 2026-11-19). gh is stubbed: logged in as pkia,
    .permissions.push false for the upstream repo.
    """
    home = tmp_path / "home"
    home.mkdir()
    proj = _project(str(home), "upstream-mirror")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "auth status") exit 0 ;;\n'
        '  "api user") echo pkia; exit 0 ;;\n'
        '  "api repos/"*) [ "$3" = "-q" ] && echo false; exit 0 ;;\n'
        "esac\n"
        "exit 1\n"
    )
    gh.chmod(0o755)
    subprocess.run(["git", "-C", str(proj), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(proj), "remote", "add", "origin",
                    "https://github.com/someone-else/upstream-mirror.git"], check=True)
    subprocess.run(["git", "-C", str(proj), "add", "."], check=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-q", "-m", "init"],
                   check=True, capture_output=True, env=dict(os.environ, **IDENTITY))
    head_before = subprocess.run(["git", "-C", str(proj), "rev-parse", "HEAD"],
                                 check=True, capture_output=True, text=True).stdout

    env = dict(os.environ, GUARD_HOME=str(home), HOME=str(home), **IDENTITY)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    r = subprocess.run([str(SCRIPT)], capture_output=True, text=True,
                       timeout=120, env=env)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"

    log = _log(str(home))
    assert "read-only (upstream mirror) - pushes skipped" in log
    assert "needs a human" not in log
    head_after = subprocess.run(["git", "-C", str(proj), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True).stdout
    assert head_after == head_before, "no CI commit on a repo that can never take it"
    assert (home / ".local/state/project-guard-readonly.state").exists()


def test_existing_repo_is_still_backed_up_despite_a_deny_list_hit():
    """The deny-list filters adoption only — autosave must keep running."""
    with tempfile.TemporaryDirectory() as home:
        proj = _project(home, "vendor-src")
        subprocess.run(["git", "-C", str(proj), "init", "-q", "-b", "main"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(proj), "add", "."], check=True,
                       capture_output=True)
        env = dict(os.environ, **IDENTITY)
        subprocess.run(["git", "-C", str(proj), "commit", "-q", "-m", "init"],
                       check=True, capture_output=True, env=env)
        _deny_list(home, "vendor-src\n")

        _run(home)

        head = subprocess.run(["git", "-C", str(proj), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout
        assert head.strip()
        assert "SKIPPED" not in _log(home), "an existing repo is not an adoption"
