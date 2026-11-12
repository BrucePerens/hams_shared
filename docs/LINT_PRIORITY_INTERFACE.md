# Lint priority class for unblock work: the interface

Status: specification, 2026-11-08 (Bruce approved the design in chat). **For the agent that owns `tools/lint_lock.py`,
`tools/lock_queue.py` and `tools/lint_batch.py` (the merge-queue agent).** The load display already reads the marks described
here; nothing in `lint_lock.py`, `lock_queue.py`, `run_linters.py` or `lint_batch.py` honours them yet. This file is the contract to
implement. It deliberately adds no code to those files.

## Why

The lint lock is one FIFO (first-in, first-out queue) in front of N slots. When agents pile up, a lint run that unblocks everyone else (the fix for a red `main`, a
PR that others are waiting to rebase on, a merge-queue repair) waits behind routine runs for 10+ minutes. Unblock work needs to go
to the front of the *waiting* line. It never preempts a run that already holds a slot.

## How a run is marked (any one of these)

| Mark | Where | Who sets it |
|---|---|---|
| Environment `HAMS_LINT_PRIORITY=unblock` | the environment of the `run_linters.py` process (it reaches `lint_lock` and any child) | the agent or coordinator that starts the run; also settable as `run_linters.py --priority unblock`, which only sets this variable |
| A marker file named by process id: `~/.cache/hams-lint/priority/<pid>` | the pid of the queued run (the ticket's `pid`) | a coordinator, for a run already queued |
| A marker file named by branch: `~/.cache/hams-lint/priority/<branch with "/" written "__">` (for `night-shift/x` the file is `night-shift__x`) | the branch checked out in the worktree the ticket names (first tab-separated field of its `info`) | a coordinator, before agents start linting that branch |

The value `unblock` is the only one defined; any other value of the variable is not priority (an unknown value must not raise).
Marker files are empty; only their existence matters. A pid marker is ignored when its process is dead. A branch marker is
ignored when its mtime is more than 12 hours old (a forgotten marker must not hold the front forever); the coordinator removes
it when the branch has merged. The directory is created by whoever writes the first marker; a missing directory means no marks.

The load display (`/dev/shm/loadscreen/stats.json`, field `queues.lint_priority_waiting`, and the QUEUES panel) already counts waiting runs
marked this way using exactly these rules, reading `/proc/<pid>/environ` of the waiting process for the variable.

## What the lock must do

1. **Ordering.** Among *waiting* tickets, every priority ticket is ahead of every non-priority ticket; within each class order stays FIFO
   by ticket number. A ticket whose run already holds a slot is not waiting and is never moved. No preemption, no killing, no signal.
2. **Where.** In `lock_queue.may_try(qdir, tickets, ticket, slots=1, is_priority=None)` and `lock_queue.acquire(..., is_priority=None)`: add the
   optional callable `is_priority(ticket_dict) -> bool` (the ticket dict has `name`, `seq`, `pid`, `start`, `queued_at`, `info`). The waiting
   list is sorted by `(not is_priority(t), seq)` before the head test. With `None` (what `test.py` passes) behaviour is unchanged.
3. **The lint side.** `lint_lock` supplies `lint_lock.is_priority_ticket(ticket, environ_of_pid=None, directory=None, now=time.time)` (new function,
   needs an anchor under ADR 0113 and a test that cites it) implementing the table above, and passes it to `acquire` / `may_try`.
   It must never raise: any read error means "not priority".
4. **Starvation guard.** A non-priority ticket that has waited more than 30 minutes is treated as priority for ordering purposes (so a
   stream of unblock runs cannot hold routine work back forever). Make the number a named constant `PRIORITY_STARVATION_SECONDS`.
5. **Batching** (`lint_batch.py`). A combined batch is priority when any member is. The batch worktree is a detached checkout, so the
   branch mark cannot be read from it: the batch leader must set `HAMS_LINT_PRIORITY=unblock` in the combined run's environment when any
   member (by env, pid mark or branch mark of the member's own worktree) is priority. A priority run should not wait out the batching window
   for companions: it closes the forming batch immediately.
6. **Status.** `run_linters.py --lock-status` and the waiting message (`waiting_message`) show `[priority]` on a priority ticket and the
   position within the sorted waiting list.
7. **Who may use it.** Only unblock work: the fix for a red main, the lint of a PR that other branches are waiting on, a merge-queue repair. Not
   "my change is important". `AGENTS.md` says so; the coordinator is responsible for marks and for removing branch marks.

## Tests the owner should add

- ordering: with tickets 1 (running), 2, 3, 4 waiting and 4 marked, the head after 1 releases is 4, then 2, then 3;
- a running ticket is never displaced by a later priority ticket;
- each of the three marks is recognised; unknown env values, a dead pid mark and an expired branch mark are not;
- the starvation guard promotes a 31-minute-old ticket ahead of a fresh priority ticket only by seq order among promoted tickets;
- `acquire` with `is_priority=None` behaves exactly as before (the existing `test_lock_queue.py` cases pass unchanged);
- a batch containing one priority member sets the variable for the combined run.

## Hand-off

Reported to the coordinator on 2026-11-08 together with this file. The display side is in place; nothing else is needed from this repository
until the lock honours the marks.
