"""
Thread-safe circuit breaker for VLM endpoint failover.

States:
  CLOSED     → normal operation, requests pass through
  OPEN       → tripped after consecutive failures, all calls rejected
  HALF_OPEN  → cooldown elapsed, testing with a single probe request
"""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Optional


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Thread-safe circuit breaker with configurable threshold and cooldown."""

    def __init__(
        self,
        name: str = "vlm",
        threshold: int = 3,
        cooldown_seconds: float = 60.0,
        max_wait: float = 120.0,
    ):
        self.name = name
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.max_wait = max_wait

        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._last_failure_time: float = 0.0
        self._last_trip_time: float = 0.0
        self._last_failure_reason: str = ""

        # Metrics
        self.total_trips = 0
        self.total_failures = 0
        self.total_successes = 0
        self.total_rejected = 0

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_transition()
            return self._state.value

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        with self._lock:
            self._maybe_transition()
            if self._state == BreakerState.CLOSED:
                return True
            if self._state == BreakerState.HALF_OPEN:
                return True
            # OPEN
            self.total_rejected += 1
            return False

    def record_success(self) -> None:
        """Record a successful request. Resets breaker to CLOSED."""
        with self._lock:
            self._consecutive_failures = 0
            self._state = BreakerState.CLOSED
            self.total_successes += 1

    def record_failure(self, reason: str = "") -> None:
        """Record a failed request. May trip breaker to OPEN."""
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()
            self._last_failure_reason = reason
            self.total_failures += 1

            if self._state == BreakerState.HALF_OPEN:
                self._trip()
            elif self._consecutive_failures >= self.threshold:
                self._trip()

    def reset(self) -> None:
        """Force reset the breaker to CLOSED state."""
        with self._lock:
            self._state = BreakerState.CLOSED
            self._consecutive_failures = 0

    def _trip(self) -> None:
        """Trip the breaker to OPEN state. Must hold _lock."""
        self._state = BreakerState.OPEN
        self._last_trip_time = time.monotonic()
        self.total_trips += 1

    def _maybe_transition(self) -> None:
        """Auto-transition OPEN → HALF_OPEN after cooldown. Must hold _lock."""
        if self._state == BreakerState.OPEN:
            elapsed = time.monotonic() - self._last_trip_time
            if elapsed >= self.cooldown_seconds:
                self._state = BreakerState.HALF_OPEN

    def status(self) -> dict:
        """Return current breaker status for observability."""
        with self._lock:
            self._maybe_transition()
            return {
                "name": self.name,
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "threshold": self.threshold,
                "cooldown_seconds": self.cooldown_seconds,
                "total_trips": self.total_trips,
                "total_failures": self.total_failures,
                "total_successes": self.total_successes,
                "total_rejected": self.total_rejected,
                "last_failure_reason": self._last_failure_reason,
            }
