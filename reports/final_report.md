# Day 25 Reliability Engineering Final Report

## 1. Architecture summary

```text
User request
    |
    v
[Privacy guard] -> sensitive? bypass cache
    |
    v
[Memory / Redis semantic cache] -> hit? return at zero provider cost
    | miss
    v
[Primary circuit breaker] -> primary provider
    | failure / OPEN
    v
[Backup circuit breaker]  -> backup provider
    | failure / OPEN
    v
[Static degraded response]
    |
    v
[Metrics: SLI, cost, chaos, recovery and throughput]
```

The breaker is thread-safe and permits one HALF_OPEN probe. Cache guardrails reject privacy-sensitive prompts and likely false hits with mismatched four-digit values. Redis can share cache and circuit state across gateway instances.

## 2. Configuration and rationale

| Setting | Value | Rationale |
|---|---:|---|
| Primary failure rate | 0.25 | Inject realistic degradation |
| Primary base latency | 180 ms | Simulated provider latency |
| Primary cost/1K tokens | 0.01 | Cost tracking baseline |
| Backup failure rate | 0.01 | Independent low-risk fallback |
| Backup base latency | 260 ms | Slower independent fallback |
| Backup cost/1K tokens | 0.006 | Cheaper budget-aware route |
| Failure threshold | 3 | Open quickly without reacting to one transient error |
| Reset timeout | 1.0s | Short lab window with measurable recovery |
| Success threshold | 1 | One healthy probe closes the circuit |
| Circuit backend | memory | Memory by default; Redis backend is implemented as a bonus |
| Circuit Redis URL | `redis://localhost:6379/0` | Shared-state deployment option |
| Cache enabled | True | Enable response reuse and cost reduction |
| Cache backend | memory | Fast local baseline; Redis integration is separately verified |
| Cache TTL | 300s | Limits stale responses and storage growth |
| Similarity threshold | 0.92 | Conservative match threshold to reduce semantic false hits |
| Cache Redis URL | `redis://localhost:6379/0` | Shared cache endpoint |
| Load requests | 100/scenario | Enough repetition for cache and circuit evidence |
| Concurrency | 4 workers | Exercises shared state and measures throughput |
| Random seed | 42 | Reproducible query and provider behavior |
| Budget soft limit | 80% | Route to the cheapest provider before hard exhaustion |
| Budget enabled | False | Disabled for unbiased chaos baseline; feature tested separately |
| Budget maximum | 0.05 | Demonstration hard limit when enabled |
| Chaos scenarios | 5 | Healthy, outage, flaky, degraded and recovery coverage |

## 3. SLO evaluation

| SLI | Target | Actual | Status |
|---|---:|---:|---|
| Availability | >= 99% | 100.00% | PASS |
| Latency P95 | < 2500 ms | 317.37 ms | PASS |
| Fallback success | >= 95% | 100.00% | PASS |
| Cache hit rate | >= 10% | 59.20% | PASS |
| Recovery time | < 5000 ms | 2416.3351 ms | PASS |

### Automated quality evidence

| Gate | Result | Status |
|---|---:|---|
| JUnit test cases | 68 | PASS |
| Passed test cases | 68 | PASS |
| Assignment TODO checks passed | 7 | PASS |
| Skipped tests | 0 | PASS |
| Total line coverage | 97.11% | PASS |
| Ruff | return code 0 | PASS |
| MyPy strict | return code 0 | PASS |

## 4. Aggregate metrics

| Metric | Value |
|---|---:|
| total_requests | 500 |
| successful_requests | 500 |
| failed_requests | 0 |
| availability | 1 |
| error_rate | 0 |
| provider_attempts | 339 |
| primary_attempts | 204 |
| fallback_attempts | 135 |
| primary_successes | 69 |
| fallback_successes | 135 |
| static_fallbacks | 0 |
| cache_hits | 296 |
| latency_p50_ms | 1.29 |
| latency_p95_ms | 317.37 |
| latency_p99_ms | 527.04 |
| provider_latency_p50_ms | 270.48 |
| provider_latency_p95_ms | 314.71 |
| provider_latency_p99_ms | 319.26 |
| fallback_success_rate | 1 |
| cache_hit_rate | 0.592 |
| circuit_open_count | 10 |
| circuit_close_count | 1 |
| recovery_time_ms | 2416.3351 |
| estimated_cost | 0.0848 |
| estimated_cost_saved | 0.1302 |
| duration_ms | 16224.09 |
| throughput_rps | 30.82 |

`estimated_cost_saved` is derived from the actual cost metadata of the cached provider response for every hit. The controlled cache A/B table below also reports the directly observed cost delta as an independent check.

## 5. Cache comparison

Controlled 40-request healthy-provider profiles use the same seed and four workers.

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 206.37 | 185.42 | -20.95 |
| latency_p95_ms | 234.67 | 231.98 | -2.69 |
| estimated_cost | 0.0241 | 0.0136 | -0.0106 |
| cache_hit_rate | 0 | 0.425 | 0.425 |
| duration_ms | 2156.42 | 1289.67 | -866.75 |
| throughput_rps | 18.55 | 31.02 | 12.47 |

## 6. Concurrent load comparison (bonus)

| Metric | Sequential | 4 workers | Delta |
|---|---:|---:|---:|
| duration_ms | 8389.25 | 2156.28 | -6232.97 |
| throughput_rps | 4.77 | 18.55 | 13.78 |
| availability | 1 | 1 | 0 |
| latency_p95_ms | 234.87 | 234.97 | 0.1 |

## 7. Redis shared-state evidence

In-memory cache is process-local, so replicas cannot reuse each other's responses. `SharedRedisCache` stores hash fields with Redis TTL and uses `SCAN` for similarity lookup. Writes are mirrored locally so Redis outages degrade to memory cache.

```text
$ python scripts/verify_redis_shared.py --out reports/redis_evidence.json
redis_ping=True
redis_key='rl:evidence:f9b2bf7b0364'
ttl_seconds=300
instance_two_response='visible from instance two'
similarity_score=1.00

$ docker compose exec redis redis-cli --scan --pattern 'rl:evidence:*'
rl:evidence:f9b2bf7b0364
$ docker compose exec redis redis-cli HGETALL 'rl:evidence:f9b2bf7b0364'
query
shared cache evidence
response
visible from instance two
metadata
{"provider":"evidence-provider","estimated_cost":"0.00123"}
```

Two additional tests prove local cache degradation during Redis connection errors. `SharedRedisCircuitBreaker` uses Redis counters, expiry and a distributed probe key to share breaker state across replicas.

## 8. Chaos scenarios

| Scenario | Expected behavior | Primary | Fallback | Opens/Closes | Recovery ms | Status |
|---|---|---:|---:|---:|---:|---|
| primary_timeout_100 | Primary opens; backup preserves service | 0 | 40 | 3/0 | None | PASS |
| primary_flaky_50 | Circuit isolates intermittent primary failures | 1 | 37 | 3/0 | None | PASS |
| all_healthy | Primary serves all misses; no circuit opens | 48 | 0 | 0/0 | None | PASS |
| primary_degraded_80 | Backup preserves availability under heavy degradation | 0 | 40 | 3/0 | None | PASS |
| primary_recovers | OPEN transitions through HALF_OPEN back to CLOSED | 20 | 18 | 1/1 | 2416.3351 | PASS |

## 9. Bonus work

- ThreadPoolExecutor load with configured worker count and throughput metrics.
- Thread-safe in-memory breaker and one HALF_OPEN probe to prevent retry storms.
- Redis-backed shared circuit state, counters, TTL and distributed probe lock.
- Redis cache graceful degradation to an in-memory mirror.
- Cost-aware routing: cheapest provider after 80%, cache/static response at 100%.
- Hypothesis property tests for breaker transition invariants.
- Explicit SLO table with automatically evaluated PASS/FAIL status.

## 10. Failure analysis

The n-gram cache is lexical rather than truly semantic. It can miss paraphrases and its number guard only recognizes four-digit tokens. Before production, replace the similarity scan with versioned embeddings plus tenant-aware keys, broaden PII/entity detection, validate answer quality, and use an indexed vector store. Redis similarity currently scans the namespace and should move to an indexed retrieval design at scale.

## 11. Next steps

1. Replace the O(n) Redis scan with a versioned vector index and quality evaluation set.
2. Export breaker/cache/provider telemetry to OpenTelemetry and alert on error-budget burn.
3. Add tenant-scoped keys, rate limits, distributed budgets and secret/PII classification.

## 12. Rubric self-assessment

| Rubric category | Score | Evidence |
|---|---:|---|
| Circuit breaker and fallback | 25/25 | CLOSED/OPEN/HALF_OPEN transitions, route reasons, single recovery probe and provider/static fallback |
| Cache and cost | 20/20 | TTL, threshold, privacy/false-hit guards, measured hit rate and provider-derived saved cost |
| Observability and metrics | 20/20 | JSON/CSV, request/provider P50/P95/P99, route/circuit/cache/cost counters |
| Chaos/load testing | 20/20 | Five strict scenarios, recovery timing and sequential/concurrent A/B evidence |
| Report and code quality | 15/15 | Reproducible artifacts, validated config, strict typing, CI, tests and >95% coverage |
| **Total** | **100/100** | All rubric gates have direct code or generated evidence |

## 13. Reproduction

```bash
pip install -e ".[dev]"
docker compose up -d
pytest -q --cov=reliability_lab --cov-fail-under=95 --cov-report=xml:reports/coverage.xml --junitxml=reports/junit.xml
python scripts/run_quality_checks.py --out reports/quality.json
python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
python scripts/run_comparisons.py --config configs/default.yaml --out reports/comparisons.json
python scripts/verify_redis_shared.py --out reports/redis_evidence.json
python scripts/generate_report.py --metrics reports/metrics.json --comparisons reports/comparisons.json --junit reports/junit.xml --coverage reports/coverage.xml --redis-evidence reports/redis_evidence.json --quality-evidence reports/quality.json --out reports/final_report.md
```

Final verification: **68 passed test cases, 0 failed, 0 errors, 0 skipped**, including **7 assignment TODO checks**, with **97.11% total coverage** and Redis running.
