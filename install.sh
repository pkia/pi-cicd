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

echo
echo "done. try:  new-project my-app --port 8100"
