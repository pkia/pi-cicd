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

On an 8 GB box that also decodes ships this is a rounding error; Grafana
(step 2) is the real cost to watch.

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

## Step 2 (next session, board item)

- Grafana from apt, bound loopback + one pinned dashboard: **CPU
  temperature against load**, **probe health over time** (from
  `node_systemd_unit_state` and, once exported, `service-probe`
  `status.json`).
- ntfy `/metrics` scrape — needs `metrics-listen-http` in the ntfy
  server config and one planned ntfy restart (backbone: do it at a quiet
  hour, not ad hoc).
- Alerting routes through `ntfy_lib` (inherits the global mute).
