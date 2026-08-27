from __future__ import annotations

import argparse
import json
from pathlib import Path

from reliability_lab.cache import SharedRedisCache


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="reports/redis_evidence.json")
    args = parser.parse_args()

    prefix = "rl:evidence:"
    query = "shared cache evidence"
    first = SharedRedisCache(
        "redis://localhost:6379/0",
        ttl_seconds=300,
        similarity_threshold=0.92,
        prefix=prefix,
    )
    second = SharedRedisCache(
        "redis://localhost:6379/0",
        ttl_seconds=300,
        similarity_threshold=0.92,
        prefix=prefix,
    )
    try:
        first.flush()
        first.set(
            query,
            "visible from instance two",
            {"provider": "evidence-provider", "estimated_cost": "0.00123"},
        )
        lookup = second.lookup(query)
        key = f"{prefix}{first._query_hash(query)}"
        redis_fields = first._redis.hgetall(key)
        evidence = {
            "redis_ping": second.ping(),
            "key": key,
            "ttl_seconds": int(first._redis.ttl(key)),
            "query": redis_fields.get("query"),
            "response": lookup.value,
            "similarity_score": lookup.score,
            "metadata": lookup.metadata,
        }
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"redis_ping={evidence['redis_ping']}")
        print(f"redis_key={key!r}")
        print(f"ttl_seconds={evidence['ttl_seconds']}")
        response = lookup.value
        score = lookup.score
        print(f"instance_two_response={response!r}")
        print(f"similarity_score={score:.2f}")
        print(f"wrote {destination}")
    finally:
        first.close()
        second.close()


if __name__ == "__main__":
    main()
