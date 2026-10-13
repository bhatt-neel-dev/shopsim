# ShopSim

A fake e-commerce shop built from Python services that **continuously emits traces, metrics, logs
and real-user monitoring (RUM) data**. A storefront web app runs the Motadata RUM SDK, and virtual
shoppers (headless browsers) use it like real visitors. Browser sessions, backend traces and logs are
linked by one trace id. A web control panel changes everything at runtime: request rate, the
GET/POST/PUT/DELETE mix, error rates and error types, latency, log volume, resource gauges, service
up/down, shopper sessions and frontend faults (JS errors, slow pages, broken images). You can also
schedule **spikes** of any of these.

Built for feeding an observability backend (e.g. Motadata ObserveOps) with realistic, controllable data.

```
   virtual shoppers (headless Chromium)            traffic generator (in control)
   browse · search · buy · abandon · account       GET/POST/PUT/DELETE at N rps
                 │                                       │
                 ▼                                       │
          ┌──────────────┐  RUM beacons  ┌───────────────┼──────────────┐
          │ web  :8184   │─────────────► Motadata RUM intake (:9477)    │
          │ storefront + │               │               │              │
          │ its API      │  traceparent  ▼               ▼              ▼
          └──────┬───────┘          ┌──────────┐  ┌──────────┐  ┌──────────┐
                 └────────────────► │ catalog  │◄─┤  orders  ├─►│ payments │──► fake card gateway
                                    │  :8181   │  │  :8182   │  │  :8183   │
                                    └──────────┘  └──────────┘  └──────────┘
     traces + metrics ── OTLP/HTTP ──► collector (:4318)
     logs (JSON, with trace_id) ── syslog UDP ─► :514  + /var/log/shopsim/<service>.log
                                  + "log" events on the span they were written under
     every service polls the control panel (:8180) every 2s for its live config
```

## The services

| Service | Port | What it does |
|---|---|---|
| `catalog` | 8181 | Products, categories, stock. `GET/POST/PUT/DELETE /products`, `/products/{id}/reserve`, `/release`, `/categories` |
| `orders` | 8182 | Checkout and order lifecycle. A checkout reserves stock in **catalog** and charges **payments**, so one order is one trace across all three services. Failures roll back (release stock, void payment). |
| `payments` | 8183 | Authorize / capture / refund / void against a fake `cardnet` gateway, with a risk-score step |
| `web` | 8184 | The storefront: a single-page shop with the Motadata RUM SDK, plus its API (cart, checkout, and read access to the other services). It relays RUM beacons to Motadata. |
| `shoppers` | - | Headless Chromium sessions that use the storefront: several journeys, desktop/mobile/tablet, visitors from 10 countries |
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

### RUM and correlation

- **RUM**: the storefront pages load the Motadata browser SDK (`@motadata365/browser-rum`, fetched and
  integrity-checked at image build). Page views, clicks, resources, long tasks, Web Vitals and JS
  errors are sent to `/rum` on the storefront, which relays them to the Motadata RUM intake. Because the
  beacons go to the same origin, the browser needs no CORS setup and never sees Motadata's certificate.
  The relay also completes views the SDK leaves open: Motadata stores a view only once it is marked
  complete, and the SDK does that a minute after the view ends, so a visitor who leaves sooner would lose it.
- **RUM → traces**: the SDK adds a W3C `traceparent` to every `/api/` call, so the browser request, the
  `shopsim-web` span and the catalog/orders/payments spans behind it are one trace. Motadata links the
  RUM resource to that trace (`apm.enabled` on the RUM application).
- **Logs → traces**: every log line carries `trace_id` and `span_id`. The same lines are also added
  to the span they were written under, as `log` events, so a trace shows its own logs (switch:
  *Logs → span events*).

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
| Telemetry | traces / metrics / each log destination on/off, logs as span events, trace sampling ratio |
| Shoppers | on/off, sessions per minute, browsers at once, think time, journey mix, device mix, spread over countries |
| Frontend faults | JS error rate and type mix, long tasks (ms and share of views), late LCP, layout shifts, broken images, rage clicks, storefront API latency |

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
| `rum_traffic` | shopper sessions multiplier (storefront) |
| `js_errors` | JS error rate per page view, 0-1 (storefront) |
| `slow_frontend` | ms of main-thread blocking on most views + late LCP (storefront) |
| `broken_assets` | share of product images that 404, 0-1 (storefront) |

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
`resource_pressure` · `flapping_payments` · `chaos_monkey` (recurring random incidents) · `bad_release`
(JS errors + slow pages + storefront API exceptions) · `slow_frontend` · `storefront_outage` · `all_up`.
Each preset is just config changes plus spikes, so you can see and cancel everything it did.

## Run it

```bash
git clone https://github.com/bhatt-neel-dev/shopsim.git && cd shopsim
cp .env.example .env        # point OTLP_ENDPOINT / SYSLOG_HOST at your collector, RUM_* at your RUM app
docker compose up -d --build
open http://localhost:8180    # control panel
open http://localhost:8184    # storefront
```

For RUM, register an application in Motadata (Settings → Real User Monitoring → Application
Registration, type JavaScript, deployment Other, APM correlation on) and copy `applicationId`,
`clientToken`, `service` and the intake (`site`) from its snippet into `RUM_APPLICATION_ID`,
`RUM_CLIENT_TOKEN`, `RUM_SERVICE` and `RUM_INTAKE_URL`. Without them the storefront runs without the SDK.

Or `bash deploy/install.sh` on a Linux Docker host: it clones or updates the repo into `~/shopsim`,
writes `.env` with the host's IP as the syslog source, and starts everything.

Config and spikes are saved in the `shopsim-data` volume, so they survive restarts.
`docker compose down -v` wipes them, along with the shop data.

**Without Docker** (dev): `pip install -r requirements-dev.txt`, then run
`python -m shopsim control`, `... catalog`, `... orders`, `... payments` and `... web` in separate
terminals. For shoppers: `pip install -r requirements-shoppers.txt && playwright install --only-shell
chromium`, then `python -m shopsim shoppers`. To get the RUM SDK for the storefront:
`python deploy/fetch_rum_sdk.py`.
Set `SHOPSIM_DATA_DIR`/`SHOPSIM_LOG_DIR` to writable paths. Without a Docker socket, "down" is
simulated: the service answers 503 and stops all telemetry.

Tests: `pytest` (41 tests, in-process, no network).

### Seeing traces and logs in Motadata

Motadata only stores spans for services registered under **Settings → APM → Application
Registration**. Until then, the collector accepts the spans and Motadata drops them without an error.
Register `shopsim-catalog`, `shopsim-orders`, `shopsim-payments` and `shopsim-web` as Python, bare metal, manual
instrumentation, against the agent whose collector (`:4318`) receives the traces. If you
register them through the API, send every field the UI sends: `service.deployment.type: BARE_METAL`,
`service.trace.state: yes`, `service.trace.attributes`, `custom.trace.attributes` and
`service.trace.notify.required`. A registration with only the name, type and agent stayed
*Inactive* on 8.2.9 and stored nothing. Set
`OTEL_RESOURCE_ATTRIBUTES=trace.business.service=<name>` in `.env` to group the three services
under a business service.

Logs arrive as syslog (UDP 514). Motadata stores them only from a registered log source: add the
ShopSim host's IP as a TCP/UDP log source (Settings → Log → Log Ingestion), with a JSON parser so
`trace_id`, `service`, `level`, `event` become searchable fields.

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
| `RUM_INTAKE_URL` | empty | Motadata RUM intake, e.g. `https://<motadata>:9477` (empty = no RUM) |
| `RUM_APPLICATION_ID` / `RUM_CLIENT_TOKEN` | empty | from the registered RUM application |
| `RUM_SERVICE` / `RUM_VERSION` | `ShopSim Storefront` / app version | exactly as in the RUM snippet |
| `SHOPSIM_STOREFRONT_URL` | `http://127.0.0.1:8184` | what the shoppers browse (compose: `STOREFRONT_URL`) |
| `SHOPSIM_MAX_SHOPPERS` | `3` | hard cap on browsers at once (~150-250 MB each) |

The control container mounts the Docker socket so it can stop and start the four
`shopsim-*` service containers. It never touches any other container. The panel has no auth by
default; set `SHOPSIM_CONTROL_PASSWORD` if the host is reachable by people who shouldn't use it.
