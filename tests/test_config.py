import pytest
from pydantic import ValidationError

from shopsim.config import Spike, SpikeSpec, compute_effective, default_config
from shopsim.control.store import Store, deep_merge


def test_defaults_are_valid_and_cover_every_service():
    cfg = default_config()
    assert set(cfg.services) == {"catalog", "orders", "payments"}
    assert cfg.services["payments"].error_types["card_declined"] > 0


def test_deep_merge_none_removes_key():
    merged = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": None, "d": 3}})
    assert merged == {"a": {"c": 2, "d": 3}}


def test_patch_validates_and_keeps_old_config_on_error(tmp_path):
    store = Store(tmp_path / "control.json")
    store.patch_config({"services": {"catalog": {"error_rate": 0.4}}})
    assert store.config.services["catalog"].error_rate == 0.4
    with pytest.raises(ValidationError):
        store.patch_config({"services": {"catalog": {"error_rate": 3}}})
    with pytest.raises(ValidationError):
        store.patch_config({"services": {"orders": {"error_types": {"no_such_error": 1}}}})
    assert store.config.services["catalog"].error_rate == 0.4


def test_config_survives_restart(tmp_path):
    store = Store(tmp_path / "control.json")
    store.patch_config({"traffic": {"rps": 42}})
    store.add_spike(SpikeSpec(kind="latency", target="orders", value=500, duration_s=60))
    again = Store(tmp_path / "control.json")
    assert again.config.traffic.rps == 42
    assert len(again.spikes) == 1


@pytest.mark.parametrize("spec", [
    {"kind": "metric", "target": "orders"},                                  # metric needs a gauge name
    {"kind": "metric", "metric": "cpu_pct", "value": 140},                   # over the gauge's max
    {"kind": "error_rate", "value": 1.5},                                    # rate above 1
    {"kind": "latency", "error_type": "db_lock"},                            # error_type only on error_rate
    {"kind": "traffic", "duration_s": 600, "repeat_every_s": 60},            # repeats overlap
])
def test_bad_spikes_are_rejected(spec):
    with pytest.raises(ValidationError):
        SpikeSpec(**spec)


def test_spike_defaults_fill_in():
    assert SpikeSpec(kind="log_storm").level == "ERROR"
    assert SpikeSpec(kind="traffic").value == 5
    assert SpikeSpec(kind="service_down", value=3).value is None


def _active(**kw) -> Spike:
    spike = Spike(**SpikeSpec(**kw).model_dump(), id="sp_x", created_at=0)
    spike.activate(0)
    return spike


def test_effective_folds_in_active_spikes():
    cfg = default_config()
    spikes = [
        _active(kind="error_rate", target="orders", value=0.6, error_type="dependency"),
        _active(kind="latency", target="all", value=300),
        _active(kind="metric", target="orders", metric="cpu_pct", value=97),
        _active(kind="log_storm", target="orders", value=250),
        _active(kind="traffic", target="orders", value=4),
        _active(kind="service_degraded", target="catalog"),
    ]
    orders = compute_effective(cfg, spikes, "orders", now=1)
    assert orders.error_rate == 0.6 and orders.forced_error_type == "dependency"
    assert orders.latency_ms == cfg.services["orders"].latency_ms + 300
    assert orders.gauges["cpu_pct"] == 97 and "cpu_pct" in orders.spiked_gauges
    assert orders.log_storms == [{"rate": 250, "level": "ERROR"}]
    assert orders.traffic_multiplier == 4
    assert orders.state == "up"
    assert compute_effective(cfg, spikes, "catalog", now=1).state == "degraded"
    assert compute_effective(cfg, spikes, "payments", now=1).error_rate == cfg.services["payments"].error_rate


def test_down_spike_beats_degraded_and_base_state():
    cfg = default_config()
    spikes = [_active(kind="service_degraded", target="payments"), _active(kind="service_down", target="payments")]
    assert compute_effective(cfg, spikes, "payments", now=1).state == "down"


def test_flapping_alternates_up_and_down():
    cfg = default_config()
    cfg.services["payments"].state = "flapping"
    cfg.services["payments"].flap_up_s = 10
    cfg.services["payments"].flap_down_s = 5
    states = [compute_effective(cfg, [], "payments", now=t, flap_since=0).state for t in (0, 9, 10, 14, 15)]
    assert states == ["up", "up", "down", "down", "up"]


def test_spike_lifecycle_schedule_activate_end_repeat(tmp_path):
    store = Store(tmp_path / "c.json")
    one_off = store.add_spike(SpikeSpec(kind="latency", target="orders", duration_s=10), now=100)
    later = store.add_spike(SpikeSpec(kind="traffic", target="all", duration_s=10, start_in_s=50,
                                      repeat_every_s=30), now=100)
    assert one_off.status == "active" and later.status == "scheduled"
    store.tick(111)
    assert one_off.status == "done"
    store.tick(150)
    assert later.status == "active" and later.runs == 1
    store.tick(160)
    assert later.status == "scheduled" and later.next_start == 180
    store.tick(180)
    assert later.status == "active" and later.runs == 2
    store.cancel_spike(later.id)
    assert later.status == "cancelled"


def test_random_target_resolves_on_each_run(tmp_path):
    store = Store(tmp_path / "c.json")
    spike = store.add_spike(SpikeSpec(kind="error_rate", target="random", value=0.9, duration_s=10), now=0)
    assert spike.resolved_target in ("catalog", "orders", "payments")
    hit = store.effective(spike.resolved_target, now=1)
    assert hit.error_rate == 0.9
