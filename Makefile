.PHONY: test quality static-quality lint typecheck run-chaos run-comparisons redis-evidence report clean docker-up docker-down

test:
	pytest -q

quality:
	pytest -q --cov=reliability_lab --cov-fail-under=95 --cov-report=term-missing --cov-report=xml:reports/coverage.xml --junitxml=reports/junit.xml

static-quality:
	python scripts/run_quality_checks.py --out reports/quality.json

lint:
	ruff check src tests scripts

typecheck:
	mypy src scripts

run-chaos:
	python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json

run-comparisons:
	python scripts/run_comparisons.py --config configs/default.yaml --out reports/comparisons.json

redis-evidence:
	python scripts/verify_redis_shared.py --out reports/redis_evidence.json

report:
	python scripts/generate_report.py --metrics reports/metrics.json --comparisons reports/comparisons.json --junit reports/junit.xml --coverage reports/coverage.xml --redis-evidence reports/redis_evidence.json --quality-evidence reports/quality.json --config configs/default.yaml --out reports/final_report.md

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache reports/metrics.json reports/metrics.csv reports/comparisons.json reports/redis_evidence.json reports/quality.json reports/junit.xml reports/coverage.xml reports/final_report.md
