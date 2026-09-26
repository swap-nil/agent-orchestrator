"""Pure logic of the token service (unit-tested)."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from orchestrator.dispatch import DispatchInfo, sign_dispatch
from orchestrator.tracing import new_trace


@dataclass(frozen=True)
class SessionPlan:
    session_id: str
    room: str
    participant_identity: str
    traceparent: str
    dispatch_metadata: str


def plan_session(*, room_prefix: str, dispatch_key: bytes, channel: str, now: float | None = None) -> SessionPlan:
    """Create identifiers for a new voice session.

    The session id is opaque and random; it is also the room name, so a room
    can never be guessed from user data. The participant identity is random
    too: user identity lives only in the orchestrator's session store.
    """
    session_id = secrets.token_urlsafe(18)
    trace = new_trace()
    metadata = sign_dispatch(
        DispatchInfo(session_id=session_id, traceparent=trace.traceparent, issued_at=int(now or time.time()), channel=channel),
        dispatch_key,
    )
    return SessionPlan(
        session_id=session_id,
        room=f"{room_prefix}-{session_id}",
        participant_identity=f"user-{secrets.token_hex(8)}",
        traceparent=trace.traceparent,
        dispatch_metadata=metadata,
    )


def participant_grants(room: str) -> dict[str, object]:
    """Minimal grants: join one room, publish microphone and data, subscribe. No admin, no recording."""
    return {
        "room_join": True,
        "room": room,
        "can_publish": True,
        "can_publish_data": True,
        "can_subscribe": True,
        "can_publish_sources": ["microphone"],
        "room_admin": False,
        "room_create": False,
        "room_record": False,
        "hidden": False,
    }
