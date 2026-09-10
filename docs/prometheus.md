# Prometheus metrics stack (step 1: scrape)

*Shipped 2026-09-02 — Prom stack idea, first step. The 09-02 devlog named
this the next pick and asked whether the graphs earn their RAM; step 1
answers with real numbers.*

## What runs

`prometheus` and `prometheus-node-exporter` from Debian (apt, no
containers — 2.53.3 and the matching node_exporter on this box), as the
packages' own systemd units plus two drop-ins that pin both daemons to
**loopback only** (`prometheus/prometheus-bind-local.conf`,
`node-exporter-bind-local.conf`): the tailnet must not see an
unauthenticated Prometheus. `install.sh` reproduces the whole thing:
copies `prometheus/prometheus.yml` to `/etc/prometheus/`, installs the
drop-ins, enables both services.

## RAM answer (measured live, RSS)

| daemon | RSS |
|---|---|
| prometheus | ~79 MB |
| prometheus-node-exporter | ~24 MB |
| **total** | **~102 MB** |

On an 8 GB box that also decodes ships this is a rounding error. The
step-2 plan said "Grafana from apt" — verified false 2026-09-05: grafana
is not in Debian trixie, and this box runs Debian packages only (no
third-party repos, no containers). The graphs get a native answer
instead: `prom-dash` (step 2, shipped) — zero extra resident daemons.

## Scrape targets

- `prometheus` — self-scrape, `127.0.0.1:9090`
- `node` — node_exporter, `127.0.0.1:9100` (systemd collector on:
  `node_systemd_unit_state` exposes every unit's state — ~990 series,
  i.e. per-service health over time, the raw material for the dashboard
  graph the devlogs quote)

`up` returns 1 for both jobs; every unit on the box, including the
pi-cicd ones (`project-guard`, `service-probe`, `pi-doctor`,
`loop-heartbeat`, ...), is a `node_systemd_unit_state` series.

## Verify

```sh
systemctl is-active prometheus prometheus-node-exporter
curl -s http://127.0.0.1:9090/api/v1/targets   # both up
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up'
curl -s http://127.0.0.1:9100/metrics | grep -c '^node_systemd_unit_state'
```

## Step 2 — shipped 2026-09-05: `prom-dash` (the pinned dashboard)

Grafana cannot get on this box the box's way (not in trixie; no
third-party repos), so `prom-dash` renders the pinned dashboard
natively: it range-queries the loopback Prometheus for the four panels
the devlogs quote — CPU temperature against load, active/failed systemd
units — and emits ONE self-contained HTML page with inline SVG
sparklines. On demand (`prom-dash`, linked by install.sh; no timer by
design), ~0 MB resident. PANELS is the pin; tests/test_prom_dash.py
binds it — a fake Prometheus must see exactly these queries, hermetic.
Unit-state semantics, verified live: node_systemd_unit_state is a 0/1
gauge per (unit, candidate state), so count() overcounts — sum() is the
number of units actually in that state (count() said 197 "active"
against 99 real; the bug died before merge). Live render 2026-09-06:
24 h temp 45.9–50.7 °C, load 0.0–1.4, 99 active / 0 failed units.
pi-cicd commits `a48fafb`, `9c02da7`.

## Step 3a shipped: ntfy `/metrics` scrape (2026-09-09)

`metrics-listen-http: "127.0.0.1:9091"` added to the live ntfy server
config (/etc/ntfy/server.yml, root-owned, pi-backup-covered — the one
planned restart happened 05:32 IST at a quiet hour), and the `ntfy` job
scrapes it loopback-only here (15 s interval). Live evidence: ntfy
active after restart, `/metrics` answering on 127.0.0.1:9091, and
`up{job="ntfy"}` = 1 via the query API.

## Step 3b shipped: `metric-alert` (the stdlib rule-check)

The board item asked for a delivery consumer
(Alertmanager-from-apt vs stdlib rule-check) and an answer to the mute
gap. Both decided 2026-09-10, in favour of `metric-alert`:

- **Rule-check, not Alertmanager.** A second daemon with its own config
  language and no apt-pinned surface worth its RAM, for five rules.
  Prometheus scrapes; `metric-alert` decides (query API only — nothing
  is written into `/etc/prometheus/`).
- **Mute gap closed.** Alerts publish through `ntfy_lib`, exactly like
  every pi-cicd publisher, so `ntfy-notify --mute` silences metric
  alerts too. An Alertmanager webhook receiver would have sat outside
  ntfy_lib and stayed un-mutable from the box.

Rules live in `/etc/metric-alert.conf` as `RULE=Name op threshold =
promql-expression`; a breach must hold for CONFIRM_FAILS sweeps
(default 2) before it alerts, and alerting is edge-triggered — one
message when a rule starts breaching, one when it recovers, silence in
between. Topic is the existing, already-provisioned `services` topic:
no new ACL grant and no ntfy restart (a dedicated `alerts` topic is a
later, one-line change if volume ever justifies it —
docs/notifications.md has the grant recipe).

Shipped rules (templates/metric-alert.conf.example): root filesystem
used %, SoC temperature, memory used %, failed systemd units, and any
scrape target down. State + rendered summary in
`~/.local/state/metric-alert/`. systemd timer every 5 min, offset 4 min
from service-probe so the two sweeps never stack — probe answers "is it
up", metric-alert answers "is it healthy".
