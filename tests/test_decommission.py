"""Tests for decommission — the project teardown runbook.

Hermetic: every source directory is a tmp_path wired in through the CLI
flags, the cron listing is a recorded file, and the command runner is a
recording fake — so "changes nothing" is asserted against a runner that
would have recorded any command, and nothing touches the real box.
"""
import importlib.machinery
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "decommission"

spec = importlib.util.spec_from_loader(
    "decommission",
    importlib.machinery.SourceFileLoader("decommission", str(SCRIPT)),
)
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)


RECORDED_CRON = """\
  db50209537ca [active]
    Name:      Pi health watchdog
    Schedule:  every 30m

  4daccc802319 [active]
    Name:      widget live loop
    Schedule:  0 1 * * *

  1234567890ab [paused]
    Name:      Radar implementer
    Schedule:  30 5 * * *
"""


class RecordingRunner:
    """A run_command stand-in that records every argv it is handed."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, argv, timeout=60):
        self.calls.append(list(argv))
        return self.results.get(tuple(argv), (0, ""))


def make_git_repo(path, origin):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n"
        f'[remote "origin"]\n\turl = {origin}\n\tfetch = +refs/heads/*\n')


def stub_project(tmp_path, project="widget"):
    """A project that owns one of every artifact kind."""
    repos = tmp_path / "repos"
    repo = repos / project
    make_git_repo(repo, f"https://github.com/pkia/{project}.git")

    sysd = tmp_path / "systemd"
    sysd.mkdir()
    (sysd / f"{project}.service").write_text("[Unit]\n")
    (sysd / f"{project}.timer").write_text("[Unit]\n")

    user = tmp_path / "user-units"
    user.mkdir()
    (user / f"{project}-user.service").write_text("[Unit]\n")

    cron = tmp_path / "cron.txt"
    cron.write_text(RECORDED_CRON)

    remotes = tmp_path / "remotes.json"
    remotes.write_text(json.dumps({project: [
        {"host": "vps-1", "task": f"{project} boot",
         "detail": r"C:\x\run.cmd"}]}))
    return repos, sysd, user, cron, remotes


def argv_for(project, dirs, extra=()):
    repos, sysd, user, cron, remotes = dirs
    return [project, "--repos-dir", str(repos), "--systemd-dir", str(sysd),
            "--user-unit-dir", str(user), "--cron-file", str(cron),
            "--remotes", str(remotes), *extra]


def test_stub_project_lists_every_artifact_and_changes_nothing(tmp_path, capsys):
    dirs = stub_project(tmp_path)
    runner = RecordingRunner()
    dc.run_command = runner  # any command would be recorded here

    rc = dc.main(argv_for("widget", dirs))
    out = capsys.readouterr().out

    assert rc == 0
    # every artifact kind the stub owns is named
    assert "[repo] widget" in out
    assert "[unit_system] widget.service" in out
    assert "[unit_system] widget.timer" in out
    assert "[unit_user] widget-user.service" in out
    assert "[cron] widget live loop" in out
    assert "[remote_task] widget boot @ vps-1" in out
    # and nothing ran, and the tree is untouched
    assert runner.calls == []
    for d in dirs[:5]:
        assert d.exists()


def test_already_gone_project_reports_only_what_exists_and_exits_zero(tmp_path, capsys):
    repos = tmp_path / "empty-repos"
    repos.mkdir()
    sysd = tmp_path / "empty-systemd"
    sysd.mkdir()
    user = tmp_path / "empty-user"
    user.mkdir()
    cron = tmp_path / "cron.txt"
    cron.write_text(RECORDED_CRON)
    remotes = tmp_path / "remotes.json"
    remotes.write_text("{}")

    rc = dc.main(argv_for("ghost", (repos, sysd, user, cron, remotes)))
    out = capsys.readouterr().out

    assert rc == 0
    assert "0 artifacts found" in out
    assert "[repo]" not in out and "[unit_system]" not in out
    assert "[cron]" not in out


def test_unclassifiable_unit_file_exits_nonzero_and_changes_nothing(tmp_path, capsys):
    repos = tmp_path / "repos"
    repos.mkdir()
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    (sysd / "widget.socket").write_text("[Unit]\n")  # not .service/.timer
    user = tmp_path / "user"
    user.mkdir()
    cron = tmp_path / "cron.txt"
    cron.write_text("")
    remotes = tmp_path / "remotes.json"
    remotes.write_text("{}")

    runner = RecordingRunner()
    dc.run_command = runner
    rc = dc.main(argv_for("widget", (repos, sysd, user, cron, remotes)))
    err = capsys.readouterr().err

    assert rc == 2
    assert "cannot classify" in err
    assert runner.calls == []
    assert (sysd / "widget.socket").exists()


def test_apply_removes_units_and_cron_but_only_those(tmp_path, capsys):
    dirs = stub_project(tmp_path)
    repos, sysd, user, cron, remotes = dirs
    runner = RecordingRunner()
    dc.run_command = runner

    rc = dc.main(argv_for("widget", dirs, extra=["--apply"]))
    capsys.readouterr()

    assert rc == 0
    calls = {tuple(c) for c in runner.calls}
    assert ("systemctl", "disable", "--now", "widget.service") in calls
    assert ("rm", "-f", str(sysd / "widget.service")) in calls
    assert ("systemctl", "--user", "disable", "--now", "widget-user.service") in calls
    assert ("hermes", "cron", "remove", "4daccc802319") in calls
    # the repo is never touched — no command may mention it
    assert not any("repos/widget" in " ".join(c) for c in runner.calls)
    assert not any(c[:2] == ["rm", "-rf"] for c in runner.calls)
    assert (repos / "widget").exists()


def test_apply_reports_a_failed_action_and_exits_one(tmp_path, capsys):
    dirs = stub_project(tmp_path)
    sysd = dirs[1]
    runner = RecordingRunner(results={
        ("rm", "-f", str(sysd / "widget.service")): (1, "Permission denied")})
    dc.run_command = runner

    rc = dc.main(argv_for("widget", dirs, extra=["--apply"]))
    err = capsys.readouterr().err

    assert rc == 1
    assert "FAILED" in err and "widget.service" in err


def test_json_output_lists_the_artifacts(tmp_path, capsys):
    dirs = stub_project(tmp_path)
    dc.run_command = RecordingRunner()

    rc = dc.main(argv_for("widget", dirs, extra=["--json"]))
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    kinds = {a["kind"] for a in data["artifacts"]}
    assert {"repo", "unit_system", "unit_user", "cron", "remote_task"} <= kinds
    assert data["unclassifiable"] == []


def test_check_gate_flags_a_leftover_unit_and_changes_nothing(tmp_path, capsys):
    """Acceptance, first half: --check exits non-zero and names the leftover."""
    dirs = stub_project(tmp_path)
    runner = RecordingRunner()
    dc.run_command = runner  # any command would be recorded here

    rc = dc.main(argv_for("widget", dirs, extra=["--check"]))
    err = capsys.readouterr().err

    assert rc == 3
    assert "NOT CLEAN" in err
    assert "widget.service" in err          # the leftover is named
    assert runner.calls == []               # a gate never changes anything


def test_check_gate_is_clean_once_the_removable_artifacts_are_gone(tmp_path, capsys):
    """Acceptance, second half: after the units and cron job are removed the
    same project is clean — its repo and remote task remain but are reported,
    not removability the gate owns."""
    dirs = stub_project(tmp_path)
    repos, sysd, user, cron, remotes = dirs
    dc.run_command = RecordingRunner()
    # what --apply leaves behind: units unlinked, cron job gone.
    for f in list(sysd.glob("widget*")) + list(user.glob("widget*")):
        f.unlink()
    cron.write_text("")

    rc = dc.main(argv_for("widget", dirs, extra=["--check"]))
    out = capsys.readouterr().out

    assert rc == 0
    assert "CLEAN" in out
    assert "[repo] widget" in out           # still reported ...
    assert "[remote_task]" in out           # ... but not locally removable


def test_check_and_apply_are_mutually_exclusive():
    import pytest
    with pytest.raises(SystemExit):
        dc.build_parser().parse_args(["widget", "--check", "--apply"])


# --- the registry's read-only query -------------------------------------

QUERY = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "vps-1", "schtasks", "/query", "/fo", "LIST"]

SCHTASKS_LISTING = """\

TaskName: \\MT5 Bridge Boot
Status:     Unknown

TaskName: \\MT5 Terminal
Status:     Ready

TaskName: \\MT5 Terminal Boot
Status:     Unknown
"""


def query_dirs(tmp_path, entries, with_unit=False):
    """Dirs for a project whose only artifact is remote task declaration(s)."""
    repos = tmp_path / "repos"
    repos.mkdir()
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    if with_unit:
        (sysd / "widget.service").write_text("[Unit]\n")
    user = tmp_path / "user"
    user.mkdir()
    cron = tmp_path / "cron.txt"
    cron.write_text("")
    remotes = tmp_path / "remotes.json"
    remotes.write_text(json.dumps({"widget": entries}))
    return repos, sysd, user, cron, remotes


def mt5_entries(**override):
    entry = {"host": "vps-1", "task": "MT5 Bridge Boot",
             "detail": r"C:\mt5-bridge\run_bridge.cmd (path removed)",
             "query": QUERY}
    no_query = override.pop("_no_query", False)
    entry.update(override)
    if no_query:
        entry.pop("query")
    return [entry]


def test_query_measures_a_task_still_registered(tmp_path, capsys):
    """Acceptance: with the registry present the tool names the remote task
    the host still holds — measured from the query's own output — and the
    verdict never makes --check unclean (a remote task is not removable
    here)."""
    dirs = query_dirs(tmp_path, mt5_entries())
    runner = RecordingRunner(results={tuple(QUERY): (0, SCHTASKS_LISTING)})
    dc.run_command = runner

    rc = dc.main(argv_for("widget", dirs, extra=["--check"]))
    out = capsys.readouterr().out

    assert rc == 0
    assert "[remote_task] MT5 Bridge Boot @ vps-1" in out
    assert "still registered on vps-1" in out
    assert "TaskName: \\MT5 Bridge Boot" in out      # the measured line quoted
    assert runner.calls == [list(QUERY)]             # the query, and only it


def test_query_json_carries_the_state(tmp_path, capsys):
    dirs = query_dirs(tmp_path, mt5_entries())
    dc.run_command = RecordingRunner(results={tuple(QUERY): (0, SCHTASKS_LISTING)})

    rc = dc.main(argv_for("widget", dirs, extra=["--json"]))
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    remote = [a for a in data["artifacts"] if a["kind"] == "remote_task"]
    assert len(remote) == 1
    assert remote[0]["state"] == "measured"


def test_one_query_serves_every_entry_sharing_it(tmp_path, capsys):
    """The three MT5 tasks share one listing command — one call, three verdicts."""
    entries = [
        {"host": "vps-1", "task": name, "detail": "orphan", "query": QUERY}
        for name in ("MT5 Bridge Boot", "MT5 Terminal", "MT5 Terminal Boot")
    ]
    dirs = query_dirs(tmp_path, entries)
    runner = RecordingRunner(results={tuple(QUERY): (0, SCHTASKS_LISTING)})
    dc.run_command = runner

    rc = dc.main(argv_for("widget", dirs))
    out = capsys.readouterr().out

    assert rc == 0
    assert "3 artifact(s) found" in out
    # each verdict prints twice: in the artifact list and again in the
    # manual-only section render() closes with.
    assert out.count("still registered on vps-1") == 6
    assert runner.calls == [list(QUERY)]             # memoised, not thrice


def test_query_cleared_marks_the_declaration_stale(tmp_path, capsys):
    """A host that answers but no longer lists the task retires the
    declaration — stale, not an orphan."""
    dirs = query_dirs(tmp_path, mt5_entries())
    dc.run_command = RecordingRunner(
        results={tuple(QUERY): (0, "INFO: No tasks are scheduled.")})

    rc = dc.main(argv_for("widget", dirs))
    out = capsys.readouterr().out

    assert rc == 0
    assert "[remote_task] MT5 Bridge Boot @ vps-1" in out   # reported, not absent
    assert "declaration is stale" in out
    assert "still registered" not in out


def test_query_failure_reports_unreachable_not_silently_absent(tmp_path, capsys):
    """An unreachable host leaves the declaration standing, named as such."""
    dirs = query_dirs(tmp_path, mt5_entries())
    dc.run_command = RecordingRunner(
        results={tuple(QUERY): (255, "ssh: connect to host vps-1 port 22: "
                                     "Connection refused")})

    rc = dc.main(argv_for("widget", dirs))
    out = capsys.readouterr().out

    assert rc == 0
    assert "[remote_task] MT5 Bridge Boot @ vps-1" in out
    assert "unreachable when checked (rc=255)" in out
    assert "declaration only" in out


def test_entry_without_query_is_declared_and_touches_nothing(tmp_path, capsys):
    dirs = query_dirs(tmp_path, mt5_entries(_no_query=True))
    runner = RecordingRunner()
    dc.run_command = runner

    rc = dc.main(argv_for("widget", dirs, extra=["--json"]))
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    remote = [a for a in data["artifacts"] if a["kind"] == "remote_task"]
    assert remote[0]["state"] == "declared"
    assert "by hand" in remote[0]["detail"]
    assert runner.calls == []


def test_apply_acts_only_locally_and_only_queries_the_remote(tmp_path, capsys):
    """No run removes anything remote: with a leftover unit and a measured
    remote orphan, --apply's every recorded call is either a local action or
    the verbatim read-only query — nothing else names the remote host."""
    dirs = query_dirs(tmp_path, mt5_entries(), with_unit=True)
    sysd = dirs[1]
    runner = RecordingRunner(results={tuple(QUERY): (0, SCHTASKS_LISTING)})
    dc.run_command = runner

    rc = dc.main(argv_for("widget", dirs, extra=["--apply"]))
    capsys.readouterr()

    assert rc == 0
    remote_calls = [c for c in runner.calls if "vps-1" in " ".join(c)]
    assert remote_calls == [list(QUERY)]
    assert not any(c[:2] == ["rm", "-rf"] for c in runner.calls)
    assert not any("delete" in " ".join(c).lower()
                   or "unregister" in " ".join(c).lower()
                   for c in runner.calls)
    assert ("systemctl", "disable", "--now", "widget.service") in \
        {tuple(c) for c in runner.calls}

