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

## Anti-Summation-Bias Pre-Commit Hook

A pre-commit hook flags commits where a file's size dropped sharply (e.g. "SUMMATION BIAS DETECTED... file size reduced by X%") as a heuristic guard against an AI quietly dropping rules, nuance, or logic while claiming to just "clean up" a file. It does not understand refactors: moving logic to another file (e.g. extracting a controller's inline logic into a reusable model method) shrinks the original file the same way deleting the logic would, and the hook can't tell the two apart.

You are authorized to bypass this hook (`git commit --no-verify`) once you have personally verified the flagged reduction is a genuine refactor and not an actual loss of logic, rules, or nuance -- e.g. by confirming the removed code now lives elsewhere and by running the real tests that cover it. Always state plainly in your response to the user why the reduction is safe before or when you bypass the hook; the bypass is conditional on that verification and explanation, not a blanket license to skip the check.

## When the Review/Coverage Loop Also Runs Dry

If you've worked through the known task list AND a round of review-fix-coverage-review turns up nothing further worth doing, cross-browser testing is a standing next fallback: re-run this project's real browser-driven tests and manual verification passes (Playwright etc.) against engines other than the default Chromium -- Firefox and WebKit in particular -- since browser-specific bugs (API availability gaps, timing differences, rendering quirks) are a real, distinct bug class this project's test suite mostly exercises against one engine only.

You have standing authorization to install "major facilities" needed for real dev/test work during an unattended session -- not just small `apt-get` packages, but larger installs like additional browser engines, alternate language toolchains, or similar substantial dependencies -- without asking first. Report what you installed and why when you report the work.
