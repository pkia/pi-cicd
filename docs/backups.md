# Backups

Deduplicated, encrypted backups of the host state git does not cover,
plus a **scheduled restore drill** — a backup that has never been
restored is a rumour, so the drill is what makes this a backup rather
than a hope.

| | |
|---|---|
| Tool | `pi-backup` (this repo) — thin stdlib-Python wrapper around **borg 1.4** (Debian package, no containers) |
| Repo | path from `/etc/pi-backup.conf`; currently on the SD card (`/var/backups/pi-borg`), moveable to USB with one config line |
| Encryption | `repokey` — the key lives inside the repo; passphrase + repo copy restores anywhere |
| Schedule | `pi-backup.timer` daily 03:30 (create + prune), `pi-backup-drill.timer` Sundays 05:30 (backup + restore + byte-compare) |
| Alerts | every outcome → the ntfy `backups` topic (failures at high priority); silent channel otherwise |
| Config | `/etc/pi-backup.conf` — root-readable only, never committed |

## What gets backed up

Everything under `/etc` that this machine's services need but git
cannot hold: the ntfy server config + user db, the loop's conf files
(`loop-heartbeat`, `ntfy-notify`, `pi-backup` itself), and the custom
systemd units. Application code, sites and dashboards all live in git
repos with their own CI and are deliberately excluded.

BACKUP_PATHS in the config is the single source of truth; `pi-backup
list` shows what archives exist, `pi-backup restore ARCHIVE` extracts
one.

## The restore drill

`pi-backup drill` does the full loop weekly and on demand:

1. create a fresh archive,
2. extract it into a temp dir,
3. byte-compare a sample of restored files (sha256) against the
   live sources — restored file count, compared count, any missing or
   mismatched files are reported,
4. publish PASS/FAIL to the ntfy `backups` topic.

The comparison maps each restored path back to its absolute source
(borg archives absolute paths without the leading `/`), so a PASS
means: these bytes, restored today, are identical to what the services
are running on. Scheduled by `pi-backup-drill.timer`; the first drill
also ran live during the 2026-08-25 setup (see IDEAS.md Done).

## Recovering

```sh
pi-backup list                                  # pick an archive
sudo pi-backup restore pi-2026-08-25T033000     # extracts to ./restore
sudo pi-backup restore pi-... /etc/ntfy         # just one subtree
sudo pi-backup check                            # repo integrity check
```

Extracted paths mirror their absolute source layout
(`restore/etc/ntfy/...`) — copy back deliberately, never blind.

## Moving to USB storage later

borg repos are self-contained directories: mount the USB stick, stop
the timers (`sudo systemctl stop pi-backup.timer pi-backup-drill.timer`),
`sudo cp -a` the repo directory across, point `REPO=` at the new mount,
re-run `pi-backup check`, start the timers. (Or start fresh on the
stick and keep the SD-copy as a second archive; borg's own
`borg transfer` also migrates repositories.)
