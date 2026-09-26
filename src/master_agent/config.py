"""Master agent configuration (YAML + ``MA__SECTION__KEY`` environment overrides)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from orchestrator.config import load_dataclass_config


@dataclass
class ProviderConfig:
    # provider: a key of master_agent.providers.STT_FACTORIES / TTS_FACTORIES
    provider: str = "deepgram"
    # Passed through to the LiveKit plugin constructor, e.g. model, language, base_url.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrchestratorClientConfig:
    url: str = "http://localhost:8080"
    timeout_ms: int = 4000
    verify_tls: bool = True
    ca_bundle: str = ""
    client_cert_file: str = ""
    client_key_file: str = ""
    # Entra scope for an app-only token (auth.mode: jwt), e.g. "api://<orchestrator-app-id>/.default".
    auth_scope: str = ""


@dataclass
class BehaviourConfig:
    greeting: str = "Hello, how can I help you today?"
    holding_phrase: str = "One moment while I check that for you."
    holding_after_ms: int = 900
    unavailable: str = "I'm having trouble right now. Please try again shortly, or I can connect you with an advisor."
    approval_rpc_method: str = "orchestrator.approval_request"
    workflow_poll_interval_s: float = 2.0
    workflow_poll_timeout_s: float = 660.0
    outcome_messages: dict[str, str] = field(default_factory=lambda: {
        "completed": "Done. Your order has been placed.",
        "declined": "Okay, I have cancelled that.",
        "expired": "The confirmation timed out, so nothing was done.",
        "failed": "I couldn't complete the order. Nothing was charged. An advisor can help you.",
    })


@dataclass
class MasterAgentConfig:
    agent_name: str = "master-agent"
    dispatch_key_env: str = "MA_DISPATCH_KEY"
    # Must be >= the token service's livekit.token_ttl_s: the room (and dispatch) may be
    # created when the user first joins, up to one token lifetime after issuance.
    dispatch_max_age_s: int = 960
    stt: ProviderConfig = field(default_factory=lambda: ProviderConfig("deepgram", {"model": "nova-3", "language": "multi"}))
    tts: ProviderConfig = field(default_factory=lambda: ProviderConfig("cartesia", {}))
    vad: ProviderConfig = field(default_factory=lambda: ProviderConfig("silero", {}))
    turn_detection: str = "multilingual"  # multilingual | vad
    orchestrator: OrchestratorClientConfig = field(default_factory=OrchestratorClientConfig)
    behaviour: BehaviourConfig = field(default_factory=BehaviourConfig)


def load_master_config(path: str | None = None) -> MasterAgentConfig:
    return load_dataclass_config(MasterAgentConfig, path or os.environ.get("MA_CONFIG_FILE"), os.environ, "MA__")
