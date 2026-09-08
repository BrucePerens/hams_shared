---
name: ai-agent-action-scope-vs-untrusted-input
description: "Bruce restricts an AI agent's direct action capability wherever the input driving it could be adversarially crafted (e.g. a pager ticket), routing action through a reviewable step (a PR/suggestion) instead of direct state changes."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 5ef88eba-a768-4870-9ddb-1f49c09220f5
  modified: 2026-09-02T04:24:21.209Z
---

On hams.com/hams_open, deciding how much autonomy to give the `PAGER_DUTY_MCP_AI_TRIAGE.md` AI
triage tool, Bruce was asked directly whether the tool should be allowed to acknowledge/resolve
real incident tickets (`set_incident_status`), which silences an on-call page. His answer, verbatim:
"For now, the AI can only make suggestions and PRs. One reason for this is that the ticket itself
may be engineered to deceive the AI."

The reasoning generalizes beyond this one feature: an incident ticket's own text is untrusted
input — anyone who can create or edit a ticket can shape what the AI reads, including content
crafted specifically to manipulate its judgment (a form of prompt injection via a legitimate-looking
data record, not just adversarial text pasted directly into a prompt). Bruce's standing preference
is that when an AI agent's *action* target is reachable through content that could be adversarially
crafted, the agent should be limited to producing a reviewable artifact (a suggestion, a pull
request) rather than taking the real, effectful action directly (changing a ticket's status,
silencing an alert, merging code). A human stays in the loop for the step that actually has real-world
consequences, precisely because the AI's own judgment on that specific decision cannot be fully
trusted when the input feeding it isn't trustworthy.

This is a standing design principle to apply when scoping *any* future AI-agent capability on this
project (or elsewhere) where the triggering/informing content comes from a source Bruce doesn't
otherwise control or fully trust (a support ticket, an inbound email, a user-submitted form, a
third-party API response) — default to "propose, don't act," and reserve direct autonomous state
changes for cases where the informing input is itself trusted or where a wrong action is cheaply,
safely reversible. This is distinct from Bruce's own `hams-dont-be-too-cautious-about-originating-work`
preference (which is about Claude's own initiative to build/originate code, a fundamentally different
question from whether an AI *feature this codebase ships* should be allowed to act autonomously on
untrusted external input) — the two are not in tension.
