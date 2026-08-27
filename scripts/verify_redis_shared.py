from __future__ import annotations

from reliability_lab.cache import SharedRedisCache


def main() -> None:
    first = SharedRedisCache(
        "redis://localhost:6379/0",
        ttl_seconds=300,
        similarity_threshold=0.92,
        prefix="rl:evidence:",
    )
    second = SharedRedisCache(
        "redis://localhost:6379/0",
        ttl_seconds=300,
        similarity_threshold=0.92,
        prefix="rl:evidence:",
    )
    try:
        first.flush()
        first.set("shared cache evidence", "visible from instance two")
        response, score = second.get("shared cache evidence")
        print(f"redis_ping={second.ping()}")
        print(f"instance_two_response={response!r}")
        print(f"similarity_score={score:.2f}")
    finally:
        first.close()
        second.close()


if __name__ == "__main__":
    main()
