# Grafana — processing / infra observability

> ## ⏸️ SHELVED (dormant) — not run at this stage
> Grafana is **intentionally not running**. At single-node scale its unique value
> (alerting, retention, time-series-at-scale) is marginal, and running it duplicated
> the in-app metrics (two SQL definitions of one number → they drift → trust erodes)
> while adding a Docker dependency and constant 1-minute background DB polling from
> the alert rules. The **in-app Observability tab is now the single source of truth**:
> it owns quality/verdict analytics *and* windowed infra time-series.
>
> This directory is kept as **ready-to-run code**. When the system goes multi-node /
> production, bring it back with the `docker compose up -d` below — no rebuild needed.
> Until then, treat it as an appendix, not live infrastructure.

The **processing side** of observability (the in-app *Observability* tab covers the
application / per-lead / quality side). Grafana reads the same Postgres and adds
time-series ops panels + alerting.

## Run

```bash
cd ops/grafana
docker compose up -d
```

Open **http://localhost:3000** → login `admin` / `admin` (you'll be asked to change it).
The **Payment Verification → Processing** dashboard is auto-provisioned.

Stop: `docker compose down` (add `-v` to also wipe Grafana's stored state).

## Panels
- **Model latency p50/p95/p99** over time — the throughput bottleneck's profile.
- **Throughput** (leads closed / min) and **Outcomes over time** (stacked verdicts).
- **Model errors over time** + **Failed jobs / Model errors** stat tiles.
- **Time per stage** — where the wall-clock goes (stage2 = the Medha call).

## Connecting to your Postgres
The datasource is provisioned from `provisioning/datasources/datasource.yml` and
defaults to the app's settings (`postgres` / `postgres` / `payment_verification` on
the host, reached from the container via `host.docker.internal`). Override by creating
`ops/grafana/.env`:

```env
PV_DB_HOST=host.docker.internal
PV_DB_PORT=5432
PV_DB_NAME=payment_verification
PV_DB_USER=postgres
PV_DB_PASSWORD=your-password
```

**If the datasource can't connect**, host Postgres is likely refusing the container's
connection. Two one-time changes on the host Postgres:
1. `postgresql.conf`: `listen_addresses = '*'`
2. `pg_hba.conf`: add `host all all 0.0.0.0/0 scram-sha-256` (or `md5`)

then restart Postgres. (This only lets the local Docker network in; keep it behind
your firewall.)

## Alerting (next)
These same queries back Grafana alert rules — e.g. p95 latency over a threshold,
throughput dropping to ~0, or model-error rate spiking. Add them under
**Alerting → Alert rules** once the dashboard is up.
