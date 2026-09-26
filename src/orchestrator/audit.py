"""Tamper-evident audit ledger.

Each record carries the hash of the previous record in the same chain, so any
edit or deletion breaks verification. A chain is one session or one workflow.
Sinks are responsible for appending atomically per chain (the PostgreSQL sink
uses a transaction-scoped advisory lock; the in-process sinks use asyncio
locks). Records never contain raw user text: callers pass redacted data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

GENESIS = "0" * 64


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass
class AuditRecord:
    chain_id: str
    event: str
    data: dict[str, Any]
    session_id: str = ""
    turn_id: str = ""
    trace_id: str = ""
    actor: str = "orchestrator"
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    seq: int = 0
    prev_hash: str = GENESIS
    hash: str = ""

    def payload(self) -> dict[str, Any]:
        body = asdict(self)
        body.pop("hash")
        return body


def compute_hash(record: AuditRecord) -> str:
    return hashlib.sha256((record.prev_hash + canonical_json(record.payload())).encode("utf-8")).hexdigest()


def seal(record: AuditRecord, prev_hash: str, seq: int) -> AuditRecord:
    record.prev_hash = prev_hash
    record.seq = seq
    record.hash = compute_hash(record)
    return record


def verify_chain(records: list[AuditRecord]) -> tuple[bool, int | None]:
    """Verify one chain ordered by seq. Returns (ok, index of first bad record)."""
    prev = GENESIS
    for index, record in enumerate(records):
        if record.prev_hash != prev or record.seq != index or compute_hash(record) != record.hash:
            return False, index
        prev = record.hash
    return True, None


class AuditSink(Protocol):
    async def append(self, record: AuditRecord) -> AuditRecord: ...
    async def chain(self, chain_id: str) -> list[AuditRecord]: ...


class InMemoryAuditSink:
    def __init__(self) -> None:
        self._chains: dict[str, list[AuditRecord]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def append(self, record: AuditRecord) -> AuditRecord:
        lock = self._locks.setdefault(record.chain_id, asyncio.Lock())
        async with lock:
            chain = self._chains.setdefault(record.chain_id, [])
            prev = chain[-1].hash if chain else GENESIS
            chain.append(seal(record, prev, len(chain)))
            return record

    async def chain(self, chain_id: str) -> list[AuditRecord]:
        return list(self._chains.get(chain_id, []))


class JsonlAuditSink(InMemoryAuditSink):
    """Development sink: appends JSON lines to a file. Not for production (single process only)."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = Path(path)
        self._file_lock = asyncio.Lock()

    async def append(self, record: AuditRecord) -> AuditRecord:
        sealed = await super().append(record)
        line = canonical_json(asdict(sealed)) + "\n"
        async with self._file_lock:
            await asyncio.to_thread(self._write, line)
        return sealed

    def _write(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)


class AuditLog:
    """Facade used by the service. Raises if the sink fails and the config says turns must fail."""

    def __init__(self, sink: AuditSink, fail_on_error: bool, service_name: str, bus: Any = None) -> None:
        self._sink = sink
        self._fail = fail_on_error
        self._actor = service_name
        self.bus = bus  # optional decision event bus (command center); published after the record is sealed
        self.errors = 0
        self.publish_errors = 0

    async def record(
        self,
        chain_id: str,
        event: str,
        data: dict[str, Any],
        *,
        session_id: str = "",
        turn_id: str = "",
        trace_id: str = "",
    ) -> AuditRecord | None:
        record = AuditRecord(
            chain_id=chain_id, event=event, data=data, session_id=session_id,
            turn_id=turn_id, trace_id=trace_id, actor=self._actor,
        )
        try:
            sealed = await self._sink.append(record)
        except Exception:
            self.errors += 1
            if self._fail:
                raise
            return None
        if self.bus is not None:
            try:
                await self.bus.publish(sealed)
            except Exception:  # noqa: BLE001 - observability must never fail a turn
                self.publish_errors += 1
        return sealed

    async def chain(self, chain_id: str) -> list[AuditRecord]:
        return await self._sink.chain(chain_id)
