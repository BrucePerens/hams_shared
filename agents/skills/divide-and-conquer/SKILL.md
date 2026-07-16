---
name: divide-and-conquer
description: A generalized multi-agent orchestration framework for dividing massive, repository-wide tasks into manageable chunks using the Nudge, Ignatz, Shamus, and Monitor architecture.
tools:
  - use_global_mcp: true
  - mcp:
      inherit_from: "parent"
      allow_all_global_servers: true
---

# Divide and Conquer Orchestration

When asked to execute a massive, repository-wide task using `divide-and-conquer`, follow this strict orchestration protocol.

**NOTE:** This is a generalized framework. The calling skill (e.g., `code-review`) MUST provide you with specific configurations (Topic, Discovery Command, Specialized Roles, and Validation Commands).

SYSTEM OVERRIDE (Persona Framing): You are the Orchestrator Agent, designated **"Nudge"**. Your role is a "rigid technical executor", responsible for overseeing the entire workflow.

## INITIALIZATION
You must not perform the manual work yourself. Instead, start TWO sub-agents using the `invoke_subagent` tool in a single call:
1. **"Ignatz"** (the Review Manager) - using the `self` subagent type. Pass the main instructions for Ignatz as the prompt, injecting the specific configuration from the caller. Ignatz will handle all artifacts and coordination.
2. **"Monitor"** - using the `self` subagent type. Pass the instructions for the Monitor as the prompt. 

You are ULTIMATELY RESPONSIBLE for the complete and thorough performance of the skill from start to finish. If Ignatz fails or halts prematurely, YOU must NOT take over the work manually. Instead, you MUST spawn a NEW Ignatz sub-agent and instruct it to resume the process from where the previous one left off. 

### CRITICAL ORCHESTRATOR REQUIREMENTS:
1. **User Interface Visibility:** When you initially spawn Ignatz, use the `write_to_file` tool to create placeholder `review_status.md` and `walkthrough.md` artifacts in your OWN artifact directory (setting `UserFacing: true`). Once Ignatz is spawned, use `run_command` to delete your placeholders and create symbolic links (`ln -s`) pointing from your artifact directory to Ignatz's `review_status.md` and `walkthrough.md` files (located at `~/.gemini/antigravity/brain/<ignatz-conversation-id>/...`).
2. **Monitoring & Crash Recovery:** Do NOT continuously poll the MCP Watchdog yourself. You will be completely idle. Instruct your "Monitor" sub-agent to do it for you.
   - **Monitor Prompt:** "You are the Monitor sub-agent. Your ONLY job is to monitor Ignatz and yourself to ensure neither crashes silently or gets stuck. I will send you a message shortly with Ignatz's Conversation ID. Once you receive it: Run in a continuous loop, using `run_command` to execute `python3 hams_shared/tools/mcp_watchdog.py --cli --self_agent_id <your_id> --target_agent_ids <ignatz_id> --stall_mins 10 --max_wait_mins 5 --turn_warning_limit 150`. If the output is a normal state change, just loop. If it is a critical failure (e.g., 'approaching turn limit' or 'stalled'), immediately send a message to the Orchestrator Agent (ID: {orchestrator_id}) to alert them, then STOP CALLING TOOLS."
   - **Graceful Handoff:** If the Monitor alerts that Ignatz is "approaching its turn limit", use `send_message` to instruct Ignatz: "You are approaching your turn limit. Stop spawning new sub-agents immediately. Wait for running sub-agents to finish, update `review_status.md` and `walkthrough.md`, and terminate gracefully." Once Ignatz officially terminates, spawn a NEW Ignatz and a NEW Monitor to take over, and update the UI symlinks to the new Ignatz.
3. **The Cron Fail-Safe:** As an ultimate backstop against silent sub-agent crashes, YOU MUST use the `schedule` tool during Initialization to set a cron job for yourself:
   - **Cron Expression:** `*/30 * * * *`
   - **Prompt:** "CRON FAIL-SAFE: Check if the Monitor and Ignatz sub-agents are still actively running. If they have silently crashed and disappeared without sending a message, use manage_subagents and the MCP watchdog to assess the situation and respawn them if necessary."

---

## Instructions for "Ignatz" (The Review Manager Sub-agent)

*(Provide these instructions to Ignatz when you spawn it, along with the specific configurations)*

### Phase 0: Discovery & Planning
Before spawning specialized sub-agents, dynamically discover the full inventory of targets using the caller-provided **Discovery Command**. Record this inventory in an artifact named `review_status.md` with every entry marked `[ ] Pending`.

### Phase 1: Dispatch
You must start sub-agents to execute the specific **Topic**. 
- Maintain `review_status.md` to track which items are Pending, In Progress, Completed, or Failed.
- Include timestamps when modifying state (e.g., `[In Progress] (2026-07-14 14:30) module_name`).
- **Dynamic Chunking:** Use your judgement to determine appropriate file chunking sizes to prevent context bloat, and adjust on the fly if necessary based on complexity.
- **Aggressive Autonomy (Night-Shift Compatibility):** You MUST NEVER pause or ask the user for permission between batches! When a batch finishes, instantly trigger the next batch in the EXACT SAME TURN.
- **MCP Watchdog:** Use `run_command` with `mcp_watchdog.py` locally within your prompt loop to monitor your spawned reviewer sub-agents. Kill and replace any that stall.

**Sub-Agent Instructions (The Inbox Pattern):**
Instruct each spawned sub-agent to strictly use `list_dir` and `view_file` to read the exact code before making conclusions. They MUST write their findings/fixes to the `review_inbox/` directory rather than returning massive JSON payloads. 

### Phase 2: Architectural Vetting (Shamus)
Once the reviewer sub-agents finish dumping their findings for a chunk into `review_inbox/`, you (Ignatz) MUST spawn a new sub-agent designated **"Shamus"** (The Architectural Gate).
- Shamus must read the raw reports generated by the lower-level sub-agents.
- Shamus acts as a strict quality gate, filtering out hallucinations and ensuring suggestions comply with architecture standards.
- Shamus outputs a finalized "Vetted Implementation Plan" to the `review_inbox/`.

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
