import asyncio
import unittest

from helpers import dev_config, make_service, open_session

from domain_agents.kit import DomainAgent, SkillResult
from domain_agents import demo_agents as demo
from orchestrator.a2a import A2AClient
from orchestrator.dispatch import DispatchError, DispatchInfo, sign_dispatch, verify_dispatch
from orchestrator.models import ResponseType, TurnRequest
from orchestrator.tracing import new_trace, parse_traceparent
from orchestrator.transport import FakeTransport, HttpResponse
from token_service.logic import participant_grants, plan_session

KEY = b"d" * 32


class DispatchTests(unittest.TestCase):
    def test_roundtrip_and_tamper(self):
        raw = sign_dispatch(DispatchInfo("s1", "00-" + "a" * 32 + "-" + "b" * 16 + "-01", 1000), KEY)
        self.assertEqual(verify_dispatch(raw, KEY, now=1010).session_id, "s1")
        with self.assertRaises(DispatchError):
            verify_dispatch(raw, b"x" * 32, now=1010)
        with self.assertRaises(DispatchError):
            verify_dispatch(raw, KEY, now=5000)
        with self.assertRaises(DispatchError):
            verify_dispatch(raw.replace("s1", "s2"), KEY, now=1010)
        with self.assertRaises(DispatchError):
            verify_dispatch("not json", KEY)


class TokenServiceLogicTests(unittest.TestCase):
    def test_session_plan(self):
        a = plan_session(room_prefix="vs", dispatch_key=KEY, channel="voice")
        b = plan_session(room_prefix="vs", dispatch_key=KEY, channel="voice")
        self.assertNotEqual(a.session_id, b.session_id)
        self.assertTrue(a.room.startswith("vs-"))
        self.assertIsNotNone(parse_traceparent(a.traceparent))
        self.assertEqual(verify_dispatch(a.dispatch_metadata, KEY).session_id, a.session_id)

    def test_minimal_grants(self):
        g = participant_grants("vs-1")
        self.assertEqual(g["room"], "vs-1")
        self.assertFalse(g["room_admin"])
        self.assertFalse(g["room_record"])
        self.assertEqual(g["can_publish_sources"], ["microphone"])


class KitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = 0

        async def skill(req):
            self.calls += 1
            return SkillResult("ok", ["src://1"], "internal", {"n": self.calls})

        self.agent = DomainAgent("a", {"s.read": skill, "s.write": skill}, write_skills={"s.write"})
        self.client = A2AClient(dev_config().a2a, FakeTransport(lambda *a: None))

    def body(self, skill="s.read", **meta):
        return self.client.build_send_request(context_id="c", instruction="", data={"skill": skill}, metadata=meta, return_immediately=False)

    async def test_version_required(self):
        out = await self.agent.handle(self.body(), {})
        self.assertIn("error", out)

    async def test_idempotency(self):
        h = {"a2a-version": "1.0"}
        first = await self.agent.handle(self.body(idempotencyKey="k1"), h)
        second = await self.agent.handle(self.body(idempotencyKey="k1"), h)
        self.assertEqual(self.calls, 1)
        self.assertEqual(first["result"], second["result"])

    async def test_concurrent_duplicates_execute_once(self):
        h = {"a2a-version": "1.0"}

        async def slow(req):
            self.calls += 1
            await asyncio.sleep(0.05)
            return SkillResult("ok", ["s"])

        agent = DomainAgent("a", {"s.read": slow})
        await asyncio.gather(*(agent.handle(self.body(idempotencyKey="same"), h) for _ in range(5)))
        self.assertEqual(self.calls, 1)

    async def test_deadline_and_write_guard(self):
        h = {"a2a-version": "1.0"}
        late = await self.agent.handle(self.body(deadlineEpochMs=1), h)
        self.assertEqual(late["error"]["code"], -32010)
        unapproved = await self.agent.handle(self.body("s.write"), h)
        self.assertEqual(unapproved["error"]["code"], -32011)
        approved = await self.agent.handle(self.body("s.write", approvalToken="v1.x"), h)
        self.assertIn("result", approved)

    async def test_token_verifier(self):
        def verify(token):
            if token != "good":
                raise ValueError("bad")
            return {"sub": "u"}

        agent = DomainAgent("a", {"s.read": self.agent._skills["s.read"]}, token_verifier=verify)
        self.assertIn("error", await agent.handle(self.body(), {"a2a-version": "1.0"}))
        self.assertIn("error", await agent.handle(self.body(), {"a2a-version": "1.0", "authorization": "Bearer bad"}))
        self.assertIn("result", await agent.handle(self.body(), {"a2a-version": "1.0", "authorization": "Bearer good"}))


class ContractTests(unittest.IsolatedAsyncioTestCase):
    """The orchestrator talks to the demo agents through the real A2A client and the real kit."""

    async def test_full_stack_against_kit(self):
        agents = demo.build_agents()

        async def handler(method, url, request):
            name = url.rsplit("/", 1)[-1]
            headers = {k.lower(): v for k, v in request["headers"].items()}
            return HttpResponse(200, await agents[name].handle(request["json"], headers))

        config = dev_config()
        config.workflows.enabled = True

        from helpers import FakeGateway
        gw = FakeGateway()
        gw.transport = FakeTransport(handler)
        service, _, wf = make_service(config, gateway=gw)
        await open_session(service)
        tp = new_trace().traceparent
        overview = await service.handle_turn(TurnRequest("s-1", "t1", "How is my portfolio doing?", traceparent=tp))
        self.assertEqual(overview.type, ResponseType.ANSWER, overview.reasons)
        self.assertIn("CHF 120,000", overview.text)
        advice = await service.handle_turn(TurnRequest("s-1", "t2", "Should I rebalance?"))
        self.assertIn("risk profile", advice.text)
        tx = await service.handle_turn(TurnRequest("s-1", "t3", "Sell 50 units of my tech ETF"))
        self.assertEqual(tx.type, ResponseType.APPROVAL_REQUIRED)
        a = tx.approval
        await service.decide_approval(a["approval_id"], approve=True, subject="u-1", acr="stepup", presented_action_hash=a["action_hash"])
        token = wf.signals[a["workflow_id"]][0]["approval_token"]
        result = await service.execute_approved_write(wf.started[a["workflow_id"]], token)
        self.assertTrue(result["success"])
        again = await service.execute_approved_write(wf.started[a["workflow_id"]], token)
        self.assertEqual(result["references"], again["references"])  # idempotent retry, same order reference


if __name__ == "__main__":
    unittest.main()


class CrossServiceConfigTests(unittest.TestCase):
    def test_dispatch_age_covers_token_ttl(self):
        from master_agent.config import MasterAgentConfig
        from token_service.config import TokenServiceConfig
        self.assertGreaterEqual(MasterAgentConfig().dispatch_max_age_s, TokenServiceConfig().livekit.token_ttl_s)

    def test_shipped_service_configs_load(self):
        import os
        from helpers import ROOT
        from master_agent.config import load_master_config
        from orchestrator.config import load_config
        from token_service.config import load_token_service_config
        self.assertEqual(load_master_config(os.path.join(ROOT, "config", "master_agent.yaml")).stt.provider, "deepgram")
        self.assertEqual(load_token_service_config(os.path.join(ROOT, "config", "token_service.yaml")).livekit.agent_name, "master-agent")
        compose = load_config(os.path.join(ROOT, "config", "orchestrator.compose.yaml"), environ={})
        self.assertEqual(compose.policy.engine, "opa")
