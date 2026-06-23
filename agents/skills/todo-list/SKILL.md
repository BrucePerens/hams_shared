---
name: "todo-list"
description: "Project management to-do list and priorities for ongoing work. Links to detailed proposals."
---

# Project To-Do List

This skill tracks ongoing to-dos, priorities, and coupling to proposal documents.
When adding a new to-do, use `write_to_file` (with Overwrite=True) to rewrite this SKILL.md with the updated list. Check off completed items.

## Current To-Dos

- [ ] Moderate and suspend user websites. (Priority: High)
  - Details: Make it possible for group websites to have violation reports, moderation, and suspension, and anything else we do to manage user websites.
  - Wait Condition: DO NOT start this until all test failures are resolved.
  - Linked Proposal: docs/proposals/website_moderation.md (Example)

## Instructions for the AI

1. Whenever the user requests adding something to the to-do list, append it here by rewriting this SKILL.md file.
2. Mark items as `[x]` when completed.
3. Keep track of priorities and dependencies.
