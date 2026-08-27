from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError
from reliability_lab.redis_circuit_breaker import SharedRedisCircuitBreaker


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None
    route_reason: str | None = None
    estimated_cost_saved: float = 0.0
    attempted_providers: tuple[str, ...] = ()


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: Mapping[str, CircuitBreaker | SharedRedisCircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        max_cost: float | None = None,
        soft_limit_ratio: float = 0.8,
    ):
        if max_cost is not None and max_cost <= 0:
            raise ValueError("max_cost must be greater than zero")
        if not 0.0 < soft_limit_ratio < 1.0:
            raise ValueError("soft_limit_ratio must be between zero and one")

        self.providers = providers
        self.breakers = dict(breakers)
        self.cache = cache
        self.max_cost = max_cost
        self.soft_limit_ratio = soft_limit_ratio
        self._cumulative_cost = 0.0
        self._budget_lock = RLock()
        self._budget_execution_lock = RLock()

    @property
    def cumulative_cost(self) -> float:
        with self._budget_lock:
            return self._cumulative_cost

    def _provider_chain(self) -> list[tuple[int, FakeLLMProvider]]:
        indexed_providers = list(enumerate(self.providers))
        if self.max_cost is None:
            return indexed_providers

        with self._budget_lock:
            budget_ratio = self._cumulative_cost / self.max_cost
        if budget_ratio >= 1.0:
            return []
        if budget_ratio < self.soft_limit_ratio or not indexed_providers:
            return indexed_providers

        cheapest_cost = min(provider.cost_per_1k_tokens for provider in self.providers)
        return [
            (index, provider)
            for index, provider in indexed_providers
            if provider.cost_per_1k_tokens == cheapest_cost
        ]

    def complete(self, prompt: str) -> GatewayResponse:
        """Route through cache, budget-aware providers, breakers and static fallback."""
        cached_response = self._cached_response(prompt)
        if cached_response is not None:
            return cached_response

        if self.max_cost is not None:
            with self._budget_execution_lock:
                cached_response = self._cached_response(prompt)
                if cached_response is not None:
                    return cached_response
                return self._complete_from_provider(prompt)
        return self._complete_from_provider(prompt)

    def _cached_response(self, prompt: str) -> GatewayResponse | None:
        if self.cache is None:
            return None

        lookup = self.cache.lookup(prompt)
        if lookup.value is None:
            return None

        raw_cost = lookup.metadata.get("estimated_cost", "0")
        try:
            estimated_cost_saved = float(raw_cost)
        except ValueError:
            estimated_cost_saved = 0.0
        cached_provider = lookup.metadata.get("provider", "unknown")
        return GatewayResponse(
            text=lookup.value,
            route=f"cache_hit:{lookup.score:.2f}",
            provider=None,
            cache_hit=True,
            latency_ms=0.0,
            estimated_cost=0.0,
            route_reason=f"cache_score={lookup.score:.4f};source_provider={cached_provider}",
            estimated_cost_saved=estimated_cost_saved,
        )

    def _complete_from_provider(self, prompt: str) -> GatewayResponse:
        provider_chain = self._provider_chain()
        last_error: str | None = None
        attempted_providers: list[str] = []
        for provider_index, provider in provider_chain:
            attempted_providers.append(provider.name)
            breaker = self.breakers[provider.name]
            try:
                response = breaker.call(provider.complete, prompt)
            except (ProviderError, CircuitOpenError) as exc:
                last_error = f"{provider.name}: {exc}"
                continue

            if self.cache is not None:
                self.cache.set(
                    prompt,
                    response.text,
                    {
                        "provider": provider.name,
                        "estimated_cost": f"{response.estimated_cost:.12f}",
                    },
                )
            with self._budget_lock:
                self._cumulative_cost += response.estimated_cost

            tier = "primary" if provider_index == 0 else "fallback"
            return GatewayResponse(
                text=response.text,
                route=tier,
                provider=response.provider,
                cache_hit=False,
                latency_ms=response.latency_ms,
                estimated_cost=response.estimated_cost,
                route_reason=f"provider={provider.name};tier={tier}",
                attempted_providers=tuple(attempted_providers),
            )

        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=last_error or (
                "Cost budget exhausted"
                if self.max_cost is not None and self.cumulative_cost >= self.max_cost
                else "No providers configured"
            ),
            route_reason=f"providers_exhausted;last_error={last_error}",
            attempted_providers=tuple(attempted_providers),
        )
