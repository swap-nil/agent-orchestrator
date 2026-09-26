# Run: opa test policies/ -v
package orchestrator_test

import rego.v1

import data.orchestrator

base := {
	"environment": "prod",
	"phase": "plan",
	"user": {"subject": "u", "acr": "standard", "acr_rank": 1, "tenant": "t", "entitlements": [], "channel": "voice"},
	"intent": {"id": "portfolio.overview", "risk": "R1", "required_acr": "standard", "required_acr_rank": 1},
	"step": {"id": "holdings", "agent": "portfolio-agent", "skill": "portfolio.holdings", "mode": "read", "data_classes": ["client_confidential"]},
	"agent": {"name": "portfolio-agent", "certified_in": ["prod"], "clearance": ["client_confidential"], "writes_allowed": false, "skills": ["portfolio.holdings"]},
	"approval": {"valid": false},
	"kill_switch": {"disabled_agents": [], "disabled_intents": [], "disabled_risk_classes": []},
	"allowed_channels": ["voice", "chat"],
}

test_allows_valid_read if {
	orchestrator.decision.allow with input as base
}

test_denies_low_acr if {
	d := orchestrator.decision with input as object.union(base, {"user": object.union(base.user, {"acr_rank": 0})})
	not d.allow
	"insufficient authentication level" in d.reasons
}

test_denies_uncertified if {
	d := orchestrator.decision with input as object.union(base, {"environment": "test2"})
	not d.allow
}

test_denies_kill_switch if {
	ks := {"disabled_agents": ["portfolio-agent"], "disabled_intents": [], "disabled_risk_classes": []}
	d := orchestrator.decision with input as object.union(base, {"kill_switch": ks})
	"agent disabled by kill switch" in d.reasons
}

test_write_needs_approval_at_execute if {
	w := object.union(base, {
		"phase": "execute",
		"intent": object.union(base.intent, {"risk": "R3"}),
		"step": object.union(base.step, {"mode": "write", "skill": "trade.execute"}),
		"agent": object.union(base.agent, {"writes_allowed": true, "skills": ["trade.execute"]}),
	})
	d := orchestrator.decision with input as w
	"write requires a valid approval" in d.reasons
	ok := orchestrator.decision with input as object.union(w, {"approval": {"valid": true}})
	ok.allow
}

test_denies_unregistered_agent if {
	d := orchestrator.decision with input as object.union(base, {"agent": null})
	not d.allow
}
