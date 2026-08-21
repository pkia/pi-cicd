# pi-tooling

![CI](https://github.com/pkia/pi-tooling/actions/workflows/ci.yml/badge.svg)

The automation that keeps every project on my Raspberry Pi versioned,
CI-tested, backed up and (for services) auto-deployed. Born out of a real
incident: an uncommitted fix to a live satellite scheduler nearly got
overwritten while setting up the first deploy pipeline.

## The rules of the system

1. **Every project is a git repo with CI** — the moment it exists.
2. **No work is lost, ever** — unpushed commits are pushed; uncommitted
   changes are snapshotted to an `autosave` branch and pushed. A dead SD
   card costs at most 10 minutes of work.
3. **Deploys are safe by construction** — byte-compile and import gates
   before restart, health check after, automatic rollback to the last
   running commit if unhealthy, and a flap guard that never retries a
   commit that already failed.
4. **Nothing secret goes public** — the guard refuses to autosave
   key/token/credential-looking files (name and content heuristics) and
   logs a warning; new repos default to private.

## project-guard

A systemd timer (`project-guard.timer`) runs `project-guard` every
10 minutes. It scans `$HOME` and `$HOME/apps` for:

- **directories with code but no git repo** — adopted automatically:
  `git init`, a `.gitignore` that excludes oversized files, the standard
  CI workflow, an initial commit, a private GitHub repo, and a push.
- **existing repos** — unpushed `main` commits are pushed, and any dirty
  working tree is committed to an `autosave` branch via a private git
  index (the working tree, the real index and HEAD are never touched),
  then pushed.

It never pulls, merges or resets anything — that is deploy's job, not
backup's. Upstream source trees and binary distributions (e.g.
`AIS-catcher-src`, `AdGuardHome`) are excluded by name.

Log: `~/.local/state/project-guard.log` (also in journald under the
`project-guard` unit).

## new-project

```bash
new-project my-app                 # private repo, CI from the first commit
new-project my-app --public        # straight to public for show-off work
new-project my-app --port 8100     # + systemd service, venv, and the same
                                   #   pull-based CD as the other services
```

Scaffolds a Flask app with a pytest suite (Flask test client), the CI
workflow (ruff fatal rules, byte-compile, pytest on Python 3.11 + 3.13),
a README with a live CI badge, and — with `--port` — the deploy script,
systemd units and health-checked auto-restart wiring.

Publish a private project when it is ready:

```bash
gh repo edit my-app --visibility public
```

## The deploy pattern (shared by all services)

Every service repo carries `deploy/deploy.sh` + a systemd timer. The Pi
polls `origin/main` every 3 minutes; a deploy triggers when origin moved
ahead *or* when the running commit (`.deployed_commit` marker) differs
from HEAD — so commits made directly on the Pi deploy too. Unpushed local
commits are never clobbered (fast-forward only), a dirty tree blocks
deploys instead of being overwritten, and PR branches from forks never
run on the host (only `origin/main` is deployed — which is why these
public repos have no self-hosted runners).

## CI for this repo itself

ShellCheck-style hygiene: `bash -n` on every script plus shellcheck when
available.
