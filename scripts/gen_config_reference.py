"""Generate docs/CONFIG_REFERENCE.md from the configuration dataclasses.

    PYTHONPATH=src python scripts/gen_config_reference.py > docs/CONFIG_REFERENCE.md

Every key must have a description below; tests/test_docs.py fails otherwise.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import typing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from master_agent.config import MasterAgentConfig  # noqa: E402
from orchestrator.config import OrchestratorConfig  # noqa: E402
from token_service.config import TokenServiceConfig  # noqa: E402

ORCH = {
    "profile": "`dev` or `prod`. `prod` enforces the production baseline and refuses to start if any rule is violated.",
    "service.name": "Service name used in telemetry and as the audit actor.",
    "service.environment": "Environment name checked against each agent's `certified_in` list (e.g. dev, test, prod).",
    "service.cell_id": "Cell identifier, recorded in policy input and telemetry.",
    "service.region": "Azure region, recorded in telemetry resource attributes.",
    "service.log_level": "Python log level.",
    "server.host": "Bind address for the API server.",
    "server.port": "Port for the API server.",
    "server.request_body_limit_bytes": "Requests with a larger Content-Length are rejected with 413. Also enforce at ingress.",
    "auth.mode": "How callers authenticate: `none` (dev only), `jwt` (service tokens) or `mesh_xfcc` (SPIFFE ID from Envoy XFCC).",
    "auth.jwt.issuer": "Expected issuer of service tokens (auth.mode=jwt).",
    "auth.jwt.audience": "Expected audience of service tokens.",
    "auth.jwt.jwks_url": "JWKS URL for service token signature keys.",
    "auth.jwt.algorithms": "Accepted signature algorithms. Asymmetric only in prod.",
    "auth.jwt.leeway_s": "Allowed clock skew in seconds.",
    "auth.acr_levels": "Authentication levels, weakest first. Intents and approvals refer to these names.",
    "auth.approval_min_acr": "Minimum level required to approve a transaction (step-up).",
    "auth.route_callers": "Map of route group (turns, sessions, approvals, workflows, admin) to permitted caller identities.",
    "auth.user_jwt.issuer": "Expected issuer of end-user tokens.",
    "auth.user_jwt.audience": "Expected audience of end-user tokens.",
    "auth.user_jwt.jwks_url": "JWKS URL for end-user token keys.",
    "auth.user_jwt.algorithms": "Accepted algorithms for end-user tokens.",
    "auth.user_jwt.leeway_s": "Allowed clock skew for end-user tokens.",
    "auth.user_subject_claim": "Claim holding the user id (Entra: `oid`).",
    "auth.user_acr_claim": "Claim holding the authentication level (string or list; Entra: `acrs`).",
    "auth.user_tenant_claim": "Claim holding the tenant.",
    "catalogue.intents_file": "Path to the intent catalogue YAML.",
    "catalogue.registry_file": "Path to the agent registry YAML.",
    "routing.min_confidence": "Minimum routing confidence per risk class (R0..R3); below it the user is asked to clarify.",
    "routing.max_clarification_rounds": "Clarifications before handing over to a human.",
    "routing.fallback_intent": "R0 intent used when nothing matches (e.g. FAQ). Empty: clarify, then hand over.",
    "routing.model_classifier.enabled": "Enable the model classifier for unmatched requests (R0/R1 intents only).",
    "routing.model_classifier.endpoint": "OpenAI-compatible chat completions URL (in-region).",
    "routing.model_classifier.model": "Model or deployment name.",
    "routing.model_classifier.api_key_env": "Environment variable holding the classifier API key.",
    "routing.model_classifier.timeout_ms": "Classifier timeout; on timeout routing falls back.",
    "routing.model_classifier.allowed_risk_classes": "Risk classes the model may choose. Validation allows only R0 and R1.",
    "budgets.turn_deadline_ms": "Total time budget for one voice turn, all steps included.",
    "budgets.max_steps": "Maximum steps in a plan.",
    "budgets.max_depth": "Maximum dependency depth (layers) of a plan. Hard limit 5.",
    "budgets.max_fan_out": "Maximum parallel steps in one layer.",
    "budgets.max_cost_units_per_turn": "Maximum plan cost (sum of step cost units) per turn.",
    "budgets.max_cost_units_per_session": "Maximum cost units per session.",
    "budgets.max_turns_per_session": "Turns after which the session is handed over.",
    "execution.default_step_timeout_ms": "Per-call timeout when a step sets none. Must not exceed the turn deadline.",
    "execution.read_retries": "Retries for read steps on retryable errors. Writes are never retried by the executor.",
    "execution.retry_base_delay_ms": "Base delay for full-jitter exponential backoff.",
    "execution.retry_max_delay_ms": "Maximum backoff delay.",
    "execution.circuit_breaker.failure_threshold": "Consecutive failures that open an agent's breaker.",
    "execution.circuit_breaker.reset_after_s": "Seconds before one probe call is allowed.",
    "execution.default_quorum": "Success rule for a plan: `all`, `majority` or `any` required step(s). Intents may override.",
    "a2a.gateway_url": "Base URL of the A2A mesh gateway. https in prod.",
    "a2a.agent_path_template": "Path per agent on the gateway; `{agent}` is replaced by the agent name.",
    "a2a.protocol_version": "Sent as the `A2A-Version` header.",
    "a2a.method_send": "JSON-RPC method for sending a message.",
    "a2a.method_get": "JSON-RPC method for reading a task.",
    "a2a.method_cancel": "JSON-RPC method for cancelling a task.",
    "a2a.verify_tls": "Verify the gateway certificate. Must be true in prod.",
    "a2a.ca_bundle": "Mesh CA bundle path (used only for the gateway, not for public endpoints).",
    "a2a.client_cert_file": "Workload certificate (SPIFFE SVID) for mTLS to the gateway.",
    "a2a.client_key_file": "Private key for the workload certificate.",
    "a2a.required_extensions": "Values sent in the `A2A-Extensions` header.",
    "a2a.user_role_value": "Role value used in A2A messages.",
    "policy.engine": "`opa` (prod) or `local` (Python mirror of the Rego policy, dev/test).",
    "policy.opa_url": "OPA base URL; normally the localhost sidecar.",
    "policy.decision_path": "OPA data path of the decision document.",
    "policy.timeout_ms": "Policy call timeout. Timeouts are denials (fail closed).",
    "policy.cache_ttl_s": "Cache lifetime for allow decisions on R0/R1 reads. 0 disables caching.",
    "policy.cache_risk_classes": "Risk classes whose read allows may be cached.",
    "policy.allowed_channels": "Channels on which intents may run.",
    "identity.mode": "`entra_obo`, `rfc8693` or `disabled` (dev). Exchanges the user token per agent call.",
    "identity.token_endpoint": "IdP token endpoint.",
    "identity.client_id": "Orchestrator's client id at the IdP.",
    "identity.client_auth": "`workload_identity` (AKS federated assertion, prod), `managed_identity` (managed identity token from IMDS as the federated assertion, Azure VM) or `secret` (dev).",
    "identity.federated_token_file": "Path to the projected federated token; default `$AZURE_FEDERATED_TOKEN_FILE`.",
    "identity.managed_identity_client_id": "Client id of the managed identity used with `client_auth: managed_identity`; default `$AZURE_CLIENT_ID`.",
    "identity.client_secret_env": "Environment variable with the client secret (client_auth=secret only).",
    "identity.sender_constraint": "Declared token binding at the resource: `mtls` or `dpop` (prod), `none` (dev).",
    "identity.refresh_skew_s": "Refresh delegated tokens this many seconds before expiry.",
    "identity.timeout_ms": "Token endpoint timeout.",
    "session.store": "`redis` (prod) or `memory` (dev).",
    "session.redis_url_env": "Environment variable with the Redis URL (use rediss:// with TLS).",
    "session.ttl_s": "Session lifetime in the store (sliding).",
    "session.lock_timeout_ms": "Per-session lock TTL. Must exceed the turn deadline by 500 ms or more.",
    "session.token_encryption_key_env": "Environment variable with the Fernet key encrypting user tokens at rest. Required in prod.",
    "audit.sink": "`postgres` (prod), `jsonl` (dev) or `memory` (tests).",
    "audit.jsonl_path": "File for the jsonl sink.",
    "audit.postgres_dsn_env": "Environment variable with the PostgreSQL DSN.",
    "audit.table": "Audit table name.",
    "audit.fail_turn_on_audit_error": "If true, a turn fails safely when its audit record cannot be written. Must be true in prod.",
    "guards.injection_action": "`block` (prod) or `flag` suspected prompt injection.",
    "guards.injection_patterns": "Case-insensitive regular expressions for prompt-injection heuristics.",
    "guards.prohibited_phrases": "Phrases that must never be spoken; the answer is withheld.",
    "guards.pressure_phrases": "Pressure-selling phrases blocked on advice and transactions.",
    "guards.r2_disclaimer": "Appended to every advice (R2) answer.",
    "guards.max_input_chars": "Longer user inputs are refused.",
    "guards.redact_pii_in_logs": "Redact IBANs, card numbers, emails and phone numbers before audit/logging.",
    "guards.response_allowed_classifications": "Artifact classifications that may be spoken to the user.",
    "admission.max_concurrent_turns": "Concurrent turns per replica.",
    "admission.soft_limit_ratio": "Utilisation above which optional steps are skipped (degraded mode).",
    "admission.hard_limit_ratio": "Utilisation above which only transactions are admitted; others get the busy message.",
    "workflows.enabled": "Enable durable transactions (Temporal). Required in prod; R3 intents are refused when off.",
    "workflows.temporal_target": "Temporal frontend host:port.",
    "workflows.namespace": "Temporal namespace.",
    "workflows.task_queue": "Task queue served by the worker.",
    "workflows.approval_timeout_s": "Time the user has to approve; then the workflow expires and nothing is done.",
    "workflows.approval_signing_key_env": "Environment variable with the approval signing secret (32+ bytes, Ed25519 seed source).",
    "workflows.payload_key_env": "Environment variable with the Fernet key encrypting Temporal payloads. Required in prod.",
    "workflows.tls": "Use TLS to Temporal.",
    "telemetry.enabled": "Export traces and metrics over OTLP.",
    "telemetry.otlp_endpoint": "OTLP gRPC endpoint (usually the collector).",
    "telemetry.capture_content": "Capture prompts/answers in spans. Must be false in prod.",
    "telemetry.semconv_stability_opt_in": "Pinned value for OTEL_SEMCONV_STABILITY_OPT_IN (GenAI conventions).",
    "kill_switch.disabled_agents": "Agents switched off by configuration (merged with runtime switches).",
    "kill_switch.disabled_intents": "Intents switched off by configuration.",
    "kill_switch.disabled_risk_classes": "Risk classes switched off (e.g. R3 during an incident).",
    "messages.refused": "Spoken when a request is refused (policy, plan, authentication).",
    "messages.blocked_input": "Spoken when the input guard blocks a request.",
    "messages.busy": "Spoken under load shedding.",
    "messages.handover": "Spoken when handing over to a human advisor.",
    "messages.clarify_default": "Clarifying question when the intent has none.",
    "messages.clarify_choice": "Question when a request matches several intents; `{options}` lists their labels.",
    "messages.clarify_repeat_prefix": "Put before a clarifying question when the user repeats the same request.",
    "messages.out_of_scope": "Spoken when nothing (including the knowledge base) can answer the request; says what the assistant can do.",
    "messages.cancelled": "Spoken when the user cancels an open question (\"never mind\").",
    "messages.action_mismatch": "Spoken when a prepared transaction does not match what the user asked for; nothing is sent for approval.",
    "messages.failure": "Spoken when agents fail on information requests.",
    "messages.partial_suffix": "Appended when part of an answer is missing.",
    "messages.transactions_unavailable": "Spoken when transactions are disabled or unavailable.",
    "messages.approval_prompt": "Appended to the read-back of a transaction.",
    "messages.session_invalid": "Spoken for unknown or closed sessions.",
    "command_center.enabled": "Enable the command center: decision event bus, metrics, alerts, evals, runtime change control and the console API.",
    "command_center.event_bus": "`redis` (prod: one cell-wide stream across replicas) or `memory` (single replica, dev).",
    "command_center.redis_url_env": "Environment variable with the Redis URL for the event stream.",
    "command_center.stream_key": "Redis stream key for decision events (one per cell).",
    "command_center.stream_maxlen": "Approximate maximum stream length; the audit ledger remains the durable record.",
    "command_center.buffer_events": "Events kept in memory for the live feed and stream resume.",
    "command_center.metrics_bucket_s": "Width of one metrics bucket in seconds.",
    "command_center.metrics_retention_s": "How far back metrics are kept in memory. Long-term metrics belong in your OTel backend.",
    "command_center.operator_auth": "How operators sign in: `jwt` (Entra ID app roles, prod) or `none` (dev: X-Operator header).",
    "command_center.operator_jwt.issuer": "Expected issuer of operator tokens.",
    "command_center.operator_jwt.audience": "Expected audience of operator tokens (the console's app registration).",
    "command_center.operator_jwt.jwks_url": "JWKS URL for operator token keys.",
    "command_center.operator_jwt.algorithms": "Accepted algorithms for operator tokens.",
    "command_center.operator_jwt.leeway_s": "Allowed clock skew for operator tokens.",
    "command_center.operator_roles_claim": "Claim listing the operator's app roles.",
    "command_center.role_members": "Console role (viewer, operator, investigator, approver) to the IdP role values that grant it.",
    "command_center.show_utterances": "Show PII-redacted user utterances to investigators. Everyone else always sees lengths only.",
    "command_center.require_reason_for_actions": "Operator actions (kill switch, terminate, rollback) require a written reason. Must be true in prod.",
    "command_center.alert_rules": "Alert rules: id, metric path, op, threshold, window, minimum samples, severity. See the user guide.",
    "command_center.evals_file": "Golden eval dataset used for eval runs and to gate runtime changes.",
    "command_center.runtime_changes_enabled": "Allow governed runtime changes to behavioural configuration from the console.",
    "command_center.require_four_eyes": "A runtime change must be approved by someone other than its proposer. Must be true in prod.",
    "command_center.change_min_pass_rate": "Minimum eval pass rate a candidate change must reach (in addition to no regressions).",
    "command_center.change_ttl_s": "Proposed changes expire after this long if not approved.",
}

MASTER = {
    "agent_name": "LiveKit agent name for explicit dispatch; must match the token service.",
    "dispatch_key_env": "Environment variable with the HMAC key for dispatch metadata (shared with the token service).",
    "dispatch_max_age_s": "Maximum age of dispatch metadata. Must be at least the token service's LiveKit token TTL.",
    "stt.provider": "Speech-to-text provider key (see master_agent/providers.py).",
    "stt.options": "Keyword arguments passed to the STT plugin (model, language, base_url...).",
    "tts.provider": "Text-to-speech provider key.",
    "tts.options": "Keyword arguments passed to the TTS plugin (voice, model, base_url...).",
    "vad.provider": "Voice activity detection provider key.",
    "vad.options": "Keyword arguments for the VAD plugin.",
    "turn_detection": "`multilingual` (turn-detector model) or `vad`.",
    "orchestrator.url": "Orchestrator base URL.",
    "orchestrator.timeout_ms": "Timeout for one turn call; should exceed the orchestrator's turn deadline.",
    "orchestrator.verify_tls": "Verify the orchestrator certificate.",
    "orchestrator.ca_bundle": "CA bundle for the orchestrator (mesh CA).",
    "orchestrator.client_cert_file": "Client certificate for mTLS.",
    "orchestrator.client_key_file": "Client key for mTLS.",
    "orchestrator.auth_scope": "Entra scope for an app-only Workload Identity token sent to the orchestrator (`auth.mode: jwt`), "
                               "e.g. `api://<orchestrator-app-id>/.default`. Empty: no token (mesh identity or local).",
    "behaviour.greeting": "First sentence spoken when the agent joins.",
    "behaviour.holding_phrase": "Spoken when the orchestrator has not answered within holding_after_ms.",
    "behaviour.holding_after_ms": "Delay before the holding phrase.",
    "behaviour.unavailable": "Spoken when the orchestrator cannot be reached.",
    "behaviour.approval_rpc_method": "LiveKit RPC method the client app registers for approval requests.",
    "behaviour.approval_cancel_rpc_method": "LiveKit RPC method asking the client app to decline the open approval when the user says \"cancel\".",
    "behaviour.local_commands": "Handle control phrases (stop, shut up, cancel, repeat, wait, greetings, thanks, help) in the agent instead of the orchestrator.",
    "behaviour.command_replies": "Replies to control phrases, per key: stop, cancel, cancel_unconfirmed, nothing_to_repeat, resume, wait, greeting, thanks, done, goodbye, help.",
    "behaviour.workflow_poll_interval_s": "Polling interval for transaction status.",
    "behaviour.workflow_poll_timeout_s": "Stop polling after this long (approval timeout plus execution).",
    "behaviour.outcome_messages": "Spoken per transaction outcome: completed, declined, expired, failed.",
}

TOKEN = {
    "profile": "`dev` or `prod` (prod requires user token validation, https orchestrator, TTL at most 900 s).",
    "cell_id": "Cell identifier used for admission counting.",
    "orchestrator_url": "Orchestrator base URL.",
    "orchestrator_auth_scope": "Entra scope for an app-only Workload Identity token sent to the orchestrator (`auth.mode: jwt`). "
                               "Empty: no token (mesh identity or local).",
    "orchestrator_ca_bundle": "CA bundle for the orchestrator.",
    "client_cert_file": "Client certificate for mTLS to the orchestrator.",
    "client_key_file": "Client key for mTLS.",
    "dispatch_key_env": "Environment variable with the dispatch HMAC key (shared with the master agent).",
    "user_jwt.issuer": "Expected issuer of user access tokens.",
    "user_jwt.audience": "Expected audience of user access tokens.",
    "user_jwt.jwks_url": "JWKS URL of the IdP.",
    "user_jwt.algorithms": "Accepted algorithms.",
    "user_jwt.leeway_s": "Allowed clock skew.",
    "acr_claim": "Claim holding the authentication level.",
    "acr_levels": "Recognised authentication levels, weakest first.",
    "livekit.url": "LiveKit URL returned to the client.",
    "livekit.api_key_env": "Environment variable with the LiveKit API key.",
    "livekit.api_secret_env": "Environment variable with the LiveKit API secret.",
    "livekit.token_ttl_s": "Lifetime of the participant token.",
    "livekit.agent_name": "Agent to dispatch into the room.",
    "livekit.room_prefix": "Prefix for room names.",
    "admission.enabled": "Enable cell-level session admission (Redis).",
    "admission.redis_url_env": "Environment variable with the Redis URL.",
    "admission.max_sessions_per_cell": "Maximum concurrent sessions per cell.",
    "admission.session_ttl_s": "Lifetime of an admission slot.",
    "allowed_channels": "Channels a client may request.",
}


def keys(cls: type, prefix: str = "") -> list[tuple[str, str, object]]:
    inst = cls()
    hints = typing.get_type_hints(cls)
    out: list[tuple[str, str, object]] = []
    for f in dataclasses.fields(cls):
        t = hints[f.name]
        value = getattr(inst, f.name)
        if dataclasses.is_dataclass(t):
            out += keys(t, prefix + f.name + ".")
        else:
            out.append((prefix + f.name, getattr(t, "__name__", str(t)).replace("typing.", ""), value))
    return out


def env_name(prefix: str, key: str) -> str:
    return prefix + "__".join(p.upper() for p in key.split("."))


def table(cls: type, descriptions: dict[str, str], env_prefix: str) -> str:
    rows = ["| Key | Default | Environment override | Description |", "|---|---|---|---|"]
    for key, _type, default in keys(cls):
        if isinstance(default, list) and default and dataclasses.is_dataclass(default[0]):
            default = [dataclasses.asdict(d) for d in default]
        shown = json.dumps(default) if not isinstance(default, str) else (f"`{default}`" if default else '`""`')
        if not isinstance(default, str):
            shown = f"`{shown}`" if len(shown) < 60 else "see source"
        rows.append(f"| `{key}` | {shown} | `{env_name(env_prefix, key)}` | {descriptions.get(key, 'MISSING')} |")
    return "\n".join(rows)


SERVICES = [
    ("Orchestrator", OrchestratorConfig, ORCH, "ORCH__", "ORCH_CONFIG_FILE"),
    ("Master agent", MasterAgentConfig, MASTER, "MA__", "MA_CONFIG_FILE"),
    ("Token service", TokenServiceConfig, TOKEN, "TS__", "TS_CONFIG_FILE"),
]


def main() -> None:
    print("# Configuration reference\n")
    print("Generated from the configuration dataclasses by `scripts/gen_config_reference.py`; do not edit by hand.\n")
    print("Values are resolved in this order: dataclass default, YAML file, environment override. "
          "Environment values are parsed as JSON when possible (numbers, booleans, lists, objects). "
          "Keys ending in `_env` name an environment variable that holds a secret; the secret itself never appears in YAML.\n")
    for title, cls, desc, prefix, file_var in SERVICES:
        print(f"## {title}\n")
        print(f"YAML file from `{file_var}`. Environment overrides use the prefix `{prefix}`.\n")
        print(table(cls, desc, prefix))
        print()


if __name__ == "__main__":
    main()
