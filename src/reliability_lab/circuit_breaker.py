from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Thread-safe CLOSED/OPEN/HALF_OPEN circuit state machine."""

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _half_open_probe_in_flight: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.failure_threshold <= 0:
            raise ValueError("failure_threshold must be greater than zero")
        if self.reset_timeout_seconds <= 0:
            raise ValueError("reset_timeout_seconds must be greater than zero")
        if self.success_threshold <= 0:
            raise ValueError("success_threshold must be greater than zero")

    def allow_request(self) -> bool:
        """Allow normal calls while CLOSED and a single recovery probe when eligible."""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True
            if self.state == CircuitState.HALF_OPEN:
                if self._half_open_probe_in_flight:
                    return False
                self._half_open_probe_in_flight = True
                return True

            if self.opened_at is None:
                return False

            elapsed = time.monotonic() - self.opened_at
            if elapsed < self.reset_timeout_seconds:
                return False

            self.success_count = 0
            self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
            self._half_open_probe_in_flight = True
            return True

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Execute a callable and update circuit state from its outcome."""
        if not self.allow_request():
            raise CircuitOpenError(f"Circuit '{self.name}' is open")

        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise

        self.record_success()
        return result

    def record_success(self) -> None:
        """Reset failures and close a recovered circuit after enough probes."""
        with self._lock:
            self.failure_count = 0
            self.success_count += 1
            self._half_open_probe_in_flight = False

            if (
                self.state == CircuitState.HALF_OPEN
                and self.success_count >= self.success_threshold
            ):
                self._transition(CircuitState.CLOSED, "probe_success")
                self.success_count = 0
                self.opened_at = None

    def record_failure(self) -> None:
        """Increment failures and open or re-open the circuit when required."""
        with self._lock:
            self.failure_count += 1
            self.success_count = 0
            self._half_open_probe_in_flight = False

            if self.state == CircuitState.HALF_OPEN:
                self.opened_at = time.monotonic()
                self._transition(CircuitState.OPEN, "probe_failure")
            elif (
                self.state == CircuitState.CLOSED
                and self.failure_count >= self.failure_threshold
            ):
                self.opened_at = time.monotonic()
                self._transition(CircuitState.OPEN, "failure_threshold_reached")

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {
                "breaker": self.name,
                "from": self.state.value,
                "to": new_state.value,
                "reason": reason,
                "ts": time.time(),
            }
        )
        self.state = new_state
