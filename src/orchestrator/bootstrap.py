"""Builds an :class:`OrchestratorService` from configuration.

Production adapters (httpx, Redis, PostgreSQL, Temporal) are imported lazily so
the core and its tests run without them. Any dependency can be injected, which
is how the tests and the local demo run fully in memory.
"""

from __future__ import annotations

import os
from typing import Any

from .a2a import A2AClient
from .admission import AdmissionController
from .approvals import ApprovalService, ApprovalSigner, WorkflowGateway
from .audit import AuditLog, AuditSink, InMemoryAuditSink, JsonlAuditSink
from .catalogue import Catalogue, load_catalogue
from .config import ConfigError, OrchestratorConfig, resolve_secret
from .executor import Executor
from .guards import ExternalClassifier, InputGuard, OutputGuard
from .identity import TokenExchanger
from .planner import Planner
from .policy import LocalPolicyEngine, OpaPolicyEngine, PolicyEnforcementPoint
from .resilience import CircuitBreakers
from .router import ModelClassifier, Router
from .service import Components, OrchestratorService
from .state import InMemorySessionStore, SessionStore, TokenCipher
from .transport import HttpTransport


def _mesh_transport(config: OrchestratorConfig) -> HttpTransport:
    """A2A gateway: mesh CA + workload certificate (mTLS)."""
    from .adapters.httpx_transport import HttpxTransport
    from .tls import client_ssl_context

    a = config.a2a
    return HttpxTransport(ssl_context=client_ssl_context(
        verify=a.verify_tls, ca_bundle=a.ca_bundle, cert_file=a.client_cert_file, key_file=a.client_key_file,
    ))


def _public_transport() -> HttpTransport:
    """IdP, model endpoint, OPA sidecar: system trust store, no client certificate."""
    from .adapters.httpx_transport import HttpxTransport

    return HttpxTransport(ssl_context=True)


def _default_store(config: OrchestratorConfig, environ: Any) -> SessionStore:
    if config.session.store == "memory":
        return InMemorySessionStore(ttl_s=config.session.ttl_s)
    from .adapters.redis_store import RedisSessionStore

    return RedisSessionStore(
        url=resolve_secret(config.session.redis_url_env, environ),
        ttl_s=config.session.ttl_s,
        lock_timeout_ms=config.session.lock_timeout_ms,
    )


def _default_audit_sink(config: OrchestratorConfig, environ: Any) -> AuditSink:
    if config.audit.sink == "memory":
        return InMemoryAuditSink()
    if config.audit.sink == "jsonl":
        return JsonlAuditSink(config.audit.jsonl_path)
    from .adapters.postgres_audit import PostgresAuditSink

    return PostgresAuditSink(dsn=resolve_secret(config.audit.postgres_dsn_env, environ), table=config.audit.table)


def _default_event_bus(config: OrchestratorConfig, environ: Any) -> Any:
    cc = config.command_center
    if cc.event_bus == "memory":
        from .console.events import InMemoryEventBus

        return InMemoryEventBus(buffer_size=cc.buffer_events)
    from .adapters.redis_event_bus import RedisEventBus

    return RedisEventBus(resolve_secret(cc.redis_url_env, environ), cc.stream_key, cc.stream_maxlen, cc.buffer_events)


def build_service(
    config: OrchestratorConfig,
    *,
    catalogue: Catalogue | None = None,
    transport: HttpTransport | None = None,
    public_transport: HttpTransport | None = None,
    store: SessionStore | None = None,
    audit_sink: AuditSink | None = None,
    workflows: WorkflowGateway | None = None,
    model_classifier: ModelClassifier | None = None,
    input_classifier: ExternalClassifier | None = None,
    event_bus: Any = None,
    environ: Any = None,
) -> OrchestratorService:
    environ = os.environ if environ is None else environ
    catalogue = catalogue or load_catalogue(
        config.catalogue.intents_file, config.catalogue.registry_file, config.auth.acr_levels
    )
    # When a single transport is injected (tests, demos) it is used for everything.
    mesh = transport or _mesh_transport(config)
    public = public_transport or transport or _public_transport()
    store = store or _default_store(config, environ)
    sink = audit_sink or _default_audit_sink(config, environ)

    cipher_key = environ.get(config.session.token_encryption_key_env, "")
    if config.profile == "prod" and not cipher_key:
        raise ConfigError(f"prod: {config.session.token_encryption_key_env} must hold a Fernet key")
    cipher = TokenCipher(cipher_key or None)

    if config.policy.engine == "opa":
        engine = OpaPolicyEngine(config.policy, public)
    else:
        engine = LocalPolicyEngine()
    pep = PolicyEnforcementPoint(engine, config.policy)

    client_secret = ""
    if config.identity.mode != "disabled" and config.identity.client_auth == "secret":
        client_secret = resolve_secret(config.identity.client_secret_env, environ)
    tokens = TokenExchanger(config.identity, public, client_secret)

    executor = Executor(
        config.execution, catalogue, A2AClient(config.a2a, mesh), tokens,
        CircuitBreakers(config.execution.circuit_breaker),
    )

    approvals = None
    approval_public_key = None
    if config.workflows.enabled:
        signer = ApprovalSigner(resolve_secret(config.workflows.approval_signing_key_env, environ).encode())
        approval_public_key = signer.public_key
        approvals = ApprovalService(
            store, signer, config.auth.acr_levels, config.auth.approval_min_acr, config.workflows.approval_timeout_s
        )
        if workflows is None:
            from .adapters.temporal_gateway import TemporalWorkflowGateway

            workflows = TemporalWorkflowGateway(config.workflows, config.profile)

    if config.routing.model_classifier.enabled and model_classifier is None:
        from .adapters.model_classifier import OpenAICompatibleClassifier

        model_classifier = OpenAICompatibleClassifier(
            config.routing.model_classifier, public,
            resolve_secret(config.routing.model_classifier.api_key_env, environ),
        )

    if event_bus is None and config.command_center.enabled:
        event_bus = _default_event_bus(config, environ)

    components = Components(
        config=config,
        catalogue=catalogue,
        router=Router(catalogue, config.routing, model_classifier),
        planner=Planner(catalogue, config.budgets, config.auth.acr_levels, config.service.environment),
        pep=pep,
        executor=executor,
        input_guard=InputGuard(config.guards, input_classifier),
        output_guard=OutputGuard(config.guards),
        audit=AuditLog(sink, config.audit.fail_turn_on_audit_error, config.service.name, bus=event_bus),
        store=store,
        cipher=cipher,
        admission=AdmissionController(config.admission),
        approvals=approvals,
        workflows=workflows if config.workflows.enabled else None,
        approval_public_key=approval_public_key,
    )
    return OrchestratorService(components)
