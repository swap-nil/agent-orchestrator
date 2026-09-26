"""Decision event bus.

Every audit record is also published as a *decision event*. The command
center's live feed, metrics and alerts are all derived from this one stream,
so what operators see is exactly what the tamper-evident ledger records.

* ``InMemoryEventBus``: one replica, ring buffer plus bounded subscriber
  queues. Right for development and for a single-replica cell.
* ``adapters/redis_event_bus.py``: Redis Streams, so every replica publishes
  into one cell-wide stream and the console sees all of them.

Publishing is best effort: a slow or failing bus never delays or fails a turn.
Slow subscribers lose their oldest events rather than blocking publishers.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncIterator, Protocol

from ..audit import AuditRecord


@dataclass
class DecisionEvent:
    seq: int
    id: str
    ts: float
    chain: str
    event: str
    session_id: str
    turn_id: str
    trace_id: str
    actor: str
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(cls, seq: int, record: AuditRecord) -> "DecisionEvent":
        return cls(
            seq=seq, id=record.record_id, ts=record.ts, chain=record.chain_id, event=record.event,
            session_id=record.session_id, turn_id=record.turn_id, trace_id=record.trace_id,
            actor=record.actor, data=dict(record.data),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus(Protocol):
    async def publish(self, record: AuditRecord) -> None: ...
    def recent(self, limit: int = 200, after_seq: int = 0) -> list[DecisionEvent]: ...
    def subscribe(self, after_seq: int = 0) -> AsyncIterator[DecisionEvent]: ...
    def add_listener(self, listener: Any) -> None: ...


class InMemoryEventBus:
    def __init__(self, buffer_size: int = 5000, queue_size: int = 1000) -> None:
        self._buffer: deque[DecisionEvent] = deque(maxlen=buffer_size)
        self._queues: set[asyncio.Queue[DecisionEvent]] = set()
        self._queue_size = queue_size
        self._seq = itertools.count(1)
        self._listeners: list[Any] = []
        self.dropped = 0

    def add_listener(self, listener: Any) -> None:
        """A synchronous callable invoked for every event (used by the telemetry aggregator)."""
        self._listeners.append(listener)

    async def publish(self, record: AuditRecord) -> None:
        self.deliver(DecisionEvent.from_record(next(self._seq), record))

    def deliver(self, event: DecisionEvent) -> None:
        self._buffer.append(event)
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - a broken listener must not affect others or the turn
                pass
        for queue in list(self._queues):
            if queue.full():
                try:
                    queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    def recent(self, limit: int = 200, after_seq: int = 0) -> list[DecisionEvent]:
        items = [e for e in self._buffer if e.seq > after_seq]
        return items[-limit:]

    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[DecisionEvent]:  # type: ignore[override]
        queue: asyncio.Queue[DecisionEvent] = asyncio.Queue(maxsize=self._queue_size)
        backlog = self.recent(self._buffer.maxlen or 0, after_seq)
        self._queues.add(queue)
        try:
            for event in backlog:
                yield event
            last = backlog[-1].seq if backlog else after_seq
            while True:
                event = await queue.get()
                if event.seq > last:
                    last = event.seq
                    yield event
        finally:
            self._queues.discard(queue)
