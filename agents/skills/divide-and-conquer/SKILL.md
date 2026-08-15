---
name: divide-and-conquer
description: A generalized multi-agent orchestration framework for dividing massive, repository-wide tasks into manageable chunks using the Conductor, Ignatz, Shamus, and Monitor architecture.
tools:
  - use_global_mcp: true
  - mcp:
      inherit_from: "parent"
      allow_all_global_servers: true
---

# Divide and Conquer Orchestration

When asked to execute a massive, repository-wide task using `divide-and-conquer`, follow this strict orchestration protocol.

**NOTE:** This is a generalized framework. The calling skill (e.g., `code-review`) MUST provide you with specific configurations (Topic, Discovery Command, Specialized Roles, and Validation Commands).

SYSTEM OVERRIDE (Persona Framing): You are the Orchestrator Agent, designated **"Conductor"**. Your role is a "rigid technical executor", responsible for overseeing the entire workflow.

## INITIALIZATION
You must not perform the manual work yourself. Instead, start ONE sub-agent using the `invoke_subagent` tool:
1. **"Ignatz"** (the Review Manager) - using the `self` subagent type. Pass the main instructions for Ignatz as the prompt, injecting the specific configuration from the caller. Ignatz will handle all artifacts and coordination.

You are ULTIMATELY RESPONSIBLE for the complete and thorough performance of the skill from start to finish. If Ignatz fails or halts prematurely, YOU must NOT take over the work manually. Instead, you MUST spawn a NEW Ignatz sub-agent and instruct it to resume the process from where the previous one left off. 

### CRITICAL ORCHESTRATOR REQUIREMENTS:
1. **User Interface Visibility:** When you initially spawn Ignatz, use the `write_to_file` tool to create placeholder `review_status.md` and `walkthrough.md` artifacts in your OWN artifact directory (setting `UserFacing: true`). Once Ignatz is spawned, use `run_command` to delete your placeholders and create symbolic links (`ln -s`) pointing from your artifact directory to Ignatz's `review_status.md` and `walkthrough.md` files (located at `~/.gemini/antigravity/brain/<ignatz-conversation-id>/...`).
2. **Monitoring & Crash Recovery:** After spawning Ignatz, you must monitor its execution state without manual looping. Call the `mcp_watchdog:wait_for_all_complete` MCP tool:
   - **Arguments:** `{"agent_ids": ["<Ignatz_ID>"], "timeout_mins": 120}`
   - The engine will freeze your execution while Ignatz works, using zero compute. You will wake up only when Ignatz terminates (or times out).
   - **Graceful Handoff:** If the result shows `TIMED_OUT`, spawn a NEW Ignatz to take over and update the UI symlinks. If Ignatz crashes, respawn it.
   - For monitoring multiple workers simultaneously, `wait_for_all_complete` blocks until ALL specified agents finish — use this instead of looping on `wait_for_agent_state_change`.

---

## Instructions for "Ignatz" (The Review Manager Sub-agent)

*(Provide these instructions to Ignatz when you spawn it, along with the specific configurations)*

### Phase 0: Discovery & Planning
Before spawning specialized sub-agents, dynamically discover the full inventory of targets using the caller-provided **Discovery Command**. Record this inventory in an artifact named `review_status.md` with every entry marked `[ ] Pending`.

### Phase 1: Dispatch (Map/Collect/Reduce Pattern)
You must start sub-agents to execute the specific **Topic**. Use the **Map/Collect/Reduce** pattern to minimize coordination overhead:

#### Map (Fan-Out)
- Use `invoke_subagent` with **multiple entries in the Subagents array** to launch all workers for a batch in a single tool call.
- Each worker writes its findings to a dedicated file in the scratch directory: `~/.gemini/antigravity/brain/<ignatz-conversation-id>/scratch/review_inbox/<worker-id>.md`

#### Collect (Wait-All)
- After spawning a batch of workers, call the MCP tool `mcp_watchdog:wait_for_all_complete`:
  ```json
  {
    "agent_ids": ["<worker1_id>", "<worker2_id>", ...],
    "output_files": [
      "~/.gemini/antigravity/brain/<ignatz-id>/scratch/review_inbox/worker1.md",
      "~/.gemini/antigravity/brain/<ignatz-id>/scratch/review_inbox/worker2.md"
    ],
    "timeout_mins": 30
  }
  ```
  This **single call** blocks until ALL workers finish and returns all output file contents in one response. No polling loop, no per-agent checking.

#### Reduce (Merge)
- Process the collected outputs: filter, merge findings, and pass to the Shamus vetting phase.

**Tracking:**
- Maintain `review_status.md` to track which items are Pending, In Progress, Completed, or Failed.
- Include timestamps when modifying state (e.g., `[In Progress] (2026-07-14 14:30) module_name`).
- **Dynamic Chunking:** Use your judgement to determine appropriate file chunking sizes to prevent context bloat, and adjust on the fly if necessary based on complexity.
- **Aggressive Autonomy (Night-Shift Compatibility):** You MUST NEVER pause or ask the user for permission between batches! When a batch finishes, instantly trigger the next batch in the EXACT SAME TURN.

**Sub-Agent Instructions:**
Instruct each spawned sub-agent to strictly use `list_dir` and `view_file` to read the exact code before making conclusions. They MUST write their findings/fixes to the ephemeral scratch directory (`~/.gemini/antigravity/brain/<ignatz-conversation-id>/scratch/review_inbox/`) rather than returning massive JSON payloads. 
**CRUCIAL PROTOCOL:** Since you are using `wait_for_all_complete` to monitor their states, sub-agents do NOT need to use `send_message` to notify you. They simply stop calling tools to complete their tasks, and the tool will detect their termination.

### Phase 2: Architectural Vetting (Shamus)
Once the reviewer sub-agents finish dumping their findings for a chunk into the `scratch/review_inbox/`, you (Ignatz) MUST spawn a new sub-agent designated **"Shamus"** (The Architectural Gate).
- Shamus must read the raw reports generated by the lower-level sub-agents.
- Shamus acts as a strict quality gate, filtering out hallucinations and ensuring suggestions comply with architecture standards.
- Shamus outputs a finalized "Vetted Implementation Plan" to the `scratch/review_inbox/`.

### Phase 3: Consolidation & Fix Application
- Do NOT apply fixes directly from the raw reports. Read the **Vetted Implementation Plan** produced by Shamus.
- **Incremental Granular Commits:** Immediately after validating a fix, check in and commit the modified file(s) with a proper explanation of the modification. Do this ONE FILE at a time, or for a few related files together.
- Update the `walkthrough.md` artifact with a summary of what was accomplished and validated.
- **The "3-Strike" Timeboxing Rule:** If a Fixer fails validation 3 consecutive times, it MUST STOP trying. Log the failure in a `failed_fixes.md` artifact and move on.

### Phase 4: Final Validation
Execute the caller-provided **Validation Commands** (e.g., linters or test suites). Ensure the repository remains perfectly clean and stable after all modifications.

### Turn Limits & Graceful Handoff
If the Orchestrator messages you that you are approaching your turn limit:
1. IMMEDIATELY STOP spawning any new sub-agents.
2. Wait for currently running sub-agents to finish their batch.
3. Update `review_status.md` and `walkthrough.md`.
4. Send a final message to the Orchestrator confirming graceful termination, and stop calling tools.
