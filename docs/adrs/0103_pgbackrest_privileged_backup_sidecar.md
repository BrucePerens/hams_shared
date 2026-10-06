# ADR 0103: pgbackrest Privileged Backup Sidecar

## Status
Accepted

## Context

`backup_management`'s on-demand/scheduled backup path (`backup.config.action_trigger_backup()` and the
pgbackrest engine branch of `backup_worker`'s own `execute_job()`) was designed to run `pgbackrest backup`
directly from `backup.worker.service`, the RabbitMQ-consuming daemon. That daemon runs as OS user `odoo`
under `ProtectSystem=strict`, `NoNewPrivileges=true` (MASTER_01 ADR section 6, "OS-Level Daemon
Restriction"). PostgreSQL's own data directory (`/var/lib/postgresql/18/main` on `hams1`) is `0700
postgres:postgres` by design, so a real `pgbackrest backup` genuinely cannot run as `odoo` at all --
confirmed live, 2026-10-01: `pgbackrest backup` as `odoo` fails with `unable to open file
'.../global/pg_control' for read: [13] Permission denied` then `[056]: unable to find primary cluster`.

Three privilege-separation options were considered for closing this gap (Bruce's own choice from among
them, 2026-10-01: "Run the pgbackrest subprocess as postgres specifically"):

1. A narrow POSIX ACL grant (`setfacl -m u:odoo:rX` on the PostgreSQL data directory, plus a default ACL) --
   narrowest in principle, but still a standing grant of read access to the live database cluster's own
   files to a network-facing, RabbitMQ-driven daemon.
2. Add `odoo` to the `postgres` OS group and loosen the data directory to `0750` -- broader than option 1,
   changes PostgreSQL's own default permissions.
3. **Run the pgbackrest subprocess specifically as `postgres`** -- Bruce's choice, with the scope: only that
   one subprocess call, not the whole `backup.worker.service` daemon.

Before implementing option 3, two things needed checking, not assumed:

- **Whether `sudo` from inside `backup.worker.service` could even work.** It cannot:
  `systemctl cat backup.worker.service` on `hams1` confirms `NoNewPrivileges=true` is set on the live unit.
  That directive exists specifically to block a process from gaining privileges it didn't start with --
  `sudo`'s setuid-root binary is exactly what it blocks. This isn't a policy preference to weigh; it's a
  kernel-enforced `PR_SET_NO_NEW_PRIVS` fact. A sudoers-rule approach (scoped `Defaults!/usr/bin/pgbackrest
  env_keep`, an exact-argv sudoers line) was drafted and discarded once this was confirmed -- it would never
  have run.
- **Whether the zero-sudo policy (MASTER_01 ADR, "The Service Account Pattern") applies here at all.** It
  doesn't, directly: that policy and its AST-enforced burn-list rule (`check_burn_list.py`'s `visit_Attribute`
  check for `.sudo` and its `getattr(..., "sudo")` obfuscation check, both gated on `self.is_odoo_module`) are
  about Odoo's own ORM `.sudo()` bypassing Access Control Lists and Record Rules -- a database-privilege
  concern. OS-level `sudo`/`runuser` in a subprocess call is a different mechanism entirely, and this
  codebase already uses it freely in tooling (`hams_shared/tools/infrastructure.py`,
  `provision.py`) and in exactly one existing production daemon-adjacent unit,
  `hams.db.local.backup.service`, which already runs `pg_dump` via `runuser -u postgres --` for the identical
  reason: PostgreSQL's own data directory permissions.

What MASTER_01 ADR section 6 ("OS-Level Daemon Restriction & Airgapped Spooling") actually prescribes for
this shape of problem is more specific than either "sudo is banned" or "sudo is fine": a sandboxed daemon
that needs a privileged, infrequent, filesystem-only operation does not call out to a privileged helper
directly -- it hands a request to a separate, unsandboxed, privileged sidecar via a spool directory, and
reads the sidecar's own result back the same way. The ADR's own worked example is SMART disk telemetry; this
is the same shape with the roles reversed (the privileged sidecar performs a *write*, not a read, and the
unprivileged daemon is the one polling for a result rather than being polled).

## Decision

**`backup_worker`'s `pgbackrest backup` operation delegates to a new privileged sidecar, never runs as
`odoo` and never calls `sudo`/`runuser` itself.**

1. **Spool directory**: `/opt/hams/backup_requests`, owned `odoo:odoo`, mode `0700` (same shape as the
   existing `/opt/hams/etc/keys` and `/opt/hams/etc/relay_cert_renew` entries in
   `hams_shared/tools/infrastructure.py`'s own `directories` list). `backup.worker.service` writes into it
   (now included in that unit's own `ReadWritePaths=`); the sidecar, running as root, can read it regardless
   of its `0700` mode -- root is not bound by permission bits.

2. **Request/result protocol** (`backup_management/daemon/main.py`'s `_run_pgbackrest_via_sidecar()`): writes
   `request-<job_id>.json` (the validated pgbackrest argv plus, only when storage is S3/B2, the two secret
   env vars `PGBACKREST_REPO1_S3_KEY`/`_SECRET` -- never written into this daemon's own log) atomically
   (write-then-rename), then polls for `result-<job_id>.json` up to a configurable timeout
   (`PGBACKREST_SIDECAR_TIMEOUT`, default one hour). A stale/abandoned request is removed on timeout so a
   late-finishing sidecar run can't resurrect a job already reported failed.

3. **The sidecar** (`backup_management/daemon/pgbackrest_sidecar.py`, deployed as
   `hams-pgbackrest-backup.service`): a `Type=oneshot` systemd unit, deliberately **not** sandboxed --
   matching `hams.db.local.backup.service`'s own established shape and its own comment on why
   (`ProtectSystem=strict` would forbid exactly the `/var/lib/postgresql/.../main` read this needs). It
   re-validates every request before acting on it (exact argv-prefix allowlist, stanza-name regex, an
   explicit allowlist of the only two env keys it will ever pass through) -- never trusting the other side of
   a privilege boundary, the same posture `backup_worker`'s own pre-existing `restore_cmd` branch already
   takes toward requests it didn't itself originate. It then runs `runuser -u postgres --
   pgbackrest backup ...`, writes the result (exit code + combined output) back into the spool directory
   (world-readable, `0644`, so the `odoo`-owned poller can read it), and deletes the request file immediately
   -- shortening the S3/B2 secret's lifetime on disk. It drains every pending request file on each run (not
   just whichever one triggered it), since it's a oneshot, not a long-running process.

4. **Trigger**: `hams-pgbackrest-backup.path` (`PathExistsGlob=/opt/hams/backup_requests/request-*.json`) --
   this codebase's first `.path` unit. `infrastructure.py`'s own systemd-unit-linking/enabling step, which
   previously only recognized `.timer` units as needing an explicit `systemctl enable` (beyond the bare
   symlink into `/etc/systemd/system/`), now treats `.path` the same way: both are `[Install] WantedBy=`
   activation units with the identical "linked but not enabled" footgun documented in that step's own
   2026-09-23 bug-fix comment.

5. **Scope, deliberately narrow**: only pgbackrest's `backup` operation is delegated
   (`_PGBACKREST_PRIVILEGED_OPS = ("backup",)` in `main.py`). `pgbackrest info` (read-only, used by
   `sync_snapshots`) already works fine as `odoo` -- confirmed against the real job log before this change --
   and stays on the direct-subprocess path. `pgbackrest restore` remains a manual admin operation, as it
   already was before this ADR; extending the sidecar to cover it is new work for later, not assumed here.

## Consequences

- `backup.worker.service`'s own sandbox (`NoNewPrivileges=true`, `ProtectSystem=strict`) is unchanged and
  untouched by this fix -- the privileged operation moved out of it entirely, rather than being carved an
  exception within it.
- No standing `sudo`/`runuser` grant exists for `odoo`; the only process that ever runs as `postgres` is the
  sidecar's own internal `runuser` call, itself gated by the sidecar's own re-validation of a request it
  receives from a lower-privileged process.
- The S3/B2 secret's exposure window is the time between `backup_worker` writing the request file and the
  sidecar consuming and deleting it -- bounded by how quickly `hams-pgbackrest-backup.path` reacts (expected:
  sub-second) rather than living in a systemd `EnvironmentFile=` for the sidecar's whole lifetime.
- This is the first `.path` unit in this codebase; the `infrastructure.py` linking/enabling step's fix
  generalizes to any future `.path` unit for free.
- A future privileged, infrequent, filesystem-only need elsewhere in this codebase has a second worked
  example to copy from beyond the SMART-disk-telemetry case MASTER_01 ADR section 6 already cites.

## Verification

- `backup_management/daemon/test_main.py` (`TestPgbackrestRequiresSidecar`, `TestRunPgbackrestViaSidecar`)
  and `backup_management/daemon/test_pgbackrest_sidecar.py` (`TestValidateRequestCmd`,
  `TestValidateRequestEnv`, `TestProcessOne`, `TestMainProcessesEveryPendingRequest`): real filesystem
  protocol exercised directly (no `subprocess` mocking in the protocol tests; `subprocess.run` mocked only in
  the sidecar's own privileged-exec tests, since the dev box doesn't run as `postgres`).
- Production end-to-end verification (`hams1`, after deploy): a real `backup.snapshot` row created from a
  triggered backup job, real bytes visible in `b2 file list --long hams-com-prod-backups`, and
  `sudo -u postgres pgbackrest --stanza=hams_prod check` passing (confirms WAL, PostgreSQL's write-ahead log, archiving is unaffected).
