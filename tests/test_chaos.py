from reliability_lab.chaos import calculate_recovery_time_ms, run_scenario
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


def _config(requests: int = 4) -> LabConfig:
    return LabConfig(
        providers=[
            ProviderConfig(
                name="primary",
                fail_rate=0.0,
                base_latency_ms=1,
                cost_per_1k_tokens=0.001,
            )
        ],
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=2,
            reset_timeout_seconds=1,
            success_threshold=1,
        ),
        cache=CacheConfig(
            enabled=False,
            ttl_seconds=60,
            similarity_threshold=0.9,
        ),
        load_test=LoadTestConfig(requests=requests, random_seed=42),
    )


def test_run_scenario_collects_success_metrics() -> None:
    metrics = run_scenario(
        _config(),
        ["test query"],
        ScenarioConfig(name="healthy", provider_overrides={"primary": 0.0}),
    )

    assert metrics.total_requests == 4
    assert metrics.successful_requests == 4
    assert metrics.failed_requests == 0
    assert len(metrics.latencies_ms) == 4


def test_calculate_recovery_time_averages_transitions() -> None:
    breaker = CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=1)
    breaker.transition_log = [
        {"from": "closed", "to": "open", "reason": "failure", "ts": 10.0},
        {"from": "open", "to": "half_open", "reason": "probe", "ts": 11.0},
        {"from": "half_open", "to": "closed", "reason": "success", "ts": 12.5},
    ]
    gateway = ReliabilityGateway([], {"primary": breaker})

    assert calculate_recovery_time_ms(gateway) == 2500.0
