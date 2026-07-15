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
