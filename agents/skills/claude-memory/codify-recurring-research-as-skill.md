---
name: codify-recurring-research-as-skill
description: "When the same research topic comes up across multiple sessions, turn it into a skill instead of re-researching it each time."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 58453db6-5ee8-4667-a749-b2d8dad1938a
  modified: 2026-08-26T20:24:23.364Z
---

On hams.com/hams_open, the user pointed out that a topic had come up multiple times in the same night-shift session -- specifically, the project's existing pattern for avoiding Claude API costs on agent-dispatch-style work (the MCP-based scheme built around `mcp_watchdog.py`, service-account users defined under `ingest/`, and skills like `ics_ingestion`/`ham_ingestion`) -- and said: "You might just make a 'avoiding API costs' skill so you don't have to research this again. It's come up several times."

The generalized lesson: when Claude notices it has re-derived or re-explained the same piece of project-specific knowledge more than once (a recurring architecture pattern, a standing constraint, a workflow that isn't obvious from the code alone), the right response is to write that knowledge down as a proper skill (a SKILL.md under `.claude/skills/`) rather than relying on memory files or re-investigating from scratch in each new session. Memory files are for behavioral corrections and standing preferences; a skill is the right home for reusable, teachable procedural/architectural knowledge that should be loaded on demand when the matching task comes up. This preference applies broadly across the hams.com/hams_open/hams_shared codebases whenever a "we've explained this before" moment recurs, not just to the API-cost-avoidance topic that prompted it.
