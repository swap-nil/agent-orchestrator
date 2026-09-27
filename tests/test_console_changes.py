import os
import unittest

from helpers import ROOT, make_service, open_session

from orchestrator.console.changes import ChangeError, RuntimeConfigManager
from orchestrator.console.evals import EvalRunner, load_suite
from orchestrator.models import TurnRequest

SUITE = os.path.join(ROOT, "config", "evals.yaml")


async def manager():
    service, gw, wf = make_service()
    runner = EvalRunner(service.cfg, load_suite(SUITE))
    mgr = RuntimeConfigManager(service, runner)
    service.runtime_refresher = mgr.refresh
    return service, mgr


FIX = [{"target": "intent", "id": "portfolio.overview", "field": "patterns",
        "value": ["\\b(my )?(portfolios?|holdings|positions|investments)\\b", "how (is|are) my (investments|portfolios?)( doing)?", "\\bwhat do i (own|hold|have invested)\\b", "\\bmy net worth\\b"]}]


class EvalTests(unittest.IsolatedAsyncioTestCase):
    async def test_baseline_suite(self):
        service, mgr = await manager()
        run = await mgr.ensure_baseline()
        self.assertEqual(run.total, len(load_suite(SUITE)))
        self.assertEqual({r.id for r in run.results if not r.passed}, {"rt-pf-networth"})  # the known routing gap
        self.assertEqual(run.totals["e2e"]["passed"], run.totals["e2e"]["total"])

    def test_suite_validation(self):
        import tempfile
        from orchestrator.config import ConfigError
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("cases:\n  - {id: a, kind: nope}\n")
        with self.assertRaises(ConfigError):
            load_suite(f.name)
        os.unlink(f.name)


class ChangeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_propose_evaluate_approve_apply_rollback(self):
        service, mgr = await manager()
        await open_session(service)
        before = await service.handle_turn(TurnRequest("s-1", "t1", "What is my net worth?"))
        self.assertEqual(before.intent, "faq.general")

        change = await mgr.propose("alice", FIX, "Customers say 'my net worth'; route them to the overview")
        self.assertEqual(change["status"], "evaluated")
        self.assertEqual(change["comparison"]["fixes"], ["rt-pf-networth"])
        self.assertEqual(change["comparison"]["regressions"], [])
        self.assertEqual(change["eval"]["pass_rate"], 1.0)

        with self.assertRaises(ChangeError) as ctx:
            await mgr.approve(change["id"], "alice", "self-approval")
        self.assertEqual(ctx.exception.status, 403)

        applied = await mgr.approve(change["id"], "bob", "looks good")
        self.assertEqual(applied["applied_version"], 1)
        self.assertEqual(service.runtime_version, 1)
        after = await service.handle_turn(TurnRequest("s-1", "t2", "What is my net worth?"))
        self.assertEqual(after.intent, "portfolio.overview")

        await mgr.rollback(0, "bob", "rehearsal")
        again = await service.handle_turn(TurnRequest("s-1", "t3", "What is my net worth?"))
        self.assertEqual(again.intent, "faq.general")
        events = [r.event for r in await service.c.audit.chain("control-plane")]
        for e in ("change_proposed", "change_evaluated", "change_applied", "runtime_rolled_back"):
            self.assertIn(e, events)

    async def test_regression_blocks_approval(self):
        service, mgr = await manager()
        breaking = [{"target": "intent", "id": "trade.sell", "field": "patterns", "value": ["\\bsell\\b"]}]
        change = await mgr.propose("alice", breaking, "Catch every sell request")
        self.assertEqual(change["status"], "gate_failed")
        self.assertIn("rt-no-trade-branch-news", change["comparison"]["regressions"])
        self.assertTrue(change["weakens"])  # changes how an R3 intent is recognised
        with self.assertRaises(ChangeError):
            await mgr.approve(change["id"], "bob", "")

    async def test_weakening_guard_is_caught_by_evals(self):
        service, mgr = await manager()
        weaker = [{"target": "guards", "field": "injection_patterns", "value": ["reveal (your|the) (system )?prompt"]}]
        change = await mgr.propose("alice", weaker, "Too many false positives")
        self.assertTrue(any("removes" in w for w in change["weakens"]))
        self.assertEqual(change["status"], "gate_failed")
        self.assertIn("sf-inj-ignore", change["comparison"]["regressions"])

    async def test_structural_and_invalid_changes_refused(self):
        service, mgr = await manager()
        bad = [
            [{"target": "intent", "id": "trade.sell", "field": "risk", "value": "R0"}],
            [{"target": "step", "id": "trade.sell/execute", "field": "mode", "value": "read"}],
            [{"target": "intent", "id": "faq.general", "field": "patterns", "value": ["(unclosed"]}],
            [{"target": "intent", "id": "faq.general", "field": "patterns", "value": [".*"]}],
            [{"target": "routing", "field": "min_confidence", "value": {"R3": 0.5}}],
            [{"target": "step", "id": "portfolio.overview/holdings", "field": "timeout_ms", "value": 9000}],
            [{"target": "intent", "id": "trade.sell", "field": "readback_template", "value": "{quantity.__class__}"}],
            [{"target": "intent", "id": "nope", "field": "patterns", "value": ["x"]}],
        ]
        for ops in bad:
            with self.assertRaises(ChangeError, msg=str(ops)):
                await mgr.propose("alice", ops, "testing invalid input")

    async def test_stale_change_cannot_be_applied(self):
        service, mgr = await manager()
        a = await mgr.propose("alice", FIX, "fix net worth routing")
        b = await mgr.propose("alice", [{"target": "intent", "id": "faq.general", "field": "clarification_prompt",
                                         "value": "Is this about our services or your accounts?"}], "clearer prompt")
        await mgr.approve(a["id"], "bob", "")
        with self.assertRaises(ChangeError) as ctx:
            await mgr.approve(b["id"], "bob", "")
        self.assertEqual(ctx.exception.status, 409)

    async def test_shadow_routing_records_agreement(self):
        service, mgr = await manager()
        await open_session(service)
        change = await mgr.propose("alice", FIX, "fix net worth routing")
        await mgr.start_shadow(change["id"], "alice")
        await service.handle_turn(TurnRequest("s-1", "t1", "How is my portfolio doing?"))
        await service.handle_turn(TurnRequest("s-1", "t2", "What is my net worth?"))
        shadow = [r.data for r in await service.c.audit.chain("s-1") if r.event == "shadow_routed"]
        self.assertEqual([s["agrees"] for s in shadow], [True, False])
        self.assertEqual(shadow[1]["candidate"], "portfolio.overview")
        applied = await mgr.approve(change["id"], "bob", "shadow looks right")
        self.assertEqual(applied["status"], "applied")
        self.assertIsNone(service.shadow)

    async def test_other_replica_picks_up_change(self):
        service, mgr = await manager()
        other_service, _, _ = make_service()
        other_service.c.store = service.c.store  # shared Redis in production
        other = RuntimeConfigManager(other_service, EvalRunner(other_service.cfg, load_suite(SUITE)))
        change = await mgr.propose("alice", FIX, "fix net worth routing")
        await mgr.approve(change["id"], "bob", "")
        other._last_refresh = 0
        await other.refresh()
        self.assertEqual(other_service.runtime_version, 1)
        decision = await other_service.c.router.route("What is my net worth?")
        self.assertEqual(decision.intent.id, "portfolio.overview")


if __name__ == "__main__":
    unittest.main()
