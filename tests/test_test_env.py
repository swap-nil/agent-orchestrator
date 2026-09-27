"""Test-environment components: service tokens, mock bank, backend agents, test client, configs."""

import os
import tempfile
import unittest

from helpers import APPROVAL_KEY, ROOT, dev_config, make_service, open_session

import httpx
from fastapi.testclient import TestClient

from domain_agents.backend_agents import DEV_SUBJECT, BankTools, build_backend_agents
from mock_backend.app import create_app as create_bank_app
from mock_backend.bank import TARGETS, MockBank
from orchestrator.approvals import ApprovalSigner, action_hash
from orchestrator.models import ResponseType, TurnRequest
from orchestrator.service_auth import ServiceTokenError, WorkloadIdentityToken
from orchestrator.transport import FakeTransport, HttpResponse


def rpc(skill: str, data: dict | None = None, metadata: dict | None = None, context_id: str = "s-1") -> dict:
    return {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": {
        "contextId": context_id, "parts": [{"data": {"skill": skill, **(data or {})}}], "metadata": metadata or {}}}}


def artifact(response: dict) -> dict:
    task = response["result"]["task"]
    return {"state": task["status"]["state"], "text": task["artifacts"][0]["parts"][0]["text"],
            "data": next((p["data"] for p in task["artifacts"][0]["parts"] if "data" in p), {})}


class ServiceTokenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".jwt")
        handle.write("projected-sa-token\n")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        self.env = {"AZURE_CLIENT_ID": "mi-client", "AZURE_TENANT_ID": "tid",
                    "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com/", "AZURE_FEDERATED_TOKEN_FILE": handle.name}
        self.calls = []
        self.now = 1000.0

    async def post(self, url, form):
        self.calls.append((url, form))
        return 200, {"access_token": f"tok-{len(self.calls)}", "expires_in": 3600}

    async def test_disabled_without_scope(self):
        self.assertEqual(await WorkloadIdentityToken("", environ=self.env, post=self.post).headers(), {})
        self.assertEqual(self.calls, [])

    async def test_client_credentials_with_federated_assertion_and_cache(self):
        source = WorkloadIdentityToken("api://orch/.default", environ=self.env, post=self.post, clock=lambda: self.now)
        self.assertEqual(await source.headers(), {"Authorization": "Bearer tok-1"})
        url, form = self.calls[0]
        self.assertEqual(url, "https://login.microsoftonline.com/tid/oauth2/v2.0/token")
        self.assertEqual(form["grant_type"], "client_credentials")
        self.assertEqual(form["client_assertion"], "projected-sa-token")
        self.assertEqual(form["scope"], "api://orch/.default")
        await source.headers()
        self.assertEqual(len(self.calls), 1)  # cached
        self.now += 3600
        self.assertEqual(await source.token(), "tok-2")  # refreshed near expiry

    async def test_managed_identity_from_imds(self):
        calls = []

        async def get(url, params):
            calls.append((url, params))
            return 200, {"access_token": f"mi-{len(calls)}", "expires_in": "3600"}

        env = {"AZURE_TOKEN_SOURCE": "managed_identity", "AZURE_CLIENT_ID": "mi-client"}
        source = WorkloadIdentityToken("api://orch/.default", environ=env, get=get, post=self.post, clock=lambda: self.now)
        self.assertEqual(await source.headers(), {"Authorization": "Bearer mi-1"})
        url, params = calls[0]
        self.assertEqual(url, "http://169.254.169.254/metadata/identity/oauth2/token")
        self.assertEqual(params["resource"], "api://orch")  # IMDS takes a v1 resource, not a scope
        self.assertEqual(params["client_id"], "mi-client")
        await source.token()
        self.assertEqual(len(calls), 1)  # cached
        self.now += 3600
        self.assertEqual(await source.token(), "mi-2")
        self.assertEqual(self.calls, [])  # no client-credentials grant
        with self.assertRaisesRegex(ServiceTokenError, "AZURE_CLIENT_ID"):
            await WorkloadIdentityToken("s", environ={"AZURE_TOKEN_SOURCE": "managed_identity"}, get=get).token()
        with self.assertRaisesRegex(ServiceTokenError, "AZURE_TOKEN_SOURCE"):
            await WorkloadIdentityToken("s", environ={"AZURE_TOKEN_SOURCE": "nope"}, get=get).token()

    async def test_errors(self):
        with self.assertRaises(ServiceTokenError):
            await WorkloadIdentityToken("s", environ={}, post=self.post).token()

        async def refused(url, form):
            return 400, {"error": "invalid_client"}
        with self.assertRaisesRegex(ServiceTokenError, "invalid_client"):
            await WorkloadIdentityToken("s", environ=self.env, post=refused).token()


class MockBankTests(unittest.TestCase):
    def setUp(self):
        self.bank = MockBank(clock=lambda: 1_700_000_000)

    def test_subject_mapping_is_stable_and_assignable(self):
        self.assertEqual(self.bank.customer_for("oid-a").id, MockBank(clock=lambda: 0).customer_for("oid-a").id)
        self.assertEqual(self.bank.assign("oid-a", "C1005").id, "C1005")
        self.assertEqual(self.bank.customer_for("oid-a").id, "C1005")
        with self.assertRaises(KeyError):
            self.bank.assign("oid-a", "nope")

    def test_every_customer_has_a_coherent_portfolio(self):
        for cid in self.bank.customers:
            self.bank.assign("t", cid)
            pf = self.bank.portfolio("t")
            self.assertGreater(pf["total_value_chf"], 10_000)
            self.assertEqual(pf["total_value_chf"], sum(p["value_chf"] for p in pf["positions"]))
            self.assertAlmostEqual(sum(pf["allocation"].values()), 1.0, places=2)
            self.assertIn(pf["risk_profile"], TARGETS)

    def test_rebalance_proposal_passes_suitability(self):
        checked = 0
        for cid in self.bank.customers:
            self.bank.assign("t", cid)
            advice = self.bank.rebalance("t")
            if advice["needed"]:
                checked += 1
                self.assertIn("percent", advice["text"])
                result = self.bank.suitability("t", advice["proposal"])
                self.assertTrue(result["reference"].startswith("SUIT-"))
        self.assertGreater(checked, 0)

    def test_order_flow_is_idempotent_and_changes_positions(self):
        quote = self.bank.quote_sell("u")
        before = next(p["units"] for p in self.bank.portfolio("u")["positions"] if p["instrument_id"] == quote["instrument_id"])
        order = self.bank.place_order("u", quote, "key-1")
        self.assertEqual(self.bank.place_order("u", quote, "key-1"), order)
        after = next(p["units"] for p in self.bank.portfolio("u")["positions"] if p["instrument_id"] == quote["instrument_id"])
        self.assertEqual(before - quote["quantity"], after)
        self.assertEqual([o["reference"] for o in self.bank.orders_for("u")], [order["reference"]])
        with self.assertRaises(ValueError):
            self.bank.place_order("u", {**quote, "quantity": 10**9}, "key-2")
        self.bank.reset()
        self.assertEqual(self.bank.orders_for("u"), [])

    def test_api(self):
        client = TestClient(create_bank_app(self.bank))
        self.assertEqual(client.get("/v1/portfolio").status_code, 400)
        pf = client.get("/v1/portfolio", headers={"X-User-Subject": "u"}).json()
        self.assertEqual(pf["customer_id"], self.bank.customer_for("u").id)
        quote = client.post("/v1/orders/quote", json={}, headers={"X-User-Subject": "u"}).json()
        placed = client.post("/v1/orders", json={"action": quote}, headers={"X-User-Subject": "u", "Idempotency-Key": "k"})
        self.assertEqual(placed.status_code, 201)
        self.assertEqual(len(client.get("/v1/orders", headers={"X-User-Subject": "u"}).json()["orders"]), 1)
        self.assertEqual(len(client.get("/v1/admin/customers").json()["customers"]), 12)
        self.assertEqual(len(client.get("/v1/kb/articles").json()["articles"]), 2)


class BackendAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bank = MockBank()
        transport = httpx.ASGITransport(app=create_bank_app(self.bank))
        self.tools = BankTools("http://bank", httpx.AsyncClient(transport=transport, base_url="http://bank"))
        self.public_key = ApprovalSigner(APPROVAL_KEY.encode()).public_key_pem()
        self.agents = build_backend_agents(self.tools, approval_public_key=self.public_key)
        self.headers = {"a2a-version": "1.0"}

    async def call(self, agent, skill, data=None, metadata=None):
        return artifact(await self.agents[agent].handle(rpc(skill, data, metadata), self.headers))

    async def test_reads_come_from_the_bank(self):
        pf = self.bank.portfolio(DEV_SUBJECT)
        holdings = await self.call("portfolio-agent", "portfolio.holdings")
        self.assertIn(f"CHF {pf['total_value_chf']:,}", holdings["text"])
        self.assertEqual(len(holdings["data"]["positions"]), len(pf["positions"]))
        self.assertIn("percent", (await self.call("market-agent", "market.quotes"))["text"])
        self.assertIn("open monday to friday", (await self.call("faq-agent", "faq.answer"))["text"].lower())

    def test_agents_answer_over_http(self):
        # Regression: the kit's FastAPI wrapper once treated `request` as a query parameter (HTTP 422).
        from domain_agents.kit import asgi_app
        client = TestClient(asgi_app(self.agents))
        res = client.post("/agents/market-agent", json=rpc("market.quotes"), headers={"A2A-Version": "1.0"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(artifact(res.json())["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(client.post("/agents/nope", json=rpc("x")).status_code, 404)

    async def test_scope_must_name_the_skill(self):
        agents = build_backend_agents(self.tools, token_verifier=lambda token: {"oid": "u", "scp": "market.quotes"})
        headers = {**self.headers, "authorization": "Bearer t"}
        denied = artifact(await agents["portfolio-agent"].handle(rpc("portfolio.holdings"), headers))
        self.assertEqual(denied["state"], "TASK_STATE_REJECTED")
        allowed = artifact(await agents["market-agent"].handle(rpc("market.quotes"), headers))
        self.assertEqual(allowed["state"], "TASK_STATE_COMPLETED")

    async def test_execute_rejects_tampered_or_unsigned_actions(self):
        action = (await self.call("trade-agent", "trade.prepare"))["data"]["action"]
        full = {"intent": "trade.sell", "session_id": "s-1", "tenant": "t", "params": action}
        a_hash = action_hash(full)
        signer = ApprovalSigner(APPROVAL_KEY.encode())
        token = signer.issue("ap-1", a_hash, 2_000_000_000)
        meta = {"tenant": "t", "idempotencyKey": "k1", "approvalToken": token}
        changed = {"action": {**action, "quantity": 1}, "actionHash": a_hash}
        tampered = await self.call("trade-agent", "trade.execute", changed, meta)
        self.assertEqual(tampered["state"], "TASK_STATE_REJECTED")
        forged_meta = {**meta, "idempotencyKey": "k2", "approvalToken": token[:-4] + "AAAA"}
        forged = await self.call("trade-agent", "trade.execute", {"action": action, "actionHash": a_hash}, forged_meta)
        self.assertEqual(forged["state"], "TASK_STATE_REJECTED")
        self.assertEqual(self.bank.orders, [])
        approved = {"action": action, "actionHash": a_hash}
        placed = await self.call("trade-agent", "trade.execute", approved, {**meta, "idempotencyKey": "k3"})
        self.assertEqual(placed["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(len(self.bank.orders), 1)

    async def test_full_stack_through_the_orchestrator(self):
        async def handler(method, url, request):
            name = url.rsplit("/", 1)[-1]
            headers = {k.lower(): v for k, v in request["headers"].items()}
            return HttpResponse(200, await self.agents[name].handle(request["json"], headers))

        from helpers import FakeGateway
        config = dev_config()
        config.workflows.enabled = True
        gw = FakeGateway()
        gw.transport = FakeTransport(handler)
        service, _, wf = make_service(config, gateway=gw)
        await open_session(service)
        pf = self.bank.portfolio(DEV_SUBJECT)
        overview = await service.handle_turn(TurnRequest("s-1", "t1", "How is my portfolio doing?"))
        self.assertEqual(overview.type, ResponseType.ANSWER, overview.reasons)
        self.assertIn(f"CHF {pf['total_value_chf']:,}", overview.text)
        advice = await service.handle_turn(TurnRequest("s-1", "t2", "Should I rebalance?"))
        self.assertEqual(advice.type, ResponseType.ANSWER, advice.reasons)
        tx = await service.handle_turn(TurnRequest("s-1", "t3", "Sell some units of my ETF"))
        self.assertEqual(tx.type, ResponseType.APPROVAL_REQUIRED, tx.reasons)
        params = tx.approval["action"]["params"]
        self.assertIn(f"sell {params['quantity']} units of {params['instrument']}", tx.text)
        a = tx.approval
        await service.decide_approval(a["approval_id"], approve=True, subject="u-1", acr="stepup",
                                      presented_action_hash=a["action_hash"])
        token = wf.signals[a["workflow_id"]][0]["approval_token"]
        result = await service.execute_approved_write(wf.started[a["workflow_id"]], token)
        self.assertTrue(result["success"], result)
        self.assertEqual(len(self.bank.orders), 1)
        self.assertEqual(self.bank.orders[0]["quantity"], params["quantity"])


class TestClientTests(unittest.TestCase):
    def setUp(self):
        self.sent = []

        def orch(request: httpx.Request) -> httpx.Response:
            self.sent.append(request)
            return httpx.Response(200, json={"accepted": True, "declined": False, "reasons": []})

        self.orch = httpx.AsyncClient(transport=httpx.MockTransport(orch), base_url="http://orch")
        self.bank = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_bank_app(MockBank())), base_url="http://bank")

    def test_dev_mode_forwards_recomputed_hash(self):
        from test_client.app import create_app
        client = TestClient(create_app({}, orchestrator=self.orch, backend=self.bank))
        self.assertTrue(client.get("/config.json").json()["devMode"])
        action = {"intent": "trade.sell", "params": {"quantity": 5}}
        res = client.post("/api/approvals/ap-1", json={"approve": True, "action": action})
        self.assertEqual(res.status_code, 200)
        import json
        body = json.loads(self.sent[0].content)
        self.assertEqual(body["action_hash"], action_hash(action))
        self.assertEqual(body["dev_user"]["acr"], "stepup")
        self.assertIn("portfolio", client.get("/api/me").json())
        self.assertIn(b"msal-browser", client.get("/").content)

    def test_entra_mode_requires_token_and_forwards_both_identities(self):
        from test_client.app import create_app

        class Validator:
            def validate(self, token):
                if token != "user-token":  # noqa: S105 - test fixture
                    from orchestrator.api.security import AuthError
                    raise AuthError("bad")
                return {"oid": "u", "sub": "u", "roles": ["stepup"]}

        class Caller:
            async def headers(self):
                return {"Authorization": "Bearer service-token"}

        env = {"TENANT_ID": "tid", "SPA_CLIENT_ID": "spa", "ORCHESTRATOR_APP_ID": "orch"}
        app = create_app(env, validator=Validator(), orchestrator=self.orch, backend=self.bank, service_token=Caller())
        client = TestClient(app)
        self.assertEqual(client.get("/config.json").json()["scopes"], ["api://orch/access_as_user"])
        self.assertEqual(client.post("/api/approvals/ap-1", json={"approve": True, "action": {}}).status_code, 401)
        res = client.post("/api/approvals/ap-1", json={"approve": True, "action": {}},
                          headers={"Authorization": "Bearer user-token"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.sent[-1].headers["x-user-token"], "user-token")
        self.assertEqual(self.sent[-1].headers["authorization"], "Bearer service-token")


class TestEnvConfigTests(unittest.TestCase):
    def test_test_profile_is_valid(self):
        from orchestrator.catalogue import load_catalogue
        from orchestrator.config import load_config
        config = load_config(os.path.join(ROOT, "config", "orchestrator.test.yaml"), environ={})
        self.assertEqual(config.service.environment, "test")
        self.assertEqual(config.auth.mode, "jwt")
        self.assertEqual(config.identity.mode, "entra_obo")
        self.assertEqual(config.command_center.operator_auth, "jwt")
        cat = load_catalogue(os.path.join(ROOT, "config", "intents.yaml"), os.path.join(ROOT, "config", "agents.yaml"),
                             config.auth.acr_levels)
        self.assertTrue(all("test" in a.certified_in for a in cat.agents.values()))

    def test_test_service_configs_load(self):
        from master_agent.config import load_master_config
        from token_service.config import load_token_service_config
        ma = load_master_config(os.path.join(ROOT, "config", "master_agent.test.yaml"))
        self.assertEqual((ma.stt.provider, ma.tts.provider), ("azure", "azure"))
        ts = load_token_service_config(os.path.join(ROOT, "config", "token_service.test.yaml"))
        self.assertEqual(ts.acr_claim, "roles")

    def test_entra_setup_script_reads_the_registry(self):
        import importlib.util
        script = os.path.join(ROOT, "deploy", "azure", "scripts", "entra_setup.py")
        spec = importlib.util.spec_from_file_location("entra_setup", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        agents = module.read_agents(os.path.join(ROOT, "config", "agents.yaml"))
        self.assertEqual(agents["trade-agent"], ["trade.prepare", "trade.execute"])
        # Scope ids are stable across runs, so re-running the script never changes consented permissions.
        self.assertEqual(module.stable_id("p-orchestrator", "scope", "access_as_user"),
                         module.stable_id("p-orchestrator", "scope", "access_as_user"))

    def test_backend_agents_cover_the_registry(self):
        from orchestrator.catalogue import load_catalogue
        cat = load_catalogue(os.path.join(ROOT, "config", "intents.yaml"), os.path.join(ROOT, "config", "agents.yaml"),
                             ["low", "standard", "stepup"])
        agents = build_backend_agents(BankTools("http://x"))
        for name, spec in cat.agents.items():
            self.assertIn(name, agents)
            self.assertEqual(set(spec.skills), set(agents[name]._skills), name)



def load_script(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "deploy", "azure", "scripts", f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_compose_env(path: str) -> dict[str, str]:
    """The subset of the Compose env-file syntax the renderer writes: 'literal' and "with \n escapes"."""
    values = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh.read().splitlines():
            key, raw = line.split("=", 1)
            if raw[:1] == "'" and raw[-1:] == "'":
                values[key] = raw[1:-1]
            elif raw[:1] == '"' and raw[-1:] == '"':
                values[key] = raw[1:-1].replace("\\n", "\n")
            else:
                values[key] = raw
    return values


class VmBundleTests(unittest.TestCase):
    """deploy/azure: the files rendered for the test VM fit the Compose file, the configs and bootstrap.sh."""

    AGENTS = ["faq-agent", "portfolio-agent", "market-agent", "advice-agent", "compliance-agent", "trade-agent"]

    @classmethod
    def setUpClass(cls):
        import json
        import shutil
        import uuid
        from types import SimpleNamespace
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.ids = {k: str(uuid.uuid4()) for k in ("orchestrator", "tokenService", "clientBackend", "masterAgent")}
        cls.tenant = str(uuid.uuid4())
        outputs = {k: {"value": v} for k, v in {
            "vmName": "vm-t", "acrName": "acrt", "acrLoginServer": "acrt.azurecr.io", "keyVaultName": "kv-t",
            "tenantId": cls.tenant, "appHost": "t-app.switzerlandnorth.cloudapp.azure.com", "speechRegion": "switzerlandnorth",
            "identities": [{"key": k, "clientId": v, "principalId": str(uuid.uuid4())} for k, v in cls.ids.items()],
        }.items()}
        cls.apps = {"orchestrator": str(uuid.uuid4()), "console": str(uuid.uuid4()), "spa": str(uuid.uuid4()),
                    "agents": {a: str(uuid.uuid4()) for a in cls.AGENTS}}
        images = "".join(f"{k}=acrt.azurecr.io/x@sha256:{'a' * 64}\n"
                         for k in ("IMG_ORCHESTRATOR", "IMG_AGENT", "IMG_TOKEN_SERVICE", "IMG_MOCK"))
        files = {"outputs.json": json.dumps(outputs), "entra.json": json.dumps({"apps": cls.apps}), "images.env": images,
                 "key.pem": ApprovalSigner(APPROVAL_KEY.encode()).public_key_pem()}
        for name, content in files.items():
            with open(os.path.join(cls.tmp, name), "w", encoding="utf-8") as fh:
                fh.write(content)
        cls.out = os.path.join(cls.tmp, "vm")

        def tmp(name):
            return os.path.join(cls.tmp, name)

        load_script("render_vm_bundle").render(SimpleNamespace(
            outputs=tmp("outputs.json"), entra=tmp("entra.json"), images=tmp("images.env"), approval_public_key=tmp("key.pem"),
            acme_email="ops@example.com", allowed_source_ranges="203.0.113.0/24, 198.51.100.7/32",
            agents=os.path.join(ROOT, "config", "agents.yaml"), policy=os.path.join(ROOT, "policies", "orchestrator.rego"),
            vm_dir=os.path.join(ROOT, "deploy", "azure", "vm"), out=cls.out))

    def env(self, name: str) -> dict[str, str]:
        return read_compose_env(os.path.join(self.out, "env", f"{name}.env"))

    def test_compose_references_only_rendered_or_bootstrapped_files(self):
        import re

        import yaml
        with open(os.path.join(self.out, "docker-compose.yaml"), encoding="utf-8") as fh:
            compose = yaml.safe_load(fh)
        with open(os.path.join(self.out, "bootstrap.sh"), encoding="utf-8") as fh:
            bootstrap = fh.read()
        from_bootstrap = set(re.findall(r"envfile (secrets/[\w-]+\.env)", bootstrap)) | {"livekit/livekit.yaml"}

        def provided(path):
            return path in from_bootstrap or os.path.isfile(os.path.join(self.out, path))

        for name, service in compose["services"].items():
            for path in service.get("env_file", []):
                self.assertTrue(provided(path), f"{name}: {path}")
            for volume in service.get("volumes", []):
                source = volume.split(":")[0]
                if source.startswith("./") and source != "./data/temporal":  # directory created by bootstrap.sh
                    self.assertTrue(provided(source[2:]), f"{name}: {source}")
        self.assertEqual({s for s in compose["services"] if s.endswith("-agent")} - {"master-agent"}, set(self.AGENTS))
        # Every secret the Bicep template stores is read by bootstrap.sh, and the reverse.
        with open(os.path.join(ROOT, "deploy", "azure", "main.bicep"), encoding="utf-8") as fh:
            bicep = fh.read()
        stored = set(re.findall(r"name: '([a-z0-9-]+)', value:", bicep)) | {"speech-key", "appinsights-connection-string"}
        read = set(re.findall(r"\$\(secret ([a-z0-9-]+)\)", bootstrap)) - {"console-client-secret"}  # from entra_setup.py
        self.assertEqual(read, stored)

    def test_orchestrator_environment_and_registry(self):
        from orchestrator.catalogue import load_catalogue
        from orchestrator.config import load_config
        env = self.env("orchestrator")
        self.assertEqual(env["ORCH_CONFIG_FILE"], "/app/config/orchestrator.test.yaml")
        config = load_config(os.path.join(ROOT, "config", "orchestrator.test.yaml"), environ=env)
        self.assertEqual(config.identity.client_auth, "managed_identity")
        self.assertEqual(env["AZURE_CLIENT_ID"], self.ids["orchestrator"])
        self.assertEqual(config.auth.route_callers["turns"], [self.ids["masterAgent"]])
        self.assertEqual(config.auth.route_callers["sessions"], [self.ids["tokenService"]])
        self.assertEqual(config.auth.jwt.audience, self.apps["orchestrator"])
        self.assertEqual(config.command_center.operator_jwt.audience, self.apps["console"])
        self.assertEqual(config.catalogue.registry_file, "/etc/orchestrator/agents.yaml")  # mounted by Compose
        cat = load_catalogue(os.path.join(ROOT, "config", "intents.yaml"), os.path.join(self.out, "config", "agents.yaml"),
                             config.auth.acr_levels)
        self.assertEqual({n: a.audience for n, a in cat.agents.items()},
                         {n: f"api://{app}" for n, app in self.apps["agents"].items()})

    def test_callers_use_their_managed_identity(self):
        for name, identity in (("token-service", "tokenService"), ("master-agent", "masterAgent"),
                               ("test-client", "clientBackend")):
            env = self.env(name)
            self.assertEqual(env["AZURE_TOKEN_SOURCE"], "managed_identity", name)
            self.assertEqual(env["AZURE_CLIENT_ID"], self.ids[identity], name)
        self.assertEqual(self.env("token-service")["TS__LIVEKIT__URL"], "wss://t-app.switzerlandnorth.cloudapp.azure.com")
        self.assertEqual(self.env("master-agent")["LIVEKIT_URL"], "ws://127.0.0.1:7880")

    def test_agents_and_gateway(self):
        import json

        from domain_agents.serve import create_app
        self.assertIn("BEGIN PUBLIC KEY", self.env("agent-trade-agent")["APPROVAL_PUBLIC_KEY"])
        for name in self.AGENTS:
            env = self.env(f"agent-{name}")
            self.assertEqual(env["AGENT_AUDIENCE"], self.apps["agents"][name])
            create_app(env)
        with open(os.path.join(self.out, "config", "envoy.yaml"), encoding="utf-8") as fh:
            envoy = json.load(fh)
        hcm = envoy["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]["typed_config"]
        jwt = next(f for f in hcm["http_filters"] if f["name"] == "envoy.filters.http.jwt_authn")["typed_config"]
        self.assertEqual({n: p["audiences"] for n, p in jwt["providers"].items()},
                         {n: [app] for n, app in self.apps["agents"].items()})
        issuer = f"https://login.microsoftonline.com/{self.tenant}/v2.0"
        self.assertTrue(all(p["issuer"] == issuer for p in jwt["providers"].values()))
        routes = {r["match"].get("path"): r for r in hcm["route_config"]["virtual_hosts"][0]["routes"]}
        self.assertEqual(routes["/agents/trade-agent"]["route"]["retry_policy"], {"num_retries": 0})

    def test_settings(self):
        settings = read_compose_env(os.path.join(self.out, "settings.env"))
        self.assertEqual(settings["ALLOWED_SOURCE_RANGES"], "203.0.113.0/24 198.51.100.7/32")
        self.assertEqual(settings["CONSOLE_CLIENT_ID"], self.apps["console"])
        self.assertEqual(settings["KEY_VAULT"], "kv-t")

if __name__ == "__main__":
    unittest.main()
