from __future__ import annotations

import random
import time
from dataclasses import dataclass
from threading import RLock


class ProviderError(RuntimeError):
    """Raised when a fake provider fails."""


@dataclass(slots=True)
class ProviderResponse:
    provider: str
    text: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    estimated_cost: float


class FakeLLMProvider:
    """Deterministic-enough fake provider for local chaos tests.

    This avoids real API keys while still simulating latency, failures, and cost.
    """

    def __init__(
        self,
        name: str,
        fail_rate: float,
        base_latency_ms: int,
        cost_per_1k_tokens: float,
        random_seed: int | None = None,
    ):
        self.name = name
        self.fail_rate = fail_rate
        self.base_latency_ms = base_latency_ms
        self.cost_per_1k_tokens = cost_per_1k_tokens
        self._random = random.Random(random_seed)
        self._random_lock = RLock()

    def complete(self, prompt: str) -> ProviderResponse:
        start = time.perf_counter()
        with self._random_lock:
            jitter_ms = self._random.randint(0, 60)
            should_fail = self._random.random() < self.fail_rate
            output_tokens = self._random.randint(20, 80)
        time.sleep((self.base_latency_ms + jitter_ms) / 1000.0)
        if should_fail:
            raise ProviderError(f"{self.name} simulated failure")
        input_tokens = max(1, len(prompt.split()))
        cost = (input_tokens + output_tokens) / 1000.0 * self.cost_per_1k_tokens
        latency_ms = (time.perf_counter() - start) * 1000
        return ProviderResponse(
            provider=self.name,
            text=f"[{self.name}] reliable answer for: {prompt[:60]}",
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost=cost,
        )
