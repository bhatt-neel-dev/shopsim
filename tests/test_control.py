import collections

import httpx

from conftest import client


def control_app(shop):
    from shopsim.control.app import create_app

    return create_app(transports={name: httpx.ASGITransport(app=app) for name, app in shop.items()})


async def test_api_round_trip(shop):
    app = control_app(shop)
    async with client(app) as c:
        assert (await c.patch("/api/config", json={"services": {"orders": {"error_rate": 0.25}}})).status_code == 200
        assert (await c.get("/api/effective/orders")).json()["error_rate"] == 0.25
        bad = await c.patch("/api/config", json={"traffic": {"rps": -1}})
        assert bad.status_code == 422 and "rps" in bad.text
        spike = await c.post("/api/spikes", json={"kind": "latency", "target": "payments", "value": 900,
                                                  "duration_s": 60})
        assert spike.status_code == 201 and spike.json()["status"] == "active"
        assert (await c.get("/api/effective/payments")).json()["latency_ms"] >= 900
        assert (await c.delete(f"/api/spikes/{spike.json()['id']}")).json()["status"] == "cancelled"
        assert (await c.post("/api/services/catalog/state", json={"state": "degraded"})).json()["state"] == "degraded"
        assert (await c.post("/api/presets/normal")).status_code == 200
        assert (await c.get("/api/effective/catalog")).json()["state"] == "up"
        status = (await c.get("/api/status")).json()
        assert set(status["services"]) == {"catalog", "orders", "payments", "web"}
        assert status["rum"]["effective"]["sessions_per_min"] > 0
        assert status["meta"]["down_mode"] == "container"  # reconciler not running in tests
        assert (await c.get("/")).status_code == 200


async def test_every_preset_applies(shop):
    app = control_app(shop)
    async with client(app) as c:
        presets = (await c.get("/api/presets")).json()
        for preset in presets:
            assert (await c.post(f"/api/presets/{preset['name']}")).status_code == 200, preset
        await c.post("/api/presets/normal")


async def test_generator_respects_crud_ratio_and_reaches_services(shop):
    from shopsim.control.store import Store
    from shopsim.control.traffic import TrafficGenerator

    store = Store()
    store.patch_config({"traffic": {"crud_ratio": {"GET": 0, "POST": 1, "PUT": 0, "DELETE": 0}}})
    gen = TrafficGenerator(store, {s: "http://test" for s in shop}, 600,
                           transports={name: httpx.ASGITransport(app=app) for name, app in shop.items()})
    assert {gen.pick_method() for _ in range(50)} == {"POST"}

    store.patch_config({"traffic": {"crud_ratio": {"GET": 60, "POST": 20, "PUT": 15, "DELETE": 5}}})
    picks = collections.Counter(gen.pick_method() for _ in range(4000))
    assert 0.55 < picks["GET"] / 4000 < 0.65 and 0.02 < picks["DELETE"] / 4000 < 0.08

    await gen.refresh_ids()
    assert gen.ids["catalog"] and gen.ids["orders"]
    # No catalog DELETE: the service tests rely on the seeded products 1-3 still existing.
    plan = {"catalog": ("GET", "POST", "PUT"), "orders": ("GET", "POST", "PUT", "DELETE"),
            "payments": ("GET", "POST", "PUT", "DELETE"), "web": ("GET", "GET", "POST")}
    for service, methods in plan.items():
        for method in methods:
            await gen.fire(service, method)
    for service, methods in plan.items():
        snap = gen.stats.snapshot(service)
        assert snap["sent"] == len(methods) and snap["error_pct"] == 0, (service, snap)
    await gen.aclose()


def test_rates_follow_weights_multiplier_and_cap():
    from shopsim.config import SpikeSpec
    from shopsim.control.store import Store
    from shopsim.control.traffic import TrafficGenerator

    store = Store()
    store.patch_config({"traffic": {"rps": 100, "service_weights": {"catalog": 50, "orders": 50, "payments": 0,
                                                                     "web": 0}}})
    gen = TrafficGenerator(store, {s: "http://x" for s in ("catalog", "orders", "payments", "web")},
                           max_total_rps=300)
    assert gen.rates() == {"catalog": 50, "orders": 50, "payments": 0, "web": 0}
    store.add_spike(SpikeSpec(kind="traffic", target="orders", value=10, duration_s=60))
    rates = gen.rates()
    assert round(sum(rates.values())) == 300 and rates["orders"] > rates["catalog"] * 9
