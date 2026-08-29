"""ntfy_lib — the one publish path every ntfy publisher on this box uses.

Two guarantees the tools inherit for free (board idea "alert-storm kill
switch and notifier timeouts", 2026-08-29):

1. **Global mute.** When the mute file exists, publishes are suppressed
   WITHOUT touching the network and reported as delivered, so jobs keep
   flowing while the owner silences a storm. Fails open: an unreadable
   mute path must never silence the box.

2. **Finite timeout on every request.** Whatever a caller passes (or
   forgets), the request that reaches urlopen always carries a sane
   positive timeout — a dead notification server can delay a job by at
   most that many seconds, never hang it forever.

Everything is stdlib-only, like the rest of pi-cicd. `_urlopen` is
injectable so tests never touch the network.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

#: Seconds a publish may take at most, used whenever the caller's value
#: is missing or not a sane positive number.
DEFAULT_TIMEOUT = 15

#: Global kill switch: one file mutes every publisher on the box. Fixed
#: absolute path (not ~-relative) because some tools run as root, where
#: ~ resolves to /root — see LESSONS 2026-08-26. Override per-process
#: with $NTFY_MUTE_FILE (tests, drills).
DEFAULT_MUTE_FILE = os.environ.get(
    "NTFY_MUTE_FILE", "/home/ev/.local/state/ntfy/mute")


def muted(mute_file=None):
    """(True, reason) when the mute file exists; (False, '') otherwise.

    The reason is the file's first line (free text, set by
    `ntfy-notify mute "reason"`); an empty file mutes with a generic
    reason. Any read failure counts as NOT muted — the kill switch must
    fail open, never silently swallow alerts because of a permission
    hiccup.
    """
    if mute_file is None:  # resolve at call time, so tests can patch
        mute_file = DEFAULT_MUTE_FILE
    try:
        first = Path(mute_file).read_text(encoding="utf-8").splitlines()
        reason = first[0].strip() if first else ""
    except OSError:
        return False, ""
    return True, (reason or "no reason given")


def publish(url, headers, payload, timeout=None,
            mute_file=None, _urlopen=urllib.request.urlopen):
    """POST one JSON payload to the ntfy root endpoint; True on any 2xx.

    Muted counts as delivered (True). Never raises: connection and HTTP
    errors are reported on stderr and returned as False, so a broken
    notification backbone degrades a job, never kills it.
    """
    if not url:
        print("ntfy: no server URL configured", file=sys.stderr)
        return False
    is_muted, reason = muted(mute_file)
    if is_muted:
        print(f"ntfy: muted ({reason}) — publish suppressed",
              file=sys.stderr)
        return True
    if not timeout or timeout <= 0:  # None, 0, negative: never hang
        timeout = DEFAULT_TIMEOUT
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST")
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.getcode() < 300
    except urllib.error.HTTPError as exc:
        try:  # fp can be None when raised by hand in tests
            body = exc.read(200).decode("utf-8", "replace")
        except Exception:
            body = ""
        print(f"ntfy: HTTP {exc.code}: {body}", file=sys.stderr)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"ntfy: publish failed: {exc}", file=sys.stderr)
    return False
