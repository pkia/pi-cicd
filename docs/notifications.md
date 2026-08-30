# Notifications

How anything on this Pi tells a human something happened: the
self-hosted **ntfy** server is the backbone, with one topic per
producing job and token-authenticated publishing. Nothing ships to a
third-party SaaS, and nothing on the LAN or internet can publish or
subscribe.

## Server

| | |
|---|---|
| Package | `ntfy` 2.11 from Debian (trixie) main — no third-party apt repo |
| Service | `systemctl status ntfy` — systemd unit shipped by the package |
| Config | `/etc/ntfy/server.yml` (provisioned on the host, not committed) |
| Listen | **tailscale IP only** (`100.122.94.33:6839`) — 6839 = NTFY on a phone keypad. ntfy 2.11 accepts exactly one `listen-http` address; binding the tailnet IP keeps it off the LAN and the internet |
| Auth | `auth-default-access: deny-all` — every request (publish or subscribe) must carry a token |
| User db | `/var/lib/ntfy/user.db` (SQLite, created on first start) |

Reaching it:

- From the tailnet (phone, laptop): `http://
- From the Pi itself: `http://100.122.94.33:6839`

Why deny-all + tokens instead of an open-but-obscure server: the topic
name is the only "secret" in open ntfy deployments, and topic names leak
(client caches, server logs, typos). Auth makes a leaked topic name
worthless, and lets publishing and subscribing be granted separately.

## Users and tokens

Two ntfy users, each with a token (tokens are how scripts and the
phone authenticate; the creation passwords are thrown away — reset with
`sudo ntfy user change-pass` if ever needed). Admin actions happen via
`sudo ntfy ...` on the Pi itself; there is deliberately no admin user:

| User | Role | Access | Used by |
|---|---|---|---|
| `publisher` | user | write-only grant on `radar`, `loop-heartbeat`, `backups`, `releases`, `services` | scripts on the Pi: loop-heartbeat, radar pings, backup jobs, release-watch, service-probe |
| `subscriber` | user | read-only grant on those topics | the owner's phone |

Tokens live in `/etc/loop-heartbeat.conf` (NTFY_TOKEN) and
`/etc/ntfy-notify.conf` — both outside the repo, like every other
secret on this machine.

## Topics

One topic per producing job. Subscribe from the phone app to exactly
what you want to hear about:

| Topic | Who publishes | What lands there |
|---|---|---|
| `radar` | the radar implementer (via `ntfy-notify`) | shipped/blocked outcomes of self-improvement runs |
| `loop-heartbeat` | loop-heartbeat (built in) | dead-man's-switch alerts: silent jobs, failure streaks, zombies, down services, stale timers — plus recovery notices |
| `backups` | pi-backup (built in) | daily backup results, prune notices, weekly restore-drill PASS/FAIL, failures at high priority |
| `releases` | release-watch (built in) | upstream release digests for the deployed software (ntfy, AdGuardHome, ais-catcher) and watched status pages — one notification per sweep, only when something moved |
| `services` | service-probe (built in) | service DOWN alerts (after 2 consecutive failed probes) and recovery notices for the dashboards, portal, funnel endpoints, ntfy itself and AdGuardHome DNS; per-probe status also lands in `~/.local/state/service-probe/status.json` for the portal |

Naming rule for new topics: the topic is named after the job that owns
it, not the content it carries — `x-writer` publishes to `x-writer`,
not to "tweets". One owner per topic keeps subscriptions meaningful and
ACLs trivial.

## Publishing from scripts

`ntfy-notify` (this repo, linked into `~/.local/bin`) is the single
sanctioned way:

    ntfy-notify -t radar -T "radar shipped" --tag rocket "ntfy backbone done"

- Reads `/etc/ntfy-notify.conf` (NTFY_URL, NTFY_TOKEN, NTFY_TOPIC
  default).
- Exit 0 on a 2xx publish, 1 otherwise — best-effort by design; callers
  log the failure and continue (a dead notification path must never
  take the job's own success path down with it).
- `--dry-run` prints the payload without touching the network.

For Python code already holding config (loop-heartbeat), publish
directly via `ntfy_post()` in `loop-heartbeat` — same JSON shape, same
auth — instead of shelling out.

## Alert-storm kill switch (global mute)

One file mutes **every** publisher on this box — loop-heartbeat,
release-watch, service-probe, pi-backup, ntfy-notify itself — because
they all publish through the shared `ntfy_lib.py` (added 2026-08-29):

    ntfy-notify --mute "ais-catcher storm, investigating"   # mute
    ntfy-notify --mute-status                               # 0 muted / 1 not
    ntfy-notify --unmute                                    # back to normal

Semantics, deliberately:

- **Mute file** is `/home/ev/.local/state/ntfy/mute` (absolute, not
  `~`-relative, because pi-backup runs as root and `~` would resolve to
  `/root`). First line is the free-text reason. Override per-process
  with `NTFY_MUTE_FILE` (drills, tests).
- **Muted publishes count as delivered** (exit 0 / True), so a muted
  job never looks failed and never blocks on the notification path —
  the storm stops, the jobs keep flowing. The suppression is logged to
  stderr (`ntfy: muted (reason) — publish suppressed`) and the CLI
  says `🔇 suppressed`, not `✓ published`.
- **Fails open**: an unreadable/corrupt mute path counts as NOT muted.
  A permissions hiccup must never silence the box by accident.
- **Not forever-silent**: pi-doctor's daily 07:00 audit reports a
  standing mute as a finding (`ntfy:muted — ... unmute with:
  ntfy-notify --unmute`), so a forgotten kill switch resurfaces within
  a day. It reports; it never unmutes on its own.
- Timeouts: every publish through `ntfy_lib` carries a finite timeout
  (15 s default, sanitised if a caller passes None/0/negative), so a
  dead ntfy server can delay a job by seconds, never hang it.

## loop-heartbeat wiring

`/etc/loop-heartbeat.conf` gains:

    NTFY_URL=http://100.122.94.33:6839
    NTFY_TOPIC=loop-heartbeat
    NTFY_TOKEN=<publisher token>

With those set, every loop-heartbeat alert (and recovery notice) goes
to **both** `hermes send` (WhatsApp) and the ntfy topic; an alert counts
as delivered when either channel accepts it. Without them, behaviour is
unchanged from before ntfy existed.

## Ops runbook

    systemctl status ntfy                 # up?
    curl -s http://100.122.94.33:6839/v1/health   # {"code":"healthy"}? (200)
    sudo ntfy user list --config /etc/ntfy/server.yml
    sudo ntfy access list --config /etc/ntfy/server.yml

Live end-to-end check (auth posture + publish→subscribe round trip,
exits non-zero on failure):

    sudo python3 docs/e2e-ntfy-check.py

Rotate/grow:

    sudo NTFY_PASSWORD=... ntfy user add --role=user publisher --config ...
    sudo ntfy token add publisher --config ...   # mint, then update /etc/*.conf

Grant for the `releases` topic (release-watch — see the script
docstring; inspect live state with `release-watch --list`):

    sudo ntfy access publisher releases write-only
    sudo ntfy access subscriber releases read-only

Grant for the `services` topic (service-probe — see the script
docstring; inspect live state with `service-probe --list`):

    sudo ntfy access publisher services write-only
    sudo ntfy access subscriber services read-only

Grant for the `chaos` topic (chaos-drill — see the script docstring;
inspect live state with `chaos-drill --list`):

    sudo ntfy access publisher chaos write-only
    sudo ntfy access subscriber chaos read-only

The `chaos` topic is deliberately in the drill's own blast radius: the
ntfy fail-closed drill publishes its probe messages there, and a drill
receipt lands there every night — so a broken ACL on the notification
backbone is caught by the drills themselves, not by silence.

The server survives reboots (systemd `WantedBy=multi-user.target`,
`Restart=on-failure`). Restarting ntfy does not touch any other service;
messages published while it is down are simply not delivered (no
persistent queue by design — alerts are deduped and re-fired by
loop-heartbeat every RENOTIFY_MIN anyway, so a transient ntfy outage
degrades to WhatsApp-only, not silence).
