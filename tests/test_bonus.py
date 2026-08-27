from reliability_lab.cache import ResponseCache
from reliability_lab.chaos import run_scenario
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
from reliability_lab.providers import FakeLLMProvider


def _load_config(concurrency: int) -> LabConfig:
    return LabConfig(
        providers=[
            ProviderConfig(
                name="primary",
                fail_rate=0.0,
                base_latency_ms=30,
                cost_per_1k_tokens=0.001,
            )
        ],
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=3,
            reset_timeout_seconds=1,
            success_threshold=1,
        ),
        cache=CacheConfig(
            enabled=False,
            ttl_seconds=60,
            similarity_threshold=0.9,
        ),
        load_test=LoadTestConfig(
            requests=8,
            random_seed=42,
            concurrency=concurrency,
        ),
    )


def test_concurrent_load_improves_wall_clock_throughput() -> None:
    scenario = ScenarioConfig(name="healthy", provider_overrides={"primary": 0.0})
    sequential = run_scenario(_load_config(1), ["query"], scenario)
    concurrent = run_scenario(_load_config(4), ["query"], scenario)

    assert concurrent.successful_requests == sequential.successful_requests == 8
    assert concurrent.duration_ms < sequential.duration_ms
    assert concurrent.throughput_rps > sequential.throughput_rps


def test_half_open_allows_only_one_probe_at_a_time() -> None:
    breaker = CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=1)
    breaker.record_failure()
    breaker.opened_at = time.monotonic() - 2

    assert breaker.allow_request()
    assert not breaker.allow_request()

    breaker.record_success()
    assert breaker.state.value == "closed"


def test_cost_budget_uses_cheaper_provider_then_stops() -> None:
    expensive = FakeLLMProvider("primary", 0.0, 1, 0.02, random_seed=1)
    cheap = FakeLLMProvider("backup", 0.0, 1, 0.001, random_seed=2)
    breakers = {
        provider.name: CircuitBreaker(provider.name, 3, 1)
        for provider in [expensive, cheap]
    }
    gateway = ReliabilityGateway(
        [expensive, cheap],
        breakers,
        ResponseCache(60, 0.9),
        max_cost=1.0,
        soft_limit_ratio=0.8,
    )

    first = gateway.complete("first unique prompt")
    gateway.max_cost = gateway.cumulative_cost / 0.9
    second = gateway.complete("second unique prompt")
    gateway.max_cost = gateway.cumulative_cost
    exhausted = gateway.complete("third unique prompt")

    assert first.provider == "primary"
    assert second.provider == "backup"
    assert second.route == "fallback"
    assert exhausted.route == "static_fallback"
    assert exhausted.error == "Cost budget exhausted"
import time
