"""Process-local pacing and shared cooldown for one upstream API key."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from threading import Lock
from time import monotonic, sleep


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """RFC delay-seconds or HTTP-date. Invalid/negative values use caller fallback."""
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        try:
            seconds = float(value)
            return seconds if math.isfinite(seconds) else None
        except ValueError:
            return None
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            return None
        seconds = (target - (now or datetime.now(timezone.utc))).total_seconds()
        return max(0.0, seconds)
    except (TypeError, ValueError, OverflowError):
        return None


class AdmissionTimeout(TimeoutError):
    """No request was sent: pacing/cooldown could not fit the time budget."""

    def __init__(self, wait_seconds: float):
        super().__init__("请求限流等待超出本次时间预算")
        self.wait_seconds = wait_seconds


class RequestGate:
    def __init__(self, *, clock=monotonic, sleeper=sleep):
        self._clock = clock
        self._sleep = sleeper
        self._lock = Lock()
        self._next_start = 0.0
        self._cooldown_until = 0.0

    def defer(self, seconds: float) -> None:
        with self._lock:
            # Concurrent 429s can extend, never shorten, the shared cooldown.
            self._cooldown_until = max(self._cooldown_until, self._clock() + seconds)

    def acquire(self, *, interval: float, budget: float) -> None:
        end = self._clock() + budget
        while True:
            with self._lock:
                now = self._clock()
                ready = max(self._next_start, self._cooldown_until)
                if now >= end or ready >= end:
                    raise AdmissionTimeout(max(0.0, ready - now))
                if now >= ready:
                    self._next_start = now + interval
                    return
                delay = ready - now
            # Recheck shared cooldown after waking, without holding the lock.
            self._sleep(min(delay, 0.25))
