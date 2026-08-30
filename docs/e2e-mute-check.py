#!/usr/bin/env python3
"""Live end-to-end check of the alert-storm kill switch (run as root).

Verifies what the unit tests cannot: that the REAL mute file silences a
REAL publish through the REAL ntfy server, that the subscriber sees
nothing while muted and the message again after --unmute, and that
pi-doctor's standing-mute finding fires. Run after changing ntfy_lib,
the mute path, or the CLI:

    sudo python3 docs/e2e-mute-check.py

Exits non-zero on any failure. No secrets are printed. The drill leaves
the box UNMUTED (try/finally); if it dies mid-run, ntfy-notify --unmute
or the next pi-doctor audit (ntfy:muted finding) resurfaces it.
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ntfy_lib  # noqa: E402

MUTE = ntfy_lib.DEFAULT_MUTE_FILE
NN = str(Path(__file__).resolve().parent.parent / "ntfy-notify")
BASE = "http://100.122.94.33:6839"


def sh(args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def read_radar(sub_token, since="3m"):
    """Messages on the radar topic since window, via subscriber token."""
    req = urllib.request.Request(
        f"{BASE}/radar/json?poll=1&since={since}",
        headers={"Authorization": f"Bearer {sub_token}"})
    with urllib.request.urlopen(req, timeout=8) as r:
        msgs = []
        for line in r.read().decode().splitlines():  # JSONL stream
            if '"event":"message"' in line:
                msgs.append(line)
        return msgs


def main():
    checks = []

    def check(name, ok):
        checks.append((name, bool(ok)))
        print(f"{'✓' if ok else '✗'} {name}")

    sub = Path("/etc/ntfy/tokens/subscriber.txt").read_text().strip()
    if not sub:
        sys.exit("subscriber token missing (/etc/ntfy/tokens/subscriber.txt)")

    rc, out, _ = sh([NN, "--mute-status"])
    if rc == 0:
        sys.exit(f"box is already muted ({out}) — unmute before drilling")

    marker = f"mute-drill-{int(time.time())}"
    pre_existing = Path(MUTE).exists()
    try:
        # 0. run as root, the mute dir may be created root-owned — which
        # would lock the owner (ev) out of their own kill switch. Chown
        # it to ev so --mute works for the human too.
        d = Path(MUTE).parent
        if d.exists():
            subprocess.run(["chown", "ev:ev", str(d)], check=False)
        # 1. mute with a reason
        rc, out, _ = sh([NN, "--mute", f"{marker} e2e drill"])
        check("mute: file created, reason recorded",
              rc == 0 and marker in out)
        check("mute: ntfy_lib sees it",
              ntfy_lib.muted() == (True, f"{marker} e2e drill"))

        # 2. a publish while muted: exit 0, honest output, nothing sent
        rc, out, err = sh([NN, "-t", "radar", "-T", "mute drill", marker])
        check("muted publish: exit 0 and says suppressed",
              rc == 0 and "suppressed" in out)

        # 3. the subscriber really got nothing about the marker
        time.sleep(2)  # allow any (wrong) delivery to land
        lines = read_radar(sub)
        check("muted publish: nothing reached the topic",
              not any(marker in ln for ln in lines))

        # 4. pi-doctor's standing-mute finding fires on the real path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import importlib.util
        import importlib.machinery
        spec = importlib.util.spec_from_loader(
            "pi_doctor",
            importlib.machinery.SourceFileLoader(
                "pi_doctor", str(Path(__file__).resolve().parent.parent
                                 / "pi-doctor")))
        doc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doc)
        f, _ = doc.check_mute()
        check("pi-doctor reports the standing mute",
              f and "ntfy:muted" in f[0] and "unmute" in f[0])
    finally:
        rc, out, _ = sh([NN, "--unmute"])
        check("unmute: file removed, publishers deliver again",
              rc == 0 and not Path(MUTE).exists())

    # 5. after unmute a publish really lands (read back via subscriber)
    rc, out, _ = sh([NN, "-t", "radar", "-T", "mute drill", marker])
    check("unmuted publish: exit 0", rc == 0)
    time.sleep(2)
    lines = read_radar(sub)
    check("unmuted publish: message reached the topic",
          any(marker in ln for ln in lines))

    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed")
    if bad:
        sys.exit("FAILED: " + "; ".join(bad))
    if not pre_existing:
        print("note: box left unmuted; mute file never pre-existed")


if __name__ == "__main__":
    main()
