"""Domain models shared by the orchestrator core.

These are plain dataclasses so the core has no web-framework dependency and is
easy to test. The API layer converts them to and from JSON.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from .slots import SlotSpec


class RiskClass(str, enum.Enum):
    R0 = "R0"  # public information
    R1 = "R1"  # personalised read
    R2 = "R2"  # advice
    R3 = "R3"  # transaction (write)

    @property
    def rank(self) -> int:
        return int(self.value[1])


class StepMode(str, enum.Enum):
    READ = "read"
    WRITE = "write"


class ResponseType(str, enum.Enum):
    ANSWER = "answer"
    CLARIFY = "clarify"
    APPROVAL_REQUIRED = "approval_required"
    HANDOVER = "handover"
    REFUSED = "refused"
    BUSY = "busy"


# A2A v1.0 task states (ProtoJSON enum names).
class TaskState(str, enum.Enum):
    SUBMITTED = "TASK_STATE_SUBMITTED"
    WORKING = "TASK_STATE_WORKING"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    REJECTED = "TASK_STATE_REJECTED"
    UNKNOWN = "TASK_STATE_UNSPECIFIED"

    @classmethod
    def parse(cls, raw: str | None) -> "TaskState":
        if not raw:
            return cls.UNKNOWN
        value = raw.upper()
        if not value.startswith("TASK_STATE_"):
            # Tolerate v0.3 style names ("completed", "input-required") from adapters.
            value = "TASK_STATE_" + value.replace("-", "_")
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN

    @property
    def terminal(self) -> bool:
        return self in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED, TaskState.REJECTED)


@dataclass(frozen=True)
class StepSpec:
    """One step of an intent's plan template (from intents.yaml)."""

    id: str
    agent: str
    skill: str
    mode: StepMode = StepMode.READ
    depends_on: tuple[str, ...] = ()
    optional: bool = False
    timeout_ms: int | None = None
    data_classes: tuple[str, ...] = ("internal",)
    cost_units: int = 1
    instruction: str = ""
    # R0 public steps only: receive the (PII-redacted) question as data.query, e.g. to search a knowledge base.
    include_query: bool = False


@dataclass(frozen=True)
class Intent:
    id: str
    risk: RiskClass
    description: str
    required_acr: str
    patterns: tuple[str, ...]
    steps: tuple[StepSpec, ...]
    quorum: str | None = None
    clarification_prompt: str = ""
    readback_template: str = ""
    # Short phrase for "did you mean A or B?" questions, e.g. "a rebalancing suggestion".
    label: str = ""
    # A request matching any of these never reaches this intent ("should I sell ..." is advice, not a trade).
    exclude_patterns: tuple[str, ...] = ()
    slots: tuple[SlotSpec, ...] = ()

    @property
    def has_writes(self) -> bool:
        return any(s.mode is StepMode.WRITE for s in self.steps)


@dataclass(frozen=True)
class AgentRecord:
    """A registered, certified domain agent."""

    name: str
    audience: str
    skills: tuple[str, ...]
    certified_in: tuple[str, ...]
    clearance: tuple[str, ...]
    writes_allowed: bool = False
    card_sha256: str = ""
    allowed_callees: tuple[str, ...] = ()
    cost_units: int = 1


@dataclass
class UserContext:
    subject: str  # pseudonymous user id
    acr: str
    tenant: str
    entitlements: tuple[str, ...] = ()
    channel: str = "voice"
    locale: str = "en-CH"


@dataclass
class TurnRequest:
    session_id: str
    turn_id: str
    text: str
    channel: str = "voice"
    traceparent: str | None = None


@dataclass
class Plan:
    intent: Intent
    steps: list[StepSpec]
    layers: list[list[StepSpec]]  # topologically ordered, each layer runs in parallel

    @property
    def cost_units(self) -> int:
        return sum(s.cost_units for s in self.steps)

    @property
    def write_steps(self) -> list[StepSpec]:
        return [s for s in self.steps if s.mode is StepMode.WRITE]


@dataclass
class Artifact:
    text: str
    sources: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    classification: str = "internal"


@dataclass
class StepResult:
    step_id: str
    agent: str
    state: TaskState
    artifacts: list[Artifact] = field(default_factory=list)
    task_id: str | None = None
    error: str | None = None
    latency_ms: float = 0.0
    attempts: int = 1
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.state is TaskState.COMPLETED


@dataclass
class ApprovalTicket:
    approval_id: str
    session_id: str
    workflow_id: str
    action_hash: str
    summary: str
    expires_at: float
    required_acr: str
    used: bool = False


@dataclass
class TurnResponse:
    type: ResponseType
    text: str
    session_id: str
    turn_id: str
    trace_id: str
    intent: str | None = None
    sources: list[str] = field(default_factory=list)
    approval: dict[str, Any] | None = None
    partial: bool = False
    reasons: list[str] = field(default_factory=list)
    # Observability details for the turn_completed event; never sent to the caller.
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "text": self.text,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "trace_id": self.trace_id,
            "intent": self.intent,
            "sources": self.sources,
            "approval": self.approval,
            "partial": self.partial,
        }
