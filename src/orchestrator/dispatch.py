"""Signed LiveKit dispatch metadata.

The token service puts the session id and root trace context into the agent
dispatch metadata of the room. Room metadata is visible to room participants,
so it carries no user identity, and it is HMAC-signed so a worker only joins
rooms the token service created.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


class DispatchError(Exception):
    pass


@dataclass(frozen=True)
class DispatchInfo:
    session_id: str
    traceparent: str
    issued_at: int
    channel: str = "voice"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_dispatch(info: DispatchInfo, key: bytes) -> str:
    body = json.dumps(
        {"sid": info.session_id, "tp": info.traceparent, "iat": info.issued_at, "ch": info.channel},
        sort_keys=True, separators=(",", ":"),
    )
    sig = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())
    return json.dumps({"v": 1, "body": body, "sig": sig})


def verify_dispatch(raw: str, key: bytes, max_age_s: int = 120, now: float | None = None) -> DispatchInfo:
    try:
        envelope = json.loads(raw)
        body, sig = envelope["body"], envelope["sig"]
        expected = _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())
        if envelope.get("v") != 1 or not hmac.compare_digest(expected, sig):
            raise DispatchError("bad signature")
        data = json.loads(body)
    except (KeyError, TypeError, ValueError) as exc:
        raise DispatchError("malformed dispatch metadata") from exc
    now = time.time() if now is None else now
    if abs(now - int(data["iat"])) > max_age_s:
        raise DispatchError("dispatch metadata expired")
    return DispatchInfo(str(data["sid"]), str(data["tp"]), int(data["iat"]), str(data.get("ch", "voice")))
