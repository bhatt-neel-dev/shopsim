"""The control panel's state: config, spikes, flapping clocks and an event log, persisted to JSON."""
from __future__ import annotations

import copy
import json
import os
import time
import uuid
from collections import deque
from pathlib import Path

from shopsim.config import (
    SERVICES,
    Effective,
    ShopConfig,
    Spike,
    SpikeSpec,
    compute_effective,
    default_config,
)

_KEEP_FINISHED_S = 3600


def deep_merge(base: dict, patch: dict) -> dict:
    """Merge ``patch`` into a copy of ``base``. A ``None`` value removes the key (back to default)."""
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Store:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else None
        self.config: ShopConfig = default_config()
        self.spikes: dict[str, Spike] = {}
        self.flap_since: dict[str, float] = {}
        self.events: deque[dict] = deque(maxlen=300)
        self.version = 0
        self.down_mode = "container"
        self.rum_report: dict = {}  # latest counters from the shoppers (not persisted)
        self.on_event = None  # callable(entry) - the app logs events to stdout
        self._load()

    # ---- persistence -----------------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self.config = ShopConfig.model_validate(data["config"])
            self.spikes = {s["id"]: Spike.model_validate(s) for s in data.get("spikes", [])}
            self.flap_since = data.get("flap_since", {})
            self.event("info", f"Loaded saved config from {self.path}")
        except Exception as exc:  # noqa: BLE001 - a corrupt file must not stop the panel
            self.event("error", f"Ignoring unreadable config {self.path}: {exc}")

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "config": self.config.model_dump(mode="json"),
            "spikes": [s.model_dump(mode="json") for s in self.spikes.values()],
            "flap_since": self.flap_since,
        }, indent=2))
        os.replace(tmp, self.path)

    def _changed(self) -> None:
        self.version += 1
        self.save()

    def event(self, level: str, message: str, **data) -> None:
        entry = {"ts": time.time(), "level": level, "message": message, **data}
        self.events.appendleft(entry)
        if self.on_event is not None:
            self.on_event(entry)

    # ---- config ----------------------------------------------------------------------------------
    def patch_config(self, patch: dict) -> ShopConfig:
        merged = deep_merge(self.config.model_dump(mode="json"), patch)
        return self._apply(ShopConfig.model_validate(merged), f"Config updated: {_summarize(patch)}")

    def replace_config(self, data: dict) -> ShopConfig:
        return self._apply(ShopConfig.model_validate(data), "Config replaced")

    def reset_config(self) -> ShopConfig:
        return self._apply(default_config(), "Config reset to defaults")

    def _apply(self, new: ShopConfig, message: str) -> ShopConfig:
        now = time.time()
        for svc in SERVICES:
            old_state, new_state = self.config.services[svc].state, new.services[svc].state
            if new_state == "flapping" and old_state != "flapping":
                self.flap_since[svc] = now
            if old_state != new_state:
                self.event("warning" if new_state != "up" else "info", f"{svc}: state {old_state} -> {new_state}",
                           service=svc)
        self.config = new
        self.event("info", message)
        self._changed()
        return new

    def set_state(self, service: str, state: str, flap_down_s: int | None = None,
                  flap_up_s: int | None = None) -> ShopConfig:
        patch: dict = {"state": state}
        if flap_down_s is not None:
            patch["flap_down_s"] = flap_down_s
        if flap_up_s is not None:
            patch["flap_up_s"] = flap_up_s
        return self.patch_config({"services": {service: patch}})

    # ---- spikes ----------------------------------------------------------------------------------
    def add_spike(self, spec: SpikeSpec, now: float | None = None) -> Spike:
        now = time.time() if now is None else now
        start = spec.start_at if spec.start_at is not None else now + spec.start_in_s
        spike = Spike(**spec.model_dump(), id=f"sp_{uuid.uuid4().hex[:8]}", created_at=now, next_start=start)
        self.spikes[spike.id] = spike
        when = "now" if start <= now else f"in {int(start - now)}s"
        repeat = f", every {spike.repeat_every_s}s" if spike.repeat_every_s else ""
        self.event("info", f"Spike scheduled: {spike.describe()} on {spike.target} for {spike.duration_s}s, "
                           f"{when}{repeat}", spike=spike.id)
        self.tick(now)
        self._changed()
        return spike

    def cancel_spike(self, spike_id: str) -> Spike:
        spike = self.spikes[spike_id]
        if spike.status in ("scheduled", "active"):
            spike.status = "cancelled"
            spike.ends_at = time.time()
            self.event("info", f"Spike cancelled: {spike.describe()} on {spike.target}", spike=spike_id)
            self._changed()
        return spike

    def cancel_all(self) -> int:
        live = [s for s in self.spikes.values() if s.status in ("scheduled", "active")]
        for spike in live:
            spike.status = "cancelled"
            spike.ends_at = time.time()
        if live:
            self.event("info", f"Cancelled {len(live)} spike(s)")
            self._changed()
        return len(live)

    def tick(self, now: float | None = None) -> bool:
        """Start due spikes, end expired ones, re-arm repeating ones. Returns True if anything moved."""
        now = time.time() if now is None else now
        changed = False
        for spike in list(self.spikes.values()):
            if spike.status == "scheduled" and spike.next_start is not None and spike.next_start <= now:
                spike.activate(now)
                where = spike.resolved_target or spike.target
                self.event("warning", f"Spike started: {spike.describe()} on {where} for {spike.duration_s}s",
                           spike=spike.id)
                changed = True
            elif spike.status == "active" and spike.ends_at is not None and now >= spike.ends_at:
                if spike.repeat_every_s:
                    spike.status = "scheduled"
                    spike.next_start = max(now, spike.started_at + spike.repeat_every_s)
                    spike.resolved_target = None
                    self.event("info", f"Spike ended: {spike.describe()}; next run in "
                                       f"{int(spike.next_start - now)}s", spike=spike.id)
                else:
                    spike.status = "done"
                    self.event("info", f"Spike ended: {spike.describe()}", spike=spike.id)
                changed = True
            elif spike.status in ("done", "cancelled") and now - (spike.ends_at or spike.created_at) > _KEEP_FINISHED_S:
                del self.spikes[spike.id]
                changed = True
        if changed:
            self._changed()
        return changed

    def active_spikes(self) -> list[Spike]:
        return [s for s in self.spikes.values() if s.status == "active"]

    # ---- views -----------------------------------------------------------------------------------
    def effective(self, service: str, now: float | None = None) -> Effective:
        return compute_effective(self.config, self.active_spikes(), service, now=now,
                                 flap_since=self.flap_since.get(service), down_mode=self.down_mode,
                                 version=self.version)

    def traffic_multiplier(self, service: str) -> float:
        mult = 1.0
        for spike in self.active_spikes():
            if spike.kind == "traffic" and service in spike.targets():
                mult *= spike.value
        return mult


def _summarize(patch: dict, prefix: str = "") -> str:
    parts = []
    for key, value in patch.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            parts.append(_summarize(value, f"{path}."))
        else:
            parts.append(f"{path}={value}")
    text = ", ".join(p for p in parts if p)
    return text if len(text) < 200 else text[:197] + "..."
