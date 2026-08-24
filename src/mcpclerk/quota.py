"""Per-tool quotas: calls per run and per sliding minute. Pure; the clock is injected."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable

WINDOW_S = 60.0


@dataclass(frozen=True)
class QuotaRefusal:
    reason: str
    retry_after_s: float | None


class QuotaBook:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._run: dict[str, int] = defaultdict(int)
        self._window: dict[str, deque[float]] = defaultdict(deque)

    def try_acquire(self, name: str, per_run: int | None, per_minute: int | None) -> QuotaRefusal | None:
        """Count one call if allowed and return None; otherwise return the refusal and count nothing."""
        now = self._clock()
        if per_run is not None and self._run[name] >= per_run:
            return QuotaRefusal(f"{name}: per-run quota of {per_run} exhausted", None)
        if per_minute is not None:
            q = self._window[name]
            while q and now - q[0] >= WINDOW_S:
                q.popleft()
            if len(q) >= per_minute:
                retry = WINDOW_S - (now - q[0])
                return QuotaRefusal(f"{name}: {per_minute}/min exhausted; retry after {retry:.0f}s", round(retry, 1))
            q.append(now)
        self._run[name] += 1
        return None

    def used(self, name: str) -> int:
        return self._run[name]
