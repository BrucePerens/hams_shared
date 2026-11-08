---
name: process-watching
description: >-
  How to wait for a process, watch a long job and stop something on the shared dev box without the mistakes that
  crashed the shared PostgreSQL cluster (2026-11-08): no `pgrep -f`/`pkill`/`kill -9`, no waiting on your own shell,
  no pid lists that outlive their process. Use agent_procs.py (find, wait, run, jobs, stop, kill-pid). Triggers:
  wait for a process, wait for test.py, kill a process, stop a background job, pgrep, pkill, kill -9, background command.
---

# Watching and stopping processes (`hams_shared/tools/agent_procs.py`)

Bruce, 2026-11-08: "make utilities for agents to watch processes and kill things, so that they don't make their common
mistakes like watching the watcher." A Bash-tool guard hook (`tools/claude_hooks/guard_signals.py`, wired in
`.claude/settings.json`) now DENIES the commands below and tells you to use this tool. Full reference with every function:
`tools/README.md`, "Watching and stopping processes (`agent_procs.py`)".

## The four mistakes, and what to do instead

| Mistake | Why it fails | Do this |
|---|---|---|
| `kill -9 $(pgrep -f "[x]yz")` | The pattern matched a PostgreSQL backend (its title holds the database name); SIGKILL of one backend crashed the whole shared cluster (about 100 minutes lost). The `[x]yz` trick does not help. | `agent_procs.py find` to look, `stop NAME` or `kill-pid PID --start-time T` to stop. No SIGKILL by hand. |
| `until ! pgrep -f "tools/test.py"; do sleep 3; done` | Your `bash -c '...'` holds the pattern, so the search matches your own shell and the loop never ends (or the kill takes your shell down, exit 144). | `test.py --wait`, or `agent_procs.py wait ...`; `find` never lists itself, its ancestors or descendants. |
| A pid from an earlier command | Pids are reused; the number may now be a different process. | Name a process by pid AND start time (`ticks=` in `find` output); `wait` and `kill-pid` check it. |
| Polling a background output file | You learn nothing about whether the process ended or failed. | `run NAME -- cmd` then `wait --job NAME` (exit code), or `wait --file F --contains MARKER --while-pid PID`. |

## How to wait for a process

```bash
T=hams_shared/tools/agent_procs.py
# A job you started: its exit code decides (0 ok, 1 failed or killed, 75 timeout).
python3 $T run build --tag "$AGENT" -- nice -n 10 cargo build 2>&1 | tail -1
python3 $T wait --job build --timeout 1800

# A process somebody else started: find it once, then wait on its exact pid and start time.
python3 $T find --cmd-contains tools/test.py          # prints pid=... ticks=...  (never lists this command)
python3 $T wait --pid 4242 --start-time 98765 --timeout 3600     # 0 when it ended OR the pid now belongs to another process

# An output marker, giving up if the producer dies first.
python3 $T wait --file /tmp/job.log --contains 'RESULT=' --fail-contains 'FATAL' --while-pid 4242 --timeout 900

# A loop is fine when the check is the tool (the shell holding the text is excluded as the searcher's ancestor):
until ! python3 $T find --quiet --cmd-contains tools/test.py; do sleep 5; done
```

For the shared Odoo test runner do not wait at all: `python3 hams_shared/tools/test.py --wait -u MODULES` queues you.
Use the Bash tool's `run_in_background` for long commands and wait for its completion notice instead of polling.

## How to stop a job

```bash
python3 $T jobs                          # name, state (running, exited, lost), pid, tag, cwd, command
python3 $T stop build                    # prints each process, SIGTERM to the job's process group, waits 10 s
python3 $T stop build --force            # only if it ignored SIGTERM: SIGKILL, and only to that verified group
python3 $T kill-pid 4242 --start-time 98765     # one exact pid that is not a job: refused unless identity is proven
```

`stop` signals only a NAMED job that `run` started, after checking that the processes found in the group really belong to the
record (process group and session). `kill-pid` takes one pid, never a list, and needs `--start-time` (from `find`), a job
record, or `--cwd-under` your own worktree. `kill -TERM <literal pid>` and `kill <literal pid>` still work in the shell: print
`ps -o pid,user,lstart,args -p PID` first and look at what you are about to signal.

## What is refused, always (exit 3, there is no flag)

Processes of another user, root or `postgres`; anything whose command contains postgres, postmaster, redis-server, rabbitmq,
beam.smp, epmd, systemd, sshd, gdm, gnome or claude-desktop; the Claude application; a `test.py` run that is not in your own
worktree; a chrome of a test run that is not under your own `TMPDIR`; pid 1; your own shell and its ancestors. The one
exception, `--i-am-the-owner-and-checked`, exists only for your own job records (a job whose command legitimately contains one
of those words). If you think you must signal a protected process, stop and tell Bruce.

## The rule for briefs that hand work to other agents (paste into a brief)

> Never find processes by a command-line pattern and signal the result: no `pgrep -f`, `pkill`, `killall`, `kill -9`, no
> `kill $(...)`, no `xargs kill`, and never wait with `until ! pgrep -f ...` (the waiting shell matches itself). Start long
> work as `python3 hams_shared/tools/agent_procs.py run NAME -- COMMAND`, wait with `agent_procs.py wait --job NAME` (or
> `--pid P --start-time T`, or `--file F --contains MARKER`), stop it with `agent_procs.py stop NAME`. Use `test.py --wait` to
> queue for the Odoo test runner. A protected process (PostgreSQL, Redis, RabbitMQ, systemd, sshd, another user's or another
> agent's process) is never yours to signal. The Bash guard hook denies the forbidden commands; a human may override it with
> `HAMS_ALLOW_SIGNALS=1` (logged to `~/.cache/hams-jobs/guard.log`); an agent must never set it.
