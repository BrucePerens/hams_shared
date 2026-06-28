---
name: "todo-list"
description: "Project management to-do list and priorities for ongoing work. Links to detailed proposals."
---

# Project To-Do List

This skill tracks ongoing to-dos, priorities, and coupling to proposal documents.
When adding a new to-do, use `write_to_file` (with Overwrite=True) to rewrite this SKILL.md with the updated list. Check off completed items.

## Current To-Dos

- [x] Moderate and suspend group websites. (Priority: High)
  - Details: Make it possible for group websites to have violation reports, moderation, and suspension, and anything else we do to manage user websites.
  - Wait Condition: DO NOT start this until all test failures are resolved.
  - Linked Proposal: hams_shared/docs/proposals/website_moderation.md (Example)

- [ ] Implement a format-aware CSP editor.
  - Details: Add to content-security-policy a format-aware editor of the CSP. Allow addition of fields with a select menu for valid field names. Provide a select menu for field values where there is a constant selection. Provide a list editor allowing addition, modification, or deletion of values where the value is a list. Provide a one-button validity check on Google's CSP evaluator.

- [ ] Implement Winlink Form translation layer.
  - Details: Create mapping schema in `winlink_parser.py`. Update import/export logic to accurately translate Winlink JSON fields to Odoo's schema.

- [ ] Architect Winlink integration into the Rust relay daemon.
  - Details: Define protocol for sending/receiving Winlink B2F messages over WebSocket. Scope out how Pat and Direwolf/ARDOP will integrate into the relay daemon's architecture.
  - Linked Proposal: [WINLINK_RELAY_DAEMON_INTEGRATION.md](file:///home/bruce/workspace/hams_com/docs/proposals/WINLINK_RELAY_DAEMON_INTEGRATION.md)

- [ ] Add auto-update function to hams_relay_server.
  - Details: Implement a mechanism for the Rust relay daemon to automatically update itself when a new release is available.

## Instructions for the AI

1. Whenever the user requests adding something to the to-do list, append it here by rewriting this SKILL.md file.
2. Mark items as `[x]` when completed.
3. Keep track of priorities and dependencies.
