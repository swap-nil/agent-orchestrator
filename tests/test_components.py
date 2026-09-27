import asyncio
import unittest

from helpers import APPROVAL_KEY, FakeGateway, dev_config, task_result

from orchestrator.a2a import A2AClient, A2AError, parse_send_result
from orchestrator.approvals import ApprovalService, ApprovalSigner, action_hash, verify_approval_token
from orchestrator.audit import AuditRecord, InMemoryAuditSink, verify_chain
from orchestrator.catalogue import load_catalogue
from orchestrator.config import CircuitBreakerConfig, IdentityConfig, KillSwitchConfig, PolicyConfig
from orchestrator.executor import ExecutionContext, Executor
from orchestrator.identity import TokenExchangeError, TokenExchanger, build_exchange_form
from orchestrator.models import TaskState, UserContext
from orchestrator.planner import Planner
from orchestrator.policy import (
    LocalPolicyEngine, OpaPolicyEngine, PolicyDecision, PolicyEnforcementPoint, build_policy_input,
)
from orchestrator.resilience import CircuitBreakers
from orchestrator.state import InMemorySessionStore, SessionState, TokenCipher
from orchestrator.tracing import new_trace, parse_traceparent
from orchestrator.transport import FakeTransport, HttpResponse, TransportError

LEVELS = ["low", "standard", "stepup"]


def catalogue():
    c = dev_config()
    return load_catalogue(c.catalogue.intents_file, c.catalogue.registry_file, LEVELS)


def policy_input(cat, intent_id="portfolio.overview", step_index=0, acr="standard", phase="plan", approval=False,
                 kill=None, environment="dev", channel="voice"):
    intent = cat.intents[intent_id]
    step = intent.steps[step_index]
    return build_policy_input(
        environment=environment, cell_id="c", user=UserContext("u", acr, "t", channel=channel), intent=intent, step=step,
        agent=cat.agent(step.agent), acr_levels=LEVELS, kill=kill or KillSwitchConfig(),
        allowed_channels=["voice", "chat"], phase=phase, approval_valid=approval,
    )


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_allows_valid_read(self):
        decision = await LocalPolicyEngine().decide(policy_input(catalogue()))
        self.assertTrue(decision.allow, decision.reasons)

    async def test_local_denials(self):
        cat = catalogue()
        engine = LocalPolicyEngine()
        self.assertFalse((await engine.decide(policy_input(cat, acr="low"))).allow)
        self.assertFalse((await engine.decide(policy_input(cat, environment="prod", intent_id="advice.rebalance", step_index=1))).allow)
        self.assertFalse((await engine.decide(policy_input(cat, kill=KillSwitchConfig(disabled_agents=["portfolio-agent"])))).allow)
        self.assertFalse((await engine.decide(policy_input(cat, channel="sms"))).allow)
        write = policy_input(cat, intent_id="trade.sell", step_index=2, phase="execute", approval=False)
        self.assertIn("write requires a valid approval", (await engine.decide(write)).reasons)
        write_ok = policy_input(cat, intent_id="trade.sell", step_index=2, phase="execute", approval=True)
        self.assertTrue((await engine.decide(write_ok)).allow)

    async def test_opa_fails_closed(self):
        cfg = PolicyConfig(engine="opa")

        def boom(method, url, req):
            raise TransportError("refused")

        for handler in (boom, lambda m, u, r: HttpResponse(500, {}), lambda m, u, r: HttpResponse(200, {"result": {}})):
            decision = await OpaPolicyEngine(cfg, FakeTransport(handler)).decide(policy_input(catalogue()))
            self.assertFalse(decision.allow)

    async def test_opa_allow_and_request_shape(self):
        transport = FakeTransport(lambda m, u, r: HttpResponse(200, {"result": {"allow": True, "reasons": []}}))
        decision = await OpaPolicyEngine(PolicyConfig(engine="opa"), transport).decide(policy_input(catalogue()))
        self.assertTrue(decision.allow)
        _, url, req = transport.requests[0]
        self.assertTrue(url.endswith("/v1/data/orchestrator/decision"))
        self.assertIn("input", req["json"])

    async def test_pep_caches_only_low_risk_allows(self):
        calls = []

        class Engine:
            async def decide(self, inp):
                calls.append(1)
                return PolicyDecision(True)

        pep = PolicyEnforcementPoint(Engine(), PolicyConfig())
        cat = catalogue()
        await pep.check(policy_input(cat))
        second = await pep.check(policy_input(cat))
        self.assertTrue(second.cached)
        self.assertEqual(len(calls), 1)
        await pep.check(policy_input(cat, intent_id="advice.rebalance"))
        await pep.check(policy_input(cat, intent_id="advice.rebalance"))
        self.assertEqual(len(calls), 3)

    async def test_pep_engine_exception_denies(self):
        class Engine:
            async def decide(self, inp):
                raise ValueError("bad")

        decision = await PolicyEnforcementPoint(Engine(), PolicyConfig()).check(policy_input(catalogue()))
        self.assertFalse(decision.allow)


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    def test_forms(self):
        rfc = build_exchange_form(IdentityConfig(mode="rfc8693", client_id="c"), {}, "tok", "api://a", "s.read")
        self.assertEqual(rfc["grant_type"], "urn:ietf:params:oauth:grant-type:token-exchange")
        self.assertEqual(rfc["audience"], "api://a")
        self.assertNotIn("client_secret", rfc)
        obo = build_exchange_form(IdentityConfig(mode="entra_obo", client_id="c"), {"client_secret": "sec"}, "tok", "api://a", ".default")
        self.assertEqual(obo["requested_token_use"], "on_behalf_of")
        self.assertEqual(obo["scope"], "api://a/.default")
        self.assertEqual(obo["client_secret"], "sec")

    async def test_workload_identity_assertion(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("federated.jwt\n")
        transport = FakeTransport(lambda m, u, r: HttpResponse(200, {"access_token": "x", "expires_in": 60}))
        cfg = IdentityConfig(mode="entra_obo", token_endpoint="https://idp/token", client_auth="workload_identity")
        ex = TokenExchanger(cfg, transport, environ={"AZURE_FEDERATED_TOKEN_FILE": f.name})
        await ex.token_for("s", "user", "api://a", ".default")
        form = transport.requests[0][2]["form"]
        self.assertEqual(form["client_assertion"], "federated.jwt")
        self.assertNotIn("client_secret", form)
        missing = TokenExchanger(cfg, transport, environ={})
        with self.assertRaises(TokenExchangeError):
            await missing.token_for("s2", "user", "api://a", ".default")

    async def test_managed_identity_assertion(self):
        imds = []

        async def get(url, params):
            imds.append((url, params))
            return 200, {"access_token": "mi.jwt", "expires_in": "86399"}

        transport = FakeTransport(lambda m, u, r: HttpResponse(200, {"access_token": "x", "expires_in": 60}))
        cfg = IdentityConfig(mode="entra_obo", token_endpoint="https://idp/token", client_auth="managed_identity")
        ex = TokenExchanger(cfg, transport, environ={"AZURE_CLIENT_ID": "mi-orch"}, imds_get=get)
        await ex.token_for("s", "user", "api://a", ".default")
        await ex.token_for("s", "user", "api://b", ".default")
        form = transport.requests[0][2]["form"]
        self.assertEqual(form["client_assertion"], "mi.jwt")
        self.assertEqual(imds[0][1]["resource"], "api://AzureADTokenExchange")
        self.assertEqual(imds[0][1]["client_id"], "mi-orch")
        self.assertEqual(len(imds), 1)  # the assertion is cached across exchanges
        with self.assertRaisesRegex(TokenExchangeError, "client id"):
            await TokenExchanger(cfg, transport, environ={}, imds_get=get).token_for("s", "user", "api://a", ".default")

        async def refused(url, params):
            return 400, {"error": "invalid_request", "error_description": "Identity not found"}
        bad = TokenExchanger(cfg, transport, environ={"AZURE_CLIENT_ID": "x"}, imds_get=refused)
        with self.assertRaisesRegex(TokenExchangeError, "Identity not found"):
            await bad.token_for("s", "user", "api://a", ".default")

    async def test_cache_and_errors(self):
        now = [1000.0]
        transport = FakeTransport(lambda m, u, r: HttpResponse(200, {"access_token": "x", "expires_in": 60}))
        cfg = IdentityConfig(mode="rfc8693", token_endpoint="https://idp/token", refresh_skew_s=10, client_auth="secret")
        ex = TokenExchanger(cfg, transport, clock=lambda: now[0])
        await ex.token_for("s", "user", "api://a", "read")
        await ex.token_for("s", "user", "api://a", "read")
        self.assertEqual(len(transport.requests), 1)
        now[0] += 55  # within skew of expiry -> refresh
        await ex.token_for("s", "user", "api://a", "read")
        self.assertEqual(len(transport.requests), 2)
        with self.assertRaises(TokenExchangeError):
            await ex.token_for("s", "", "api://a", "read")
        bad = TokenExchanger(cfg, FakeTransport(lambda m, u, r: HttpResponse(400, {"error": "invalid_grant"})), "sec")
        with self.assertRaisesRegex(TokenExchangeError, "invalid_grant"):
            await bad.token_for("s", "user", "api://b", "read")


class A2ATests(unittest.IsolatedAsyncioTestCase):
    def test_request_shape(self):
        client = A2AClient(dev_config().a2a, FakeTransport(lambda *a: None))
        body = client.build_send_request(context_id="s", instruction="do", data={"k": 1}, metadata={"m": 1}, return_immediately=False)
        self.assertEqual(body["method"], "SendMessage")
        msg = body["params"]["message"]
        self.assertEqual(msg["role"], "ROLE_USER")
        self.assertEqual(msg["contextId"], "s")
        self.assertEqual(msg["parts"], [{"text": "do"}, {"data": {"k": 1}}])
        headers = client.headers(traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01", bearer="t")
        self.assertEqual(headers["A2A-Version"], "1.0")
        self.assertEqual(headers["Authorization"], "Bearer t")

    def test_parse_task_and_message(self):
        parsed = parse_send_result(task_result("hello", ["src"]))
        self.assertEqual(parsed.state, TaskState.COMPLETED)
        self.assertEqual(parsed.artifacts[0].sources, ["src"])
        msg = parse_send_result({"message": {"parts": [{"text": "hi"}], "metadata": {"sources": ["x"]}}})
        self.assertEqual(msg.artifacts[0].text, "hi")
        self.assertEqual(TaskState.parse("input-required"), TaskState.INPUT_REQUIRED)
        with self.assertRaises(A2AError):
            parse_send_result("nope")

    async def test_error_mapping(self):
        for status, retryable in ((503, True), (403, False)):
            client = A2AClient(dev_config().a2a, FakeTransport(lambda m, u, r, s=status: HttpResponse(s, {})))
            with self.assertRaises(A2AError) as ctx:
                await client.send("a", context_id="s", instruction="", data={}, metadata={}, traceparent=new_trace().traceparent, bearer=None, timeout_s=1)
            self.assertEqual(ctx.exception.retryable, retryable)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def _executor(self, gateway, cat, breaker_threshold=5):
        config = dev_config()
        tokens = TokenExchanger(config.identity, gateway.transport)
        from orchestrator.a2a import A2AClient as C
        return Executor(config.execution, cat, C(config.a2a, gateway.transport), tokens,
                        CircuitBreakers(CircuitBreakerConfig(failure_threshold=breaker_threshold, reset_after_s=60)))

    def _ctx(self, deadline_s=2.0):
        return ExecutionContext("s", "t", "tenant", "tok", new_trace(), asyncio.get_running_loop().time() + deadline_s)

    async def _allow(self, step):
        return PolicyDecision(True)

    async def test_fan_out_and_trace_propagation(self):
        cat = catalogue()
        gw = FakeGateway()
        plan = Planner(cat, dev_config().budgets, LEVELS, "dev").build(cat.intents["portfolio.overview"])
        outcome = await self._executor(gw, cat).run(plan, self._ctx(), self._allow)
        self.assertTrue(outcome.success)
        self.assertEqual({c["skill"] for c in gw.calls}, {"portfolio.holdings", "market.quotes"})
        tp = gw.calls[0]["headers"]["traceparent"]
        self.assertIsNotNone(parse_traceparent(tp))
        self.assertEqual(gw.calls[0]["body"]["params"]["message"]["metadata"]["traceparent"], tp)

    async def test_retry_on_retryable_read(self):
        attempts = []

        def flaky(req):
            attempts.append(1)
            return HttpResponse(503, {}) if len(attempts) < 2 else task_result("ok", ["s"])

        cat = catalogue()
        gw = FakeGateway({"faq.answer": flaky})
        plan = Planner(cat, dev_config().budgets, LEVELS, "dev").build(cat.intents["faq.general"])
        outcome = await self._executor(gw, cat).run(plan, self._ctx(), self._allow)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.results["answer"].attempts, 2)

    async def test_optional_failure_is_partial_and_dependency_skipped(self):
        cat = catalogue()
        gw = FakeGateway({"market.quotes": lambda r: HttpResponse(403, {})})
        plan = Planner(cat, dev_config().budgets, LEVELS, "dev").build(cat.intents["portfolio.overview"])
        outcome = await self._executor(gw, cat).run(plan, self._ctx(), self._allow)
        self.assertTrue(outcome.success)
        self.assertTrue(outcome.partial)

        gw2 = FakeGateway({"portfolio.holdings": lambda r: HttpResponse(403, {})})
        plan2 = Planner(cat, dev_config().budgets, LEVELS, "dev").build(cat.intents["advice.rebalance"])
        outcome2 = await self._executor(gw2, cat).run(plan2, self._ctx(), self._allow)
        self.assertFalse(outcome2.success)
        self.assertTrue(outcome2.results["proposal"].skipped)
        self.assertNotIn("advice.rebalance", [c["skill"] for c in gw2.calls])

    async def test_deadline_enforced(self):
        cat = catalogue()
        gw = FakeGateway(delay_s=0.5)
        plan = Planner(cat, dev_config().budgets, LEVELS, "dev").build(cat.intents["faq.general"])
        outcome = await self._executor(gw, cat).run(plan, self._ctx(deadline_s=0.2), self._allow)
        self.assertFalse(outcome.success)

    async def test_circuit_breaker_opens(self):
        cat = catalogue()
        gw = FakeGateway({"faq.answer": lambda r: HttpResponse(403, {})})
        ex = self._executor(gw, cat, breaker_threshold=2)
        plan = Planner(cat, dev_config().budgets, LEVELS, "dev").build(cat.intents["faq.general"])
        for _ in range(3):
            await ex.run(plan, self._ctx(), self._allow)
        self.assertEqual(len(gw.calls), 2)

    async def test_policy_denial_skips_call(self):
        cat = catalogue()
        gw = FakeGateway()
        plan = Planner(cat, dev_config().budgets, LEVELS, "dev").build(cat.intents["faq.general"])

        async def deny(step):
            return PolicyDecision(False, ["no"])

        outcome = await self._executor(gw, cat).run(plan, self._ctx(), deny)
        self.assertEqual(outcome.results["answer"].state, TaskState.REJECTED)
        self.assertEqual(gw.calls, [])


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_chain_verifies_and_detects_tamper(self):
        sink = InMemoryAuditSink()
        for i in range(4):
            await sink.append(AuditRecord(chain_id="s", event="e", data={"i": i}))
        chain = await sink.chain("s")
        self.assertEqual(verify_chain(chain), (True, None))
        chain[2].data["i"] = 99
        self.assertEqual(verify_chain(chain), (False, 2))

    async def test_concurrent_appends_stay_ordered(self):
        sink = InMemoryAuditSink()
        await asyncio.gather(*(sink.append(AuditRecord(chain_id="s", event="e", data={"i": i})) for i in range(50)))
        self.assertTrue(verify_chain(await sink.chain("s"))[0])


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = [1000.0]
        self.store = InMemorySessionStore(clock=lambda: 0)  # sessions never expire in this test
        await self.store.put(SessionState("s", "u", "t", "standard"))
        self.signer = ApprovalSigner(APPROVAL_KEY.encode())
        self.service = ApprovalService(self.store, self.signer, LEVELS, "stepup", 600, clock=lambda: self.now[0])
        self.action = {"intent": "trade.sell", "params": {"quantity": 5}}
        self.ticket = await self.service.create("s", "wf", self.action, "sell")

    async def test_happy_path_once(self):
        ok = await self.service.decide(self.ticket.approval_id, approve=True, subject="u", acr="stepup", presented_action_hash=action_hash(self.action))
        self.assertTrue(ok.approved)
        self.assertTrue(verify_approval_token(self.signer.public_key, ok.token, self.ticket.action_hash, now=1001))
        self.assertTrue(verify_approval_token(self.signer.public_key_pem(), ok.token, self.ticket.action_hash, now=1001))
        again = await self.service.decide(self.ticket.approval_id, approve=True, subject="u", acr="stepup", presented_action_hash=action_hash(self.action))
        self.assertFalse(again.approved)

    async def test_rejections(self):
        h = action_hash(self.action)
        cases = [
            dict(subject="other", acr="stepup", presented_action_hash=h),
            dict(subject="u", acr="standard", presented_action_hash=h),
            dict(subject="u", acr="stepup", presented_action_hash=action_hash({"params": {"quantity": 500}})),
        ]
        for case in cases:
            self.assertFalse((await self.service.decide(self.ticket.approval_id, approve=True, **case)).approved)
        self.now[0] += 601
        self.assertIn("approval expired", (await self.service.decide(self.ticket.approval_id, approve=True, subject="u", acr="stepup", presented_action_hash=h)).reasons)

    def test_token_bound_to_hash_and_key(self):
        token = self.signer.issue("a", "h1", 2000)
        pub = self.signer.public_key
        self.assertTrue(verify_approval_token(pub, token, "h1", now=1000))
        self.assertFalse(verify_approval_token(pub, token, "h2", now=1000))
        self.assertFalse(verify_approval_token(pub, token, "h1", now=3000))
        other = ApprovalSigner(b"x" * 48)
        self.assertFalse(verify_approval_token(other.public_key, token, "h1", now=1000))
        forged = other.issue("a", "h1", 2000)
        self.assertFalse(verify_approval_token(pub, forged, "h1", now=1000))
        self.assertFalse(verify_approval_token(pub, "garbage", "h1", now=1000))

    def test_short_key_rejected(self):
        with self.assertRaises(ValueError):
            ApprovalSigner(b"short")


class CipherTests(unittest.TestCase):
    def test_roundtrip_and_downgrade_refused(self):
        from cryptography.fernet import Fernet
        cipher = TokenCipher(Fernet.generate_key().decode())
        self.assertEqual(cipher.decrypt(cipher.encrypt("tok")), "tok")
        with self.assertRaises(Exception):
            cipher.decrypt("plain:tok")


if __name__ == "__main__":
    unittest.main()
