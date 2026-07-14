:L--
name: code-review
description: How to do a code-review.
---

# Code Review

When asked to do a code-review:

You are the orchestrating agent, designated **"nudge"**, responsible for overseeing the entire code‑review workflow.

Start a sub-agent called **"ignatz"** that will run the code-review process.
It must periodically report its progress to you. While "ignatz" is in charge of all artifacts, reporting to the user, and asking the user for review, you are ULTIMATELY RESPONSIBLE for the complete and thorough performance of the entire code-review skill from start to finish.

This means you are accountable for ensuring that ALL phases are completed, including Phase 3 (Final Validation), where tests must be run and a 100% pass rate achieved across both repositories. If "ignatz" fails, crashes, loses the thread, or halts prematurely, YOU must NOT take over the work manually. Instead, you MUST spawn a NEW "ignatz" sub-agent and instruct it to resume the process from where the previous one left off. Your role remains strictly to supervise until the job is successfully completed.

**CRITICAL ORCHESTRATOR REQUIREMENT:**
As **"nudge"**, you MUST use the `schedule` tool to set a timer for 5 minutes (e.g. `DurationSeconds=300`, `TimerCondition="<ignatz-conversation-id>"`) before ending your turn. This ensures you wake up every 5 minutes to actively supervise "ignatz". When you wake up, you must read `review_status.md` and check the timestamps for any item that is `[In Progress]` or `[Validating]`. If an item's timestamp is over 45 minutes old, you MUST send a message to nudge Ignatz to handle the stalled task. You must monitor "ignatz" continuously and verify its performance until it fully completes Phase 3 (running tests and achieving 100% pass).

---

## Phase 0: Module Discovery & Planning

Before spawning any review sub-agents, **"ignatz"** MUST dynamically discover the
full inventory of reviewable targets:

1. Run `find hams_open hams_com -name "__manifest__.py" -not -path "*/.git/*"` to discover all Odoo modules.
2. List all `daemon/` and `daemons/` subdirectories in both repos.
3. List `hams_shared/tools/`, `hams_shared/docs/`, and `hams_shared/agents/`.
4. Record this full inventory in `review_status.md` with every entry initially marked `[ ] Pending`.

**"ignatz"** MUST NOT rely on a hardcoded module count. The number of modules
changes as the codebase evolves.

---

## Phase 1: Review Dispatch

**"ignatz"** must start sub-agents at a rate it is comfortable with. For example: start
5 and then continuously start additional ones as the ones already started finish.
**"ignatz"** must maintain `review_status.md` to track which modules are Pending,
In Progress, and Completed, so it doesn't lose track of its queue when
processing incoming messages.

**State Persistence with Timestamps:** When marking a module as `[In Progress]`, `[Validating]`, `[Done]`, or `[Failed]`, **"ignatz"** MUST include the current timestamp (e.g., `[In Progress] (2026-07-14 14:30) module_name`). If **"ignatz"** observes a module has been in progress or validating for over 45 minutes, it must assume the worker sub-agent died or got stuck, mark the module as `[Failed - Timeout]`, and re-queue it or assign a new worker.

**CRITICAL: DO NOT STOP or wait for user approval between batches.** **"ignatz"**
must continue this cycle of spawning sub-agents until every module in the
inventory has been reviewed.

**CRITICAL: AVOID GHOST SUBAGENTS HANGING THE SYSTEM.** Subagents frequently crash or finish silently without sending a message back to you. If you go idle waiting for them, you will wait forever.
Therefore, you MUST follow this protocol EVERY SINGLE TIME you go idle to wait for subagents:
1. You MUST use the `schedule` tool to set a timer for 2 minutes (e.g. `DurationSeconds=120`, `TimerCondition="any"`) before you finish your turn.
2. Every time you wake up, you MUST use the `manage_subagents` tool with `Action="list"` to check if the subagents you are waiting for are actually still running.
3. If a subagent you are waiting for is NO LONGER in the `manage_subagents` list, it is a "ghost" (it died or finished silently). You must immediately use `manage_subagents` to `kill` any remaining stuck components, check its logs if necessary, and either move its module to Phase 2/3 or restart the subagent.
Never assume a subagent will reliably message you back. Always trust `manage_subagents` to verify they are still alive.

### Tiered Sub-Agent Allocation and File Chunking

Not every module needs three separate sub-agents. Furthermore, large modules must be chunked to prevent context bloat. **"ignatz"** must use the following allocation:

* **Small modules** (fewer than 10 files): Spawn **one** sub-agent that performs all three review roles sequentially.
* **Large modules** (10 or more files): Do NOT assign the entire module to a single agent. Instead, **"ignatz"** MUST chunk the module into batches of **5-10 files maximum**. For each batch, spawn **three** specialized sub-agents (one per role).

This ensures no sub-agent ever reads more than 10 files, keeping their context windows clean and preventing hallucinations.

### Required Pre-Reading for All Sub-Agents

Each sub-agent MUST read the following before beginning its review:

**Skills and References:**
- `hams_shared/agents/skills/linter-compliance/SKILL.md`
- `hams_shared/agents/skills/project-experience/SKILL.md`
- `hams_shared/agents/skills/odoo-development/SKILL.md`
- `hams_shared/agents/skills/code-review/references/ai_antipatterns_guide.md`

**ADR Index:**
- `hams_shared/docs/adrs/README.md` — the master index of all Architecture
  Decision Records (52 lines). Every sub-agent must read this so it
  understands the full set of architectural mandates before reviewing code.

This prevents sub-agents from re-discovering known traps and ensures they
apply the project's established standards and architectural decisions.

### Preventing Sub-Agent Hallucinations & Communication Failures

When defining the initial prompt for each sub-agent, **"ignatz"** MUST include
strict anti-hallucination guardrails:

1. **Mandatory Tool Use**: Instruct the sub-agent to use `list_dir` to find
   actual files and `view_file` to read the exact code *before* drawing any
   conclusions.
2. **No Guessing**: Explicitly forbid the sub-agent from guessing file names,
   assuming code logic, or reporting issues without reading the source first.
3. **Strict Citation Format**: Require the sub-agent to quote the exact
   `File Path`, `Line Number`, and `Original Code Snippet` for every
   reported issue.
4. **Actionable Fixes**: Instruct sub-agents to not just report issues, but
   to provide the exact `TargetContent` and `ReplacementContent` strings
   needed to fix them. This allows **"ignatz"** to act as an automated patch
   applier rather than having to re-derive the fix.
5. **Acceptance of Perfection**: Tell the sub-agent that it is perfectly fine
   to return "No issues found." They should not invent bugs just to provide
   a finding.
6. **Report Delivery (Mandatory)**: Instruct the sub-agent that it MUST use the
   `send_message` tool to transmit its final markdown report back to you (the orchestrator).
   You MUST include your own Conversation ID in the prompt so the subagent knows what to use for the `Recipient` argument.
   Explicitly forbid it from simply ending its turn without sending the message, because you will NOT be notified if it does, and you MUST NOT parse its transcript manually to extract it. If a subagent finishes without using `send_message`, you must send it a message telling it to resend its report using the `send_message` tool.

### Severity Classification (Mandatory)

Every sub-agent MUST classify each finding with one of these severities:

| Severity | Meaning | Examples |
|----------|---------|---------|
| **CRITICAL** | Security vulnerabilities, data corruption, crash bugs | SQL injection, privilege escalation, unhandled crash |
| **ERROR** | Functional bugs, linter violations, test failures | Missing `limit=`, deprecated API, broken anchor |
| **WARNING** | Performance issues, code smells, missing tests | N+1 loops, untested code paths, poor naming |
| **INFO** | Style suggestions, documentation improvements, UX ideas | Better docstrings, UI polish, wording changes |

### Report Format

Sub-agents MUST structure their reports using the template in
`references/report_template.md` within this skill's directory. This ensures
consistent, machine-parseable output that **"ignatz"** can efficiently process.

### Handling `audit-ignore` and `burn-ignore` Tags

Sub-agents MUST NOT flag existing `audit-ignore-*` or `burn-ignore-*` tags as
violations if:
1. The tag is in a file under `zero_sudo/`, `tools/`, or test files
   (`tests/` directories).
2. The tagged code has a corresponding test proving the safety assertion
   (check the anchor linkage per the linter-compliance skill).

Only flag a bypass tag as a finding if the corresponding safety test is
MISSING or inadequate.

---

## Phase 2: Findings Consolidation & Fix Application

The sub-agents should suggest changes but not perform the fixes themselves.
**"ignatz"** must make the changes, to prevent collisions between sub-modules.
Fixes must be performed individually, not automated. Fixes must not simply
silence problems, they must be comprehensive fixes. For example: do not simply
tie a missing test anchor to a catch-all test, make a specific test if one is
missing.

### Deduplication

Before adding a finding to the implementation plan, **"ignatz"** must
deduplicate by `(file, line_number, issue_type)`. If two reviewers report
the same line, keep the finding with the higher severity and richer context.
Discard the duplicate.

### Incremental Implementation Plan

As sub-agents report, **"ignatz"** must incrementally build an implementation
plan, organized by severity (CRITICAL first, then ERROR, WARNING, INFO).

### Delegated Fix Application & Incremental Commits

Once all reviewers for a given module (or file chunk) have reported, **"ignatz"** MAY spawn a single "fixer" sub-agent to apply the approved fixes.
**Incremental Commits:** After successfully applying a batch of fixes and passing validation, **"ignatz"** MUST immediately run `git commit -m "Auto: applied code-review fixes for [module/chunk]"` to create a save point. This ensures work is never lost if a crash occurs.

### Test-Driven Development & Validation Workflow

When fixing logic, security, or structural bugs, **"ignatz"** (or its fixer sub-agent) MUST follow a strict TDD and validation workflow:

1. Add a test that specifically exercises the bug or vulnerability (it should fail before the fix).
2. Run the test to confirm it fails (Fail Fast).
3. Fix the issue in the source code.
4. **Linter-First Validation:** Run the fast AST/syntax linter (`python3 tools/run_linters.py`) on the *specific changed files* first. Do NOT run the expensive test suite until the linter passes perfectly.
5. Run the test suite to confirm the tests pass.
6. Mark the item in the implementation plan as fixed.

**CRITICAL RULE - NO DUMMY TESTS:**
When satisfying `[@ANCHOR]` requirements for linters (such as for `audit-ignore-sql`), you MUST write **real, functional tests**. You are strictly forbidden from creating "dummy" test functions (e.g., `test_dummy_anchors_satisfy_linter`) that just contain a list of anchor comments and a `pass` statement. Every anchor MUST be placed inside a real test that actively executes and verifies the specific code or SQL query being ignored. One anchor per real test.

**The "3-Strike" Timeboxing Rule:**
If a fixer sub-agent attempts to fix an issue but fails validation (linter or tests) 3 consecutive times, it MUST STOP trying. It must leave the file in its last (broken or partially fixed) state, log the failure in a `failed_fixes.md` artifact, and immediately move on to the next task in the queue.
If, and only if, the rest of the entire code-review process is completely finished and there is still ample time remaining in the shift (e.g., the user is still asleep for hours), the orchestrator may return to the items in `failed_fixes.md` and attempt to resolve them.

---

## Review Roles

**"ignatz"** must assign each review to one of three roles with the following
**non-overlapping** criteria:

### Role 1: Compliance_and_Quality_Reviewer

**Required ADRs** (read before reviewing):
- `MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md` — Agile workflow & anchor traceability
- `MASTER_12_QA_TESTING_MANDATES.md` — CI/CD and test verification
- `0073_fail_fast_dependency_resolution.md` — Fail-fast dependency mandates
- `0075_llm_dependency_contract_visibility.md` — LLM API contracts
- `0076_ui_tour_mandate_and_bypass_governance.md` — Tour mandate & bypass rules

**Review criteria:**
* Linter improvement suggestions that would help avoid issues found during
  review, improve code quality, Odoo 19 compatibility, and counter AI foibles.
* **Licensing**: Except for "radae", which is owned by someone else who
  determines its licensing, code in hams_com must indicate that it is
  proprietary and trade-secret, and copyrighted by Bruce Perens K6BP.
  Code in hams_shared and hams_open should be AGPL-3, again preserving the
  license of anything we don't own. Sub-agents must check for standard
  `SPDX-License-Identifier` headers or standard copyright comment blocks
  at the top of files to verify this.
* Odoo 19 compliance and repair of deprecated and removed coding patterns.
* AI issues (incomplete code, laziness, placeholder stubs, etc.)

### Role 2: Architecture_and_Security_Reviewer

**Required ADRs** (read before reviewing):
- `MASTER_01_SECURITY_ZERO_SUDO.md` — Zero-sudo & service account architecture
- `MASTER_03_EDGE_ROUTING_THREAT_MITIGATION.md` — Cloudflare, WAF, tarpitting
- `MASTER_06_DNS_CQRS.md` — DNS read/write isolation via RabbitMQ
- `MASTER_07_ZERO_DB_ARCHITECTURE.md` — Redis caching for ephemeral data
- `MASTER_08_CORE_ARCHITECTURE_PERFORMANCE.md` — Hybrid monolith-daemon, distributed cache
- `MASTER_09_API_INTEGRATIONS.md` — HMAC, idempotency, ethical crawling
- `MASTER_10_IDENTITY_ACCESS_CONTROL.md` — Proxy ownership, domain sandbox
- `MASTER_16_FINANCIAL_DATA_PROTECTION.md` — Defense-in-depth, SQL view masking
- `0083_multi_tenant_context_management.md` — `.with_company()` mandate

**Review criteria:**
* Security vulnerabilities and access control issues.
* Performance bottlenecks and resource leaks.
* Usage of SQL procedures or functions to perform SQL operations, with
  the goal of reducing database turn-around for an operation to just one
  turn, and to reduce latency.
* Latency, reduction of bandwidth utilization, and the possibility to
  reduce the number of turn‑arounds per operation **between** different
  systems. These systems include, but are not limited to: Odoo and its
  database, daemon processes and their databases, the central HAMS relay
  daemon with local relay daemons, the browser interacting with the
  hams.com site, and local relay daemons either directly or via the
  central relay daemon. Keep in mind that all of these components may be
  located halfway around the globe from one another.

### Role 3: Product_and_UX_Reviewer

**Required ADRs** (read before reviewing):
- `MASTER_02_DATA_PRIVACY_RETENTION.md` — GDPR erasure, geographic fuzzing
- `MASTER_04_MODULARITY_SHARED_SERVICES.md` — Shared services, anti-entanglement
- `MASTER_05_SWL_LIFECYCLE.md` — SWL sandbox & licensing progression
- `MASTER_13_FRONTEND_UX.md` — ARIA live-regions, OLED burn-in protection
- `MASTER_14_LLM_CONTEXT_MANAGEMENT.md` — AI agent rules & patching protocols
- `MASTER_15_DOMAIN_IDENTITY.md` — Identity verification, shadow profiles
- `0074_User_Facing_Semantic_Anchors_and_Context-Sensitive_Help.md` — Context-sensitive help
- `0081_ui_testability_and_tour_friendly_design.md` — Tour-friendly UI design

**Review criteria:**
* Architecture of the subsystem under review. If it seems incompletely
  thought out and has significant missing features, decide whether you
  can fix it, or if it should be brought to the attention of the user
  to redesign it.
* Quality and lack of conflict in AI directives and documentation —
  **"ignatz"** must put these as open questions for the user in the
  implementation plan, where the correct action is not clear.
* Appropriateness and completeness for use: hams_open is for hams and
  non-hams equally, hams_com is for hams and ham radio aspirants (SWLs),
  and we think first-responders and emergency services personnel as well
  as hams will be interested in ham_auxcomm_training. Modules that are
  mainly for use by system administrators are welcome to use
  system-administrator jargon.
* Attractiveness: The site must compel users to join hams.com and
  continue to use it. Thus, the features themselves must be ones that
  attract users, and you can improve them as necessary. The UI must be
  easy to use, visually attractive, and consistent across the entire
  system. Think about things that could be annoying to the operator as
  they are currently implemented, and try to improve them.
* Coverage and quality of testing.
* Coverage and quality of documentation in `data/*.html`.
* UI/UX consistency across the portal.

---

## Phase 3: Final Validation

**"ignatz"** must run linters and tests from the repository roots:

1. `cd hams_open && python3 tools/run_linters.py` — this also covers
   `hams_shared` since it is a subdirectory of `hams_open`.
2. `cd hams_com && python3 tools/run_linters.py`

Fix all linter complaints. Then run full tests in both `hams_open` and
`hams_com` and iteratively fix any remaining problems until the tests
100% pass.

---

## Crash Recovery & Checkpointing

**"ignatz"** must persist its state to `review_status.md` after every batch of
sub-agents completes. If **"ignatz"** is restarted (due to context exhaustion,
timeout, or any other failure), it must:

1. Read `review_status.md` to determine which modules are already Completed.
2. Resume from the first module still marked Pending or In Progress.
3. Skip all modules marked Completed.

---

## Time Management

**"ignatz"** must reserve the final 90 minutes of the session for:

1. Running linters in both repos and fixing any new violations.
2. Running full test suites in both repos.
3. Creating the final summary artifact for the user.

If time is constrained, **"ignatz"** must prioritize fixes by severity:
**CRITICAL > ERROR > WARNING**. INFO-level findings should be documented
in the implementation plan but not fixed under time pressure.
