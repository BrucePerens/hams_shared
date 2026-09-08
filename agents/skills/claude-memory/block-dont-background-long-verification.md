---
name: block-dont-background-long-verification
description: "When running a long verification command (e.g. a real Odoo test suite), block on it in the same turn rather than backgrounding it and ending the turn."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-06T23:19:38.989Z
---

During a hams.com/hams_open night-shift session, a forked worker agent launched a long-running real
Odoo test-suite verification run (the mandatory, non-negotiable final step of an anchor-coverage
sweep) via `Bash` with `run_in_background: true`, then ended its turn with a message saying it would
"pick back up" once the run completed. The coordinator's correction, verbatim: "Your last report said
'The Odoo test suite is still running under the monitor; I'll pick back up when it reports
completion' -- but your turn ended before that completion actually arrived. Check right now whether
your test-server process is still running... Use a real blocking wait for any long-running command
rather than a backgrounded one you end your turn on."

The problem is not that background execution or notifications are unreliable -- they generally do
fire correctly. The problem is the pattern of ending a turn on the assumption that a background task
will resume the work later: it leaves an ambiguous, unverified gap between "I said I'd check back"
and an actual re-engagement with the real, current state of that process. Combined with the standing
"keep working until actually done" discipline, an agent that ends its turn mid-verification has, in
effect, silently paused a mandatory step and moved the burden of noticing onto whoever reads the
transcript next.

The fix, now general practice: when a task's own instructions call for running a real verification
command that takes real wall-clock time (a full test suite, a build, a long-running migration), issue
it as a blocking foreground call within the same turn -- either plain `Bash` without
`run_in_background` (using a generous `timeout`), or a poll loop (`until grep -q "DONE" logfile; do
sleep N; done`) run in the foreground -- so the turn does not end until the real result is in hand and
has actually been read. Reserve backgrounding for cases where the user explicitly wants to keep
chatting while something long runs in parallel, not for an agent's own mandatory internal verification
gate before it can honestly call a task done.
