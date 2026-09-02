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

# State dir for the global alert-storm kill switch (ntfy_lib mute file).
# Created as the invoking user (ev) BEFORE any root-run tool can create
# it root-owned — pi-backup runs as root and would otherwise lock the
# owner out of ntfy-notify --mute (see docs/notifications.md).
mkdir -p "$HOME/.local/state/ntfy"

ln -sf "$REPO_DIR/project-guard" "$BIN_DIR/project-guard"
ln -sf "$REPO_DIR/new-project"   "$BIN_DIR/new-project"
ln -sf "$REPO_DIR/loop-heartbeat" "$BIN_DIR/loop-heartbeat"
ln -sf "$REPO_DIR/ntfy-notify"   "$BIN_DIR/ntfy-notify"
ln -sf "$REPO_DIR/pi-backup"     "$BIN_DIR/pi-backup"
ln -sf "$REPO_DIR/release-watch" "$BIN_DIR/release-watch"
ln -sf "$REPO_DIR/service-probe" "$BIN_DIR/service-probe"
ln -sf "$REPO_DIR/chaos-drill"   "$BIN_DIR/chaos-drill"
chmod +x "$REPO_DIR/loop-heartbeat" "$REPO_DIR/ntfy-notify" \
         "$REPO_DIR/pi-backup" "$REPO_DIR/release-watch" \
         "$REPO_DIR/service-probe" "$REPO_DIR/chaos-drill"
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

# Prometheus metrics stack, step 1 (Prom stack idea, 2026-09-02): Debian
# packages, loopback-bound, minimal scrape config. Requires the packages:
#   sudo apt install prometheus prometheus-node-exporter
# Grafana dashboard + alerting are step 2 (docs/prometheus.md).
if command -v prometheus >/dev/null 2>&1; then
    sudo mkdir -p /etc/prometheus \
        /etc/systemd/system/prometheus.service.d \
        /etc/systemd/system/prometheus-node-exporter.service.d
    sudo cp "$REPO_DIR/prometheus/prometheus.yml" /etc/prometheus/prometheus.yml
    sudo cp "$REPO_DIR/prometheus/prometheus-bind-local.conf" \
        /etc/systemd/system/prometheus.service.d/bind-local.conf
    sudo cp "$REPO_DIR/prometheus/node-exporter-bind-local.conf" \
        /etc/systemd/system/prometheus-node-exporter.service.d/bind-local.conf
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet --now prometheus prometheus-node-exporter
    echo "prometheus + node_exporter enabled (loopback-bound)"
else
    echo "note: prometheus not installed - run: sudo apt install prometheus prometheus-node-exporter"
fi

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

# service-probe: uptime scoreboard for the long-running services.
# Needs /etc/service-probe.conf (NTFY keys, PROBE_HTTP/PROBE_DNS) and the
# 'services' topic ACLs — see the script's docstring,
# templates/service-probe.conf.example and docs/notifications.md.
# Install is skipped silently when the config is absent.
if [ -f /etc/service-probe.conf ]; then
    sudo cp "$REPO_DIR/systemd/service-probe.service" \
            "$REPO_DIR/systemd/service-probe.timer" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet --now service-probe.timer
    echo "service-probe timer installed and active"
else
    echo "note: /etc/service-probe.conf not found - service-probe not installed"
fi

# chaos-drill: nightly deliberate-failure drills (dead-port detection,
# ntfy fail-closed, timer liveness). Needs /etc/chaos-drill.conf and a
# read grant on the 'chaos' topic for the subscriber — see the script's
# docstring, templates/chaos-drill.conf.example and docs/notifications.md.
# Install is skipped silently when the config is absent.
if [ -f /etc/chaos-drill.conf ]; then
    sudo cp "$REPO_DIR/systemd/chaos-drill.service" \
            "$REPO_DIR/systemd/chaos-drill.timer" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet --now chaos-drill.timer
    echo "chaos-drill timer installed and active"
else
    echo "note: /etc/chaos-drill.conf not found - chaos-drill not installed"
fi

echo
echo "done. try:  new-project my-app --port 8100"
