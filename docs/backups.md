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

Everything this machine's services need but git cannot hold: the ntfy
server config (`/etc/ntfy`) plus its **user db** at
`/var/lib/ntfy/user.db` — the users, ACL grants and tokens the whole
notification backbone is authenticated against, which `auth-file` in
`server.yml` points at, so it is the path that matters, not the one next
to the config — the loop's conf files (`loop-heartbeat`, `ntfy-notify`,
`pi-backup` itself), the custom systemd units, and the agent's state
below. Application code, sites and dashboards all live in git repos with
their own CI and are deliberately excluded.

Checked 2026-10-01 during a pi-doctor pass: `/etc/ntfy` held only config
and tokens (no `user.db`), and nothing covered `/var/lib/ntfy` — the
backup was one directory short of the auth db it claimed to carry.

BACKUP_PATHS in the config is the single source of truth; `pi-backup
list` shows what archives exist, `pi-backup restore ARCHIVE` extracts
one.

The agent's own state rides along (`~/.hermes`: the state db, auth and
config files, profiles, cron and skills) — it is host state too, and
nothing else backs it up.

## Live databases

Some of that state is **being written while the backup runs**: the
agent's `state.db`, the ntfy user db. Borg fails on a file that changes
under it (`file changed while we backed it up`) and a database copied
mid-commit may be torn, so `pi-backup` snapshots first: every file
carrying the SQLite header — a `BACKUP_PATHS` file entry or a match
inside a directory entry — goes through sqlite3's **backup API**, which
reads a transactionally consistent database even while another process
commits. The dump is archived under `SNAPSHOT_DIR` (default
`/var/backups/pi-sqlite`) mirroring the source path, and the live file
is excluded from the archive, so a restore of it lands at
`restore/var/backups/pi-sqlite/home/ev/.hermes/state.db` — unambiguous,
not mixed in with a torn copy. A database that cannot be dumped (torn
file, unreadable) fails the run loudly instead of archiving something
unusable.

Seen 2026-09-30 and 2026-10-01: two nightly runs in a row failed on
exactly this, with the archive holding a mid-write `state.db`.

## Volatile files

Atomic writers leave artefacts that exist in the scan and are gone by
the time borg reads them — borg fails the run (`stat: ... No such file
or directory`), and it has no flag to tolerate that. `EXCLUDE` in the
config therefore carries the classes rather than individual names:

```
EXCLUDE=*.db-wal *.db-shm *.db-journal *.tmp *.lock *.ready
```

The SQLite side files are also why the dumps matter: with `-wal` and
`-shm` left out, an archived `state.db` would be incomplete anyway.
Seen 2026-10-01: a run that had already survived `state.db` churn died
on `.hermes/cron/.jobs_*.tmp`, a cron bookkeeping temp file.

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
sudo pi-backup verify                           # rehearse the newest archive's DB snapshot
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
