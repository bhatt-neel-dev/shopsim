"""One-click scenarios. Each one is plain config patches and spikes, so anything a preset does can
also be done by hand from the panel or the API."""
from __future__ import annotations

from shopsim.config import SERVICES, SpikeSpec


def _normal(store) -> None:
    store.cancel_all()
    store.reset_config()


def _black_friday(store) -> None:
    store.add_spike(SpikeSpec(kind="traffic", target="all", value=6, duration_s=900, name="Black Friday traffic x6"))
    store.add_spike(SpikeSpec(kind="latency", target="orders", value=250, duration_s=900))
    store.add_spike(SpikeSpec(kind="metric", target="all", metric="cpu_pct", value=88, duration_s=900))
    store.add_spike(SpikeSpec(kind="metric", target="orders", metric="queue_depth", value=4500, duration_s=900))
    store.add_spike(SpikeSpec(kind="error_rate", target="payments", value=0.15, error_type="card_declined",
                              duration_s=900))


def _payment_outage(store) -> None:
    store.add_spike(SpikeSpec(kind="error_rate", target="payments", value=0.8, error_type="gateway_timeout",
                              duration_s=600, name="Payment gateway outage"))
    store.add_spike(SpikeSpec(kind="log_storm", target="payments", value=120, level="ERROR", duration_s=600))


def _slow_db(store) -> None:
    for service in ("catalog", "orders"):
        store.add_spike(SpikeSpec(kind="db_latency", target=service, value=900, duration_s=600,
                                  name=f"Slow DB on {service}"))
        store.add_spike(SpikeSpec(kind="metric", target=service, metric="db_pool_used", value=48, duration_s=600))


def _log_storm(store) -> None:
    store.add_spike(SpikeSpec(kind="log_storm", target="all", value=300, level="ERROR", duration_s=300,
                              name="ERROR log storm"))
    store.add_spike(SpikeSpec(kind="log_storm", target="all", value=200, level="WARNING", duration_s=300))


def _cascading_failure(store) -> None:
    store.add_spike(SpikeSpec(kind="service_down", target="catalog", duration_s=180, name="catalog outage"))
    store.add_spike(SpikeSpec(kind="latency", target="orders", value=400, duration_s=240))


def _resource_pressure(store) -> None:
    store.add_spike(SpikeSpec(kind="cpu_burn", target="orders", value=1, duration_s=300, name="Real CPU burn"))
    store.add_spike(SpikeSpec(kind="memory_burn", target="orders", value=200, duration_s=300))
    store.add_spike(SpikeSpec(kind="metric", target="orders", metric="cpu_pct", value=96, duration_s=300))
    store.add_spike(SpikeSpec(kind="metric", target="orders", metric="memory_pct", value=91, duration_s=300))
    store.add_spike(SpikeSpec(kind="metric", target="orders", metric="gc_pause_ms", value=850, duration_s=300))


def _flapping_payments(store) -> None:
    store.set_state("payments", "flapping", flap_down_s=30, flap_up_s=60)


def _chaos_monkey(store) -> None:
    store.add_spike(SpikeSpec(kind="error_rate", target="random", value=0.5, duration_s=120, repeat_every_s=600,
                              name="Chaos: error burst"))
    store.add_spike(SpikeSpec(kind="latency", target="random", value=2000, duration_s=90, repeat_every_s=420,
                              start_in_s=180, name="Chaos: latency"))
    store.add_spike(SpikeSpec(kind="service_down", target="random", duration_s=60, repeat_every_s=900,
                              start_in_s=300, name="Chaos: outage"))


def _bad_release(store) -> None:
    store.add_spike(SpikeSpec(kind="js_errors", target="web", value=0.6, duration_s=600,
                              name="Bad release: JS errors on 60% of page views"))
    store.add_spike(SpikeSpec(kind="slow_frontend", target="web", value=1200, duration_s=600))
    store.add_spike(SpikeSpec(kind="error_rate", target="web", value=0.25, error_type="exception", duration_s=600,
                              name="Bad release: storefront API exceptions"))


def _slow_frontend(store) -> None:
    store.add_spike(SpikeSpec(kind="slow_frontend", target="web", value=2500, duration_s=600,
                              name="Slow pages: 2.5s long tasks + late LCP"))
    store.add_spike(SpikeSpec(kind="broken_assets", target="web", value=0.3, duration_s=600))
    store.add_spike(SpikeSpec(kind="latency", target="web", value=600, duration_s=600))


def _storefront_outage(store) -> None:
    store.add_spike(SpikeSpec(kind="service_down", target="web", duration_s=180, name="Storefront outage"))


def _all_up(store) -> None:
    store.patch_config({"services": {s: {"state": "up"} for s in SERVICES}})


PRESETS = {
    "normal": ("Healthy baseline: defaults restored, every spike cancelled, all services up", _normal),
    "black_friday": ("15 min: traffic and shoppers x6, +250ms orders latency, CPU 88%, queue backlog, "
                     "15% card declines", _black_friday),
    "payment_outage": ("10 min: payments gateway timeouts at 80% + ERROR log storm", _payment_outage),
    "slow_db": ("10 min: +900ms DB latency on catalog and orders, DB pool near exhaustion", _slow_db),
    "log_storm": ("5 min: 300 ERROR + 200 WARNING lines/s on every service", _log_storm),
    "cascading_failure": ("catalog container down for 3 min; orders fails checkouts and slows down",
                          _cascading_failure),
    "resource_pressure": ("5 min on orders: real 1-core CPU burn + 200 MB memory, CPU/memory/GC gauges spiked",
                          _resource_pressure),
    "flapping_payments": ("payments flaps: 30s down / 60s up until set back to Up", _flapping_payments),
    "chaos_monkey": ("Recurring random incidents: error bursts, latency and short outages on random services",
                     _chaos_monkey),
    "bad_release": ("10 min: JS errors on 60% of page views, slow pages, 25% storefront API exceptions",
                    _bad_release),
    "slow_frontend": ("10 min: 2.5s long tasks and late LCP, 30% broken images, +600ms storefront API",
                      _slow_frontend),
    "storefront_outage": ("Storefront (web) container down for 3 min: shoppers' pages fail", _storefront_outage),
    "all_up": ("Set every service back to Up (keeps spikes)", _all_up),
}
