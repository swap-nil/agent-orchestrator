"""Master agent: the LiveKit voice/chat front door.

The master agent owns the conversation (audio, turn-taking, interruptions,
holding phrases) and nothing else. Every user request goes to the orchestrator,
which routes, checks policy, calls domain agents and returns the text to say.
The master agent has no business logic, no tools and never sees user tokens.

Conversation control stays here: "stop", "shut up", "cancel", "say that
again", "hold on", greetings and thanks are recognised by fixed phrase lists
(``commands.py``) and handled without the orchestrator. "Stop" silences the
agent and drops the answer of a request still in flight; "cancel" while an
order awaits confirmation asks the app to decline it.

Transactions: when the orchestrator answers ``approval_required``, the agent
reads the action back, notifies the client app over LiveKit RPC so it can
show the confirmation screen, and then polls the workflow until it finishes.
The approval itself goes from the app to its backend with step-up
authentication, never through this agent.

Run: ``python -m master_agent.agent start`` (LiveKit Agents CLI).
Written against LiveKit Agents 1.x; verify hook names against the version you pin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from livekit.agents import Agent, AgentSession, JobContext, StopResponse, WorkerOptions, cli, room_io
from livekit.agents.voice import SpeechHandle
from livekit.agents.llm import ChatContext, ChatMessage

from orchestrator.dispatch import DispatchError, DispatchInfo, verify_dispatch
from orchestrator.tracing import continue_or_start, parse_traceparent

from . import providers
from .commands import Command, ConversationState, classify, react
from .config import MasterAgentConfig, load_master_config
from .orchestrator_client import OrchestratorClient, OrchestratorUnavailable

log = logging.getLogger("master_agent")
CONFIG: MasterAgentConfig = load_master_config()
providers.import_plugins(CONFIG.stt.provider, CONFIG.tts.provider, CONFIG.vad.provider,
                         *(["turn_detector.multilingual"] if CONFIG.turn_detection == "multilingual" else []))


class MasterAgent(Agent):
    def __init__(self, ctx: JobContext, dispatch: DispatchInfo, client: OrchestratorClient, config: MasterAgentConfig) -> None:
        # Instructions are unused for business answers (no LLM reply is generated), but keep
        # a strict persona in case an LLM is later configured for small talk.
        super().__init__(instructions="You relay answers from the bank's orchestrator. Never invent information.")
        self._ctx = ctx
        self._dispatch = dispatch
        self._client = client
        self._cfg = config
        self._root = parse_traceparent(dispatch.traceparent) or continue_or_start(None)
        self._watchers: set[asyncio.Task[None]] = set()
        # Bumped by "stop" and "cancel": answers to requests sent before that are not spoken.
        self._epoch = 0
        self._in_flight = 0
        self._last_text = ""
        self._interrupted = False
        self._awaiting_answer = False
        self._pending_approval: dict[str, Any] | None = None
        self._last_speech: SpeechHandle | None = None

    async def on_enter(self) -> None:
        await self.session.say(self._cfg.behaviour.greeting, allow_interruptions=True)

    async def on_user_turn_completed(self, turn_ctx: ChatContext, new_message: ChatMessage) -> None:
        await self.handle_turn(new_message.text_content or "")
        raise StopResponse()  # the orchestrator's answer is final; no LLM reply

    async def handle_turn(self, text: str) -> None:
        """Handle one user turn (spoken or typed): a control command here, anything else via the orchestrator."""
        text = text.strip()
        if not text:
            return
        command = classify(text) if self._cfg.behaviour.local_commands else None
        if command is not None and await self._handle_command(command, text):
            return
        await self._ask_orchestrator(text)

    def _speaking(self) -> bool:
        speech = self.session.current_speech
        return speech is not None and not speech.done()

    def _cut_off(self) -> bool:
        """The last answer was interrupted, e.g. by the user talking over it (voice barge-in)."""
        return self._last_speech is not None and self._last_speech.interrupted

    def _say(self, text: str, *, allow_interruptions: bool = True, remember: bool = True) -> None:
        handle = self.session.say(text, allow_interruptions=allow_interruptions)
        if remember:
            self._last_text, self._interrupted, self._last_speech = text, False, handle

    async def _interrupt(self) -> None:
        if self._speaking() or self._cut_off():
            self._interrupted = True
        try:
            await self.session.interrupt()  # non-interruptible speech (an order read-back) is left to finish
        except Exception:  # noqa: BLE001 - the session may be closing; nothing left to interrupt
            log.debug("interrupt failed", exc_info=True)

    async def _handle_command(self, command: Command, text: str) -> bool:
        """Act on a control command. False means it should go to the orchestrator after all."""
        b = self._cfg.behaviour
        state = ConversationState(
            # Talking over the agent has already stopped it by the time the words arrive: that counts as speaking.
            speaking=self._speaking() or self._cut_off(), busy=self._in_flight > 0, last_text=self._last_text,
            interrupted=self._interrupted,
            awaiting_answer=self._awaiting_answer, approval_pending=self._pending_approval is not None,
        )
        reaction = react(command, state, b.command_replies)
        log.info("control command", extra={"command": command.value, "session_id": self._dispatch.session_id})
        if reaction.interrupt:
            await self._interrupt()
        if reaction.drop_in_flight:
            self._epoch += 1
        if reaction.cancel_approval:
            if not await self._cancel_approval():
                self._say(b.command_replies["cancel_unconfirmed"])
        if reaction.say:
            # A repeated answer stays the one to repeat; a command's own reply does not replace it.
            self._say(reaction.say, remember=False)
        if reaction.forward:
            await self._ask_orchestrator(text)
        return True

    async def _ask_orchestrator(self, text: str) -> None:
        trace = self._root.child()
        epoch = self._epoch
        self._in_flight += 1
        call = asyncio.create_task(
            self._client.turn(self._dispatch.session_id, text, trace.traceparent, self._dispatch.channel, uuid.uuid4().hex)
        )
        b = self._cfg.behaviour
        try:
            done, _ = await asyncio.wait({call}, timeout=b.holding_after_ms / 1000)
            if not done:
                self.session.say(b.holding_phrase, allow_interruptions=True, add_to_chat_ctx=False)
            response = await call
        except OrchestratorUnavailable:
            log.warning("orchestrator unavailable", extra={"trace_id": trace.trace_id})
            self._say(b.unavailable)
            return
        finally:
            self._in_flight -= 1

        if epoch != self._epoch:
            # The user said "stop" or "cancel" while this was in flight. An order prepared meanwhile is not
            # offered for confirmation, so it expires without effect.
            log.info("answer dropped after stop", extra={"trace_id": trace.trace_id, "type": response.get("type")})
            return
        self._awaiting_answer = response.get("type") == "clarify"
        self._say(response.get("text") or b.unavailable, allow_interruptions=response.get("type") != "approval_required")
        if response.get("type") == "approval_required" and response.get("approval"):
            await self._request_approval(response["approval"])

    async def _cancel_approval(self) -> bool:
        """Ask the app to decline the order awaiting confirmation. True when the app says it did."""
        approval = self._pending_approval
        if approval is None:
            return False
        payload = json.dumps({"approval_id": approval["approval_id"]})
        declined = False
        for participant in self._ctx.room.remote_participants.values():
            try:
                reply = await self._ctx.room.local_participant.perform_rpc(
                    destination_identity=participant.identity, method=self._cfg.behaviour.approval_cancel_rpc_method,
                    payload=payload,
                )
                declined = declined or bool(json.loads(reply or "{}").get("declined"))
            except Exception:  # noqa: BLE001 - an app without the method just lets the approval expire
                log.warning("approval cancel RPC not delivered")
        return declined

    async def _request_approval(self, approval: dict[str, Any]) -> None:
        payload = json.dumps({
            "approval_id": approval["approval_id"],
            "action_hash": approval["action_hash"],
            "action": approval["action"],
            "expires_at": approval["expires_at"],
            "required_acr": approval["required_acr"],
        })
        for participant in self._ctx.room.remote_participants.values():
            try:
                await self._ctx.room.local_participant.perform_rpc(
                    destination_identity=participant.identity, method=self._cfg.behaviour.approval_rpc_method, payload=payload,
                )
            except Exception:  # noqa: BLE001 - app may be backgrounded; the push notification path covers it
                log.warning("approval RPC not delivered")
        self._pending_approval = approval
        task = asyncio.create_task(self._watch_workflow(approval["workflow_id"]))
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    async def _watch_workflow(self, workflow_id: str) -> None:
        try:
            await self._poll_workflow(workflow_id)
        finally:
            if self._pending_approval and self._pending_approval.get("workflow_id") == workflow_id:
                self._pending_approval = None

    async def _poll_workflow(self, workflow_id: str) -> None:
        b = self._cfg.behaviour
        loop = asyncio.get_running_loop()
        deadline = loop.time() + b.workflow_poll_timeout_s
        while loop.time() < deadline:
            await asyncio.sleep(b.workflow_poll_interval_s)
            try:
                status = await self._client.workflow_status(workflow_id, self._dispatch.session_id)
            except Exception:  # noqa: BLE001 - keep polling through transient errors
                continue
            if status.get("status") == "completed":
                result = status.get("result") or {}
                outcome = result.get("status", "completed")
                if outcome == "completed" and result.get("text"):
                    # The trade agent's confirmation names the instrument, units and order reference.
                    self._say(f"Done. {result['text']}")
                else:
                    self._say(b.outcome_messages.get(outcome, b.outcome_messages["failed"]))
                return
            if status.get("status") in ("failed", "terminated", "canceled", "timed_out"):
                self._say(b.outcome_messages["failed"])
                return


def _build_session(config: MasterAgentConfig) -> AgentSession:
    kwargs: dict[str, Any] = {
        "stt": providers.build("stt", providers.STT_FACTORIES, config.stt.provider, config.stt.options),
        "tts": providers.build("tts", providers.TTS_FACTORIES, config.tts.provider, config.tts.options),
        "vad": providers.build("vad", providers.VAD_FACTORIES, config.vad.provider, config.vad.options),
    }
    if config.turn_detection == "multilingual":
        from livekit.plugins.turn_detector.multilingual import MultilingualModel

        kwargs["turn_detection"] = MultilingualModel()
    return AgentSession(**kwargs)


async def entrypoint(ctx: JobContext) -> None:
    key = os.environ.get(CONFIG.dispatch_key_env, "").encode()
    if not key:
        raise RuntimeError(f"{CONFIG.dispatch_key_env} is not set")
    try:
        dispatch = verify_dispatch(ctx.job.metadata or "", key, CONFIG.dispatch_max_age_s)
    except DispatchError as exc:
        log.error("rejected dispatch: %s", exc)
        return  # do not join rooms the token service did not create
    await ctx.connect()
    client = OrchestratorClient(CONFIG.orchestrator)

    async def _shutdown() -> None:
        # End of the conversation: drop the session and its bound user token in the orchestrator.
        await client.close_session(dispatch.session_id)
        await client.aclose()

    ctx.add_shutdown_callback(_shutdown)
    session = _build_session(CONFIG)
    agent = MasterAgent(ctx, dispatch, client, CONFIG)

    async def _on_text(sess: AgentSession, ev: room_io.TextInputEvent) -> None:
        # Typed chat (lk.chat) skips on_user_turn_completed: the default callback calls
        # generate_reply, which fails without an LLM. Handle it like a spoken turn instead
        # (handle_turn interrupts current speech itself where the command calls for it).
        await agent.handle_turn(ev.text)

    await session.start(
        room=ctx.room, agent=agent,
        room_options=room_io.RoomOptions(text_input=room_io.TextInputOptions(text_input_cb=_on_text)),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # agent_name enables explicit dispatch: the worker only joins rooms that request it.
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=CONFIG.agent_name))
