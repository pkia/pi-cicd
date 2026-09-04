# Unit index — every running unit, its config, timer and topic

The single map of the live system: every scheduled unit on the Pi that
this repo owns or drives, one row each, with where its configuration
lives, what state it keeps, and which ntfy topic it publishes to. Keep
this table in sync with reality — `tests/test_units_doc.py` fails if a
unit disappears from the index or a unit file in `systemd/` is not
indexed here.

Legend: **systemd** = timer + oneshot service (unit files in `systemd/`
unless noted); **Hermes cron** = scheduled job defined outside this
repo; **helper** = on-demand command, no unit. Topics are ntfy topics
on the self-hosted server, except *(hermes)* which alerts over the
messaging platform via `hermes send`.

| Unit | Kind | Schedule | Config | State | Topic | Verify |
|---|---|---|---|---|---|---|
| project-guard | systemd | every 10 min | — (scans `$HOME`, `$HOME/apps/*`) | `~/.local/state/project-guard.log` | — (push-only, silent) | `systemctl status project-guard.timer` |
| deploy.sh (per service) | systemd | every 3 min | unit from `templates/deploy.timer`; marker `.deployed_commit` in service repo | `.deployed_commit` / `.deploy_failed` | — | `systemctl list-timers '*deploy*'` |
| pipeline-check | Hermes cron (no-agent) | 01:00 & 13:00 (cron `0 1,13 * * *`) | Hermes job `pipeline-check-wrapper.sh` (execs repo tool by absolute path) | — (prints alerts) | — *(hermes)* | `hermes cron list` |
| pi-doctor | Hermes cron (agent) | daily 06:30 (cron `30 6 * * *`) | Hermes job `pi-doctor daily audit` | `pi-doctor-state.json` (untracked) | — *(hermes, deduped)* | `pi-doctor --verbose` |
| loop-heartbeat | systemd | every 30 min | `/etc/loop-heartbeat.conf` | `~/.local/state/loop-heartbeat/` | `loop-heartbeat` (+ WhatsApp) | `loop-heartbeat --dry-run -v` |
| ntfy-notify | helper | on demand | `/etc/ntfy-notify.conf` | — | per-job topic argument | `ntfy-notify -t radar -T test hi` |
| ntfy server | systemd (Debian package) | always on | `/etc/ntfy/server.yml`, `/etc/ntfy/tokens/` | `~/.local/state/ntfy/` | all topics | `systemctl status ntfy` |
| pi-backup | systemd | daily 03:30 | `/etc/pi-backup.conf` (600, root) | borg repo `/var/backups/pi-borg` | `backups` | `systemctl list-timers pi-backup*` |
| pi-backup-drill | systemd | Sun 05:30 | same | same | `backups` | `journalctl -u pi-backup-drill` |
| release-watch | systemd | 10:12 / 22:12 | sources in code | `~/.local/state/release-watch/state.json` | `releases` | `release-watch --list` |
| service-probe | systemd | every 5 min | probe list in code (chaos-drill drives a shadow copy) | `~/.local/state/service-probe/status.json` | `services` | `service-probe --list` |
| chaos-drill | systemd | nightly 04:45 | drill manifest in code | `~/.local/state/chaos-drill/status.json` | `chaos` | `chaos-drill --list` |

## Notes

- **Portal panels** (project-hub) render from the state files above:
  `/api/probes` → Uptime Scoreboard, `/api/chaos` → Chaos Drills panel.
- **Deploy layer**: every service repo carries its own
  `deploy/deploy.sh` (from `templates/`) plus a `*-deploy.timer`. Live
  services today: maritime-dashboard, project-hub, sat-audio, shelfmate,
  book-app, kiosk-home, cs2-dashboard, cs2-tracker.
- **Mute**: every ntfy publisher routes through `ntfy_lib.py` — the
  global kill switch (`ntfy-notify --mute REASON`) suppresses all topics
  above at once; see [notifications.md](notifications.md).
- One page per operational layer: [layers.md](layers.md). The incidents
  that shaped the rules: [architecture.md](architecture.md).
