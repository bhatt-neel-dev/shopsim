"""Real resource pressure for cpu_burn / memory_burn spikes (capped by settings)."""
from __future__ import annotations

import math
import multiprocessing as mp
import time

_MB = 1024 * 1024


def _burn(duty, stop) -> None:
    period = 0.1
    x = 1
    while not stop.is_set():
        d = duty.value
        if d <= 0:
            time.sleep(0.2)
            continue
        end = time.perf_counter() + period * d
        while time.perf_counter() < end:
            x = (x * 7919) % 1_000_003
        rest = period * (1 - d)
        if rest > 0:
            time.sleep(rest)


class CpuBurner:
    """``cores`` worth of busy-looping, spread over ceil(cores) processes (the GIL rules out threads)."""

    def __init__(self, max_cores: float):
        self.max_cores = max_cores
        self.cores = 0.0
        self._mp = mp.get_context("spawn")
        self._workers: list[tuple] = []

    @property
    def pids(self) -> list[int]:
        return [p.pid for p, _, _ in self._workers if p.pid]

    def set(self, cores: float) -> None:
        cores = max(0.0, min(cores, self.max_cores))
        if abs(cores - self.cores) < 1e-6:
            return
        n = math.ceil(cores) if cores > 0 else 0
        duty = cores / n if n else 0.0
        while len(self._workers) < n:
            value, stop = self._mp.Value("d", duty), self._mp.Event()
            proc = self._mp.Process(target=_burn, args=(value, stop), daemon=True, name="shopsim-cpu-burn")
            proc.start()
            self._workers.append((proc, value, stop))
        while len(self._workers) > n:
            proc, _, stop = self._workers.pop()
            stop.set()
            proc.join(timeout=2)
            if proc.is_alive():
                proc.terminate()
        for _, value, _ in self._workers:
            value.value = duty
        self.cores = cores

    def shutdown(self) -> None:
        self.set(0)


class MemoryBurner:
    """Holds ``mb`` megabytes of touched memory, growing or shrinking up to 64 MB per step."""

    def __init__(self, max_mb: float):
        self.max_mb = max_mb
        self._blocks: list[bytearray] = []

    @property
    def mb(self) -> int:
        return len(self._blocks)

    def step(self, target_mb: float) -> None:
        target = int(max(0, min(target_mb, self.max_mb)))
        current = len(self._blocks)
        if current < target:
            for _ in range(min(64, target - current)):
                self._blocks.append(bytearray(b"\xa5") * _MB)
        elif current > target:
            del self._blocks[target:]
