# Day 25 — Production Reliability for LLM Agents

A production-style reliability layer for an LLM gateway with circuit breakers, provider
fallback, guarded semantic caching, shared Redis state, chaos/load testing, observability,
cost controls and reproducible evidence.

## Final status

- Core, integration and reliability quality tests: **68/68 passed**
- Assignment TODO checks: **7 passed checks**
- Failures/skips with Redis running: **0/0**
- Test coverage: **97.11%**
- Chaos scenarios: **5/5 passed**
- Aggregate availability: **100%**
- End-to-end latency P95: **317.37 ms**
- Cache hit rate: **59.20%**
- Measured circuit recovery: **~2.4 s**
- Rubric self-assessment: **100/100**

Exact values are stored in `reports/metrics.json` and summarized in
`reports/final_report.md`.

## Architecture

```text
Request
  |
  v
Privacy guard -> Memory/Redis semantic cache -> cache hit
  | miss
  v
Primary circuit breaker -> Primary provider
  | failure/open
  v
Backup circuit breaker  -> Backup provider
  | failure/open
  v
Static degraded response
  |
  v
Latency, availability, fallback, cache, cost and recovery metrics
```

## Features

- CLOSED → OPEN → HALF_OPEN → CLOSED circuit state machine.
- Thread-safe counters and one HALF_OPEN probe to prevent retry storms.
- Ordered provider fallback with a static degraded response.
- Character 3-gram + word-token cosine response cache.
- TTL eviction, privacy bypass and four-digit false-hit detection.
- Redis shared cache with TTL and shared state across gateway replicas.
- Graceful Redis cache degradation to an in-memory mirror.
- Optional Redis-backed shared circuit state and distributed probe lock.
- Reproducible chaos scenarios with JSON/CSV metrics.
- Concurrent load using `ThreadPoolExecutor` and throughput measurement.
- Cost-aware provider routing at soft and hard budget limits.
- Hypothesis property tests for circuit state invariants.

## Setup

Python 3.10+ and Docker are required. No API key is needed; providers are simulated locally.

### PowerShell

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

docker compose up -d
docker compose exec redis redis-cli ping
python -m pytest -q
```

### Bash

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

docker compose up -d
docker compose exec redis redis-cli ping
pytest -q
```

## Reproduce all evidence

```bash
python scripts/run_chaos.py \
  --config configs/default.yaml \
  --out reports/metrics.json

python scripts/run_comparisons.py \
  --config configs/default.yaml \
  --out reports/comparisons.json

pytest -q --cov=reliability_lab \
  --cov-fail-under=95 \
  --cov-report=xml:reports/coverage.xml \
  --junitxml=reports/junit.xml

python scripts/verify_redis_shared.py \
  --out reports/redis_evidence.json

python scripts/run_quality_checks.py \
  --out reports/quality.json

python scripts/generate_report.py \
  --metrics reports/metrics.json \
  --comparisons reports/comparisons.json \
  --junit reports/junit.xml \
  --coverage reports/coverage.xml \
  --redis-evidence reports/redis_evidence.json \
  --quality-evidence reports/quality.json \
  --config configs/default.yaml \
  --out reports/final_report.md
```

Make targets provide the same workflow on systems with GNU Make:

```bash
make docker-up
make test
make quality
make static-quality
make run-chaos
make run-comparisons
make redis-evidence
make report
make lint
make typecheck
```

## Checkpoints

| Checkpoint | Deliverable | Commit |
|---|---|---|
| 1 | Production-safe circuit breaker | `3896a53` |
| 2 | Guarded in-memory semantic cache | `e429bd2` |
| 3 | Gateway cache/provider/static fallback pipeline | `23affa6` |
| 4 | Reproducible chaos metrics, CSV and SLO evidence | `6ecdcf3` |
| 5 | Shared Redis cache and graceful degradation | `87ab2ff` |
| 6 | Concurrent load and reliability stretch goals | `0b7e343` |
| 7 | Final report, comparisons and repository QA | latest commit on `main` |

## Repository structure

```text
src/reliability_lab/
  cache.py                    memory + Redis cache and guardrails
  circuit_breaker.py          thread-safe local circuit breaker
  redis_circuit_breaker.py    optional shared Redis circuit state
  gateway.py                  cache and provider routing pipeline
  chaos.py                    scenario runner and recovery measurement
  metrics.py                  SLI calculation and JSON/CSV export
  providers.py                deterministic fake LLM providers
  config.py                   validated YAML configuration

scripts/
  run_chaos.py                generate aggregate/scenario metrics
  run_comparisons.py          cache and concurrency A/B profiles
  verify_redis_shared.py      reproduce cross-instance Redis evidence
  generate_report.py          generate the complete final report

reports/
  metrics.json                reproducible final chaos metrics
  metrics.csv                 flattened metrics export
  comparisons.json            controlled A/B measurements
  junit.xml                    raw machine-readable test log
  coverage.xml                 measured line-coverage evidence
  redis_evidence.json          live cross-instance Redis proof
  quality.json                 Ruff/MyPy commands, output and return codes
  final_report.md             rubric-aligned report
```

## Quality gates

```bash
pytest -q
ruff check src tests scripts
mypy src scripts
```

The known production limitation is that n-gram matching is lexical and Redis similarity lookup
is an O(n) namespace scan. The final report describes an embedding index, tenant isolation,
stronger entity/PII detection and quality validation as the next production step.
