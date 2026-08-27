from typing import NoReturn

from redis.exceptions import ConnectionError

from reliability_lab.cache import SharedRedisCache


class UnavailableRedis:
    def ping(self) -> NoReturn:
        raise ConnectionError("redis unavailable")

    def hget(self, key: str, field: str) -> NoReturn:
        raise ConnectionError("redis unavailable")

    def pipeline(self, transaction: bool = True) -> NoReturn:
        raise ConnectionError("redis unavailable")

    def scan_iter(self, pattern: str) -> NoReturn:
        raise ConnectionError("redis unavailable")

    def close(self) -> NoReturn:
        raise ConnectionError("redis unavailable")


def test_redis_outage_degrades_to_memory_cache() -> None:
    cache = SharedRedisCache("redis://localhost:6379/0", 60, 0.5)
    cache._redis = UnavailableRedis()

    cache.set("hello world", "fallback response")

    assert not cache.ping()
    assert cache.get("hello world") == ("fallback response", 1.0)


def test_redis_outage_still_enforces_privacy_guard() -> None:
    cache = SharedRedisCache("redis://localhost:6379/0", 60, 0.5)
    cache._redis = UnavailableRedis()

    cache.set("account balance for user 123", "private")

    assert cache.get("account balance for user 123") == (None, 0.0)
