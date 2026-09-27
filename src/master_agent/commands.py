"""Conversation control phrases the master agent handles itself.

"Stop", "shut up", "say that again" or "thanks" are about the conversation,
not requests for the bank: sending them to the orchestrator produced answers
like branch opening hours. They are recognised here with fixed phrase lists
(no model) and only when the whole utterance is the command, so "stop the
order for my Nestle shares" is still a request.

Framework-free so it can be unit-tested without LiveKit.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class Command(str, enum.Enum):
    STOP = "stop"          # stop talking now; stay quiet
    CANCEL = "cancel"      # drop what is in progress (a question, an order awaiting confirmation)
    REPEAT = "repeat"      # say the last answer again
    RESUME = "resume"      # carry on after being stopped
    WAIT = "wait"          # the user needs a moment
    GREETING = "greeting"
    THANKS = "thanks"
    DONE = "done"          # "that's all", "no thanks": nothing else needed
    GOODBYE = "goodbye"
    HELP = "help"          # what can you do?


# Longest utterance (in words, after fillers are removed) still treated as a command.
MAX_WORDS = 6

_FILLERS = frozenset({"ok", "okay", "oh", "um", "uh", "er", "hmm", "hey", "please", "just", "alright", "right", "so",
                      "now", "well", "actually", "sorry"})
_TRAILING = frozenset({"please", "now", "already", "then", "thanks"})

_PATTERNS: tuple[tuple[Command, str], ...] = (
    (Command.STOP, r"(stop|shut up|shut it|be quiet|quiet|silence|hush|enough|that's enough|thats enough|that is enough|"
                   r"pause|zip it|stop stop|stop talking|stop speaking|be silent|keep quiet|quiet please)"
                   r"( (it|talking|speaking|for a (second|moment|sec)))*"),
    (Command.CANCEL, r"(cancel|cancel (it|that|this|the order|the sale|my order|the trade)|never ?mind|forget (it|about it|that)|"
                     r"don't do (it|that)|do not do (it|that)|abort|scrap that|i changed my mind|i've changed my mind|"
                     r"don't sell|do not sell|don't|do not)"),
    (Command.REPEAT, r"(repeat|repeat (that|it|yourself|the answer)|say (that|it) again|could you say that again|come again|"
                     r"pardon|pardon me|sorry|what|what did you say|i didn't (hear|catch) (that|you|it)|once more|"
                     r"one more time|excuse me)"),
    (Command.RESUME, r"(go on|continue|carry on|keep going|you can continue|please continue|sorry go on)"),
    (Command.WAIT, r"(wait|hold on|hang on|one (moment|second|sec|minute)|just a (moment|second|sec|minute)|"
                   r"give me a (moment|second|sec|minute)|wait a (moment|second|sec|minute)|let me think)"),
    (Command.GREETING, r"(hi|hello|hey there|hi there|hello there|good (morning|afternoon|evening)|gruezi|grüezi|grüessech|"
                       r"hoi|salut|bonjour|guten tag)"),
    (Command.THANKS, r"(thanks|thank you|thanks a lot|thank you very much|many thanks|cheers|merci|danke|great|perfect|"
                     r"great thanks|perfect thanks|that's great|thats great|lovely)"),
    (Command.DONE, r"(no thanks|no thank you|that's all|thats all|that is all|nothing else|that's it|thats it|"
                   r"that'll be all|i'm done|im done|i am done|no that's all|nope)"),
    (Command.GOODBYE, r"(bye|goodbye|good bye|bye bye|see you|see you later|ciao|tschüss|tschau|adieu|have a nice day)"),
    (Command.HELP, r"(help|help me|what can you do|what can you help (me )?with|how can you help( me)?|what do you do|"
                   r"what are my options|options|menu)"),
)
_COMPILED = tuple((command, re.compile(rf"^{pattern}$")) for command, pattern in _PATTERNS)


def normalise(text: str) -> str:
    text = text.lower().replace("’", "'")
    return " ".join(re.sub(r"[^\w\s']", " ", text).split())


def classify(text: str) -> Command | None:
    """The control command this whole utterance expresses, or None for a normal request."""
    words = normalise(text).split()
    if not words or len(words) > MAX_WORDS + 3:
        return None
    # Try the utterance as said, then without leading fillers ("okay stop"), then without trailing ones ("stop now").
    variants = [words]
    while words and words[0] in _FILLERS:
        words = words[1:]
    variants.append(words)
    if len(words) > 2 and words[-2:] == ["thank", "you"]:
        words = words[:-2]
    while len(words) > 1 and words[-1] in _TRAILING:
        words = words[:-1]
    variants.append(words)
    for variant in variants:
        if not variant or len(variant) > MAX_WORDS:
            continue
        said = " ".join(variant)
        for command, pattern in _COMPILED:
            if pattern.match(said):
                return command
    return None


@dataclass
class ConversationState:
    speaking: bool = False          # the agent is talking right now
    busy: bool = False              # a request is with the orchestrator
    last_text: str = ""             # the last answer the agent spoke
    interrupted: bool = False       # the last answer was cut off
    awaiting_answer: bool = False   # the orchestrator asked the user a question
    approval_pending: bool = False  # an order is waiting for confirmation in the app


@dataclass
class Reaction:
    say: str | None = None
    interrupt: bool = False         # stop the current speech
    drop_in_flight: bool = False    # do not speak (or act on) the answer of a request still in flight
    forward: bool = False           # also send the words to the orchestrator (it asked a question they answer)
    cancel_approval: bool = False   # ask the app to decline the order awaiting confirmation


def react(command: Command, state: ConversationState, replies: dict[str, str]) -> Reaction:
    """What the agent does for a control command. ``replies`` are the configured phrases."""
    if command is Command.STOP:
        # Silence is the natural answer to "stop" or "shut up"; acknowledge only if there was nothing to stop.
        quiet = state.speaking or state.busy
        return Reaction(say=None if quiet else replies["stop"], interrupt=True, drop_in_flight=True)
    if command is Command.CANCEL:
        if state.approval_pending:
            return Reaction(interrupt=True, drop_in_flight=True, cancel_approval=True)
        if state.awaiting_answer:
            return Reaction(interrupt=True, drop_in_flight=True, forward=True)
        return Reaction(say=replies["cancel"], interrupt=True, drop_in_flight=True)
    if command is Command.REPEAT:
        return Reaction(say=state.last_text or replies["nothing_to_repeat"], interrupt=True)
    if command is Command.RESUME:
        if state.interrupted and state.last_text:
            return Reaction(say=state.last_text, interrupt=True)
        return Reaction(say=replies["resume"], interrupt=True)
    if command is Command.WAIT:
        return Reaction(say=replies["wait"], interrupt=True)
    if command is Command.DONE and state.awaiting_answer:
        # "No thanks" in answer to "which holding would you like to sell?" drops the request.
        return Reaction(interrupt=True, drop_in_flight=True, forward=True)
    return Reaction(say=replies[command.value], interrupt=state.speaking)
