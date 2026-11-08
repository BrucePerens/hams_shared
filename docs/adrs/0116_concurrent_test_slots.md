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
1. **Opt in, default one.** `HAMS_TEST_SLOTS=N` or the first line of `/opt/hams/etc/test_slots` (in that order of
   precedence), N from 1 to 15. `--slots N` can only raise the count (amended 2026-11-08, see "Amendments"). Without the opt-in the count is 1 and every derived value equals the old one
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
4. **Refuse what cannot be made per slot, fail fast.** Above slot 1: `--mcp` (clients are configured for one server on
   8075, which a private namespace cannot offer to the outside), and, only for a run on the host (`HAMS_ISOLATED_NS=1` set
   by hand, no private namespace): `--pause-on-fail` (the harness pins Chrome's port 9222) and modules under test that
   mention RabbitMQ (`pika`, `amqp`, `rabbitmq` in their Python) while a RabbitMQ listens on 5672 (its queues would be shared,
   and a vhost cannot be created as the `odoo` user). The full pipeline, which is the supported way to run slots, gives
   every run its own network namespace, Redis and RabbitMQ on the same ports, and has neither host-only limit. The
   refusal names every reason, and a host-only reason says the run is on the host because `HAMS_ISOLATED_NS=1` is set and
   to leave it out.
5. **The wait queue stays one FIFO.** The queue directory is derived from the base lock path. A ticket named by a live
   holder record, or whose process has one, is a running run; the oldest waiters, as many as there are free slots, may
   try the slot locks, so a slow or unlucky head never stops the others from taking the other free slots. With one slot
   the head rule is the old one (the lowest live ticket, running holder included), so `lint_lock.py` and default runs
   wait exactly as before.
6. **The slot travels in the command line.** The invoking process appends `--slots N --slot-held K` to the command it
   re-execs (sudo, systemd-run, unshare). The root phase takes back only slot K, with the ticket it adopted, and the
   final test process takes the database and file names from it. An environment variable might not survive a layer,
   and a child that lost its slot would have taken slot 1.
7. **Visible.** Holder records carry the slot and ticket in every mode; `--queue-status` lists each slot; a run prints the slot,
   database, port and Redis database it uses.

8. **A run in slot mode removes what it owns (amended 2026-11-08).** On a machine with more than one slot, every run, in
   any slot including 1, drops its database WITH (FORCE), removes its Odoo filestore and its slot TMPDIR when it ends,
   whether it passed, failed or was stopped (exit hook, SIGTERM and SIGHUP handlers; in the full pipeline the root phase
   repeats the drop after the test process, which may have been killed outright). `--keep-db` keeps all three and prints
   the database name last, for triage. The database is marked with a PostgreSQL comment
   (`hams_test_runner:v1 slot=K kept=0|1`) written when it is created, and only a database whose marker names the run's
   slot and is not kept is dropped. Before building anything, every run in slot mode sweeps the leftovers of runs that
   are gone (SIGKILL, power cut, an older `test.py`), slot by slot, only while it holds that slot's flock and no live
   holder record names it; it refuses on a production host and never touches `hams_prod`, a kept database, or a name
   that is not this runner's. With one slot nothing changes: `hams_test` is kept and rebuilt by the next run.
   Reference: "Clean-up in slot mode" in `tools/README.md`.

## Amendments, 2026-11-08 (Bruce: "remove all single-slot-only issues from the test pipeline")
- **Queue head-of-line block.** A run started with one slot wrote its holder record without its ticket, so a slotted
  waiter counted the running holder's ticket as the oldest waiter and never tried although three of four slots were
  free. Holder records now always carry slot and ticket, a ticket whose process has a holder record is a running run
  even if the record lacks the ticket (older `test.py`), and the waiters at positions below the number of free slots
  may try.
- **A full-pipeline run was invisible to the queue.** The root phase (`--internal-ns-init`) ran the queue code inside
  its new PID namespace, where the invoking process's pid is not visible: its ticket was deleted as dead and the ticket and
  holder record it wrote carried namespace pids that the host deleted as dead, so `--queue-status` showed 0 slots busy
  with four running. The root phase now only takes back its slot's flock (`take_back_slot_lock`) and writes nothing in the queue
  directory; the invoking process's ticket and holder record (its pid lives as long as `sudo`) stay valid for the whole run.
  A slot named by a live holder record is also not taken by another run while its flock is momentarily free (the exec
  window in which two runs once shared a slot).
- **A run kills only its own Chrome.** `pkill -u odoo -f <pattern>` in `test.py` and the `zero_sudo` harness's reap of "every
  headless Chrome of this uid" (written when one run per machine was the rule) killed the browsers of other runs: a run in its
  own PID namespace is visible in the host's process table, and a slot 1 run's pattern matched its profile directories. Tours of
  full-pipeline runs died with exit code 9 (`CancelledError` in `Page.navigate`, or a stalled step). Both now signal only
  descendants of the calling process and orphans whose `--user-data-dir` is a directory in the caller's own view of the
  filesystem (`chrome_processes_of_this_run`, `_headless_chrome_of_this_run`).
- **The namespace init reaps.** The root phase is PID 1 of the run's PID namespace and re-adopts every orphan; it never waited for
  them (150 to 170 zombies per run after hours). A reaper thread (`reap_orphans_forever`) reaps zombie children by pid, never
  `waitpid(-1)`, so the exit status of the test process is untouched.
- **The load gate waits for CPU saturation.** The one-minute load average read 25 to 50 on 32 mostly idle cores (hundreds of short-lived
  tasks), so the old gate (cores x ratio) held runs back with a slot free. It now holds only for a `Runner.Worker` or when the processors are
  at least 92% busy AND the kernel's `some avg10` CPU pressure is at least 25%; `HAMS_CI_LOAD_GATE_RATIO`, when set, still adds the old rule. Each
  run gates when it reaches the head of the queue, so several free slots gate concurrently. Reference: "The CI load gate" in `tools/README.md`.
- **Batches may run as many at once as there are slots.** A new test batch starts when fewer batches than the slot count run (ADR 0115 allowed one);
  when every running slot is busy new requests still coalesce into the forming batch. Attribution and splitting are per batch and unchanged. The queue
  state keeps its first running batch under the old key, so a leader of an older version sees one running batch.
- **One count per machine.** Every run reads `HAMS_TEST_SLOTS` / `/opt/hams/etc/test_slots`; `--slots` can only raise
  it. A run that asked for fewer slots than the machine has no longer takes slot 1 only and queues as if the others
  did not exist. To run with one slot, set `HAMS_TEST_SLOTS=1`.
- **The full pipeline is the supported way.** The RabbitMQ and `--pause-on-fail` limits exist only for a
  `HAMS_ISOLATED_NS=1` run, which shares the host's network; they are removed for the full pipeline (decision 4).
  `HAMS_ISOLATED_NS=1` keeps working, is not needed for normal runs, and its messages say so.
- **Batching (ADR 0115) composes with slots.** The batch leader is a `test.py` that holds no slot while the batch
  forms; its nested `test.py -u <union> --solo --wait` queues in the one FIFO and takes a slot like any run, while the
  members wait on the leader's verdict in the batch queue, which holds no slot or lock: no cycle. One combined run per
  repository and kind still runs at a time, so a request arriving during a running batch joins the next batch instead of
  taking a free slot. Not batched: `--solo`, `HAMS_TEST_BATCH=off`, a dirty tree, a hand-linked `hams_shared`,
  `-u mod:Class`, no `-u`, `--test-tags`, a mode other than standard, `--coverage`, `--mcp`, `--pause-on-fail`,
  `--offline-isolation`, `--profile`, `--build-daemons`, `--keep-db`, a working directory that is not a repository root.

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
  another slot (the full pipeline has no such collision); a full-pipeline run leaves behind only its log files, the
  shared `postgres` roles and the lock and queue files; the "possible browser leak" advisory counts browsers box-wide;
  the coalescing batch queue still runs one combined run per repository at a time; `remote_odoo_test.py` keeps its
  one-job-per-remote-host lock and so uses slot 1 there.
- The window in which a re-exec'd phase re-takes its parent's flock (the exec closes the descriptor; sudo closes it
  anyway) is now covered: another run skips a slot named by a live holder record, and the phase retries for 30 s
  (`HAMS_TEST_TAKE_BACK_SECONDS`). Only a run of an older `test.py` can still take the slot in the window; the phase then
  fails with the usual "Another instance" message.
- Raising N is a one-line change on the machine; it needs no code change and no restart.
