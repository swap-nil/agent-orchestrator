"""Token service configuration (YAML + ``TS__SECTION__KEY`` environment overrides)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from orchestrator.config import JwtConfig, load_dataclass_config


@dataclass
class LiveKitConfig:
    url: str = "wss://livekit.example.internal"
    api_key_env: str = "LIVEKIT_API_KEY"
    api_secret_env: str = "LIVEKIT_API_SECRET"
    token_ttl_s: int = 600
    agent_name: str = "master-agent"
    room_prefix: str = "vs"


@dataclass
class AdmissionConfig:
    enabled: bool = False
    redis_url_env: str = "TS_REDIS_URL"
    max_sessions_per_cell: int = 2000
    session_ttl_s: int = 3600


@dataclass
class TokenServiceConfig:
    profile: str = "dev"
    cell_id: str = "local"
    orchestrator_url: str = "http://localhost:8080"
    # Entra scope for an app-only token to the orchestrator (auth.mode: jwt), e.g.
    # "api://<orchestrator-app-id>/.default". Empty: no token (mesh identity or local).
    orchestrator_auth_scope: str = ""
    orchestrator_ca_bundle: str = ""
    client_cert_file: str = ""
    client_key_file: str = ""
    dispatch_key_env: str = "MA_DISPATCH_KEY"
    user_jwt: JwtConfig = field(default_factory=JwtConfig)
    acr_claim: str = "acr"
    acr_levels: list[str] = field(default_factory=lambda: ["low", "standard", "stepup"])
    livekit: LiveKitConfig = field(default_factory=LiveKitConfig)
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    allowed_channels: list[str] = field(default_factory=lambda: ["voice", "chat"])


def load_token_service_config(path: str | None = None) -> TokenServiceConfig:
    config = load_dataclass_config(TokenServiceConfig, path or os.environ.get("TS_CONFIG_FILE"), os.environ, "TS__")
    if config.profile == "prod":
        if not config.user_jwt.jwks_url:
            raise ValueError("prod: user_jwt must be configured")
        if not config.orchestrator_url.startswith("https://"):
            raise ValueError("prod: orchestrator_url must use https")
        if config.livekit.token_ttl_s > 900:
            raise ValueError("prod: livekit.token_ttl_s must be at most 900")
    return config
