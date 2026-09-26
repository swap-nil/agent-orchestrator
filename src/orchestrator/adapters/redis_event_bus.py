"""Redis Streams event bus: every replica publishes into one cell-wide stream.

The console's replica consumes the stream (XREAD) into its local buffer, so
metrics, alerts and the live feed cover the whole cell, not one pod.
Trim with MAXLEN ~ to bound memory; the audit ledger stays the durable record.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any, AsyncIterator

import redis.asyncio as redis

from ..audit import AuditRecord
from ..console.events import DecisionEvent, InMemoryEventBus


class RedisEventBus(InMemoryEventBus):
    def __init__(self, url: str, stream_key: str, maxlen: int, buffer_size: int) -> None:
        super().__init__(buffer_size=buffer_size)
        self._r = redis.from_url(url, decode_responses=True)
        self._key = stream_key
        self._maxlen = maxlen
        self._consumer: asyncio.Task[None] | None = None
        self._seq_counter = 0

    async def publish(self, record: AuditRecord) -> None:
        # Local delivery happens when the consumer reads the entry back, so ordering is the stream's order.
        await self._r.xadd(self._key, {"r": json.dumps(asdict(record), default=str)}, maxlen=self._maxlen, approximate=True)

    def start(self) -> None:
        if self._consumer is None:
            self._consumer = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        last_id = "$"
        while True:
            try:
                batches = await self._r.xread({self._key: last_id}, block=5000, count=500)
            except redis.RedisError:
                await asyncio.sleep(1)
                continue
            for _stream, entries in batches or []:
                for entry_id, fields in entries:
                    last_id = entry_id
                    try:
                        record = AuditRecord(**json.loads(fields["r"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    self._seq_counter += 1
                    self.deliver(DecisionEvent.from_record(self._seq_counter, record))

    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[DecisionEvent]:  # type: ignore[override]
        self.start()
        async for event in super().subscribe(after_seq):
            yield event

    def extra(self) -> dict[str, Any]:
        return {"stream": self._key}
