"""Regression suite for the conversation of 2026-09-27 and the fixes it led to.

In that conversation the assistant sold 50 Novartis shares when asked to sell
the Emerging Markets ETF, answered "How many portfolios do I have?", "Stop"
and a weather question with branch opening hours, asked "Could you tell me a
bit more?" twice for an advice question and then handed over, and sent "Stop"
to the orchestrator as a request.

Layers, fastest first: slot extraction and instrument matching, routing,
catalogue validation, orchestrator turns against a fake gateway, the whole
conversation against the fake core bank and backend agents, and the master
agent's control commands against a fake LiveKit session.
"""

import asyncio
import importlib.util
import json
import os
import unittest
from types import SimpleNamespace

from helpers import ROOT, FakeGateway, dev_config, make_service, open_session, task_result

import httpx

from domain_agents.backend_agents import DEV_SUBJECT, BankTools, build_backend_agents
from master_agent.commands import Command, ConversationState, classify, react
from master_agent.config import BehaviourConfig
from mock_backend.app import create_app as create_bank_app
from mock_backend.bank import MockBank
from orchestrator.catalogue import catalogue_from_source, load_catalogue
from orchestrator.config import ConfigError, RoutingConfig
from orchestrator.models import ResponseType, TurnRequest
from orchestrator.router import Router
from orchestrator.service import action_mismatch
from orchestrator.slots import extract_instrument, extract_quantity, extract_slots, match_instruments, units_for
from orchestrator.transport import FakeTransport, HttpResponse

ACR = ["low", "standard", "stepup"]
CATALOGUE = load_catalogue(os.path.join(ROOT, "config", "intents.yaml"), os.path.join(ROOT, "config", "agents.yaml"), ACR)
SELL_SLOTS = CATALOGUE.intents["trade.sell"].slots
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"


def turn(text, turn_id, session_id="s-1"):
    return TurnRequest(session_id=session_id, turn_id=turn_id, text=text)


def sent_data(call):
    return call["body"]["params"]["message"]["parts"][-1]["data"]


# ---------------------------------------------------------------- slots


class SlotExtractionTests(unittest.TestCase):
    def test_quantities(self):
        cases = {
            "Sell 50 units of my tech ETF": {"units": 50},
            "please sell 10 shares of Nestle": {"units": 10},
            "sell half of my SMI tracker fund": {"fraction": 0.5},
            "sell a quarter of my gold": {"fraction": 0.25},
            "sell 25 percent of my Roche": {"fraction": 0.25},
            "sell 10% of my bonds": {"fraction": 0.1},
            "sell all my Roche": {"fraction": 1.0},
            "sell everything in the tech ETF": {"fraction": 1.0},
            "sell five units of Novartis": {"units": 5},
            "Sell Emerging Markets ETF.": None,
        }
        for text, expected in cases.items():
            self.assertEqual(extract_quantity(text), expected, text)

    def test_a_bare_number_counts_only_when_asked_how_much(self):
        self.assertIsNone(extract_quantity("20"))
        self.assertEqual(extract_quantity("20", bare=True), {"units": 20})
        self.assertEqual(extract_quantity("all of it please", bare=True), {"fraction": 1.0})

    def test_instruments(self):
        cases = {
            "Sell Emerging Markets ETF.": "Emerging Markets ETF",
            "sell half of my SMI tracker fund": "SMI tracker fund",
            "Sell 50 units of my tech ETF": "tech ETF",
            "please sell 10 shares of Nestle": "Nestle",
            "sell all my Roche from my portfolio": "Roche",
            "can you sell a quarter of the gold ETC now": "gold ETC",
            "Actually, sell the tech ETF instead": "tech ETF",
        }
        for text, expected in cases.items():
            self.assertEqual(extract_instrument(text), {"query": expected}, text)

    def test_no_instrument_named(self):
        for text in ("I want to sell", "sell my position in my holdings", "sell some", "sell half"):
            self.assertIsNone(extract_instrument(text), text)

    def test_follow_up_answers(self):
        self.assertEqual(extract_slots(SELL_SLOTS, "the Novartis one", asking="instrument"),
                         {"instrument": {"query": "Novartis"}})
        self.assertEqual(extract_slots(SELL_SLOTS, "half", asking="quantity"), {"quantity": {"fraction": 0.5}})
        self.assertEqual(extract_slots(SELL_SLOTS, "the tech ETF", asking="instrument"), {"instrument": {"query": "tech ETF"}})

    def test_units_for(self):
        self.assertEqual(units_for({"fraction": 0.5}, 101), 50)
        self.assertEqual(units_for({"fraction": 1.0}, 37), 37)
        self.assertEqual(units_for({"fraction": 0.1}, 3), 1)  # never zero
        self.assertEqual(units_for({"units": 12}, 5), 12)  # the agent refuses more than held


class InstrumentMatchingTests(unittest.TestCase):
    POSITIONS = [
        {"instrument": "Novartis registered share", "instrument_id": "NOVN"},
        {"instrument": "SMI Tracker Fund", "instrument_id": "SMI-TRK"},
        {"instrument": "Tech ETF", "instrument_id": "TECH-ETF"},
        {"instrument": "Global Equity Fund", "instrument_id": "GLOB-EQ"},
    ]

    def names(self, query):
        return [p["instrument"] for p in match_instruments(query, self.POSITIONS)]

    def test_the_named_holding_and_nothing_else(self):
        self.assertEqual(self.names("Emerging Markets ETF"), [])  # the incident: never the largest position instead
        self.assertEqual(self.names("SMI tracker fund"), ["SMI Tracker Fund"])
        self.assertEqual(self.names("novartis"), ["Novartis registered share"])
        self.assertEqual(self.names("tech ETFs"), ["Tech ETF"])
        self.assertEqual(self.names("smi-trk"), ["SMI Tracker Fund"])

    def test_generic_words_are_ambiguous(self):
        self.assertEqual(self.names("fund"), ["SMI Tracker Fund", "Global Equity Fund"])

    def test_prepared_action_must_match_the_request(self):
        intent = CATALOGUE.intents["trade.sell"]
        asked = {"instrument": {"query": "Emerging Markets ETF"}, "quantity": {"units": 50}}
        novartis = {"instrument": "Novartis registered share", "instrument_id": "NOVN", "quantity": 50}
        self.assertTrue(action_mismatch(intent, asked, novartis))
        self.assertTrue(action_mismatch(intent, {**asked, "instrument": {"query": "Novartis"}}, {**novartis, "quantity": 49}))
        self.assertEqual(action_mismatch(intent, {**asked, "instrument": {"query": "Novartis"}}, novartis), [])


# ---------------------------------------------------------------- routing


class RoutingRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.router = Router(CATALOGUE, RoutingConfig(fallback_intent="faq.general"))

    async def route(self, text):
        return await self.router.route(text)

    async def test_transcript_utterances(self):
        expected = {
            "How many portfolios do I have?": "portfolio.overview",
            "How is My Portfolio doing?": "portfolio.overview",
            "Update My Portfolio.": "portfolio.overview",
            "How are my investments?": "portfolio.overview",
            "Sell Emerging Markets ETF.": "trade.sell",
            "sell novartis": "trade.sell",
            "Should I rebalance and sell half of my SMI tracker fund?": "advice.rebalance",
            "Should I sell my Novartis shares?": "advice.rebalance",
        }
        for text, intent in expected.items():
            decision = await self.route(text)
            self.assertFalse(decision.needs_clarification, text)
            self.assertEqual(decision.intent.id if decision.intent else None, intent, text)

    async def test_questions_about_selling_are_never_orders(self):
        for text in ("Should I sell my tech ETF?", "Would you recommend I sell the gold?", "How can I sell shares in e-banking?",
                     "How do I sell my old car to a dealer?", "Is it a good idea to sell my Roche?"):
            decision = await self.route(text)
            self.assertNotEqual(decision.intent.id if decision.intent else None, "trade.sell", text)
            self.assertFalse(any(i.id == "trade.sell" for _, i in decision.segments), text)

    async def test_compound_request_is_split_reads_first(self):
        decision = await self.route("How is My Portfolio and sell half of my SMI tracker fund?")
        self.assertEqual([(t, i.id) for t, i in decision.segments],
                         [("How is My Portfolio", "portfolio.overview"), ("sell half of my SMI tracker fund", "trade.sell")])
        decision = await self.route("Sell 10 units of my tech ETF and show me my holdings")
        self.assertEqual([i.id for _, i in decision.segments], ["portfolio.overview", "trade.sell"])

    async def test_two_risky_requests_are_not_combined(self):
        decision = await self.route("Rebalance my portfolio and sell my tech ETF")
        self.assertEqual(decision.segments, ())
        self.assertTrue(decision.needs_clarification)

    async def test_ambiguity_names_the_candidates(self):
        decision = await self.route("sell my position in my holdings")
        self.assertTrue(decision.needs_clarification)
        self.assertEqual(set(decision.candidates), {"trade.sell", "portfolio.overview"})


class CatalogueValidationTests(unittest.TestCase):
    def source(self, **changes):
        import copy
        source = copy.deepcopy(CATALOGUE.source)
        for intent in source["intents"]["intents"]:
            if intent["id"] in changes:
                changes[intent["id"]](intent)
        return source

    def test_user_words_only_go_to_public_information_steps(self):
        def leak(intent):
            intent["steps"][0]["include_query"] = True
        with self.assertRaises(ConfigError):
            catalogue_from_source(self.source(**{"portfolio.overview": leak}), ACR)

    def test_required_slot_needs_a_prompt_and_a_known_kind(self):
        def no_prompt(intent):
            intent["slots"][0]["prompt"] = ""
        def bad_kind(intent):
            intent["slots"][0]["kind"] = "iban"
        for change in (no_prompt, bad_kind):
            with self.assertRaises(ConfigError):
                catalogue_from_source(self.source(**{"trade.sell": change}), ACR)

    def test_exclude_patterns_must_compile(self):
        def broken(intent):
            intent["exclude_patterns"] = ["(unclosed"]
        with self.assertRaises(ConfigError):
            catalogue_from_source(self.source(**{"trade.sell": broken}), ACR)


# ---------------------------------------------------------------- orchestrator turns (fake gateway)


class OrchestratorTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config = dev_config()
        config.workflows.enabled = True
        self.gw = FakeGateway()
        self.service, _, self.wf = make_service(config, gateway=self.gw)
        await open_session(self.service)
        self.n = 0

    async def say(self, text):
        self.n += 1
        return await self.service.handle_turn(turn(text, f"t{self.n}"))

    def calls(self, skill):
        return [c for c in self.gw.calls if c["skill"] == skill]

    async def events(self):
        return [r.event for r in await self.service.c.audit.chain("s-1")]

    async def test_wrong_instrument_is_never_offered_for_approval(self):
        # The fake trade agent always prepares 50 Tech ETF, like the agent that sold Novartis.
        r = await self.say("Sell 50 units of my Nestle shares")
        self.assertEqual(r.type, ResponseType.REFUSED, r.text)
        self.assertEqual(r.reasons, ["action_mismatch"])
        self.assertIsNone(r.approval)
        self.assertEqual(self.wf.started, {})
        self.assertIn("action_mismatch", await self.events())

    async def test_wrong_quantity_is_never_offered_for_approval(self):
        r = await self.say("Sell 10 units of my tech ETF")
        self.assertEqual((r.type, r.reasons), (ResponseType.REFUSED, ["action_mismatch"]))
        self.assertEqual(self.wf.started, {})

    async def test_trade_agent_receives_the_slots(self):
        r = await self.say("Sell 50 units of my tech ETF")
        self.assertEqual(r.type, ResponseType.APPROVAL_REQUIRED, r.reasons)
        data = sent_data(self.calls("trade.prepare")[0])
        self.assertEqual(data["slots"], {"instrument": {"query": "tech ETF"}, "quantity": {"units": 50}})
        self.assertNotIn("query", data)  # the user's words themselves never reach a non-public step

    async def test_missing_slots_are_asked_before_any_agent_runs(self):
        r = await self.say("Sell Emerging Markets ETF.")
        self.assertEqual(r.type, ResponseType.CLARIFY)
        self.assertIn("How much of the Emerging Markets ETF", r.text)
        self.assertEqual(self.gw.calls, [])
        await open_session(self.service, session_id="s-2")
        r = await self.service.handle_turn(turn("I want to sell", "t1", "s-2"))
        self.assertIn("Which of your holdings", r.text)
        self.assertEqual(self.gw.calls, [])

    async def test_slot_filling_across_turns(self):
        self.assertEqual((await self.say("Sell my tech ETF")).type, ResponseType.CLARIFY)
        r = await self.say("50")
        self.assertEqual(r.type, ResponseType.APPROVAL_REQUIRED, r.text)
        self.assertIn("50 units of Tech ETF", r.text)

    async def test_agent_question_is_asked_back_and_answered(self):
        not_held = task_result("I can't find Emerging Markets ETF in your portfolio. You hold the Tech ETF. "
                               "Which would you like to sell?",
                               ["core://positions/1"], data={"missing": "instrument"}, state=INPUT_REQUIRED,
                               classification="client_confidential")
        self.gw.replies["trade.prepare"] = lambda req: not_held
        r = await self.say("Sell half of my Emerging Markets ETF")
        self.assertEqual(r.type, ResponseType.CLARIFY)
        self.assertIn("can't find Emerging Markets ETF", r.text)
        self.assertEqual(self.wf.started, {})
        self.gw.replies["trade.prepare"] = lambda req: task_result("Prepared.", ["core://q/1"], data={"action": {
            "instrument": "Tech ETF", "quantity": 200, "account_mask": "****1", "estimated_amount": 1, "currency": "CHF"}})
        r = await self.say("the tech ETF then")
        self.assertEqual(r.type, ResponseType.APPROVAL_REQUIRED, r.text)
        # The quantity from the first turn is kept; only the holding was asked again.
        self.assertEqual(sent_data(self.calls("trade.prepare")[-1])["slots"],
                         {"quantity": {"fraction": 0.5}, "instrument": {"query": "tech ETF"}})

    async def test_moving_on_drops_the_open_question(self):
        await self.say("Sell my tech ETF")
        r = await self.say("How is my portfolio doing?")
        self.assertEqual((r.type, r.intent), (ResponseType.ANSWER, "portfolio.overview"))
        r = await self.say("50")
        self.assertNotEqual(r.type, ResponseType.APPROVAL_REQUIRED)

    async def test_never_mind_cancels_the_open_question(self):
        await self.say("Sell my tech ETF")
        r = await self.say("Never mind.")
        self.assertEqual(r.type, ResponseType.ANSWER)
        self.assertIn("cancelled", r.text)
        self.assertNotEqual((await self.say("50")).type, ResponseType.APPROVAL_REQUIRED)

    async def test_faq_gets_the_redacted_question(self):
        await self.service.close_session("s-1")
        await open_session(self.service, session_id="s-2", acr="low")
        r = await self.service.handle_turn(turn("What are the fees for paying CH93 0076 2011 6238 5295 7?", "t1", "s-2"))
        self.assertEqual(r.type, ResponseType.ANSWER, r.reasons)
        query = sent_data(self.calls("faq.answer")[0])["query"]
        self.assertIn("fees", query)
        self.assertNotIn("6238 5295", query)

    async def test_no_knowledge_base_answer_is_out_of_scope(self):
        self.gw.replies["faq.answer"] = lambda req: task_result("", [], state=INPUT_REQUIRED, classification="public")
        for text in ("What is the weather of Zurich today?", "Stop."):
            r = await self.say(text)
            self.assertEqual(r.type, ResponseType.CLARIFY, text)
            self.assertIn("can't help", r.text)
            self.assertNotIn("9:00", r.text)

    async def test_advice_question_is_answered_not_clarified(self):
        r = await self.say("Should I rebalance and sell half of my SMI tracker fund?")
        self.assertEqual((r.type, r.intent), (ResponseType.ANSWER, "advice.rebalance"))
        self.assertEqual(self.calls("trade.prepare"), [])

    async def test_compound_request_answers_both_parts(self):
        r = await self.say("How is my portfolio doing and sell 50 units of my tech ETF")
        self.assertEqual(r.type, ResponseType.APPROVAL_REQUIRED, r.reasons)
        self.assertIn("CHF 120,000", r.text)
        self.assertIn("50 units of Tech ETF", r.text)
        self.assertTrue(r.text.index("CHF 120,000") < r.text.index("50 units"))
        self.assertIsNotNone(r.approval)
        self.assertEqual(len(self.wf.started), 1)

    async def test_ambiguity_offers_named_options_and_accepts_the_choice(self):
        r = await self.say("sell my position in my holdings")
        self.assertEqual(r.type, ResponseType.CLARIFY)
        self.assertIn("to sell part of a holding or an overview of your portfolio", r.text)
        r = await self.say("the second one")
        self.assertEqual((r.type, r.intent), (ResponseType.ANSWER, "portfolio.overview"))

    async def test_repeating_the_same_request_says_so(self):
        await self.say("sell my position in my holdings")
        r = await self.say("sell my position in my holdings")
        self.assertEqual(r.type, ResponseType.CLARIFY)
        self.assertTrue(r.text.startswith("Sorry, I still need to know which you mean."), r.text)
        self.assertEqual((await self.say("sell my position in my holdings")).type, ResponseType.HANDOVER)


# ---------------------------------------------------------------- the conversation, end to end


class TranscriptReplayTests(unittest.IsolatedAsyncioTestCase):
    """The conversation again, against the fake core bank and the backend agents."""

    async def asyncSetUp(self):
        self.bank = MockBank()
        # A customer without an Emerging Markets ETF, like the one in the conversation.
        cid = next(cid for cid, c in sorted(self.bank.customers.items()) if "EM-ETF" not in c.positions and len(c.positions) >= 3)
        self.bank.assign(DEV_SUBJECT, cid)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_bank_app(self.bank)), base_url="http://bank")
        self.agents = build_backend_agents(BankTools("http://bank", client))

        async def handler(method, url, request):
            if url.endswith("/token"):
                return HttpResponse(200, {"access_token": "t", "expires_in": 300})
            name = url.rsplit("/", 1)[-1]
            headers = {k.lower(): v for k, v in request["headers"].items()}
            return HttpResponse(200, await self.agents[name].handle(request["json"], headers))

        config = dev_config()
        config.workflows.enabled = True
        gw = FakeGateway()
        gw.transport = FakeTransport(handler)
        self.service, _, self.wf = make_service(config, gateway=gw)
        await open_session(self.service)
        self.n = 0

    async def say(self, text):
        self.n += 1
        return await self.service.handle_turn(turn(text, f"t{self.n}"))

    async def test_the_conversation(self):
        pf = self.bank.portfolio(DEV_SUBJECT)
        total = f"CHF {pf['total_value_chf']:,}"
        target = pf["positions"][1]  # not the largest: the old agent always sold the largest

        r = await self.say("How many portfolios do I have?")
        self.assertEqual((r.type, r.intent), (ResponseType.ANSWER, "portfolio.overview"), r.text)
        self.assertIn(total, r.text)
        self.assertNotIn("open Monday", r.text)

        r = await self.say("Sell Emerging Markets ETF.")
        self.assertEqual(r.type, ResponseType.CLARIFY)
        self.assertIn("How much of the Emerging Markets ETF", r.text)

        r = await self.say("half")
        self.assertEqual(r.type, ResponseType.CLARIFY, r.text)
        self.assertIn("can't find Emerging Markets ETF", r.text)
        self.assertIn(target["instrument"], r.text)
        self.assertEqual(self.wf.started, {})

        r = await self.say(f"the {target['instrument']}")
        self.assertEqual(r.type, ResponseType.APPROVAL_REQUIRED, r.text)
        params = r.approval["action"]["params"]
        half = units_for({"fraction": 0.5}, target["units"])
        self.assertEqual((params["instrument_id"], params["quantity"]), (target["instrument_id"], half))
        await self.service.decide_approval(r.approval["approval_id"], approve=True, subject="u-1", acr="stepup",
                                           presented_action_hash=r.approval["action_hash"])
        token = self.wf.signals[r.approval["workflow_id"]][0]["approval_token"]
        result = await self.service.execute_approved_write(self.wf.started[r.approval["workflow_id"]], token)
        self.assertTrue(result["success"], result)
        self.assertEqual([(o["instrument_id"], o["quantity"]) for o in self.bank.orders],
                         [(target["instrument_id"], params["quantity"])])
        self.assertIn(target["instrument"], result["text"])  # the confirmation says what was sold

        for text in ("Stop.", "What is the weather of Zurich today?"):
            r = await self.say(text)
            self.assertEqual(r.type, ResponseType.CLARIFY, text)
            self.assertIn("can't help", r.text)
            self.assertNotIn("branches", r.text)

        r = await self.say("What are your opening hours?")
        self.assertIn("Monday to Friday", r.text)
        self.assertNotIn("0800", r.text)  # only the matching article, not every featured one
        r = await self.say("How much are the custody fees?")
        self.assertIn("0.25 percent", r.text)

        r = await self.say("Should I rebalance and sell half of my SMI tracker fund?")
        self.assertEqual((r.type, r.intent), (ResponseType.ANSWER, "advice.rebalance"), r.text)

        other = self.bank.portfolio(DEV_SUBJECT)["positions"][0]
        r = await self.say(f"How is My Portfolio and sell 1 unit of my {other['instrument']}?")
        self.assertEqual(r.type, ResponseType.APPROVAL_REQUIRED, r.text)
        self.assertIn("CHF", r.text)
        self.assertIn(f"sell 1 units of {other['instrument']}", r.text)

        r = await self.say("How is My Portfolio doing?")
        self.assertEqual((r.type, r.intent), (ResponseType.ANSWER, "portfolio.overview"))


class MockBankRegressionTests(unittest.TestCase):
    def test_quote_for_an_instrument_not_held_is_refused(self):
        bank = MockBank()
        cid = next(cid for cid, c in sorted(bank.customers.items()) if "EM-ETF" not in c.positions)
        bank.assign("u", cid)
        with self.assertRaises(ValueError):
            bank.quote_sell("u", "EM-ETF", 10)  # used to fall back to the largest position

    def test_knowledge_base_ignores_filler_words(self):
        bank = MockBank()
        self.assertEqual(bank.articles("What is the weather of Zurich today?"), [])
        self.assertEqual(bank.articles("Stop."), [])
        self.assertEqual([a["id"] for a in bank.articles("What are your opening hours?")], ["opening-hours"])


# ---------------------------------------------------------------- master agent: control commands


class CommandClassificationTests(unittest.TestCase):
    def test_commands(self):
        cases = {
            "Stop.": Command.STOP, "Shut up!": Command.STOP, "okay stop": Command.STOP, "please be quiet": Command.STOP,
            "stop talking please": Command.STOP, "That's enough.": Command.STOP, "hush": Command.STOP,
            "Cancel": Command.CANCEL, "never mind": Command.CANCEL, "forget it": Command.CANCEL, "don't do that": Command.CANCEL,
            "Say that again?": Command.REPEAT, "Sorry?": Command.REPEAT, "pardon": Command.REPEAT,
            "What did you say?": Command.REPEAT,
            "go on": Command.RESUME, "hold on": Command.WAIT, "one second": Command.WAIT,
            "Hello": Command.GREETING, "Grüezi": Command.GREETING, "thank you": Command.THANKS, "Thanks a lot!": Command.THANKS,
            "no thanks": Command.DONE, "that's all": Command.DONE, "Bye": Command.GOODBYE, "What can you do?": Command.HELP,
        }
        for text, command in cases.items():
            self.assertEqual(classify(text), command, text)

    def test_requests_are_not_commands(self):
        for text in ("Stop the order for my Nestle shares", "Update my portfolio", "Hi, how is my portfolio doing?",
                     "Sell half of my SMI tracker fund", "What is the weather of Zurich today?", "no", "sell", ""):
            self.assertIsNone(classify(text), text)

    def test_reactions(self):
        replies = BehaviourConfig().command_replies
        # "Stop" while talking: stop, drop what is in flight, say nothing.
        r = react(Command.STOP, ConversationState(speaking=True), replies)
        self.assertEqual((r.interrupt, r.drop_in_flight, r.say, r.forward), (True, True, None, False))
        self.assertEqual(react(Command.STOP, ConversationState(busy=True), replies).say, None)
        self.assertEqual(react(Command.STOP, ConversationState(), replies).say, "Okay.")
        # "Cancel" with an order awaiting confirmation asks the app to decline it.
        self.assertTrue(react(Command.CANCEL, ConversationState(approval_pending=True), replies).cancel_approval)
        # ... with an open question, the orchestrator drops it.
        self.assertTrue(react(Command.CANCEL, ConversationState(awaiting_answer=True), replies).forward)
        self.assertTrue(react(Command.DONE, ConversationState(awaiting_answer=True), replies).forward)
        said = react(Command.REPEAT, ConversationState(last_text="You hold 4 positions."), replies).say
        self.assertEqual(said, "You hold 4 positions.")
        said = react(Command.RESUME, ConversationState(last_text="You hold", interrupted=True), replies).say
        self.assertEqual(said, "You hold")
        for command in Command:
            react(command, ConversationState(), replies)  # every command has a reply configured


HAVE_LIVEKIT = importlib.util.find_spec("livekit.agents") is not None


@unittest.skipUnless(HAVE_LIVEKIT, "LiveKit Agents not installed")
class MasterAgentTurnTests(unittest.IsolatedAsyncioTestCase):
    """The master agent's turn handling with a fake LiveKit session and orchestrator client."""

    async def asyncSetUp(self):
        from master_agent import agent as agent_module
        from master_agent.config import MasterAgentConfig

        class Handle:
            def __init__(self, done):
                self.interrupted, self._done = False, done

            def done(self):
                return self._done

        class Session:
            def __init__(self):
                self.said, self.talking, self.current_speech, self.interrupts = [], False, None, 0

            def say(self, text, allow_interruptions=True, add_to_chat_ctx=True):
                self.said.append(text)
                self.current_speech = Handle(done=not self.talking)
                return self.current_speech

            def interrupt(self, force=False):
                self.interrupts += 1
                if self.current_speech is not None and not self.current_speech.done():
                    self.current_speech.interrupted, self.current_speech._done = True, True
                future = asyncio.get_running_loop().create_future()
                future.set_result(None)
                return future

        class Client:
            def __init__(self):
                self.turns, self.gate, self.reply, self.status = [], None, {"type": "answer", "text": "You hold 4 positions."}, {}

            async def turn(self, session_id, text, traceparent, channel, turn_id):
                self.turns.append(text)
                if self.gate is not None:
                    await self.gate.wait()
                return self.reply

            async def workflow_status(self, workflow_id, session_id):
                return self.status

        class Participant:
            def __init__(self):
                self.rpcs, self.decline = [], True

            async def perform_rpc(self, destination_identity, method, payload):
                self.rpcs.append((method, json.loads(payload)))
                return json.dumps({"declined": self.decline} if method.endswith("cancel") else {"shown": True})

        session = Session()

        class Probe(agent_module.MasterAgent):
            @property
            def session(self):
                return session

        cfg = MasterAgentConfig()
        cfg.behaviour.holding_after_ms = 5000
        cfg.behaviour.workflow_poll_interval_s = 0.01
        self.local = Participant()
        ctx = SimpleNamespace(room=SimpleNamespace(remote_participants={"app": SimpleNamespace(identity="app")},
                                                   local_participant=self.local))
        dispatch = SimpleNamespace(traceparent=None, session_id="s-1", channel="voice")
        self.client, self.session = Client(), session
        self.agent = Probe(ctx, dispatch, self.client, cfg)

    async def asyncTearDown(self):
        for task in list(self.agent._watchers):
            task.cancel()

    async def test_stop_is_not_sent_to_the_orchestrator(self):
        await self.agent.handle_turn("Stop.")
        self.assertEqual(self.client.turns, [])
        self.assertEqual(self.session.said, ["Okay."])

    async def test_shut_up_while_talking_stops_silently(self):
        self.session.talking = True
        await self.agent.handle_turn("How is my portfolio doing?")
        await self.agent.handle_turn("shut up")
        self.assertEqual(self.session.said, ["You hold 4 positions."])
        self.assertTrue(self.session.current_speech.interrupted)

    async def test_voice_barge_in_stop_stays_silent(self):
        await self.agent.handle_turn("How is my portfolio doing?")
        self.session.current_speech.interrupted = True  # LiveKit cut the answer off when the user started talking
        await self.agent.handle_turn("stop")
        self.assertEqual(self.session.said, ["You hold 4 positions."])

    async def test_stop_drops_the_answer_in_flight(self):
        self.client.gate = asyncio.Event()
        pending = asyncio.create_task(self.agent.handle_turn("Update my portfolio"))
        await asyncio.sleep(0)
        await self.agent.handle_turn("stop")
        self.client.gate.set()
        await pending
        self.assertEqual(self.client.turns, ["Update my portfolio"])
        self.assertEqual(self.session.said, [])

    async def test_repeat_and_resume(self):
        await self.agent.handle_turn("How is my portfolio doing?")
        await self.agent.handle_turn("say that again")
        self.assertEqual(self.session.said, ["You hold 4 positions."] * 2)
        self.assertEqual(len(self.client.turns), 1)

    async def test_small_talk_is_answered_locally(self):
        for text in ("thanks", "hello", "what can you do?", "bye"):
            await self.agent.handle_turn(text)
        self.assertEqual(self.client.turns, [])
        self.assertEqual(len(self.session.said), 4)

    async def test_never_mind_answers_an_open_question_via_the_orchestrator(self):
        self.client.reply = {"type": "clarify", "text": "How much of the tech ETF would you like to sell?"}
        await self.agent.handle_turn("sell my tech ETF")
        self.client.reply = {"type": "answer", "text": "Okay, I've cancelled that."}
        await self.agent.handle_turn("never mind")
        self.assertEqual(self.client.turns, ["sell my tech ETF", "never mind"])
        self.assertEqual(self.session.said[-1], "Okay, I've cancelled that.")

    async def test_cancel_declines_the_order_awaiting_confirmation(self):
        approval = {"approval_id": "ap-1", "workflow_id": "wf-1", "action_hash": "h", "action": {}, "expires_at": 0,
                    "required_acr": "stepup"}
        self.client.reply = {"type": "approval_required", "text": "You are about to sell 5 units of Tech ETF.",
                             "approval": approval}
        await self.agent.handle_turn("sell 5 units of my tech ETF")
        await self.agent.handle_turn("cancel")
        self.assertEqual(self.local.rpcs[-1], ("orchestrator.approval_cancel", {"approval_id": "ap-1"}))
        self.assertEqual(self.client.turns, ["sell 5 units of my tech ETF"])
        self.local.decline = False  # an app that cannot decline: say that nothing happens without confirmation
        await self.agent.handle_turn("cancel")
        self.assertIn("Nothing happens unless you confirm it", self.session.said[-1])

    async def test_order_confirmation_names_what_was_sold(self):
        approval = {"approval_id": "ap-1", "workflow_id": "wf-1", "action_hash": "h", "action": {}, "expires_at": 0,
                    "required_acr": "stepup"}
        self.client.reply = {"type": "approval_required", "text": "You are about to sell 5 units of Tech ETF.",
                             "approval": approval}
        self.client.status = {"status": "completed", "result": {
            "status": "completed", "text": "Your order to sell 5 units of the Tech ETF has been placed, reference ORD-1."}}
        await self.agent.handle_turn("sell 5 units of my tech ETF")
        await asyncio.gather(*self.agent._watchers)
        self.assertEqual(self.session.said[-1],
                         "Done. Your order to sell 5 units of the Tech ETF has been placed, reference ORD-1.")
        self.assertIsNone(self.agent._pending_approval)

    async def test_requests_still_go_to_the_orchestrator(self):
        await self.agent.handle_turn("Stop the order for my Nestle shares")
        self.assertEqual(self.client.turns, ["Stop the order for my Nestle shares"])


if __name__ == "__main__":
    unittest.main()
