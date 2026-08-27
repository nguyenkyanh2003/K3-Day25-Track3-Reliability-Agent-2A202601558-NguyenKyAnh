.PHONY: test lint typecheck run-chaos run-comparisons report clean docker-up docker-down

test:
	pytest -q

lint:
	ruff check src tests scripts

typecheck:
	mypy src scripts

run-chaos:
	python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json

run-comparisons:
	python scripts/run_comparisons.py --config configs/default.yaml --out reports/comparisons.json

report:
	python scripts/generate_report.py --metrics reports/metrics.json --comparisons reports/comparisons.json --config configs/default.yaml --out reports/final_report.md

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache reports/metrics.json reports/metrics.csv reports/comparisons.json reports/final_report.md
