---
name: kill-0-not-cross-user-liveness-check
description: "Use `ps -p $pid`, not `kill -0 $pid`, to check whether a process owned by another user is still running."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-08T14:24:04.355Z
---

`kill -0 $pid` is not a reliable way to check whether a process is still alive when that process is owned by a different user than the one running the check. As the `bruce` user, `kill -0` against a PID owned by `odoo` (or any other user) returns a permission-denied error, not "no such process" — but a naive bash liveness loop like `while kill -0 $pid; do sleep 1; done` treats any non-zero exit code the same way, so it reads "permission denied" as "process exited" and breaks out of the loop while the process is still very much running.

This caused a real, several-turn debugging detour on hams.com/hams_open: a background Odoo test-suite process running as `odoo` appeared to have exited (via a `kill -0`-based wait loop), which briefly looked like it might explain an unrelated test failure — when in fact the process was still alive and consuming 40%+ CPU, confirmed by `ps aux`. The failure being investigated turned out to be a real, separate production bug, and the false "process exited" reading from `kill -0` nearly caused that real bug to be misattributed to my own concurrent-process interference instead.

The fix is mechanical and generalizes to any cross-user process-liveness check on any project: use `ps -p $pid > /dev/null 2>&1` instead of `kill -0 $pid` in a wait/poll loop. `ps -p` correctly reports whether a PID exists regardless of which user owns it, so it doesn't produce false negatives when checking on a process started via `sudo -u <otheruser>`.
