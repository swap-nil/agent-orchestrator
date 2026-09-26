"""Configuration for the orchestrator.

Configuration is layered, lowest precedence first:

1. Defaults defined in the dataclasses below.
2. A YAML file (``ORCH_CONFIG_FILE`` or the path passed to :func:`load_config`).
3. Environment variables of the form ``ORCH__SECTION__KEY=value``. Nested keys use
   double underscores. Values are parsed as JSON when possible, so lists and
   numbers work: ``ORCH__BUDGETS__MAX_STEPS=6``,
   ``ORCH__KILL_SWITCH__DISABLED_AGENTS='["trade-agent"]'``.

Secrets are never stored in the YAML file. Fields ending in ``_env`` name the
environment variable that holds the secret, and are resolved at runtime.

``validate_config`` enforces the production baseline. A ``prod`` profile that
violates it refuses to start.
"""

from __future__ import annotations

import dataclasses
import json
import os
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_PREFIX = "ORCH__"
RISK_CLASSES = ("R0", "R1", "R2", "R3")


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or fails validation."""


# --------------------------------------------------------------------------- sections


@dataclass
class ServiceConfig:
    name: str = "orchestrator"
    environment: str = "dev"  # environment name used for agent certification checks
    cell_id: str = "cell-01"
    region: str = "switzerlandnorth"
    log_level: str = "INFO"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    request_body_limit_bytes: int = 64_000


@dataclass
class JwtConfig:
    issuer: str = ""
    audience: str = ""
    jwks_url: str = ""
    algorithms: list[str] = field(default_factory=lambda: ["RS256"])
    leeway_s: int = 30


@dataclass
class AuthConfig:
    # How callers (master agent, client backend) authenticate to the orchestrator.
    mode: str = "none"  # none | jwt | mesh_xfcc
    jwt: JwtConfig = field(default_factory=JwtConfig)
    # Ordered from weakest to strongest. Used to compare the user's `acr` claim.
    acr_levels: list[str] = field(default_factory=lambda: ["low", "standard", "stepup"])
    approval_min_acr: str = "stepup"
    # Least privilege per route: which caller identities (SPIFFE IDs or token client ids)
    # may call which route group. Groups: turns, sessions, approvals, workflows, admin.
    route_callers: dict[str, list[str]] = field(default_factory=dict)
    # Validation of end-user tokens (session binding and step-up approvals).
    user_jwt: JwtConfig = field(default_factory=JwtConfig)
    user_subject_claim: str = "sub"
    user_acr_claim: str = "acr"
    user_tenant_claim: str = "tid"


@dataclass
class CatalogueConfig:
    intents_file: str = "config/intents.yaml"
    registry_file: str = "config/agents.yaml"


@dataclass
class ModelClassifierConfig:
    enabled: bool = False
    endpoint: str = ""  # OpenAI-compatible chat completions endpoint (in-region)
    model: str = ""
    api_key_env: str = "ORCH_CLASSIFIER_API_KEY"
    timeout_ms: int = 600
    allowed_risk_classes: list[str] = field(default_factory=lambda: ["R0", "R1"])


@dataclass
class RoutingConfig:
    min_confidence: dict[str, float] = field(
        default_factory=lambda: {"R0": 0.5, "R1": 0.6, "R2": 0.8, "R3": 0.9}
    )
    max_clarification_rounds: int = 2
    fallback_intent: str = ""  # e.g. a FAQ intent; empty means clarify then hand over
    model_classifier: ModelClassifierConfig = field(default_factory=ModelClassifierConfig)


@dataclass
class BudgetConfig:
    turn_deadline_ms: int = 2500
    max_steps: int = 8
    max_depth: int = 3
    max_fan_out: int = 4
    max_cost_units_per_turn: int = 20
    max_cost_units_per_session: int = 400
    max_turns_per_session: int = 200


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    reset_after_s: float = 30.0


@dataclass
class ExecutionConfig:
    default_step_timeout_ms: int = 1500
    read_retries: int = 2
    retry_base_delay_ms: int = 100
    retry_max_delay_ms: int = 800
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    default_quorum: str = "all"  # all | majority | any


@dataclass
class A2AConfig:
    gateway_url: str = "http://localhost:8443"
    agent_path_template: str = "/agents/{agent}"
    protocol_version: str = "1.0"
    # JSON-RPC method names, configurable so a spec revision is a config change.
    method_send: str = "SendMessage"
    method_get: str = "GetTask"
    method_cancel: str = "CancelTask"
    verify_tls: bool = True
    ca_bundle: str = ""
    client_cert_file: str = ""
    client_key_file: str = ""
    required_extensions: list[str] = field(default_factory=list)
    user_role_value: str = "ROLE_USER"


@dataclass
class PolicyConfig:
    engine: str = "local"  # opa | local
    opa_url: str = "http://localhost:8181"
    decision_path: str = "orchestrator/decision"
    timeout_ms: int = 250
    cache_ttl_s: int = 30
    cache_risk_classes: list[str] = field(default_factory=lambda: ["R0", "R1"])
    allowed_channels: list[str] = field(default_factory=lambda: ["voice", "chat"])


@dataclass
class IdentityConfig:
    mode: str = "disabled"  # rfc8693 | entra_obo | disabled
    token_endpoint: str = ""
    client_id: str = ""
    # How the orchestrator authenticates to the IdP for the exchange:
    #   workload_identity: federated assertion from AKS Workload Identity (no secret)
    #   secret: client secret from client_secret_env (development only)
    client_auth: str = "workload_identity"
    federated_token_file: str = ""  # defaults to $AZURE_FEDERATED_TOKEN_FILE
    client_secret_env: str = "ORCH_IDP_CLIENT_SECRET"
    sender_constraint: str = "mtls"  # mtls | dpop | none
    refresh_skew_s: int = 30
    timeout_ms: int = 800


@dataclass
class SessionConfig:
    store: str = "memory"  # memory | redis
    redis_url_env: str = "ORCH_REDIS_URL"
    ttl_s: int = 3600
    lock_timeout_ms: int = 5000
    token_encryption_key_env: str = "ORCH_SESSION_KEY"


@dataclass
class AuditConfig:
    sink: str = "memory"  # memory | jsonl | postgres
    jsonl_path: str = "audit.jsonl"
    postgres_dsn_env: str = "ORCH_AUDIT_DSN"
    table: str = "audit_ledger"
    fail_turn_on_audit_error: bool = True


@dataclass
class GuardConfig:
    injection_action: str = "block"  # block | flag
    injection_patterns: list[str] = field(
        default_factory=lambda: [
            r"ignore (all|any|the|your) (previous|prior|above) (instructions|rules)",
            r"disregard (your|the) (rules|instructions|policy)",
            r"you are now (in )?(developer|dan|jailbreak) mode",
            r"reveal (your|the) (system )?prompt",
            r"act as (an? )?(unfiltered|unrestricted)",
        ]
    )
    prohibited_phrases: list[str] = field(
        default_factory=lambda: ["guaranteed return", "risk-free", "cannot lose"]
    )
    pressure_phrases: list[str] = field(
        default_factory=lambda: ["act now", "last chance", "before it's too late", "don't miss out"]
    )
    r2_disclaimer: str = (
        "This is general information based on your data, not a personal recommendation. "
        "An advisor can review it with you."
    )
    max_input_chars: int = 2000
    redact_pii_in_logs: bool = True
    # Artifacts with any other classification are never spoken to the user.
    response_allowed_classifications: list[str] = field(
        default_factory=lambda: ["public", "internal", "client_confidential"]
    )


@dataclass
class AdmissionConfig:
    max_concurrent_turns: int = 200
    # Above this utilisation, optional steps are skipped and R0 answers may be served degraded.
    soft_limit_ratio: float = 0.8
    # Above this utilisation, only R3 continuations and handovers are admitted.
    hard_limit_ratio: float = 1.0


@dataclass
class WorkflowConfig:
    enabled: bool = False
    temporal_target: str = "localhost:7233"
    namespace: str = "default"
    task_queue: str = "orchestrator-transactions"
    approval_timeout_s: int = 600
    approval_signing_key_env: str = "ORCH_APPROVAL_KEY"
    payload_key_env: str = "ORCH_TEMPORAL_PAYLOAD_KEY"  # Fernet key; required in prod
    tls: bool = False


@dataclass
class TelemetryConfig:
    enabled: bool = False
    otlp_endpoint: str = "http://localhost:4317"
    capture_content: bool = False
    semconv_stability_opt_in: str = "gen_ai_latest_experimental"


@dataclass
class KillSwitchConfig:
    disabled_agents: list[str] = field(default_factory=list)
    disabled_intents: list[str] = field(default_factory=list)
    disabled_risk_classes: list[str] = field(default_factory=list)


@dataclass
class AlertRuleConfig:
    id: str = ""
    # Dotted path into the metrics snapshot, e.g. "rates.handover_rate" or "latency.p95_ms".
    # "agents.*.error_rate" is evaluated per agent.
    metric: str = ""
    op: str = ">"  # > | <
    threshold: float = 0.0
    window_s: int = 300
    min_samples: int = 20  # turns (or agent calls for agents.*) in the window before the rule can fire
    severity: str = "warning"  # warning | critical
    description: str = ""


def _default_alert_rules() -> list[AlertRuleConfig]:
    return [
        AlertRuleConfig("latency-p95", "latency.p95_ms", ">", 2200, 300, 20, "warning", "p95 turn latency near the turn deadline"),
        AlertRuleConfig("handover-rate", "rates.handover_rate", ">", 0.15, 300, 20, "warning", "Unusually many handovers to humans"),
        AlertRuleConfig("error-rate", "rates.error_rate", ">", 0.02, 300, 20, "critical", "Internal errors on turns"),
        AlertRuleConfig("busy-rate", "rates.busy_rate", ">", 0.05, 300, 20, "warning", "Load shedding is turning users away"),
        AlertRuleConfig("input-block-rate", "rates.input_block_rate", ">", 0.10, 300, 20, "warning", "Spike in blocked inputs: possible attack"),
        AlertRuleConfig("policy-denial-rate", "rates.policy_denial_rate", ">", 0.20, 300, 20, "warning", "Many policy denials: misconfiguration or probing"),
        AlertRuleConfig("agent-errors", "agents.*.error_rate", ">", 0.20, 300, 10, "critical", "Domain agent failing"),
    ]


@dataclass
class CommandCenterConfig:
    enabled: bool = True
    event_bus: str = "memory"  # memory | redis
    redis_url_env: str = "ORCH_REDIS_URL"
    stream_key: str = "orch:events"
    stream_maxlen: int = 100_000
    buffer_events: int = 5000
    metrics_bucket_s: int = 10
    metrics_retention_s: int = 3600
    # How operators sign in to the console: none (dev only) or jwt (Entra ID app roles).
    operator_auth: str = "none"
    operator_jwt: JwtConfig = field(default_factory=JwtConfig)
    operator_roles_claim: str = "roles"
    # Console role -> IdP role values that grant it.
    role_members: dict[str, list[str]] = field(default_factory=lambda: {
        "viewer": ["CC.Viewer"], "operator": ["CC.Operator"], "investigator": ["CC.Investigator"],
        "approver": ["CC.ChangeApprover"],
    })
    # Show (already PII-redacted) user utterances to the investigator role. Everyone else sees lengths only.
    show_utterances: bool = False
    require_reason_for_actions: bool = True
    alert_rules: list[AlertRuleConfig] = field(default_factory=_default_alert_rules)
    evals_file: str = "config/evals.yaml"
    runtime_changes_enabled: bool = True
    require_four_eyes: bool = True
    change_min_pass_rate: float = 0.95
    change_ttl_s: int = 86_400


@dataclass
class MessagesConfig:
    """User-facing fallback wording. Localise per deployment."""

    refused: str = "I'm sorry, I can't help with that here. I can connect you with an advisor if you like."
    blocked_input: str = "I can't process that request. Could you rephrase what you need?"
    busy: str = "We're very busy right now. Please try again in a moment, or I can arrange a call back."
    handover: str = "Let me connect you with one of our advisors who can help further."
    clarify_default: str = "Could you tell me a bit more about what you'd like to do?"
    failure: str = "I couldn't complete that just now. Please try again shortly."
    partial_suffix: str = "Some information is temporarily unavailable."
    transactions_unavailable: str = "I can't carry out transactions at the moment. An advisor can help you."
    approval_prompt: str = "Please confirm this in your app to continue."
    session_invalid: str = "Your session has ended. Please start a new conversation."


@dataclass
class OrchestratorConfig:
    profile: str = "dev"  # dev | prod
    service: ServiceConfig = field(default_factory=ServiceConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    catalogue: CatalogueConfig = field(default_factory=CatalogueConfig)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    a2a: A2AConfig = field(default_factory=A2AConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    guards: GuardConfig = field(default_factory=GuardConfig)
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    workflows: WorkflowConfig = field(default_factory=WorkflowConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    kill_switch: KillSwitchConfig = field(default_factory=KillSwitchConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    command_center: CommandCenterConfig = field(default_factory=CommandCenterConfig)


# --------------------------------------------------------------------------- loading


def _parse_env_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _apply_env_overrides(data: dict[str, Any], environ: typing.Mapping[str, str], prefix: str = ENV_PREFIX) -> dict[str, Any]:
    for key, raw in environ.items():
        if not key.startswith(prefix):
            continue
        path = [p.lower() for p in key[len(prefix):].split("__") if p]
        if not path:
            continue
        node = data
        for part in path[:-1]:
            existing = node.get(part)
            if not isinstance(existing, dict):
                existing = {}
                node[part] = existing
            node = existing
        node[path[-1]] = _parse_env_value(raw)
    return data


def _coerce(value: Any, target: Any, where: str) -> Any:
    origin = typing.get_origin(target)
    if dataclasses.is_dataclass(target):
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a mapping")
        return _build(target, value, where)
    if origin is list:
        (item_type,) = typing.get_args(target) or (Any,)
        if not isinstance(value, list):
            raise ConfigError(f"{where}: expected a list")
        return [_coerce(v, item_type, f"{where}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        key_type, val_type = typing.get_args(target) or (Any, Any)
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a mapping")
        return {
            _coerce(k, key_type, where): _coerce(v, val_type, f"{where}.{k}") for k, v in value.items()
        }
    if target is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
            return value.lower() in ("true", "1", "yes")
        raise ConfigError(f"{where}: expected a boolean")
    if target is int:
        if isinstance(value, bool):
            raise ConfigError(f"{where}: expected an integer")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: expected an integer") from exc
    if target is float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: expected a number") from exc
    if target is str:
        if isinstance(value, (dict, list)):
            raise ConfigError(f"{where}: expected a string")
        return str(value)
    return value


def _build(cls: type, data: dict[str, Any], where: str = "config") -> Any:
    hints = typing.get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    defaults = {f.name: f for f in dataclasses.fields(cls)}
    for name, value in data.items():
        if typing.get_origin(hints[name]) is dict and isinstance(value, dict):
            # Merge partial overrides onto the default mapping so ORCH__X__KEY=... changes one key only.
            fld = defaults[name]
            base = fld.default_factory() if fld.default_factory is not dataclasses.MISSING else {}  # type: ignore[misc]
            merged = dict(base)
            lookup = {str(k).lower(): k for k in merged}
            for k, v in value.items():
                merged[lookup.get(str(k).lower(), k)] = v
            value = merged
        kwargs[name] = _coerce(value, hints[name], f"{where}.{name}")
    return cls(**kwargs)


def load_dataclass_config(
    cls: type, path: str | os.PathLike[str] | None, environ: typing.Mapping[str, str], prefix: str
) -> Any:
    """Generic YAML + environment loader used by the other services in this repo."""
    data: dict[str, Any] = {}
    if path:
        file = Path(path)
        if not file.is_file():
            raise ConfigError(f"config file not found: {file}")
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return _build(cls, _apply_env_overrides(data, environ, prefix))


def load_config(
    path: str | os.PathLike[str] | None = None,
    environ: typing.Mapping[str, str] | None = None,
    validate: bool = True,
) -> OrchestratorConfig:
    """Load configuration from YAML and environment, then validate it."""
    environ = os.environ if environ is None else environ
    path = path or environ.get("ORCH_CONFIG_FILE")
    data: dict[str, Any] = {}
    if path:
        file = Path(path)
        if not file.is_file():
            raise ConfigError(f"config file not found: {file}")
        loaded = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError("config file must contain a mapping at the top level")
        data = loaded
    data = _apply_env_overrides(data, environ)
    config = _build(OrchestratorConfig, data)
    if validate:
        errors = validate_config(config)
        if errors:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))
    return config


# --------------------------------------------------------------------------- validation


def _check_enum(errors: list[str], name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        errors.append(f"{name} must be one of {list(allowed)}, got {value!r}")


def validate_config(config: OrchestratorConfig) -> list[str]:
    """Return a list of human-readable problems. Empty means valid."""
    errors: list[str] = []
    _check_enum(errors, "profile", config.profile, ("dev", "prod"))
    _check_enum(errors, "auth.mode", config.auth.mode, ("none", "jwt", "mesh_xfcc"))
    _check_enum(errors, "policy.engine", config.policy.engine, ("opa", "local"))
    _check_enum(errors, "identity.mode", config.identity.mode, ("rfc8693", "entra_obo", "disabled"))
    _check_enum(errors, "identity.client_auth", config.identity.client_auth, ("workload_identity", "secret"))
    _check_enum(errors, "identity.sender_constraint", config.identity.sender_constraint, ("mtls", "dpop", "none"))
    _check_enum(errors, "session.store", config.session.store, ("memory", "redis"))
    _check_enum(errors, "audit.sink", config.audit.sink, ("memory", "jsonl", "postgres"))
    _check_enum(errors, "guards.injection_action", config.guards.injection_action, ("block", "flag"))
    _check_enum(errors, "execution.default_quorum", config.execution.default_quorum, ("all", "majority", "any"))

    if config.auth.approval_min_acr not in config.auth.acr_levels:
        errors.append("auth.approval_min_acr must be one of auth.acr_levels")
    for rc in RISK_CLASSES:
        threshold = config.routing.min_confidence.get(rc)
        if threshold is None or not 0.0 <= threshold <= 1.0:
            errors.append(f"routing.min_confidence.{rc} must be between 0 and 1")
    bad_model_rc = set(config.routing.model_classifier.allowed_risk_classes) - {"R0", "R1"}
    if bad_model_rc:
        errors.append(
            "routing.model_classifier.allowed_risk_classes may only contain R0 and R1; "
            "advice and transactions must be routed deterministically"
        )
    b = config.budgets
    for name in ("turn_deadline_ms", "max_steps", "max_depth", "max_fan_out", "max_cost_units_per_turn"):
        if getattr(b, name) <= 0:
            errors.append(f"budgets.{name} must be positive")
    if b.max_depth > 5:
        errors.append("budgets.max_depth must not exceed 5")
    if config.execution.default_step_timeout_ms > b.turn_deadline_ms:
        errors.append("execution.default_step_timeout_ms must not exceed budgets.turn_deadline_ms")
    a = config.admission
    if not 0 < a.soft_limit_ratio <= a.hard_limit_ratio:
        errors.append("admission ratios must satisfy 0 < soft_limit_ratio <= hard_limit_ratio")
    if a.max_concurrent_turns <= 0:
        errors.append("admission.max_concurrent_turns must be positive")
    if config.session.lock_timeout_ms <= config.budgets.turn_deadline_ms + 500:
        errors.append("session.lock_timeout_ms must exceed budgets.turn_deadline_ms by at least 500 ms")
    cc = config.command_center
    _check_enum(errors, "command_center.event_bus", cc.event_bus, ("memory", "redis"))
    _check_enum(errors, "command_center.operator_auth", cc.operator_auth, ("none", "jwt"))
    if not 0 < cc.change_min_pass_rate <= 1:
        errors.append("command_center.change_min_pass_rate must be in (0, 1]")
    if cc.metrics_bucket_s <= 0 or cc.metrics_retention_s < cc.metrics_bucket_s:
        errors.append("command_center metrics bucket and retention must be positive, retention >= bucket")
    for rule in cc.alert_rules:
        if not rule.id or not rule.metric or rule.op not in (">", "<") or rule.severity not in ("warning", "critical"):
            errors.append(f"command_center.alert_rules: rule {rule.id or '?'} needs id, metric, op > or <, severity warning or critical")
    if config.policy.timeout_ms <= 0:
        errors.append("policy.timeout_ms must be positive")

    if config.profile == "prod":
        errors.extend(_production_rules(config))
    return errors


def _production_rules(config: OrchestratorConfig) -> list[str]:
    errors: list[str] = []
    if config.auth.mode == "none":
        errors.append("prod: auth.mode must not be 'none'")
    if config.auth.mode == "jwt" and not (config.auth.jwt.issuer and config.auth.jwt.audience and config.auth.jwt.jwks_url):
        errors.append("prod: auth.jwt.issuer, audience and jwks_url are required")
    if any(alg.upper().startswith("HS") or alg.lower() == "none" for alg in config.auth.jwt.algorithms + config.auth.user_jwt.algorithms):
        errors.append("prod: JWT algorithms must be asymmetric")
    if not (config.auth.user_jwt.issuer and config.auth.user_jwt.audience and config.auth.user_jwt.jwks_url):
        errors.append("prod: auth.user_jwt.issuer, audience and jwks_url are required to validate user tokens")
    for group in ("turns", "sessions", "approvals", "workflows", "admin"):
        if not config.auth.route_callers.get(group):
            errors.append(f"prod: auth.route_callers.{group} must list the permitted callers")
    if config.policy.engine != "opa":
        errors.append("prod: policy.engine must be 'opa'")
    if config.identity.mode == "disabled":
        errors.append("prod: identity.mode must not be 'disabled' (on-behalf-of tokens are required)")
    if config.identity.client_auth == "secret":
        errors.append("prod: identity.client_auth must be 'workload_identity' (no client secrets in production)")
    if config.identity.sender_constraint == "none":
        errors.append("prod: identity.sender_constraint must be 'mtls' or 'dpop'")
    if config.session.store != "redis":
        errors.append("prod: session.store must be 'redis'")
    if config.audit.sink != "postgres":
        errors.append("prod: audit.sink must be 'postgres'")
    if not config.audit.fail_turn_on_audit_error:
        errors.append("prod: audit.fail_turn_on_audit_error must be true")
    if not config.a2a.gateway_url.startswith("https://"):
        errors.append("prod: a2a.gateway_url must use https")
    if not config.a2a.verify_tls:
        errors.append("prod: a2a.verify_tls must be true")
    if not config.policy.opa_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        errors.append("prod: policy.opa_url must be https or a localhost sidecar")
    if config.telemetry.capture_content:
        errors.append("prod: telemetry.capture_content must be false")
    if config.guards.injection_action != "block":
        errors.append("prod: guards.injection_action must be 'block'")
    if not config.workflows.enabled:
        errors.append("prod: workflows.enabled must be true (transactions require durable execution)")
    cc = config.command_center
    if cc.enabled:
        if cc.operator_auth != "jwt":
            errors.append("prod: command_center.operator_auth must be 'jwt' (operators sign in with the IdP)")
        elif not (cc.operator_jwt.issuer and cc.operator_jwt.audience and cc.operator_jwt.jwks_url):
            errors.append("prod: command_center.operator_jwt issuer, audience and jwks_url are required")
        if not cc.require_four_eyes:
            errors.append("prod: command_center.require_four_eyes must be true")
        if not cc.require_reason_for_actions:
            errors.append("prod: command_center.require_reason_for_actions must be true")
        if cc.event_bus != "redis":
            errors.append("prod: command_center.event_bus must be 'redis' so the console sees every replica")
    return errors


def resolve_secret(env_name: str, environ: typing.Mapping[str, str] | None = None, required: bool = True) -> str:
    """Read a secret from the environment variable named in configuration."""
    environ = os.environ if environ is None else environ
    value = environ.get(env_name, "")
    if required and not value:
        raise ConfigError(f"required secret environment variable {env_name!r} is not set")
    return value
