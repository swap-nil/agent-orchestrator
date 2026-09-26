"""PostgreSQL audit sink.

Appends are serialised per chain with a transaction-scoped advisory lock, so
replicas never fork a chain. Grant the service INSERT and SELECT only; no
UPDATE or DELETE. Replicate the table to immutable (WORM) storage for
long-term retention.
"""

from __future__ import annotations

import json

import asyncpg

from ..audit import GENESIS, AuditRecord, canonical_json, seal

SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    chain_id   text        NOT NULL,
    seq        integer     NOT NULL,
    record_id  text        NOT NULL UNIQUE,
    ts         double precision NOT NULL,
    event      text        NOT NULL,
    session_id text        NOT NULL,
    turn_id    text        NOT NULL,
    trace_id   text        NOT NULL,
    actor      text        NOT NULL,
    data       jsonb       NOT NULL,
    prev_hash  char(64)    NOT NULL,
    hash       char(64)    NOT NULL,
    PRIMARY KEY (chain_id, seq)
);
CREATE INDEX IF NOT EXISTS {table}_trace_idx ON {table} (trace_id);
CREATE INDEX IF NOT EXISTS {table}_session_idx ON {table} (session_id);
"""


class PostgresAuditSink:
    def __init__(self, dsn: str, table: str = "audit_ledger", min_size: int = 2, max_size: int = 10) -> None:
        if not table.replace("_", "").isalnum():
            raise ValueError("invalid table name")
        self._dsn = dsn
        self._table = table
        self._pool: asyncpg.Pool | None = None
        self._min, self._max = min_size, max_size

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=self._min, max_size=self._max)
        return self._pool

    async def migrate(self) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA.format(table=self._table))

    async def append(self, record: AuditRecord) -> AuditRecord:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", record.chain_id)
            row = await conn.fetchrow(
                f"SELECT seq, hash FROM {self._table} WHERE chain_id=$1 ORDER BY seq DESC LIMIT 1", record.chain_id
            )
            prev, seq = (row["hash"], row["seq"] + 1) if row else (GENESIS, 0)
            seal(record, prev, seq)
            await conn.execute(
                f"INSERT INTO {self._table} (chain_id, seq, record_id, ts, event, session_id, turn_id, trace_id, actor, data, prev_hash, hash)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12)",
                record.chain_id, record.seq, record.record_id, record.ts, record.event, record.session_id,
                record.turn_id, record.trace_id, record.actor, canonical_json(record.data), record.prev_hash, record.hash,
            )
        return record

    async def chain(self, chain_id: str) -> list[AuditRecord]:
        pool = await self._get_pool()
        rows = await pool.fetch(f"SELECT * FROM {self._table} WHERE chain_id=$1 ORDER BY seq", chain_id)
        return [
            AuditRecord(
                chain_id=r["chain_id"], event=r["event"], data=json.loads(r["data"]), session_id=r["session_id"],
                turn_id=r["turn_id"], trace_id=r["trace_id"], actor=r["actor"], record_id=r["record_id"], ts=r["ts"],
                seq=r["seq"], prev_hash=r["prev_hash"], hash=r["hash"],
            )
            for r in rows
        ]
