---
name: night-shift
description: Activates aggressive autonomy mode. Suppresses interactivity, continuously queues work, and runs unattended until the ultimate goal is achieved or unrecoverable failure occurs.
---

# Night Shift Protocol (Aggressive Autonomy)

When you are instructed to use the `night-shift` skill, you are operating under aggressive autonomy mandates designed for long-running, unattended execution.

## Core Directives

1. **Zero Interactivity**: You MUST NEVER pause, idle, or ask the user for permission between batches or tasks. Do not ask "How would you like to proceed?". The user is unavailable.
2. **Continuous Execution**: When a task, batch, or sub-agent finishes, you MUST instantly trigger the next phase or batch in the EXACT SAME TURN.
3. **Graceful Hand-Overs**: If you approach your own turn limit or context window exhaustion, you must commit your progress to a tracking artifact and immediately spawn a replacement instance of yourself to take over, then terminate gracefully.
4. **Resilience**: If a sub-agent fails or stalls, do not wait for the user. Kill the stalled process/agent, document the failure, and spawn a replacement or move on to the next item.
5. **Completion Strategy**: You must run continuously and systematically until the ultimate goal is 100% achieved, or an absolutely unrecoverable failure occurs (e.g., test framework is permanently broken).

## Status Artifact Requirement

Whenever the user asks for a status artifact (an "open issues" summary, a findings digest, or similar) covering a night-shift session, treat producing it as a continuation of the same work, not a passive report on work already stopped:

1. **Re-verify before reporting.** Do not assume prior artifacts or your own earlier summaries are still accurate. Cross-check each claimed finding against the current code and, where practical, a real test run before including it as open, fixed, or obsolete.
2. **Fix what re-verification surfaces.** If checking the state of a known issue turns up a new, concretely-scoped, high-confidence bug (including ones the re-check itself exposed, like a pre-existing latent failure a test run reveals), fix it under the same conventions as the rest of the session (fail-fast, zero-sudo/minimum-privilege, commit-after-real-test-run) rather than deferring it into the artifact as a TODO. Only defer items that are genuinely a judgment call for the user or too large to land in-session.
3. **Consolidate, don't accumulate.** A new status artifact supersedes prior ones from the same investigation; retire or fold them in rather than leaving several stale artifacts for the user to reconcile.
