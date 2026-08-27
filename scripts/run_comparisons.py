from __future__ import annotations

import argparse
import json
from pathlib import Path

from reliability_lab.chaos import load_queries, run_scenario
from reliability_lab.config import LabConfig, ScenarioConfig, load_config


def _profile(config: LabConfig, *, cache_enabled: bool, concurrency: int) -> dict[str, object]:
    requests = min(config.load_test.requests, 40)
    profile_config = config.model_copy(
        update={
            "cache": config.cache.model_copy(
                update={"enabled": cache_enabled, "backend": "memory"}
            ),
            "load_test": config.load_test.model_copy(
                update={"requests": requests, "concurrency": concurrency}
            ),
            "budget": config.budget.model_copy(update={"enabled": False}),
        }
    )
    scenario = ScenarioConfig(
        name="comparison_healthy",
        description="Both providers healthy for a controlled comparison",
        provider_overrides={provider.name: 0.0 for provider in config.providers},
    )
    return run_scenario(profile_config, load_queries(), scenario).to_report_dict()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/comparisons.json")
    args = parser.parse_args()

    config = load_config(args.config)
    results = {
        "cache_comparison": {
            "without_cache": _profile(
                config,
                cache_enabled=False,
                concurrency=config.load_test.concurrency,
            ),
            "with_cache": _profile(
                config,
                cache_enabled=True,
                concurrency=config.load_test.concurrency,
            ),
        },
        "concurrency_comparison": {
            "sequential": _profile(config, cache_enabled=False, concurrency=1),
            "concurrent": _profile(
                config,
                cache_enabled=False,
                concurrency=config.load_test.concurrency,
            ),
        },
    }

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"wrote {destination}")


if __name__ == "__main__":
    main()
