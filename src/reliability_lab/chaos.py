from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider
from reliability_lab.redis_circuit_breaker import SharedRedisCircuitBreaker


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for provider_index, p in enumerate(config.providers):
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(
            FakeLLMProvider(
                p.name,
                fail_rate,
                p.base_latency_ms,
                p.cost_per_1k_tokens,
                random_seed=config.load_test.random_seed + provider_index,
            )
        )
    breakers: dict[str, CircuitBreaker | SharedRedisCircuitBreaker]
    if config.circuit_breaker.backend == "redis":
        breakers = {
            p.name: SharedRedisCircuitBreaker(
                name=p.name,
                failure_threshold=config.circuit_breaker.failure_threshold,
                reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
                success_threshold=config.circuit_breaker.success_threshold,
                redis_url=config.circuit_breaker.redis_url,
            )
            for p in config.providers
        }
    else:
        breakers = {
            p.name: CircuitBreaker(
                name=p.name,
                failure_threshold=config.circuit_breaker.failure_threshold,
                reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
                success_threshold=config.circuit_breaker.success_threshold,
            )
            for p in config.providers
        }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(
        providers,
        breakers,
        cache,
        max_cost=config.budget.max_cost if config.budget.enabled else None,
        soft_limit_ratio=config.budget.soft_limit_ratio,
    )


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Return mean OPEN-to-CLOSED recovery time from breaker transition logs."""
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        opened_at: float | None = None
        for transition in breaker.transition_log:
            transition_time = float(transition["ts"])
            if transition["to"] == "open":
                opened_at = transition_time
            elif transition["to"] == "closed" and opened_at is not None:
                recovery_times.append((transition_time - opened_at) * 1000)
                opened_at = None

    return sum(recovery_times) / len(recovery_times) if recovery_times else None


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run one reproducible sequential or concurrent chaos scenario."""
    if not queries:
        raise ValueError("At least one query is required to run a scenario")

    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()

    scenario_seed = config.load_test.random_seed + sum(map(ord, scenario.name))
    query_random = random.Random(scenario_seed)
    prompts = [query_random.choice(queries) for _ in range(config.load_test.requests)]

    try:
        def invoke(prompt: str) -> tuple[GatewayResponse, float]:
            request_started_at = time.perf_counter()
            response = gateway.complete(prompt)
            elapsed_ms = (time.perf_counter() - request_started_at) * 1000
            return response, elapsed_ms

        def execute(batch: list[str]) -> list[tuple[GatewayResponse, float]]:
            if config.load_test.concurrency == 1:
                return [invoke(prompt) for prompt in batch]
            with ThreadPoolExecutor(max_workers=config.load_test.concurrency) as executor:
                return list(executor.map(invoke, batch))

        started_at = time.perf_counter()
        recovery_point = scenario.recover_after_requests
        if recovery_point is not None and recovery_point < len(prompts):
            results = execute(prompts[:recovery_point])
            for provider in gateway.providers:
                if provider.name in scenario.recovered_provider_overrides:
                    provider.fail_rate = scenario.recovered_provider_overrides[provider.name]
            time.sleep(config.circuit_breaker.reset_timeout_seconds + 0.05)
            results.extend(execute(prompts[recovery_point:]))
        else:
            results = execute(prompts)
        metrics.duration_ms = (time.perf_counter() - started_at) * 1000

        primary_name = gateway.providers[0].name if gateway.providers else None
        for result, request_latency_ms in results:
            metrics.total_requests += 1
            metrics.estimated_cost += result.estimated_cost
            metrics.estimated_cost_saved += result.estimated_cost_saved
            metrics.latencies_ms.append(request_latency_ms)

            metrics.provider_attempts += len(result.attempted_providers)
            metrics.primary_attempts += sum(
                provider_name == primary_name
                for provider_name in result.attempted_providers
            )
            metrics.fallback_attempts += sum(
                provider_name != primary_name
                for provider_name in result.attempted_providers
            )

            if result.cache_hit:
                metrics.cache_hits += 1

            if result.route == "primary":
                metrics.primary_successes += 1
                metrics.successful_requests += 1
            elif result.route == "fallback":
                metrics.fallback_successes += 1
                metrics.successful_requests += 1
            elif result.route == "static_fallback":
                metrics.static_fallbacks += 1
                metrics.failed_requests += 1
            else:
                metrics.successful_requests += 1

            if result.latency_ms > 0:
                metrics.provider_latencies_ms.append(result.latency_ms)

        metrics.circuit_open_count = sum(
            transition["to"] == "open"
            for breaker in gateway.breakers.values()
            for transition in breaker.transition_log
        )
        metrics.circuit_close_count = sum(
            transition["to"] == "closed"
            for breaker in gateway.breakers.values()
            for transition in breaker.transition_log
        )
        metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
        return metrics
    finally:
        close_cache = getattr(gateway.cache, "close", None)
        if callable(close_cache):
            close_cache()
        for breaker in gateway.breakers.values():
            close_breaker = getattr(breaker, "close", None)
            if callable(close_breaker):
                close_breaker()


def _scenario_passed(name: str, metrics: RunMetrics) -> bool:
    """Evaluate a scenario against the behavior it is intended to prove."""
    if name == "primary_timeout_100":
        return (
            metrics.availability >= 0.99
            and metrics.primary_attempts > 0
            and metrics.primary_successes == 0
            and metrics.fallback_successes > 0
            and metrics.circuit_open_count > 0
            and metrics.static_fallbacks == 0
        )
    if name == "all_healthy":
        return (
            metrics.availability == 1.0
            and metrics.primary_successes > 0
            and metrics.fallback_successes == 0
            and metrics.static_fallbacks == 0
            and metrics.circuit_open_count == 0
        )
    if name == "primary_flaky_50":
        return (
            metrics.availability >= 0.99
            and metrics.primary_successes > 0
            and metrics.fallback_successes > 0
            and metrics.circuit_open_count > 0
        )
    if name == "primary_degraded_80":
        return (
            metrics.availability >= 0.99
            and metrics.fallback_successes > 0
            and metrics.circuit_open_count > 0
        )
    if name == "primary_recovers":
        return (
            metrics.availability >= 0.99
            and metrics.recovery_time_ms is not None
            and metrics.circuit_open_count > 0
            and metrics.circuit_close_count > 0
            and metrics.primary_successes > 0
            and metrics.fallback_successes > 0
        )
    return metrics.availability >= 0.95


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run and aggregate all configured scenarios or a default baseline."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    recovery_times: list[float] = []
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        passed = _scenario_passed(scenario.name, result)
        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.scenario_metrics[scenario.name] = result.to_report_dict()

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.provider_attempts += result.provider_attempts
        combined.primary_attempts += result.primary_attempts
        combined.fallback_attempts += result.fallback_attempts
        combined.primary_successes += result.primary_successes
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.circuit_close_count += result.circuit_close_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.duration_ms += result.duration_ms
        combined.latencies_ms.extend(result.latencies_ms)
        combined.provider_latencies_ms.extend(result.provider_latencies_ms)
        if result.recovery_time_ms is not None:
            recovery_times.append(result.recovery_time_ms)

    if recovery_times:
        combined.recovery_time_ms = sum(recovery_times) / len(recovery_times)

    return combined
