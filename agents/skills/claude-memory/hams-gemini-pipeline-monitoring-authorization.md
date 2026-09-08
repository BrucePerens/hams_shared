---
name: hams-gemini-pipeline-monitoring-authorization
description: "Standing authorization for Claude to watch live Gemini ingestion runs on hams.com/hams_open via mcp_watchdog and message the Gemini orchestrator on its own initiative if it spots a problem."
metadata:
  pinned: false
---

The user explicitly authorized Claude to monitor live Gemini/Antigravity ingestion pipeline runs on hams.com/hams_open "fully autonomous, any time" -- Claude does not need to be told to start watching a run, and does not need to ask before messaging the Gemini orchestrator if it spots a real problem during a run it's watching. This was a direct, deliberate choice among three offered options (ask-first only, autonomous-only-once-told-to-start, fully autonomous), with the user picking the broadest one.

The mechanism is `hams_shared/tools/mcp_watchdog.py`, a FastMCP server Gemini's own Antigravity Conductor sessions already use (registered in `~/.gemini/antigravity/mcp_config.json`), which Claude registered for itself too via `claude mcp add mcp_watchdog --scope local -e RELOAD_TRIGGER=2 -- python3 -u /home/bruce/workspace/hams_open/hams_shared/tools/mcp_watchdog.py` (local scope: private to this user and project, not committed to git, so it doesn't change what other contributors' sessions can reach). MCP servers only connect at session startup, so this tool only becomes usable in a session started after that registration.

Two very different classes of tool live in mcp_watchdog.py, and the distinction matters operationally:

- **Read-only, safe to use freely**: `wait_for_agent_state_change` and the family-tree/transcript-parsing it's built on watch Gemini's own `~/.gemini/antigravity/brain/{agent_id}/.system_generated/logs/transcript.jsonl` files for changes via `pyinotify` -- detecting stalls, deaths, and state transitions without ever consuming or claiming anything. This is the actual "god's eye view" the user described, and is how Claude should watch a run.
- **Consuming/injecting, needs care**: `wait_for_inbox` pulls a message off a named queue -- if Claude calls this on the same queue Gemini's own Conductor is listening on, Claude could steal a message meant for Gemini. `send_ipc_message` writes to a queue with no built-in distinction between "this is a status report" and "this is a work item" -- whatever's listening treats it the same way. Claude should never use `wait_for_inbox` for passive observation (that's what the transcript-watching tools are for), and should only use `send_ipc_message` deliberately, to a queue Claude has confirmed is the actual orchestrator's inbox, when it has something real to report.

The user's plan was staged: first prove out pure read-only watching works and correctly distinguishes healthy from stalled/errored runs, before relying on the intervention (message-sending) half.
