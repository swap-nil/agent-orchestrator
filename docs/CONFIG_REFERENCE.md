# Configuration reference

Generated from the configuration dataclasses by `scripts/gen_config_reference.py`; do not edit by hand.

Values are resolved in this order: dataclass default, YAML file, environment override. Environment values are parsed as JSON when possible (numbers, booleans, lists, objects). Keys ending in `_env` name an environment variable that holds a secret; the secret itself never appears in YAML.

## Orchestrator

YAML file from `ORCH_CONFIG_FILE`. Environment overrides use the prefix `ORCH__`.

| Key | Default | Environment override | Description |
|---|---|---|---|
| `profile` | `dev` | `ORCH__PROFILE` | `dev` or `prod`. `prod` enforces the production baseline and refuses to start if any rule is violated. |
| `service.name` | `orchestrator` | `ORCH__SERVICE__NAME` | Service name used in telemetry and as the audit actor. |
| `service.environment` | `dev` | `ORCH__SERVICE__ENVIRONMENT` | Environment name checked against each agent's `certified_in` list (e.g. dev, test, prod). |
| `service.cell_id` | `cell-01` | `ORCH__SERVICE__CELL_ID` | Cell identifier, recorded in policy input and telemetry. |
| `service.region` | `switzerlandnorth` | `ORCH__SERVICE__REGION` | Azure region, recorded in telemetry resource attributes. |
| `service.log_level` | `INFO` | `ORCH__SERVICE__LOG_LEVEL` | Python log level. |
| `server.host` | `0.0.0.0` | `ORCH__SERVER__HOST` | Bind address for the API server. |
| `server.port` | `8080` | `ORCH__SERVER__PORT` | Port for the API server. |
| `server.request_body_limit_bytes` | `64000` | `ORCH__SERVER__REQUEST_BODY_LIMIT_BYTES` | Requests with a larger Content-Length are rejected with 413. Also enforce at ingress. |
| `auth.mode` | `none` | `ORCH__AUTH__MODE` | How callers authenticate: `none` (dev only), `jwt` (service tokens) or `mesh_xfcc` (SPIFFE ID from Envoy XFCC). |
| `auth.jwt.issuer` | `""` | `ORCH__AUTH__JWT__ISSUER` | Expected issuer of service tokens (auth.mode=jwt). |
| `auth.jwt.audience` | `""` | `ORCH__AUTH__JWT__AUDIENCE` | Expected audience of service tokens. |
| `auth.jwt.jwks_url` | `""` | `ORCH__AUTH__JWT__JWKS_URL` | JWKS URL for service token signature keys. |
| `auth.jwt.algorithms` | `["RS256"]` | `ORCH__AUTH__JWT__ALGORITHMS` | Accepted signature algorithms. Asymmetric only in prod. |
| `auth.jwt.leeway_s` | `30` | `ORCH__AUTH__JWT__LEEWAY_S` | Allowed clock skew in seconds. |
| `auth.acr_levels` | `["low", "standard", "stepup"]` | `ORCH__AUTH__ACR_LEVELS` | Authentication levels, weakest first. Intents and approvals refer to these names. |
| `auth.approval_min_acr` | `stepup` | `ORCH__AUTH__APPROVAL_MIN_ACR` | Minimum level required to approve a transaction (step-up). |
| `auth.route_callers` | `{}` | `ORCH__AUTH__ROUTE_CALLERS` | Map of route group (turns, sessions, approvals, workflows, admin) to permitted caller identities. |
| `auth.user_jwt.issuer` | `""` | `ORCH__AUTH__USER_JWT__ISSUER` | Expected issuer of end-user tokens. |
| `auth.user_jwt.audience` | `""` | `ORCH__AUTH__USER_JWT__AUDIENCE` | Expected audience of end-user tokens. |
| `auth.user_jwt.jwks_url` | `""` | `ORCH__AUTH__USER_JWT__JWKS_URL` | JWKS URL for end-user token keys. |
| `auth.user_jwt.algorithms` | `["RS256"]` | `ORCH__AUTH__USER_JWT__ALGORITHMS` | Accepted algorithms for end-user tokens. |
| `auth.user_jwt.leeway_s` | `30` | `ORCH__AUTH__USER_JWT__LEEWAY_S` | Allowed clock skew for end-user tokens. |
| `auth.user_subject_claim` | `sub` | `ORCH__AUTH__USER_SUBJECT_CLAIM` | Claim holding the user id (Entra: `oid`). |
| `auth.user_acr_claim` | `acr` | `ORCH__AUTH__USER_ACR_CLAIM` | Claim holding the authentication level (string or list; Entra: `acrs`). |
| `auth.user_tenant_claim` | `tid` | `ORCH__AUTH__USER_TENANT_CLAIM` | Claim holding the tenant. |
| `catalogue.intents_file` | `config/intents.yaml` | `ORCH__CATALOGUE__INTENTS_FILE` | Path to the intent catalogue YAML. |
| `catalogue.registry_file` | `config/agents.yaml` | `ORCH__CATALOGUE__REGISTRY_FILE` | Path to the agent registry YAML. |
| `routing.min_confidence` | `{"R0": 0.5, "R1": 0.6, "R2": 0.8, "R3": 0.9}` | `ORCH__ROUTING__MIN_CONFIDENCE` | Minimum routing confidence per risk class (R0..R3); below it the user is asked to clarify. |
| `routing.max_clarification_rounds` | `2` | `ORCH__ROUTING__MAX_CLARIFICATION_ROUNDS` | Clarifications before handing over to a human. |
| `routing.fallback_intent` | `""` | `ORCH__ROUTING__FALLBACK_INTENT` | R0 intent used when nothing matches (e.g. FAQ). Empty: clarify, then hand over. |
| `routing.model_classifier.enabled` | `false` | `ORCH__ROUTING__MODEL_CLASSIFIER__ENABLED` | Enable the model classifier for unmatched requests (R0/R1 intents only). |
| `routing.model_classifier.endpoint` | `""` | `ORCH__ROUTING__MODEL_CLASSIFIER__ENDPOINT` | OpenAI-compatible chat completions URL (in-region). |
| `routing.model_classifier.model` | `""` | `ORCH__ROUTING__MODEL_CLASSIFIER__MODEL` | Model or deployment name. |
| `routing.model_classifier.api_key_env` | `ORCH_CLASSIFIER_API_KEY` | `ORCH__ROUTING__MODEL_CLASSIFIER__API_KEY_ENV` | Environment variable holding the classifier API key. |
| `routing.model_classifier.timeout_ms` | `600` | `ORCH__ROUTING__MODEL_CLASSIFIER__TIMEOUT_MS` | Classifier timeout; on timeout routing falls back. |
| `routing.model_classifier.allowed_risk_classes` | `["R0", "R1"]` | `ORCH__ROUTING__MODEL_CLASSIFIER__ALLOWED_RISK_CLASSES` | Risk classes the model may choose. Validation allows only R0 and R1. |
| `budgets.turn_deadline_ms` | `2500` | `ORCH__BUDGETS__TURN_DEADLINE_MS` | Total time budget for one voice turn, all steps included. |
| `budgets.max_steps` | `8` | `ORCH__BUDGETS__MAX_STEPS` | Maximum steps in a plan. |
| `budgets.max_depth` | `3` | `ORCH__BUDGETS__MAX_DEPTH` | Maximum dependency depth (layers) of a plan. Hard limit 5. |
| `budgets.max_fan_out` | `4` | `ORCH__BUDGETS__MAX_FAN_OUT` | Maximum parallel steps in one layer. |
| `budgets.max_cost_units_per_turn` | `20` | `ORCH__BUDGETS__MAX_COST_UNITS_PER_TURN` | Maximum plan cost (sum of step cost units) per turn. |
| `budgets.max_cost_units_per_session` | `400` | `ORCH__BUDGETS__MAX_COST_UNITS_PER_SESSION` | Maximum cost units per session. |
| `budgets.max_turns_per_session` | `200` | `ORCH__BUDGETS__MAX_TURNS_PER_SESSION` | Turns after which the session is handed over. |
| `execution.default_step_timeout_ms` | `1500` | `ORCH__EXECUTION__DEFAULT_STEP_TIMEOUT_MS` | Per-call timeout when a step sets none. Must not exceed the turn deadline. |
| `execution.read_retries` | `2` | `ORCH__EXECUTION__READ_RETRIES` | Retries for read steps on retryable errors. Writes are never retried by the executor. |
| `execution.retry_base_delay_ms` | `100` | `ORCH__EXECUTION__RETRY_BASE_DELAY_MS` | Base delay for full-jitter exponential backoff. |
| `execution.retry_max_delay_ms` | `800` | `ORCH__EXECUTION__RETRY_MAX_DELAY_MS` | Maximum backoff delay. |
| `execution.circuit_breaker.failure_threshold` | `5` | `ORCH__EXECUTION__CIRCUIT_BREAKER__FAILURE_THRESHOLD` | Consecutive failures that open an agent's breaker. |
| `execution.circuit_breaker.reset_after_s` | `30.0` | `ORCH__EXECUTION__CIRCUIT_BREAKER__RESET_AFTER_S` | Seconds before one probe call is allowed. |
| `execution.default_quorum` | `all` | `ORCH__EXECUTION__DEFAULT_QUORUM` | Success rule for a plan: `all`, `majority` or `any` required step(s). Intents may override. |
| `a2a.gateway_url` | `http://localhost:8443` | `ORCH__A2A__GATEWAY_URL` | Base URL of the A2A mesh gateway. https in prod. |
| `a2a.agent_path_template` | `/agents/{agent}` | `ORCH__A2A__AGENT_PATH_TEMPLATE` | Path per agent on the gateway; `{agent}` is replaced by the agent name. |
| `a2a.protocol_version` | `1.0` | `ORCH__A2A__PROTOCOL_VERSION` | Sent as the `A2A-Version` header. |
| `a2a.method_send` | `SendMessage` | `ORCH__A2A__METHOD_SEND` | JSON-RPC method for sending a message. |
| `a2a.method_get` | `GetTask` | `ORCH__A2A__METHOD_GET` | JSON-RPC method for reading a task. |
| `a2a.method_cancel` | `CancelTask` | `ORCH__A2A__METHOD_CANCEL` | JSON-RPC method for cancelling a task. |
| `a2a.verify_tls` | `true` | `ORCH__A2A__VERIFY_TLS` | Verify the gateway certificate. Must be true in prod. |
| `a2a.ca_bundle` | `""` | `ORCH__A2A__CA_BUNDLE` | Mesh CA bundle path (used only for the gateway, not for public endpoints). |
| `a2a.client_cert_file` | `""` | `ORCH__A2A__CLIENT_CERT_FILE` | Workload certificate (SPIFFE SVID) for mTLS to the gateway. |
| `a2a.client_key_file` | `""` | `ORCH__A2A__CLIENT_KEY_FILE` | Private key for the workload certificate. |
| `a2a.required_extensions` | `[]` | `ORCH__A2A__REQUIRED_EXTENSIONS` | Values sent in the `A2A-Extensions` header. |
| `a2a.user_role_value` | `ROLE_USER` | `ORCH__A2A__USER_ROLE_VALUE` | Role value used in A2A messages. |
| `policy.engine` | `local` | `ORCH__POLICY__ENGINE` | `opa` (prod) or `local` (Python mirror of the Rego policy, dev/test). |
| `policy.opa_url` | `http://localhost:8181` | `ORCH__POLICY__OPA_URL` | OPA base URL; normally the localhost sidecar. |
| `policy.decision_path` | `orchestrator/decision` | `ORCH__POLICY__DECISION_PATH` | OPA data path of the decision document. |
| `policy.timeout_ms` | `250` | `ORCH__POLICY__TIMEOUT_MS` | Policy call timeout. Timeouts are denials (fail closed). |
| `policy.cache_ttl_s` | `30` | `ORCH__POLICY__CACHE_TTL_S` | Cache lifetime for allow decisions on R0/R1 reads. 0 disables caching. |
| `policy.cache_risk_classes` | `["R0", "R1"]` | `ORCH__POLICY__CACHE_RISK_CLASSES` | Risk classes whose read allows may be cached. |
| `policy.allowed_channels` | `["voice", "chat"]` | `ORCH__POLICY__ALLOWED_CHANNELS` | Channels on which intents may run. |
| `identity.mode` | `disabled` | `ORCH__IDENTITY__MODE` | `entra_obo`, `rfc8693` or `disabled` (dev). Exchanges the user token per agent call. |
| `identity.token_endpoint` | `""` | `ORCH__IDENTITY__TOKEN_ENDPOINT` | IdP token endpoint. |
| `identity.client_id` | `""` | `ORCH__IDENTITY__CLIENT_ID` | Orchestrator's client id at the IdP. |
| `identity.client_auth` | `workload_identity` | `ORCH__IDENTITY__CLIENT_AUTH` | `workload_identity` (federated assertion, prod) or `secret` (dev). |
| `identity.federated_token_file` | `""` | `ORCH__IDENTITY__FEDERATED_TOKEN_FILE` | Path to the projected federated token; default `$AZURE_FEDERATED_TOKEN_FILE`. |
| `identity.client_secret_env` | `ORCH_IDP_CLIENT_SECRET` | `ORCH__IDENTITY__CLIENT_SECRET_ENV` | Environment variable with the client secret (client_auth=secret only). |
| `identity.sender_constraint` | `mtls` | `ORCH__IDENTITY__SENDER_CONSTRAINT` | Declared token binding at the resource: `mtls` or `dpop` (prod), `none` (dev). |
| `identity.refresh_skew_s` | `30` | `ORCH__IDENTITY__REFRESH_SKEW_S` | Refresh delegated tokens this many seconds before expiry. |
| `identity.timeout_ms` | `800` | `ORCH__IDENTITY__TIMEOUT_MS` | Token endpoint timeout. |
| `session.store` | `memory` | `ORCH__SESSION__STORE` | `redis` (prod) or `memory` (dev). |
| `session.redis_url_env` | `ORCH_REDIS_URL` | `ORCH__SESSION__REDIS_URL_ENV` | Environment variable with the Redis URL (use rediss:// with TLS). |
| `session.ttl_s` | `3600` | `ORCH__SESSION__TTL_S` | Session lifetime in the store (sliding). |
| `session.lock_timeout_ms` | `5000` | `ORCH__SESSION__LOCK_TIMEOUT_MS` | Per-session lock TTL. Must exceed the turn deadline by 500 ms or more. |
| `session.token_encryption_key_env` | `ORCH_SESSION_KEY` | `ORCH__SESSION__TOKEN_ENCRYPTION_KEY_ENV` | Environment variable with the Fernet key encrypting user tokens at rest. Required in prod. |
| `audit.sink` | `memory` | `ORCH__AUDIT__SINK` | `postgres` (prod), `jsonl` (dev) or `memory` (tests). |
| `audit.jsonl_path` | `audit.jsonl` | `ORCH__AUDIT__JSONL_PATH` | File for the jsonl sink. |
| `audit.postgres_dsn_env` | `ORCH_AUDIT_DSN` | `ORCH__AUDIT__POSTGRES_DSN_ENV` | Environment variable with the PostgreSQL DSN. |
| `audit.table` | `audit_ledger` | `ORCH__AUDIT__TABLE` | Audit table name. |
| `audit.fail_turn_on_audit_error` | `true` | `ORCH__AUDIT__FAIL_TURN_ON_AUDIT_ERROR` | If true, a turn fails safely when its audit record cannot be written. Must be true in prod. |
| `guards.injection_action` | `block` | `ORCH__GUARDS__INJECTION_ACTION` | `block` (prod) or `flag` suspected prompt injection. |
| `guards.injection_patterns` | see source | `ORCH__GUARDS__INJECTION_PATTERNS` | Case-insensitive regular expressions for prompt-injection heuristics. |
| `guards.prohibited_phrases` | `["guaranteed return", "risk-free", "cannot lose"]` | `ORCH__GUARDS__PROHIBITED_PHRASES` | Phrases that must never be spoken; the answer is withheld. |
| `guards.pressure_phrases` | see source | `ORCH__GUARDS__PRESSURE_PHRASES` | Pressure-selling phrases blocked on advice and transactions. |
| `guards.r2_disclaimer` | `This is general information based on your data, not a personal recommendation. An advisor can review it with you.` | `ORCH__GUARDS__R2_DISCLAIMER` | Appended to every advice (R2) answer. |
| `guards.max_input_chars` | `2000` | `ORCH__GUARDS__MAX_INPUT_CHARS` | Longer user inputs are refused. |
| `guards.redact_pii_in_logs` | `true` | `ORCH__GUARDS__REDACT_PII_IN_LOGS` | Redact IBANs, card numbers, emails and phone numbers before audit/logging. |
| `guards.response_allowed_classifications` | `["public", "internal", "client_confidential"]` | `ORCH__GUARDS__RESPONSE_ALLOWED_CLASSIFICATIONS` | Artifact classifications that may be spoken to the user. |
| `admission.max_concurrent_turns` | `200` | `ORCH__ADMISSION__MAX_CONCURRENT_TURNS` | Concurrent turns per replica. |
| `admission.soft_limit_ratio` | `0.8` | `ORCH__ADMISSION__SOFT_LIMIT_RATIO` | Utilisation above which optional steps are skipped (degraded mode). |
| `admission.hard_limit_ratio` | `1.0` | `ORCH__ADMISSION__HARD_LIMIT_RATIO` | Utilisation above which only transactions are admitted; others get the busy message. |
| `workflows.enabled` | `false` | `ORCH__WORKFLOWS__ENABLED` | Enable durable transactions (Temporal). Required in prod; R3 intents are refused when off. |
| `workflows.temporal_target` | `localhost:7233` | `ORCH__WORKFLOWS__TEMPORAL_TARGET` | Temporal frontend host:port. |
| `workflows.namespace` | `default` | `ORCH__WORKFLOWS__NAMESPACE` | Temporal namespace. |
| `workflows.task_queue` | `orchestrator-transactions` | `ORCH__WORKFLOWS__TASK_QUEUE` | Task queue served by the worker. |
| `workflows.approval_timeout_s` | `600` | `ORCH__WORKFLOWS__APPROVAL_TIMEOUT_S` | Time the user has to approve; then the workflow expires and nothing is done. |
| `workflows.approval_signing_key_env` | `ORCH_APPROVAL_KEY` | `ORCH__WORKFLOWS__APPROVAL_SIGNING_KEY_ENV` | Environment variable with the approval signing secret (32+ bytes, Ed25519 seed source). |
| `workflows.payload_key_env` | `ORCH_TEMPORAL_PAYLOAD_KEY` | `ORCH__WORKFLOWS__PAYLOAD_KEY_ENV` | Environment variable with the Fernet key encrypting Temporal payloads. Required in prod. |
| `workflows.tls` | `false` | `ORCH__WORKFLOWS__TLS` | Use TLS to Temporal. |
| `telemetry.enabled` | `false` | `ORCH__TELEMETRY__ENABLED` | Export traces and metrics over OTLP. |
| `telemetry.otlp_endpoint` | `http://localhost:4317` | `ORCH__TELEMETRY__OTLP_ENDPOINT` | OTLP gRPC endpoint (usually the collector). |
| `telemetry.capture_content` | `false` | `ORCH__TELEMETRY__CAPTURE_CONTENT` | Capture prompts/answers in spans. Must be false in prod. |
| `telemetry.semconv_stability_opt_in` | `gen_ai_latest_experimental` | `ORCH__TELEMETRY__SEMCONV_STABILITY_OPT_IN` | Pinned value for OTEL_SEMCONV_STABILITY_OPT_IN (GenAI conventions). |
| `kill_switch.disabled_agents` | `[]` | `ORCH__KILL_SWITCH__DISABLED_AGENTS` | Agents switched off by configuration (merged with runtime switches). |
| `kill_switch.disabled_intents` | `[]` | `ORCH__KILL_SWITCH__DISABLED_INTENTS` | Intents switched off by configuration. |
| `kill_switch.disabled_risk_classes` | `[]` | `ORCH__KILL_SWITCH__DISABLED_RISK_CLASSES` | Risk classes switched off (e.g. R3 during an incident). |
| `messages.refused` | `I'm sorry, I can't help with that here. I can connect you with an advisor if you like.` | `ORCH__MESSAGES__REFUSED` | Spoken when a request is refused (policy, plan, authentication). |
| `messages.blocked_input` | `I can't process that request. Could you rephrase what you need?` | `ORCH__MESSAGES__BLOCKED_INPUT` | Spoken when the input guard blocks a request. |
| `messages.busy` | `We're very busy right now. Please try again in a moment, or I can arrange a call back.` | `ORCH__MESSAGES__BUSY` | Spoken under load shedding. |
| `messages.handover` | `Let me connect you with one of our advisors who can help further.` | `ORCH__MESSAGES__HANDOVER` | Spoken when handing over to a human advisor. |
| `messages.clarify_default` | `Could you tell me a bit more about what you'd like to do?` | `ORCH__MESSAGES__CLARIFY_DEFAULT` | Clarifying question when the intent has none. |
| `messages.failure` | `I couldn't complete that just now. Please try again shortly.` | `ORCH__MESSAGES__FAILURE` | Spoken when agents fail on information requests. |
| `messages.partial_suffix` | `Some information is temporarily unavailable.` | `ORCH__MESSAGES__PARTIAL_SUFFIX` | Appended when part of an answer is missing. |
| `messages.transactions_unavailable` | `I can't carry out transactions at the moment. An advisor can help you.` | `ORCH__MESSAGES__TRANSACTIONS_UNAVAILABLE` | Spoken when transactions are disabled or unavailable. |
| `messages.approval_prompt` | `Please confirm this in your app to continue.` | `ORCH__MESSAGES__APPROVAL_PROMPT` | Appended to the read-back of a transaction. |
| `messages.session_invalid` | `Your session has ended. Please start a new conversation.` | `ORCH__MESSAGES__SESSION_INVALID` | Spoken for unknown or closed sessions. |
| `command_center.enabled` | `true` | `ORCH__COMMAND_CENTER__ENABLED` | Enable the command center: decision event bus, metrics, alerts, evals, runtime change control and the console API. |
| `command_center.event_bus` | `memory` | `ORCH__COMMAND_CENTER__EVENT_BUS` | `redis` (prod: one cell-wide stream across replicas) or `memory` (single replica, dev). |
| `command_center.redis_url_env` | `ORCH_REDIS_URL` | `ORCH__COMMAND_CENTER__REDIS_URL_ENV` | Environment variable with the Redis URL for the event stream. |
| `command_center.stream_key` | `orch:events` | `ORCH__COMMAND_CENTER__STREAM_KEY` | Redis stream key for decision events (one per cell). |
| `command_center.stream_maxlen` | `100000` | `ORCH__COMMAND_CENTER__STREAM_MAXLEN` | Approximate maximum stream length; the audit ledger remains the durable record. |
| `command_center.buffer_events` | `5000` | `ORCH__COMMAND_CENTER__BUFFER_EVENTS` | Events kept in memory for the live feed and stream resume. |
| `command_center.metrics_bucket_s` | `10` | `ORCH__COMMAND_CENTER__METRICS_BUCKET_S` | Width of one metrics bucket in seconds. |
| `command_center.metrics_retention_s` | `3600` | `ORCH__COMMAND_CENTER__METRICS_RETENTION_S` | How far back metrics are kept in memory. Long-term metrics belong in your OTel backend. |
| `command_center.operator_auth` | `none` | `ORCH__COMMAND_CENTER__OPERATOR_AUTH` | How operators sign in: `jwt` (Entra ID app roles, prod) or `none` (dev: X-Operator header). |
| `command_center.operator_jwt.issuer` | `""` | `ORCH__COMMAND_CENTER__OPERATOR_JWT__ISSUER` | Expected issuer of operator tokens. |
| `command_center.operator_jwt.audience` | `""` | `ORCH__COMMAND_CENTER__OPERATOR_JWT__AUDIENCE` | Expected audience of operator tokens (the console's app registration). |
| `command_center.operator_jwt.jwks_url` | `""` | `ORCH__COMMAND_CENTER__OPERATOR_JWT__JWKS_URL` | JWKS URL for operator token keys. |
| `command_center.operator_jwt.algorithms` | `["RS256"]` | `ORCH__COMMAND_CENTER__OPERATOR_JWT__ALGORITHMS` | Accepted algorithms for operator tokens. |
| `command_center.operator_jwt.leeway_s` | `30` | `ORCH__COMMAND_CENTER__OPERATOR_JWT__LEEWAY_S` | Allowed clock skew for operator tokens. |
| `command_center.operator_roles_claim` | `roles` | `ORCH__COMMAND_CENTER__OPERATOR_ROLES_CLAIM` | Claim listing the operator's app roles. |
| `command_center.role_members` | see source | `ORCH__COMMAND_CENTER__ROLE_MEMBERS` | Console role (viewer, operator, investigator, approver) to the IdP role values that grant it. |
| `command_center.show_utterances` | `false` | `ORCH__COMMAND_CENTER__SHOW_UTTERANCES` | Show PII-redacted user utterances to investigators. Everyone else always sees lengths only. |
| `command_center.require_reason_for_actions` | `true` | `ORCH__COMMAND_CENTER__REQUIRE_REASON_FOR_ACTIONS` | Operator actions (kill switch, terminate, rollback) require a written reason. Must be true in prod. |
| `command_center.alert_rules` | see source | `ORCH__COMMAND_CENTER__ALERT_RULES` | Alert rules: id, metric path, op, threshold, window, minimum samples, severity. See the user guide. |
| `command_center.evals_file` | `config/evals.yaml` | `ORCH__COMMAND_CENTER__EVALS_FILE` | Golden eval dataset used for eval runs and to gate runtime changes. |
| `command_center.runtime_changes_enabled` | `true` | `ORCH__COMMAND_CENTER__RUNTIME_CHANGES_ENABLED` | Allow governed runtime changes to behavioural configuration from the console. |
| `command_center.require_four_eyes` | `true` | `ORCH__COMMAND_CENTER__REQUIRE_FOUR_EYES` | A runtime change must be approved by someone other than its proposer. Must be true in prod. |
| `command_center.change_min_pass_rate` | `0.95` | `ORCH__COMMAND_CENTER__CHANGE_MIN_PASS_RATE` | Minimum eval pass rate a candidate change must reach (in addition to no regressions). |
| `command_center.change_ttl_s` | `86400` | `ORCH__COMMAND_CENTER__CHANGE_TTL_S` | Proposed changes expire after this long if not approved. |

## Master agent

YAML file from `MA_CONFIG_FILE`. Environment overrides use the prefix `MA__`.

| Key | Default | Environment override | Description |
|---|---|---|---|
| `agent_name` | `master-agent` | `MA__AGENT_NAME` | LiveKit agent name for explicit dispatch; must match the token service. |
| `dispatch_key_env` | `MA_DISPATCH_KEY` | `MA__DISPATCH_KEY_ENV` | Environment variable with the HMAC key for dispatch metadata (shared with the token service). |
| `dispatch_max_age_s` | `960` | `MA__DISPATCH_MAX_AGE_S` | Maximum age of dispatch metadata. Must be at least the token service's LiveKit token TTL. |
| `stt.provider` | `deepgram` | `MA__STT__PROVIDER` | Speech-to-text provider key (see master_agent/providers.py). |
| `stt.options` | `{}` | `MA__STT__OPTIONS` | Keyword arguments passed to the STT plugin (model, language, base_url...). |
| `tts.provider` | `deepgram` | `MA__TTS__PROVIDER` | Text-to-speech provider key. |
| `tts.options` | `{}` | `MA__TTS__OPTIONS` | Keyword arguments passed to the TTS plugin (voice, model, base_url...). |
| `vad.provider` | `deepgram` | `MA__VAD__PROVIDER` | Voice activity detection provider key. |
| `vad.options` | `{}` | `MA__VAD__OPTIONS` | Keyword arguments for the VAD plugin. |
| `turn_detection` | `multilingual` | `MA__TURN_DETECTION` | `multilingual` (turn-detector model) or `vad`. |
| `orchestrator.url` | `http://localhost:8080` | `MA__ORCHESTRATOR__URL` | Orchestrator base URL. |
| `orchestrator.timeout_ms` | `4000` | `MA__ORCHESTRATOR__TIMEOUT_MS` | Timeout for one turn call; should exceed the orchestrator's turn deadline. |
| `orchestrator.verify_tls` | `true` | `MA__ORCHESTRATOR__VERIFY_TLS` | Verify the orchestrator certificate. |
| `orchestrator.ca_bundle` | `""` | `MA__ORCHESTRATOR__CA_BUNDLE` | CA bundle for the orchestrator (mesh CA). |
| `orchestrator.client_cert_file` | `""` | `MA__ORCHESTRATOR__CLIENT_CERT_FILE` | Client certificate for mTLS. |
| `orchestrator.client_key_file` | `""` | `MA__ORCHESTRATOR__CLIENT_KEY_FILE` | Client key for mTLS. |
| `orchestrator.auth_scope` | `""` | `MA__ORCHESTRATOR__AUTH_SCOPE` | Entra scope for an app-only Workload Identity token sent to the orchestrator (`auth.mode: jwt`), e.g. `api://<orchestrator-app-id>/.default`. Empty: no token (mesh identity or local). |
| `behaviour.greeting` | `Hello, how can I help you today?` | `MA__BEHAVIOUR__GREETING` | First sentence spoken when the agent joins. |
| `behaviour.holding_phrase` | `One moment while I check that for you.` | `MA__BEHAVIOUR__HOLDING_PHRASE` | Spoken when the orchestrator has not answered within holding_after_ms. |
| `behaviour.holding_after_ms` | `900` | `MA__BEHAVIOUR__HOLDING_AFTER_MS` | Delay before the holding phrase. |
| `behaviour.unavailable` | `I'm having trouble right now. Please try again shortly, or I can connect you with an advisor.` | `MA__BEHAVIOUR__UNAVAILABLE` | Spoken when the orchestrator cannot be reached. |
| `behaviour.approval_rpc_method` | `orchestrator.approval_request` | `MA__BEHAVIOUR__APPROVAL_RPC_METHOD` | LiveKit RPC method the client app registers for approval requests. |
| `behaviour.workflow_poll_interval_s` | `2.0` | `MA__BEHAVIOUR__WORKFLOW_POLL_INTERVAL_S` | Polling interval for transaction status. |
| `behaviour.workflow_poll_timeout_s` | `660.0` | `MA__BEHAVIOUR__WORKFLOW_POLL_TIMEOUT_S` | Stop polling after this long (approval timeout plus execution). |
| `behaviour.outcome_messages` | see source | `MA__BEHAVIOUR__OUTCOME_MESSAGES` | Spoken per transaction outcome: completed, declined, expired, failed. |

## Token service

YAML file from `TS_CONFIG_FILE`. Environment overrides use the prefix `TS__`.

| Key | Default | Environment override | Description |
|---|---|---|---|
| `profile` | `dev` | `TS__PROFILE` | `dev` or `prod` (prod requires user token validation, https orchestrator, TTL at most 900 s). |
| `cell_id` | `local` | `TS__CELL_ID` | Cell identifier used for admission counting. |
| `orchestrator_url` | `http://localhost:8080` | `TS__ORCHESTRATOR_URL` | Orchestrator base URL. |
| `orchestrator_auth_scope` | `""` | `TS__ORCHESTRATOR_AUTH_SCOPE` | Entra scope for an app-only Workload Identity token sent to the orchestrator (`auth.mode: jwt`). Empty: no token (mesh identity or local). |
| `orchestrator_ca_bundle` | `""` | `TS__ORCHESTRATOR_CA_BUNDLE` | CA bundle for the orchestrator. |
| `client_cert_file` | `""` | `TS__CLIENT_CERT_FILE` | Client certificate for mTLS to the orchestrator. |
| `client_key_file` | `""` | `TS__CLIENT_KEY_FILE` | Client key for mTLS. |
| `dispatch_key_env` | `MA_DISPATCH_KEY` | `TS__DISPATCH_KEY_ENV` | Environment variable with the dispatch HMAC key (shared with the master agent). |
| `user_jwt.issuer` | `""` | `TS__USER_JWT__ISSUER` | Expected issuer of user access tokens. |
| `user_jwt.audience` | `""` | `TS__USER_JWT__AUDIENCE` | Expected audience of user access tokens. |
| `user_jwt.jwks_url` | `""` | `TS__USER_JWT__JWKS_URL` | JWKS URL of the IdP. |
| `user_jwt.algorithms` | `["RS256"]` | `TS__USER_JWT__ALGORITHMS` | Accepted algorithms. |
| `user_jwt.leeway_s` | `30` | `TS__USER_JWT__LEEWAY_S` | Allowed clock skew. |
| `acr_claim` | `acr` | `TS__ACR_CLAIM` | Claim holding the authentication level. |
| `acr_levels` | `["low", "standard", "stepup"]` | `TS__ACR_LEVELS` | Recognised authentication levels, weakest first. |
| `livekit.url` | `wss://livekit.example.internal` | `TS__LIVEKIT__URL` | LiveKit URL returned to the client. |
| `livekit.api_key_env` | `LIVEKIT_API_KEY` | `TS__LIVEKIT__API_KEY_ENV` | Environment variable with the LiveKit API key. |
| `livekit.api_secret_env` | `LIVEKIT_API_SECRET` | `TS__LIVEKIT__API_SECRET_ENV` | Environment variable with the LiveKit API secret. |
| `livekit.token_ttl_s` | `600` | `TS__LIVEKIT__TOKEN_TTL_S` | Lifetime of the participant token. |
| `livekit.agent_name` | `master-agent` | `TS__LIVEKIT__AGENT_NAME` | Agent to dispatch into the room. |
| `livekit.room_prefix` | `vs` | `TS__LIVEKIT__ROOM_PREFIX` | Prefix for room names. |
| `admission.enabled` | `false` | `TS__ADMISSION__ENABLED` | Enable cell-level session admission (Redis). |
| `admission.redis_url_env` | `TS_REDIS_URL` | `TS__ADMISSION__REDIS_URL_ENV` | Environment variable with the Redis URL. |
| `admission.max_sessions_per_cell` | `2000` | `TS__ADMISSION__MAX_SESSIONS_PER_CELL` | Maximum concurrent sessions per cell. |
| `admission.session_ttl_s` | `3600` | `TS__ADMISSION__SESSION_TTL_S` | Lifetime of an admission slot. |
| `allowed_channels` | `["voice", "chat"]` | `TS__ALLOWED_CHANNELS` | Channels a client may request. |

