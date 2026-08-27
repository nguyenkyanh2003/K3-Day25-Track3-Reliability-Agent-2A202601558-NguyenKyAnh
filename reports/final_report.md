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
| Latency P95 | < 2500 ms | 314.58 ms | PASS |
| Fallback success | >= 95% | 100.00% | PASS |
| Cache hit rate | >= 10% | 59.20% | PASS |
| Recovery time | < 5000 ms | 2414.3806 ms | PASS |

## 4. Aggregate metrics

| Metric | Value |
|---|---:|
| total_requests | 500 |
| availability | 1 |
| error_rate | 0 |
| latency_p50_ms | 270.41 |
| latency_p95_ms | 314.58 |
| latency_p99_ms | 319.21 |
| fallback_success_rate | 1 |
| cache_hit_rate | 0.592 |
| circuit_open_count | 10 |
| recovery_time_ms | 2414.3806 |
| estimated_cost | 0.0848 |
| estimated_cost_saved | 0.296 |
| duration_ms | 16186.85 |
| throughput_rps | 30.89 |

## 5. Cache comparison

Controlled 40-request healthy-provider profiles use the same seed and four workers.

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 206.31 | 204.65 | -1.66 |
| latency_p95_ms | 234.57 | 234.16 | -0.41 |
| estimated_cost | 0.0241 | 0.0136 | -0.0106 |
| cache_hit_rate | 0 | 0.425 | 0.425 |
| duration_ms | 2156.85 | 1295.79 | -861.06 |
| throughput_rps | 18.55 | 30.87 | 12.32 |

## 6. Concurrent load comparison (bonus)

| Metric | Sequential | 4 workers | Delta |
|---|---:|---:|---:|
| duration_ms | 8390.27 | 2159.82 | -6230.45 |
| throughput_rps | 4.77 | 18.52 | 13.75 |
| availability | 1 | 1 | 0 |
| latency_p95_ms | 234.74 | 234.47 | -0.27 |

## 7. Redis shared-state evidence

In-memory cache is process-local, so replicas cannot reuse each other's responses. `SharedRedisCache` stores hash fields with Redis TTL and uses `SCAN` for similarity lookup. Writes are mirrored locally so Redis outages degrade to memory cache.

```text
$ python scripts/verify_redis_shared.py
redis_ping=True
instance_two_response='visible from instance two'
similarity_score=1.00

$ docker compose exec redis redis-cli KEYS 'rl:evidence:*'
rl:evidence:f9b2bf7b0364
$ docker compose exec redis redis-cli HGETALL 'rl:evidence:f9b2bf7b0364'
query
shared cache evidence
response
visible from instance two
```

Two additional tests prove local cache degradation during Redis connection errors. `SharedRedisCircuitBreaker` uses Redis counters, expiry and a distributed probe key to share breaker state across replicas.

## 8. Chaos scenarios

| Scenario | Expected behavior | Availability | Opens | Recovery ms | Status |
|---|---|---:|---:|---:|---|
| primary_timeout_100 | Primary opens; backup preserves service | 100.00% | 3 | None | PASS |
| primary_flaky_50 | Circuit isolates intermittent primary failures | 100.00% | 3 | None | PASS |
| all_healthy | Primary serves all misses; no circuit opens | 100.00% | 0 | None | PASS |
| primary_degraded_80 | Backup preserves availability under heavy degradation | 100.00% | 3 | None | PASS |
| primary_recovers | OPEN transitions through HALF_OPEN back to CLOSED | 100.00% | 1 | 2414.3806 | PASS |

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

## 12. Reproduction

```bash
pip install -e ".[dev]"
docker compose up -d
pytest -q
python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json
python scripts/run_comparisons.py --config configs/default.yaml --out reports/comparisons.json
python scripts/generate_report.py --metrics reports/metrics.json --comparisons reports/comparisons.json --out reports/final_report.md
ruff check src scripts
mypy src scripts
```

Final verification: **45 passed, 7 xpassed, 0 failed, 0 skipped**, **85% total coverage**, with Redis running.
