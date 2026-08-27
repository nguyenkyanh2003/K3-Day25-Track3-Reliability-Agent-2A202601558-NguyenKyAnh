from hypothesis import given
from hypothesis import strategies as st

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState


@given(
    failure_threshold=st.integers(min_value=1, max_value=10),
    outcomes=st.lists(st.booleans(), min_size=1, max_size=50),
)
def test_circuit_only_opens_after_consecutive_failure_threshold(
    failure_threshold: int,
    outcomes: list[bool],
) -> None:
    breaker = CircuitBreaker("property", failure_threshold, 60)
    consecutive_failures = 0

    for succeeded in outcomes:
        if succeeded:
            breaker.record_success()
            consecutive_failures = 0
        else:
            breaker.record_failure()
            consecutive_failures += 1

        if breaker.state == CircuitState.OPEN:
            assert consecutive_failures >= failure_threshold
            break
        assert consecutive_failures < failure_threshold


@given(st.integers(min_value=1, max_value=20))
def test_success_resets_any_subthreshold_failure_run(failures: int) -> None:
    breaker = CircuitBreaker("property", failures + 1, 60)
    for _ in range(failures):
        breaker.record_failure()

    breaker.record_success()

    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0
