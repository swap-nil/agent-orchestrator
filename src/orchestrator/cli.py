"""Operator CLI: ``python -m orchestrator.cli <command>``.

    validate-config [file]     load + validate config and catalogue, print a summary
    gen-keys                   print fresh session, approval and dispatch keys
    migrate-audit              create the PostgreSQL audit table (idempotent)
    verify-audit <chain_id>    verify a chain in the configured audit sink
    approval-public-key        print the PEM public key downstream services use to verify approvals
    run-evals [--min 0.95]     run the golden eval suite against the configured catalogue (CI gate)
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import sys

from .audit import verify_chain
from .catalogue import load_catalogue
from .config import ConfigError, load_config, resolve_secret


def _validate(path: str | None) -> int:
    try:
        config = load_config(path)
        cat = load_catalogue(config.catalogue.intents_file, config.catalogue.registry_file, config.auth.acr_levels)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"profile={config.profile} environment={config.service.environment} cell={config.service.cell_id}")
    print(f"intents={len(cat.intents)} agents={len(cat.agents)} policy={config.policy.engine} audit={config.audit.sink}")
    return 0


def _gen_keys() -> int:
    from cryptography.fernet import Fernet

    print(f"ORCH_SESSION_KEY={Fernet.generate_key().decode()}")
    print(f"ORCH_APPROVAL_KEY={base64.urlsafe_b64encode(secrets.token_bytes(48)).decode()}")
    print(f"MA_DISPATCH_KEY={base64.urlsafe_b64encode(secrets.token_bytes(48)).decode()}")
    print(f"ORCH_TEMPORAL_PAYLOAD_KEY={Fernet.generate_key().decode()}")
    return 0


async def _migrate() -> int:
    from .adapters.postgres_audit import PostgresAuditSink

    config = load_config()
    await PostgresAuditSink(resolve_secret(config.audit.postgres_dsn_env), config.audit.table).migrate()
    print("audit table ready")
    return 0


async def _verify(chain_id: str) -> int:
    from .bootstrap import _default_audit_sink

    config = load_config()
    records = await _default_audit_sink(config, os.environ).chain(chain_id)
    ok, bad = verify_chain(records)
    print(f"chain={chain_id} records={len(records)} valid={ok} first_invalid={bad}")
    return 0 if ok else 2


async def _run_evals(minimum: float | None) -> int:
    from .console.evals import EvalRunner, load_suite

    config = load_config()
    cat = load_catalogue(config.catalogue.intents_file, config.catalogue.registry_file, config.auth.acr_levels)
    run = await EvalRunner(config, load_suite(config.command_center.evals_file)).run(
        cat, config.guards, config.routing, label="cli", runtime_version=0, record=False)
    for name, t in sorted(run.totals.items()):
        print(f"{name:8s} {t['passed']:3d}/{t['total']:<3d}")
    for r in run.results:
        if not r.passed:
            print(f"FAIL {r.id}: expected {r.expected!r}, got {r.actual!r} {r.detail}")
    threshold = config.command_center.change_min_pass_rate if minimum is None else minimum
    print(f"pass rate {run.pass_rate:.1%} (gate {threshold:.0%})")
    return 0 if run.pass_rate >= threshold else 3


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]
    if cmd == "validate-config":
        return _validate(rest[0] if rest else None)
    if cmd == "gen-keys":
        return _gen_keys()
    if cmd == "migrate-audit":
        return asyncio.run(_migrate())
    if cmd == "approval-public-key":
        from .approvals import ApprovalSigner

        config = load_config()
        print(ApprovalSigner(resolve_secret(config.workflows.approval_signing_key_env).encode()).public_key_pem(), end="")
        return 0
    if cmd == "run-evals":
        return asyncio.run(_run_evals(float(rest[rest.index("--min") + 1]) if "--min" in rest else None))
    if cmd == "verify-audit" and rest:
        return asyncio.run(_verify(rest[0]))
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
