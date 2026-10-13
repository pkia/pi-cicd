"""Tests for journal-receipt — the durable slice and the shutdown verdict.

Hermetic: journalctl is a recording runner answering with recorded
`--list-boots` output and recorded boot tails, the boot id is a file, and
the durable store is a tmp dir. Nothing here reads this box's journal, and
nothing here can act on the system: the last test asserts the tool's own
source carries no action verb and that every command it runs is
`journalctl` reading.
"""
import importlib.machinery
import importlib.util
import json
import os
import time
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "journal-receipt"

spec = importlib.util.spec_from_loader(
    "journal_receipt",
    importlib.machinery.SourceFileLoader("journal_receipt", str(SCRIPT)),
)
jr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jr)

CUR_BOOT = "a" * 32
PREV_BOOT = "b" * 32

BOOTS_TWO = (
    "IDX BOOT ID                          FIRST ENTRY                 LAST ENTRY\n"
    f" -1 {PREV_BOOT} Mon 2026-10-12 03:00:00 IST Tue 2026-10-12 04:14:00 IST\n"
    f"  0 {CUR_BOOT} Mon 2026-10-12 23:55:29 IST Tue 2026-10-13 05:31:29 IST\n"
)
BOOTS_ONE = (
    "IDX BOOT ID                          FIRST ENTRY                 LAST ENTRY\n"
    f"  0 {CUR_BOOT} Mon 2026-10-12 23:55:29 IST Tue 2026-10-13 05:31:29 IST\n"
)
TAIL_CLEAN = """2026-10-12T04:13:40+01:00 pi systemd[1]: Stopping User Manager for UID 1000...
2026-10-12T04:13:58+01:00 pi systemd[1]: Reached target Shutdown.
2026-10-12T04:13:59+01:00 pi systemd-shutdown[1]: Sending SIGTERM to remaining processes...
"""
TAIL_UNCLEAN = """2026-10-12T04:10:01+01:00 pi kernel: out of memory: killed process 1234 (node)
2026-10-12T04:13:29+01:00 pi kernel: raspberrypi-firmware soc:firmware: watchdog did not stop!
"""
SLICE = """2026-10-12T04:09:00+01:00 pi kernel: Out of memory: Killed process 1234
2026-10-12T04:12:00+01:00 pi systemd[1]: watchdog: service hung, killing
"""


class Runner:
    """Records every argv; answers journalctl from the recorded fixtures."""

    def __init__(self, boots=BOOTS_TWO, tails=None, boots_rc=0, tail_rc=0):
        self.calls = []
        self.boots = boots
        self.tails = tails or {}
        self.boots_rc = boots_rc
        self.tail_rc = tail_rc

    def __call__(self, argv, timeout=60):
        self.calls.append(list(argv))
        assert argv and argv[0] == "journalctl", f"not a journal read: {argv}"
        if "--list-boots" in argv:
            return self.boots_rc, self.boots
        idx = argv[argv.index("-b") + 1]
        return self.tail_rc, self.tails.get(idx, "")


def env(monkeypatch, tmp_path, boot_id=CUR_BOOT, keep=None):
    store = tmp_path / "receipts"
    bootfile = tmp_path / "boot_id"
    bootfile.write_text((boot_id or "") + "\n")
    monkeypatch.setenv("JOURNAL_RECEIPT_DIR", str(store))
    monkeypatch.setenv("JOURNAL_RECEIPT_BOOT_ID_FILE", str(bootfile))
    if keep is not None:
        monkeypatch.setenv("JOURNAL_RECEIPT_KEEP", str(keep))
    return store


def run(argv):
    try:
        return jr.main(argv)
    except SystemExit as e:      # argparse
        return e.code


def test_record_writes_a_durable_filtered_slice(monkeypatch, tmp_path, capsys):
    store = env(monkeypatch, tmp_path)
    runner = Runner(tails={"-b": ""})
    runner.tails = {}
    monkeypatch.setattr(jr, "run_command",
                        lambda argv, timeout=60: (0, SLICE) if "-p" in argv
                        else runner(argv, timeout))
    assert run(["record"]) == 0
    out = capsys.readouterr().out
    path = store / f"{CUR_BOOT}.log"
    assert path.is_file(), out
    text = path.read_text()
    assert "Out of memory: Killed process 1234" in text
    assert f"boot={CUR_BOOT}" in text and "priority<=warning" in text
    # the read really was filtered and bounded, not the whole journal
    argv = jr.slice_argv("warning", 200)
    assert argv[0] == "journalctl" and "-p" in argv and "warning" in argv
    assert "-n" in argv and "200" in argv


def test_record_replaces_the_same_boot_and_prunes_older_ones(monkeypatch,
                                                            tmp_path):
    store = env(monkeypatch, tmp_path, keep=2)
    monkeypatch.setattr(jr, "run_command", lambda argv, timeout=60: (0, SLICE))
    assert run(["record"]) == 0
    assert run(["record"]) == 0          # same boot: replace, not duplicate
    first = (store / f"{CUR_BOOT}.log").read_text()
    assert first.count("Out of memory: Killed process 1234") == 1
    for i, old in enumerate(["c" * 32, "d" * 32, "e" * 32]):
        p = store / f"{old}.log"
        p.write_text(f"# old receipt {i}\n")
        os.utime(p, (1_000 + i, 1_000 + i))       # oldest first
    assert run(["record"]) == 0
    left = sorted(p.name for p in store.glob("*.log"))
    assert len(left) == 2, left
    assert f"{CUR_BOOT}.log" in left and f"{'e' * 32}.log" in left


def test_check_reports_a_recorded_shutdown(monkeypatch, tmp_path, capsys):
    env(monkeypatch, tmp_path)
    monkeypatch.setattr(jr, "run_command",
                        Runner(tails={"-1": TAIL_CLEAN}))
    assert run(["check"]) == 0
    out = capsys.readouterr().out
    assert "SHUTDOWN RECORDED" in out and PREV_BOOT in out
    assert "Reached target Shutdown." in out


def test_check_names_the_missing_shutdown_and_quotes_the_receipt(
        monkeypatch, tmp_path, capsys):
    """The acceptance: an unclean boot, with the durable slice's lines."""
    store = env(monkeypatch, tmp_path)
    (store).mkdir(parents=True, exist_ok=True)
    (store / f"{PREV_BOOT}.log").write_text(
        f"# journal-receipt boot={PREV_BOOT} priority<=warning recorded_at=x\n"
        + SLICE)
    monkeypatch.setattr(jr, "run_command",
                        Runner(tails={"-1": TAIL_UNCLEAN}))
    assert run(["check"]) == 1
    out = capsys.readouterr().out
    assert "NO SHUTDOWN RECORD" in out and PREV_BOOT in out
    assert "watchdog did not stop!" in out                    # journal tail
    assert "Out of memory: Killed process 1234" in out        # durable slice


def test_check_with_the_slice_disabled_names_the_absent_receipt(
        monkeypatch, tmp_path, capsys):
    env(monkeypatch, tmp_path)
    monkeypatch.setattr(jr, "run_command",
                        Runner(tails={"-1": TAIL_UNCLEAN}))
    assert run(["check"]) == 1
    out = capsys.readouterr().out
    assert "NO SHUTDOWN RECORD" in out
    assert f"durable receipt: none for boot {PREV_BOOT}" in out


def test_check_is_unmeasurable_when_there_is_no_previous_boot(
        monkeypatch, tmp_path, capsys):
    """This box's real state: the journal holds only the running boot."""
    store = env(monkeypatch, tmp_path)
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{PREV_BOOT}.log").write_text(
        f"# journal-receipt boot={PREV_BOOT} priority<=warning recorded_at=x\n"
        + SLICE)
    monkeypatch.setattr(jr, "run_command", Runner(boots=BOOTS_ONE))
    assert run(["check", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert payload["state"] == "unmeasurable" and payload["receipt_path"]
    monkeypatch.setattr(jr, "run_command", Runner(boots=BOOTS_ONE))
    assert run(["check"]) == 2
    text = capsys.readouterr().out
    assert "no previous boot" in text
    assert f"newest durable receipt ({store / (PREV_BOOT + '.log')})" in text
    assert "Out of memory: Killed process 1234" in text


def test_source_carries_no_action_verb_and_only_read_only_journalctl():
    src = SCRIPT.read_text()
    assert "systemctl" not in src
    calls = []
    orig = jr.run_command
    jr.run_command = lambda argv, timeout=60: (calls.append(list(argv)), (0, ""))[1]
    try:
        jr.list_boots()
        jr.slice_argv("warning", 5)
        jr.boot_tail("-1", 5)
        jr.newest_receipt()
    finally:
        jr.run_command = orig
    action = {"restart", "stop", "start", "poweroff", "reboot", "halt",
              "enable", "disable", "kill", "daemon-reload"}
    for argv in calls:
        assert argv[0] == "journalctl"
        assert not (set(argv) & action), argv
        assert "--no-pager" in argv, argv          # read, never a pager
