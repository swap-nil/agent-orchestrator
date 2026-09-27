# Command Center

The command center is the operations console for the orchestrator. It shows what the agents are doing in real time and why each decision was made. It measures quality with evals, and it lets authorised people act: switch capabilities off, end sessions, reset breakers, and improve agent behaviour at runtime under four-eyes control.

Everything it shows comes from one source: the decision events the orchestrator writes to its tamper-evident audit ledger. The live feed, metrics, alerts and turn traces therefore always agree with the record an auditor would check.

## 1. Try it

```bash
make console            # whole stack in one process, simulated customers
open http://127.0.0.1:8765/console
```

The development server runs the real orchestrator with the reference agents in-process, an in-process transaction engine that mirrors the Temporal workflow, and a traffic generator. Virtual customers ask questions, trade, approve, decline and sometimes attack. It needs only the standard library and binds to localhost.

Opened without an orchestrator behind it (for example as a published page), the console switches to **demo mode**. It then simulates the platform in the browser, using the repository's own intent catalogue, agent registry, guard settings, routing thresholds, messages, eval suite and reference-agent answers. `make console-seed` re-embeds them, and a test fails when the embedded copy is out of date. Demo mode starts with 30 minutes of history, including a resolved market-data incident, so every view has something to show.

## 2. The views

| View | What it answers |
|---|---|
| **Overview** | Is the service healthy right now? Throughput, p95 latency against the turn deadline, answer, handover and refusal rates, blocked inputs, policy denials, transactions, active sessions, outcome and latency charts, agent health, safeguard activity, runtime version and latest eval score. |
| **Live decisions** | What is happening, event by event? Every audited decision streams in with a plain-language summary, filterable by type and session. Click any turn to open the **turn inspector**: each stage from hearing the user to responding, with its timing, the reason for the decision, the policy engine's verdict, and whether the session's audit chain verifies. |
| **Agents** | How is each domain agent doing? Calls, failures, error rate, p50 and p95 latency, retries, timeouts, policy denials, circuit-breaker state, and one-click disable or breaker reset. Also a per-intent breakdown. |
| **Safeguards** | Are the guardrails working? Blocked inputs by reason, PII redactions by kind, output checks, ungrounded answers dropped, policy denials by reason, the approval funnel for transactions, and the risk-class mix. |
| **Evals** | Is quality holding? Latest pass rate per category (routing, safety, policy, end to end), pass rate by behaviour version against the change gate, every case with expected and actual results, and run history. |
| **Skill studio** | Improve behaviour at runtime: edit routing patterns, agent instructions, prompts, read-backs, guard lists and thresholds. Test a sentence live, then propose, evaluate, shadow, approve and roll back. |
| **Controls** | Kill switches for risk classes, intents and agents (staged, then applied with a reason), circuit breakers, open sessions with termination, and fault injection on the development server and in demo mode. |
| **Audit** | Every operator action on the control-plane chain with its hash, chain verification, active and resolved alerts, and the alert rules in force. |

Times are shown in Europe/Zurich. The window selector (5 min, 15 min, 1 h) scopes every number on the page.

## 3. Roles and sign-in

| Role | Can |
|---|---|
| viewer | See every dashboard, the live feed, turn traces (utterances hidden), evals and the audit trail. |
| operator | Viewer, plus: kill switches, session termination, breaker reset, alert acknowledgement, eval runs, proposing and shadowing changes, rollback, fault injection (development only). |
| investigator | Viewer, plus: sees user utterances in turn traces when `command_center.show_utterances` is on. Utterances are PII-redacted before they are ever stored, and every view is audited as `utterance_viewed`. |
| approver | Viewer, plus: approves or rejects runtime changes, never their own. |

In production operators sign in with Entra ID (`command_center.operator_auth: jwt`). Create app roles `CC.Viewer`, `CC.Operator`, `CC.Investigator` and `CC.ChangeApprover` on the console's app registration, and assign them to groups. `command_center.role_members` maps them to console roles. Serve `/console` behind your internal authenticating proxy (for example Application Gateway with Entra authentication, APIM, or oauth2-proxy). The proxy adds the operator's bearer token to `/admin/cc/*` requests, and the orchestrator validates it (issuer, audience, expiry, signature, roles) on every call. The console is never exposed to the internet; restrict the route to the operations network.

In development (`operator_auth: none`) the `X-Operator` header names the operator and `X-Operator-Roles` narrows the roles. The console's operator picker sets both, so four-eyes flows can be rehearsed: propose as Olga, approve as Anna.

Every state-changing action requires a written reason (`command_center.require_reason_for_actions`, mandatory in production). The reason is recorded with the operator's identity on the `control-plane` audit chain.

## 4. How the numbers are made

The orchestrator records one terminal `turn_completed` event per turn on every path, including busy, error and replayed turns. The event carries the outcome, intent, risk class, total latency, reasons, sources, dropped artifacts, degraded mode and the runtime version. Richer events explain each decision: `routed` (with threshold and risk), `planned` (steps, layers, cost, skipped optional steps), `policy_checked` (every step's verdict, engine and decision id), `step_result` (agent, skill, attempts, latency, error), `output_checked` (sources, dropped artifacts, disclaimer), `approval_*`, `transaction_*` and `shadow_routed`.

The telemetry aggregator keeps these in 10-second buckets for an hour (`metrics_bucket_s`, `metrics_retention_s`) and computes any window from them. Rates use turns in the window as the denominator; replays are excluded. Latency percentiles are exact over the window's samples. For long-term history, use the OpenTelemetry pipeline; this is the operational, real-time view.

With several replicas, set `command_center.event_bus: redis` (mandatory in production). Every replica then publishes into one Redis stream per cell, and the console sees the whole cell. The in-memory bus shows only the replica serving the console. Publishing is best effort: a slow or failing bus never delays or fails a turn, and slow viewers lose their oldest events rather than holding anything up.

## 5. Alerts

Rules live in `command_center.alert_rules`:

```yaml
command_center:
  alert_rules:
    - {id: latency-p95, metric: latency.p95_ms, op: ">", threshold: 2200, window_s: 300, min_samples: 20, severity: warning, description: p95 turn latency near the turn deadline}
    - {id: agent-errors, metric: "agents.*.error_rate", op: ">", threshold: 0.2, window_s: 300, min_samples: 10, severity: critical, description: Domain agent failing}
```

`metric` is a dotted path into the metrics snapshot. `agents.*.<field>` is evaluated per agent, with `min_samples` counted in that agent's calls. Two critical alerts are built in: an open circuit breaker on any agent, and audit write failures. Alerts fire when their condition holds, can be acknowledged with a note, and resolve by themselves when the condition clears. Firing and resolution are recorded on the `alerts` chain, acknowledgements on `control-plane`. Forward `alert_fired` events to your on-call tool from the event stream or the audit ledger.

## 6. Evals

The golden dataset is `config/evals.yaml` (`command_center.evals_file`), with six kinds of case:

| Kind | Checks |
|---|---|
| `routing` | text → expected intent, `clarify`, or `disabled` |
| `input_guard` | text → blocked or not, with expected flags |
| `pii` | text → kinds redacted, strings that must not survive, no false positives with `exact: true` |
| `output_guard` | answer text, risk class and grounding → allowed or not |
| `policy` | intent, step, authentication level, environment, channel, phase, approval, kill switch → allow or a specific deny reason |
| `e2e` | a whole turn, or with `turns` a whole conversation, through an isolated orchestrator with the reference agents → outcome, intent, required and forbidden phrases, sources |

Run them from the Evals view, or in CI with `make evals` (`python -m orchestrator.cli run-evals`, non-zero exit below the gate). The suite runs in about 30 ms, which is why it can gate every change. End-to-end cases use the reference agents, so they test orchestration behaviour, not your production agents' answers; add live-agent evals in your integration environment.

Treat the dataset as the platform's memory. Every complaint, incident or near miss becomes a case. While building this, the suite itself had a gap: broadening the trade pattern to `\bsell\b` passed every case until `rt-no-trade-sell-word` ("How do I sell my old car to a dealer?") was added; since the trade intent gained exclusion patterns for questions, `rt-no-trade-branch-news` ("Is the bank going to sell the branch in Bern?") is the case that catches it. The repository ships with one known failing case, `rt-pf-networth` ("What is my net worth?"), so the first improvement in the skill studio has something real to fix. The `cv-*` cases replay real conversations turn by turn; see section 6.1.

### 6.1 Conversations

An `e2e` case with `turns` replays a conversation in one session and checks every turn, so follow-up questions, slot filling and cancellations are tested the way users meet them:

```yaml
- id: cv-slot-filling
  kind: e2e
  expect_type: approval_required
  turns:
    - {text: "I want to sell", expect_type: clarify, must_contain: ["Which of your holdings"]}
    - {text: "the tech ETF", expect_type: clarify, must_contain: ["How much of the tech ETF"]}
    - {text: "all of it", expect_type: approval_required, must_contain: ["400 units of Tech ETF"]}
```

`cv-transcript` replays the conversation that sold the wrong instrument and answered "Stop" and a weather question with branch opening hours. Routing cases for a compound request expect the intents joined with `+` in execution order (`portfolio.overview+trade.sell`). `tests/test_conversation_regressions.py` covers the same ground against the fake core bank and the backend agents, and the master agent's control commands against a fake LiveKit session.

## 7. Improving behaviour at runtime

The skill studio changes **behaviour**, never **structure**:

| Can change at runtime | Needs code review and deployment |
|---|---|
| Routing patterns of an intent | Risk class of an intent |
| Agent instructions and step timeouts (within bounds) | Steps, dependencies, agents, skills |
| Clarifying questions, read-back templates, descriptions | Write permissions, certification, clearance |
| Injection patterns, prohibited and pressure phrases, advice disclaimer | Authentication levels, policy rules |
| Routing thresholds (never below the deployed floor for R2 and R3) | Budgets, deadlines, anything in `orchestrator.yaml` beyond these |

That boundary is what makes hot-swapping safe: a turn that straddles a swap still sees the same plan shape.

Every change follows the same path, each step audited on `control-plane`:

1. **Propose.** Edit in the studio and give a reason. Use *Try a sentence* first to see how the live configuration and your edits route it.
2. **Validate.** Only allowed fields may change. Patterns must compile and must not match everything, templates may use only plain field names, timeouts and thresholds must stay within bounds, and the resulting catalogue must pass the same validation as at start-up. Removing guard entries, lowering thresholds or changing how an R2 or R3 intent is recognised is flagged as sensitive.
3. **Evaluate.** The full suite runs against the candidate and is compared with the live baseline. The gate needs no regressions and at least `change_min_pass_rate`.
4. **Shadow (optional).** Live traffic is routed both ways and agreement is recorded; customers only ever get the live answer.
5. **Approve.** Someone with the approver role other than the proposer applies it (`require_four_eyes`, mandatory in production). A change evaluated against an older version cannot be applied; propose it again.
6. **Apply.** The change becomes the next version. Replicas pick it up from the shared store within about 5 seconds, a fresh baseline eval runs, and turns record the `runtime_version` they ran under.
7. **Roll back.** Return to any earlier version with a reason. Later versions stay in history.

Runtime changes live in the session store (Redis) and in the audit ledger. Fold approved changes back into the repository's YAML regularly, so the deployed files stay the source of truth. Until then, a redeploy starts from the files and the stored versions are re-applied from the store.

## 8. API

All routes are under `/admin/cc` and are served by the orchestrator's FastAPI app, and by the development server with exactly the same code (`ConsoleAPI.dispatch`).

| Method and path | Role | Purpose |
|---|---|---|
| `GET /me` | viewer | operator, roles, environment, capabilities |
| `GET /overview?window=` | viewer | metrics snapshot, alerts, kill switches, breakers, runtime, evals, health |
| `GET /timeseries?window=&points=` | viewer | series per outcome, p95, blocks, errors |
| `GET /stream` (SSE) | viewer | live decision events; resume with `Last-Event-ID` or `?after=` |
| `GET /events?after=&session=` | viewer | recent events (polling fallback) |
| `GET /sessions`, `GET /sessions/{id}` | viewer | recent sessions; one session's turns with chain verification |
| `GET /sessions/{id}/turns/{turn}` | viewer | turn inspector |
| `POST /sessions/{id}/terminate` | operator | end a session (reason) |
| `GET /kill-switch`, `PUT /kill-switch` | viewer / operator | read or set runtime kill switches (reason) |
| `POST /breakers/{agent}/reset` | operator | close a breaker (reason) |
| `GET /alerts`, `POST /alerts/{key}/ack` | viewer / operator | alerts and rules; acknowledge (note) |
| `GET /evals`, `GET /evals/{run}`, `POST /evals/run` | viewer / operator | eval history and results; run now |
| `GET /catalogue` | viewer | effective behaviour configuration and what is editable |
| `POST /preview` | viewer | route a sentence with the live configuration and optional candidate edits |
| `GET /changes`, `GET /changes/{id}`, `POST /changes` | viewer / operator | list, inspect, propose (reason) |
| `POST /changes/{id}/shadow`, `POST /shadow/stop` | operator | shadow testing |
| `POST /changes/{id}/approve`, `POST /changes/{id}/reject` | approver | decide (never your own) |
| `GET /versions`, `POST /versions/rollback` | viewer / operator | version history; roll back (reason) |
| `GET /audit/control`, `GET /audit/{chain}/verify` | viewer | operator actions with hashes; verify any chain |
| `GET /chaos`, `PUT /chaos` | viewer / operator | fault injection (development server only) |

## 9. Production checklist

- `command_center.operator_auth: jwt` with the console's issuer, audience and JWKS, and app roles assigned to groups, not individuals.
- `event_bus: redis` with a per-cell `stream_key`.
- `require_four_eyes` and `require_reason_for_actions` on (validation enforces both).
- `show_utterances` off unless your data-protection assessment allows investigators to see redacted utterances.
- `/console` and `/admin/cc` reachable only from the operations network, through the authenticating proxy.
- Alert events forwarded to on-call.
- `make evals` in CI for every change to `config/intents.yaml`, `config/agents.yaml` or guard settings.

## 10. What is verified

The backend (event bus, telemetry, alerts, inspector, evals, runtime change control, roles and the API) is covered by unit tests. The development server is tested over real sockets, including the event stream. The console was exercised in a real browser (Chromium through Playwright), both against the live Python backend and in demo mode, on these flows:
- every view renders and the turn inspector opens;
- a kill switch applies, and a too-short reason is refused;
- a sentence routes in the preview;
- a change is proposed and passes its gate, and self-approval is blocked;
- shadow testing records agreement, a second operator approves, and a rollback works;
- evals run and the audit chain verifies.

It was also checked at phone width and in dark mode, with no console errors. The browser script is `tests/browser/console.e2e.js`.

The FastAPI routes and the Redis Streams bus are syntax-checked only, because the build environment had no FastAPI or Redis. Run them against your pinned versions before production.
