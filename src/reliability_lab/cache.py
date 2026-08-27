from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from threading import RLock
from typing import Any

from redis.exceptions import RedisError

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class CacheLookup:
    value: str | None
    score: float
    metadata: dict[str, str]


class ResponseCache:
    """Thread-safe in-memory response cache with privacy and false-hit guards."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be between zero and one")

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []
        self._lock = RLock()

    def get(self, query: str) -> tuple[str | None, float]:
        """Return the best unexpired guarded similarity match and its score."""
        lookup = self.lookup(query)
        return lookup.value, lookup.score

    def lookup(self, query: str) -> CacheLookup:
        """Return a cache match with metadata used for cost accounting."""
        if _is_uncacheable(query):
            return CacheLookup(None, 0.0, {})

        now = time.time()
        with self._lock:
            self._entries = [
                entry
                for entry in self._entries
                if now - entry.created_at <= self.ttl_seconds
            ]

            best_entry: CacheEntry | None = None
            best_score = 0.0
            for entry in self._entries:
                score = self.similarity(query, entry.key)
                if score > best_score:
                    best_entry = entry
                    best_score = score

            if best_entry is None or best_score < self.similarity_threshold:
                return CacheLookup(None, best_score, {})

            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_entry.key,
                        "score": best_score,
                        "reason": "date_or_number_mismatch",
                        "ts": now,
                    }
                )
                return CacheLookup(None, best_score, {})

            return CacheLookup(best_entry.value, best_score, dict(best_entry.metadata))

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response unless the query contains sensitive data."""
        if _is_uncacheable(query):
            return

        entry = CacheEntry(
            key=query,
            value=value,
            created_at=time.time(),
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._entries.append(entry)

    def clear(self) -> None:
        """Remove all entries and cache audit events."""
        with self._lock:
            self._entries.clear()
            self.false_hit_log.clear()

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute cosine similarity over word tokens and character 3-grams."""
        if a == b:
            return 1.0

        def tokenize(text: str) -> list[str]:
            words = re.findall(r"\w+", text.casefold())
            tokens = list(words)
            for word in words:
                tokens.extend(word[index : index + 3] for index in range(len(word) - 2))
            return tokens

        vector_a = Counter(tokenize(a))
        vector_b = Counter(tokenize(b))
        if not vector_a or not vector_b:
            return 0.0

        dot_product = sum(
            count * vector_b.get(token, 0) for token, count in vector_a.items()
        )
        magnitude_a = math.sqrt(sum(count * count for count in vector_a.values()))
        magnitude_b = math.sqrt(sum(count * count for count in vector_b.values()))
        denominator = magnitude_a * magnitude_b
        return dot_product / denominator if denominator else 0.0


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed cache with shared TTL state and local outage degradation."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._fallback = ResponseCache(ttl_seconds, similarity_threshold)
        self._fallback.false_hit_log = self.false_hit_log

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except RedisError:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Return an exact or guarded similarity match from Redis."""
        lookup = self.lookup(query)
        return lookup.value, lookup.score

    def lookup(self, query: str) -> CacheLookup:
        """Return a Redis cache match and persisted cost/provider metadata."""
        if _is_uncacheable(query):
            return CacheLookup(None, 0.0, {})

        exact_key = f"{self.prefix}{self._query_hash(query)}"
        try:
            exact_response = self._redis.hget(exact_key, "response")
            if exact_response is not None:
                metadata = self._decode_metadata(self._redis.hget(exact_key, "metadata"))
                return CacheLookup(str(exact_response), 1.0, metadata)

            best_query: str | None = None
            best_response: str | None = None
            best_metadata: dict[str, str] = {}
            best_score = 0.0
            keys = list(self._redis.scan_iter(f"{self.prefix}*"))
            with self._redis.pipeline(transaction=False) as pipeline:
                for key in keys:
                    pipeline.hgetall(key)
                cached_entries = pipeline.execute()
            for cached_entry in cached_entries:
                cached_query = cached_entry.get("query")
                if cached_query is None:
                    continue
                score = ResponseCache.similarity(query, str(cached_query))
                if score > best_score:
                    best_query = str(cached_query)
                    cached_response = cached_entry.get("response")
                    best_response = (
                        str(cached_response) if cached_response is not None else None
                    )
                    best_metadata = self._decode_metadata(cached_entry.get("metadata"))
                    best_score = score
        except RedisError:
            return self._fallback.lookup(query)

        if (
            best_query is None
            or best_response is None
            or best_score < self.similarity_threshold
        ):
            return CacheLookup(None, best_score, {})

        if _looks_like_false_hit(query, best_query):
            self.false_hit_log.append(
                {
                    "query": query,
                    "cached_key": best_query,
                    "score": best_score,
                    "reason": "date_or_number_mismatch",
                    "ts": time.time(),
                }
            )
            return CacheLookup(None, best_score, {})

        return CacheLookup(best_response, best_score, best_metadata)

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a guarded response in Redis and the local fallback with TTL."""
        if _is_uncacheable(query):
            return

        self._fallback.set(query, value, metadata)
        key = f"{self.prefix}{self._query_hash(query)}"
        try:
            with self._redis.pipeline(transaction=True) as pipeline:
                pipeline.hset(
                    key,
                    mapping={
                        "query": query,
                        "response": value,
                        "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                    },
                )
                pipeline.expire(key, self.ttl_seconds)
                pipeline.execute()
        except RedisError:
            # The local copy keeps the gateway useful while Redis is unavailable.
            return

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        self._fallback.clear()
        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                self._redis.delete(key)
        except RedisError:
            return

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            try:
                self._redis.close()
            except RedisError:
                return

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]

    @staticmethod
    def _decode_metadata(raw: object) -> dict[str, str]:
        if not isinstance(raw, str):
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(value, dict):
            return {}
        return {str(key): str(item) for key, item in value.items()}
