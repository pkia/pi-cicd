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

# The one list of repo-root tools (2026-11-16). install.sh and
# tests/test_install_sh.py used to carry a copy each, so a new tool had to
# be added twice and the two could drift — the 11-16 red build was that
# shape. Both readers read tools.manifest now; the installer links from it
# AND refuses a tool at the repo root that it omits, and the test drives
# this script's own --check-tools instead of restating the rule.
MANIFEST="$REPO_DIR/tools.manifest"

manifest_tools() {
    # Non-comment, non-blank lines of the manifest, in file order.
    awk '{ sub(/#.*/, ""); gsub(/[ \t\r]/, ""); if ($0 != "") print }' "$1"
}

repo_root_tools() {
    # Every tool script at the repo root: extensionless regular files that
    # start with a shebang. Libraries carry .py, install.sh carries .sh,
    # and the manifest is data — so the rule needs no hand-kept ignore
    # list to rot alongside the tools themselves.
    local f
    for f in "$REPO_DIR"/*; do
        [ -f "$f" ] || continue
        case "$(basename "$f")" in *.*) continue ;; esac
        [ "$(head -c 2 "$f" 2>/dev/null)" = "#!" ] || continue
        basename "$f"
    done
}

check_tools() {
    # Validates the manifest against the tree; changes nothing.
    [ -f "$MANIFEST" ] || {
        echo "install.sh: $MANIFEST is missing — it is the one list of" \
             "repo-root tools that both this installer and the test read." >&2
        exit 1
    }
    local listed=0 name missing="" unlisted="" dupes=""
    local -A seen=()
    while IFS= read -r name; do
        listed=$((listed + 1))
        case "$name" in
            */*|.|..|*[[:space:]]*)
                echo "install.sh: tools.manifest entry '$name' is not a bare name" >&2
                exit 1 ;;
        esac
        [ -z "${seen[$name]:-}" ] || dupes+=" $name"
        seen[$name]=1
        [ -f "$REPO_DIR/$name" ] || missing+=" $name"
    done < <(manifest_tools "$MANIFEST")

    [ "$listed" -gt 0 ] || {
        echo "install.sh: tools.manifest lists no tools" >&2; exit 1
    }
    for name in $(repo_root_tools); do
        [ -n "${seen[$name]:-}" ] || unlisted+=" $name"
    done

    [ -z "$dupes" ] || {
        echo "install.sh: tools.manifest lists duplicate(s):$dupes" >&2; exit 1
    }
    [ -z "$missing" ] || {
        echo "install.sh: tools.manifest lists tool(s) that are not at the" \
             "repo root:$missing — the manifest and the tree disagree." >&2
        exit 1
    }
    [ -z "$unlisted" ] || {
        echo "install.sh: repo root holds tool(s) missing from" \
             "tools.manifest:$unlisted — add each name so this installer" \
             "links it." >&2
        exit 1
    }
    echo "tools.manifest: $listed tool(s) listed, every one present;" \
         "no tool at the repo root is unlisted"
}

case "${1:-}" in
    --check-tools) check_tools; exit 0 ;;
    "") ;;
    *) echo "usage: install.sh [--check-tools]" >&2; exit 2 ;;
esac

check_tools
mkdir -p "$BIN_DIR"

# State dir for the global alert-storm kill switch (ntfy_lib mute file).
# Created as the invoking user (ev) BEFORE any root-run tool can create
# it root-owned — pi-backup runs as root and would otherwise lock the
# owner out of ntfy-notify --mute (see docs/notifications.md).
mkdir -p "$HOME/.local/state/ntfy"

ln_manifest_tools() {
    # Link and mark executable each tool the manifest lists. One loop, not
    # eighteen hand-written lines: the manifest is the list.
    local tool
    while IFS= read -r tool; do
        ln -sf "$REPO_DIR/$tool" "$BIN_DIR/$tool"
        chmod +x "$REPO_DIR/$tool"
    done < <(manifest_tools "$MANIFEST")
}

ln_manifest_tools
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
#
# The `cluster` scrape job (2026-11-03) polls the coordinator's
# bearer-authed GET /metrics with the token read at scrape time from
# /etc/cluster-metrics.token. Prometheus validates referenced credential
# files at config load, so that file MUST exist before the yml that
# names it is copied — otherwise the whole metrics stack refuses its
# next load. Guarded below: token first (from the coordinator's own
# var/token, root:prometheus 0640 — never inline, never in a repo),
# yml second; no token found -> the yml copy is skipped with a loud
# note and the previously-installed config keeps running.
if command -v prometheus >/dev/null 2>&1; then
    sudo mkdir -p /etc/prometheus \
        /etc/systemd/system/prometheus.service.d \
        /etc/systemd/system/prometheus-node-exporter.service.d
    if [ -f "$HOME/cluster-prototype/var/token" ]; then
        sudo install -m 0640 -o root -g prometheus \
            "$HOME/cluster-prototype/var/token" /etc/cluster-metrics.token
        echo "cluster scrape token installed (/etc/cluster-metrics.token, root:prometheus 0640)"
    elif [ ! -f /etc/cluster-metrics.token ]; then
        echo "NOTE: no coordinator token at ~/cluster-prototype/var/token and" \
             "/etc/cluster-metrics.token absent — prometheus.yml NOT updated (the" \
             "cluster job names that file and Prometheus validates it at load)." \
             "Run the coordinator once, then re-run install.sh."
    fi
    if [ -f /etc/cluster-metrics.token ]; then
        sudo cp "$REPO_DIR/prometheus/prometheus.yml" /etc/prometheus/prometheus.yml
    fi
    sudo cp "$REPO_DIR/prometheus/prometheus-bind-local.conf" \
        /etc/systemd/system/prometheus.service.d/bind-local.conf
    sudo cp "$REPO_DIR/prometheus/node-exporter-bind-local.conf" \
        /etc/systemd/system/prometheus-node-exporter.service.d/bind-local.conf
    # Console page for the cluster numbers (2026-11-05): /consoles/cluster.html,
    # served from /etc/prometheus/consoles with the console flags from
    # consoles.conf. The page builds on the package's own example libraries
    # (prom.lib, menu.lib — shipped unused until now). A reload cannot pick
    # up new flags, so the unit is try-restarted below (loopback-only,
    # sub-second gap) as part of this owner-run pass.
    sudo install -m 0644 "$REPO_DIR/prometheus/consoles/cluster.html" \
        /etc/prometheus/consoles/cluster.html
    if [ -d /usr/share/doc/prometheus/examples/console_libraries ]; then
        sudo install -m 0644 \
            /usr/share/doc/prometheus/examples/console_libraries/prom.lib \
            /usr/share/doc/prometheus/examples/console_libraries/menu.lib \
            /etc/prometheus/console_libraries/
    else
        echo "NOTE: prometheus example console libraries not found -" \
             "/consoles/cluster.html will fail to render until prom.lib and" \
             "menu.lib are installed to /etc/prometheus/console_libraries/"
    fi
    sudo install -m 0644 "$REPO_DIR/prometheus/prometheus-consoles.conf" \
        /etc/systemd/system/prometheus.service.d/consoles.conf
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet --now prometheus prometheus-node-exporter
    sudo systemctl try-restart prometheus 2>/dev/null || true
    sudo systemctl reload prometheus 2>/dev/null || true
    echo "prometheus + node_exporter enabled (loopback-bound, /consoles/cluster.html)"
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

# metric-alert: metric rules against the loopback Prometheus.
# Needs /etc/metric-alert.conf (NTFY keys, PROM_URL, RULE lines) —
# see the script docstring, templates/metric-alert.conf.example and
# docs/prometheus.md (step 3b). Skipped silently when absent.
if [ -f /etc/metric-alert.conf ]; then
    sudo cp "$REPO_DIR/systemd/metric-alert.service" \
            "$REPO_DIR/systemd/metric-alert.timer" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet --now metric-alert.timer
    echo "metric-alert timer installed and active"
else
    echo "note: /etc/metric-alert.conf not found - metric-alert not installed"
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

# pi-doctor's boot check: this Pi has no RTC battery, so after a power cut
# the clock comes back at the last value systemd-timesyncd saved. Running
# 20 s after boot — while that file still holds the pre-outage clock — is the
# only moment the dark window can be measured (see pi-doctor --dark-window).
sudo cp "$REPO_DIR/systemd/pi-doctor-boot.service" \
        "$REPO_DIR/systemd/pi-doctor-boot.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --quiet --now pi-doctor-boot.timer
echo "pi-doctor boot check installed"

# journal-receipt: the durable slice of the journal (warning and above,
# bounded) plus the boot verdict. Runs on the boot and every 5 min: the
# `record` half keeps a receipt on real storage (~/.local/state, not the
# /var/log RAM disk), and the `check` half says out loud whether the
# previous boot recorded a shutdown sequence. No config file — the
# defaults (priority warning, 200 lines, 4 boots kept) are the point.
sudo cp "$REPO_DIR/systemd/journal-receipt.service" \
        "$REPO_DIR/systemd/journal-receipt.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --quiet --now journal-receipt.timer
echo "journal-receipt timer installed and active"

# feed-fresh: the devlog's heartbeat, measured from outside. One GET of the
# published feed an hour, one line, one exit code — 1 when the newest post is
# older than 36 h (the devlog fires daily at 01:00, so that is at least one
# missed fire). Stateless on purpose; the failed unit is what pi-doctor's
# sys:failed-unit finding names in the daily audit, and the journal keeps the
# line. No config file: the URL and the limit are the defaults the unit pins.
sudo cp "$REPO_DIR/systemd/feed-fresh.service" \
        "$REPO_DIR/systemd/feed-fresh.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --quiet --now feed-fresh.timer
echo "feed-fresh timer installed and active"

# dns-watch: dns-check on a timer, paging once per verdict change. Born from
# the 2026-11-16 outage (docs/incidents/2026-11-16-dns-outage.md): the profile
# wrote a header-only /etc/resolv.conf, every hostname died, and nothing on the
# box said so until Hermes failed to reach a model provider. The probe stays
# read-only; dns-watch writes only its state file and the page ledger beside
# it (~/.local/state/dns-watch/{state.json,pages.log}; `--history` reads them).
# Paging needs its own /etc/dns-watch.conf (templates/dns-watch.conf.example);
# without it the unit still runs and still fails on a broken verdict — it just
# cannot page, which is the truthful state of the fast path until the owner
# writes that file.
sudo cp "$REPO_DIR/systemd/dns-watch.service" \
        "$REPO_DIR/systemd/dns-watch.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --quiet --now dns-watch.timer
echo "dns-watch timer installed and active (pages need /etc/dns-watch.conf)"

# ntfy boot ordering: /etc/ntfy/server.yml binds a fixed Tailscale address
# (listen-http: 100.x.y.z:6839). On a reboot ntfy can start before tailscale0
# carries that address, fail to bind, and exhaust the default 5-restart limit —
# the service then stays dead while everything else comes up (2026-11-16).
# Conditioned on ntfy being installed and bound to a tailnet address, so a LAN-
# or 0.0.0.0-bound host does not get a pointless dependency on tailscaled.
if [ -f /etc/ntfy/server.yml ] && grep -qE '^listen-http: *100\.' /etc/ntfy/server.yml; then
    sudo mkdir -p /etc/systemd/system/ntfy.service.d
    sudo install -m 0644 "$REPO_DIR/systemd/ntfy.service.d/10-tailscale-bind.conf" \
        /etc/systemd/system/ntfy.service.d/10-tailscale-bind.conf
    sudo systemctl daemon-reload
    sudo systemctl restart ntfy
    echo "ntfy started after tailscaled (tailnet bind drop-in installed)"
else
    echo "note: ntfy not installed or not bound to a tailnet address - drop-in skipped"
fi

echo
echo "done. try:  new-project my-app --port 8100"
