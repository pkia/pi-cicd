#!/usr/bin/env python3
"""Live end-to-end check of release-watch on the ntfy backbone (root on Pi).

Verifies what the unit tests cannot: that the release-watch digest
really lands on the `releases` topic and reads back with the phone's
(subscriber) credentials. Two modes:

    sudo python3 docs/e2e-release-watch-check.py            # live check
    sudo python3 docs/e2e-release-watch-check.py --drill     # seeded drill

Live check (5 assertions): ACL posture for the topic, config sanity,
publish->read-back of a probe digest, and that the two-sided watcher
state matches what the state file says.

Drill mode proves the whole path with a seeded change: it snapshots the
real state file, rewrites one source's remembered version to a fake
'v0.0.1-drill', runs release-watch for real (which must publish a
digest), reads that digest back via the SUBSCRIBER token (the phone's
view), then restores the snapshot. No secret is printed. Exits non-zero
on any failure. Companion to docs/notifications.md.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://100.122.94.33:6839"
CONF = "/etc/release-watch.conf"
STATE = Path("/home/ev/.local/state/release-watch/state.json")
BIN = "/home/ev/.local/bin/release-watch"

fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def req(method, path, token=None, data=None, timeout=8):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(data).encode() if data else None, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode()[:4000]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except urllib.error.URLError as e:
        return None, str(e)


def read_topic(token, since="10m"):
    code, body = req("GET", f"/releases/json?poll=1&since={since}",
                     token=token)
    msgs = []
    if code == 200:
        for line in body.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("event") in (None, "message"):
                msgs.append(obj)
    return code, msgs


def main():
    drill = "--drill" in sys.argv[1:]
    print(f"release-watch live check ({'DRILL' if drill else 'basic'}) "
          f"against {BASE}")

    sub = Path("/etc/ntfy/tokens/subscriber.txt").read_text().strip()
    pub = subprocess.run(
        ["grep", "-oE", r"tk_[A-Za-z0-9]+", CONF],
        capture_output=True, text=True).stdout.strip()

    # 1. ACL posture: anonymous denied on the topic, publisher can write,
    #    subscriber can read.
    code, _ = req("GET", "/releases/json?poll=1")
    check("anonymous read denied", code == 403, f"got {code}")
    code, _ = req("POST", "/releases", token=pub,
                  data={"message": "probe"})
    check("publisher write allowed", code == 200, f"got {code}")
    code, msgs = read_topic(sub, since="5m")
    check("subscriber read allowed", code == 200, f"got {code}")

    # 2. Config sanity.
    conf_text = Path(CONF).read_text()
    check("config names the releases topic",
          "NTFY_TOPIC=releases" in conf_text)

    if not drill:
        # 3. The probe publish above is readable by the phone account.
        check("probe digest read back",
              any(m.get("message") == "probe" for m in msgs),
              f"{len(msgs)} msgs")
        print(f"\n{'FAIL' if fails else 'PASS'}: "
              f"{len(fails)} failure(s)" if fails else
              f"\nall checks passed")
        return 1 if fails else 0

    # ---- drill: seeded change must publish and be readable -------------
    if not STATE.exists():
        print(f"FAIL: no state at {STATE} — run release-watch once first")
        return 1
    snapshot = json.loads(STATE.read_text())
    key = "github:jvde-github/AIS-catcher"
    if key not in snapshot["sources"]:
        print(f"FAIL: {key} not in state")
        return 1

    bak = Path(tempfile.mkdtemp()) / "state.json"
    shutil.copy(STATE, bak)
    st = STATE.stat()
    try:
        # seed: pretend the last-seen version was a fake old one
        seeded = json.loads(json.dumps(snapshot))
        seeded["sources"][key]["version"] = "v0.0.1-drill"
        STATE.write_text(json.dumps(seeded, indent=2, sort_keys=True) + "\n")
        # run as the service user, state pinned: `~` must not resolve to
        # /root just because this check runs as root
        r = subprocess.run(
            ["sudo", "-u", "ev", BIN, "--state", str(STATE), "-v"],
            capture_output=True, text=True)
        out = r.stdout + r.stderr
        check("drill run exit 0", r.returncode == 0, out[-300:])
        check("drill detected the seeded change",
              "v0.0.1-drill" in out and "CHANGE:" in out, out[-300:])
        code, msgs = read_topic(sub, since="5m")
        digest = next((m for m in msgs
                       if "v0.0.1-drill" in m.get("message", "")), None)
        check("digest landed on the releases topic", digest is not None,
              f"{len(msgs)} msgs, last: "
              f"{msgs[-1].get('message', '')[:120] if msgs else 'none'}")
        if digest:
            print(f"  digest title: {digest.get('title')}")
    finally:
        shutil.copy(bak, STATE)  # restore the real baseline
        os.chown(STATE, st.st_uid, st.st_gid)
        shutil.rmtree(bak.parent, ignore_errors=True)

    # after restore, the next sweep must be silent again
    r = subprocess.run(
        ["sudo", "-u", "ev", BIN, "--state", str(STATE)],
        capture_output=True, text=True)
    check("post-drill sweep silent", r.returncode == 0
          and "unchanged" in r.stdout, r.stdout[-200:])

    print(f"\n{'FAIL: ' + ', '.join(fails) if fails else 'all checks passed'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
