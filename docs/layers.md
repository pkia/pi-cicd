# Layers — one page per operational layer

The unit index ([units.md](units.md)) maps every running unit to its
schedule, config, state and topic; this document explains each layer's
job and how to work with it. Verify commands live in the index. Design
reasoning and the incidents that produced the rules are in
[architecture.md](architecture.md).

## Deploy — pull-based CD with rollback

Every service deploys itself; there are no self-hosted runners (on a
public repo a fork PR is attacker-controlled workflow input). Each
service repo carries `deploy/deploy.sh` (generated from
`templates/deploy.sh`) and a `*-deploy.timer` firing every 3 minutes.
Contract: fetch `origin/main`, fast-forward only on a clean tree (never
merge/reset dirty); when `.deployed_commit` differs from HEAD, gate on
byte-compile + import with the project venv *before* any restart;
restart, health-check ≤ 30 s; on failure roll back to the previous
commit and record `.deploy_failed` so a bad commit is never retried
until something newer lands. A deploy is idempotent.

## project-guard — the "no work is lost" engine

Oneshot every 10 minutes. For each code directory under `$HOME` (and
`$HOME/apps/*`): adopt unversioned projects (private GitHub repo,
hygiene `.gitignore` starting from files over 20 MB, standard CI, hard
50 MB gate on staged files, secrets gate on changed paths); push
unpushed commits under their own branch name; snapshot dirty trees to
an `autosave` branch via a private git index — the working tree, real
index and HEAD are never touched. Additive only: it pushes, never
pulls, merges or resets. Log: `~/.local/state/project-guard.log`.

## pipeline-check — the compliance layer

Hourly Hermes cron job (no-agent). For every project: is it versioned,
does it have a remote, are there unpushed commits, is CI on its newest
commit green — and for services, does the deploy marker match HEAD and
is the deploy timer active. Docs-only repos are exempt until code
arrives. Silent when green; prints alert lines only for problems.
Since the self-healing edition (commit `60044c8`) it also fixes what it
can safely fix on its own; what it cannot, it escalates.

## pi-doctor — the daily deep audit

Daily 07:00 Hermes cron job (agent-driven, so findings get investigated
with the owner's standing approval). Per project: service active and
not flapping, healthz answers, running commit matches `origin/main`
(a drift means the deploy timer itself is broken — re-runs deploy.sh
as the fix), CI green, tree clean. System layer: disk, CPU temp,
memory, failed units, the watchers' own state. Safe fixes are applied
automatically (restart a dead service, re-run a deploy); everything
else escalates. Alert state lives in `pi-doctor-state.json` (untracked)
so a persistent issue re-alerts at most every few hours. All-green runs
are silent.

## loop-heartbeat — dead-man's switch on the loop

Every 30 minutes. Reads the Hermes cron jobs' durable execution history
(`hermes cron list`/`runs`) and alerts on missed schedules, failure
streaks, zombie "running" entries, vanished jobs, plus optional
systemd service/timer staleness. Deduped with recovery notices; silent
when green. Config `/etc/loop-heartbeat.conf` keeps the alert target
and second channel (WhatsApp + ntfy `loop-heartbeat` topic) out of the
public repo. Poll-based by design: it watches the loop from outside.

## Notifications & mute — ntfy backbone and the kill switch

Self-hosted ntfy (Debian package) on the tailnet only (`:6839`),
`auth-default-access: deny-all`, a write-only `publisher` user and a
read-only `subscriber` user, tokens in `/etc/ntfy/tokens/`. One topic
per job (`radar`, `loop-heartbeat`, `backups`, `releases`, `services`,
`chaos`). Scripts publish through the bundled `ntfy-notify` helper
(config `/etc/ntfy-notify.conf`). Every publisher routes through
`ntfy_lib.py`, which sanitises timeouts centrally (None/0/negative →
15 s, so a dead server delays a job by seconds, never hangs it) and
implements the **global mute**: one variable suppresses every topic
without touching the network, counts as delivered so jobs keep
flowing, fails open, and pi-doctor's daily audit reports a standing
mute so the box can't be silenced forever. Runbook:
[notifications.md](notifications.md).

## Backup — pi-backup and the weekly restore drill

Daily 03:30: borg create + prune (7 daily / 4 weekly / 6 monthly) of
the `/etc` state git cannot hold (ntfy server config + user db, loop
configs, units). Passphrase and ntfy keys live in `/etc/pi-backup.conf`
(600, root-only, never committed). Weekly Sunday 05:30 **restore
drill**: extract a fresh archive and byte-compare (sha256, sampled)
against the live sources — PASS/FAIL published to the `backups` topic.
Repo is `/var/backups/pi-borg` (on SD until USB storage is attached;
runbook: [backups.md](backups.md)).

## Release watching — release-watch

Twice daily (10:12 / 22:12). Polls the GitHub releases API of every
piece of software this machine runs (plus optional sha256 page watches
for sources without an API). First observation is a baseline, not an
alert; ONE digest per sweep to the `releases` topic; error-streak
escalation for failing sources. State:
`~/.local/state/release-watch/state.json`.

## Service probing — service-probe

Every 5 minutes. One stdlib probe per long-running service: HTTP for
the seven local services (including cs2-tracker's JSON `healthy` gate),
the three public funnel endpoints, an ntfy self-check, and a
hand-built-UDP DNS query for AdGuardHome (12 probes total). DOWN is
confirmed only after 2 consecutive failures (anti-flap); recovery
notices carry the downtime duration. Alerts to the `services` topic;
atomic state at `~/.local/state/service-probe/status.json`, which the
portal's Uptime Scoreboard renders (`/api/probes`). Inspect live state:
`service-probe --list`.

## Chaos drills — chaos-drill

Nightly 04:45 (clear of the 03:30 backup, the 04:00 implementer and
the 05:30 drill), one drill per night on date-hashed rotation:
(1) **service-probe-dead-port** — drives the REAL service-probe
pipeline through a shadow copy of the probe config until the probe
flips DOWN and back UP, proving the detection chain end to end with
the live scoreboard untouched; (2) **ntfy-auth** — the backbone must
be fail-closed: anonymous publish DENIED, publisher accepted, receipt
read back via the subscriber token; (3) **probe-timer-alive** —
timezone-proof timer liveness. PASS/FAIL receipt to the `chaos` topic
(inheriting the global mute and timeouts); state at
`~/.local/state/chaos-drill/status.json`, rendered by the portal's
Chaos Drills panel (`/api/chaos`). Inspect: `chaos-drill --list`.
