"""Each service's live view of the control panel: the ``Effective`` config, refreshed by polling."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from shopsim import settings as settings_mod
from shopsim.config import Effective, compute_effective, default_config


class Runtime:
    def __init__(self, service: str):
        self.service = service
        # Until the control panel answers, run with the defaults it would hand out.
        self.eff: Effective = compute_effective(default_config(), [], service)
        self.connected = False
        self.last_ok: float | None = None

    @property
    def muted(self) -> bool:
        """A simulated outage (no Docker to really stop us): stop emitting anything."""
        return self.eff.state == "down" and self.eff.down_mode == "simulated"

    async def poll_forever(self, logger: logging.Logger) -> None:
        s = settings_mod.settings
        url = f"{s.control_url}/api/effective/{self.service}"
        async with httpx.AsyncClient(timeout=2.0) as client:
            while True:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    previous = self.eff
                    self.eff = Effective.model_validate(resp.json())
                    self.last_ok = time.time()
                    if not self.connected:
                        logger.info("Connected to control panel at %s", s.control_url,
                                    extra={"fields": {"event": "control.connected"}})
                    self.connected = True
                    if previous.state != self.eff.state:
                        logger.warning("Service state changed: %s -> %s", previous.state, self.eff.state,
                                       extra={"fields": {"event": "service.state", "state": self.eff.state}})
                except Exception as exc:  # noqa: BLE001 - keep running on the last known config
                    if self.connected:
                        logger.warning("Lost control panel (%s); keeping last known config", exc,
                                       extra={"fields": {"event": "control.disconnected"}})
                    self.connected = False
                await asyncio.sleep(s.poll_interval_s)
