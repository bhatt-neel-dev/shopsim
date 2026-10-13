"""Everything a service does on its own: config polling, background logs and log storms, periodic
jobs (which produce their own traces), resource gauges and real CPU/memory burns."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time

import psutil
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind

from shopsim import settings as settings_mod
from shopsim.common import bugs, faults, logtemplates
from shopsim.common.burn import CpuBurner, MemoryBurner
from shopsim.config import GAUGES

_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR,
           "CRITICAL": logging.CRITICAL}
_MAX_LINES_PER_TICK = 300  # 3000 lines/s ceiling per service


def _count(rate: float, dt: float, rng: random.Random) -> int:
    expected = rate * dt
    n = int(expected)
    return n + (1 if rng.random() < expected - n else 0)


class GaugeSim:
    """Random-walk around the configured value; snaps quickly to a spiked value."""

    def __init__(self, runtime, rng: random.Random):
        self.runtime = runtime
        self.rng = rng
        self.values: dict[str, float] = {}

    def value(self, name: str) -> float:
        eff = self.runtime.eff
        target = eff.gauges.get(name, 0.0)
        current = self.values.get(name, target)
        spiked = name in eff.spiked_gauges
        pull, noise = (0.75, 0.02) if spiked else (0.3, 0.06)
        current += (target - current) * pull
        current *= 1 + self.rng.uniform(-noise, noise)
        current = max(0.0, min(current, GAUGES[name]["max"]))
        self.values[name] = current
        return round(current, 2)


class Background:
    def __init__(self, ctx, jobs: list[tuple[str, float, callable]]):
        s = settings_mod.settings
        self.ctx = ctx
        self.jobs = jobs
        self.rng = random.Random()
        self.tasks: list[asyncio.Task] = []
        self.cpu = CpuBurner(s.max_burn_cores)
        self.mem = MemoryBurner(s.max_burn_mb)
        self.proc = psutil.Process(os.getpid())
        self.gauges = GaugeSim(ctx.runtime, self.rng)
        self._cpu_cache = (0.0, 0.0)
        self._children: dict[int, psutil.Process] = {}
        self._register_gauges()

    # ---- lifecycle -------------------------------------------------------------------------------
    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.tasks = [loop.create_task(self.ctx.runtime.poll_forever(self.ctx.logger)),
                      loop.create_task(self._logs()),
                      loop.create_task(self._burns())]
        self.tasks += [loop.create_task(self._job_loop(name, every, fn)) for name, every, fn in self.jobs]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.to_thread(self.cpu.shutdown)
        self.mem.step(0)

    # ---- logs ------------------------------------------------------------------------------------
    async def _logs(self) -> None:
        tick = 0.1
        while True:
            await asyncio.sleep(tick)
            eff = self.ctx.runtime.eff
            if self.ctx.runtime.muted:
                continue
            budget = _MAX_LINES_PER_TICK
            levels = {k: v for k, v in eff.log_levels.items() if v > 0}
            if levels and eff.log_rate > 0:
                for _ in range(min(budget, _count(eff.log_rate, tick, self.rng))):
                    self.emit(self.rng.choices(list(levels), weights=list(levels.values()))[0])
                    budget -= 1
            for storm in eff.log_storms:
                for _ in range(min(budget, _count(storm["rate"], tick, self.rng))):
                    self.emit(storm["level"], storm=True)
                    budget -= 1

    def emit(self, level: str, storm: bool = False) -> None:
        ctx = self.ctx
        message, fields = logtemplates.make(ctx.service, level, self.rng)
        fields["event.source"] = "storm" if storm else "background"
        levelno = _LEVELS[level]
        ctx.metrics.log_lines.add(1, {"level": level, "source": fields["event.source"]})
        if levelno >= logging.ERROR and self.rng.random() < 0.35:
            try:
                bugs.run_business_rules(ctx.service)
            except Exception:  # noqa: BLE001 - we want the traceback in the log
                ctx.logger.log(levelno, message, exc_info=True, extra={"fields": fields})
                return
        ctx.logger.log(levelno, message, extra={"fields": fields})

    # ---- jobs ------------------------------------------------------------------------------------
    async def _job_loop(self, name: str, every: float, fn) -> None:
        ctx = self.ctx
        await asyncio.sleep(self.rng.uniform(3, min(every, 20)))
        while True:
            if not ctx.runtime.muted and ctx.runtime.eff.state == "up":
                await self.run_job(name, fn)
            await asyncio.sleep(every * self.rng.uniform(0.85, 1.15))

    async def run_job(self, name: str, fn) -> None:
        ctx = self.ctx
        eff = ctx.runtime.eff
        injection = None
        if self.rng.random() < eff.error_rate:
            injection = faults.Injection(self.rng.choice(["db_lock", "exception"]))
        token = faults.set_injection(injection)
        started = time.perf_counter()
        try:
            with ctx.tracer.start_as_current_span(name, kind=SpanKind.INTERNAL, attributes={"shopsim.job": name}):
                await fn()
                await faults.checkpoint(ctx, "logic")
            ctx.logger.debug("Job %s finished in %.0fms", name, (time.perf_counter() - started) * 1000,
                             extra={"fields": {"event": "job.finished", "job": name}})
        except Exception as exc:  # noqa: BLE001 - a failing job is logged, never fatal
            ctx.metrics.errors.add(1, {"job": name, "error.type": type(exc).__name__})
            ctx.logger.error("Background job %s failed: %s: %s", name, type(exc).__name__, exc, exc_info=True,
                             extra={"fields": {"event": "job.failed", "job": name}})
        finally:
            faults.reset_injection(token)

    # ---- burns -----------------------------------------------------------------------------------
    async def _burns(self) -> None:
        while True:
            eff = self.ctx.runtime.eff
            cores = 0.0 if self.ctx.runtime.muted else eff.cpu_burn_cores
            mb = 0.0 if self.ctx.runtime.muted else eff.memory_burn_mb
            if abs(cores - self.cpu.cores) > 1e-6:
                self.ctx.logger.warning("CPU pressure %s: burning %.2f cores", "on" if cores else "off",
                                        min(cores, self.cpu.max_cores),
                                        extra={"fields": {"event": "burn.cpu", "cores": cores}})
                await asyncio.to_thread(self.cpu.set, cores)
            before = self.mem.mb
            self.mem.step(mb)
            if (before == 0) != (self.mem.mb == 0):
                self.ctx.logger.warning("Memory pressure %s: holding %d MB", "on" if self.mem.mb else "off",
                                        self.mem.mb, extra={"fields": {"event": "burn.memory", "mb": self.mem.mb}})
            await asyncio.sleep(1)

    # ---- gauges ----------------------------------------------------------------------------------
    def _register_gauges(self) -> None:
        meter = self.ctx.meter
        for name, meta in GAUGES.items():
            meter.create_observable_gauge(meta["metric"], callbacks=[self._gauge_cb(name)], unit=meta["unit"],
                                          description=f"Simulated {meta['label']}")
        meter.create_observable_gauge("process.cpu.utilization", callbacks=[self._cpu_cb], unit="1",
                                      description="Real CPU utilization incl. burn workers, 0-1 of all CPUs")
        meter.create_observable_gauge("shopsim.process.cpu.cores", callbacks=[self._cores_cb], unit="{core}",
                                      description="Real CPU cores in use incl. burn workers")
        meter.create_observable_gauge("process.memory.usage", callbacks=[self._rss_cb], unit="By",
                                      description="Real resident memory incl. burn workers")
        meter.create_observable_gauge("process.thread.count", callbacks=[self._threads_cb], unit="{thread}")

    def _gauge_cb(self, name: str):
        def callback(options: CallbackOptions):
            return [Observation(self.gauges.value(name))]
        return callback

    def _all_procs(self) -> list[psutil.Process]:
        alive = {}
        for pid in self.cpu.pids:
            proc = self._children.get(pid)
            if proc is None:
                try:
                    proc = psutil.Process(pid)
                    proc.cpu_percent(None)
                except psutil.Error:
                    continue
            alive[pid] = proc
        self._children = alive
        return [self.proc, *alive.values()]

    def _cpu_pct(self) -> float:
        total = 0.0
        for proc in self._all_procs():
            try:
                total += proc.cpu_percent(None)
            except psutil.Error:
                pass
        return total

    def _cpu_cb(self, options: CallbackOptions):
        pct = self._cpu_pct()
        self._cpu_cache = (time.time(), pct)
        return [Observation(round(pct / 100 / (psutil.cpu_count() or 1), 4))]

    def _cores_cb(self, options: CallbackOptions):
        ts, pct = self._cpu_cache
        if time.time() - ts > 5:
            pct = self._cpu_pct()
        return [Observation(round(pct / 100, 3))]

    def _rss_cb(self, options: CallbackOptions):
        total = 0
        for proc in self._all_procs():
            try:
                total += proc.memory_info().rss
            except psutil.Error:
                pass
        return [Observation(total)]

    def _threads_cb(self, options: CallbackOptions):
        return [Observation(self.proc.num_threads())]
