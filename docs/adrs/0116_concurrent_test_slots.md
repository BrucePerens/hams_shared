# ADR 0116: A Machine Can Opt In to Several Concurrent test.py Runs (Slots)

## Status
Accepted (Bruce, 2026-11-07: the dev box `server` has 32 cores and 125 GB and is "free for our exclusive use"; one
`test.py` run at a time left it idle).

## Scope
`tools/test.py`'s single-instance lock, the FIFO wait queue in front of it (`tools/lock_queue.py`), and every resource
two runs on one machine would share. The derivations are in `tools/runner_slots.py`; the reference is the "Concurrent
test slots" section of `tools/README.md`. The lint lock (`lint_lock.py`) is not changed.

## Context
`test.py` took one flock on `/tmp/hams_odoo_test_runner.lock` (2026-09-09, after two concurrent runs on the shared
database and ports killed each other's Chrome and corrupted results), so a machine ran exactly one run. The rule was
about shared state, not about concurrency as such: the full pipeline already gives each run a private mount, PID and
network namespace with its own Redis and RabbitMQ, and the only things the runs shared were the PostgreSQL server,
the host log directory and the host `/var/lib/odoo` tree.

## Decision
1. **Opt in, default one.** `--slots N`, `HAMS_TEST_SLOTS=N` or the first line of `/opt/hams/etc/test_slots` (in that
   order of precedence), N from 1 to 15. Without the opt-in the count is 1 and every derived value equals the old one
   (lock file, database, port 8075, Redis database 1, file names, Chrome patterns, command line of the re-exec'd
   phases). A bad value is an error (exit 2), never a quiet 1.
2. **One lock per slot; slot 1 is the original file.** A run takes the lowest free slot with a non-blocking flock on
   `<lock>` (slot 1) or `<lock>.slot-K`. Slot 1 keeps the original path so that a `test.py` without slots, or a machine
   that has not opted in, excludes a slotted run on slot 1 instead of sharing its database and ports.
3. **Everything shared is derived from the slot.** Database `<db>_sK` (and with it Odoo's filestore directory), log
   directory file names (`filtered_test.slot-K.txt`, `test_progress.slot-K.txt`, `coverage_report.slot-K`,
   `odoo_test.slot-K.pro`, `offline_ns.slot-K`, `log.slot-K`), and, for a run on the host, the HTTP port (8075 + K - 1),
   the Redis database (K), `TMPDIR` (`<TMPDIR>/slot-K`) and the patterns that kill and clean up that run's Chrome
   (`pkill -f _chrome_odoo` matched every slot's browsers). In the full pipeline the network namespace already makes
   ports, Redis, RabbitMQ and the process table private, so nothing but the database and the file names changes there.
4. **Refuse what cannot be made per slot, fail fast.** Above slot 1: `--pause-on-fail` (the harness pins Chrome's port
   9222), `--mcp` (clients are configured for 8075), and a run on the host whose modules under test mention RabbitMQ
   (`pika`, `amqp`, `rabbitmq` in their Python) while a RabbitMQ listens on 5672: its queues would be shared, and a vhost
   cannot be created as the `odoo` user. Modules that never touch the broker run in any slot. The full pipeline starts a
   private RabbitMQ and has none of these problems. The refusal names every reason and says what to do.
5. **The wait queue stays one FIFO.** The queue directory is derived from the base lock path. A ticket named by a live
   holder record is a running run; the head of the queue is the oldest waiter and may try the slot locks while fewer
   than N slots are busy, so the head takes the first free slot. With one slot the head rule is the old one (the lowest
   live ticket, running holder included), so `lint_lock.py` and default runs wait exactly as before.
6. **The slot travels in the command line.** The invoking process appends `--slots N --slot-held K` to the command it
   re-execs (sudo, systemd-run, unshare). The root phase takes back only slot K, with the ticket it adopted, and the
   final test process takes the database and file names from it. An environment variable might not survive a layer,
   and a child that lost its slot would have taken slot 1.
7. **Visible.** Holder records carry the slot and ticket; `--queue-status` lists each slot; a run prints the slot,
   database, port and Redis database it uses.

## Related fix in the same change
A run in which no test ran used to end with "No test failures or system crashes detected" (Odoo died on "Port 8075 is
in use" or a failed database login, so no error block was captured), and an Odoo that exited 0 without a verdict counted
as a pass. `exit_status_for_run` returns the child's non-zero status, or 1 when there is no `odoo.tests.result:` line,
and the closing banner says `TEST RUN FAILED: no tests ran`.

## Consequences
- Throughput on an opted-in machine rises to N runs; the CI load gate and the OOM watchdog still bound each run
  (9 GB per run in the full pipeline).
- Known limits, each stated in the README: a flat `HAMS_ISOLATED_NS=1` run shares the host's Redis server (one database
  per slot), PostgreSQL roles and the host's ports, so a test that binds a fixed port collides with the same test in
  another slot (the full pipeline has no such collision); the "possible browser leak" advisory counts browsers box-wide;
  the coalescing batch queue still runs one combined run per repository at a time; `remote_odoo_test.py` keeps its
  one-job-per-remote-host lock and so uses slot 1 there.
- The window in which a re-exec'd phase re-takes its parent's flock (the exec closes the descriptor; sudo closes it
  anyway) existed before slots; a run that grabs that slot in the window makes the phase fail with the usual
  "Another instance" message.
- Raising N is a one-line change on the machine; it needs no code change and no restart.
