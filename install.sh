#!/usr/bin/env bash
# pi-cicd installer - run from a clone of this repo on the target host.
#
# Makes the tools available on PATH, installs the project-guard systemd
# units, sets a sane git identity (from gh if available), and enables
# the guard timer. Idempotent: safe to re-run.
set -euo pipefail

REPO_DIR=$(cd "$(dirname "$0")" && pwd)
BIN_DIR="$HOME/.local/bin"

[ "$(basename "$REPO_DIR")" = "pi-cicd" ] || {
    echo "expected the clone to be named pi-cicd (got $REPO_DIR)"; exit 1
}

mkdir -p "$BIN_DIR"
ln -sf "$REPO_DIR/project-guard" "$BIN_DIR/project-guard"
ln -sf "$REPO_DIR/new-project"   "$BIN_DIR/new-project"
ln -sf "$REPO_DIR/loop-heartbeat" "$BIN_DIR/loop-heartbeat"
ln -sf "$REPO_DIR/ntfy-notify"   "$BIN_DIR/ntfy-notify"
ln -sf "$REPO_DIR/pi-backup"     "$BIN_DIR/pi-backup"
ln -sf "$REPO_DIR/release-watch" "$BIN_DIR/release-watch"
chmod +x "$REPO_DIR/loop-heartbeat" "$REPO_DIR/ntfy-notify" \
         "$REPO_DIR/pi-backup" "$REPO_DIR/release-watch"
echo "tools linked into $BIN_DIR"

# Sane git defaults (no identity guessing: gh first, then a local fallback).
if gh auth status >/dev/null 2>&1; then
    GH_USER=$(gh api user -q .login)
    git config --global user.name  "$GH_USER"
    git config --global user.email "$GH_USER@users.noreply.github.com"
    gh auth setup-git >/dev/null
    echo "git identity set from gh: $GH_USER"
else
    echo "note: gh not authenticated - new-project needs 'gh auth login'"
    echo "      (guard still adopts and autosaves locally until then)"
fi
git config --global init.defaultBranch main

sudo cp "$REPO_DIR/systemd/project-guard.service" "$REPO_DIR/systemd/project-guard.timer" \
     /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --quiet --now project-guard.timer
echo "project-guard timer installed and active"

# loop-heartbeat: dead-man's switch for the scheduled loop. Needs
# /etc/loop-heartbeat.conf (SEND_TARGET etc.) - see the script's docstring.
# Install is skipped silently when the config is absent.
if [ -f /etc/loop-heartbeat.conf ]; then
    sudo cp "$REPO_DIR/systemd/loop-heartbeat.service" \
            "$REPO_DIR/systemd/loop-heartbeat.timer" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet --now loop-heartbeat.timer
    echo "loop-heartbeat timer installed and active"
else
    echo "note: /etc/loop-heartbeat.conf not found - loop-heartbeat not installed"
fi

# pi-backup: deduplicated borg backups + weekly restore drill. Needs
# /etc/pi-backup.conf (REPO/PASSPHRASE/BACKUP_PATHS, ntfy keys) and the
# borgbackup package — see the script's docstring and docs/backups.md.
# Install is skipped silently when the config is absent.
if [ -f /etc/pi-backup.conf ]; then
    sudo cp "$REPO_DIR/systemd/pi-backup.service" \
            "$REPO_DIR/systemd/pi-backup.timer" \
            "$REPO_DIR/systemd/pi-backup-drill.service" \
            "$REPO_DIR/systemd/pi-backup-drill.timer" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet --now pi-backup.timer
    sudo systemctl enable --quiet --now pi-backup-drill.timer
    echo "pi-backup timers installed and active"
else
    echo "note: /etc/pi-backup.conf not found - pi-backup not installed"
fi

# release-watch: upstream release digests to the ntfy 'releases' topic.
# Needs /etc/release-watch.conf (NTFY keys, WATCH_GITHUB/WATCH_URLS) and
# topic ACLs — see the script's docstring, templates/release-watch.conf.example
# and docs/notifications.md. Install is skipped silently when absent.
if [ -f /etc/release-watch.conf ]; then
    sudo cp "$REPO_DIR/systemd/release-watch.service" \
            "$REPO_DIR/systemd/release-watch.timer" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet --now release-watch.timer
    echo "release-watch timer installed and active"
else
    echo "note: /etc/release-watch.conf not found - release-watch not installed"
fi

echo
echo "done. try:  new-project my-app --port 8100"
