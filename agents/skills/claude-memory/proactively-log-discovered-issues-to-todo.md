---
name: proactively-log-discovered-issues-to-todo
description: "Bruce wants Claude to write down problems it notices while doing other work into the project's standing to-do file, rather than fixing them immediately (scope creep) or letting them go unrecorded."
metadata:
  node_type: memory
  pinned: false
  originSessionId: c0284e30-6ccb-4ad4-9b91-456b9fb13774
---

# Proactively log discovered issues to the to-do, don't drop them or chase them mid-task

Bruce Perens (K6BP) stated this as an explicit, standing policy during a hams.com/hams_open
night-shift session: "We should be proactive when we notice issues and put them in the to-do, to
solve when we have time or are done with the current task."

This came up after a verification step for an unrelated fix (running the repo's
`verify_anchors.py` documentation-coverage checker to confirm a new anchor/test pair was wired up
correctly) surfaced a large pre-existing backlog of unrelated documentation gaps across the repo,
and separately, a real bug noticed only in passing while trying to run an Odoo test suite manually
(a daemon module reads a required config value via `os.environ[...]` at import time with no
fallback, so any test run or tool invocation that doesn't already have that variable set crashes
outright). Neither belonged in the diff for the task actually being worked, but both were worth
keeping.

Apply this broadly, in any project with a durable to-do/backlog document (`night_shift_todo.md` in
this codebase): when a problem surfaces as a side effect of other work -- a lint failure, a crash,
a stale assumption, missing test coverage -- don't silently drop it, and don't necessarily stop the
current task to fix it either unless it actually blocks that task. Instead, add a clear, dated
entry to the standing to-do file describing what was found, where (file/line), and why it matters,
then continue the current task. This keeps forward progress on the primary task while ensuring real
findings are never lost.
