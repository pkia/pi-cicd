"""Tests for staleness — the report on a process older than its code.

Hermetic: the systemd side is a recording runner answering with recorded
`systemctl show` output, the wallclock and uptime are files, and the code
side is a real (tiny) git repo in tmp_path whose commit dates are pinned
through GIT_COMMITTER_DATE/GIT_AUTHOR_DATE — so "the process predates the
commit" is a measured fact, not a stub.
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "staleness"

spec = importlib.util.spec_from_loader(
    "staleness",
    importlib.machinery.SourceFileLoader("staleness", str(SCRIPT)),
)
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)


NOW = 1_800_000_000        # a fixed wallclock epoch
UPTIME_S = 10_000          # seconds since boot -> boot wall = NOW - UPTIME_S


class RecordingRunner:
    """A run_command stand-in: records every argv, answers git for real.

    systemctl is answered from `show_output` (a recorded `systemctl show`
    reply); everything else is handed to the real runner, so the code side
    of the comparison is a genuine git reading, not a fixture.
    """

    def __init__(self, show_output=""):
        self.calls = []
        self.show_output = show_output

    def __call__(self, argv, timeout=60):
        self.calls.append(list(argv))
        if argv and argv[0] == "systemctl":
            return 0, self.show_output
        return _real(argv, timeout)


def _real(argv, timeout=60):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def show(monotonic_us, state="active"):
    return (f"ActiveState={state}\n"
            f"ActiveEnterTimestampMonotonic={monotonic_us}\n")


def monotonic_for(start_epoch):
    """Monotonic µs that converts back to start_epoch under NOW/UPTIME_S."""
    return int((start_epoch - (NOW - UPTIME_S)) * 1_000_000)


def git(repo, *args, when=None):
    env = dict(os.environ)
    if when is not None:
        stamp = f"{when} +0000"
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, env=env)


def make_repo(tmp_path, base_epoch, new_epoch):
    """A repo with an old commit and a newer one touching src/."""
    repo = tmp_path / "code"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "README").write_text("base\n")
    git(repo, "add", "README")
    git(repo, "commit", "-q", "-m", "base commit", when=base_epoch)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("fix\n")
    git(repo, "add", "src")
    git(repo, "commit", "-q", "-m", "land the fix", when=new_epoch)
    rc, out = _real(["git", "-C", str(repo), "rev-parse", "HEAD"])
    return repo, out.strip()


def argv_for(tmp_path, unit="widget.service", extra=()):
    uptime = tmp_path / "uptime"
    uptime.write_text(f"{UPTIME_S}.00 0.00\n")
    return [unit, "--repo", str(tmp_path / "code"), "--path", "src",
            "--uptime-file", str(uptime), "--grace", "0", *extra]


def run(monkeypatch, tmp_path, show_output, extra=(), argv_extra=()):
    monkeypatch.setenv("STALENESS_NOW", str(NOW))
    runner = RecordingRunner(show_output)
    monkeypatch.setattr(st, "run_command", runner)
    rc = st.main(argv_for(tmp_path, extra=extra))
    return rc, runner


def test_stale_when_the_process_predates_the_newest_commit(tmp_path, monkeypatch,
                                                          capsys):
    """Acceptance 1: started before the commit -> fails and names the commit."""
    repo, sha = make_repo(tmp_path, NOW - 7200, NOW - 600)
    # started 3000 s ago: after the base commit, 2400 s before the fix.
    rc, runner = run(monkeypatch, tmp_path, show(monotonic_for(NOW - 3000)))

    err = capsys.readouterr().err
    assert rc == 1
    assert "STALE" in err
    assert "widget.service" in err
    assert sha[:12] in err and "land the fix" in err
    # reads only: no command that could restart, stop or remove anything.
    verbs = {c[0] for c in runner.calls}
    assert verbs <= {"systemctl", "git"}
    assert all(not any(w in " ".join(c) for w in ("restart", "stop", "start-",
                                                  "rm ", "kill"))
               for c in runner.calls)


def test_fresh_after_a_restart(tmp_path, monkeypatch, capsys):
    """Acceptance 2: the same unit restarted after the commit exits 0."""
    make_repo(tmp_path, NOW - 7200, NOW - 600)
    rc, _ = run(monkeypatch, tmp_path, show(monotonic_for(NOW - 60)))

    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "" and out.err == ""


def test_silent_forever_when_the_code_has_not_moved(tmp_path, monkeypatch,
                                                    capsys):
    """Acceptance 3: newest commit older than the process -> silent, exit 0."""
    make_repo(tmp_path, NOW - 9000, NOW - 7200)
    rc, _ = run(monkeypatch, tmp_path, show(monotonic_for(NOW - 3600)))

    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "" and out.err == ""


def test_grace_absorbs_a_restart_that_lags_its_commit(tmp_path, monkeypatch,
                                                      capsys):
    """A deploy commits then restarts seconds later — not a finding."""
    make_repo(tmp_path, NOW - 7200, NOW - 600)
    uptime = tmp_path / "uptime"
    uptime.write_text(f"{UPTIME_S}.00 0.00\n")
    monkeypatch.setenv("STALENESS_NOW", str(NOW))
    runner = RecordingRunner(show(monotonic_for(NOW - 630)))  # 30 s behind
    monkeypatch.setattr(st, "run_command", runner)
    rc = st.main([argv_for(tmp_path)[0], "--repo", str(tmp_path / "code"),
                  "--path", "src", "--uptime-file", str(uptime),
                  "--grace", "120"])
    out = capsys.readouterr()
    assert rc == 0 and out.out == "" and out.err == ""


def test_a_stopped_unit_is_not_stale_and_is_only_reported_when_asked(
        tmp_path, monkeypatch, capsys):
    make_repo(tmp_path, NOW - 7200, NOW - 600)
    rc, _ = run(monkeypatch, tmp_path, show(0, state="inactive"))
    out = capsys.readouterr()
    assert rc == 0 and out.out == ""          # quiet by default
    rc, _ = run(monkeypatch, tmp_path, show(0, state="inactive"),
                extra=["--verbose"])
    assert rc == 0 and "not running" in capsys.readouterr().out


def test_unmeasurable_code_side_exits_two(tmp_path, monkeypatch, capsys):
    """A repo/path with no commits to compare is named, not silently passed."""
    repo = tmp_path / "code"
    repo.mkdir()
    rc, _ = run(monkeypatch, tmp_path, show(monotonic_for(NOW - 60)))
    assert rc == 2
    assert "no commit to compare" in capsys.readouterr().err


def test_json_carries_both_sides(tmp_path, monkeypatch, capsys):
    repo, sha = make_repo(tmp_path, NOW - 7200, NOW - 600)
    rc, _ = run(monkeypatch, tmp_path, show(monotonic_for(NOW - 3000)),
                extra=["--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert data["state"] == "stale"
    assert data["commit"] == sha and data["gap_s"] == -2400
    assert data["start_epoch"] == NOW - 3000


def test_source_has_no_restart_path():
    """The report is the output; the restart stays a decision."""
    src = SCRIPT.read_text()
    for verb in ("restart", "stop", "kill", "disable", "enable"):
        assert f'"{verb}"' not in src and f"'{verb}'" not in src
