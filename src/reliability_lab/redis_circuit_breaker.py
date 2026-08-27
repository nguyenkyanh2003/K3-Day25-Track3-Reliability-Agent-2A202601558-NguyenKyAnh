from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any, TypeVar

from reliability_lab.circuit_breaker import CircuitOpenError, CircuitState

T = TypeVar("T")


class SharedRedisCircuitBreaker:
    """Circuit breaker whose state and counters are shared through Redis."""

    def __init__(
        self,
        name: str,
        failure_threshold: int,
        reset_timeout_seconds: float,
        success_threshold: int = 1,
        redis_url: str = "redis://localhost:6379/0",
        prefix: str = "rl:circuit:",
        state_ttl_seconds: int = 3600,
    ) -> None:
        import redis as redis_lib

        if failure_threshold <= 0 or success_threshold <= 0:
            raise ValueError("Circuit breaker thresholds must be greater than zero")
        if reset_timeout_seconds <= 0 or state_ttl_seconds <= 0:
            raise ValueError("Circuit breaker timeouts must be greater than zero")

        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self.success_threshold = success_threshold
        self.state_ttl_seconds = state_ttl_seconds
        self.transition_log: list[dict[str, str | float]] = []
        self._base_key = f"{prefix}{name}"
        self._state_key = f"{self._base_key}:state"
        self._failure_key = f"{self._base_key}:failures"
        self._success_key = f"{self._base_key}:successes"
        self._opened_key = f"{self._base_key}:opened_at"
        self._probe_key = f"{self._base_key}:probe"
        self._transition_lock_key = f"{self._base_key}:transition_lock"
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._redis.set(self._state_key, CircuitState.CLOSED.value, nx=True)
        self._touch(self._state_key)

    @property
    def state(self) -> CircuitState:
        value = self._redis.get(self._state_key) or CircuitState.CLOSED.value
        return CircuitState(str(value))

    @property
    def failure_count(self) -> int:
        return int(self._redis.get(self._failure_key) or 0)

    @property
    def success_count(self) -> int:
        return int(self._redis.get(self._success_key) or 0)

    def allow_request(self) -> bool:
        state = self.state
        if state == CircuitState.CLOSED:
            return True
        if state == CircuitState.HALF_OPEN:
            return self._acquire_probe()

        opened_at = float(self._redis.get(self._opened_key) or time.time())
        if time.time() - opened_at < self.reset_timeout_seconds:
            return False

        acquired_lock = bool(
            self._redis.set(self._transition_lock_key, "1", nx=True, ex=5)
        )
        if not acquired_lock:
            return False
        try:
            if self.state != CircuitState.OPEN:
                return False
            self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
            return self._acquire_probe()
        finally:
            self._redis.delete(self._transition_lock_key)

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
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
        state = self.state
        with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.delete(self._failure_key)
            pipeline.incr(self._success_key)
            pipeline.expire(self._success_key, self.state_ttl_seconds)
            results = pipeline.execute()
        successes = int(results[1])
        self._redis.delete(self._probe_key)

        if state == CircuitState.HALF_OPEN and successes >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self._redis.delete(self._success_key, self._opened_key)

    def record_failure(self) -> None:
        state = self.state
        with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.incr(self._failure_key)
            pipeline.expire(self._failure_key, self.state_ttl_seconds)
            pipeline.delete(self._success_key, self._probe_key)
            results = pipeline.execute()
        failures = int(results[0])

        if state == CircuitState.HALF_OPEN:
            self._open("probe_failure")
        elif state == CircuitState.CLOSED and failures >= self.failure_threshold:
            self._open("failure_threshold_reached")

    def flush(self) -> None:
        for key in self._redis.scan_iter(f"{self._base_key}:*"):
            self._redis.delete(key)
        self._redis.set(self._state_key, CircuitState.CLOSED.value)
        self._touch(self._state_key)
        self.transition_log.clear()

    def close(self) -> None:
        self._redis.close()

    def _open(self, reason: str) -> None:
        self._redis.set(self._opened_key, time.time(), ex=self.state_ttl_seconds)
        self._transition(CircuitState.OPEN, reason)

    def _acquire_probe(self) -> bool:
        probe_ttl = max(1, math.ceil(self.reset_timeout_seconds))
        return bool(self._redis.set(self._probe_key, "1", nx=True, ex=probe_ttl))

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        old_state = self.state
        if old_state == new_state:
            return
        self._redis.set(
            self._state_key,
            new_state.value,
            ex=self.state_ttl_seconds,
        )
        self.transition_log.append(
            {
                "breaker": self.name,
                "from": old_state.value,
                "to": new_state.value,
                "reason": reason,
                "ts": time.time(),
            }
        )

    def _touch(self, key: str) -> None:
        self._redis.expire(key, self.state_ttl_seconds)
