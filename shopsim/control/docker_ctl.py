"""Stops and starts the service containers over the Docker socket, so 'down' is a real outage:
the port closes and the service's telemetry stops. Only the three shopsim service containers
are ever touched."""
from __future__ import annotations

import os
import time

import httpx

from shopsim.config import SERVICES


class DockerControl:
    def __init__(self, socket_path: str, prefix: str):
        self.socket_path = socket_path
        self.prefix = prefix
        self.available = False
        self.reason = "not checked yet"
        self.states: dict[str, str] = {s: "unknown" for s in SERVICES}
        self._client: httpx.AsyncClient | None = None
        self._checked_at = 0.0

    def name(self, service: str) -> str:
        return f"{self.prefix}{service}"

    async def check(self) -> bool:
        self._checked_at = time.time()
        if not os.path.exists(self.socket_path):
            self.available, self.reason = False, f"no Docker socket at {self.socket_path}"
            return False
        if self._client is None:
            self._client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.socket_path),
                                             base_url="http://docker", timeout=20)
        try:
            resp = await self._client.get(f"/containers/{self.name(SERVICES[0])}/json")
            if resp.status_code == 404:
                self.available, self.reason = False, f"container {self.name(SERVICES[0])} not found"
            else:
                resp.raise_for_status()
                self.available, self.reason = True, "ok"
        except httpx.HTTPError as exc:
            self.available, self.reason = False, f"Docker API error: {exc}"
        return self.available

    async def refresh(self) -> dict[str, str]:
        if not self.available:
            if time.time() - self._checked_at > 30:
                await self.check()
            if not self.available:
                self.states = {s: "n/a" for s in SERVICES}
                return self.states
        for service in SERVICES:
            try:
                resp = await self._client.get(f"/containers/{self.name(service)}/json")
                self.states[service] = resp.json()["State"]["Status"] if resp.status_code == 200 else "missing"
            except (httpx.HTTPError, KeyError, ValueError):
                self.states[service] = "unknown"
        return self.states

    async def stop(self, service: str) -> None:
        resp = await self._client.post(f"/containers/{self.name(service)}/stop", params={"t": 2})
        if resp.status_code not in (204, 304):
            raise RuntimeError(f"docker stop {self.name(service)}: HTTP {resp.status_code} {resp.text[:200]}")

    async def start(self, service: str) -> None:
        resp = await self._client.post(f"/containers/{self.name(service)}/start")
        if resp.status_code not in (204, 304):
            raise RuntimeError(f"docker start {self.name(service)}: HTTP {resp.status_code} {resp.text[:200]}")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
