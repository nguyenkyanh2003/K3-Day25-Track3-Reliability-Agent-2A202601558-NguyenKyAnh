from __future__ import annotations

import time

import pytest
import redis

from reliability_lab.circuit_breaker import CircuitState
from reliability_lab.redis_circuit_breaker import SharedRedisCircuitBreaker


def _redis_available() -> bool:
    try:
        client = redis.Redis.from_url("redis://localhost:6379/0")
        client.ping()
        client.close()
        return True
    except redis.RedisError:
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason="Redis is not running")


def test_circuit_state_is_shared_across_instances() -> None:
    first = SharedRedisCircuitBreaker(
        "shared-test",
        failure_threshold=2,
        reset_timeout_seconds=0.05,
        prefix="rl:test:circuit:",
    )
    second = SharedRedisCircuitBreaker(
        "shared-test",
        failure_threshold=2,
        reset_timeout_seconds=0.05,
        prefix="rl:test:circuit:",
    )
    first.flush()
    try:
        first.record_failure()
        assert second.failure_count == 1

        second.record_failure()
        assert first.state == CircuitState.OPEN

        time.sleep(0.1)
        assert first.allow_request()
        assert not second.allow_request()

        first.record_success()
        assert second.state == CircuitState.CLOSED
    finally:
        first.flush()
        first.close()
        second.close()
