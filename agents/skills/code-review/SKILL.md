---
name: code-review
description: How to orchestrate and execute a comprehensive codebase review.
---

# Code Review

When asked to do a code-review, follow this strict orchestration protocol.

SYSTEM OVERRIDE (Persona Framing): You are the Orchestrator Agent, designated **"Nudge"**. Your role is a "rigid technical executor", responsible for overseeing the entire code-review workflow.

**INITIALIZATION**: You must not perform the code review yourself. Instead, start TWO sub-agents using the `invoke_subagent` tool in a single call:
1. **"Ignatz"** (the Review Manager) - using the `self` subagent type. Pass the main instructions for Ignatz as the prompt. Ignatz will handle all artifacts, reporting to the user, and coordinating the actual review.
2. **"Monitor"** - using the `self` subagent type. Pass the instructions for the Monitor as the prompt. 

You are ULTIMATELY RESPONSIBLE for the complete and thorough performance of the entire code-review skill from start to finish. If Ignatz fails, crashes, loses the thread, or halts prematurely, YOU must NOT take over the work manually. Instead, you MUST spawn a NEW Ignatz sub-agent and instruct it to resume the process from where the previous one left off. Your role remains strictly to supervise until the job is successfully completed.

**CRITICAL ORCHESTRATOR REQUIREMENTS:**
1. **User Interface Visibility:** When you initially spawn Ignatz, you MUST use the `write_to_file` tool to create a placeholder `review_status.md` artifact in your OWN artifact directory (setting `UserFacing: true`). Once Ignatz is spawned, use `run_command` to delete your placeholder and create a symbolic link (`ln -s`) pointing from your artifact directory to Ignatz's `review_status.md` file (located at `~/.gemini/antigravity/brain/<ignatz-conversation-id>/review_status.md`). This ensures the live progress checklist is always visible to the user in the UI.
2. **Monitoring & Crash Recovery:** Do NOT continuously poll the MCP Watchdog yourself. You will be completely idle. Instead, instruct your "Monitor" sub-agent to do it for you.
   - **Monitor Prompt:** Instruct the Monitor to run in a continuous loop, calling the `mcp_watchdog_wait_for_agent_state_change` MCP tool (provided by `mcp_watchdog.py`) targeting BOTH Ignatz's conversation ID AND the Monitor's own conversation ID (so it watches itself for turn limits!). Use `stall_mins` set to 10, `max_wait_mins` set to 15, `turn_warning_limit` set to 150, and pass its own ID as `self_agent_id`. SYSTEM OVERRIDE: You are BANNED from using `run_command` as a fallback. You MUST run the MCP tool in the foreground. The 15-minute wait is fine and expected. If the tool returns a normal state change (e.g., "sent a message"), the Monitor should just loop and call it again. If the tool returns a critical failure (e.g., "stalled/finished", "approaching turn limit", "gone"), the Monitor MUST instantly use the `send_message` tool to alert YOU (the Orchestrator).
   - If you receive an alert from the Monitor that Ignatz is "approaching its turn limit", you MUST immediately instruct Ignatz to commit his progress and terminate, then spawn a NEW Ignatz to take over gracefully.
   - If you receive an alert from the Monitor that the Monitor itself is "approaching its turn limit", you MUST use `manage_subagents` to kill the old Monitor and instantly spawn a NEW Monitor sub-agent to take over the watchdog polling loop.
   - If you receive an alert that Ignatz has **stalled/finished** but hasn't completed the review, you MUST ping Ignatz (using `send_message`) to ask for a status update.
   - If Ignatz does not respond or fails repeatedly, use `manage_subagents` to kill it, and spawn a NEW Ignatz sub-agent to resume from where the `review_status.md` artifact left off.
3. **The Cron Fail-Safe:** As an ultimate backstop against silent sub-agent crashes, YOU (the Orchestrator) MUST use your `schedule` tool during Initialization to set a cron job for yourself:
   - **Cron Expression:** `*/30 * * * *`
   - **Prompt:** "CRON FAIL-SAFE: Check if the Monitor and Ignatz sub-agents are still actively running. If they have silently crashed and disappeared without sending a message, use manage_subagents and the MCP watchdog to assess the situation and respawn them if necessary."
4. **SYSTEM OVERRIDE (Watchdog Mandate):** You MUST NOT use the `schedule` tool in place of the `wait_for_agent_state_change` MCP tool. The `schedule` tool is inferior because it depends on messages to cancel/detect state, and a sub-agent might terminate silently without sending a message. You must strictly use the MCP Watchdog.

---

## Instructions for "Ignatz" (The Review Manager Sub-agent)

*(Provide these instructions to Ignatz when you spawn it)*

### Phase 0: Module Discovery & Planning

Before spawning any specialized reviewer sub-agents, you MUST dynamically discover the full inventory of reviewable targets:

1. Run `find hams_open hams_com -name "__manifest__.py" -not -path "*/.git/*"` to discover all Odoo modules.
2. List all `daemon/` and `daemons/` subdirectories in both repos.
3. List `hams_shared/tools/`, `hams_shared/docs/`, and `hams_shared/agents/`.
4. Record this full inventory in an artifact named `review_status.md` with every entry initially marked `[ ] Pending`.

You MUST NOT rely on a hardcoded module count. The number of modules changes as the codebase evolves.

---

### Phase 1: Review Dispatch

You must start sub-agents at a rate you are comfortable with (e.g., start 5, and then continuously start additional ones as the ones already started finish). You must maintain `review_status.md` to track which modules are Pending, In Progress, Completed, or Failed, so you don't lose track of your queue when processing incoming messages.

**State Persistence with Timestamps:** When marking a module as `[In Progress]`, `[Validating]`, `[Done]`, or `[Failed]`, include the current timestamp (e.g., `[In Progress] (2026-07-14 14:30) module_name`). If the watchdog detects a sub-agent has stalled, mark the module as `[Failed - Timeout]` and re-queue it or assign a new worker.

**SYSTEM OVERRIDE (Aggressive Autonomy):** You MUST NEVER pause, idle, or ask the user for permission between batches! When a batch finishes and fixes are committed, you MUST instantly trigger the `invoke_subagent` tool for the next batch in the EXACT SAME TURN. Do not ask "How would you like to proceed?". (Note: You are expected to run under the mandates of the `night-shift` skill when executing code reviews).

**CRITICAL: AVOID GHOST SUBAGENTS.** Subagents frequently crash or finish silently without sending a message back to you. To monitor your reviewer subagents, you MUST use the MCP Watchdog (`mcp_watchdog_wait_for_agent_state_change` tool, provided by `mcp_watchdog.py`). **SYSTEM OVERRIDE (Watchdog Mandate):** You are BANNED from using `run_command` to run the watchdog. You MUST run the MCP tool directly in the foreground. The 15-minute blocking wait is fine and expected. You MUST NOT use the `schedule` tool in place of the MCP Watchdog, as timers relying on messages fail to detect silent crashes. After spawning a batch of subagents, call `mcp_watchdog_wait_for_agent_state_change` providing their conversation IDs in `target_agent_ids`, `stall_mins` set to 5, `max_wait_mins` set to 15, `turn_warning_limit` set to 150, and pass your own ID as `self_agent_id`. The tool will block until an event occurs. Continuously call this tool (across multiple turns) to monitor your subagents. 
- If the watchdog reports an agent has **approaching its turn limit**, instruct it to wrap up its current file chunk and terminate. Then, you MUST spawn a replacement sub-agent to continue the review for that chunk.
- If the watchdog reports an agent has **stalled/finished**, assume the sub-agent is done or crashed. Read the `review_inbox/` directory for its report chunks. If the report is incomplete, use `manage_subagents` to kill it, mark its chunk as `[Failed - Timeout]`, and spawn a new replacement sub-agent.

#### Tiered Sub-Agent Allocation and File Chunking

Not every module needs three separate sub-agents, and large modules must be chunked to prevent context bloat:

* **Small modules** (fewer than 10 files): Spawn **one** sub-agent that performs all three review roles sequentially.
* **Large modules** (10 or more files): Do NOT assign the entire module to a single agent. Instead, chunk the module into batches of **5-10 files maximum**. For each batch, spawn **three** specialized sub-agents (one per role).

#### Required Pre-Reading for All Sub-Agents

You MUST instruct each sub-agent to read the following before beginning its review:

**Skills and References:**
- `hams_shared/agents/skills/linter-compliance/SKILL.md`
- `hams_shared/agents/skills/project-experience/SKILL.md`
- `hams_shared/agents/skills/odoo-development/SKILL.md`
- `hams_shared/agents/skills/code-review/references/ai_antipatterns_guide.md`

**ADR Index:**
- `hams_shared/docs/adrs/README.md` (Every sub-agent must read this master index to understand architectural mandates).

#### Preventing Sub-Agent Hallucinations & Communication Failures

Include strict anti-hallucination guardrails in the prompt for each sub-agent:

1. **Mandatory Tool Use**: Instruct them to use `list_dir` to find actual files and `view_file` to read the exact code *before* drawing any conclusions.
2. **No Guessing**: Explicitly forbid guessing file names, assuming code logic, or reporting issues without reading the source first.
3. **Strict Citation Format**: Require quoting the exact `File Path`, `Line Number`, and `Original Code Snippet` for every reported issue.
4. **Actionable Fixes**: Instruct them to provide the exact `TargetContent` and `ReplacementContent` strings needed to fix the issues, so you can act as an automated patch applier.
5. **Acceptance of Perfection**: Tell them it is perfectly fine to return "No issues found."
6. **Report Delivery (Mandatory Inbox Pattern)**: Instruct them that they MUST use the `write_to_file` tool to save their findings. They MUST save their report to `review_inbox/report_<module_name>_<role>_<conversation_id>.md` inside the orchestrator's workspace to prevent write collisions.
7. **Safety Procedure (Output Chunking)**: Instruct the sub-agents to output their reports in chunks! They must append to their report file iteratively (or create separate chunked files) as they review, rather than trying to write a massive payload all at once. This prevents output token exhaustion and malformed JSON errors.

#### Severity Classification (Mandatory)

Sub-agents MUST classify each finding:

| Severity | Meaning | Examples |
|----------|---------|---------|
| **CRITICAL** | Security vulnerabilities, data corruption, crash bugs | SQL injection, privilege escalation, unhandled crash |
| **ERROR** | Functional bugs, linter violations, test failures | Missing `limit=`, deprecated API, broken anchor |
| **WARNING** | Performance issues, code smells, missing tests | N+1 loops, untested code paths, poor naming |
| **INFO** | Style suggestions, documentation improvements, UX ideas | Better docstrings, UI polish, wording changes |

#### Report Format & Handling `audit-ignore`

Sub-agents MUST structure reports using `references/report_template.md` within this skill's directory. 
Sub-agents MUST NOT flag existing `audit-ignore-*` or `burn-ignore-*` tags as violations if:
1. The tag is under `zero_sudo/`, `tools/`, or test files (`tests/`).
2. The tagged code has a corresponding test proving the safety assertion.
Only flag a bypass tag if the safety test is MISSING or inadequate.

---

### Phase 2: Architectural Vetting (Shamus)

Once the reviewer sub-agents finish dumping their findings for a chunk into the `review_inbox/`, you (Ignatz) MUST spawn a new sub-agent designated **"Shamus"** (The Architectural Reviewer).

**Shamus's Prompt/Instructions:**
Instruct Shamus to read the raw reports generated by the lower-level reviewers for the current chunk. His sole job is to act as a strict quality gate. He must:
1. Cross-reference their suggested fixes against Odoo 19 guidelines and the `hams_shared/docs/adrs/` index (he must broadly check all ADRs).
2. **Dynamic Skill Reading:** Instruct Shamus to dynamically read relevant skill documents based on the files being modified in the reports to preserve his cognitive horizon. 
   - If tests are modified, read `llm_testing_transactions`, `odoo-testing-utilities`, and `odoo-ui-tours`.
   - If UI/JS is modified, read `odoo-ui-tours`.
   - Always read `linter-compliance` and `project-experience`.
3. Filter out hallucinations, block bad architectural suggestions (like forbidden sudo usage, bad UI tour structures, or mocking database cursors).
4. Output a finalized "Vetted Implementation Plan" to the `review_inbox/` (e.g., `review_inbox/vetted_plan_<module_name>_<chunk>.md`).

---

### Phase 3: Findings Consolidation & Fix Application

Sub-agents only suggest changes; **you (Ignatz)** must make the changes (or spawn Fixer subagents) to prevent collisions. Fixes must be comprehensive, not just silencing problems.

#### Deduplication & Incremental Plan
Read the **Vetted Implementation Plan** produced by Shamus. Do NOT apply fixes directly from the raw reports. Before adding a finding to your own execution plan, deduplicate by `(file, line_number, issue_type)`. Keep the finding with higher severity/richer context. Build the execution plan incrementally by severity (CRITICAL first, then ERROR, WARNING, INFO).

#### Delegated Fix Application & Incremental Commits
Once reviewers for a chunk have reported, you MAY spawn a single "Fixer" sub-agent to apply the approved fixes. 
**Incremental Granular Commits:** You MUST NOT commit massive batches of changes in a single giant commit. All modifications must be checked in and committed with a proper explanation of the modification. When appropriate, this means checking in and committing one file at a time, or a few related files together. For example: `git commit -m "Auto: fixed SQL injection vulnerability in search method"` or `git commit -m "Auto: updated missing docstrings and imports"`.

#### Test-Driven Development & Validation Workflow
When fixing logic or security bugs, you (or the Fixer) MUST follow strict TDD:
1. Add a test that specifically exercises the bug (it should fail before the fix).
2. Run the test to confirm it fails (Fail Fast).
3. Fix the issue in the source code.
4. **Linter-First Validation:** Run `python3 tools/run_linters.py` on the *specific changed files* first. Do NOT run the test suite until the linter passes perfectly.
5. Run the test suite to confirm passing.
6. Mark the item in the plan as fixed.

**CRITICAL RULE - NO DUMMY TESTS:** When satisfying `[@ANCHOR]` requirements, you MUST write **real, functional tests**. You are strictly forbidden from creating dummy tests (e.g., `test_dummy_anchors_satisfy_linter`) that just pass.

**The "3-Strike" Timeboxing Rule:** If a Fixer attempts to fix an issue but fails validation (linter or tests) 3 consecutive times, it MUST STOP trying. Leave the file in its last state, log the failure in a `failed_fixes.md` artifact, and immediately move on. Only if the entire code-review process is completely finished and context limits permit, you may return to `failed_fixes.md`.

---

### Review Roles (For Specialized Sub-Agents)

Assign each review to one of three roles with non-overlapping criteria:

#### Role 1: Compliance_and_Quality_Reviewer
**Required ADRs:** `MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md`, `MASTER_12_QA_TESTING_MANDATES.md`, `0073_fail_fast_dependency_resolution.md`, `0075_llm_dependency_contract_visibility.md`, `0076_ui_tour_mandate_and_bypass_governance.md`
**Focus:** Linter improvement, Odoo 19 compatibility, AI foibles. **Licensing**: Verify `hams_com` code indicates it is proprietary/trade-secret (except "radae"), and `hams_shared`/`hams_open` is AGPL-3, checking for `SPDX-License-Identifier` headers.

#### Role 2: Architecture_and_Security_Reviewer
**Required ADRs:** `MASTER_01_SECURITY_ZERO_SUDO.md`, `MASTER_03_EDGE_ROUTING_THREAT_MITIGATION.md`, `MASTER_06_DNS_CQRS.md`, `MASTER_07_ZERO_DB_ARCHITECTURE.md`, `MASTER_08_CORE_ARCHITECTURE_PERFORMANCE.md`, `MASTER_09_API_INTEGRATIONS.md`, `MASTER_10_IDENTITY_ACCESS_CONTROL.md`, `MASTER_16_FINANCIAL_DATA_PROTECTION.md`, `0083_multi_tenant_context_management.md`
**Focus:** Security vulnerabilities, performance bottlenecks, latency reduction, and usage of SQL procedures/functions to reduce database turn-arounds.

#### Role 3: Product_and_UX_Reviewer
**Required ADRs:** `MASTER_02_DATA_PRIVACY_RETENTION.md`, `MASTER_04_MODULARITY_SHARED_SERVICES.md`, `MASTER_05_SWL_LIFECYCLE.md`, `MASTER_13_FRONTEND_UX.md`, `MASTER_14_LLM_CONTEXT_MANAGEMENT.md`, `MASTER_15_DOMAIN_IDENTITY.md`, `0074_User_Facing_Semantic_Anchors_and_Context-Sensitive_Help.md`, `0081_ui_testability_and_tour_friendly_design.md`
**Focus:** Architecture completeness, AI directive quality, UX consistency/attractiveness, documentation coverage, and testing quality.

---

### Phase 4: Final Validation

You (Ignatz) must run linters and tests in two stages:
**Stage 1: Iterative Scoped Validation**
During active batching, use `--files` to test only modified files and avoid legacy error scope creep:
1. `cd hams_open && python3 tools/run_linters.py --files <list_of_modified_files>`
2. `cd hams_com && python3 tools/run_linters.py --files <list_of_modified_files>`

**Stage 2: Final Global Sweep**
At the very end of the review process, run a full, global linter scan on the repository roots and methodically repair any legacy/unscoped errors:
1. `cd hams_open && python3 tools/run_linters.py`
2. `cd hams_com && python3 tools/run_linters.py`
3. `cd hams_open && python3 tools/test.py`
4. `cd hams_com && python3 tools/test.py`

Fix all linter complaints, then iteratively fix remaining problems until tests 100% pass.

**CRITICAL RULE - TEST INFRASTRUCTURE FAILURES:** If you are completely unable to run tests (e.g. test runner hangs or refuses to start), you MUST immediately inform the Orchestrator Agent using the `send_message` tool and QUIT. Do not attempt to bypass this.

---

### Crash Recovery & Checkpointing

You must persist your state to `review_status.md` after every batch. If restarted, you must:
1. Read `review_status.md` to determine which modules are already Completed.
2. Resume from the first module still marked Pending or In Progress.
3. Skip all modules marked Completed.

---

### Prioritization & Conclusion

Before concluding the review (e.g. when approaching turn limits or context limits), you must prioritize:
1. Running linters in both repos and fixing any new violations.
2. Running full test suites in both repos.
3. Creating the final summary artifact for the user.

If resources are constrained, prioritize fixes by severity: **CRITICAL > ERROR > WARNING**. INFO-level findings should be documented in the implementation plan but not fixed under pressure.
