from __future__ import annotations

from pathlib import Path

import pytest
import redis
from pydantic import ValidationError

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.chaos import (
    _scenario_passed,
    build_gateway,
    load_queries,
    run_scenario,
    run_simulation,
)
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import (
    CacheConfig,
    CircuitBreakerConfig,
    LabConfig,
    LoadTestConfig,
    ProviderConfig,
    ScenarioConfig,
)
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics, percentile
from reliability_lab.providers import FakeLLMProvider
from reliability_lab.redis_circuit_breaker import SharedRedisCircuitBreaker


def _config(
    *,
    requests: int = 3,
    cache_enabled: bool = True,
    reset_timeout_seconds: float = 0.05,
) -> LabConfig:
    return LabConfig(
        providers=[
            ProviderConfig(
                name="primary",
                fail_rate=0.0,
                base_latency_ms=1,
                cost_per_1k_tokens=0.01,
            ),
            ProviderConfig(
                name="backup",
                fail_rate=0.0,
                base_latency_ms=1,
                cost_per_1k_tokens=0.005,
            ),
        ],
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=1,
            reset_timeout_seconds=reset_timeout_seconds,
            success_threshold=1,
        ),
        cache=CacheConfig(
            enabled=cache_enabled,
            ttl_seconds=60,
            similarity_threshold=0.9,
        ),
        load_test=LoadTestConfig(requests=requests, random_seed=7, concurrency=1),
    )


def _redis_available() -> bool:
    try:
        client = redis.Redis.from_url("redis://localhost:6379/0")
        client.ping()
        client.close()
        return True
    except redis.RedisError:
        return False


def test_cache_hit_reports_actual_provider_cost_and_route_reason() -> None:
    provider = FakeLLMProvider("primary", 0.0, 1, 0.01, random_seed=7)
    gateway = ReliabilityGateway(
        [provider],
        {"primary": CircuitBreaker("primary", 3, 1)},
        ResponseCache(60, 0.9),
    )

    original = gateway.complete("cost-accounted query")
    cached = gateway.complete("cost-accounted query")

    assert original.estimated_cost > 0
    assert cached.estimated_cost_saved == pytest.approx(original.estimated_cost)
    assert original.route_reason == "provider=primary;tier=primary"
    assert "source_provider=primary" in (cached.route_reason or "")


def test_scenario_separates_end_to_end_and_provider_latency() -> None:
    metrics = run_scenario(
        _config(requests=3),
        ["same query"],
        ScenarioConfig(name="quality", provider_overrides={"primary": 0.0}),
    )

    assert metrics.cache_hits == 2
    assert metrics.primary_successes == 1
    assert metrics.estimated_cost_saved > 0
    assert len(metrics.latencies_ms) == 3
    assert len(metrics.provider_latencies_ms) == 1
    assert metrics.percentile(50) < metrics.provider_percentile(50)


@pytest.mark.parametrize(
    ("name", "metrics"),
    [
        (
            "primary_timeout_100",
            RunMetrics(
                total_requests=10,
                successful_requests=10,
                primary_attempts=3,
                fallback_successes=10,
                circuit_open_count=1,
            ),
        ),
        (
            "primary_flaky_50",
            RunMetrics(
                total_requests=10,
                successful_requests=10,
                primary_successes=5,
                fallback_successes=5,
                circuit_open_count=1,
            ),
        ),
        (
            "all_healthy",
            RunMetrics(
                total_requests=10,
                successful_requests=10,
                primary_successes=10,
            ),
        ),
        (
            "primary_degraded_80",
            RunMetrics(
                total_requests=10,
                successful_requests=10,
                fallback_successes=8,
                circuit_open_count=1,
            ),
        ),
        (
            "primary_recovers",
            RunMetrics(
                total_requests=10,
                successful_requests=10,
                primary_successes=5,
                fallback_successes=5,
                circuit_open_count=1,
                circuit_close_count=1,
                recovery_time_ms=100.0,
            ),
        ),
    ],
)
def test_named_scenarios_require_their_intended_behavior(
    name: str,
    metrics: RunMetrics,
) -> None:
    assert _scenario_passed(name, metrics)


def test_flaky_scenario_does_not_pass_without_fallback_or_open() -> None:
    metrics = RunMetrics(
        total_requests=10,
        successful_requests=10,
        primary_successes=10,
    )

    assert not _scenario_passed("primary_flaky_50", metrics)


def test_recovery_scenario_records_close_transition() -> None:
    scenario = ScenarioConfig(
        name="primary_recovers",
        provider_overrides={"primary": 1.0, "backup": 0.0},
        recover_after_requests=2,
        recovered_provider_overrides={"primary": 0.0, "backup": 0.0},
    )
    metrics = run_scenario(
        _config(requests=6, cache_enabled=False),
        load_queries()[:2],
        scenario,
    )

    assert metrics.circuit_open_count >= 1
    assert metrics.circuit_close_count >= 1
    assert metrics.recovery_time_ms is not None


def test_default_simulation_and_empty_query_validation() -> None:
    config = _config(requests=2, cache_enabled=False)
    metrics = run_simulation(config, ["query"])

    assert metrics.scenarios == {"default": "pass"}
    with pytest.raises(ValueError, match="At least one query"):
        run_scenario(config, [], ScenarioConfig(name="empty"))


def test_multi_scenario_simulation_aggregates_every_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenarios = [ScenarioConfig(name="one"), ScenarioConfig(name="two")]
    config = _config(requests=2, cache_enabled=False).model_copy(
        update={"scenarios": scenarios}
    )
    scenario_results = {
        "one": RunMetrics(
            total_requests=2,
            successful_requests=2,
            provider_attempts=2,
            primary_attempts=2,
            primary_successes=2,
            cache_hits=1,
            circuit_open_count=1,
            circuit_close_count=1,
            estimated_cost=0.2,
            estimated_cost_saved=0.1,
            duration_ms=20,
            latencies_ms=[1, 2],
            provider_latencies_ms=[2],
            recovery_time_ms=100,
        ),
        "two": RunMetrics(
            total_requests=2,
            successful_requests=1,
            failed_requests=1,
            provider_attempts=3,
            primary_attempts=2,
            fallback_attempts=1,
            fallback_successes=1,
            static_fallbacks=1,
            cache_hits=1,
            circuit_open_count=1,
            estimated_cost=0.3,
            estimated_cost_saved=0.2,
            duration_ms=30,
            latencies_ms=[3, 4],
            provider_latencies_ms=[3],
            recovery_time_ms=300,
        ),
    }

    def fake_run_scenario(
        _config: LabConfig,
        _queries: list[str],
        scenario: ScenarioConfig,
    ) -> RunMetrics:
        return scenario_results[scenario.name]

    monkeypatch.setattr("reliability_lab.chaos.run_scenario", fake_run_scenario)
    metrics = run_simulation(config, ["query"])

    assert metrics.total_requests == 4
    assert metrics.successful_requests == 3
    assert metrics.failed_requests == 1
    assert metrics.provider_attempts == 5
    assert metrics.primary_attempts == 4
    assert metrics.fallback_attempts == 1
    assert metrics.primary_successes == 2
    assert metrics.fallback_successes == 1
    assert metrics.static_fallbacks == 1
    assert metrics.cache_hits == 2
    assert metrics.circuit_open_count == 2
    assert metrics.circuit_close_count == 1
    assert metrics.estimated_cost == pytest.approx(0.5)
    assert metrics.estimated_cost_saved == pytest.approx(0.3)
    assert metrics.duration_ms == 50
    assert metrics.latencies_ms == [1, 2, 3, 4]
    assert metrics.provider_latencies_ms == [2, 3]
    assert metrics.recovery_time_ms == 200
    assert metrics.scenarios == {"one": "pass", "two": "fail"}
    assert set(metrics.scenario_metrics) == {"one", "two"}


def test_static_fallback_is_counted_as_failed_request() -> None:
    metrics = run_scenario(
        _config(requests=2, cache_enabled=False),
        ["unique query"],
        ScenarioConfig(
            name="all_down",
            provider_overrides={"primary": 1.0, "backup": 1.0},
        ),
    )

    assert metrics.static_fallbacks == 2
    assert metrics.failed_requests == 2
    assert metrics.successful_requests == 0


def test_validation_and_empty_value_branches(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        ResponseCache(0, 0.5)
    with pytest.raises(ValueError, match="similarity_threshold"):
        ResponseCache(1, 1.1)
    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreaker("invalid", 0, 1)
    with pytest.raises(ValueError, match="reset_timeout_seconds"):
        CircuitBreaker("invalid", 1, 0)
    with pytest.raises(ValueError, match="success_threshold"):
        CircuitBreaker("invalid", 1, 1, success_threshold=0)
    with pytest.raises(ValueError, match="max_cost"):
        ReliabilityGateway([], {}, max_cost=0)
    with pytest.raises(ValueError, match="soft_limit_ratio"):
        ReliabilityGateway([], {}, soft_limit_ratio=1)
    with pytest.raises(ValueError, match="thresholds"):
        SharedRedisCircuitBreaker("invalid", 0, 1)
    with pytest.raises(ValueError, match="timeouts"):
        SharedRedisCircuitBreaker("invalid", 1, 0)

    assert ResponseCache.similarity("", "non-empty") == 0
    assert percentile([], 95) == 0
    assert SharedRedisCache._decode_metadata(None) == {}
    assert SharedRedisCache._decode_metadata("not-json") == {}
    assert SharedRedisCache._decode_metadata("[]") == {}
    assert SharedRedisCache._decode_metadata('{"cost": 1}') == {"cost": "1"}

    bad_metadata_cache = ResponseCache(60, 0.9)
    bad_metadata_cache.set("bad cost", "cached", {"estimated_cost": "invalid"})
    cached = ReliabilityGateway([], {}, bad_metadata_cache).complete("bad cost")
    assert cached.estimated_cost_saved == 0
    assert "source_provider=unknown" in (cached.route_reason or "")

    queries_path = tmp_path / "queries.jsonl"
    queries_path.write_text('\n{"query": "kept"}\n', encoding="utf-8")
    assert load_queries(queries_path) == ["kept"]

    metrics_path = tmp_path / "nested" / "metrics.json"
    RunMetrics(total_requests=1, successful_requests=1).write_json(metrics_path)
    assert metrics_path.is_file()


@pytest.mark.skipif(not _redis_available(), reason="Redis is not running")
def test_build_gateway_supports_redis_backends() -> None:
    config = _config(requests=1).model_copy(
        update={
            "providers": [
                ProviderConfig(
                    name="quality-redis-provider",
                    fail_rate=0,
                    base_latency_ms=1,
                    cost_per_1k_tokens=0.001,
                )
            ],
            "circuit_breaker": CircuitBreakerConfig(
                failure_threshold=1,
                reset_timeout_seconds=0.05,
                success_threshold=1,
                backend="redis",
            ),
            "cache": CacheConfig(
                enabled=True,
                backend="redis",
                ttl_seconds=60,
                similarity_threshold=0.9,
            ),
        }
    )
    gateway = build_gateway(config)
    breaker = gateway.breakers["quality-redis-provider"]

    assert isinstance(gateway.cache, SharedRedisCache)
    assert isinstance(breaker, SharedRedisCircuitBreaker)
    gateway.cache.flush()
    try:
        result = gateway.complete("redis-backed quality branch")
        assert result.provider == "quality-redis-provider"
    finally:
        breaker.flush()
        breaker.close()
        gateway.cache.flush()
        gateway.cache.close()


def test_invalid_cache_backend_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CacheConfig.model_validate(
            {
                "enabled": True,
                "backend": "unknown",
                "ttl_seconds": 60,
                "similarity_threshold": 0.9,
            }
        )


@pytest.mark.skipif(not _redis_available(), reason="Redis is not running")
def test_redis_shares_cost_metadata_across_instances() -> None:
    first = SharedRedisCache("redis://localhost:6379/0", 60, 0.9, prefix="rl:test:metadata:")
    second = SharedRedisCache("redis://localhost:6379/0", 60, 0.9, prefix="rl:test:metadata:")
    first.flush()
    try:
        first.set("metadata query", "response", {"estimated_cost": "0.0042"})
        lookup = second.lookup("metadata query")

        assert lookup.value == "response"
        assert lookup.metadata["estimated_cost"] == "0.0042"
    finally:
        first.flush()
        first.close()
        second.close()
