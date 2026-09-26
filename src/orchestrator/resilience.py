"""Circuit breakers and retry backoff."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

from .config import CircuitBreakerConfig


@dataclass
class _BreakerState:
    failures: int = 0
    opened_at: float | None = None
    half_open_in_flight: bool = False


class CircuitBreakers:
    """One breaker per key (agent name). Closed -> open after N failures -> half-open after cool-down."""

    def __init__(self, config: CircuitBreakerConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._clock = clock
        self._states: dict[str, _BreakerState] = {}

    def allow(self, key: str) -> bool:
        state = self._states.setdefault(key, _BreakerState())
        if state.opened_at is None:
            return True
        if self._clock() - state.opened_at >= self._config.reset_after_s:
            if not state.half_open_in_flight:
                state.half_open_in_flight = True  # let exactly one probe through
                return True
        return False

    def record_success(self, key: str) -> None:
        self._states[key] = _BreakerState()

    def record_failure(self, key: str) -> None:
        state = self._states.setdefault(key, _BreakerState())
        state.failures += 1
        state.half_open_in_flight = False
        if state.opened_at is not None or state.failures >= self._config.failure_threshold:
            state.opened_at = self._clock()

    def snapshot(self) -> dict[str, dict[str, object]]:
        """State per key for the command center: closed, open or half_open."""
        now = self._clock()
        out: dict[str, dict[str, object]] = {}
        for key, st in self._states.items():
            if st.opened_at is None:
                state = "closed"
            elif now - st.opened_at >= self._config.reset_after_s:
                state = "half_open"
            else:
                state = "open"
            out[key] = {"state": state, "consecutive_failures": st.failures,
                        "retry_in_s": max(0.0, round(self._config.reset_after_s - (now - st.opened_at), 1)) if st.opened_at else 0.0}
        return out

    def reset(self, key: str) -> None:
        """Operator action: close a breaker manually (for example after a fix is deployed)."""
        self._states.pop(key, None)

    def is_open(self, key: str) -> bool:
        state = self._states.get(key)
        return bool(state and state.opened_at is not None)


def backoff_delay_s(attempt: int, base_ms: int, max_ms: int, rng: random.Random | None = None) -> float:
    """Full-jitter exponential backoff. ``attempt`` starts at 1 for the first retry."""
    rng = rng or random
    cap = min(max_ms, base_ms * (2 ** (attempt - 1)))
    return rng.uniform(0, cap) / 1000
