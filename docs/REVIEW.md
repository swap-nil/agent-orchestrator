# Review record

Two review passes were run over the implementation after it was complete, each followed by fixes and a full test run. Pass 1 looked at correctness and robustness; pass 2 at security and operations. Issues caught earlier by the test suite while building are listed first for completeness. Every fix below is covered by a unit test unless marked otherwise.

## Found by tests during implementation

| ID | Finding | Fix |
|---|---|---|
| T-1 | An environment override of one dictionary key (for example `ORCH__ROUTING__MIN_CONFIDENCE__R2`) replaced the whole dictionary and lower-cased the key, so the other risk classes lost their thresholds and validation failed. | Dictionary fields are now merged onto their defaults with case-insensitive key matching. |
| T-2 | The domain-agent kit cached results by idempotency key, but concurrent duplicates arriving before the first finished would all execute. For a write this means a duplicate order. | Duplicates of one key are serialised with a per-key lock; later ones return the cached result. |
| T-3 | A retried turn from the master agent (after a dropped connection) would be executed twice, including starting a second transaction. | Turns are idempotent by `turn_id`: the session keeps the last 20 responses and replays them. |

## Pass 1: correctness and robustness

| ID | Finding | Fix |
|---|---|---|
| P1-1 | A request aimed at a kill-switched intent ("sell my ETF" while trading is off) fell through to the FAQ fallback and got an unrelated answer. | The router reports a disabled match; the service answers with `messages.transactions_unavailable` (R3) or `messages.refused`. |
| P1-2 | All outbound HTTP shared the A2A mesh TLS settings. With a private mesh CA, calls to Entra ID would fail certificate verification. httpx also deprecates passing CA paths and certificate tuples directly. | Two transports: a mesh transport (mesh CA + workload certificate) for the gateway only, and a public transport (system trust store) for the IdP, OPA and the classifier. Both use explicit `ssl.SSLContext` objects (`orchestrator/tls.py`). |
| P1-3 | Intents with several write steps were accepted, but there is no compensation (saga) logic, so a failure after the first write would leave a partial transaction. | The catalogue rejects more than one write step per intent until saga support is added. |
| P1-4 | Approved writes ran with a deadline shorter than the write step's own configured timeout. | The write deadline is the longest write-step timeout plus 1 s of retry headroom. |
| P1-5 | Declines were detected by comparing a reason string. | Explicit `declined` flag on the approval outcome. |
| P1-6 | Nothing closed orchestrator sessions, so bound user tokens lived until the session TTL. | The master agent closes the session when the room ends; `DELETE /v1/sessions/{id}` accepts the `turns` or `sessions` caller group. |
| P1-7 | Lock contention on a session (barge-in) surfaced as a handover. | It now returns the busy message. |
| P1-8 | Default session lock TTL (3 s) was no longer than the turn deadline plus margin, so a slow turn could lose its lock. | Default raised to 5 s; validation requires lock TTL ≥ turn deadline + 500 ms. |
| P1-9 | `server.host` and `server.port` were configurable but unused. | `python -m orchestrator` starts uvicorn from configuration; the image uses it. |
| P1-10 | Code quality: private attribute access across classes, `__import__` shortcuts, `__dict__` copying of frozen dataclasses, unused imports. | Replaced with public methods, normal imports and `dataclasses.replace`. |

## Pass 2: security and operations

| ID | Finding | Fix |
|---|---|---|
| P2-1 | Approval tokens were HMAC-signed, so every service that verifies them (tool gateway, trade agent) would need the signing key and could mint approvals. | Ed25519 signatures. Only the orchestrator holds the private key; verifiers get the public key from `python -m orchestrator.cli approval-public-key`. |
| P2-2 | The write executed with the session's original token, which is not step-up authenticated and may have expired during the approval wait. | The step-up token presented with the approval is bound (encrypted) to the session for that workflow and used for the write's token exchange. It is never placed in Temporal history. |
| P2-3 | Token exchange sent a client secret or nothing. In AKS the orchestrator should use Workload Identity federation. | `identity.client_auth: workload_identity` sends the projected federated token as a client assertion, re-read on each call because it rotates. `secret` is refused in prod. |
| P2-4 | Transaction inputs and results (instrument, quantity, masked account) were stored in plaintext in Temporal history and visible in its UI. | Payload codec encrypts all workflow payloads (`adapters/temporal_codec.py`); the key is required in prod. |
| P2-5 | The master agent rejected dispatch metadata older than 120 s, but LiveKit tokens live 600 s, so users joining after two minutes would get no agent. | Default max age 960 s, and a test asserts it covers the token TTL. |
| P2-6 | A failed session bind in the token service leaked a cell admission slot until its TTL. | Slot released on failure. |
| P2-7 | Approval decisions were only recorded on the workflow's audit chain, making session investigations incomplete. | Also recorded on the session chain. |

## Pass 3: command center

The command center was built on the audit ledger rather than beside it, so every dashboard number traces back to a verifiable record. Its review found and fixed these, each covered by a test or by the browser run:

| ID | Finding | Fix |
|---|---|---|
| P3-1 | Early exits in the turn pipeline (blocked, clarify, refused, busy, error) wrote no terminal record, so latency and outcome metrics would have counted only answered turns. | One `turn_completed` event on every path, including busy and error. |
| P3-2 | The live stream registered its subscriber on first read, so events between connecting and reading were lost. | The stream pins the sequence number at connect time and replays newer events from the buffer. |
| P3-3 | Switching off an optional agent (market data) refused every portfolio question instead of answering without prices. The dashboard made this visible as a spike in policy denials. | The planner drops optional steps whose agent is switched off; the answer is marked partial. Required agents still refuse. |
| P3-4 | Broadening the trade intent's pattern to `\bsell\b` passed every eval: the golden set had no benign sentence using "sell". | Added `rt-no-trade-sell-word`; the gate now rejects that change. |
| P3-5 | The kill-switch toggle's visual layer sat over its checkbox, which broke direct hit-testing and assistive technology. | The input sits on top, transparent. |
| P3-6 | Wide tables stretched the page at phone width instead of scrolling inside their panels. | Grid tracks use `minmax(0, 1fr)`; verified with no horizontal overflow on every view at 400 px. |
| P3-7 | In development the operator picker could show a different person than the one the page was acting as, which misleads during four-eyes flows. | The picker shows the actual current operator. |
| P3-8 | Runtime changes could have altered structure (risk class, steps, write permissions) or lowered advice and transaction thresholds. | Only behavioural fields are editable, with a floor for R2 and R3 thresholds; everything else needs a deployment. |

Design decisions worth reviewing with risk and compliance:
- Utterances are hidden from everyone except investigators, and only when `show_utterances` is on; each view is audited.
- Runtime changes need an eval gate, four eyes and a reason.
- Demo mode is a faithful simulation for training and review, never connected to data.

## Pass 4: Azure test environment

Building the test environment ran the services over real HTTP for the first time and checked the charts against a cluster-shaped configuration. Found and fixed:

| ID | Finding | Fix |
|---|---|---|
| P4-1 | The agent kit's FastAPI wrapper answered every call with HTTP 422: with postponed annotations and a function-local import, FastAPI could not resolve `Request` and treated it as a query parameter. The compose stub agents were affected too. Unit tests called `handle` directly and missed it. | Kit no longer uses postponed annotations; a test calls the agents over HTTP. |
| P4-2 | The OPA sidecar's readiness probe targeted `127.0.0.1`, which the kubelet cannot reach, so API pods would never become ready. | OPA serves health on `--diagnostic-addr` (port 8282); the policy API stays on loopback. |
| P4-3 | The orchestrator NetworkPolicy allowed no egress to Entra ID (JWKS, token exchange) or to private endpoints by default. | `egressPublicHttps` and per-range `egressCidrs` with ports. |
| P4-4 | Callers other than the mesh had no way to authenticate (`auth.mode: jwt` needed a token source). | `orchestrator.service_auth`: Workload Identity client-credentials tokens for the token service, master agent and client backend. |
| P4-5 | No audit table migration in the chart; unpinned dependencies; image tag 1.0.0 against app 1.1.0. | Migration init container; `requirements/*.lock` used as build constraints; images pinned by digest. |
| P4-6 | The chart covered only the orchestrator. | Charts for domain agents and A2A gateway, voice, edge (token service, test client, console sign-in, ingress) and platform (Temporal, OTel). |

Verified: the master agent's LiveKit Agents usage (`on_user_turn_completed`, `StopResponse`, `session.say`, `perform_rpc`, `WorkerOptions(agent_name)`, health on 8081) matches livekit-agents 1.8.3.

## Open items (not code defects; verify in your environment)

These could not be verified here because the sandbox has no network and none of the third-party SDKs installed. They are integration tests, not unit tests.

The LiveKit Agents hooks (`on_user_turn_completed`, `StopResponse`, `session.say`, `perform_rpc`) and whether `AgentSession` runs without an LLM should be checked against the pinned `livekit-agents` version; if an LLM is required, configure a small in-region model, since `StopResponse` prevents it from ever answering. The A2A JSON-RPC method and field names follow v1.0 as documented and are configurable, but conformance should be proven with the A2A TCK. The Microsoft Agent Framework sample's class names should be checked against the pinned version. The Rego policy has tests (`opa test policies/`) that were not run here; a parity test checks that its deny messages match the Python engine. The FastAPI app (including the command-center routes), Redis (including the Redis Streams event bus), PostgreSQL, Temporal and httpx adapters are syntax-checked only.

Design limitations to track: admission control is per replica (the token service adds cell-level admission); the input guard's injection patterns are heuristics and should be complemented by a model-based classifier; clients should recompute the action hash from the action they display rather than echo the one they were sent (see the user guide); request-body limits rely on `Content-Length` and should also be enforced at ingress; card canonicalisation for signed Agent Cards (JCS) is not implemented in this release because card verification is done by the registry pipeline.
