"""A2A v1.0 client.

All calls go to the A2A mesh gateway, never directly to an agent. The gateway
resolves the agent from the registry, re-originates mTLS and enforces the call
graph. This client builds JSON-RPC 2.0 requests using the A2A v1.0 method and
field names; method names are configurable so a spec revision is a config
change. Validate against the A2A TCK for the SDK version you pin.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .config import A2AConfig
from .models import Artifact, TaskState
from .transport import HttpTransport, TransportError


class A2AError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class A2AResult:
    state: TaskState
    task_id: str | None
    artifacts: list[Artifact]
    status_text: str = ""


def _parts_to_artifact(parts: list[dict[str, Any]], metadata: dict[str, Any]) -> Artifact:
    texts: list[str] = []
    data: dict[str, Any] = {}
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("text"), str):
            texts.append(part["text"])
        if isinstance(part.get("data"), dict):
            data.update(part["data"])
    sources = metadata.get("sources") or data.get("sources") or []
    if not isinstance(sources, list):
        sources = []
    return Artifact(
        text="\n".join(t.strip() for t in texts if t.strip()),
        sources=[str(s) for s in sources],
        data=data,
        classification=str(metadata.get("classification", data.get("classification", "internal"))),
    )


def parse_send_result(result: Any) -> A2AResult:
    """Parse a SendMessage result, which holds either a task or a direct message."""
    if not isinstance(result, dict):
        raise A2AError("malformed A2A result", retryable=False)
    task = result.get("task")
    if task is None and "status" in result:
        task = result  # tolerate adapters that return the task directly
    if isinstance(task, dict):
        status = task.get("status") or {}
        state = TaskState.parse(status.get("state"))
        artifacts = [
            _parts_to_artifact(a.get("parts") or [], a.get("metadata") or {})
            for a in task.get("artifacts") or []
            if isinstance(a, dict)
        ]
        status_msg = status.get("message") or {}
        status_text = _parts_to_artifact(status_msg.get("parts") or [], {}).text if isinstance(status_msg, dict) else ""
        return A2AResult(state, task.get("id"), artifacts, status_text)
    message = result.get("message")
    if isinstance(message, dict):
        artifact = _parts_to_artifact(message.get("parts") or [], message.get("metadata") or {})
        return A2AResult(TaskState.COMPLETED, None, [artifact] if (artifact.text or artifact.data) else [])
    raise A2AError("A2A result contains neither task nor message", retryable=False)


class A2AClient:
    def __init__(self, config: A2AConfig, transport: HttpTransport) -> None:
        self._config = config
        self._transport = transport
        self._base = config.gateway_url.rstrip("/")

    def agent_url(self, agent: str) -> str:
        return self._base + self._config.agent_path_template.format(agent=agent)

    def build_send_request(
        self,
        *,
        context_id: str,
        instruction: str,
        data: dict[str, Any],
        metadata: dict[str, Any],
        return_immediately: bool,
        request_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        parts: list[dict[str, Any]] = []
        if instruction:
            parts.append({"text": instruction})
        if data:
            parts.append({"data": data})
        return {
            "jsonrpc": "2.0",
            "id": request_id or uuid.uuid4().hex,
            "method": self._config.method_send,
            "params": {
                "message": {
                    "messageId": message_id or uuid.uuid4().hex,
                    "role": self._config.user_role_value,
                    "contextId": context_id,
                    "parts": parts,
                    "metadata": metadata,
                },
                "configuration": {"returnImmediately": return_immediately},
                "metadata": metadata,
            },
        }

    def headers(self, *, traceparent: str, bearer: str | None) -> dict[str, str]:
        headers = {
            "A2A-Version": self._config.protocol_version,
            "traceparent": traceparent,
            "Content-Type": "application/json",
        }
        if self._config.required_extensions:
            headers["A2A-Extensions"] = ",".join(self._config.required_extensions)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    async def send(
        self,
        agent: str,
        *,
        context_id: str,
        instruction: str,
        data: dict[str, Any],
        metadata: dict[str, Any],
        traceparent: str,
        bearer: str | None,
        timeout_s: float,
        return_immediately: bool = False,
    ) -> A2AResult:
        body = self.build_send_request(
            context_id=context_id,
            instruction=instruction,
            data=data,
            metadata=metadata,
            return_immediately=return_immediately,
            # Message id doubles as idempotency key so a retried read is recognised by the agent.
            message_id=str(metadata.get("idempotencyKey") or uuid.uuid4().hex),
        )
        try:
            response = await self._transport.post(
                self.agent_url(agent), json_body=body, headers=self.headers(traceparent=traceparent, bearer=bearer), timeout_s=timeout_s
            )
        except TimeoutError as exc:
            raise A2AError("agent call timed out", retryable=True) from exc
        except TransportError as exc:
            raise A2AError(f"agent unreachable: {exc}", retryable=True) from exc
        if response.status in (429, 502, 503, 504):
            raise A2AError(f"agent temporarily unavailable (HTTP {response.status})", retryable=True)
        if response.status in (401, 403):
            raise A2AError(f"agent refused the call (HTTP {response.status})", retryable=False)
        if response.status != 200 or not isinstance(response.body, dict):
            raise A2AError(f"unexpected HTTP {response.status} from gateway", retryable=response.status >= 500)
        if "error" in response.body:
            err = response.body["error"] or {}
            raise A2AError(f"A2A error {err.get('code')}: {err.get('message')}", retryable=False)
        return parse_send_result(response.body.get("result"))

    async def cancel(self, agent: str, task_id: str, *, traceparent: str, bearer: str | None, timeout_s: float) -> None:
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": self._config.method_cancel, "params": {"id": task_id}}
        try:
            await self._transport.post(
                self.agent_url(agent), json_body=body, headers=self.headers(traceparent=traceparent, bearer=bearer), timeout_s=timeout_s
            )
        except (TransportError, TimeoutError):
            pass  # best effort; the agent's task TTL reclaims it
