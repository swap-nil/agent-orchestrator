# Orchestrator delegation policy (OPA 1.x / Rego v1).
# Mirrors orchestrator.policy.local_deny_reasons; tests/test_policy_parity.py
# checks that every deny message exists in both. Query: data.orchestrator.decision
package orchestrator

import rego.v1

default decision := {"allow": false, "reasons": ["no decision"]}

decision := {"allow": count(deny) == 0, "reasons": sort([r | some r in deny])}

deny contains "agent not registered" if {
	input.agent == null
}

deny contains "agent not certified for environment" if {
	input.agent != null
	not input.environment in input.agent.certified_in
}

deny contains "skill not offered by agent" if {
	input.agent != null
	not input.step.skill in input.agent.skills
}

deny contains "insufficient authentication level" if {
	input.user.acr_rank < 0
}

deny contains "insufficient authentication level" if {
	input.user.acr_rank < input.intent.required_acr_rank
}

deny contains "missing tenant" if {
	input.user.tenant == ""
}

deny contains "channel not allowed" if {
	not input.user.channel in input.allowed_channels
}

deny contains "agent not cleared for data class" if {
	input.agent != null
	some c in input.step.data_classes
	not c in input.agent.clearance
}

deny contains "agent not permitted to write" if {
	input.step.mode == "write"
	input.agent != null
	not input.agent.writes_allowed
}

deny contains "write outside a transaction intent" if {
	input.step.mode == "write"
	input.intent.risk != "R3"
}

deny contains "write requires a valid approval" if {
	input.step.mode == "write"
	input.phase == "execute"
	not input.approval.valid
}

deny contains "agent disabled by kill switch" if {
	input.agent != null
	input.agent.name in input.kill_switch.disabled_agents
}

deny contains "intent disabled by kill switch" if {
	input.intent.id in input.kill_switch.disabled_intents
}

deny contains "risk class disabled by kill switch" if {
	input.intent.risk in input.kill_switch.disabled_risk_classes
}
