"""Per-host politeness: the random delay between two requests, 429 back-off, robots crawl-delay.

Crawlee has no random per-request delay, so this runs in our pre-navigation
hook. Each host has a "next allowed start" time; a request waits until then and moves it
forward by a random delay in [min, max] (or the robots crawl-delay, if larger).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque

# the automatic slow-down (README 4.15)
WINDOW = 20  # the host's last pages looked at
MIN_PAGES = 6  # pages before it judges
ERROR_SHARE = 0.2  # errors/timeouts/unusable answers in the window that slow the host down
SLOW_MEDIAN = 3.0  # a median response time this many times the host's first median (and ≥ 5 s) also does
MAX_FACTOR = 8.0


class HostThrottle:
    def __init__(self, delay_min: float, delay_max: float) -> None:
        self.delay_min = max(0.0, delay_min)
        self.delay_max = max(self.delay_min, delay_max)
        self._next: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._crawl_delay: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._obs: dict[str, deque] = {}
        self._slow: dict[str, dict] = {}

    def factor(self, host: str) -> float:
        return float(self._slow.get(host, {}).get("factor", 1.0))

    def observe(self, host: str, ok: bool, seconds: float | None) -> tuple[str, str] | None:
        """One page of ``host`` ended (``ok`` false: a server error, a timeout, a network error, an
        unusable answer). Returns (``slowed_down``|``relaxed``, plain words) when the host's delay
        factor changed: doubled (up to 8×) when the last pages show ≥ 20 % errors or a median
        response time 3× the first one (and ≥ 5 s); eased by 1.5 after 10 clean pages. The
        configured delays are the floor: the factor never goes below 1."""
        w = self._obs.setdefault(host, deque(maxlen=WINDOW))
        w.append((bool(ok), seconds))
        st = self._slow.setdefault(host, {"factor": 1.0, "since": 0, "base": None})
        st["since"] += 1
        times = sorted(t for _o, t in w if t is not None)
        median = times[len(times) // 2] if times else None
        if st["base"] is None and len(times) >= MIN_PAGES:
            st["base"] = median
        if len(w) < MIN_PAGES:
            return None
        errors = sum(1 for o, _t in w if not o)
        slow = median is not None and st["base"] is not None and median >= max(SLOW_MEDIAN * st["base"], 5.0)
        if (errors / len(w) >= ERROR_SHARE or slow) and st["since"] >= 5 and st["factor"] < MAX_FACTOR:
            st["factor"] = min(MAX_FACTOR, st["factor"] * 2)
            st["since"] = 0
            why = f"{errors} errors in the last {len(w)} pages" + (f", median response {median:.1f} s" if slow else "")
            return "slowed_down", f"slowed down {host}: {why}; delay now {self._range(host)}"
        if errors == 0 and not slow and st["factor"] > 1 and st["since"] >= 10:
            st["factor"] = max(1.0, st["factor"] / 1.5)
            st["since"] = 0
            return "relaxed", f"relaxed {host}: no errors in the last {len(w)} pages; delay now {self._range(host)}"
        return None

    def _range(self, host: str) -> str:
        f = self.factor(host)
        lo, hi = self.delay_min * f, self.delay_max * f
        floor = 0.5 * (f - 1)
        return f"{max(lo, floor):.1f}–{max(hi, floor):.1f} s" + (" (one page at a time)" if f > 1 else "")

    def _median(self, host: str) -> float:
        times = sorted(t for _o, t in self._obs.get(host, ()) if t is not None)
        return times[len(times) // 2] if times else 0.0

    def set_crawl_delay(self, host: str, seconds: float | None) -> None:
        if seconds:
            self._crawl_delay[host] = min(float(seconds), 60.0)

    def backoff(self, host: str, retry_after: float | None = None) -> float:
        """Called on 429: doubles the host's back-off (2 s .. 120 s) or honours Retry-After."""
        current = self._backoff.get(host, 1.0) * 2
        current = min(max(current, 2.0), 120.0)
        if retry_after is not None:
            current = min(max(retry_after, 1.0), 300.0)
        self._backoff[host] = current
        self._next[host] = max(self._next.get(host, 0.0), time.monotonic() + current)
        return current

    def success(self, host: str) -> None:
        if host in self._backoff:
            self._backoff[host] = max(1.0, self._backoff[host] / 2)

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            start = max(now, self._next.get(host, 0.0))
            gap = random.uniform(self.delay_min, self.delay_max)
            f = self.factor(host)
            if f > 1:
                # slowed down: the delay widened, at least 0.5 s per step, and no new page of the host
                # starts before the usual answer time has passed (one page at a time)
                gap = max(gap * f, 0.5 * (f - 1), self._median(host))
            gap = max(gap, self._crawl_delay.get(host, 0.0))
            self._next[host] = start + gap
        delay = start - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
