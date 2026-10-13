# ShopSim

A fake e-commerce shop built from three Python services that **continuously emits traces, metrics and
logs**. A web control panel changes everything at runtime: request rate, the GET/POST/PUT/DELETE mix,
error rates and error types, latency, log volume, resource gauges, and service up/down. You can
also schedule **spikes** of any of these.

Built for feeding an observability backend (e.g. Motadata ObserveOps) with realistic, controllable data.

```
            traffic generator (in control)
        GET/POST/PUT/DELETE at N rps, ratio you set
                 │            │             │
                 ▼            ▼             ▼
          ┌──────────┐  ┌──────────┐  ┌──────────┐
          │ catalog  │◄─┤  orders  ├─►│ payments │──► fake card gateway span
          │  :8181   │  │  :8182   │  │  :8183   │
          └────┬─────┘  └────┬─────┘  └────┬─────┘
            SQLite        SQLite        SQLite
                 │            │             │
     traces + metrics ── OTLP/HTTP ──► collector (:4318)
     logs (JSON) ─────── syslog UDP ─► :514   + /var/log/shopsim/<service>.log
                 ▲
     every service polls the control panel (:8180) every 2s for its live config
```

## The services

| Service | Port | What it does |
|---|---|---|
| `catalog` | 8181 | Products, categories, stock. `GET/POST/PUT/DELETE /products`, `/products/{id}/reserve`, `/release`, `/categories` |
| `orders` | 8182 | Checkout and order lifecycle. A checkout reserves stock in **catalog** and charges **payments**, so one order is one trace across all three services. Failures roll back (release stock, void payment). |
| `payments` | 8183 | Authorize / capture / refund / void against a fake `cardnet` gateway, with a risk-score step |
| `control` | 8180 | Control panel UI + REST API, traffic generator, spike scheduler, container control |

Every service also runs background jobs (inventory replenish, fulfillment sweep, settlement batches,
retention) that produce their own traces, and emits background logs at a configurable rate.

## Telemetry

- **Traces** (OTLP/HTTP): server spans (FastAPI), DB spans with the SQL text, service-to-service client
  spans with W3C propagation, business spans (`order.pricing`, `risk.score`, `gateway.authorize`, ...).
  Failures carry **real Python exceptions and stack traces**, recorded on the span that actually failed.
- **Metrics** (OTLP/HTTP, every 10s):
  - Requests and latency: `shopsim.http.requests`, `shopsim.http.duration`, `shopsim.errors`, plus the standard `http.server.*` metrics.
  - DB and service-to-service calls: `shopsim.db.duration`, `shopsim.downstream.duration`.
  - Business: `shopsim.orders.placed/revenue/value/failed`, `shopsim.payments.processed/declined/refunds`, `shopsim.catalog.stock.units`, ...
  - Simulated resource gauges: `shopsim.cpu.utilization`, `shopsim.memory.utilization`, `shopsim.disk.utilization`, `shopsim.db.pool.used`, `shopsim.queue.depth`, `shopsim.threads.active`, `shopsim.gc.pause`, `shopsim.cache.hit_ratio`.
  - Real process metrics: `process.cpu.utilization`, `process.memory.usage`, `process.thread.count`.
- **Logs**: one JSON object per line with `trace_id`/`span_id`, `error.type`, `error.stack_trace`
  and event fields. Sent to syslog (UDP, RFC 3164 header), to rotating files and to stdout, and optionally over OTLP.
  You switch each destination on or off at runtime.

Resource attributes: `service.name=shopsim-<service>`, `service.namespace=shopsim`,
`deployment.environment`, `host.name`, `service.version`.

## What you can change at runtime

All of it from the panel, or via the API (see `/docs` on port 8180):

| Area | Knobs |
|---|---|
| Traffic | on/off, base rps, CRUD ratio (GET/POST/PUT/DELETE weights), share per service, max in flight |
| Errors | error rate per service, **per HTTP method** overrides, error-type mix: `http_500`, `exception` (real stack traces), `db_lock`, `timeout`, `rate_limited`, `validation`, `not_found`, `out_of_stock`, `dependency`, `card_declined`, `fraud_suspected`, `gateway_timeout` |
| Latency | base + jitter per service, DB latency, timeout length |
| Logs | background lines/s, level mix (DEBUG..CRITICAL), access logs on/off, destinations |
| Gauges | baseline for every simulated resource gauge |
| State | **Up** · **Degraded** (503s) · **Down** (container really stopped: port closed, no telemetry) · **Flapping** (down N s / up M s) |
| Telemetry | traces / metrics / each log destination on/off, trace sampling ratio |

### Spikes

A spike is a temporary change. You can fire it now, at a set time, or after a delay, and it can repeat
every N minutes. Its target is one service, all services, or a random one (chosen fresh on each run).

| Kind | Value |
|---|---|
| `traffic` | rate multiplier (x) |
| `error_rate` | 0-1, optionally forcing one error type |
| `latency` / `db_latency` | added ms |
| `log_storm` | extra lines/s at a chosen level |
| `metric` | drive a gauge (`cpu_pct`, `memory_pct`, `queue_depth`, ...) to a value |
| `cpu_burn` / `memory_burn` | **real** CPU cores / MB in the container (capped by `SHOPSIM_MAX_BURN_*`) |
| `service_down` / `service_degraded` | outage for the duration |

```bash
# 60% errors on orders for 5 minutes, forcing downstream connection resets
curl -X POST localhost:8180/api/spikes -H 'content-type: application/json' \
  -d '{"kind":"error_rate","target":"orders","value":0.6,"error_type":"dependency","duration_s":300}'

# CPU gauge to 95% on a random service for 2 min, every 15 min, starting in 10 min
curl -X POST localhost:8180/api/spikes -H 'content-type: application/json' \
  -d '{"kind":"metric","metric":"cpu_pct","value":95,"target":"random","duration_s":120,"repeat_every_s":900,"start_in_s":600}'

# Take payments down / bring it back
curl -X POST localhost:8180/api/services/payments/state -H 'content-type: application/json' -d '{"state":"down"}'
curl -X POST localhost:8180/api/services/payments/state -H 'content-type: application/json' -d '{"state":"up"}'

# Change base settings (deep merge; null removes an override)
curl -X PATCH localhost:8180/api/config -H 'content-type: application/json' \
  -d '{"traffic":{"rps":30,"crud_ratio":{"GET":40,"POST":30,"PUT":20,"DELETE":10}},
       "services":{"catalog":{"error_rate_by_method":{"DELETE":0.5}}}}'
```

### Presets

`normal` · `black_friday` · `payment_outage` · `slow_db` · `log_storm` · `cascading_failure` ·
`resource_pressure` · `flapping_payments` · `chaos_monkey` (recurring random incidents) · `all_up`.
Each preset is just config changes plus spikes, so you can see and cancel everything it did.

## Run it

```bash
git clone https://github.com/bhatt-neel-dev/shopsim.git && cd shopsim
cp .env.example .env        # point OTLP_ENDPOINT / SYSLOG_HOST at your collector
docker compose up -d --build
open http://localhost:8180
```

Or `bash deploy/install.sh` on a Linux Docker host: it clones or updates the repo into `~/shopsim`,
writes `.env` with the host's IP as the syslog source, and starts everything.

Config and spikes are saved in the `shopsim-data` volume, so they survive restarts.
`docker compose down -v` wipes them, along with the shop data.

**Without Docker** (dev): `pip install -r requirements-dev.txt`, then run
`python -m shopsim control`, `... catalog`, `... orders` and `... payments` in four terminals.
Set `SHOPSIM_DATA_DIR`/`SHOPSIM_LOG_DIR` to writable paths. Without a Docker socket, "down" is
simulated: the service answers 503 and stops all telemetry.

Tests: `pytest` (32 tests, in-process, no network).

### Seeing traces in Motadata APM

Motadata only stores spans for services registered under **Settings → APM → Application
Registration**. Until then, the collector accepts the spans and Motadata drops them without an error.
Register `shopsim-catalog`, `shopsim-orders` and `shopsim-payments` as Python, bare metal, manual
instrumentation, against the agent whose collector (`:4318`) receives the traces. If you
register them through the API, send every field the UI sends: `service.deployment.type: BARE_METAL`,
`service.trace.state: yes`, `service.trace.attributes`, `custom.trace.attributes` and
`service.trace.notify.required`. A registration with only the name, type and agent stayed
*Inactive* on 8.2.9 and stored nothing. Set
`OTEL_RESOURCE_ATTRIBUTES=trace.business.service=<name>` in `.env` to group the three services
under a business service.

## Settings (environment)

| Variable | Default | |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://127.0.0.1:4318` | OTLP/HTTP base URL (`/v1/traces`, `/v1/metrics`, `/v1/logs`) |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | `<base>/v1/traces` | full URL to send traces elsewhere (compose: `OTLP_TRACES_ENDPOINT` in `.env`) |
| `SHOPSIM_SYSLOG_HOST` / `_PORT` | `127.0.0.1` / `514` | syslog target (UDP) |
| `SHOPSIM_LOG_DIR` | `/var/log/shopsim` | JSON log files |
| `SHOPSIM_ENV` | `lab` | `deployment.environment` |
| `SHOPSIM_METRIC_INTERVAL_S` | `10` | metric export interval |
| `SHOPSIM_CONTROL_PASSWORD` | empty | basic auth for the panel (user `admin`) |
| `SHOPSIM_MAX_TOTAL_RPS` / `_MAX_BURN_CORES` / `_MAX_BURN_MB` | `600` / `2` / `300` | safety caps |

The control container mounts the Docker socket so it can stop and start the three
`shopsim-*` service containers. It never touches any other container. The panel has no auth by
default; set `SHOPSIM_CONTROL_PASSWORD` if the host is reachable by people who shouldn't use it.
