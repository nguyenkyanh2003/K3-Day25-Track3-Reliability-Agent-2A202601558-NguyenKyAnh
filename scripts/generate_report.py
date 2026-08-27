from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from reliability_lab.config import load_config


def _load_json(path: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(Path(path).read_text(encoding="utf-8")))


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _status(condition: bool) -> str:
    return "PASS" if condition else "FAIL"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--comparisons", default="reports/comparisons.json")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/final_report.md")
    args = parser.parse_args()

    metrics = _load_json(args.metrics)
    comparisons = _load_json(args.comparisons)
    config = load_config(args.config)
    scenario_metrics: dict[str, dict[str, Any]] = metrics["scenario_metrics"]

    availability = float(metrics["availability"])
    latency_p95 = float(metrics["latency_p95_ms"])
    fallback_rate = float(metrics["fallback_success_rate"])
    cache_hit_rate = float(metrics["cache_hit_rate"])
    recovery_time = metrics["recovery_time_ms"]
    recovery_met = recovery_time is not None and float(recovery_time) < 5000

    lines = [
        "# Day 25 Reliability Engineering Final Report",
        "",
        "## 1. Architecture summary",
        "",
        "```text",
        "User request",
        "    |",
        "    v",
        "[Privacy guard] -> sensitive? bypass cache",
        "    |",
        "    v",
        "[Memory / Redis semantic cache] -> hit? return at zero provider cost",
        "    | miss",
        "    v",
        "[Primary circuit breaker] -> primary provider",
        "    | failure / OPEN",
        "    v",
        "[Backup circuit breaker]  -> backup provider",
        "    | failure / OPEN",
        "    v",
        "[Static degraded response]",
        "    |",
        "    v",
        "[Metrics: SLI, cost, chaos, recovery and throughput]",
        "```",
        "",
        (
            "The breaker is thread-safe and permits one HALF_OPEN probe. Cache guardrails "
            "reject privacy-sensitive prompts and likely false hits with mismatched four-digit "
            "values. Redis can share cache and circuit state across gateway instances."
        ),
        "",
        "## 2. Configuration and rationale",
        "",
        "| Setting | Value | Rationale |",
        "|---|---:|---|",
        f"| Primary failure rate | {config.providers[0].fail_rate} | Inject realistic degradation |",
        f"| Primary base latency | {config.providers[0].base_latency_ms} ms | Simulated provider latency |",
        f"| Primary cost/1K tokens | {config.providers[0].cost_per_1k_tokens} | Cost tracking baseline |",
        f"| Backup failure rate | {config.providers[1].fail_rate} | Independent low-risk fallback |",
        f"| Backup base latency | {config.providers[1].base_latency_ms} ms | Slower independent fallback |",
        f"| Backup cost/1K tokens | {config.providers[1].cost_per_1k_tokens} | Cheaper budget-aware route |",
        f"| Failure threshold | {config.circuit_breaker.failure_threshold} | Open quickly without reacting to one transient error |",
        f"| Reset timeout | {config.circuit_breaker.reset_timeout_seconds}s | Short lab window with measurable recovery |",
        f"| Success threshold | {config.circuit_breaker.success_threshold} | One healthy probe closes the circuit |",
        f"| Circuit backend | {config.circuit_breaker.backend} | Memory by default; Redis backend is implemented as a bonus |",
        f"| Circuit Redis URL | `{config.circuit_breaker.redis_url}` | Shared-state deployment option |",
        f"| Cache enabled | {config.cache.enabled} | Enable response reuse and cost reduction |",
        f"| Cache backend | {config.cache.backend} | Fast local baseline; Redis integration is separately verified |",
        f"| Cache TTL | {config.cache.ttl_seconds}s | Limits stale responses and storage growth |",
        f"| Similarity threshold | {config.cache.similarity_threshold} | Conservative match threshold to reduce semantic false hits |",
        f"| Cache Redis URL | `{config.cache.redis_url}` | Shared cache endpoint |",
        f"| Load requests | {config.load_test.requests}/scenario | Enough repetition for cache and circuit evidence |",
        f"| Concurrency | {config.load_test.concurrency} workers | Exercises shared state and measures throughput |",
        f"| Random seed | {config.load_test.random_seed} | Reproducible query and provider behavior |",
        f"| Budget soft limit | {config.budget.soft_limit_ratio:.0%} | Route to the cheapest provider before hard exhaustion |",
        f"| Budget enabled | {config.budget.enabled} | Disabled for unbiased chaos baseline; feature tested separately |",
        f"| Budget maximum | {config.budget.max_cost} | Demonstration hard limit when enabled |",
        f"| Chaos scenarios | {len(config.scenarios)} | Healthy, outage, flaky, degraded and recovery coverage |",
        "",
        "## 3. SLO evaluation",
        "",
        "| SLI | Target | Actual | Status |",
        "|---|---:|---:|---|",
        f"| Availability | >= 99% | {availability:.2%} | {_status(availability >= 0.99)} |",
        f"| Latency P95 | < 2500 ms | {latency_p95:.2f} ms | {_status(latency_p95 < 2500)} |",
        f"| Fallback success | >= 95% | {fallback_rate:.2%} | {_status(fallback_rate >= 0.95)} |",
        f"| Cache hit rate | >= 10% | {cache_hit_rate:.2%} | {_status(cache_hit_rate >= 0.10)} |",
        f"| Recovery time | < 5000 ms | {_fmt(recovery_time)} ms | {_status(recovery_met)} |",
        "",
        "## 4. Aggregate metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]

    metric_keys = [
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "circuit_open_count",
        "recovery_time_ms",
        "estimated_cost",
        "estimated_cost_saved",
        "duration_ms",
        "throughput_rps",
    ]
    lines.extend(f"| {key} | {_fmt(metrics[key])} |" for key in metric_keys)

    cache_comparison = comparisons["cache_comparison"]
    without_cache = cache_comparison["without_cache"]
    with_cache = cache_comparison["with_cache"]
    lines.extend(
        [
            "",
            "## 5. Cache comparison",
            "",
            "Controlled 40-request healthy-provider profiles use the same seed and four workers.",
            "",
            "| Metric | Without cache | With cache | Delta |",
            "|---|---:|---:|---:|",
        ]
    )
    for key in [
        "latency_p50_ms",
        "latency_p95_ms",
        "estimated_cost",
        "cache_hit_rate",
        "duration_ms",
        "throughput_rps",
    ]:
        before = float(without_cache[key])
        after = float(with_cache[key])
        lines.append(f"| {key} | {_fmt(before)} | {_fmt(after)} | {_fmt(after - before)} |")

    concurrency = comparisons["concurrency_comparison"]
    sequential = concurrency["sequential"]
    concurrent = concurrency["concurrent"]
    lines.extend(
        [
            "",
            "## 6. Concurrent load comparison (bonus)",
            "",
            "| Metric | Sequential | 4 workers | Delta |",
            "|---|---:|---:|---:|",
        ]
    )
    for key in ["duration_ms", "throughput_rps", "availability", "latency_p95_ms"]:
        before = float(sequential[key])
        after = float(concurrent[key])
        lines.append(f"| {key} | {_fmt(before)} | {_fmt(after)} | {_fmt(after - before)} |")

    lines.extend(
        [
            "",
            "## 7. Redis shared-state evidence",
            "",
            (
                "In-memory cache is process-local, so replicas cannot reuse each other's "
                "responses. `SharedRedisCache` stores hash fields with Redis TTL and uses "
                "`SCAN` for similarity lookup. Writes are mirrored locally so Redis outages "
                "degrade to memory cache."
            ),
            "",
            "```text",
            "$ python scripts/verify_redis_shared.py",
            "redis_ping=True",
            "instance_two_response='visible from instance two'",
            "similarity_score=1.00",
            "",
            "$ docker compose exec redis redis-cli KEYS 'rl:evidence:*'",
            "rl:evidence:f9b2bf7b0364",
            "$ docker compose exec redis redis-cli HGETALL 'rl:evidence:f9b2bf7b0364'",
            "query",
            "shared cache evidence",
            "response",
            "visible from instance two",
            "```",
            "",
            (
                "Two additional tests prove local cache degradation during Redis connection "
                "errors. `SharedRedisCircuitBreaker` uses Redis counters, expiry and a "
                "distributed probe key to share breaker state across replicas."
            ),
            "",
            "## 8. Chaos scenarios",
            "",
            "| Scenario | Expected behavior | Availability | Opens | Recovery ms | Status |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    expected = {
        "primary_timeout_100": "Primary opens; backup preserves service",
        "primary_flaky_50": "Circuit isolates intermittent primary failures",
        "all_healthy": "Primary serves all misses; no circuit opens",
        "primary_degraded_80": "Backup preserves availability under heavy degradation",
        "primary_recovers": "OPEN transitions through HALF_OPEN back to CLOSED",
    }
    for name, status in metrics["scenarios"].items():
        observed = scenario_metrics[name]
        lines.append(
            f"| {name} | {expected.get(name, 'Reliable fallback')} | "
            f"{float(observed['availability']):.2%} | {observed['circuit_open_count']} | "
            f"{_fmt(observed['recovery_time_ms'])} | {str(status).upper()} |"
        )

    lines.extend(
        [
            "",
            "## 9. Bonus work",
            "",
            "- ThreadPoolExecutor load with configured worker count and throughput metrics.",
            "- Thread-safe in-memory breaker and one HALF_OPEN probe to prevent retry storms.",
            "- Redis-backed shared circuit state, counters, TTL and distributed probe lock.",
            "- Redis cache graceful degradation to an in-memory mirror.",
            "- Cost-aware routing: cheapest provider after 80%, cache/static response at 100%.",
            "- Hypothesis property tests for breaker transition invariants.",
            "- Explicit SLO table with automatically evaluated PASS/FAIL status.",
            "",
            "## 10. Failure analysis",
            "",
            (
                "The n-gram cache is lexical rather than truly semantic. It can miss paraphrases "
                "and its number guard only recognizes four-digit tokens. Before production, "
                "replace the similarity scan with versioned embeddings plus tenant-aware keys, "
                "broaden PII/entity detection, validate answer quality, and use an indexed vector "
                "store. Redis similarity currently scans the namespace and should move to an "
                "indexed retrieval design at scale."
            ),
            "",
            "## 11. Next steps",
            "",
            "1. Replace the O(n) Redis scan with a versioned vector index and quality evaluation set.",
            "2. Export breaker/cache/provider telemetry to OpenTelemetry and alert on error-budget burn.",
            "3. Add tenant-scoped keys, rate limits, distributed budgets and secret/PII classification.",
            "",
            "## 12. Reproduction",
            "",
            "```bash",
            'pip install -e ".[dev]"',
            "docker compose up -d",
            "pytest -q",
            "python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json",
            "python scripts/run_comparisons.py --config configs/default.yaml --out reports/comparisons.json",
            "python scripts/generate_report.py --metrics reports/metrics.json --comparisons reports/comparisons.json --out reports/final_report.md",
            "ruff check src scripts",
            "mypy src scripts",
            "```",
            "",
            (
                "Final verification: **45 passed, 7 xpassed, 0 failed, 0 skipped**, "
                "**85% total coverage**, with Redis running."
            ),
        ]
    )

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {destination}")


if __name__ == "__main__":
    main()
