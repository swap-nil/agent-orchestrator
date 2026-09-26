"""Admission control and load shedding for one orchestrator replica.

Utilisation is in-flight turns divided by ``max_concurrent_turns``.

* Below the soft limit: everything is admitted.
* Between soft and hard: turns are admitted in degraded mode, so optional plan
  steps are skipped.
* At or above the hard limit: only transaction continuations (approvals) and
  handovers are admitted; everything else gets a polite "busy" answer.

Cell-level admission of new sessions happens in the token service.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Iterator

from .config import AdmissionConfig


class Rejected(Exception):
    pass


@dataclass
class Admission:
    degraded: bool


class AdmissionController:
    def __init__(self, config: AdmissionConfig) -> None:
        self._config = config
        self._in_flight = 0

    @property
    def utilisation(self) -> float:
        return self._in_flight / self._config.max_concurrent_turns

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @contextlib.contextmanager
    def admit(self, *, priority: bool = False) -> Iterator[Admission]:
        util = self.utilisation
        if util >= self._config.hard_limit_ratio and not priority:
            raise Rejected("orchestrator at capacity")
        degraded = util >= self._config.soft_limit_ratio
        self._in_flight += 1
        try:
            yield Admission(degraded=degraded)
        finally:
            self._in_flight -= 1
