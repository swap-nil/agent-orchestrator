# Agent Orchestrator

Python implementation of the enterprise multi-agent orchestrator described in the whitepaper: a LiveKit master agent for voice and chat, a deterministic orchestrator that plans, checks policy and calls domain agents over A2A v1.0, durable transactions with step-up approval on Temporal, and a tamper-evident audit ledger. Built for Azure (AKS, Entra ID, Key Vault, Managed Redis, PostgreSQL) with Switzerland-only data residency in mind.

## Start here

```bash
make test                 # 155 unit tests, no network needed
make console              # command center + whole stack in one process: http://127.0.0.1:8765/console
make evals                # golden eval suite, the same gate that guards runtime changes
make compose-up           # full local stack (Redis, PostgreSQL, Temporal, OPA, OTel, LiveKit, demo agents)
make azure-deploy         # complete test environment on one Azure VM, with a fake core-banking backend
```

Then follow the scripted conversation in the user guide, section 4.

## Documentation

| Document | Contents |
|---|---|
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Architecture, quick start, catalogue and policy authoring, adding agents, client integration, AKS deployment, operations, security checklist, troubleshooting |
| [docs/COMMAND_CENTER.md](docs/COMMAND_CENTER.md) | The operations console: live decisions and why, dashboards, evals, kill switches, governed runtime changes, roles, API |
| [docs/AZURE_TEST_ENV.md](docs/AZURE_TEST_ENV.md) | Deploying the complete test environment to one Azure VM (Docker Compose, Entra ID, LiveKit, fake bank), testing it, differences from production, teardown |
| [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | Every configuration key for all three services, generated from the code |
| [docs/REVIEW.md](docs/REVIEW.md) | Findings and fixes from the two review passes, and open items to verify |

## Layout

```
src/orchestrator/      core pipeline, adapters (httpx, Redis, PostgreSQL, Temporal, OTel), API, workflows, CLI
src/orchestrator/console/  command center: event bus, telemetry, alerts, inspector, evals, runtime changes, API, dev server, web app
src/master_agent/      LiveKit Agents worker and provider factories
src/token_service/     session start: user token validation, admission, LiveKit token with signed dispatch
src/domain_agents/     agent kit implementing the A2A contract, demo agents, backend-driven test agents, Agent Framework sample
src/mock_backend/      fake core-banking API (seeded customers, portfolios, prices, orders) for test environments
src/test_client/       browser test client (Entra sign-in, voice and chat over LiveKit, approvals) and its client backend
config/                profiles (dev, compose, prod), intent catalogue, agent registry, golden evals, service configs
policies/              OPA policy (Rego v1) and its tests
deploy/                Dockerfile, docker compose, Helm charts, Azure test environment (Bicep, VM stack, scripts), Envoy A2A gateway, OTel collector
requirements/          pinned dependency locks per image (make lock)
scripts/               configuration reference generator
tests/                 unit, contract and consistency tests
```

## Status

The orchestration core is unit-tested. The FastAPI and httpx paths (orchestrator API, agents, token service, test client) have been run over real HTTP, and the LiveKit Agents calls were checked against the pinned 1.8.3 release. The Redis, PostgreSQL, Temporal and LiveKit runtime integrations are exercised by the Azure test environment and its smoke checks; see section 15 of the user guide.
