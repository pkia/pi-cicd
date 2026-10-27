#!/usr/bin/env python3
"""Live end-to-end check of the ntfy backbone (run as root on the Pi).

Verifies what the unit tests cannot: the real server's auth posture and
a full publish->subscribe round trip. Run after installing/upgrading
ntfy or changing /etc/ntfy/server.yml:

    sudo python3 docs/e2e-ntfy-check.py

Exits non-zero on any failure. No secrets are printed. Companion to
docs/notifications.md.
"""
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request


def _base_from_conf(default="http://100.122.94.33:6839"):
    """NTFY_URL from /etc/ntfy-notify.conf — the same source every other
    tool here uses, so this check follows the host instead of a hardcoded
    address (the backbone IP changed when the lab moved to a new Pi)."""
    try:
        with open("/etc/ntfy-notify.conf", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("NTFY_URL="):
                    return line.split("=", 1)[1].strip() or default
    except OSError:
        pass
    return default


BASE = _base_from_conf()


def _magicdns_name():
    """The tailnet DNS name of this host, from the backbone IP itself."""
    ip = BASE.split("//", 1)[-1].split(":", 1)[0]
    try:
        name = socket.gethostbyaddr(ip)[0].rstrip(".")
        return f"http://{name}:{BASE.rsplit(':', 1)[1]}"
    except (OSError, IndexError):
        return None


DNS = _magicdns_name()


def req(method, path, token=None, data=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, method=method,
                               data=json.dumps(data).encode() if data else None,
                               headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=8) as resp:
            return resp.getcode(), resp.read().decode()[:4000]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except urllib.error.URLError as e:
        return None, str(e)


def stream_msgs(code, body):
    """NDJSON body -> list of message event dicts."""
    if code != 200:
        return []
    out = []
    for line in body.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("event") in (None, "message"):
            out.append(obj)
    return out


def main():
    pub = subprocess.run(
        ["grep", "-oE", r"tk_[A-Za-z0-9]+", "/etc/ntfy-notify.conf"],
        capture_output=True, text=True).stdout.strip()
    sub = open("/etc/ntfy/tokens/subscriber.txt").read().strip()
    if not (pub and sub):
        sys.exit("tokens missing from /etc configs — run provisioning first")

    checks = []
    code, body = req("GET", "/v1/health")
    checks.append(("health (tailnet IP)", code == 200 and "healthy" in body))
    if DNS:
        try:
            with urllib.request.urlopen(DNS + "/v1/health", timeout=8) as resp:
                checks.append((f"health (MagicDNS name: {DNS.split('//')[-1].split(':')[0]})",
                               resp.getcode() == 200))
        except (urllib.error.URLError, OSError):
            checks.append(("health (MagicDNS name)", False))
    else:
        checks.append(("health (MagicDNS name: unresolved)", False))

    code, _ = req("POST", "/", data={"topic": "radar", "message": "anon"})
    checks.append(("anonymous publish denied (403)", code == 403))
    code, _ = req("GET", "/radar/json?poll=1")
    checks.append(("anonymous subscribe denied (403)", code == 403))
    code, _ = req("GET", "/radar/json?poll=1", token=pub)
    checks.append(("publisher token cannot read (403)", code == 403))

    p = subprocess.run(["sudo", "-u", "ev", "/home/ev/.local/bin/ntfy-notify",
                        "-t", "radar", "-T", "e2e", "live backbone check"],
                       capture_output=True, text=True)
    checks.append(("ntfy-notify publishes", p.returncode == 0))

    code, body = req("GET", "/radar/json?poll=1&since=10m", token=sub)
    msgs = stream_msgs(code, body)
    checks.append(("subscriber reads it back",
                   any("live backbone check" in m.get("message", "") for m in msgs)))

    fail = 0
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        fail += not ok
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
