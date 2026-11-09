# HAMS Shared Tools

This directory contains various utility scripts and tools used for development, testing, linting, and infrastructure management within the `hams_open` project.

## Purpose
The scripts in this directory automate common development workflows and enforce project standards. This includes running tests (`test.py`), enforcing linter rules (`run_linters.py`), validating environments (`env_validator.py`), and managing infrastructure (`infrastructure.py`, `provision.py`).

## Usage
Tools are typically executed directly from the command line or invoked as part of a CI/CD pipeline or local development workflow. For example:
- Run tests: `python hams_shared/tools/test.py`
- Run linters: `python hams_shared/tools/run_linters.py`
- Check tracked external dependencies (ardopcf, mercury, pat, hamlib, plus `binary_manifest.json`'s entries) against their real upstream releases: `python hams_shared/tools/check_dependency_releases.py` (manifest: `dependency_watch.json`; wired into a weekly scheduled CI run, `.github/workflows/dependency-watch.yml`)

## Provisioning download cache
`infrastructure.py` fetches apt keys, the kopia release, wkhtmltopdf and the speech models through `provision_cache.py`, so repeated provisions and `test.py` runs do not re-download.
- Location: `~/.cache/hams_provision/` (non-root), `/var/cache/hams-provision/` (root), or `HAMS_PROVISION_CACHE`. Entries are `<sha256 of URL>/{meta.json,<sha256 of content>.blob}`, written by temp file + rename.
- Fresh for `HAMS_PROVISION_CACHE_MAX_AGE_DAYS` (default 7), then revalidated with `If-None-Match`/`If-Modified-Since`. If the network fails the cached copy is used with a warning (stale-if-error); with no copy the fetch fails.
- A MANIFEST entry with `sha256` / `sha256_by_arch` (kopia) is pinned: a cached copy is used with no network and a download with another hash is refused. Without a published hash (apt keys, wkhtmltopdf) the first observed hash is recorded and logged (trust on first use); a later change is **refused** with both hashes in the error, because a changed signing key must be reviewed. After reviewing, re-run with `provision.py --refresh-cache` (or `HAMS_PROVISION_REFRESH=1`) to accept it.
- Never cached: non-https URLs, credentials in the URL, credential-like query parameters.
- Entries marked `"apt_only": True` (or whose destination is under `/usr/share/keyrings/` or `/etc/apt/`) are skipped entirely when `skip_apt` is true, which is how `test.py` provisions, so a test run never contacts the apt key servers.
- To clear the cache, delete the directory.

## Coalescing lint and test batches
`run_linters.py --changed` and `test.py -u MODULES` requests that arrive while a run is in progress join the next run, which covers all their branches in one combined tree; each caller still gets its own exit code, verdict and pass stamp. Mechanism `batch_queue.py`, lint side `lint_batch.py`, test side `odoo_test_batch.py`; `--solo` opts out. See `docs/runbooks/LINT_TEST_BATCHING.md`, the anchor reference `docs/runbooks/LINT_TEST_BATCHING_ANCHORS.md` and ADR 0115.

## Held pins in `dependency_watch.json`

`check_dependency_releases.py` fails the weekly Dependency Release Watch when a pin is behind upstream. A pin that cannot move yet
(for example a program whose loopback-bind patch must be rewritten and rebuilt for five platforms) is recorded as held instead of
being left red or removed from the watch (ADR 0114 decision 2).

### `[@ANCHOR: dependency_watch_hold_marker]` hold marker

An optional `"hold"` object on a `dependency_watch.json` entry, plus the constant `MAX_HOLD_DAYS` (90) in `check_dependency_releases.py`.

| Field | Type | Meaning |
|---|---|---|
| `reason` | string | Non-empty. Why the pin cannot move, in plain words, and where the work is tracked. Printed in the weekly report. |
| `until` | string | ISO date `YYYY-MM-DD`, the last day the hold applies. Must be at most `MAX_HOLD_DAYS` days after the day the check runs. |

Invariants: a hold never hides a pin that is current (the report says the hold is not needed); once `until` has passed the pin counts as
stale again and the weekly run fails with "hold expired"; a malformed hold is an error and fails the run. Extending a hold means editing
`until` in a reviewed pull request, so nobody can leave a pin silently behind. Written by people editing the JSON, read only by
`hold_status()`.

### `[@ANCHOR: dependency_hold_status]` `hold_status(entry, today)`

Purpose: classifies one entry's hold marker for `check_one()`, so a held, stale pin is reported but does not fail the run.

| Argument | Type | Meaning |
|---|---|---|
| `entry` | dict | One entry of `dependency_watch.json`; only the optional `"hold"` key is read. |
| `today` | `datetime.date` | The day to judge the hold against (injected so tests are deterministic). |

Returns `None` when the entry has no `"hold"`; otherwise a tuple `(state, message)` where `state` is `"active"` (message is
`<reason> (until <date>)`), `"expired"` (`until` is before `today`) or `"invalid"` (not an object, empty reason, missing or unparseable
`until`, or `until` more than `MAX_HOLD_DAYS` ahead). Raises nothing for bad input. Pure function: no I/O, no network, safe to call
repeatedly. See also the tests in `test_check_dependency_releases.py`.

## Test-host PostgreSQL access

### `[@ANCHOR: infrastructure:_allow_cache_manager_socket_password_for_test]` `_allow_cache_manager_socket_password_for_test(hba_paths=None)`

Purpose: makes the real `cache_manager.py` daemon work inside a remote test run. The remote runner sets `PGHOST=/var/run/postgresql` so
Odoo (the `odoo` OS user) connects by peer authentication; the daemon honours `PGHOST`, connects as `cache_manager_ro` through the same
socket, and Debian's `local all all peer` rule refuses it ("Peer authentication failed for user cache_manager_ro"). On hams1 the daemon
has no `PGHOST` and uses TCP with its password. This function adds one `pg_hba.conf` rule giving that role the same password check on the
socket. `provision_environment()` calls it only when provisioning with `--test` (so `provision.py --test` on yoga, bruce17 and the mac1
VM, run by `provision_hams_test_vm.py`), then reloads PostgreSQL if it returns `True`. Production never reaches it.

| Argument | Type | Meaning |
|---|---|---|
| `hba_paths` | list of str or `None` | `pg_hba.conf` files to edit. `None` means every `/etc/postgresql/*/main/pg_hba.conf`. Tests pass a temporary file. |

The line added is the module constant `CACHE_MANAGER_SOCKET_HBA_LINE`: `local all cache_manager_ro scram-sha-256`, inserted immediately
before the first `local all all ...` rule because `pg_hba.conf` is first-match (appended at the end it would never be reached).

Returns `True` when at least one file changed (PostgreSQL needs a reload, not a restart), `False` when the rule was already present.
Raises `RuntimeError` when a file has no `local all all` rule to anchor the insertion, rather than guessing a position; `OSError` if a file
cannot be read or replaced.

Special concerns: blocking file I/O only; no locks. Idempotent and safe to retry. The file is replaced atomically (temporary file in the
same directory, owner and mode copied, then `os.replace`). Honours `--plan` (records the write, changes nothing). Security: the password is
still required and no other role is affected; the rule does not weaken the `odoo`/`postgres` peer rules. See also the tests
`CacheManagerSocketRuleForTestHostsTests` in `test_infrastructure.py` and `_provision_cache_manager_role`.

## The plaintext pgBackRest repository on a fresh host (ADR 0117)

Bruce's decision of 2026-11-08: every new backup on Backblaze B2 is stored in the clear. pgBackRest cannot change a repository's cipher
in place, so the unencrypted repository is a new one, `repo3`, path `/hams_prod_plain`, with no cipher line. The number is the same on every
host, so `backup.config.pgbackrest_repo` is 3 everywhere. On hams1 the encrypted `repo2` (`/hams_prod_enc`) stays configured to restore
the old history. How hams1 itself is switched, step by step: `hams_com/docs/runbooks/PLAINTEXT_BACKUP_REPO_SWITCH.md`. The two entries
below are what `provision.py` does for a host that has no `pgbackrest.conf` yet. The kopia file backup is a different tool and is not
changed (`hams_com/docs/runbooks/b2_backup.md`, "Why the files stay encrypted").

### `[@ANCHOR: infrastructure:render_pgbackrest_plain_repo_conf]` `render_pgbackrest_plain_repo_conf(bucket, endpoint, region, key, secret, pg_data_dir)`

Purpose: builds the text of `/etc/pgbackrest/pgbackrest.conf` for a host whose only repository is the plaintext `repo3` on B2. Called by
`_configure_pgbackrest_plain_repo`; pure, so a test can check the text without a host.

| Argument | Type | Meaning |
|---|---|---|
| `bucket` | str | B2 bucket name (`hams-com-prod-backups` on hams1). Written to `repo3-s3-bucket`. |
| `endpoint` | str | S3 endpoint host without a scheme (`s3.us-east-005.backblazeb2.com`). Written to `repo3-s3-endpoint`. |
| `region` | str | S3 region; B2 accepts the placeholder `us-east-1`. Written to `repo3-s3-region`. |
| `key` | str | B2 application key ID. Written to `repo3-s3-key`. |
| `secret` | str | B2 application key. Written to `repo3-s3-key-secret`. |
| `pg_data_dir` | str | The cluster's data directory, written as `pg1-path` of stanza `hams_prod`. |

Returns the file text (str). It sets `repo3-path=/hams_prod_plain` and `repo3-retention-full=30`, and contains no `cipher` line, no `repo1`
and no `repo2` line. Raises nothing. Special concerns: the secret is in the returned text, so a caller must not log it; the file that
receives it is mode 0640 root:postgres. The key goes in the file because `archive-push` runs inside the postgres process, whose
environment cannot change without a restart. See also `PgbackrestPlainRepoTests` in `test_infrastructure.py`.

### `[@ANCHOR: infrastructure:_configure_pgbackrest_plain_repo]` `_configure_pgbackrest_plain_repo(conf_path=None, env_path=None, b2_config_path=None, pg_data_dirs=None)`

Purpose: gives a freshly provisioned host a `pgbackrest.conf` with the plaintext `repo3`. `provision_environment()` calls it right after
`_retire_pgbackrest_repo1()`, inside the PostgreSQL hook, on every provisioning run.

| Argument | Type | Meaning |
|---|---|---|
| `conf_path` | str or `None` | The file to create. `None` means `/etc/pgbackrest/pgbackrest.conf`. |
| `env_path` | str or `None` | systemd EnvironmentFile with the B2 key: `PGBACKREST_REPO3_S3_KEY` and `_S3_KEY_SECRET`, else the `PGBACKREST_REPO1_...` names hams1 uses. `None` means `/opt/hams/etc/pgbackrest.env`. |
| `b2_config_path` | str or `None` | JSON with `backend.bucket`, `backend.endpoint` and optionally `backend.region`. `None` means `/opt/hams/etc/b2_backup/config.json` (written by the MANIFEST on the `b2_backup` host class). |
| `pg_data_dirs` | list of str or `None` | Data directories of the PostgreSQL clusters. `None` means `/var/lib/postgresql/<version>/main` for each `/etc/postgresql/*/main/postgresql.conf`. Tests pass a list. |

Returns `True` when it wrote the file (or, under `--plan`, would have), else `False`. It returns `False`, writing nothing, when the file
already exists, when either input file is missing, when the key, secret, bucket or endpoint is empty, or when the number of clusters is not
exactly one. An existing file is never edited: hams1's holds the repo2 passphrase, and adding repo3 there is a manual, ordered procedure
(the stanza must exist in the new repository before `archive-push` is pointed at it). It does not run `stanza-create` and does not set
`archive_command`. Raises `OSError` if the file cannot be created, `KeyError` if it runs as root and group `postgres` does not exist,
`ValueError` if `config.json` is not valid JSON.

Special concerns: blocking file I/O, no locks, no network. Idempotent: the second run sees the file and returns `False`. The file is created
with `O_EXCL` and mode 0640 and chowned to `root:postgres` when run as root, so it is never briefly readable by others. Honours `--plan`.
See also the tests `PgbackrestPlainRepoTests` and `_retire_pgbackrest_repo1`, which removes only `repo1-*` lines and leaves repo3 alone.

## Rotated Odoo API keys and the processes that hold them

`daemon_key_manager` rotates every daemon's Odoo API key: the monthly cron, and every start of `hams.daemon.keys.service` (it runs
`action_force_provision_all`). A rotation writes the new key to the daemon's key file, revokes the old key and restarts nothing. A
process that read its key once therefore fails every Odoo call with 401 until it is restarted (`backup.worker`, 2026-10-27 to
2026-11-05). `Type=oneshot` units read the file afresh on every run and are safe; every other process that names a file under
`/opt/hams/etc/keys/` has to re-read it itself. The two anchors below are the guard that keeps that true as units are added. The audit
with the evidence for each row is the "Rotation safety audit 2026-11-05" section of `daemon_key_manager/README.md`.

### `[@ANCHOR: infrastructure:rotated_key_reload_policy]` `KEY_RELOAD_MECHANISMS` and `ROTATED_KEY_RELOAD_POLICY`

Purpose: the declared answer, for every long-running key holder in the MANIFEST, to "how does this process survive a rotation?". Two
module-level dicts in `infrastructure.py`, read only by `test_infrastructure.py` (`RotatedKeyReloadPolicyTests`) and by the companion
source check in hams_com (`daemons/test_daemon_key_reload_coverage.py`).

`KEY_RELOAD_MECHANISMS` is the closed vocabulary, `{mechanism: sentence}`:

| Mechanism | Meaning |
|---|---|
| `hams_config` | Odoo calls go through `hams_config.get_odoo_client()`; its `execute()` re-reads `ODOO_KEY_FILE` after a 401 and retries once. |
| `own_reload` | The daemon has its own re-read of the key file after a 401, with one retry (`backup_worker`, `generalized_monitor`). |
| `per_call` | The key file is read again for every use, because each use makes a new `get_odoo_client()`. |
| `unused` | The key is loaded but never presented to Odoo, so a revoked key cannot fail anything. |

`ROTATED_KEY_RELOAD_POLICY` is `{name: (mechanism, reason)}`:

| Part | Type | Meaning |
|---|---|---|
| `name` | str | Base name of the unit file (`backup.worker.service`) or of a wrapper script that exports `ODOO_KEY_FILE` (`run-ticket-triage-mcp.sh`). Must equal a name returned by `long_running_key_holders()`. |
| `mechanism` | str | A key of `KEY_RELOAD_MECHANISMS`. |
| `reason` | str | More than 20 characters: the source file that does the re-read, and any detail a reviewer needs (for example that the unit still loads an `EnvironmentFile=` that only seeds the first key). |

Invariants (each is a test): every holder the MANIFEST yields has a row; every row names a current holder; every mechanism is in the
vocabulary; a holder whose key arrives through `EnvironmentFile=` (systemd reads that once, at start) is `own_reload` or `unused`.
Lifetime: edited by hand in the same pull request that adds, removes or changes a unit; nothing writes it at run time. Concurrency: none
(import-time constants). Adding a long-running daemon that holds a key means adding a row here and a row to the audit table; the
companion check in hams_com then confirms the daemon's source really goes through `get_odoo_client()` (or the named own reload).
Created and read by: `long_running_key_holders()` (below) and the tests.

### `[@ANCHOR: infrastructure:long_running_key_holders]` `long_running_key_holders(manifest)`

Purpose: lists the MANIFEST processes that can outlive a key rotation and name a key file, so the guard compares them with
`ROTATED_KEY_RELOAD_POLICY`.

| Argument | Type | Meaning |
|---|---|---|
| `manifest` | dict | A MANIFEST-shaped dict; only `manifest["static_files"]` is read, each item's `path` and `content`. Borrowed, never changed. Tests pass a synthetic one. |

Returns `{name: set_of_how}`. `name` is the base name of a `.service` file whose `Type=` is not `oneshot` (no `Type=` means `simple`), or of
a `.sh` wrapper script. `how` holds `"environment_file"` (an `EnvironmentFile=` line, with or without the `-`, that points under
`/opt/hams/etc/keys/`) and/or `"key_file"` (any other uncommented line that mentions a path under that directory, such as
`Environment="ODOO_KEY_FILE=..."` or `export ODOO_KEY_FILE=...`). Comment lines (`#`, `;`) do not count. Drop-in `.conf` files, timers,
sudoers files and oneshot services are skipped. Raises nothing for odd input (missing keys are treated as empty). Pure function: no I/O, no
locks, safe to call repeatedly. Example: `long_running_key_holders(MANIFEST)["backup.worker.service"] == {"environment_file"}`. See also the
tests `RotatedKeyReloadPolicyTests` in `test_infrastructure.py` and `ROTATED_KEY_RELOAD_POLICY` above.

## PowerDNS test helpers: waiting without guessing the load

`PdnsPublicConfigTests` in `test_infrastructure.py` starts a real `pdns_server` and queries it over loopback. Two tests failed once
under load (2026-11-07) and, reproduced with 96 CPU hogs on 16 cores, five did: the old helpers guessed how slow the machine was
(a 15 s start deadline, a 3 s reply wait scaled by the 1-minute load average, which lags a sudden load, and a fixed 1 s wait that
treated "no reply yet" as "dropped"). They now wait for events and bound only a real hang.

### `[@ANCHOR: test_infrastructure:pdns_hang_seconds]` `_PDNS_HANG_SECONDS` and `_PDNS_READY_LINE`

Purpose: the two constants of the helpers below, class attributes of `PdnsPublicConfigTests`.

| Name | Type | Meaning |
|---|---|---|
| `_PDNS_HANG_SECONDS` | int, 90 | the longest any one wait for a reply, a connection or the start of `pdns_server` may last. Not a speed estimate: a correct server answers as soon as it is scheduled, so only a hung one reaches it. A test may patch it smaller to prove the wait is bounded |
| `_PDNS_READY_LINE` | str | the line `pdns_server` logs when its threads are launched (`ready to distribute questions`); `_start_shipped_pdns` waits for it in `pdns.log` before the first query |

Invariants: both are read only, by `_start_shipped_pdns`, `_udp`, `_tcp` and `_udp_dropped`. Tests: `test_a_server_that_never_answers_still_fails_the_helper`.

### `[@ANCHOR: test_infrastructure:pdns_udp]` `PdnsPublicConfigTests._udp(port, packet)`

Purpose: send one DNS query over UDP to the server under test on 127.0.0.1 and return its reply, for every test that expects an answer.

| Argument | Type | Meaning |
|---|---|---|
| `port` | int | the UDP port of the server |
| `packet` | bytes | the whole query, from `_packet` or `_query_with` |

Returns a dict from `_reply_fields`: `tc`, `ra` (bool), `rcode`, `answers`, `size`. Raises `socket.timeout` (an `OSError`) when no reply arrives within
`_PDNS_HANG_SECONDS`. Blocking, one socket per call, no retry (loopback UDP does not lose a datagram), no side effects. Example:
`self._udp(port, self._packet("alias.u.example.org", self.TYPE_TXT))["rcode"] == 0`. See also `_tcp`, `_udp_dropped`; tests
`test_a_reply_that_takes_longer_than_the_old_fixed_wait_is_still_received`, and every real-server test of the class.

### `[@ANCHOR: test_infrastructure:pdns_udp_dropped]` `PdnsPublicConfigTests._udp_dropped(port, packet)`

Purpose: show that the server gives NO reply to a packet (opcodes it ignores, a reply sent as a query, malformed packets). The absence of a
reply cannot be waited for, so it is shown by ordering. Replaces `_udp_or_none`, whose 1 s wait called a slow reply "dropped" and
whose `None` crashed the tests that expected a reply.

| Argument | Type | Meaning |
|---|---|---|
| `port` | int | the UDP port of the server |
| `packet` | bytes | the probe; its id must not be 0x4321 (the control id) |

Returns `True` when nothing but the control replies came back, `False` when any datagram with another id arrived. Method: the probe, then three
control queries (id 0x4321) on the same socket, each waited for up to `_PDNS_HANG_SECONDS` (each reply needs the server's threads to have run),
then 0.3 s of straggler wait. Raises `socket.timeout` when a control query is not answered (the server is hung or dead, so a "drop" proves nothing).
Blocking. Residual risk: a worker thread descheduled for longer than the control round trips plus 0.3 s could make a late reply look like a drop; the
three round trips make that vanishingly rare, and it can only hide a reply, never invent one. See also `_udp`; tests
`test_dropped_is_true_for_a_packet_the_server_ignores_even_when_replies_are_slow`, `test_dropped_is_false_when_the_server_does_answer_the_packet`.

## test.py pre-flight: host packages and the sibling repository

These run in `test.py`'s pre-flight, before any lock, namespace or database work. They were changed so a fresh Ubuntu 24.04
host (a Claude Code cloud session container, the jetson-1 LAN host) and a combined batch tree pass the pre-flight when nothing
is really wrong. hams_com's `docs/runbooks/CLOUD_SESSION_TESTING.md` lists every step a cloud session takes.

### `[@ANCHOR: infrastructure:pip_fallback_apt_packages]` `PIP_FALLBACK_APT_PACKAGES`

Purpose: names the apt packages the MANIFEST lists that some OS release no longer ships, and the pip distribution installed in
their place. It lives at module level in `infrastructure.py` so provisioning (which installs the pip distribution) and
`test.py`'s pre-flight (which accepts it) read one list.

| Key | Value | Meaning |
|---|---|---|
| apt package name (str) | pip distribution name (str) | `"awscli": "awscli"`: Ubuntu 24.04 dropped the `awscli` package (snap or pip only). Needed only by `daemons/ses_inbound_mail_ingest/main.py`'s `aws` shell-out. |

Invariants: every key is an apt package name in `MANIFEST["apt_packages"]`; the value is a name `importlib.metadata.version()`
accepts. Read by `provision_environment()` (after an `apt-cache show` finds no candidate, it runs
`pip3 install --break-system-packages <value>`) and by `missing_host_apt_packages()` below. Nobody writes it at run time.
Adding an entry is a decision that pip is an acceptable source for that tool on every host. See also the tests
`MissingHostAptPackagesTests` in `test_test.py`.

### `[@ANCHOR: test:missing_host_apt_packages]` `missing_host_apt_packages(pkg_specs, os_id, dpkg_installed, pip_installed, have_cargo=False)`

Purpose: decides which MANIFEST apt packages a host lacks. A pure function so the decision can be tested without dpkg;
`check_host_apt_packages()` is its only production caller.

| Argument | Type | Meaning |
|---|---|---|
| `pkg_specs` | list of dict | `MANIFEST["apt_packages"]` entries: `name`, optional `debian_name`. |
| `os_id` | str | `get_os_identifier()`: `"debian"` selects `debian_name` (falling back to `name`); anything else uses `name`. |
| `dpkg_installed` | callable(str) -> bool | True when dpkg reports the package `install ok installed`. |
| `pip_installed` | callable(str) -> bool | True when the pip distribution is installed for the running interpreter. |
| `have_cargo` | bool | True when a `cargo` is on PATH (rustup's). Default False. |

Returns the missing package names as a list of str, in manifest order (empty when nothing is missing). A package counts as
present when dpkg has it, or when it is a key of `PIP_FALLBACK_APT_PACKAGES` and `pip_installed(value)` is true (pip is never
consulted for any other package), or when it is one of `infrastructure.APT_RUST_TOOLCHAIN_NAMES` (`cargo`, `rustc` and their
`-web` variants) and `have_cargo` is true: provisioning (`_filter_apt_install`) never installs the distro toolchain over a
cargo already on PATH, so demanding it here stopped every run on such a host (a cloud container, 2026-11-07). Raises whatever the callables raise (`check_host_apt_packages()` treats `FileNotFoundError`
from a host without `dpkg-query` as "cannot check").

Special concerns: no side effects of its own; blocking only as far as the callables are. See also
`MissingHostAptPackagesTests` in `test_test.py`.

### `[@ANCHOR: test:check_host_apt_packages]` `check_host_apt_packages()`

Purpose: stops a non-isolated run (one without `HAMS_ISOLATED_NS=1`) before the namespace is built when a MANIFEST apt package
is missing, with the `apt-get install` command that fixes it. `main()` calls it once, in the first process, before re-executing
through `unshare`.

Call interface: no arguments; returns `None` when nothing is missing or when `dpkg-query` does not exist (a non-Debian host:
the check is skipped, logged at debug). Otherwise prints `Missing required host APT packages: <names>` and the install command,
then exits with status 1 (`SystemExit`). Uses `_dpkg_installed()` (`dpkg-query -W -f=${Status}`, one subprocess per package)
and `_pip_distribution_installed()` (`importlib.metadata.version()` in the running interpreter) through
`missing_host_apt_packages()`, with `have_cargo` true when `shutil.which("cargo")` finds one on this process's PATH.

Special concerns: blocking, about 60 short subprocesses; read-only; no locks. Before 2026-11-07 it demanded `awscli` from apt,
so every non-isolated run on Ubuntu 24.04 stopped here even after provisioning had installed the pip `awscli`. See also
`MissingHostAptPackagesTests` in `test_test.py`.

### `[@ANCHOR: test:add_test_companions]` `add_test_companions(install_modules, target_modules, base_dir, environ=None)`

Purpose: gives a plain `test.py -u <module>` run the test companions the remote runner already installs: modules a tested
module's tests need although its manifest does not depend on them (a service account looked up by xml id, an imported test
helper's needs, the `TEST_COMPANIONS` table in hams_com's `devbox_tools/asset_gate.py`). Before 2026-11-07 a local
`test.py -u ham_events` or `-u ham_club_management` errored in 12 to 17 tests ("Service Account
ham_communications_consent.user_comm_consent_supervision_service not found") because only the remote path added them. Called
once by `main()` when the run names modules (`-u`, or a `--test-tags` selector naming modules).

| Argument | Type | Meaning |
|---|---|---|
| `install_modules` | list of str | The modules the run would install (from `select_modules()`). |
| `target_modules` | list of str | The modules whose tests run; a `module:Class.test` entry is looked up by its module name. |
| `base_dir` | str | The checkout under test; it and the sibling hams_com and hams_open checkouts are the module trees searched. |
| `environ` | mapping or `None` | Environment for the sibling lookup (`HAMS_COM_TREE`, `HAMS_OPEN_TREE`); `None` means `os.environ`. |

Returns `(modules, note)`. `modules` is `install_modules` followed by each companion not already in it, in the order found;
`note` is a one-line "installing X without testing them" message, or `""` when nothing was added. The companions are installed
only: the test tags are still built from `target_modules`, so their tests do not run. If no checkout in the search has
`devbox_tools/asset_gate.py` (a hams_open-only run), nothing is added and `note` is `""`.

Special concerns: loads `asset_gate.py` from the tree (it is not on `sys.path` and is not part of hams_shared), so a rename
of its `test_companions` function breaks this call; no writes; no network. See also `AddTestCompanionsTests` in
`test_test.py` and hams_com's `asset_gate_test_companions`.

### `[@ANCHOR: test:anchor_scan_sibling_dir]` `anchor_scan_sibling_dir(base_dir, other_repo_name, environ=None)`

Purpose: finds the other repository's checkout (hams_open for a hams_com run, and the reverse) so the pre-flight anchor scan
covers both repositories. Anchors in each cite the other's docs and tests, so a scan of one alone reports hundreds of false
"missing" findings. Called once by `check_linters()`.

| Argument | Type | Meaning |
|---|---|---|
| `base_dir` | str | The checkout under test (the working directory test.py was started in). |
| `other_repo_name` | str | `"hams_open"` or `"hams_com"`. |
| `environ` | mapping or `None` | Environment to read `<OTHER_REPO_NAME>_TREE` from (`HAMS_OPEN_TREE`, `HAMS_COM_TREE`); `None` means `os.environ`. |

Returns the absolute path of the first of these that is a directory, else `None`:
1. `<OTHER_REPO_NAME>_TREE` from `environ` (an explicit choice wins; a value that is not a directory is ignored);
2. `<other_repo_name>` beside `base_dir` (a plain checkout, or a deliberately paired pair of worktrees);
3. `<other_repo_name>` beside the main checkout of a linked worktree, from `resolve_repo_layout()` (the same rule
   `get_addons_path()` uses for the addons path).

Step 3 is what a combined batch tree needs (`~/workspace/tmp/batch_test_<repo>_*/hams_com`, or under `HAMS_BATCH_TREE_DIR`):
nothing sits beside it. Before 2026-11-07 the scan of a batch tree covered hams_com alone and failed with about 220 false
findings, so every batched `test.py -u` run failed its pre-flight unless `HAMS_OPEN_TREE` was set.

Special concerns: runs one `git rev-parse --git-common-dir` for a worktree; no writes; no locks. See also
`AnchorScanSiblingDirTests` in `test_test.py`, `resolve_repo_layout()`, and `verify_anchors.py`'s own
`_add_sibling_repo_target()`.

## Lint cache directories: named Cargo builds and the unbudgeted caches

Written 2026-11-07. Code: `tools/lint_cache.py`; the runbook is `docs/runbooks/LINT_CACHES.md`. The relay export test
(`test_export_source_release.py` in hams_com) compiled the relay in `~/.cache/hams-relay-export-test-target`, outside the
budget; on `mac1-vm` it reached 19 GB and the lint cache's free-space floor then cleared the warm Cargo directory on every
run. These anchors put that build under the budget and make every other cache visible.

### `[@ANCHOR: lint_cache:named_cargo_session]` `named_cargo_session(name, environ=None, log=None)`

Purpose: a context manager that gives a test which runs cargo (the relay export test) a Cargo target directory under the
same budget, least-recently-used pruning, lock and unused-days rule as the lint Cargo directories.

| Argument | Type | Meaning |
|---|---|---|
| `name` | str | Directory name stem, for example `relay-export-test`; the directory is `<cache>/hams-lint-cargo/<name>-<rustc release>-<8 hex>`. A path separator or empty string is the caller's error (not checked). |
| `environ` | mapping or `None` | Environment to read (`XDG_CACHE_HOME` for the location, `CARGO_TARGET_DIR`, `HAMS_LINT_NO_CACHE`, `HAMS_LINT_MACHINE` and the `HAMS_LINT_*` budget overrides); `None` means `os.environ`. |
| `log` | callable or `None` | Receives each `lint cache: ...` line (size against budget, prune, clear, removal). |

Yields (str) the directory to pass as `CARGO_TARGET_DIR`:
1. `HAMS_LINT_NO_CACHE=1`: a new throwaway directory, deleted when the block ends;
2. a `CARGO_TARGET_DIR` already in `environ`: that directory, untouched (never pruned or locked);
3. otherwise the named directory, after `prepare_cargo_target`: unused directories of the base removed, the directory
   pruned (incremental data first, then whole units, oldest first, to 80 % of the budget) when it is over `cargo_gb` or
   the disk is under `free_floor_gb`, and cleared when that is not enough. It never raises for lack of space.

Special concerns: holds the exclusive flock `<dir>.lock` from before the prune until the block ends, so a second build in the
same directory waits and a prune never runs under a live build; blocks while another holder has it. The budget is the
machine's `cache_budgets` entry in `lint_machines.json` (key `HAMS_LINT_MACHINE`, else the host name). The caller should
also set `CARGO_INCREMENTAL=0`, as the lint builds do, so no `incremental/` data accumulates. Not idempotent on disk: each
entry may prune. Not for use from several threads of one process with the same name (the flock is per open file; the
second waits for the first, which is the intent). See `CargoBudgetTest` in `test_lint_cache.py`.

### `[@ANCHOR: lint_cache:legacy_cargo_dirs]` `legacy_cargo_dirs(environ=None)`

Purpose: names the old Cargo target directories that predate the budget so `show` can report them and `clear cargo` can
delete them.

| Argument | Type | Meaning |
|---|---|---|
| `environ` | mapping or `None` | Environment for `cache_root` (`XDG_CACHE_HOME`, else `~/.cache`); `None` means `os.environ`. |

Returns a list of two absolute paths, in this order: `<cache>/hams-cargo-target-lint` (the old single lint directory,
which `adopt_legacy_dir` renames into the budget on first use) and `<cache>/hams-relay-export-test-target` (the relay
export test's old directory, not adopted: its 19 GB was mostly incremental data). The paths need not exist. No side
effects.

### `[@ANCHOR: lint_cache:other_caches]` `other_caches(environ=None)`

Purpose: the directories the lint and test steps create or need that no budget governs. `show_lines` (and so
`python3 tools/lint_cache.py show`, `run_linters.py --lint-caches show` and `lint_remote.py caches`) reports the size of
each one that exists, so none can grow unseen. They are reported, never pruned by this code, because each is needed.

| Argument | Type | Meaning |
|---|---|---|
| `environ` | mapping or `None` | Environment to read `HOME` from (else `~` is expanded); `None` means `os.environ`. |

Returns a list of `(label, path, why)` tuples (all str), always these six:

| Label | Path | Why it is kept |
|---|---|---|
| lint mirror | `~/hams-lint-mirror` | The runner's copy of the repositories (8.6 GB on `mac1-vm`), refreshed by every run. |
| test sources | `/opt/hams-testsrc` | Sources of the Odoo test hosts' test trees (26 GB on `mac1-vm`). |
| playwright | `~/.cache/ms-playwright` | Browsers for the browser tests, downloaded once (0.6 GB). |
| machines | `/var/lib/machines` | Root file systems for the arm64 Pi builds (7 GB on `mac1-vm`); needed. |
| relay test target | `~/ctest-target` | The Cargo target directory of `remote_cargo_test.py` relay test runs. Reported only: `devbox_tools/ctest_prune.py` prunes it at the end of each run (the lint cache's policy), not this code. |
| relay test sources | `~/ctest-src` | The source copies `remote_cargo_test.py` makes for those runs. Reported only; `ctest_prune` owns pruning. |

Special concerns: no side effects; sizing walks the tree (tens of seconds for `/opt/hams-testsrc`) and skips files it
cannot read (`/var/lib/machines` is root-only, so its size is what the running user can see).

### `[@ANCHOR: lint_cache:checker_version]` `checker_version(*paths)`

Purpose: the version string of a per-file result cache (`ResultCache`): a hash of the checker's own source, so an edit to a
checker discards its stored results and nothing else does. Fixed 2026-11-07: the hash used to include each file's absolute
path, and every lint run works in a new checkout path (a batch directory or worktree), so every run opened a new, empty
cache file. `check_float_isfinite` had 347 such files (370 kB each) and both it and `check_int_float_overflow` re-parsed
every Python file on every run (about 42 s each in a batch, 6-8 s on an idle dev box; 0.09 s with a warm cache).

| Argument | Type | Meaning |
|---|---|---|
| `*paths` | str (one or more) | The checker's source files, each readable. A missing file raises `OSError` (the caller passes its own `__file__`). |

Returns a 16-character lowercase hex str: SHA-256 over the (base name, bytes) pairs, sorted, so the same text under any
directory gives the same version, a changed byte or a different file name gives another, and the order of `paths` does not
matter. Two different checkers never share a file anyway: the cache path also holds the checker's name.

Special concerns: reads each file in full on every call (a few kilobytes); no writes, no locks. Old per-path version files
age out through `_prune_results_dirs` (`UNUSED_DAYS`). See `CheckerVersionTest` in `test_lint_cache.py`, including one test
that runs the real checker from two copies in different directories and counts the cache files.

## Linting a hams_shared tree: the sibling layout (`lint_layout.py`)

Why: until 2026-11-07 `run_linters.py` started from a hams_shared worktree read the sibling hams_open and hams_com **main
checkouts**, whose `hams_shared` submodule is whatever commit the main checkout has. Every cross-repository step (`verify_anchors`,
`check_claims_freshness`, the burn-list scan, the module checkers, ...) therefore judged the old hams_shared and never the branch.
hams_shared #411 passed its gate, merged, and broke `verify_anchors` on every branch in all three repositories (its new runbook line
cited an anchor in a form the scanner reports as missing); its fix #412 could not pass its own gate for the same reason in reverse,
and the coordinator linted it in a layout built by hand. Now `run_linters.py` builds that layout itself, for every run started from a
hams_shared worktree (`shared_worktree_root` is true), before it computes any path, and deletes it at exit. Local and `--remote` runs
use it alike: `lint_remote.repo_roles` takes the layout's hams_open and hams_com and the worktree as hams_shared, and the machines'
mirror (`hams_open/hams_shared` synced from the worktree) is the same shape. A hams_shared branch worktree is batched like a hams_com
branch (`lint_batch.py`, since 2026-11-08: the combined tree is a hams_shared worktree named `hams_shared_wt`, linted by its own tools in a
layout of its own); the main hams_shared checkout and a branch that changes the batching tools run alone.

### `[@ANCHOR: lint_layout:Layout]` `Layout(parent, hams_open, hams_com, shared_source, shared_copy)`

Purpose: the record `lint_layout.build` returns and `run_linters.py` keeps for the run: where the throwaway layout is, so the run can
point its repository root at it, stamp only the hams_shared tree, translate changed paths, and delete it. A `collections.namedtuple`
defined in `tools/lint_layout.py`; immutable (`_replace` makes a changed copy). Lives for one lint run and is never stored.

| Field | Type | Meaning |
|---|---|---|
| `parent` | str | The directory that holds the layout and nothing else: `<HAMS_BATCH_TREE_DIR or ~/workspace/tmp>/batch_lintlayout_hams_shared_<pid>-<start>`. The name is what `reap_orphans` matches, so a run that was killed is cleaned up by the next one. |
| `hams_open` | str | `<parent>/hams_open`: a shared-object git clone of the main hams_open at `base_commit`, whose `hams_shared` directory is `shared_copy`. Becomes the run's `repo_root`. |
| `hams_com` | str or None | `<parent>/hams_com`: a clone of the main hams_com at its `base_commit`; its tracked `hams_shared` link resolves to `../hams_open/hams_shared`. None when no hams_com checkout exists (then the run falls back to the main one for the sibling role). |
| `shared_source` | str | Real path of the hams_shared worktree under lint (the run's `dir_path`). Never written. |
| `shared_copy` | str | `<hams_open>/hams_shared`: a real directory (not a link: the checkers do not follow one), itself a git clone at the worktree's HEAD with the worktree's uncommitted regular files overlaid. |

Invariants: `hams_open` and `hams_com` are siblings in `parent`; `shared_copy` is below `hams_open`; none of the paths is inside the
main checkouts or the worktree. Ownership: created by `build`, held by `run_linters.main`, deleted by `remove` (atexit). Serialised
form: none. Concurrency: one run owns it; other runs have layouts of their own (names carry the pid). Functions: `build` creates it,
`remove` deletes it, `mirror_changed` reads it, `reap_orphans` clears dead ones, `run_linters.stamp_repos` reads it.

### `[@ANCHOR: lint_layout:base_commit]` `base_commit(checkout, fetch=True)`

Purpose: chooses the commit a sibling repository is linted at, so the layout shows the world as it will be after this hams_shared
change merges: `origin/main` of the sibling (the main checkouts are only fast-forwarded and are often behind), else its HEAD.

| Argument | Type | Meaning |
|---|---|---|
| `checkout` | str | A git checkout of the sibling (the main hams_open or hams_com). Read only, except that `git fetch -q origin` may update its remote-tracking refs. |
| `fetch` | bool | True (default): run `git fetch -q origin` first, limited to 20 s, failure ignored. False in tests and offline runs. |

Returns a 40-character commit sha (str), or None when `checkout` has neither `origin/main` nor a HEAD (not a git repository). Raises
nothing: a failing git call is the same as a missing ref. Special concerns: blocking for at most 20 s (the fetch); needs the network
only for the fetch; no locks; idempotent. See also: `build` (the only caller); tests `BaseCommitTests` in `test_lint_layout.py`.

### `[@ANCHOR: lint_layout:build]` `build(shared_tree, open_main, com_main, environ=None, fetch=True, out=print)`

Purpose: makes the layout in which a hams_shared tree is linted, so the cross-repository steps see THIS hams_shared inside the sibling
hams_open. Called once by `run_linters.main` for a hams_shared worktree, before any path is computed.

| Argument | Type | Meaning |
|---|---|---|
| `shared_tree` | str | The hams_shared worktree (or checkout) under lint. Read only; its HEAD and its uncommitted regular files (`lint_remote.dirty_files`: secrets, `.git`, `target`, `node_modules` and symlinks are skipped) are copied. |
| `open_main` | str | The main hams_open checkout. Read only (see `base_commit` for the fetch). |
| `com_main` | str or None | The main hams_com checkout. None or a missing directory gives a layout without hams_com. |
| `environ` | mapping or None | Environment for `batch_queue.tree_parent`: `HAMS_BATCH_TREE_DIR` chooses the directory the layout goes under (default `~/workspace/tmp`). None means `os.environ`. |
| `fetch` | bool | Passed to `base_commit`. |
| `out` | callable | Receives the one progress line (run_linters passes a stderr printer so --list-json stays clean) (`[*] hams_shared tree: linting in a layout ...`). |

Returns a `Layout`. Errors: `LayoutError` (a `RuntimeError`) when a git command fails or a sibling has no commit; the half-built layout
is removed first. `OSError` for a filesystem failure, also after removing what was made. `run_linters.py` prints the error and exits 2:
it must never fall back to the main checkouts, which is the bug this removes.

Special concerns: blocking, a few seconds to tens of seconds (a `git clone --shared` and checkout of each sibling; hams_open is about
8,700 files); no network except the optional fetch; nothing is registered in any source repository (shared-object clones, so deleting
the directory is the whole cleanup); disk use is the checked-out files of both siblings (a few hundred MB). Copies no secrets.
Not idempotent: each call makes a new directory. The caller must run `remove` (atexit in `run_linters.py`); `reap_orphans` is the
safety net for a killed run.

Example:

```python
layout = lint_layout.build(dir_path, main_open, main_com)
atexit.register(lint_layout.remove, layout)
repo_root = layout.hams_open        # anchors, claims, burn list now read this hams_shared
```

See also: tests `BuildTests` and `IncidentTests` in `test_lint_layout.py`: a hams_shared change whose docs cite a missing anchor fails
`verify_anchors` in the layout (and passes when the citation is fixed); the main checkouts, which the old code scanned, pass the same
change.

### `[@ANCHOR: lint_layout:remove]` `remove(layout)`

Purpose: deletes the layout's directory when the run ends.

| Argument | Type | Meaning |
|---|---|---|
| `layout` | `Layout` | The layout to delete; `layout.parent` and everything below it go. |

Returns None. Errors: none raised (`batch_queue.remove_tree` ignores failures). Special concerns: idempotent and safe on a half-built
layout; files a test run left owned by another user are removed with a non-interactive `sudo -n rm -rf`; blocking for the time a recursive
delete takes. See also: `build`, `reap_orphans`; tests `BuildTests.test_remove_deletes_the_directory_and_is_safe_twice`.

### `[@ANCHOR: lint_layout:mirror_changed]` `mirror_changed(changed, layout)`

Purpose: makes `--changed` see a hams_shared change in the layout. `run_linters.py --changed` reports the files the branch changed in
the worktree; the layout lints a copy of them, and a step whose inputs are rooted at hams_open (not at the worktree) would not be
selected for a change that exists only under the worktree path.

| Argument | Type | Meaning |
|---|---|---|
| `changed` | set of str | Absolute real paths reported as changed. Not modified. |
| `layout` | `Layout` or None | The run's layout; None when the run has none. |

Returns a new set: `changed` plus, for every path below `layout.shared_source`, the same relative path below `layout.shared_copy`. With
`layout` None the argument itself is returned. Errors: none. Pure (no files, no locks). See also: tests `MirrorChangedTests`.

### `[@ANCHOR: lint_layout:reap_orphans]` `reap_orphans(repo, environ=None)`

Purpose: deletes layouts left by a lint run that was killed (SIGKILL skips atexit), so `~/workspace/tmp` does not fill up. Called by
`run_linters.py` just before it builds a new layout.

| Argument | Type | Meaning |
|---|---|---|
| `repo` | str | Any git checkout; `batch_queue.remove_tree` runs `git worktree prune` there. |
| `environ` | mapping or None | `HAMS_BATCH_TREE_DIR` names the directory to scan (default `~/workspace/tmp`). |

Returns the list of removed parent directories. Only names `batch_lintlayout_hams_shared_<pid>-<start>[...]` whose process
(`lock_queue.is_live`) is gone are removed; a live run's layout is never touched. Errors: none (an unreadable directory gives an empty
list). Special concerns: idempotent; safe to run concurrently with other runs. See also: `batch_queue:reap_orphan_trees`, which it
calls; tests `ReapOrphansTests`.

### `[@ANCHOR: run_linters:stamp_repos]` `stamp_repos(repo_root, tools_dir, dir_path, layout=None)`

Purpose: the repositories a passing run may stamp (`devbox_tools/prmerge.sh` refuses to merge a tip with no stamp, so a stamp must
describe code that was really linted).

| Argument | Type | Meaning |
|---|---|---|
| `repo_root` | str | The run's repository root (hams_open or hams_com checkout; the layout's hams_open for a hams_shared run). |
| `tools_dir` | str | The `tools` directory the run used; its parent is the hams_shared checkout that supplied the linters. |
| `dir_path` | str | The directory the run started from. |
| `layout` | `Layout` or None | Given for a hams_shared worktree run. |

Returns a list of repository directories (str). With `layout`: only the hams_shared tree (the layout's siblings are throwaway clones
of main with a foreign hams_shared, and a stamp on main's commit would vouch for code that was not linted in that form). Without it:
`repo_root`, plus the hams_shared checkout when the run started from it or scanned it below `repo_root` without crossing a symlink (a
link to the main checkout is not followed by the directory walks, so such a run must not vouch for its commit; found 2026-10-30).
Pure apart from `os.path.realpath` calls. Tests: `StampReposTests` in `test_lint_layout.py`.

## Odoo-test hosts and the lint runner; signal exits of test.py

The mac1 VM (`hams-test-debian`) and bruce17 run Odoo browser tests (Odoo, PostgreSQL, Chromium) and are also lint runners and relay
`cargo test` hosts. On 2026-11-04 a relay build overlapped an Odoo browser test in the 11 GiB VM and test.py's OOM watchdog killed the
Odoo run (exit 247). `remote_cargo_test.py` now takes the host's Odoo-test lock; the lint runner takes it too, through the entries below.

## Build and test scratch directories in RAM (`ram_dirs.py`)

Bruce, 2026-11-08: "use this box's spare RAM to cut SSD writes." A Cargo build writes 5-12 GB of target data per job and throws it away; the
lint Cargo caches and the TMPDIR of a test run (Chrome profiles) are scratch too. `ram_dirs.py` puts them under `/dev/shm` (a tmpfs, capped at
half of the memory: 63 GB on the 125 GB dev box) when it has room, and on the disk, with a warning on standard error, when it has not.

Rules, all in one module: **never silent** (a refusal prints the free space and the minimum); a machine with less than 64 GB of memory keeps its disk paths
quietly (`HAMS_RAM_DIRS=1` forces RAM, `HAMS_RAM_DIRS=0` forbids it); every per-job directory carries an owner marker so the sweep can tell a crashed job's from a
live one; `run_job` stops a job that outgrows its limit or squeezes the free memory. Settings (environment): `HAMS_RAM_DIRS` (`0`, `1`, else auto),
`HAMS_RAM_ROOT` (another tmpfs or directory; naming one turns the tmpfs check off), `HAMS_RAM_MIN_FREE_GB` (20), `HAMS_RAM_JOB_MAX_GB` (30),
`HAMS_RAM_HARD_FREE_GB` (5), `HAMS_RAM_LINT_GB` (24: the whole lint cache in RAM). What does not move: anything that must outlive a reboot, and the PostgreSQL data of the test databases. The full pipeline of `test.py`
(its private namespace) already keeps `/var` and `/tmp` on a memory overlay, so it needs nothing; the move helps the `HAMS_ISOLATED_NS=1` runs on the host.
Command line: `ram_dirs.py run JOB -- COMMAND...` (the job-end trap), `path JOB` / `release JOB` (a shell's own job), `sweep`, `status`.
Tests: `test_ram_dirs.py`, `RamCargoBaseTest` in `test_lint_cache.py`, `RamTestTmpdirTests` in `test_test.py`.

### `[@ANCHOR: ram_dirs:RamPolicy]` `RamPolicy`

Purpose: the settings every decision reads; built by `policy`. A frozen dataclass in `ram_dirs.py`; one instance per decision, never shared state.

| Field | Type | Meaning |
|---|---|---|
| `mode` | str | `auto` (default: RAM on a machine with at least `min_ram_gb` of memory), `on` (RAM regardless of the memory size), `off` (never). |
| `root` | str | The RAM directory, default `/dev/shm`. |
| `min_free_gb` | float | Free space `root` must have before a new directory is placed there; default 20. |
| `job_max_gb` | float | The size at which `run_job` stops a job whose directory is in RAM; default 30. |
| `hard_free_gb` | float | The free space of `root` under which `run_job` stops a RAM job; default 5. |
| `min_ram_gb` | float | Memory size below which `auto` keeps the disk quietly; default 64. |
| `lint_budget_gb` | float | Total size of `hams-lint-cargo` in RAM; `prune_lint_cache` removes least recently used unused directories above it; default 24 (`HAMS_RAM_LINT_GB`). |
| `idle_hours` | float | `gc` removes an ownerless job directory idle this long; default 3 (`HAMS_RAM_IDLE_HOURS`). |
| `keep_hours` | float | The same for a directory of a job that ran with `--keep`; default 6 (`HAMS_RAM_KEEP_HOURS`). |
| `require_tmpfs` | bool | `root` must be a tmpfs; default True, False when `HAMS_RAM_ROOT` names the root. |

Invariants: a policy never changes after it is built. The settings are read from the environment only by `policy`.

### `[@ANCHOR: ram_dirs:policy]` `policy(environ=None)`

Purpose: builds the `RamPolicy` the environment asks for. Argument `environ`: mapping or `None` for `os.environ`. Reads `HAMS_RAM_DIRS` (`0`, `off`, `no`, `false` mean off; `1`, `on`,
`yes`, `true` mean on; anything else auto), `HAMS_RAM_ROOT`, `HAMS_RAM_MIN_FREE_GB`, `HAMS_RAM_JOB_MAX_GB`, `HAMS_RAM_HARD_FREE_GB`. Returns a `RamPolicy`. A number that is
missing, not a number, not finite or negative keeps its default. Never raises, touches no file.

### `[@ANCHOR: ram_dirs:total_ram_gb]` `total_ram_gb()`

Purpose: the installed memory, used to decide whether a machine is a RAM-directory machine. Returns GiB (float) from `MemTotal` of `/proc/meminfo`, or `None` when it cannot be read (then
`place` assumes a big machine). Reads one file; never raises.

### `[@ANCHOR: ram_dirs:is_tmpfs]` `is_tmpfs(path)`

Purpose: guards against a RAM root that is really a disk directory. Returns True when the longest mount point of `/proc/mounts` that holds `path` (after `realpath`) has type `tmpfs`. False
when `/proc/mounts` cannot be read. Never raises.

### `[@ANCHOR: ram_dirs:free_gb]` `free_gb(path)`

Purpose: the space a new directory can use. Returns the unprivileged free space (`f_bavail * f_frsize`) in GiB of the file system holding `path`, measured at its nearest existing parent, or `None`
when it cannot be read (including the empty path). Never raises.

### `[@ANCHOR: ram_dirs:warn_once]` `_warn(key, text, out=None)`

Purpose: the loud half of "fall back loudly". Prints `ram_dirs: WARNING: <text>` on `out` (default standard error) the first time `key` is seen in this process, nothing afterwards, so a lint run that asks
for the Cargo base twenty times warns once. State: the module set `_warned` (cleared by the tests only). No return value.

### `[@ANCHOR: ram_dirs:Placement]` `Placement`

Purpose: the answer of `place`. A frozen dataclass in `ram_dirs.py`.

| Field | Type | Meaning |
|---|---|---|
| `path` | str | The directory to use: `<root>/<name>` or the disk path the caller offered. |
| `in_ram` | bool | True when `path` is under the RAM root. |
| `reason` | str | One line saying why (free space, or the reason for the fallback). A fallback's reason was also printed. |

### `[@ANCHOR: ram_dirs:place]` `place(name, ssd_path, pol=None, environ=None, warn=_warn)`

Purpose: the one decision: RAM or disk, for every caller (Cargo target, lint cache base, test TMPDIR).

| Argument | Type | Meaning |
|---|---|---|
| `name` | str | Directory name under the RAM root. |
| `ssd_path` | str | The disk path returned when RAM is refused. |
| `pol` | `RamPolicy` or `None` | The settings; `None` builds them from `environ`. |
| `environ` | mapping or `None` | Passed to `policy`. |
| `warn` | callable `(key, text)` | Receives the fallback warning; the default prints once per reason on standard error. |

Returns a `Placement`. RAM is chosen when the mode is not `off`, the machine has the memory (auto mode), the root exists, is a tmpfs (when required) and has `min_free_gb` free. `off` and a small machine in auto mode keep
the disk quietly; a missing root, a root that is not a tmpfs, unreadable free space and too little free space keep it with a warning that names the numbers. Never raises, creates nothing. The free space is a snapshot:
other jobs can use it before this one writes, which is what the guard of `run_job` and the floor of the lint cache are for.

### `[@ANCHOR: ram_dirs:check_job_name]` `_check_job_name(job)`

Purpose: keeps a job name usable as a directory name. Returns `job` when it is 1-64 characters of `A-Za-z0-9._-` starting with a letter or digit. Raises `ValueError` otherwise (so `..`, a slash or a leading `-` can never
reach a path or a `rm`).

### `[@ANCHOR: ram_dirs:job_directories]` `job_directories(job, pol=None, environ=None)`

Purpose: the two places a job's directory can be, because the free space changes between `path` and `release`. Returns `[<root>/hams-cargo-target-<job>, <cache>/hams-cargo-target-<job>]` (`<cache>` is `XDG_CACHE_HOME` or `~/.cache`).
Raises `ValueError` for a bad name. `release` removes whichever exists.

### `[@ANCHOR: ram_dirs:cargo_target_dir]` `cargo_target_dir(job, pol=None, environ=None, home=None)`

Purpose: the Cargo target directory of a job: `/dev/shm/hams-cargo-target-<job>`, or `~/.cache/hams-cargo-target-<job>` when RAM is refused (the location AGENTS.md used before). Arguments: `job` (a name for `_check_job_name`),
`pol` and `environ` as in `place`, `home` the home directory (default `~`). Returns a `Placement`; creates nothing. Raises `ValueError` for a bad job name. One name per concurrent job: two builds in one target directory overwrite each other's binaries.

### `[@ANCHOR: ram_dirs:test_tmp_base]` `test_tmp_base(environ=None, pol=None, create=True)`

Purpose: the base TMPDIR of a test run that shares the host (Odoo's temporary files and the Chrome profiles; `slot-K` directories below it for slots above 1). Returns `/dev/shm/hams-test-tmp` (created, mode 1777 so the `odoo` account and the
developer can both use it) or `None`: when the run is inside the full pipeline's private namespace (`HAMS_ISOLATED_NS=1` and `HAMS_TEST_LOCK_HELD=1`; its `/var` is a memory overlay that disappears with it, a directory in the host's `/dev/shm` would not), when `place` refuses RAM
(with a warning), or when the directory cannot be created (warning). Called by `test.py`'s `default_test_tmpdir`. Argument `create` (bool): False only names the directory without creating it (the start-up sweep of a full-pipeline run runs on the host before its namespace and needs no directory).

### `[@ANCHOR: ram_dirs:lint_cargo_base]` `lint_cargo_base(environ=None, pol=None)`

Purpose: the base of the lint Cargo caches (`lint_cache.cargo_base`). Returns `/dev/shm/hams-lint-cargo` or `None`. `None` when `XDG_CACHE_HOME` is set (the caller chose a cache location; the tests and the remote
runners do) and when `place` refuses RAM (with a warning). Creates nothing. The caches there are budgeted like the disk ones (`lint_cache`), and a directory unused for `LINT_UNUSED_DAYS` (2) days is removed. They are lost at a reboot, which costs one cold build.

### `[@ANCHOR: ram_dirs:under_ram_root]` `under_ram_root(path, pol=None, environ=None)`

Purpose: says by name whether `path` is below the RAM root, without touching the file system. Returns bool. `lint_cache` uses it to skip adopting a 31 GB legacy directory into RAM and to age RAM caches faster.

### `[@ANCHOR: ram_dirs:start_ticks]` `_start_ticks(pid)`

Purpose: the process start time that makes an owner marker immune to pid reuse. Returns field 22 of `/proc/<pid>/stat` (clock ticks since boot, read after the last `)` so a name with spaces or parentheses is safe), or `None` when the process is gone or the file is unreadable.

### `[@ANCHOR: ram_dirs:claim]` `claim(path, pid=None, now=time.time, keep=False, job=None)`

Purpose: marks `path` as owned by a process. Creates the directory (mode 0700) if it is missing and writes `.hams-ram-owner` in it (atomically, through a temporary file): JSON `{"pid": int, "start": int or null, "created": int, "name": str or null, "job": str or null, "keep": bool}`, `start` being
`_start_ticks(pid)` and `name` the owner's program name (`process_name`). Arguments: `pid` (default this process; `ram_dirs.py path` passes the caller's), `now` (clock, for tests), `keep` (the job ran with `--keep`, so `gc` waits `keep_hours`) and `job` (the job name). Returns `path`. Raises `OSError` when the marker cannot be written.

### `[@ANCHOR: ram_dirs:process_name]` `process_name(pid, proc_root="/proc")`

Purpose: the program name of a process (`/proc/<pid>/comm`) for the owner marker and for `status`; None when the process is gone.

### `[@ANCHOR: ram_dirs:owner_record]` `owner_record(path)`

Purpose: the marker of a directory as a dict `pid, start, created, name, job, keep` (fields an older marker lacks are None, `keep` False); None without a readable marker. Reads one file; never raises.

### `[@ANCHOR: ram_dirs:newest_activity]` `newest_activity(path, depth=3, per_directory=2000)`

Purpose: the newest modification or access time in a tree, sampled cheaply: three levels down (`debug/deps/<file>`) and at most `per_directory` entries of each directory. Reading a build's files counts, so a directory something still reads is not idle. Never raises.

### `[@ANCHOR: ram_dirs:references]` `references(path, proc_root="/proc")`

Purpose: which live processes refer to a directory, so `gc` never removes one in use: `CARGO_TARGET_DIR` or `TMPDIR` in the environment at or below `path`, a working directory below it, or an open file below it. Sorted pids; unreadable processes (other users') are not examined; this process is not listed; `proc_root` is for tests.

### `[@ANCHOR: ram_dirs:gc_log_path]` `gc_log_path(pol=None, environ=None)`

Purpose: the removal log, `hams-ram-gc.log` in the RAM root.

### `[@ANCHOR: ram_dirs:log_removal]` `log_removal(pol, name, size_bytes, idle_seconds, reason, now=time.time, owner=None)`

Purpose: appends `time removed NAME SIZE GB idle H h owner pid P (program): reason` to the removal log and keeps the last 2000 lines. Called for every removal by `sweep` and `gc`. Returns the line; never raises.

### `[@ANCHOR: ram_dirs:gc]` `gc(pol=None, environ=None, say=None, now=time.time, remove=None, proc_root="/proc", dry_run=False)`

Purpose: garbage collection of the RAM root, run at the start of every `run` and by `status`, by `ram_dirs.py gc [--dry-run]` and, when someone enables it, by the user timer `ram_dirs_units/hams-ram-dirs-gc.timer` (every 30 minutes; the files are not installed or enabled by anything: `mkdir -p ~/.config/systemd/user && cp tools/ram_dirs_units/hams-ram-dirs-gc.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now hams-ram-dirs-gc.timer`; adjust the path in the service).
A `hams-cargo-target-*` directory is removed when its owner is not running AND (its job ended without `--keep`, or nothing in it was modified or read for `idle_hours` (default 3, `HAMS_RAM_IDLE_HOURS`), `keep_hours` (default 6, `HAMS_RAM_KEEP_HOURS`) for a `--keep` job) AND no process refers to it (`references`). A directory whose owner is alive is never touched. The lint cache `hams-lint-cargo` has no age limit; when over `lint_budget_gb` it is trimmed by `prune_lint_cache`. Every removal is said and logged (`log_removal`) with name, size, idle time, owner and reason. Returns `[(name, bytes, reason)]`; with `dry_run` nothing is removed or logged. Never raises.

### `[@ANCHOR: ram_dirs:describe_owner]` `describe_owner(path, now=time.time, proc_root="/proc")`

Purpose: the owner phrase `status` prints: `owner pid 123 (cargo) alive, started 2.0 h ago, idle 5 min, job t1, kept`, or `unmarked, idle 4.0 h` with the pids that refer to the directory.

### `[@ANCHOR: ram_dirs:owner_state]` `owner_state(path)`

Purpose: the question the sweep asks. Returns `"alive"` (the marker names a process that runs with the recorded start time), `"dead"` (the process is gone, or the pid now belongs to a process started at another time) or `"unmarked"` (no readable marker: a directory made by hand).
A marker without a start time counts as alive while the pid exists. Reads one file and one `/proc` entry; never raises.

### `[@ANCHOR: ram_dirs:newest_mtime]` `newest_mtime(path, depth=2)`

Purpose: the age test for unmarked directories. Returns the latest modification time (seconds since the epoch) of `path` and of its entries down to `depth` levels (a build touches `debug/deps`, two levels down), 0.0 when `path` is missing. Does not follow symbolic links.

### `[@ANCHOR: ram_dirs:leak_mode]` `leak_mode(environ=None)`

Purpose: the leak check's setting. Returns `"warn"` (default), `"fail"` or `"off"` from `HAMS_LEAK_CHECK`; any other value is `"warn"`. Test: `test_a_clean_job_is_silent_and_a_job_that_leaves_a_file_is_named_loudly_then_cleaned`.

### `[@ANCHOR: ram_dirs:make_job_tmp]` `make_job_tmp(job, environ=None)`

Purpose: a private, empty TMPDIR for one job, a fresh directory `hams-job-<job>-XXXX` under the caller's TMPDIR (else the system temporary directory). Returns its path, or `None` when it cannot be made. Because nothing else uses it, whatever is in it afterwards was left by the job, however many other runs share the host.

### `[@ANCHOR: ram_dirs:leftovers]` `leftovers(path)`

Purpose: what a job left in its private TMPDIR: sorted `(name, bytes)` pairs, directories counted recursively; empty when the directory is gone. Used by `run_job`, which reports them as `LEAK:` on standard error and removes the directory.

### `[@ANCHOR: ram_dirs:remove_tree]` `remove_tree(path)`

Purpose: removal that cannot be turned against us. Removes the tree at `path`; a symbolic link is only unlinked, never followed. Returns True when `path` is gone afterwards. Never raises (errors inside the tree are ignored; the return value tells).

### `[@ANCHOR: ram_dirs:sweep]` `sweep(pol=None, environ=None, say=None, now=time.time, remove=remove_tree)`

Purpose: the clean-up of jobs that could not clean up after themselves (SIGKILL, power cut, an agent that forgot). Called at the start of every `run_job` and `path`, by `ram_dirs.py sweep`, before a lint build in a RAM directory (`lint_cache.prepare_cargo_target`) and when a test run picks the RAM TMPDIR (`test.py` `default_test_tmpdir`), so any later RAM user frees what a crashed job left.

| Argument | Type | Meaning |
|---|---|---|
| `pol`, `environ` | | As in `place`. |
| `say` | callable `(text)` | Reports each removal or failure; default prints on standard error. |
| `now` | callable | The clock, for tests. |
| `remove` | callable `(path) -> bool` | The removal; default `remove_tree`. |

Removes under the RAM root: a `hams-cargo-target-*` directory whose `owner_state` is `dead`; an `unmarked` one in which nothing changed for 24 hours; and, in `hams-test-tmp`, entries that are not `slot-K` directories and were untouched for 12 hours (the Chrome profiles of a
slot-1 run; `slot-K` directories are test.py's own start-up sweep, under the slot lock). Never touches other names (`hams-lint-cargo` is budgeted by `lint_cache`; `PostgreSQL.*` and the rest are not ours). Returns the sorted names removed. A directory that cannot be removed (another
user's) is reported with the `sudo rm -rf` line and the sweep goes on. Never raises. Safe to run from several processes at once: removing a path twice is harmless.

### `[@ANCHOR: ram_dirs:tree_bytes]` `tree_bytes(path)`

Purpose: the size measure of the guard. Returns the sum of the file sizes under `path` in bytes (links not followed; files that vanish while walking are skipped), 0 if it does not exist. Cost: one `lstat` per file, about 0.1 s for a relay target; called every `interval` seconds by `run_job`.

### `[@ANCHOR: ram_dirs:prune_lint_cache]` `prune_lint_cache(base, keep=None, pol=None, environ=None, say=None, remove=remove_tree)`

Purpose: the total budget of the lint Cargo caches in RAM. Four lint slots, two repositories and the relay export test once made 11 directories and 37 GB (2026-11-08); the per-directory `cargo_gb` cap does not bound the sum.

| Argument | Type | Meaning |
|---|---|---|
| `base` | str | `/dev/shm/hams-lint-cargo`. |
| `keep` | str or `None` | The directory about to be built in; never removed. |
| `pol`, `environ` | | As in `place`; the budget is `pol.lint_budget_gb`. |
| `say` | callable | Receives one `lint cache: removed ...` line per directory. |
| `remove` | callable | The removal (default `remove_tree`). |

Returns `[(name, bytes)]` removed. Directories go whole, least recently used first (`.last_used`), until the total is within the budget. A directory is skipped when a live lint run holds its exclusive lock `<dir>.lock` (lint_cache takes it for a whole build; the prune takes it without waiting and holds it during the removal), so a build in progress is never touched, and the lock is the liveness test. May leave the total above the budget when everything else is in use. Called by `lint_cache.prepare_cargo_target` before every build. Never raises.

### `[@ANCHOR: ram_dirs:usage]` `usage(pol=None, environ=None, now=time.time)`

Purpose: the numbers a status display shows. Returns a dict: `time` (epoch seconds), `root`, `total_gb`, `used_gb`, `free_gb`, `min_free_gb`, `hard_free_gb`, `lint_cache` (`budget_gb`, `used_gb`, `dirs`: list of `{name, gb, in_use}`), `jobs` (list of `{name, gb, owner}` for `hams-cargo-target-*`), `test_tmp_gb`. Walks the cache (about a second for 24 GB); never raises.

### `[@ANCHOR: ram_dirs:write_usage]` `write_usage(pol=None, environ=None)`

Purpose: publishes `usage()` as `/dev/shm/hams-ram-usage.json` (mode 0644, written through a temporary file and renamed, so a reader never sees half a file). Called after each lint prune, and at the start and end of `run_job`; `ram_dirs.py usage` prints it and writes it. Returns the path or `None` when it cannot be written (another account owns the existing file, the root is missing): a display file never fails a build.

### `[@ANCHOR: ram_dirs:guard_verdict]` `guard_verdict(size_bytes, free_now_gb, pol, in_ram)`

Purpose: the per-job size and memory guard as a pure function. Returns a sentence saying why the job must be stopped, or `None`. Only for a RAM job (`in_ram`): `size_bytes` above `pol.job_max_gb` GiB, or `free_now_gb` (the free space of the root; `None` means unknown and never stops) below
`pol.hard_free_gb`. A job on the disk is never stopped by it.

### `[@ANCHOR: ram_dirs:JobResult]` `JobResult`

Purpose: what `run_job` returns. A dataclass in `ram_dirs.py`, created once per job.

| Field | Type | Meaning |
|---|---|---|
| `returncode` | int | The command's exit status; `128 + n` when signal `n` ended it; 75 (`EX_TEMPFAIL`) when a live job already owns the directory and nothing ran. |
| `target` | str | The job's directory. |
| `in_ram` | bool | Whether it was in RAM. |
| `stopped` | str or `None` | The guard's reason when it stopped the job. |
| `peak_bytes` | int | Largest size measured (every `interval` seconds, and once at the end). |
| `elapsed` | float | Seconds the command ran. |
| `block_write_bytes` | int | Bytes the command and its children caused to be written to block devices (`getrusage` `ru_oublock` times 512): an upper bound that counts data later deleted unwritten; near 0 for a tmpfs. |

### `[@ANCHOR: ram_dirs:die_with_parent]` `_die_with_parent()`

Purpose: the child-side `preexec_fn` of `run_job`. Asks the kernel (`prctl(PR_SET_PDEATHSIG, SIGTERM)`) to send the command SIGTERM when the runner dies, so a runner killed with SIGKILL (which cannot clean up) does not leave a build
writing into a directory the next `sweep` removes as dead. No arguments, no return value; a platform without `prctl` silently skips it. Acts on the command's first process only; its descendants end when it does.

### `[@ANCHOR: ram_dirs:stop_group]` `_stop_group(child, grace)`

Purpose: ends a job's whole process group. Sends SIGTERM to `child`'s process group (the child was started in its own session) and waits up to `grace` seconds, then SIGKILL and waits 5 more. Returns when the child is gone or the group no longer exists. No return value; never raises.

### `[@ANCHOR: ram_dirs:run_job]` `run_job(job, argv, keep=False, force_ssd=False, pol=None, environ=None, interval=5.0, say=None, popen=subprocess.Popen, grace=10.0)`

Purpose: the job-end trap, the guard and the placement in one call. `ram_dirs.py run JOB -- COMMAND...` calls it; AGENTS.md tells agents to wrap `cargo` in it.

| Argument | Type | Meaning |
|---|---|---|
| `job` | str | Job name (see `_check_job_name`); one name per concurrent job. |
| `argv` | list of str | The command; it runs with `CARGO_TARGET_DIR` set to the job's directory. |
| `keep` | bool | Leave the directory afterwards (for triage). |
| `force_ssd` | bool | Use `~/.cache/hams-cargo-target-<job>` (`--ssd`); still removed afterwards. |
| `pol`, `environ` | | As in `place`. `environ` is also the command's environment. |
| `interval` | float seconds | How often the guard measures. |
| `say` | callable | Report lines; default standard error. |
| `popen` | callable | `subprocess.Popen` (replaceable in tests). |
| `grace` | float seconds | SIGTERM-to-SIGKILL delay when the guard stops the job. |

Returns a `JobResult`. Order of events: `sweep`; placement; refuse with `EX_TEMPFAIL` if a live job owns the directory, else remove what a dead one left; `claim`; start the command in a new session; forward SIGINT, SIGTERM and SIGHUP
to its group (only in the main thread); every `interval` seconds measure and apply `guard_verdict` (the reason is reported, the group stopped, `JobResult.stopped` set); at the end remove the directory (in a `finally`, so an exception here cannot leave it) unless `keep`, and print one summary line (exit status,
time, peak size, RAM or disk, block writes). Blocks until the command ends. Cost: a tree walk per interval. A runner killed with SIGKILL cannot clean up: its directory is found dead by the next `sweep`.

### `[@ANCHOR: ram_dirs:status_lines]` `status_lines(pol=None, environ=None)`

Purpose: the text of `ram_dirs.py status`. Returns a list of lines: the root's free space and the limits, then each `hams-cargo-target-*`, `hams-test-tmp` and `hams-lint-cargo` entry with its size and `owner_state`.

### `[@ANCHOR: ram_dirs:main]` `main(argv=None)`

Purpose: the command line. Subcommands `run JOB [--keep] [--ssd] -- COMMAND...` (exit status of the command, 75 if the job name is in use), `path JOB [--owner-pid PID]` (sweeps, creates and claims the directory for `PID`, default the parent
process, and prints the path; for a shell that cannot wrap its command: `export CARGO_TARGET_DIR=$(python3 hams_shared/tools/ram_dirs.py path myjob --owner-pid $$)` and `trap 'python3 hams_shared/tools/ram_dirs.py release myjob' EXIT`),
`release JOB` (removes the job's directory from either place), `sweep`, `gc [--dry-run]`, `status` (runs `gc` first, then lists each directory with its owner). Returns the exit status; 2 for a bad job name.

### `[@ANCHOR: lint_cache:disk_cargo_base]` `disk_cargo_base(environ=None)`

Purpose: the disk location of the lint Cargo caches, `<cache>/hams-lint-cargo` (`XDG_CACHE_HOME` or `~/.cache`); where they live when RAM is refused, and where the old ones are. Returns the path (str); creates nothing.

### `[@ANCHOR: lint_cache:cargo_base]` `cargo_base(environ=None)`

Purpose: where the lint Cargo caches live now (`cargo_target_dir`, `named_cargo_session` and the budget all use it). Returns `/dev/shm/hams-lint-cargo` when `ram_dirs.lint_cargo_base` allows it, else `disk_cargo_base`. `lint_cache.py` run alone (`lint_remote.py caches` sends it over standard input)
has no `ram_dirs` and always answers with the disk path. The budget, the prune and the exclusive lock work the same in RAM; a RAM directory unused for 2 days (not 14) is removed, the whole cache is held to `HAMS_RAM_LINT_GB` (24) by `prune_lint_cache` and reported by a `lint cache: RAM cache X GB of Y GB budget` line, and a legacy directory is never adopted into RAM.

### `[@ANCHOR: lint_cache:cargo_bases]` `cargo_bases(environ=None)`

Purpose: every base that may hold Cargo caches, so `show`, `prune` and `clear` see both. Returns a list: the RAM base when it exists (and `XDG_CACHE_HOME` is unset), then `disk_cargo_base`.

### `[@ANCHOR: lint_cache:in_ram]` `in_ram(path, environ=None)`

Purpose: whether `path` is below the RAM root. Returns bool; False when `ram_dirs` cannot be imported.

### `[@ANCHOR: test:default_test_tmpdir]` `default_test_tmpdir(environ, directory=DEFAULT_TEST_TMPDIR, create=True)`

Purpose: the TMPDIR of a run that shares the host: RAM when `ram_dirs.test_tmp_base` offers it, else what `apply_default_test_tmpdir` chose before. `main()` and the start-up sweep call it.

| Argument | Type | Meaning |
|---|---|---|
| `environ` | dict | Mutated: `TMPDIR` is set when the caller set none. |
| `directory` | str | The disk directory (default `/var/lib/odoo/tmp`). |
| `create` | bool | False: do not create the RAM base (the start-up sweep). |

Returns the TMPDIR in force, or `None`. A TMPDIR the caller chose is never replaced. Inside the full pipeline's private namespace the answer is the disk directory (a memory overlay there already). If RAM is refused for lack of space a warning is printed (`ram_dirs`) and the disk directory is used.

### `[@ANCHOR: test:ram_chrome_profile_globs]` `ram_chrome_profile_globs(resources, tmpdir)`

Purpose: keeps the end-of-run Chrome profile removal correct after TMPDIR moved. Arguments: `resources` (`SlotResources`), `tmpdir` (the TMPDIR in force). Returns a tuple of globs: `resources.chrome_profile_globs`, plus `<tmpdir>/*_chrome_odoo` when the run
is not slot-scoped and `tmpdir` is in RAM. A scoped run's globs and kill pattern already name its own `slot-K` directory (`/dev/shm/hams-test-tmp/slot-K/...`; the characters are in the safe set, so the pattern matches only that slot's Chrome), so they are returned unchanged.

## The dev box is the primary lint runner: slots

Bruce, 2026-11-08: "we aren't limited by those less powerful machines." The dev box (32 cores, 125 GB) runs the lint itself, every step
including `check_cargo_fmt`, `check_cargo_clippy` and `check_cargo_deny`, with several heavy runs side by side. Its toolchain is the
repositories' pinned Rust (rustup, user level, `provision_build_host.sh apply-user`: rustfmt, clippy, the `wasm32-unknown-unknown` target).
`lint_machines.json` gives it `"lint_slots": 4`: four runs at once, one lock file per slot (`lint_lock.py`), each run at `nice` 10 with its own
TMPDIR (slots above 1: `<TMPDIR>/s<K>`) and its own warm Cargo target directory (`-sK`). A fifth run takes one of the **burst slots** (`lint_burst_slots`: 8) while the processors are mostly idle (`burst_headroom`, one new burst slot per 20 s) and otherwise queues in the same FIFO. The other machines
keep one slot. `--remote auto` no longer spreads steps to idle machines: another machine takes a **whole** run only as overflow, when every slot
is taken or a run waits (a high load alone does not send it away: the run waits at the load gate, whose limit is 1.5 per core here), and a run whose overflow machine is claimed or unreachable simply waits for a
slot here: it always ends with a real result and a stamp.

### `[@ANCHOR: lint_lock:slot_count]` `slot_count(environ=None, machines_file=None)`

Purpose: how many heavy lint runs this machine lets go at once. `RunLock` and `local_busy` ask it.

| Argument | Type | Meaning |
|---|---|---|
| `environ` | dict or `None` | Environment to read (`HAMS_LINT_SLOTS`, `HAMS_LINT_MACHINE`, `XDG_CACHE_HOME`); `None` means `os.environ`. |
| `machines_file` | str or `None` | The inventory to read; `None` means `lint_machines.json` beside the module. |

Returns an int from 1 to `runner_slots.MAX_SLOTS` (15): `HAMS_LINT_SLOTS` when set and valid, else `lint_slots` of the machine's entry
(`lint_cache.machine_name`: the entry whose `hostnames` hold this host's name, else `devbox`), else 1. A value that is not a whole number in range
gives 1 rather than an error (a typo must not stop the lint). Special concerns: reads one small file, no other side effects. Tests: `SlotTests` in `test_lint_lock.py`.

### `[@ANCHOR: lint_lock:burst_slot_count]` `burst_slot_count(environ=None, machines_file=None)`

Purpose: how many EXTRA lint slots the machine opens while it has spare processors (written 2026-11-08 after a day in which the dev box sat at 5 to 10 % CPU while fifteen agents waited
for its four slots: a run uses about two cores on average, because its longest step is serial; 75 of 142 measured runs queued for a slot). Returns an int from 0 to `runner_slots.MAX_SLOTS`
minus `slot_count`: `HAMS_LINT_BURST_SLOTS` when set, else `lint_burst_slots` of the machine's entry in `lint_machines.json` (the dev box: 8), else 0. A value that is not a whole number
gives 0 (a typo must not add runs); a sum above 15 is cut. A burst slot is taken only when `burst_headroom` holds (`RunLock._try_burst`); the base slots start as before. Reads one small file, no
side effects. Tests: `BurstSlotTests` in `test_lint_lock.py`.

### `[@ANCHOR: lint_lock:total_slot_count]` `total_slot_count(environ=None, machines_file=None)`

Purpose: base plus burst slots, the most heavy runs that can be live at once: what the FIFO queue (`lock_queue` `slots`), `--lock-status` and `RunLock.slots` count. Returns an int
(`slot_count + burst_slot_count`). `jobs_per_slot` follows the base count (a burst run has as many parallel steps as a base run); a burst slot K has its own TMPDIR and Cargo directory (`-sK`) like any slot. Tests: `BurstSlotTests`.

### `[@ANCHOR: lint_lock:burst_headroom]` `burst_headroom(busy, psi, mem_gb, busy_limit, psi_limit, min_mem_gb)`

Purpose: the test for opening a burst slot, stricter than the saturation gate (`saturated`): the base slots start whenever the box is not saturated (busy at least 92 % AND stall at least 25 %), a
burst slot only when it is mostly idle. Arguments: `busy` percent 0 to 100 (`cpu_busy_percent`), `psi` percent (`cpu_stall`), `mem_gb` available GiB (`mem_available_gb`); any of the three may be
None. Returns `(ok, reason)`: ok when `busy < busy_limit`, `psi < psi_limit` and `mem_gb >= min_mem_gb`; a signal that is None refuses (nothing is added blind). Pure. Tests: `BurstSlotTests`.

### `[@ANCHOR: lint_lock:burst_limits]` `burst_limits(environ=None)`

Purpose: the four numbers a burst slot needs. Returns `(busy percent, pressure percent, minimum available GiB, spacing seconds)` from `HAMS_LINT_BURST_BUSY_PERCENT` (60),
`HAMS_LINT_BURST_PSI_PERCENT` (10), `HAMS_LINT_BURST_MIN_MEM_GB` (24) and `HAMS_LINT_BURST_SPACING_SECONDS` (20); an unreadable or non-finite value means its default. Pure. Tests: `BurstSlotTests`.

### `[@ANCHOR: lint_lock:mem_available_gb]` `mem_available_gb(path="/proc/meminfo")`

Purpose: the machine's `MemAvailable` in GiB. Returns a float, or None when the file cannot be read or has no such line. Reads one file. Tests: `BurstSlotTests`.

### `[@ANCHOR: lint_lock:try_burst]` `RunLock._try_burst(headroom=None, now=time.time)`

Purpose: one non-blocking attempt to take a burst slot (slots above `slot_count`). Takes an flock of its own, `<cache>/hams-lint/burst.admit`, with LOCK_NB: a waiter that cannot get it returns False
and tries again at its next poll, so only one waiter at a time looks at the machine. Under that lock it refuses when another burst slot was taken less than `HAMS_LINT_BURST_SPACING_SECONDS` ago (the
mtime of the file; a run that has just started has not loaded the processors yet, so without the spacing every waiter would see an idle box and start at once), or when `burst_headroom` fails
(0.5 s of `/proc/stat`, `/proc/pressure/cpu`, `/proc/meminfo`; `headroom`, a function returning bool, replaces the measurement in tests), else takes the lowest free burst slot, stamps the file and
returns True with the slot in `self.slot` and its lock in `self.fd`. Returns False when no burst slot is free. Blocks for the sample only. Tests: `BurstSlotTests`.

### `[@ANCHOR: lint_lock:machine_load_ratio]` `machine_load_ratio(environ=None, machines_file=None)`

Purpose: the load-per-core ratio of this machine's lint load gate. `load_limit()` multiplies it by the core count unless `HAMS_LINT_LOAD_LIMIT`, `--load-limit` or
`HAMS_CI_LOAD_GATE_RATIO` says otherwise. Returns the `lint_load_ratio` of the machine's `lint_machines.json` entry (the dev box: 1.5, because with several lint slots and
niced cargo tests its load runs above the core count by design), else 1.0 (also for a missing or unreadable file, or a value that is not a positive number). Special
concerns: reads one small file; no side effects. Tests: `test_limit_and_timeout_precedence` in `test_lint_lock.py`.

### `[@ANCHOR: lint_lock:slots_files]` `slots_files(environ=None)`

Purpose: the setting files `slot_count` reads for the number of lint slots, so an administrator can change it without editing the repository, as test.py's `/opt/hams/etc/test_slots` does.
Returns a list of paths, first match wins: `[$HAMS_LINT_SLOTS_FILE]` alone when that is set (tests), else `["/opt/hams/etc/lint_slots", "<HOME>/.config/hams-lint/slots"]`. The first
line of the file is the number (1 to 15). A file that does not exist is skipped; one whose first line is not a whole number in range means one slot (a typo must not become the inventory's
default). Argument `environ`: dict or `None` for `os.environ`. No side effects. Tests: `test_slot_count_reads_the_setting_files_in_order` in `test_lint_lock.py`.

### `[@ANCHOR: lint_lock:jobs_per_slot]` `jobs_per_slot(cpu_count, slots)`

Purpose: the default `-j` of a lint run. A lone run starts half the CPUs of parallel steps; with several slots each run takes its share, so that all slots together do not start more
workers than half the CPUs (the dev box: 32 threads, 4 slots, 4 each). Arguments: `cpu_count` int, `slots` int. Returns an int, at least 2 with several slots, at least 1 always.
`$HAMS_LINT_JOBS` and `-j` still override. Pure. Tests: `test_jobs_are_shared_out_so_all_slots_together_stay_at_half_the_cpus` in `test_lint_lock.py`.

### `[@ANCHOR: lint_lock:cpu_stall]` `cpu_stall(path="/proc/pressure/cpu")`

Purpose: one input of the lint load gate, the same figure test.py's CI load gate reads (`read_cpu_pressure`, #438). Returns `some avg10` of the kernel's pressure file as a float
percentage (the share of the last ten seconds in which a runnable task waited for a CPU), or `None` when the file is missing, unreadable or not a finite number. No side effects. The load
average is not used because it counts every runnable or disk-waiting task: it was 86 on the dev box at 18 % stall (2026-11-08). Argument `path`: tests pass a temporary file.
Tests: `test_cpu_stall_reads_psi_some_avg10_and_the_gate_prefers_it_to_the_load_average` in `test_lint_lock.py`.

### `[@ANCHOR: lint_lock:cpu_busy_percent]` `cpu_busy_percent(window=1.0, path="/proc/stat", sleep=time.sleep)`

Purpose: the second input, the processors' busy percentage (0 to 100) over `window` seconds from two samples of /proc/stat's first line (everything but idle and iowait). Returns a float, or
`None` when the file cannot be parsed or the counters did not move. Blocks for `window` seconds. Same test.

### `[@ANCHOR: lint_lock:saturated]` `saturated(busy, psi, busy_limit, psi_limit)`

Purpose: test.py's rule (`evaluate_cpu_saturation_gate`) for the lint gate. Returns `(bool, reason)`: saturated only when BOTH `busy` and `psi` are at or above their limits (92 % busy and 25 % pressure
by default); a missing signal leaves the other to decide alone; neither means not saturated. Pure. Limits come from `saturation_limits()`: `HAMS_CI_LOAD_GATE_CPU_BUSY_PERCENT`, `HAMS_CI_LOAD_GATE_PSI_PERCENT`.
Same test.

### `[@ANCHOR: lint_lock:wait_for_stall]` `wait_for_stall(timeout, environ=None, busy=cpu_busy_percent, psi=cpu_stall, sleep=..., now=..., out=print)`

Purpose: block until the processors are not saturated. Returns True when the run may start (also at once when `HAMS_SKIP_CI_LOAD_GATE=1` or nothing can be measured), False after `timeout`
seconds (it prints why). Polls every 15 s. Same test.

### `[@ANCHOR: lint_lock:gate]` `gate(environ, load_limit_override=None, load_timeout_override=None, out=print, stall=cpu_stall, measurable_busy=...)`

Purpose: the one load gate `begin_heavy_run` applies. Uses `wait_for_stall` when the saturation can be measured and nobody set a load limit (`--load-limit`, `HAMS_LINT_LOAD_LIMIT`,
`HAMS_CI_LOAD_GATE_RATIO`); otherwise the 1-minute load average against `load_limit`, as before. Returns True when the run may start. Same test.

### `[@ANCHOR: lint_lock:slot_lock_path]` `slot_lock_path(slot, environ=None)`

Purpose: the lock file of one slot. Slot 1 is `~/.cache/hams-lint/run.lock`, the file a checkout without slots takes, so old and new runs exclude
each other on it; slot K is `run.lock.slot-K` beside it. `slot` is the 1-based number; `environ` as in `slot_count`. Returns the path (str); creates nothing.
Tests: `SlotTests` in `test_lint_lock.py`.

### `[@ANCHOR: lint_lock:enter_slot]` `enter_slot(lock, environ=None, base_tmp=None)`

Purpose: gives a run that holds a slot of a multi-slot machine its own working space. `run_linters.py` calls it right after `begin_heavy_run`.

| Argument | Type | Meaning |
|---|---|---|
| `lock` | `RunLock` or `None` | The lock the run holds; `None` (no lock taken) changes nothing. |
| `environ` | dict or `None` | Source of `TMPDIR`; `None` means `os.environ`. |
| `base_tmp` | str or `None` | Replaces the TMPDIR base (tests). |

Returns the environment changes as a dict (`HAMS_LINT_SLOT`, `TMPDIR`), `{}` for a run with no lock or a single-slot machine. Special concerns: calls
`os.nice(10)` (children inherit it), creates `<TMPDIR or /tmp>/s<K>` for K above 1 (slot 1 keeps TMPDIR; the name is short because Chrome's singleton socket path must stay under 107 bytes), and updates `os.environ`; the caller passes the dict on to steps
whose environment was captured earlier. Tests: `test_a_run_in_a_later_slot_gets_its_own_tmpdir_and_environment` in `test_lint_lock.py`.

### `[@ANCHOR: lint_lock:running_count]` `running_count(qdir)`

Purpose: how many runs hold a slot now, counted from the live holder records of the queue directory `qdir` (`lint_lock.queue_dir()`). Returns an int; records of dead
processes are ignored and removed. Tests: `test_three_runs_hold_three_slots_at_once_and_the_fourth_waits` in `test_lint_lock.py`.

### `[@ANCHOR: lint_cache:cargo_target_dir]` `cargo_target_dir(repo_name, environ=None)`

Purpose: the persistent Cargo target directory of a repository on this machine: `~/.cache/hams-lint-cargo/<repo>-<rustc release>-<8 hex of rustc -vV>`, with `-sK`
appended in lint slot K above 1 (`HAMS_LINT_SLOT`, set by `RunLock.acquire`), so runs side by side never share a build directory. Returns the path (str); creates
nothing. The directory is pruned to the machine's budget (`cache_budgets`) as before. Tests: `test_a_run_in_a_later_slot_gets_its_own_tmpdir_and_environment` in `test_lint_lock.py`.

### `[@ANCHOR: lint_remote:prefer_user_toolchain]` `prefer_user_toolchain(environ=None)`

Purpose: puts `~/.cargo/bin` (rustup's cargo, rustfmt and clippy at the pinned version) first on PATH when it holds a `cargo`, so the lint uses it and not the distribution's
component-less `/usr/bin/cargo`. `run_linters.main()` calls it first. Argument `environ`: dict or `None` for `os.environ`. Returns True when PATH changed. Tests:
`test_prefer_user_toolchain_puts_rustup_first_once` in `test_lint_remote.py`.

### `[@ANCHOR: lint_remote:apply_reserve]` `apply_reserve(machine, probe, jobs)`

Purpose: the `reserve` policy of a `lint_machines.json` entry, for a machine that a person also uses interactively and that therefore takes overflow only. `lint_remote.make_plan()` calls it for every
candidate machine before the load check. Keys of `reserve` (all optional): `skip_when_user_session` (true: skipped while any login session is open, as `who` reports in the probe's `sessions=` line),
`max_core_fraction` (the run gets at most that share of the cores, at least one job) and `never_caps` (capabilities hidden from the router, e.g. `["chrome"]`, so a browser step is never sent there).
The entry also uses the existing `nice`, `max_concurrent` and `max_load_ratio` (a low ratio is the low load limit). An entry with `pending_address: true` and no `ssh` is listed as not a runner until an `ssh` entry is added.

| Argument | Type | Meaning |
|---|---|---|
| `machine` | dict | The inventory entry. |
| `probe` | `Probe` | What the probe measured (`sessions`, `cores`, `caps`). |
| `jobs` | int | The entry's worker count before the policy. |

Returns `(probe, jobs, skip_reason)`: the probe with the hidden capabilities removed, the limited job count, and a non-empty reason when the machine must be skipped. Pure. Tests: `ReserveTests` in `test_lint_remote.py`.

### `[@ANCHOR: lint_remote:expected_blobs]` `expected_blobs(repo, sha, sub_exclude=())`

Purpose: the exact file set a machine's mirror of `repo` must hold to equal what the local run reads, as `{path: git blob id}`: the files tracked in commit `sha`, minus secrets and build output
(`excluded`) and `sub_exclude` (a nested repository mirrored on its own, hams_open's `hams_shared`), with the working tree's uncommitted regular files at their present content and its deleted files
gone. A blob id is the SHA-1 of `blob <size>\0<content>`, the same on every machine. Paths with a newline or a tab are left out. Reads `repo` through git only. Used by `mirror_problems()`.
Tests: `test_a_mirror_at_an_older_commit_than_its_manifest_says_is_rebuilt_before_any_step_runs` and `test_uncommitted_files_of_the_branch_under_test_are_part_of_the_exact_tree` in `test_lint_remote.py`.

### `[@ANCHOR: lint_remote:mirror_problems]` `mirror_problems(transport, repo, sha, dest, sub_exclude=(), log=print)`

Purpose: proves a remote mirror equals the local tree before any step reads it (2026-11-08: steps on a remote machine linted a stale sibling hams_open and failed a branch against commits main had already
fixed; the same staleness could pass one). It hashes every expected file on the machine (`BLOB_SCRIPT`, standard-library Python, paths on stdin) and compares with `expected_blobs()`. Returns the problems
left, `missing: <path>` and `differs: <path>`; empty means the mirror is exact. Files in the mirror that the local tree does not have are removed (logged) unless they are caches or build output
(`MIRROR_JUNK_DIRS`, `MIRROR_JUNK_FILES`). `run_on_machine()` calls it for hams_com, hams_open and hams_shared (`verify_mirrors`) after the sync; a mirror with problems is rebuilt in full once, and when it
still differs the machine returns `infra`: nothing runs there, the steps run on this machine and the run says why. Does not trust the mirror's own manifest.

| Argument | Type | Meaning |
|---|---|---|
| `transport` | SshTransport or ShellTransport | How to run commands on the machine. |
| `repo`, `sha` | str | The local repository and the commit its HEAD names. |
| `dest` | str | Absolute mirror path on the machine. |
| `sub_exclude` | tuple of str | Paths mirrored separately (skipped here). |

Tests: `EndToEndTests.test_a_mirror_at_an_older_commit_than_its_manifest_says_is_rebuilt_before_any_step_runs`, `test_a_mirror_that_cannot_be_brought_to_the_local_commits_runs_nothing_there`,
`test_files_left_behind_by_an_earlier_sync_are_removed_and_caches_are_ignored`.

### `[@ANCHOR: lint_remote:siblings_text]` `siblings_text(info)`

Purpose: names the commits a remote run read: `hams_com@<12 hex> hams_open@<12 hex> hams_shared@<12 hex>`, `+dirty` after a commit whose uncommitted files were shipped too. `run_on_machine()` prints it as the
first line of the machine's log (`[host] siblings: ...`) and `collect()` returns it for the pass stamp (`siblings=<host>/<role>@<sha>/...`, `run_linters.stamp_text`). Pure. Test:
`test_a_mirror_at_an_older_commit_than_its_manifest_says_is_rebuilt_before_any_step_runs`; the stamp field is in `test_stamp_metadata_names_the_host_and_the_gate_still_parses_it` (`test_lint_remote.py`).

### `[@ANCHOR: lint_lock:held_locks]` `HeldLocks`

Purpose: a handle that stands for several open lock files at once, so a lint run's claim on a machine and its hold on that machine's
Odoo-test lock are released together when the run's share ends. Defined in `lint_lock.py`; created by `lint_remote.dispatch()`;
closed by `RemoteJob._run()` (through its `claim` attribute) in a `finally`.

| Field | Type | Meaning |
|---|---|---|
| `handles` | list of open file objects | The locks held, in the order given. `None` entries passed to the constructor are dropped. |

Constructor: `HeldLocks(handles)` with an iterable of open files or `None`. `close()` closes every handle and returns `None`; it may be
called again (closing a closed file is a no-op). Invariants: after `close()` every flock it owned is released (the kernel also drops them
if the process dies). Lifetime: owned by one `RemoteJob`; never shared between threads, never persisted. Anything with a `close()` method
can be used where a claim is expected, which is why this quacks like the plain file `try_claim()` returns.

### `[@ANCHOR: lint_lock:try_odoo_test_lock]` `try_odoo_test_lock(lock_name, directory=None)`

Purpose: lets a lint run use a remote host that also runs Odoo tests only while no Odoo test run uses it, and keeps Odoo test runs off it
meanwhile. `lint_remote.dispatch()` calls it (through `run_linters.py`) for every machine chosen for a run whose entry in
`lint_machines.json` has `"odoo_test_lock": "<name>"` (the mac1 VM: `mac1`; bruce17: `bruce17`). The name is the lock file of
`devbox_tools/remote_odoo_test.py` (`~/.cache/hams-remote-test/<name>.lock`, `remote_odoo_test.lock_path`), the same file
`remote_cargo_test.try_lock` takes for relay builds.

| Argument | Type | Meaning |
|---|---|---|
| `lock_name` | str | Odoo-test host name, without `.lock`. |
| `directory` | str or `None` | Lock directory; `None` means `~/.cache/hams-remote-test` (`ODOO_TEST_LOCK_DIR`). Tests pass a temporary directory. |

Returns the open, exclusively flocked file (keep it open for as long as the machine is in use; close it to release), stamped with
`pid N lint run since <time>`; or `None` when another process holds the lock (an Odoo test run, a relay build, another lint run) or the
directory cannot be created. It never raises for those causes and never waits.

Special concerns: non-blocking; creates the directory and the lock file; no other side effects; safe to retry. The caller treats `None` as
"this machine is busy": the machine is excluded and the run is planned again without it (whole or spread plans alike), so a lint run never
queues behind an Odoo test or the reverse. The lock is not inherited by child processes. See also `HeldLocks`, `make_plan()` (its
`exclude` set is honoured in every mode), and the tests `test_the_odoo_test_lock_is_exclusive_with_an_odoo_run_and_released_together_with_the_claim`
in `test_lint_lock.py` and `test_a_machine_whose_odoo_test_lock_is_taken_is_not_used_and_the_lock_is_kept_for_the_run` in
`test_lint_remote.py`.

### `[@ANCHOR: test:signal_exit_verdict]` `signal_exit_verdict(returncode, watchdog_fired, odoo_result_lines)`

Purpose: turns an Odoo process that a signal killed into a readable status and summary line. Before it, `run_cmd()` returned the
negative `subprocess` code and `sys.exit(-9)` appeared as exit 247, and a run killed after `odoo.tests.result: 0 failed` looked like an
ordinary failure. `run_cmd()` calls it once per run, after the process is reaped.

| Argument | Type | Meaning |
|---|---|---|
| `returncode` | int | `Popen.returncode`: 0 or positive is a normal exit; `-N` means signal N. |
| `watchdog_fired` | bool | True when this runner's `OOMWatchdog` set its `fired` event, that is, it sent the kill itself. |
| `odoo_result_lines` | list of dict or `None` | `FailureExtractor.odoo_result_lines`: dicts with integer `failed`, `errors`, `total` for each `odoo.tests.result:` line seen. |

Returns `(exit status, line)`. For a normal exit: `(returncode, None)`. For signal N: `(128 + N, line)`, the shell convention (SIGKILL: 137,
SIGTERM: 143), and a line of the form `Odoo was killed by SIGKILL (signal 9; OOM watchdog: yes|no): exit status 137.` followed by either the
verdict Odoo had printed ("still a failure because the process did not exit normally") or "no test verdict exists". A run is a failure in
every signal case. An unknown signal number is named `signal N`. `run_cmd()` prints the line, logs it at error level and stores it in
`FailureExtractor.signal_line`, which `finish_and_write()` prints in the summary unless the CPU-limit message (`cpu_limit_verdict`) already
explains the kill.

Special concerns: pure, no I/O, no locks. The status changed from 256 - N (247) to 128 + N, so anything that matched 247 must match 137.
See also the test class `SignalExitVerdictTests` in `test_test.py`, `OOMWatchdog` and `cpu_limit_verdict()`.

## The CI load gate (`wait_for_ci_load_to_subside`)

Before a run takes a slot, `test.py` waits while the box is too busy to test on (tours and `url_open` tests fail on their own ten-second timeouts
when the processors have no spare time). The signal is **CPU saturation**, not the load average. The one-minute load average counts every
runnable and every uninterruptible task, so a box running hundreds of short-lived Chrome, PostgreSQL and test processes reads 25 to 50 on 32 cores while
the processors are mostly idle (Bruce, 2026-11-08: a load average in the thirties is normal here); the old rule (cores x ratio, default 32) held runs
back for nothing. The gate holds the run only while

* a GitHub Actions `Runner.Worker` process is running (unchanged), or
* the processors are busy at or above `HAMS_CI_LOAD_GATE_CPU_BUSY_PERCENT` (default 92) over a 1 s window of `/proc/stat` AND the kernel's pressure-stall
  figure `some avg10` in `/proc/pressure/cpu` is at or above `HAMS_CI_LOAD_GATE_PSI_PERCENT` (default 25). With one of the two unavailable the other decides
  alone; with neither the box is not busy. Pressure is read first: below its limit no sampling window is needed and the gate costs one file read.

`HAMS_CI_LOAD_GATE_RATIO` alone is IGNORED, with a loud warning (`legacy_load_gate_ratio`): it switched the gate to the legacy load-average rule (load average above cores x ratio) that Bruce rejected (a load average in the thirties is normal on the dev box) and overrode the CPU and pressure rule, so runs started from an old command line stalled. With `HAMS_CI_LOAD_GATE_LEGACY=1` as well the old rule applies in addition. `HAMS_CI_LOAD_GATE_TIMEOUT`
(default 900 s) and `HAMS_SKIP_CI_LOAD_GATE=1` are unchanged. The gate runs per run, when the run reaches the head of the wait queue, not under a lock: with
several free slots the oldest waiters (as many as there are free slots) are all heads and gate at the same time, so one run's gate never holds the other slots
behind it. The lint run lock (`lint_lock.py`) has its own load gate and is not changed.

### `[@ANCHOR: test:read_cpu_counters]` `read_cpu_counters(proc_root="/proc")`

Purpose: one sample of the processors' time. Returns `(busy, total)` jiffies summed over all processors from the first line of `<proc_root>/stat`: `busy` is everything but
idle and iowait (a processor waiting for the disk has spare time), `total` the sum of the first eight counters (guest times are already inside user and nice). `None` when the
file is unreadable or malformed. Reads one small file.

### `[@ANCHOR: test:read_cpu_pressure]` `read_cpu_pressure(proc_root="/proc")`

Purpose: the `some avg10` value of `<proc_root>/pressure/cpu`: the percentage of the last ten seconds in which at least one runnable task waited for a processor. Returns a float,
or `None` when the kernel has no pressure-stall information, the file is unreadable, or the value is not finite.

### `[@ANCHOR: test:measure_cpu_busy_percent]` `measure_cpu_busy_percent(sleep_func=time.sleep, window=1.0, read=read_cpu_counters)`

Purpose: the processors' busy percentage (0 to 100) over `window` seconds: a sample, `sleep_func(window)`, a second sample. Returns `None` when either sample fails or the counters did
not advance. Blocks for `window`.

### `[@ANCHOR: test:evaluate_cpu_saturation_gate]` `evaluate_cpu_saturation_gate(runner_workers, cpu_busy_percent, psi_some_avg10, busy_limit, psi_limit, load_average_1min=None, cpu_count=1, load_ratio=None)`

Purpose: the gate's decision, pure so that every boundary is testable without a loaded machine.

| Argument | Type | Meaning |
|---|---|---|
| `runner_workers` | list | `find_runner_worker_processes()`; any entry makes the box busy. |
| `cpu_busy_percent` | float or `None` | `measure_cpu_busy_percent()`. |
| `psi_some_avg10` | float or `None` | `read_cpu_pressure()`. |
| `busy_limit`, `psi_limit` | float | The percentages at or above which each signal counts as saturated. |
| `load_average_1min`, `cpu_count`, `load_ratio` | float, int, float or `None` | The legacy rule (`evaluate_ci_load_gate`), applied only when `load_ratio` is not `None`, which `wait_for_ci_load_to_subside` passes when `HAMS_CI_LOAD_GATE_RATIO` is set. |

Returns `(busy, reason)`; `reason` is the sentence printed to the operator and is empty when not busy. Busy only when both signals are at or above their limits (busy time with no waiting
task is a box that is keeping up; waiting tasks with idle processors are the many-short-processes case the load average mistakes for load).

## Concurrent test slots (ADR 0116)

`test.py` allows one run per machine unless the machine opts in. An opted-in machine runs up to N `test.py` instances at once; each holds
one numbered **slot**, taken with a flock of its own, and everything two runs would otherwise share is derived from the slot number. Without
the opt-in the count is 1, slot 1 is derived, and every value equals what `test.py` used before slots existed.

Opt in with `HAMS_TEST_SLOTS=N` or the first line of `/opt/hams/etc/test_slots` (the environment outranks the file); `--slots N` can only
RAISE that count. Every run reads the machine's setting, so every run on one machine agrees on N and queues in the same way: a run that
asked for fewer slots than the machine has once took slot 1 only, and the slotted runs behind it sat idle with three of four slots free.
To run with one slot set `HAMS_TEST_SLOTS=1`. `test.py --queue-status` shows each slot. The values below are for a run in slot K of N.

**The supported way to run slots is the full pipeline** (what `python3 hams_shared/tools/test.py ...` does with no `HAMS_ISOLATED_NS`): every
run gets its own mount, PID and network namespace with its own Redis and RabbitMQ and the same ports (8075, 6379, 5672) in every run, so
nothing but the database name differs, broker modules run in any slot, and `--pause-on-fail` has a debugging port of its own. Set
`HAMS_ISOLATED_NS=1` only to skip the namespace on purpose; such a run shares the host's network and is limited above slot 1 (the
messages say so and say to leave it out). The one limit of the full pipeline above slot 1 is `--mcp`: its clients are configured for one
server and a server in a private namespace cannot be reached from outside.

| Resource | Slot 1 (and every machine with one slot) | Slot K > 1 |
|---|---|---|
| Lock file | `/tmp/hams_odoo_test_runner.lock` (`HAMS_TEST_LOCK_PATH`) | the same path plus `.slot-K` |
| Queue directory (`--wait`) | `<lock>.queue.d` | the same one: a single FIFO for every slot |
| PostgreSQL database (`--db`, default `hams_test`) | `--db` | `<db>_sK`, for example `hams_test_relay_s2`; Odoo's filestore directory is named after the database, so it differs too |
| Odoo HTTP port | 8075 | on the host: 8075 + K - 1; in the private namespace: 8075 |
| Redis database | tests use 1 (the flush is `$REDIS_DB`, default 0) | on the host: K, flushed and used; in the private namespace: unchanged |
| RabbitMQ | the host's, or the private one the full pipeline starts | the private one the full pipeline starts, for any module; only a `HAMS_ISOLATED_NS=1` run on the host is refused when a RabbitMQ listens on 5672 and a module under test mentions it (`pika`, `amqp`, `rabbitmq` in its Python); other modules run |
| `TMPDIR` (Chrome profiles) and the Chrome kill and clean-up patterns | `/var/lib/odoo/tmp`; every `*_chrome_odoo` | on the host with several slots: `<TMPDIR>/slot-K` (slot 1: `<TMPDIR>`), patterns limited to the run's own directory |
| Log directory files | `filtered_test.txt`, `test_progress.txt`, `coverage_report`, `odoo_test.pro`, `offline_ns`, `log` | `filtered_test.slot-K.txt`, `test_progress.slot-K.txt`, `coverage_report.slot-K`, `odoo_test.slot-K.pro`, `offline_ns.slot-K`, `log.slot-K` |
| Chrome debugging port | 9222 only with `--pause-on-fail` | private in the full pipeline; `--pause-on-fail` on the host (`HAMS_ISOLATED_NS=1`) and `--mcp` anywhere are refused above slot 1 |
| What the run removes when it ends | nothing (a run with one slot keeps `hams_test`; the next run drops and rebuilds it) | in slot mode (more than one slot), on every slot including 1: its database (dropped WITH (FORCE)), its Odoo filestore and its slot TMPDIR, whether it passed, failed or was stopped, unless `--keep-db` (see "Clean-up in slot mode") |

"On the host" means a `HAMS_ISOLATED_NS=1` run, which skips the namespace; "private namespace" is the final process of the full pipeline
(`HAMS_ISOLATED_NS=1` and `HAMS_TEST_LOCK_HELD=1`), whose ports, Redis, RabbitMQ, process table and `/tmp` belong to that run alone. Not
shared by design: PostgreSQL itself (one server, distinct databases), and the `odoo`/`root`-named roles every run creates if missing.

The root phase of the full pipeline is started with `--slots N --slot-held K` appended to its command line (`slot_handoff_args`). A command
line survives sudo, systemd-run and unshare; an environment variable might not, and a child that lost its slot would have taken slot 1.

### `[@ANCHOR: runner_slots:parse_slot_count]` `parse_slot_count(text, source)`

Purpose: validates one slot count, so that a typo is an error and never quietly one slot.

| Argument | Type | Meaning |
|---|---|---|
| `text` | str or int | The value. Surrounding white space is ignored; only the ASCII digits `0`-`9` are accepted. |
| `source` | str | Where it came from (`--slots`, `HAMS_TEST_SLOTS`, a file path); named in the error. |

Returns the count as int, 1 to `MAX_SLOTS` (15: Redis has 16 databases and database 0 belongs to the host). Raises `SlotConfigError` (a
`ValueError`) for an empty string, a sign, a decimal point, an exponent, a non-ASCII digit, 0, or a count above 15. Pure; no locks or I/O.

### `[@ANCHOR: runner_slots:configured_slot_count]` `configured_slot_count(cli_value=None, environ=None, slots_file="/opt/hams/etc/test_slots")`

Purpose: how many runs this machine allows at once; the only place the opt-in is read.

| Argument | Type | Meaning |
|---|---|---|
| `cli_value` | str, int or None | The `--slots` value; it can only raise the count (the result is the larger of it and the machine's setting). |
| `environ` | mapping or None | Where `HAMS_TEST_SLOTS` is read; default `os.environ`. An empty value counts as unset. |
| `slots_file` | str | A file whose first line is the count. |

Returns the count (1 to 15): the larger of `cli_value` (1 when None) and the machine's setting, which is `HAMS_TEST_SLOTS`, else the first line of
`slots_file`. A missing `slots_file` means 1: the machine has not opted in. Raises `SlotConfigError` for an invalid value from
any source and `OSError` for a file that exists and cannot be read; `test.py` prints the cause and exits 2, it never falls back to 1. One file
read, no locks, no side effects. Only a fresh `test.py` invocation calls it; the phases it re-execs take the count from `--slots`.

### `[@ANCHOR: runner_slots:slot_lock_path]` `slot_lock_path(base_path, slot)`

Purpose: the lock file of a slot. Returns `base_path` for slot 1 and `base_path + ".slot-" + slot` otherwise (`base_path` is
`HAMS_TEST_LOCK_PATH` or `/tmp/hams_odoo_test_runner.lock`). Slot 1 is the original file on purpose: a `test.py` without slots, or a machine
that has not opted in, takes exactly that file, so it and a slotted run exclude each other on slot 1 instead of sharing its database and
ports. The queue directory is always derived from the base path, never from a slot's path. Pure.

### `[@ANCHOR: runner_slots:slot_database_name]` `slot_database_name(database, slot)`

Purpose: the PostgreSQL database a run uses. Returns `database` unchanged for slot 1 and `database + "_s" + slot` otherwise. Odoo's filestore
is the directory `filestore/<database>`, so distinct names also give each slot its own filestore, which `remove_stale_filestore()` then
clears without touching a sibling's. Raises `SlotConfigError` when the name exceeds 63 bytes (PostgreSQL would truncate it silently and two
slots could end up on one database). The caller has already validated `database` against `_SAFE_DB_NAME_RE`; the suffix keeps the name safe.

### `[@ANCHOR: runner_slots:slot_file_name]` `slot_file_name(name, slot)`

Purpose: names a file or directory in the log directory per slot. Slot 1 returns `name`; otherwise the tag `.slot-K` is inserted before the
extension (`filtered_test.txt` becomes `filtered_test.slot-2.txt`, `coverage_report` becomes `coverage_report.slot-2`). Concurrent runs
overwrote each other's report, progress file and coverage data in the shared directory before this. Pure.

### `[@ANCHOR: runner_slots:in_private_network]` `in_private_network(environ)`

Purpose: tells the final process of the full pipeline from a direct `HAMS_ISOLATED_NS=1` run. Returns True only when `HAMS_ISOLATED_NS` and
`HAMS_TEST_LOCK_HELD` are both `"1"`: the root phase sets them just after it unshared the network namespace, and a direct run sets only the
first. Pure.

### `[@ANCHOR: runner_slots:SlotResources]` `SlotResources`

A frozen dataclass: what a run in `slot` of `slots` uses that another slot must not. Built by `slot_resources`, read by `test.py`'s `main()`;
it lives for one run in one process and is never serialized or versioned.

| Field | Type | Meaning |
|---|---|---|
| `slot`, `slots` | int | This run's slot (1 to `slots`) and the machine's slot count. |
| `private_network` | bool | True inside `test.py`'s own network namespace (`in_private_network`). |
| `scoped` | bool | `slots > 1 and not private_network`: the run shares the host with other slots, so the next five fields differ per slot. |
| `http_port` | int | Odoo's `--http-port` and the port of `ODOO_URL`: 8075 + slot - 1 when scoped, else 8075. |
| `redis_db` | int | The database number put in `REDIS_DB` for the tests: `slot` when scoped, else 1. When scoped it is also the database `rebuild_db()` flushes. |
| `tmpdir` | str or None | `TMPDIR` for the run. Scoped: the base `TMPDIR`, with `/slot-K` added above slot 1. Else the base `TMPDIR` unchanged. |
| `chrome_kill_pattern` | str | The `pkill -f` pattern for the run's Chrome. Scoped: `<tmpdir>/[^/ ]*_chrome_odoo` with regular-expression characters escaped. Else `_chrome_odoo`. |
| `chrome_profile_globs` | tuple of str | Chrome profile directories removed at the end. Scoped: `<tmpdir>/*_chrome_odoo`. Else `/tmp/*_chrome_odoo` and `/var/tmp/*_chrome_odoo`. |

Invariants: when `scoped` is False every field except `slot`, `slots` and `private_network` equals the value `test.py` used before slots.
Two scoped runs with different slots never share `http_port`, `redis_db`, `tmpdir`, nor a Chrome the other's pattern matches (the profile
directory of slot 2 lies below slot 1's `tmpdir`, and the `[^/ ]*` keeps slot 1's pattern from reaching it).

### `[@ANCHOR: runner_slots:slot_resources]` `slot_resources(slot, slots, private_network, base_tmpdir)`

Purpose: builds the `SlotResources` of a run.

| Argument | Type | Meaning |
|---|---|---|
| `slot` | int | The slot held, 1 to `slots`. |
| `slots` | int | The machine's slot count. |
| `private_network` | bool | `in_private_network(os.environ)`, or True when the caller knows the full pipeline will run. |
| `base_tmpdir` | str or None | The `TMPDIR` in force. Only used when scoped, where it must be an absolute path of `A-Za-z0-9_./-` (the pattern and the clean-up glob embed it). |

Returns a `SlotResources`. Raises `SlotConfigError` for a slot outside `1..slots` and, when scoped, for a missing or oddly spelled `base_tmpdir`.
Pure; `test.py` creates the directory (`apply_slot_resources`).

### `[@ANCHOR: runner_slots:modules_mentioning_broker]` `modules_mentioning_broker(module_dirs)`

Purpose: which modules under test can use the host's RabbitMQ. `module_dirs` maps a module name to its directory. Returns the sorted names whose
`*.py` files (outside `.git`, `__pycache__`, `node_modules`, `target`, `static`) match `pika|amqp|rabbitmq`, any case. Deliberately broad (a comment
counts) and limited to the modules named, not their dependencies. A missing directory is not a user. Reads the files; no side effects.

### `[@ANCHOR: runner_slots:broker_listening]` `broker_listening(port=5672, host="127.0.0.1", timeout=0.5)`

Purpose: whether a RabbitMQ answers on the host, which decides whether `broker_users_sharing_the_host` looks at the modules at all. Returns True if a TCP connection is accepted
within `timeout` seconds, False on any `OSError` (refused, unreachable, timed out). Opens and closes one connection; sends nothing.

### `[@ANCHOR: runner_slots:slot_refusals]` `slot_refusals(resources, pause_on_fail, mcp, broker_users)`

Purpose: why a run cannot use its slot. Slot 1 is never refused (returns `[]`).

| Argument | Type | Meaning |
|---|---|---|
| `resources` | `SlotResources` | The run's slot and whether it is in the private namespace (`private_network`). |
| `pause_on_fail` | bool | `--pause-on-fail` or `HAMS_PAUSE_ON_FAIL=1`. |
| `mcp` | bool | `--mcp`. |
| `broker_users` | list of str | Modules under test that use a RabbitMQ listening on the host; `[]` when nothing listens or no module mentions it. |

Returns one sentence per reason, as a list. In the full pipeline (`private_network` true: own namespace, Redis, RabbitMQ, same ports in every
run) the only reason is `--mcp` (its clients are configured for port 8075 and a server in a private namespace cannot be reached from
outside). A run on the host (`HAMS_ISOLATED_NS=1` set by hand) shares the network and adds `--pause-on-fail` (the harness pins Chrome's
debugging port to 9222, `zero_sudo/tests/common.py`) and `broker_users` (their queues would be shared with the other slots, and a vhost
cannot be created as the `odoo` user). Every host-only sentence says the run is on the host because `HAMS_ISOLATED_NS=1` is set and to leave
it out. Pure.

### `[@ANCHOR: lock_queue:write_holder]` `write_holder(qdir, info, pid=None, now=time.time, slot=None, ticket=None)`

Purpose: records that a process holds the lock, so queue lines and `--queue-status` can name it.

| Argument | Type | Meaning |
|---|---|---|
| `qdir` | str | The queue directory (created, mode 1777, if missing). |
| `info` | str | What the holder runs (`test.py` and its arguments, at most 200 characters). |
| `pid` | int or None | The holder; default this process. Its start time is read from `/proc` and is part of the record's name. |
| `now` | callable | Clock for `since`; default `time.time`. |
| `slot` | int or None | The slot held. `test.py` passes it in every mode, also on a machine with one slot; a record without it counts as slot 1. |
| `ticket` | str or None | The queue ticket the run waited with (or inherited from its parent). `test.py` passes it in every mode; a record without it (an older `test.py`) still marks the run's ticket as running, see `may_try`. |

Returns the record name `holder-<pid>-<start>`, or None if the process has no readable start time or the directory is unwritable (it must
never stop a test run). Rewrites an existing record of the same process. The record is JSON written to a temporary file and linked into
place, so a reader never sees half of it. Removed by `release_ticket` at exit; a dead holder's record is ignored and deleted by the readers.

### `[@ANCHOR: lock_queue:read_holder]` `read_holder(qdir)`

Purpose: the newest live holder record, for the one-slot status line. Returns a dict (`pid`, `start`, `since`, `info`, and `slot`, `ticket`
when written) or None. A record whose pid is gone or whose start time differs (a reused pid) is deleted. Read-only otherwise.

### `[@ANCHOR: lock_queue:read_holders]` `read_holders(qdir)`

Purpose: the holder of each slot. Returns a list of dicts, one per slot, ordered by slot number: the newest live record of that slot (a record
without `slot` counts as slot 1), each with `slot` set. A re-exec'd run leaves its parent's record and writes its own for the same slot; the
older is dropped here. Dead records are deleted. A run that failed to write its record is invisible, as before.

### `[@ANCHOR: lock_queue:may_try]` `may_try(qdir, tickets, ticket, slots=1)`

Purpose: whether the waiter holding `ticket` is at the head of the queue and may try the lock now.

| Argument | Type | Meaning |
|---|---|---|
| `qdir` | str | The queue directory. |
| `tickets` | list of dict | `live_tickets()`: lowest first; contains `ticket`. |
| `ticket` | str | The waiter's ticket name. |
| `slots` | int | The machine's slot count. |

Returns a bool. `slots` of 1: True for the lowest live ticket, including a running holder's ticket, exactly as before slots (so nobody else
tries while the holder runs). `slots` above 1: a ticket is a running run, not a waiter, when a live holder record names it or when a live holder
record has the same pid and start time as the ticket (a run of an older `test.py`, or one that wrote its record without the ticket: record and
ticket belong to one process; counting that ticket as the oldest waiter once blocked the whole queue with three of four slots free). The
waiters, oldest first, may try while slots are free: the waiter at position p (0 is the oldest) may try when p is less than the number of free
slots (`slots` minus the busy slots, not counting the record that names `ticket` itself: a re-exec'd child takes back the slot its parent held),
so a head that is slow (the load gate) or loses a race does not stop the others from taking the other free slots. A fail-fast run holds a slot
with no ticket and counts as busy. Reads the directory each call; no locks.

### `[@ANCHOR: lock_queue:describe_queue]` `describe_queue(qdir, my_ticket=None, now=time.time, slots=1)`

Purpose: the lines of `test.py --queue-status`. Read-only apart from deleting dead records; it never takes or probes a lock.
`slots` of 1: `holder: pid P running INFO for AGE` (or `holder: none recorded`), then the queue. `slots` above 1: `slots: N, B busy`, one line
per slot (`slot K: free` or `slot K: pid P running INFO for AGE`; a held slot above N is listed with `(beyond the configured N)`), then only
the real waiters as `queue I: pid P INFO waiting AGE`, with `<- you` on `my_ticket`; `queue: empty` when there are none.

### `[@ANCHOR: lock_queue:acquire]` `acquire(qdir, try_lock, info="", ticket=None, timeout=None, poll=1.5, report_every=60.0, on_head=None, sleep=time.sleep, now=time.time, out=print, handle_signals=True, slots=1)`

Purpose: wait in the FIFO queue, then take the lock. `try_lock()` makes one non-blocking attempt (with several slots, at each slot's lock in
turn) and returns True on success; it is called only while `may_try` says the waiter is at the head. `ticket` adopts a ticket a parent took;
`on_head()` runs once, when the waiter first becomes the head, and returns False to give up; `timeout` is seconds or None; `slots` is passed
to `may_try` and to the report line.

Returns `(status, ticket)`: `ACQUIRED` (ticket kept, the caller releases it at exit), `TIMEOUT` or `GATE_FAILED` (ticket released). The
ticket is also released on an exception and on SIGTERM or SIGHUP (exit 128 + signal). Polls every `poll` seconds, prints a position line every
`report_every`. Blocking; the signal handlers are installed only on the main thread. `lint_lock.py` calls it with the default `slots=1`.

### `[@ANCHOR: test:acquire_lock_via_queue]` `_acquire_lock_via_queue(lock_file_path, timeout, slots=1, only_slot=None)`

Purpose: the `--wait` acquisition. Returns the slot number taken. Queues behind `lock_queue.acquire` with `try_lock` set to
`_try_slot_locks(lock_file_path, slots, only_slot)`; a child started by `sudo`, `systemd-run` and `unshare` adopts its parent's ticket from
`HAMS_TEST_QUEUE_TICKET` and takes back only `only_slot`. The load gate runs once the run reaches the head (not for an adopted ticket).
Exits 1 when the gate refuses and 75 on timeout. On success it exports the ticket, registers its release at exit, and (with `slots` above 1)
writes the holder record with the slot and the ticket.

### `[@ANCHOR: test:try_slot_locks]` `_try_slot_locks(lock_base_path, slots, only_slot=None)`

Purpose: one non-blocking pass over the slot locks. Tries slot 1 up to `slots` in order (only `only_slot` when given) and returns the slot
number whose flock it took, or None when none is free. The open, locked file lives in the module global `_single_instance_lock` for the life
of the process (the lock goes with the process, however it ends); without `only_slot`, a slot named by a live run's holder record (another pid, written less than 120 s ago: `SLOT_RESERVATION_SECONDS`) is skipped even when its flock is free, because the invoking process of the full pipeline lets go of its flock when it execs into `sudo` / `systemd-run` / `unshare` and the root phase takes the slot back a moment later (a run that took the slot in that window shared its database); a file whose lock was not taken is closed. The lock file is opened with
`O_NOFOLLOW`, falling back to read-only when `fs.protected_regular` refuses a write open (`_open_lock_file`). With `slots` of 1 this is the
single flock `test.py` always took.

### `[@ANCHOR: test:take_back_slot_lock]` `take_back_slot_lock(lock_base_path, slots, only_slot, sleep=time.sleep, clock=time.monotonic, environ=None)`

Purpose: the root phase of the full pipeline (`--internal-ns-init`, the process inside the new PID namespace) takes back the slot its invoking
process held. It replaces the queue acquisition that phase used to do, which was wrong inside the namespace: `/proc` shows only the namespace,
so the invoking process's ticket looked dead and was deleted, and the ticket and holder record the phase wrote carried namespace pids that
the host took for dead. The effect was that a full-pipeline run was invisible to `--queue-status` and to the queue (0 slots busy while four
ran). Now the phase writes nothing in the queue directory; the invoking process's holder record and ticket (its pid lives as long as `sudo`
does) describe the run for its whole life.

| Argument | Type | Meaning |
|---|---|---|
| `lock_base_path` | str | Base lock path. |
| `slots` | int | The machine's slot count. |
| `only_slot` | int or `None` | `--slot-held` of the command line; `None` takes any free slot. |
| `sleep`, `clock` | callables | Test seams: `time.sleep` and `time.monotonic`. |
| `environ` | mapping or `None` | Where `HAMS_TEST_TAKE_BACK_SECONDS` is read (default 30; a bad value means 30). |

Returns the slot whose flock was taken (kept in `_single_instance_lock` for the process's life), or `None` when the slot stayed taken until the
limit: another run took it in the moment the invoking process had let go (an older `test.py`, which has no holder-record reservation, can). The
caller then prints `_no_slot_message` and exits 1. Blocking; polls every 0.2 s; never takes a slot other than `only_slot` when that is given.

### `[@ANCHOR: test:chrome_processes_of_this_run]` `chrome_processes_of_this_run(pattern, uid=None, own_pids=None, processes=None)`

Purpose: which headless Chrome processes `kill_test_chrome()` may signal (hang recovery in `run_cmd`, `robust_reap`, the end-of-run clean-up). It replaced
`pkill -u odoo -f <pattern>`, which signalled every `odoo` Chrome on the machine whose command line matched, including the browsers of other
runs: a run in its own PID namespace is visible in the host's process table, and a slot 1 run's pattern (`<TMPDIR>/[^/ ]*_chrome_odoo`)
matched their profile directories. The symptom was a tour in a full-pipeline run whose Chrome died with exit code 9 (SIGKILL) a fraction of a second
after it started, with `CancelledError` in `Page.navigate` or a tour that stalled at a step.

| Argument | Type | Meaning |
|---|---|---|
| `pattern` | str | Regular expression searched in the space-joined command line (`_CHROME_KILL_PATTERN`). |
| `uid` | int or `None` | Effective uid of the processes; default the `odoo` account (no such account: empty result). |
| `own_pids` | set of int or `None` | Pids of this process's descendants; default `psutil.Process().children(recursive=True)`. |
| `processes` | callable or `None` | Test seam; default `psutil.process_iter`. |

Returns a list of `psutil.Process` with `info` holding pid, uids and cmdline. A process belongs to this run when it is a descendant of this process, or an
orphan whose `--user-data-dir` is a directory that exists in this process's own view of the filesystem (a leftover of a dead parent of this run;
another run's profile directories are in its private overlay, or on the host where a namespaced run cannot see them). Reads `/proc`; sends no signal.

### `[@ANCHOR: test:reap_orphans_forever]` `reap_orphans_forever(protected_pids, stop, interval=0.2, min_age=0.3, clock=time.monotonic)`

Purpose: the loop of the reaper thread of the root phase, which is PID 1 of its PID namespace (`unshare -p -f`). Every process orphaned anywhere in the run
(Chrome helpers, shell wrappers, the processes of `rabbitmq-server`) is re-parented to it, and a Python init that only waits for its one test process never
reaped them: 150 to 170 zombies per run were seen after a few hours, and a multi-hour run could exhaust the PID limit.

| Argument | Type | Meaning |
|---|---|---|
| `protected_pids` | set of int | Children this process waits for itself (Redis, RabbitMQ); never reaped here, their exit status is wanted. |
| `stop` | `threading.Event` | The loop ends when it is set. |
| `interval` | float | Seconds between scans. |
| `min_age` | float | Seconds a child must have been seen as a zombie before it is reaped, so a child whose parent code is about to wait for it is left to that code. |
| `clock` | callable | Test seam, `time.monotonic`. |

Returns the number of zombies reaped. Reaps with `os.waitpid(pid, WNOHANG)` on the zombie's own pid only: `waitpid(-1)` would steal the exit status of the test
process that `run_protected` waits for, and the run's exit code would read 0. Started by `setup_namespace_and_run_tests` as a daemon thread before the test
process starts and stopped in its `finally`.

Why the defaults are 0.2 s and 0.3 s (they were 1.0 s and 3.0 s): Chrome of every browser test runs under `setsid -w unshare ...`; at teardown Odoo waits up to
5 s for the browser tree to vanish, and the exited `unshare` is a zombie child of this init until it is reaped, so every browser test of the full pipeline paid
the `min_age`. The same 15 `ham_relay_bridge` tests took 5.1 s each with the old values and 2.3 s each with the new ones. Safety does not depend on the age: only
a child that is already a zombie is waited for, with its own pid and `WNOHANG` (a live process is never touched), only direct children of this namespace's init
are looked at (another run's processes are in another PID namespace), and the test process is in `protected_pids` through `run_protected`.

### `[@ANCHOR: test:run_protected]` `run_protected(cmd, protected_pids, **popen_kwargs)`

Purpose: run the test process of the root phase and return its return code, like `subprocess.run(cmd, ...).returncode`, but with the child's pid added to `protected_pids`
(the set the reaper thread was given) right after it starts, so the reaper can never steal its exit status however short `min_age` is. Extra keyword arguments go to
`subprocess.Popen` (the root phase passes `preexec_fn`). Any exception while waiting (a signal turned into `sys.exit`, Ctrl-C) kills the child and waits for it before the
exception propagates, as `subprocess.run` does.

### `[@ANCHOR: test:run_preflight_in_slot]` `run_preflight_in_slot(args, lock_file_path, held_slot, slots, preflight=None)`

Purpose: the pre-flight (semantic anchor scan, burn-list and audit linters, init-import and JavaScript syntax checks, and for a `HAMS_ISOLATED_NS=1` run the test-host check) runs
INSIDE the slot the run holds, not before it. It used to run first: up to eight runs scanned about 18,000 anchors at the same time outside any slot accounting, the
slot count read free, and `--queue-status` showed busy runs as waiting. Now the order is: cheap checks (command line, repository root), load gate and queue wait, slot taken, slot
refusals, **pre-flight**, start-up sweep, Odoo. What the pre-flight checks is unchanged.

| Argument | Type | Meaning |
|---|---|---|
| `args` | namespace | The parsed command line (`parse_runner_args`). |
| `lock_file_path` | str | Base lock path; the queue directory is derived from it. |
| `held_slot`, `slots` | int | The slot held and the machine's slot count (for the message). |
| `preflight` | callable or `None` | Test seam; default `run_preflight`. |

Called once per run by the invoking process (the flat process of a `HAMS_ISOLATED_NS=1` run), right after it took its slot. The re-exec'd phases (`--internal-ns-init`, and the final
test process) carry `--preflight-done` and never run it, so it runs exactly once however many `sudo` / `systemd-run` / `unshare` layers follow. While it runs the holder record says
`phase: preflight` (`--queue-status`: `slot K: pid P pre-flight of test.py ... for 40 s`); when it returns the record becomes `running` (`lock_queue.set_holder_phase`). If it ends
the run (`sys.exit(1)` with the findings printed), the slot is released at once: the slot lock is closed, the holder record and the queue ticket are removed, `Pre-flight failed: slot K of N
released.` is printed and the exit propagates unchanged, so the findings and status are exactly what they were. Nothing else is dropped: no database exists yet. A run that cannot get a slot
therefore does not pre-flight until it has one (a failing pre-flight is now reported after the wait for a slot, not before it). A batch leader holds no slot while a batch forms; its nested
`test.py` pre-flights in its own slot like any run. Not cached: the pre-flight took about 6 s for one module (`-u parking`), the inputs (both repositories' trees, the sibling checkout, tool
versions, installed Odoo stubs, the module list) are not all in one hashable place, and a stale pass would skip real checks; the saving does not justify the risk.

### `[@ANCHOR: lock_queue:set_holder_phase]` `set_holder_phase(qdir, phase, pid=None, now=time.time)`

Purpose: moves this process's holder record from `preflight` to `running`, keeping its `since`, and stamps `phase_since`. Returns True when it rewrote the record, False (nothing written)
when the process has no live record or the directory is unwritable (a label must never stop a run). The rewrite is atomic (temporary file, rename over the record). `write_holder` takes the same
`phase`; a record without one is a running run. `describe_queue` shows `running` or `pre-flight of` per slot, and no longer lists a running holder's ticket as a waiter with one slot.

### `[@ANCHOR: test:slot_handoff_args]` `slot_handoff_args(slots, held_slot)`

Purpose: the command-line words that give a re-exec'd phase the slot of its parent. Returns `[]` when `slots` is 1 (the command line stays
exactly what it was) and `["--slots", "N", "--slot-held", "K"]` otherwise. `main()` appends them to the `unshare` command; the root phase
passes its own arguments, so the final test process receives them too. They are in the command line because sudo, systemd-run and unshare
each decide for themselves which environment variables survive. `--slot-held` is hidden from `--help`; given without `--slots` or outside
`1..slots` it is a usage error (exit 2). Pure.

### `[@ANCHOR: test:broker_users_sharing_the_host]` `broker_users_sharing_the_host(resources, target_modules, addons_path)`

Purpose: the `broker_users` argument of `slot_refusals`. Returns `[]` for slot 1, for a run in the private namespace, and when no RabbitMQ accepts
a connection on 127.0.0.1:5672; otherwise the modules among `target_modules` (found as `<entry>/<module>` for the comma-separated entries of
`addons_path`) that `modules_mentioning_broker` names. One TCP connect and a read of the modules' Python, only in that case.

### `[@ANCHOR: test:enforce_slot_refusals]` `enforce_slot_refusals(resources, args, broker_users=())`

Purpose: stops a run that cannot use its slot, with every reason printed. Calls `runner_slots.slot_refusals` with `--pause-on-fail` or
`HAMS_PAUSE_ON_FAIL=1`, `--mcp` and `broker_users`. Called by the invoking process (no broker check, the full pipeline is coming), by the process
that runs Odoo (no broker check yet), and again once the modules under test are known (with `broker_users_sharing_the_host`). Prints
`test slot K of N cannot run this:`, one line per reason and `NEXT: ...`, then exits 1; returns nothing otherwise. Slot 1 never exits here.

### `[@ANCHOR: test:exit_status_for_run]` `exit_status_for_run(returncode, extractor, results_before=0)`

Purpose: the exit status of one Odoo test process, so that a run in which no test ran is never a pass. Returns `returncode` when it is not 0 (Odoo
died before the tests: "Address already in use" on port 8075, a database login failure; or a signal, already mapped by `signal_exit_verdict`);
else 0 if the extractor holds more `odoo.tests.result:` lines than `results_before`; else prints `Odoo exited 0 but printed no odoo.tests.result
line` and returns 1. `results_before` is the number of verdict lines held before this process (individual mode runs one process per module).
`run_cmd` records its status in `FailureExtractor.child_exit_status`; `finish_and_write()` then prints `TEST RUN FAILED: no tests ran` when no
verdict exists (it used to print "No test failures or system crashes detected") and `Odoo exited with status N` for any non-zero status.

### `[@ANCHOR: test:apply_slot_resources]` `apply_slot_resources(resources)`

Purpose: makes the process-wide choices a scoped slot needs; does nothing for a run that is not scoped. Creates `resources.tmpdir` (changing its
mode to 0777 only when the mode is not already 0777: a directory another account created, such as the `odoo` account's earlier
`/var/lib/odoo/tmp/slot-K`, cannot be `chmod`ed by anyone else, and needs no change; when the mode differs and `chmod` fails, the run prints the
directory, its mode, the owner's uid and `sudo chmod 0777 <dir>` and exits 1) and sets `TMPDIR` to it, so Odoo's Chrome profiles (`tempfile` under `TMPDIR`, suffix `_chrome_odoo`) are the only
ones there, and narrows the module globals `_CHROME_KILL_PATTERN` and `_CHROME_PROFILE_GLOBS`, which `kill_test_chrome()` (hang recovery,
`robust_reap`) and the end-of-run clean-up read. Called once, before Odoo or any child is started. Raises `OSError` if the directory cannot
be created; the run stops there.

## Clean-up in slot mode (ADR 0116)

In slot mode (the machine has more than one slot) a run owns, in its slot K, a PostgreSQL database (`<db>_sK`, slot 1: `<db>`), Odoo's filestore
directory of that name, and, on the host, `<TMPDIR>/slot-K`. They used to be removed only when the next run in the same slot started, so each
one outlived its run and a crashed or killed run left them for good. Now:

* **At the end of every run** (pass, fail, `sys.exit`, an uncaught exception, SIGTERM, SIGHUP) `run_cleanup_at_exit` drops the database WITH
  (FORCE) and removes the filestore and the slot directory. In the full pipeline the root phase repeats the drop after the test process ends
  (`root_phase_database_cleanup`), because the database lives on the host's PostgreSQL even though the filestore, Redis and RabbitMQ lived in
  the private overlay and went with it; it covers a test process killed by the out-of-memory watchdog or SIGKILL.
* **`--keep-db`** keeps all three and prints `Database NAME (slot K) KEPT; drop it when done: sudo -u postgres dropdb --force NAME` as the last
  line, for triage of a failure. The next run in the same slot replaces it; the sweep never removes it. A run is not batched with `--keep-db`.
* **At the start of every run in slot mode**, under the slot lock it just took, `sweep_slot_leftovers` removes the leftovers of runs that are
  gone, in every slot from 1 to 15: databases carrying this runner's marker, databases an older `test.py` left under the slot naming
  (`hams_test..._sK`, K from 2), orphan filestores of that naming, and `slot-K` directories. A slot is swept only while the sweeper holds that
  slot's flock and no live holder record names it, so a database a live run holds is never touched. It refuses (exit 2) on a production host
  (`hams_prod` exists, or the deploy marker files), and never drops `hams_prod`, `postgres`, `template0`, `template1`, a kept database or a name that is
  not a plain identifier.
* **The marker** is a PostgreSQL comment on the database, `hams_test_runner:v1 slot=K kept=0|1` (`database_marker`). It is written in the same
  `psql` session that creates the database and goes away with it; no second record exists to lose, and no file permission to trip over
  between the accounts that run `test.py`. A database without a marker is dropped by a run only in the older naming case above, never
  by guessing: slot 1's bare name (`hams_test`) is also the database of a run without slots, so it is removed only when a slot-mode run made it.
* **With one slot** nothing changes: no marker, no clean-up, no sweep, `hams_test` is kept and rebuilt by the next run.
* **What a full-pipeline run leaves outside its namespace**: its log files in the log directory (`filtered_test.slot-K.txt` is the
  verdict; they are never removed), the `postgres` roles `odoo` and the invoking user (created if missing, shared by every run), and the
  lock and queue files. Its mounts, processes, Redis, RabbitMQ, filestore and `/var/lib/odoo/tmp` contents go with the namespace.

### `[@ANCHOR: runner_cleanup:database_marker]` `database_marker(slot, kept)`

Purpose: the comment `rebuild_db()` puts on a database it creates in slot mode: `hams_test_runner:v1 slot=2 kept=0`. `slot` is an int (1 to 15),
`kept` a bool (`--keep-db`). Returns the string. Pure. See also `parse_database_marker`, `comment_database_sql`.

### `[@ANCHOR: runner_cleanup:parse_database_marker]` `parse_database_marker(comment)`

Purpose: reads a marker back. `comment` is a database comment (str, `None` or ""). Returns `(slot, kept)` for a comment that is exactly a marker, else
`None` (any other text, including the marker followed by more text). Pure.

### `[@ANCHOR: runner_cleanup:is_droppable_name]` `is_droppable_name(name)`

Purpose: the one gate in front of every `DROP DATABASE` and `COMMENT ON DATABASE` this runner builds. True for a plain identifier
(`[A-Za-z_][A-Za-z0-9_]*`, so it is safe inside double quotes) that is not `hams_prod`, `postgres`, `template0` or `template1`. Pure.

### `[@ANCHOR: runner_cleanup:drop_database_sql]` `drop_database_sql(name)`

Purpose: returns `DROP DATABASE IF EXISTS "name" WITH (FORCE);` (FORCE ends the sessions a killed Odoo left open). Raises `ValueError` for a name
`is_droppable_name` refuses. Pure.

### `[@ANCHOR: runner_cleanup:comment_database_sql]` `comment_database_sql(name, slot, kept)`

Purpose: returns `COMMENT ON DATABASE "name" IS '<marker>';`, which `rebuild_db()` runs right after `CREATE DATABASE` in the same `psql` session.
Raises `ValueError` for a name `is_droppable_name` refuses. Pure.

### `[@ANCHOR: runner_cleanup:owned_slot]` `owned_slot(name, comment)`

Purpose: which slot's run owns database `name`, the question the sweep asks of every database on the server. Returns the marker's slot (1 to 15) when
`comment` is a marker; else K when `name` is `hams_test`, any `[a-z0-9_]*`, then `_sK` with K from 2 to 15 (what an older `test.py` produced);
else `None` (not ours: slot 1's bare name, every other name, every protected or unsafe name, a marker whose slot is out of range). Pure.

### `[@ANCHOR: runner_cleanup:SlotLeftovers]` `SlotLeftovers`

A dataclass built by `leftovers`, read by `sweep`; it lives for one sweep.

| Field | Type | Meaning |
|---|---|---|
| `slot` | int | The slot, 1 to 15. |
| `databases` | list of str | Databases to drop (never kept ones). |
| `filestores` | list of str | Names of Odoo filestore directories to remove: those of the databases and orphans of the older naming with no database. |
| `tmpdir` | bool | True when `<TMPDIR>/slot-K` exists and is to be removed; never set for slot 1, whose TMPDIR is the shared one. |

### `[@ANCHOR: runner_cleanup:leftovers]` `leftovers(rows, filestore_names, slot_directory_names)`

Purpose: decides what to sweep, from what is on the server and the disk.

| Argument | Type | Meaning |
|---|---|---|
| `rows` | list of `(name, comment)` | Every database on the server. |
| `filestore_names` | list of str | The entries of Odoo's filestore directory. |
| `slot_directory_names` | list of str | The entries of the base TMPDIR; only `slot-2` to `slot-15` count. |

Returns a list of `SlotLeftovers` in slot order, empty slots omitted. A kept database (marker `kept=1`) and its filestore are skipped, as is every name
`owned_slot` does not own. Pure: it does not look at which slots are still held; `sweep` does.

### `[@ANCHOR: runner_cleanup:SweepOperations]` `SweepOperations`

A dataclass of the side effects `sweep` performs, so that the rules can be tested without touching the machine.

| Field | Type | Meaning |
|---|---|---|
| `claim_slot` | callable(slot) | Tries to take the slot's lock without waiting; returns a release function, or `None` when a live run holds the slot. The slot the sweeper's own run holds returns a function that does nothing. |
| `drop_database` | callable(name) | Returns True when the database is gone. |
| `remove_filestore` | callable(name) | Returns True when the directory is gone. |
| `remove_slot_tmpdir` | callable(slot) | Returns True when `<TMPDIR>/slot-K` is gone. |
| `say` | callable(text) | Reports a line. |

The three removal callables must not raise.

### `[@ANCHOR: runner_cleanup:sweep]` `sweep(plan, operations)`

Purpose: removes the leftovers in `plan` (a list of `SlotLeftovers`), slot by slot, each only while `operations.claim_slot` holds that slot's lock; the
lock is released after the slot, also when a removal fails. A slot whose lock cannot be taken is skipped and reported.
Returns `{"removed": [...], "skipped": [...], "failed": [...]}` of item names (`database NAME`, `filestore NAME`, `tmpdir slot-K`, `slot K`). Not
re-entrant across processes: two sweepers claiming one slot exclude each other through its flock.

### `[@ANCHOR: runner_cleanup:finish_run]` `finish_run(database, slot, keep, read_comment, drop_database, remove_filestore, remove_slot_tmpdir, say)`

Purpose: the end-of-run decision for one run's database; called by the test process at exit and again by the root phase of the full pipeline.

| Argument | Type | Meaning |
|---|---|---|
| `database` | str | The run's slot-derived database. |
| `slot` | int | The slot the caller holds (holding its lock is what makes the removal safe). |
| `keep` | bool | `--keep-db`. |
| `read_comment` | callable(name) | The database's comment, or `None` when it does not exist. |
| `drop_database`, `remove_filestore` | callable(name) | Return True when gone. |
| `remove_slot_tmpdir` | callable() or `None` | Takes no argument; `None` when there is no directory to remove (slot 1, a private namespace). |
| `say` | callable(text) | Reports a line. |

Returns `"kept"` (with `keep`, or a marker with `kept=1`; prints `Database NAME (slot K) KEPT; drop it when done: ...` when the database exists, nothing is
removed), `"dropped"` (marker names this slot and is not kept: dropped, then the filestore and directory), `"absent"` (no database: only the
filestore and directory are removed), `"foreign"` (a database of that name exists with no marker for this slot, so it was not made by this run:
left alone with a warning) or `"failed"` (the drop failed: files kept). Idempotent: the second call finds `"absent"`.

### `[@ANCHOR: test:PostgresAccess]` `PostgresAccess(psql_cmd, connection_args=(), env=None, preexec_fn=None)`

A small class: the PostgreSQL operations clean-up needs, through `psql -X -q -At` (unaligned, tab separated, `ON_ERROR_STOP`).

| Field | Type | Meaning |
|---|---|---|
| `psql_cmd` | str | Path of `psql` (`infrastructure.get_pg_bin("psql")`). |
| `connection_args` | list of str | Extra arguments, e.g. `["-h", "/var/run/postgresql"]`. |
| `env` | dict or `None` | Environment for `psql`; `None` is this process's. |
| `preexec_fn` | callable or `None` | Runs in the child first; the root phase uses it to become the `postgres` account. |

Methods: `run(sql, **variables)` (the `CompletedProcess`; variables become `-v name=value`), `database_rows()` (`[(name, comment)]` or `None` when
the server cannot be asked), `database_comment(name)` (the comment, `""` for none, `None` when the database does not exist or the server cannot be asked),
`drop_database(name)` (True when gone; raises `ValueError` for a name `is_droppable_name` refuses). Nothing exits or raises on a `psql` failure.
`default_postgres_access()` builds one for the invoking process with the runner's defaults (host `localhost`, role `odoo`, password `odoo`).

### `[@ANCHOR: test:remove_directory_tree]` `remove_directory_tree(path)`

Purpose: removes a directory tree on behalf of the sweep and the end-of-run clean-up. Returns True when the path is gone (a missing path counts). Plain
`shutil.rmtree` first; on a `PermissionError` the non-interactive `sudo -n rm -rf --` that `remove_stale_filestore` uses. A symbolic link is refused (False,
warning), so a planted link is never followed. Never raises.

### `[@ANCHOR: test:sweep_slot_leftovers]` `sweep_slot_leftovers(pg, held_slot, lock_base_path, base_tmpdir, filestore_base=ODOO_FILESTORE_BASE, say=print)`

Purpose: the start-up sweep. Called by `main()` once per run in slot mode, in the invoking process (the flat process of a `HAMS_ISOLATED_NS=1` run),
right after the slot lock was taken and before anything is built.

| Argument | Type | Meaning |
|---|---|---|
| `pg` | `PostgresAccess` | The server to sweep. |
| `held_slot` | int | The slot the caller holds; swept too, without a second lock. |
| `lock_base_path` | str | Base lock path; slot K's lock is `runner_slots.slot_lock_path`, the queue directory `lock_queue.queue_dir_for_lock`. |
| `base_tmpdir` | str or `None` | The TMPDIR whose `slot-K` directories are swept. When it is in RAM (`/dev/shm/hams-test-tmp`), the old disk directory `/var/lib/odoo/tmp` is swept as well, so slot directories left there before TMPDIR moved are removed. |
| `filestore_base` | str | Odoo's filestore directory. |
| `say` | callable | Reports a line. |

Returns the `runner_cleanup.sweep` outcome (`{"removed", "skipped", "failed"}`), or `None` after printing `Sweep skipped` when the databases cannot be listed. Exits 2
with the production-host message when the server has `hams_prod`. A slot is claimed with a non-blocking flock on a lock file opened like the runner's
own (`_open_lock_file`) and only if it has something to sweep, so the sweep creates no lock files for idle slots; a slot named by a live holder record is skipped
even if its flock looks free. Takes and releases one slot lock at a time; the slow part is `DROP DATABASE`.

### `[@ANCHOR: test:install_run_cleanup]` `install_run_cleanup(database, slot, keep, tmpdir)`

Purpose: arms the end-of-run clean-up in the process that runs Odoo, in slot mode only.

| Argument | Type | Meaning |
|---|---|---|
| `database` | str | The slot-derived database. |
| `slot` | int | The slot. |
| `keep` | bool | `--keep-db`. |
| `tmpdir` | str or `None` | The slot's own TMPDIR to remove at the end; `None` for slot 1 and for a private namespace. |

Sets the module state `_RUN_CLEANUP` (which `rebuild_db()` reads to mark the database), registers `run_cleanup_at_exit` with `atexit` and installs SIGTERM and
SIGHUP handlers that print a line and `sys.exit(128 + signal)`. Main thread only. SIGKILL and a power cut cannot be handled: the next run's sweep removes
those leftovers.

### `[@ANCHOR: test:run_cleanup_at_exit]` `run_cleanup_at_exit(pg=None)`

Purpose: the exit hook. Runs once (a second call, or a call before `install_run_cleanup`, returns `None`). Kills the run's remaining Chrome
(`kill_test_chrome("KILL")`; limited to the run's own TMPDIR on the host), then `runner_cleanup.finish_run` with `pg` (default `default_postgres_access()`). Returns
`finish_run`'s word, or `"failed"` after a warning when `psql`, `pkill` or the disk raised: the run's verdict is already decided and is never changed by clean-up.

### `[@ANCHOR: test:root_phase_database_cleanup]` `root_phase_database_cleanup(run_args, slot, pg)`

Purpose: the backstop in the root phase of the full pipeline, which outlives the test process and still holds the slot's lock. Does nothing (returns `None`) with one slot, with
`--keep-db`, or when the slot-derived name is too long or unsafe. Otherwise `finish_run` on `slot_database_name(run_args.db, slot)` through `pg`, a
`PostgresAccess` running `psql` as the `postgres` account over the host socket; its filestore is in the discarded overlay, so only the database is dropped. `run_args` is the
parsed command line (`slots`, `db`, `keep_db`).

## Known-red baseline and flakes (`known_red.py`, ADR 0118)

`test.py` used to turn red for any failing test, whoever caused it, so every whole-system failure had to be debugged by hand before anything else could
be judged. After Odoo's run, a failing standard-mode run now goes through this policy; a passing run is untouched.

1. A failing test listed in a **baseline file** is "known red, not new". It does not turn the run red and is named in the summary with its age ("known red for 3.0 days"), owner, first-seen
   commit and reason. **It is tolerated for a limited time (ADR 0118 amendment, 2026-11-08):** an entry older than `HAMS_KNOWN_RED_MAX_AGE_DAYS` (default 3), counted from its last
   renewal, is no longer tolerated: the run fails on it ("EXPIRED known red", never rerun) until it is fixed (the entry removed) or renewed with a reason
   (`python3 hams_shared/tools/known_red.py renew TEST_ID --reason TEXT [--owner NAME]`; `known_red.py show` lists the entries oldest first). A baselined test that ran and passed is named too ("remove its baseline entry").
2. Any other failing test is **rerun once, alone**: a fresh database (`rebuild_db`, the slot's own database), the same modules installed, only the failed tests
   selected with `--test-tags /module:Class.method,...`. Passing on the rerun is a **flake**: it is counted in the flake file, named in the summary, and does
   not turn the run red. Failing again is a real failure and the run is red.
3. A **setup or class error** (`setUpClass`, `tearDownClass`, a module-level error) is never rerun. It is reported as "NEW SETUP/CLASS ERROR (never rerun)" and
   is red unless baselined. More than 25 new failures (a systemic problem), or a failure the log parser could not name (Odoo counted more failures than
   were found in the log), is also red and is not rerun.
4. The summary prints every tolerated test. Nothing is hidden. The exit status is 0 only when Odoo printed a verdict, exited with status 1 because of counted test
   failures, and every failure is known red or a flake. A signal, the CPU limit or a run with no verdict is never made green.
5. `--raw-failures` switches the policy off: no baseline, no rerun, every failure red. It still writes `test_results.json`. Tools that need Odoo's own list
   of failing tests (the module bisection) use it.
6. The run writes `test_results.json` (slot files: `test_results.slot-K.json`) beside `filtered_test.txt` when it fails. Version 1; see `PolicyOutcome.results`.

The policy applies to `--mode standard`. `--mode individual` keeps Odoo's verdict untouched.

**The baseline file** `known_red_baseline.json`: `{"version": 1, "entries": [{"test_id", "first_seen", "first_seen_time", "owner", "reason", "renewed"?}, ...]}`. `test_id` is
`<module>:<Class>.<method>` (a class-level error uses the method `setUpClass`); `first_seen` the commit that first showed the failure; `owner` who fixes it;
`first_seen_time` when it was first seen (UTC `YYYY-MM-DDTHH:MM:SSZ`; the age starts here); `reason` why it is red; `renewed` (optional) `{"time", "reason"}`, the last renewal, from which the age is counted instead. The first five are required and non-empty. A malformed file, a malformed id or a duplicate stops the run (exit status unchanged, the cause
printed), because tolerating or dropping tests on a guess would hide failures. Files read: `HAMS_KNOWN_RED_BASELINE` (an `os.pathsep` list) if set, else
`known_red_baseline.json` in the repository root being tested, in a sibling or nested `hams_open`, and `tools/known_red_baseline.json` here.
A private repository keeps its entries in its own root file; this directory's file lists only tests of this public repository.

**The flake file** is `test_flakes.json` in the log directory (`HAMS_TEST_FLAKE_FILE` overrides): `{"version": 1, "tests": {id: {"count", "first", "last",
"last_commit"}}}`. Tests whose count is above `HAMS_FLAKE_FIX_ME_THRESHOLD` (default 3) and whose module is under test are listed as "FIX ME" in every summary.

### `[@ANCHOR: known_red:FailureRecord]` `FailureRecord`

A frozen dataclass: one failing test found in Odoo's log. It lives for one run in one process and is never serialized.

| Field | Type | Meaning |
|---|---|---|
| `test_id` | str | `<module>:<Class>.<method>`; a class-level error uses `setUpClass`, `tearDownClass`, `setUpModule` or `tearDownModule` as the method. |
| `flavour` | str | Odoo's word: `FAIL` (an assertion) or `ERROR` (an exception). |
| `setup` | bool | True for a class- or module-level error, which is never rerun. |

Invariants: `test_id` always matches `module:Class.method`. A failing subtest is recorded under its parent test; one test with several failing subtests
gives several records with the same id (Odoo counts each).

### `[@ANCHOR: known_red:parse_failure_line]` `parse_failure_line(line)`

Purpose: finds a failing test in one log line. Called for every line by `RunObservation.feed`.

| Argument | Type | Meaning |
|---|---|---|
| `line` | str | One Odoo log line without ANSI colour. |

Returns a `FailureRecord`, or None. It reads Odoo's logger form `ERROR <db> odoo.addons.<module>...: FAIL|ERROR: Class.method` (also `Subtest Class.method (...)`) and the
class form `ERROR <db> unittest.suite: ERROR: setUpClass (odoo.addons.<module>.tests.<file>.Class)`. Anything else, including an error
line from a logger that is not an Odoo add-on, returns None. Pure; no errors.

### `[@ANCHOR: known_red:parse_started_line]` `parse_started_line(line)`

Purpose: names a test Odoo started, so a baselined test that ran and passed can be told from one that did not run. Returns `<module>:<Class>.<method>` from
`INFO <db> odoo.addons.<module>...: Starting Class.method ...`, else None. Pure.

### `[@ANCHOR: known_red:RunObservation]` `RunObservation`

What one Odoo process said about its tests, built line by line. It also carries the attributes `run_cmd` sets on its consumer, so a rerun uses it in place of
the `FailureExtractor` (which writes reports and removes the report file when created). `FailureExtractor.observation` is one; it is fed from `process_line`.

| Field or method | Type | Meaning |
|---|---|---|
| `failures` | list of `FailureRecord` | Every failing test line, in order, duplicates kept (so the count matches Odoo's). |
| `started` | set of str | Ids of every test Odoo started. |
| `odoo_result_lines` | list of dict | One per `odoo.tests.result:` line: `failed`, `errors`, `total`, `raw`. |
| `cpu_limit_line`, `signal_line`, `child_exit_status` | str, str, int or None | Set by `run_cmd`; the policy refuses to make a run green when the first two are set. |
| `feed(line)`, `process_line(line)` | method | Read one line (`process_line` first strips ANSI colour). |
| `counted()` | method, int | Failures plus errors Odoo counted, summed over every result line. |
| `failed_ids()` | method, set | The ids in `failures`. |

Not thread safe; `run_cmd` feeds it from one reader thread.

### `[@ANCHOR: known_red:BaselineEntry]` `BaselineEntry`

A frozen dataclass of one baseline line: `test_id`, `first_seen`, `first_seen_time`, `owner`, `reason` (strings from the file), `source` (path of the file it came from) and `renewed` (None, or `(time, reason)`). `age_days(now=time.time)` returns the days since the last renewal, else since `first_seen_time`. Created by
`load_baseline`, read by `classify` and the summary; never mutated.

### `[@ANCHOR: known_red:baseline_paths]` `baseline_paths(base_dir, environ=None)`

Purpose: which baseline files this run reads.

| Argument | Type | Meaning |
|---|---|---|
| `base_dir` | str | The repository root being tested (the working directory of `test.py`). |
| `environ` | mapping or None | Where `HAMS_KNOWN_RED_BASELINE` is read; default `os.environ`. |

Returns the existing files, de-duplicated by real path, in the order: environment list, or `<base_dir>/known_red_baseline.json`,
`<base_dir>/hams_open/...`, `<base_dir>/../hams_open/...`, `tools/known_red_baseline.json` beside the module. A missing file is skipped (no baseline is
an empty baseline). Reads only file metadata.

### `[@ANCHOR: known_red:load_baseline]` `load_baseline(paths)`

Purpose: reads and validates baseline files. Returns `{test_id: BaselineEntry}`. Raises `BaselineError` (a `ValueError`) for an unreadable or non-JSON file, a version other than
1, an entry without all five non-empty fields (or with a time that is not `YYYY-MM-DDTHH:MM:SSZ`, or a renewal without a reason), an id that is not `module:Class.method` (a leading `/` is accepted and removed), or an id listed twice, naming the
file. The caller prints the cause and keeps Odoo's exit status. Reads the files; no side effects.

### `[@ANCHOR: known_red:flake_threshold]` `flake_threshold(environ=None)`

Purpose: the count above which a flaky test is listed as "FIX ME". Returns `HAMS_FLAKE_FIX_ME_THRESHOLD` or 3. Raises `BaselineError` for a value that is not a whole number
of at least 1.

### `[@ANCHOR: known_red:load_flakes]` `load_flakes(path)`

Purpose: the flake counters. Returns `{test_id: {"count": int, "first": str, "last": str, "last_commit": str}}`, `{}` when the file does not exist, and raises `BaselineError`
for a file that exists but is not the documented JSON (an unreadable history must not look like an empty one). Reads the file without locking: `record_flakes` replaces it atomically.

### `[@ANCHOR: known_red:record_flakes]` `record_flakes(path, test_ids, commit, now=None)`

Purpose: counts one flake for each id.

| Argument | Type | Meaning |
|---|---|---|
| `path` | str | The flake file; its directory is created. |
| `test_ids` | iterable of str | Ids that failed and then passed alone. Duplicates count once. |
| `commit` | str | The commit under test, stored as `last_commit`. |
| `now` | str or None | UTC timestamp `YYYY-MM-DDTHH:MM:SSZ`; default the current time. |

Returns the updated entries for those ids. Takes an exclusive `flock` on `<path>.lock`, then writes a temporary file and `os.replace`s it, so concurrent runs in other slots lose no count and
readers never see a half-written file (mode 0666, as several accounts write it). Raises `BaselineError` for a corrupt existing file and `OSError` for an unwritable directory.

### `[@ANCHOR: known_red:Classification]` `Classification`

A dataclass built by `classify`: `known_red` (dict id to `BaselineEntry`: tolerated), `new` (list of ids to rerun), `setup_new` (new class-level errors, never rerun),
`unparsed` (int: failures Odoo counted that no log line named), `now_passing` (baselined ids that were started and did not fail). One run, one process.

### `[@ANCHOR: known_red:classify]` `classify(observation, baseline, max_age=None, now=time.time)`

Purpose: splits the first run's failures. Returns a `Classification`; a baselined failure older than `max_age` days (default `max_age_days()`) lands in `expired`, not `known_red`. `unparsed` is `observation.counted() - len(observation.failures)` (not below 0): a failure that no line named is
treated as new, never tolerated. A baselined id that did not run is neither failing nor "now passing". Pure.

### `[@ANCHOR: known_red:rerun_tags]` `rerun_tags(test_ids)`

Purpose: the `--test-tags` value for the single rerun: `/module:Class.method` for each id, sorted, comma separated. Pure.

### `[@ANCHOR: known_red:PolicyOutcome]` `PolicyOutcome`

A dataclass: the policy's verdict. `test.py` stores it as `extractor.policy_outcome`, prints `summary_lines()` under the run headline, and writes `results(commit, counts)` to `test_results.json`.

| Field | Type | Meaning |
|---|---|---|
| `run_green` | bool | True when the run exits 0. `apply_failure_policy` sets it from the final exit status. |
| `known_red` | dict | Tolerated baselined failures, id to `BaselineEntry`. |
| `flakes` | dict | Ids that failed and passed alone this run, with their updated counter entries. |
| `failed_again` | list | New failures that failed again alone (all failures, with `--raw-failures`). |
| `setup_new` | list | New class-level errors. |
| `not_rerun` | dict | New failures not rerun, id to reason (systemic number, or an unnamed failure present). |
| `unparsed` | int | Failures Odoo counted that no log line named. |
| `now_passing` | list | Baselined ids that passed: their entries should be removed. |
| `fix_me` | dict | Flake entries above the threshold for modules under test. |
| `rerun_ran` | bool | Whether the rerun happened. |

`test_results.json` (version 1): `version`, `commit`, `run_green`, `counts` (`failed`, `errors`, `total`), `known_red` (id to owner, reason, first_seen), `flakes`, `new_failures`
(sorted failed-again, setup and not-rerun ids), `setup_errors`, `unparsed`, `now_passing`, `fix_me`.

### `[@ANCHOR: known_red:decide]` `decide(first, baseline, rerun, flake_path, commit, threshold, modules=None, now=None, max_rerun=25, max_age=None, clock=time.time)`

Purpose: applies the policy to the first run.

| Argument | Type | Meaning |
|---|---|---|
| `first` | `RunObservation` | The whole run. |
| `baseline` | dict | From `load_baseline`. |
| `rerun` | callable | `rerun(test_ids)` runs those tests alone once and returns a `RunObservation`. Called at most once. |
| `flake_path`, `commit`, `threshold`, `now` | str, str, int, str | See `record_flakes`, `flake_threshold`. |
| `modules` | set or None | Modules under test; limits the fix-me list to them. |
| `max_rerun` | int | More new failures than this are systemic: not rerun, red. |
| `max_age`, `clock` | float or None, callable | Days a baseline entry is tolerated (default `max_age_days()`: 3) and the time source its age is read from. An entry past it is `expired`: the run is red, it is not rerun. |

Returns a `PolicyOutcome` (with `expired`, `clock` and `limit`; `results()` adds `age_days` to each known-red entry and an `expired` map, and puts expired ids in `new_failures`). A rerun counts as a pass only when it printed a verdict, started the test and did not fail it; a rerun that did not run the test or printed no verdict leaves it red.
Writes the flake file when something passed alone. Raises `BaselineError` for a corrupt flake file. The rerun is the expensive part (database rebuild and module install).

### `[@ANCHOR: known_red:max_age_days]` `max_age_days(environ=None)`

Purpose: how long a baseline entry is tolerated. Returns `HAMS_KNOWN_RED_MAX_AGE_DAYS` or 3 (float). Raises `BaselineError` for anything that is not a finite number above 0: a limit that quietly became "forever" would defeat it. Pure.

### `[@ANCHOR: known_red:age_text]` `age_text(days)`

Purpose: `3.1 days`, or `12 hours` below one day, for summaries. Pure.

### `[@ANCHOR: known_red:show_lines]` `show_lines(baseline, max_age=None, clock=time.time)`

Purpose: `known_red.py show`: the baseline, oldest entry first, each with age, `(EXPIRED: fails the run)` when past the limit, owner, first-seen commit, renewal reason if any, and its reason; one line saying it is empty when it is. Returns a list of lines. Pure.

### `[@ANCHOR: known_red:renew_entry]` `renew_entry(baseline, test_id, reason, owner=None, now=time.time)`

Purpose: `known_red.py renew`: renews an entry for another window. Rewrites the file that holds the entry (`BaselineEntry.source`) with `renewed: {"time", "reason"}` (and `owner`), atomically, keeping every other field and entry; returns that path. Raises `BaselineError` for an empty reason or an id not in `baseline`. A renewal is a change to a tracked file: commit it with a pull request.

### `[@ANCHOR: known_red:main]` `main(argv=None, out=print)`

Purpose: the command line `python3 hams_shared/tools/known_red.py show|renew`; `--base-dir` (default the working directory) finds the files as `baseline_paths` does. Returns 0, or 2 with the cause (bad baseline, empty reason, unknown id).

### `[@ANCHOR: known_red:raw_outcome]` `raw_outcome(first)`

Purpose: the outcome for `--raw-failures`: every failure red, nothing tolerated, nothing rerun. Returns a `PolicyOutcome` with `run_green` False. Pure.

### `[@ANCHOR: known_red:git_head]` `git_head(directory)`

Purpose: the commit under test, recorded as `first_seen` hints, flake `last_commit` and in `test_results.json`. Returns the full 40-character id from `git rev-parse HEAD` (with `safe.directory` set for
this one call, as the runner does for worktrees), or "" on any failure. Never raises; 20 second timeout.

### `[@ANCHOR: known_red:write_results]` `write_results(path, outcome, commit, observation)`

Purpose: writes `test_results.json`. `path` is the file (its directory must exist), `outcome` a `PolicyOutcome`, `commit` a string, `observation` the first run's `RunObservation`
(supplies `counts`). Writes a temporary file and replaces atomically, mode 0644. Raises `OSError` when the directory is unwritable; `apply_failure_policy` prints that and carries on.

### `[@ANCHOR: test:apply_failure_policy]` `apply_failure_policy(args, extractor, rc_odoo, rerun, base_dir, target_modules, slot)`

Purpose: `test.py`'s one call into the policy, from `main()` after a failing standard-mode Odoo run.

| Argument | Type | Meaning |
|---|---|---|
| `args` | Namespace | Parsed options; `raw_failures` is read. |
| `extractor` | `FailureExtractor` | Supplies `observation`, `output_path`, `cpu_limit_line`, `signal_line`; receives `policy_outcome`. |
| `rc_odoo` | int | The run's exit status (non-zero). |
| `rerun` | callable | The rerun callback (`rerun_alone` in `main`: `rebuild_db`, then the same command with `--test-tags` replaced). |
| `base_dir`, `target_modules`, `slot` | str, list, int | The repository root, modules under test, the slot (names `test_results.json`). |

Returns the exit status: 0 when `rc_odoo` is 1, Odoo counted failures, there was no CPU limit or signal, and the outcome is green; else `rc_odoo` (also when the baseline or the flake file is invalid:
the cause is printed). Writes `test_results.json`; takes the slot's database for the rerun through the callback.

## Minimal module sets (`test.py --bisect-modules`, `bisect_modules.py`, ADR 0118)

```
test.py --bisect-modules -u <the failing module list> --test-tags /ham_club:TestA,/ham_club:TestB [--bisect-jobs 2] [--bisect-max-runs 40] [--bisect-report FILE] [-l LOGDIR]
```

A whole-system failure can depend on another module installed in the same database. This finds which ones, replacing the manual halving (the whole list, half, a quarter, ...).

1. The modules the tags name (`/module:...`) are fixed: the tests need their own module. The other modules of `-u` are the candidates.
2. Trial 1 runs every candidate. Its failing tests (from `test_results.json`) are the targets. If nothing fails, the report says `not_reproduced` and the exit status is 1.
3. Rounds of a binary search for the shortest prefix of the remaining candidates that, with the modules found so far, still fails every target; its last module
   is needed. A failure that needs k modules costs about k times log2(n) runs. With `--bisect-jobs 2` two prefixes run at once (the default when the machine has
   two or more slots; never more than 2). The search assumes adding a module never hides the failure; the set it returns was run and did reproduce.
4. Each trial is an ordinary `test.py` run (`-u <set> --test-tags <tags> --raw-failures --wait --solo -l <trial directory>`), so it waits its turn in the slot queue, runs
   in its own database and is `nice 10` like the driver. Trials that die without a verdict count as not reproducing. `--raw-failures` means a baselined or flaky test is not hidden.
5. Output: the minimal `-u` set and the still-failing tests, and a JSON report (`bisect_<time>.json` in the log directory, or `--bisect-report`): `status` (`minimal`,
   `incomplete` when `--bisect-max-runs` stopped it, `not_reproduced`), `fixed`, `candidates`, `needed`, `minimal`, `targets`, `runs`, `trials` (modules, exit status,
   failed tests, whether it reproduced, seconds, log directory). Exit status 0: minimal; 1: not reproduced or incomplete; 2: bad arguments; 75: the full run had no verdict.

### `[@ANCHOR: bisect_modules:find_needed]` `find_needed(items, reproduces, jobs=1, max_runs=40, on_step=None)`

Purpose: the search of point 3; knows nothing about `test.py`.

| Argument | Type | Meaning |
|---|---|---|
| `items` | list of str | Ordered candidate modules. All together must reproduce (the caller ran them). |
| `reproduces` | callable | `reproduces(sublist) -> bool`: True when every target test still fails with those modules (plus the fixed ones). Called from up to `jobs` threads; never twice for the same sublist. It must be thread safe. |
| `jobs` | int | 1: halve; 2 or more: probe two prefixes at once. |
| `max_runs` | int | Stop after this many calls of `reproduces`. |
| `on_step` | callable or None | `on_step(sublist, result)` after each call (from the calling thread). |

Returns `(needed, complete, runs)`: `needed` in the order of `items` (`[]` when no extra module is needed), `complete` False when `max_runs` stopped the search or the failure was not deterministic (needed
alone failed to reproduce although the whole set did); in that case `needed` is the smallest set known to reproduce, plus the part not searched. `runs` the number of calls. Blocks until the runs end; no other side effects.

### `[@ANCHOR: bisect_modules:read_failed_tests]` `read_failed_tests(log_directory)`

Purpose: the failing tests of a finished trial. Returns the `new_failures` ids of every `test_results*.json` in the directory, or None when there is no file (the run passed or died before a verdict;
the caller uses the exit status to tell). Raises `BisectError` for a file that cannot be parsed or has no `new_failures` list. Reads files only.

### `[@ANCHOR: bisect_modules:child_command]` `child_command(test_py, modules, test_tags, log_directory, extra=())`

Purpose: the command line of one trial. Returns a list: the Python interpreter, `test_py`, `-u` with the modules joined by commas, `--test-tags`, `--raw-failures`, `--wait`, `--solo`, `-l` the directory, then `extra`. Pure.

### `[@ANCHOR: bisect_modules:Bisector]` `Bisector(test_py, fixed, test_tags, work_directory, targets=None, runner=None, extra=())`

Runs trials and decides what reproduces. `fixed`: modules in every trial; `work_directory`: where `trial_NNN` directories are made (mode 0777, because the nested run may be another account); `targets`: the ids that must all fail
(None until `bisect` ran the full set); `runner(command) -> exit status` (default `subprocess.call`, replaced in tests); `trials`: the `Trial` records in order. `run_trial(extra_modules)` runs one and returns it;
`reproduces(extra_modules)` is True when the targets are all in the trial's failures (False when targets are unknown, empty, or the trial died). Thread safe: two threads may run trials, each in its own directory.

### `[@ANCHOR: bisect_modules:bisect]` `bisect(bisector, candidates, jobs=1, max_runs=40, out=print)`

Purpose: the whole bisection. Runs the full set, sets the targets from its failures, calls `find_needed`, and returns the report dictionary described above. Returns `status` `not_reproduced` when the full run had no failing test. Raises `BisectError`
when the full run exited non-zero and wrote no results (it died; there is no verdict to reduce). `out` receives one progress line per trial.

### `[@ANCHOR: bisect_modules:write_report]` `write_report(path, report)`

Purpose: stores the report as JSON (indent 1, sorted keys), through a temporary file and an atomic replace; creates the directory. Raises `OSError` when it cannot write.

### `[@ANCHOR: bisect_modules:run]` `run(args, test_py, tag_modules, runner=None, out=print, slots=1)`

Purpose: the `--bisect-modules` command, called from `main()` before any lock is taken (the trials take the slots, the driver does not).

| Argument | Type | Meaning |
|---|---|---|
| `args` | Namespace | The parsed command line: `module`, `test_tags`, `log_directory`, `bisect_jobs`, `bisect_max_runs`, `bisect_report`. |
| `test_py` | str | Path of `test.py`, run for each trial. |
| `tag_modules` | list | Modules the tags name; fixed in every trial. |
| `runner`, `out` | callable | As in `Bisector` and `bisect`. |
| `slots` | int | The machine's slot count; `--bisect-jobs` defaults to `min(2, slots)`. |

Returns 0, 1, 2 or 75 as above. Lowers its own priority with `os.nice(10)`; each trial inherits it. Writes the report; never kills a process. The trials run on this machine only.

## Whole-system gate tree (`gate_snapshot.py`, `test.py --snapshot-gate`, ADR 0118)

A whole-system run needs hams_com, hams_open and the hams_shared commit hams_open records, all at exact commits, plus build outputs some tests load, and `HAMS_OPEN_TREE`
pointing at the right hams_open. Three gate shards failed in one night on a hams_open checkout behind `origin/main` and on a `libcloudflared.so` copied over with an old
timestamp. `gate_snapshot.py` builds all of it from the current `origin/main` commits and refuses to continue when any input is stale or missing.

```
python3 hams_shared/tools/gate_snapshot.py create  [--workspace ~/workspace] [--parent DIR] [--shards 4] [--shards-file FILE] [--no-fetch] [--no-build] [--cargo-target-dir DIR]
python3 hams_shared/tools/gate_snapshot.py verify  GATE_DIR [--no-fetch]    # exit 1 and every problem named when stale
python3 hams_shared/tools/gate_snapshot.py commands GATE_DIR                # the shard command lines
python3 hams_shared/tools/gate_snapshot.py prune   [--parent DIR] [--keep 1]
python3 hams_shared/tools/test.py --snapshot-gate                           # create with the defaults
```

`create`: refuses on the production host (`hams1`, or a production marker file); fetches `origin main` of hams_com, hams_open and hams_shared; reads the three commits (hams_shared
from the gitlink of hams_open's `origin/main`); makes `<parent>/gate_<com>_<open>/{hams_com,hams_open,hams_open/hams_shared}` as detached worktrees (an existing directory for the
same commits is reused); builds `libcloudflared.so` (with `build_cloudflared_ffi.py --force`) and the relay test binary (`cargo build --features test-hooks --locked`, then the
`.console_tree` file) when they are missing or stale, at `nice -n 10`; writes a stamp (`<output>.gate_stamp`: the git tree ids of the sources) beside each output; chooses shards
(`--shards-file`: modules keep their shard from an earlier `gate_shards.json`; otherwise round robin); writes `gate_snapshot.json` and `gate_shards.json` (a list of lists of
module names) in the gate directory; verifies; prints the shard command lines. No test is started.

`verify` (also run at the end of `create`) refuses when: a tree is missing, not at its recorded commit, or has uncommitted changes; a tree's commit is no longer `origin/main`
(after a fetch); `HAMS_OPEN_TREE` is unset or is not the gate's hams_open; the hams_com tree's `hams_shared` link does not resolve into the gate; a shard names a module with no manifest;
an output is missing, has no stamp for its current sources (copied in or built from other sources), is older than a source file, is built for another CPU, or (relay) lacks a
`.console_tree` naming the current console, is not executable, or `HAMS_CONSOLE_TEST_RELAY_BIN` names another file. The relay builds into `--cargo-target-dir` (default
`$CARGO_TARGET_DIR`, else `~/.cache/hams-test/gate/cargo-target`).

### `[@ANCHOR: gate_snapshot:refuse_production]` `refuse_production(hostname=None, marker_files=(...))`

Purpose: the gate never runs on production. Raises `SnapshotError` when the host name is `hams1` (or starts with `hams1.`) or a marker file exists; returns None otherwise. Reads the host name and file metadata.

### `[@ANCHOR: gate_snapshot:origin_main]` `origin_main(repo, fetch=True)`

Purpose: the commit that is `origin/main` now. `fetch` runs `git fetch -q origin main` first (network). Returns the 40-character id. Raises `SnapshotError` when the fetch or `rev-parse` fails: a stale answer must not look current.

### `[@ANCHOR: gate_snapshot:recorded_shared_commit]` `recorded_shared_commit(open_repo, open_commit)`

Purpose: which hams_shared commit hams_open's tree records. Returns the id of the `hams_shared` gitlink (`git ls-tree`). Raises `SnapshotError` when there is none.

### `[@ANCHOR: gate_snapshot:gate_dir_name]` `gate_dir_name(com_commit, open_commit)`

Purpose: the snapshot directory name `gate_<9 hex>_<9 hex>`; the same commits give the same name, so a rerun reuses the tree. Pure.

### `[@ANCHOR: gate_snapshot:create_trees]` `create_trees(workspace, parent, com_commit, open_commit, shared_commit)`

Purpose: makes the three worktrees. `workspace` holds the main checkouts `hams_com`, `hams_open` and `hams_open/hams_shared`; `parent` the directory for `gate_*`; the commits must exist in those repositories.
Returns `Trees` (frozen dataclass: `parent` (the gate directory), `com`, `open`, `shared` paths, and `com_commit`, `open_commit`, `shared_commit`; one snapshot, serialized in `gate_snapshot.json`). Raises `SnapshotError` for a missing
commit, a failing `git worktree add`, or a hams_com `hams_shared` link that does not resolve into the gate. Writes worktrees (and removes the empty `hams_shared` directory of the hams_open worktree); needs no network.

### `[@ANCHOR: gate_snapshot:artifact_output]` `artifact_output(artifact, trees, cargo_target_dir)`

Purpose: where an output is. `Artifact` (frozen dataclass: `name` `cloudflared_ffi` or `relay_test_hooks`, `tree` `com` or `open`, `sources` tree-relative directories) is listed in `ARTIFACTS`. Returns
`<hams_open tree>/daemons/cloudflared-ffi/libcloudflared.so` or `<cargo_target_dir>/debug/hams_local_relay`. Pure.

### `[@ANCHOR: gate_snapshot:source_stamp]` `source_stamp(artifact, trees)`

Purpose: the content of the stamp file: `{source directory: git tree id at the tree's HEAD}` (`git rev-parse HEAD:<dir>`). Changes whenever any file of a source directory changes in a commit. Reads git only.

### `[@ANCHOR: gate_snapshot:artifact_problems]` `artifact_problems(artifact, trees, cargo_target_dir, environ=None)`

Purpose: whether an output may be used. Returns a list of sentences, empty when it may. The checks are the output and stamp rules of `verify`; `environ` supplies `HAMS_CONSOLE_TEST_RELAY_BIN`. Reads files and git; changes nothing.
Source files are compared by modification time excluding build products (`libcloudflared.so`, its header, stamps, `.console_tree`).

### `[@ANCHOR: gate_snapshot:build_artifact]` `build_artifact(artifact, trees, cargo_target_dir, run=None, out=print)`

Purpose: builds one output and stamps it. `run(command, cwd, env)` returns an exit status (default `nice -n 10` plus `subprocess.call`; tests pass a fake). Returns the output path. Raises `SnapshotError` when the command fails or the output
is missing afterwards. Slow (Go build; cargo build, minutes cold). Side effects: the build outputs, the `.console_tree` and stamp files.

### `[@ANCHOR: gate_snapshot:discover_modules]` `discover_modules(trees)`

Purpose: the whole-system module list: directories at the root of the hams_com and hams_open trees that contain `__manifest__.py`, sorted, unique. Pass `--shards-file` to keep a curated list's placement; modules absent from it are added.

### `[@ANCHOR: gate_snapshot:make_shards]` `make_shards(modules, count, previous=None)`

Purpose: the shard split. Returns a list of lists of module names, each module in exactly one list (`gate_shards.json` form). With `previous`, old placement is kept, vanished modules dropped, new modules go to the smallest shard. Pure.

### `[@ANCHOR: gate_snapshot:shard_command]` `shard_command(trees, shard, log_directory, relay_binary, extra_env=None)`

Purpose: the exact command line of one shard: `cd <gate hams_com> && source /etc/profile.d/hams_test_environment.sh && HAMS_OPEN_TREE=<gate hams_open> HAMS_CONSOLE_TEST_RELAY_BIN=<binary> HAMS_TEST_BATCH=off nice -n 10 python3 hams_shared/tools/test.py --wait -l <log directory> -u <modules>`. The full pipeline as the invoking user (a slot, a private network namespace, its own databases), queued (`--wait`) behind other runs; `HAMS_TEST_BATCH=off` because a snapshot run must test exactly the snapshot's commits and not join a batch of other branches. No `sudo -u odoo`, `HAMS_ISOLATED_NS` or `HAMS_CI_LOAD_GATE_RATIO`: those stalled runs. Pure; the string is run by a shell.

### `[@ANCHOR: gate_snapshot:verify_snapshot]` `verify_snapshot(gate_dir, fetch=True, environ=None, cargo_target_dir=None)`

Purpose: the refusal. Returns every problem listed above (empty: current and complete). `fetch` refreshes `origin/main` of the repositories first; `environ` is where `HAMS_OPEN_TREE` is read (default `os.environ`). Raises `SnapshotError` for an unreadable manifest. A stale
input is a returned problem, never an exception, so one call names all of them.

### `[@ANCHOR: gate_snapshot:create_snapshot]` `create_snapshot(workspace, parent, shards, shards_file, fetch, build, cargo_target_dir, run, out, hostname)`

Purpose: `create`. Returns the manifest dictionary (`version`, `created`, `workspace`, `trees`, `cargo_target_dir`, `shards`, `commands`). Raises `SnapshotError` for a production host, a failed step, a malformed shard file, or a snapshot that does not verify afterwards.
Takes no test slot and starts no test.

### `[@ANCHOR: gate_snapshot:prune_snapshots]` `prune_snapshots(parent, keep=1, out=print)`

Purpose: removes old trees. Keeps the `keep` newest `gate_*` directories of `parent` (by modification time); for each other one runs `git worktree remove --force` through the repository that owns each worktree, then deletes the directory. Returns the removed paths. Never touches other names.

### `[@ANCHOR: gate_snapshot:main]` `main(argv=None, out=print)`

Purpose: the command line above. Returns 0 on success, 1 for a stale or incomplete snapshot or a failed step (`REFUSING:` and one `  - problem` line each), 2 on the production host.

## Nightly whole-system canary (`gate_canary.py`, ADR 0118)

```
python3 hams_shared/tools/gate_canary.py run [--only-if-new-commit] [--jobs 1] [--max-jobs-when-idle 2] [--no-bisect] [--shards 4] [--shards-file FILE] [--state-dir ~/.cache/hams-test/canary]
python3 hams_shared/tools/gate_canary.py status
```

`run` takes `<state-dir>/canary.lock` (a second run exits 3 at once), then: builds the gate tree with `gate_snapshot.py` (a stale or missing input is a refusal, exit 2), runs each shard's command line
(`gate_snapshot.shard_command`, so through the ordinary `test.py` slot queue, `nice -n 10`, as the invoking user) one shard at a time (`--jobs`; `--max-jobs-when-idle` raises it when the one-minute load
average is below 4, never above the machine's slot count), re-verifies the snapshot (no fetch) before each shard, reads each shard's `test_results.json` (written by `test.py` when a run fails; known-red and flaky
tests are already separated, ADR 0118) and writes, atomically:

* `<state-dir>/status.json`: `commit` (the hams_com commit), `commits` (`com`, `open`, `shared`), `time`, `status` (`green` or `red`), `new_failures`, `tolerated`, `flakes`, `now_passing`, `fix_me`, `shard_errors`, `counts`, `shards`, and
  `bisect` (per shard: `tags`, `report`, `exit_status`) when a bisection ran. A shard that exits non-zero without results, a shard whose input was stale, and failures no log line named are `shard_errors`: the night is red, never silently green.
* `<state-dir>/known_red_candidates.json`: the red tests of this run in the baseline's format (`{"version": 1, "entries": [{test_id, first_seen, owner, reason}]}`): tolerated tests keep their entries, new failures become `unassigned`
  entries with the canary's commit as `first_seen`. A proposal only: nothing writes it into a tracked baseline.
* `<state-dir>/gate_shards.json`: the shard split, reused by the next run so a module keeps its shard.

When a NEW failure appears, each shard that has one is bisected: the shard's own command with `--bisect-modules --test-tags /module:Class,... --bisect-max-runs 30 --bisect-report <shard log directory>/bisect_report.json`
appended (see "Minimal module sets"). Exit status: 0 green, 1 red, 2 refused, 3 already running. `--only-if-new-commit` does nothing when `origin/main` of hams_com and hams_open is what the last run tested.

It runs on this machine only: no remote host, no `kill`, refuses on the production host. Each shard is the full pipeline (a private network namespace per run), so shards that run at once work in any slot, RabbitMQ modules included; `--jobs` and `--max-jobs-when-idle` bound how many.


**systemd.** `tools/hams-gate-canary.service` (oneshot, `Nice=10`, idle I/O, `--only-if-new-commit`) and `tools/hams-gate-canary.timer` (01:30 nightly, `Persistent=true`) are user units. They are provided and not enabled; the install and
enable commands are in the service file's header comment. On demand: `systemctl --user start hams-gate-canary.service` or run the script.

### `[@ANCHOR: gate_canary:read_shard_results]` `read_shard_results(log_directory)`

Purpose: one shard's verdict. Returns the `test_results*.json` files of the directory merged into one dictionary (`new_failures`, `known_red`, `flakes`, `now_passing`, `fix_me`, `unparsed`, `counts`, `commit`), or None when there is none (the shard
passed, or died before a verdict; the exit status tells which). Raises `CanaryError` for a file that is not the documented JSON. Reads files.

### `[@ANCHOR: gate_canary:aggregate]` `aggregate(shards, commits, now=None)`

Purpose: the status document. `shards` is a list of `{"number", "modules", "exit_status", "results", "problems"?}`; `commits` is `{"com", "open", "shared"}`; `now` an ISO UTC time (default now). Returns the `status.json` dictionary described above, lists sorted and unique,
`status` `red` when there is a new failure or a shard error. Pure.

### `[@ANCHOR: gate_canary:baseline_candidates]` `baseline_candidates(status, known)`

Purpose: the proposal file. `known` maps a test id to `{"owner", "reason", "first_seen"}` (from the shard results' `known_red`). Returns `{"version": 1, "entries": [...]}` sorted by id: the loader `known_red.load_baseline` accepts it unchanged. Pure.

### `[@ANCHOR: gate_canary:note_lines]` `note_lines(status, lint_lines=())`

Purpose: `<state-dir>/note.txt`, the canary note that lists every tolerated item so none is forgotten: the verdict line, each new or expired failure, each shard error, each tolerated test with its age and owner (`tolerated_detail`), flaky tests, FIX ME flakes, baselined tests that now pass, and the lint baseline's `--baseline-show` lines. Returns a list of lines. Pure.

### `[@ANCHOR: gate_canary:failure_tags]` `failure_tags(test_ids)`

Purpose: the `--test-tags` of a bisection. Returns `/module:Class` for each distinct class among `module:Class.method` ids, sorted and comma separated (a whole class keeps its `setUpClass`); other ids are ignored. Pure.

### `[@ANCHOR: gate_canary:bisect_command]` `bisect_command(shard_command, tags, report_path)`

Purpose: the bisection command of a shard: its command line with `--bisect-modules --test-tags <tags> --bisect-max-runs 30 --bisect-report <report_path>` appended, so the tree, environment, account and `-u` list are the shard's own. Pure.

### `[@ANCHOR: gate_canary:choose_jobs]` `choose_jobs(requested, idle_cap, slots, shard_count, load=None)`

Purpose: shards at once. `requested` is the base; when the one-minute load average (`load`, default `os.getloadavg()[0]`) is below 4 the larger of `requested` and `idle_cap` is used; the result is never above `slots` or `shard_count` and at least 1. Returns an int.

### `[@ANCHOR: gate_canary:shell_runner]` `shell_runner(command, log_path)`

Purpose: runs a shard command line with `bash -c`, appending its output to `log_path`, and returns the exit status. Blocks until it ends; signals nothing. `run_canary` takes any function of the same shape so tests can replace it.

### `[@ANCHOR: gate_canary:run_canary]` `run_canary(workspace, parent, state_dir, shards, shards_file, jobs, idle_jobs, only_if_new_commit, bisect, fetch, runner, snapshot, out, hostname, slots, load)`

Purpose: one canary run. Returns `(exit status, status document or None)`; None when nothing ran (refused, locked, or unchanged). Exit statuses: 0 green, 1 red, 2 refused (production host, stale or missing input), 3 another run holds the lock.
`runner`, `snapshot`, `hostname`, `slots`, `load` replace the shell runner, `gate_snapshot.create_snapshot`, the host name, the slot count and the load average (tests). Lowers its own priority with `os.nice(10)`; takes `canary.lock` with a non-blocking `flock` held for the run.
Long: the shards take as long as the whole-system run (about 75 minutes of tests per set of modules), then the bisections.

### `[@ANCHOR: gate_canary:main]` `main(argv=None, out=print)`

Purpose: the command line. `run` returns `run_canary`'s status (1 also for unreadable results); `status` prints `status.json` and returns 0, or 1 when there is none.

### `[@ANCHOR: test:legacy_load_gate_ratio]` `legacy_load_gate_ratio(environ, ratio, say=print)`

Purpose: stops the old `HAMS_CI_LOAD_GATE_RATIO=3` command line from stalling runs. Called by `wait_for_ci_load_to_subside`.

| Argument | Type | Meaning |
|---|---|---|
| `environ` | mapping | Where `HAMS_CI_LOAD_GATE_RATIO` and `HAMS_CI_LOAD_GATE_LEGACY` are read. |
| `ratio` | float | The parsed ratio (`_ci_load_gate_settings`). |
| `say` | callable | Prints the warning. |

Returns None when the ratio variable is unset or set without `HAMS_CI_LOAD_GATE_LEGACY=1` (and then calls `say` with a loud warning that the variable is ignored: it would apply the load-average rule Bruce rejected and override the CPU/pressure rule),
and `ratio` when `HAMS_CI_LOAD_GATE_LEGACY=1` is also set. Pure apart from `say`.

### `[@ANCHOR: test:warn_isolated_ns_with_slots]` `warn_isolated_ns_with_slots(environ, slots, say=print)`

Purpose: warns about `HAMS_ISOLATED_NS=1` on a machine with several slots, called from `main()` right after the slot count is known. Returns True when it warned: `HAMS_ISOLATED_NS` is `1`, this is not the re-exec'd final phase
(`HAMS_TEST_LOCK_HELD` is not `1`) and `slots` is above 1. The message says the run is pinned to the host network namespace (RabbitMQ modules refused above slot 1, ADR 0116) and gives the normal command, `python3 hams_shared/tools/test.py --wait -u <modules>`.
Never changes behaviour. Pure apart from `say`.

## `check_pip_audit.py`: a passing audit is reused, a stalled one is retried once

`pip-audit` waits on PyPI's advisory API, and a stalled PyPI (or a starved box) used to fail unrelated branches after 300 s. Now: (1) a requirements file whose audit PASSED is not audited again for
`HAMS_PIP_AUDIT_CACHE_HOURS` hours (default 6; 0 turns the cache off), as long as the file, its path and the pip-audit version are unchanged; the run prints "reusing that result" and the age, so it is never silent; the entry
expires, so a new vulnerability is still found within the window. (2) Only a PASS is ever cached: a finding, a timeout and any other error are not. (3) A call that exceeds the limit (300 s, stretched by `HAMS_LINT_TIMEOUT_SCALE`) is
retried once before the gate fails, "twice" in the message; the bound is two limits. The runner's own serial retry of timed-out steps (`retry_timed_out_steps`) applies only when the machine was saturated at the end, which a stalled network is not,
so this in-step retry covers that case. Cache files: `~/.cache/hams-lint/pip-audit/<key>.json` (`HAMS_PIP_AUDIT_CACHE_DIR` overrides).

### `[@ANCHOR: check_pip_audit:cache_hours]` `cache_hours(environ=None)`

Returns the reuse window in hours: `HAMS_PIP_AUDIT_CACHE_HOURS` as a float of at least 0 (0 means off), else 6.0 for an unset, non-numeric, infinite, NaN or negative value. Pure.

### `[@ANCHOR: check_pip_audit:included_requirement_files]` `included_requirement_files(req_file)`

Returns the real paths of every file `req_file` includes through `-r`, `--requirement`, `-c` or `--constraint` lines (`-r x`, `-rx`, `--requirement x`, `--requirement=x`; a ` #` comment is dropped), directly or through other included files, in first-seen order, each once; a cycle ends where it closes. A relative path is taken from the including file's directory, as pip does. Raises `OSError` for an included file that cannot be read, an include that names a URL, or nesting deeper than `MAX_INCLUDE_DEPTH` (8): an input the key cannot cover must never be answered from the cache.

### `[@ANCHOR: check_pip_audit:cache_key]` `cache_key(req_file, version_text)`

Returns 64 hex digits: SHA-256 over the real path of `req_file`, its content, the path and content of every file in `included_requirement_files(req_file)`, and the stripped `version_text` (the `pip-audit --version` output), each length-prefixed. Another checkout path, an edit to the file or to an included file, or another pip-audit version gives another key. Raises `OSError` for an unreadable file or included file (the audit then runs and nothing is cached).

### `[@ANCHOR: check_pip_audit:cached_pass_age_hours]` `cached_pass_age_hours(key, now=None, environ=None)`

Returns the age in hours of the recorded pass for `key`, or None when there is none, it is older than `cache_hours()`, it is dated in the future, the cache is off, or the file is unreadable or malformed. Reads one small JSON file; never raises.

### `[@ANCHOR: check_pip_audit:store_pass]` `store_pass(key, now=None, environ=None)`

Records `{"passed_at": now}` for `key` (default the current time) in the cache directory, through a temporary file and an atomic replace; creates the directory. Returns True when written; False (and nothing happens) when the cache is off or the directory cannot be written: the cache is an optimisation and never a reason to fail.

## Staged-file size check

`check_staged_files.py` (run by `git-hooks/pre-commit`) refuses a commit that stages a file over 5 MB (`MAX_BYTES`) or
anything under a `target/` directory.

### `[@ANCHOR: tools:large_files_allowed]` `LARGE_FILES_ALLOWED`

- **What it is:** a `frozenset` of repository-relative paths (strings, `/`-separated, exactly as `git diff --cached
  --name-only` prints them) that may be staged even though they are larger than `MAX_BYTES`. Membership is by exact
  path: the same file name in another directory is still refused, and a path under `target/` is refused whatever
  this set says (the build-output check runs first).
- **Members and why:** `daemons/hams_local_relay/rade_c/third_party/opus-940d4e5af64351ca8ba8390df3f555484c567fbb.zip`
  (5.3 MB): GitHub's zip of Opus commit 940d4e5, the exact bytes `rade_c/cmake/BuildOpus.cmake` checks by SHA-256.
  Committed on Bruce's decision of 2026-11-07 so relay builds need no download; cloud sessions, whose GitHub gate
  refuses repository archives, can then build the relay.
- **Invariants and concerns:** add a path only for a deliberate, reviewed large file, with its reason in the comment
  beside the set and here. The set is read-only at run time; nothing else exempts a file from the size limit.
- **Tests:** `tools/test_check_staged_files.py`,
  `test_the_named_opus_archive_may_exceed_the_limit_but_the_same_name_elsewhere_may_not`.

## Night-shift history: one file per entry

Bruce, 2026-11-08: "Convert night shift history into a directory." The completed-work log of the night-shift agents was
one file, `night_shift_history.md` (about 1 MB, newest entry at the top, older entries in
`night_shift_history_archive/part_NN/`), and every agent edited it at the top. Every pull request therefore conflicted
with every other, the conflict was resolved by hand (keep both sides) in nearly every pull request, and a conflicting
branch is dropped to a solo run by the lint and test batching (ADR 0115). The log is now the directory
`night_shift_history/` in the repository that keeps it, **one file per entry**. A new entry is a new file with a name no
other agent picks, so two branches never touch the same path.

`night_shift_history.py` is the whole tool: `latest` prints entries, `new` creates one, `check` lints the layout and is the
`check_night_shift_history` step of `run_linters.py`. The step runs on the repository under lint only; `hams_open` and
`hams_shared` have no such directory and are not judged.

```bash
python3 hams_shared/tools/night_shift_history.py latest -n 5       # the 5 newest entries, newest first (default 5)
python3 hams_shared/tools/night_shift_history.py latest --titles   # one line per entry: file name, tab, heading
python3 hams_shared/tools/night_shift_history.py new --title "Short title" <<'TEXT'
- what was wrong, what you changed (commit or PR), how you verified it
TEXT
python3 hams_shared/tools/night_shift_history.py check             # the layout and every entry file; exit 1 on a problem
```

**The layout, and the two-step rollout.** Without `night_shift_history/` the repository has the old single-file layout and
`check` judges nothing (`latest` then reads the top of the old file; `new` refuses and says to add the entry at the top of
the old file). With the directory, `night_shift_history.md` must be exactly the stub (`STUB_TEXT`), the archive directory
must not exist, and every file in the directory is an entry or this directory's `README.md`. So tooling that knows both
layouts lands first, then one pull request creates the directory, replaces the old file with the stub and deletes the
archive. A branch that was started earlier and still appends to the old file conflicts with the stub when it is merged, and
if the conflict is "resolved" by keeping its text, `check` fails with the instruction to move the entry into a new file.

### `[@ANCHOR: tools:night_shift_history_entry_format]` `NAME_PATTERN`, `STRICT_HEADING`, `LEGACY_THROUGH`, `SLUG_MAX`

- **What it is:** the format of one entry file. The name is `YYYY-MM-DD-HHMMSS-<slug>-<6 hex>.md`: a calendar date and
  a time of day (the local time of the machine that wrote it), a slug of at most `SLUG_MAX` (60) characters, and six random
  lower-case hex digits (`secrets.token_hex(3)`, 24 bits). Plain string order of the names is time order, so `ls` is the
  history and the last name is the newest entry. Two agents never pick the same name: the date, second, slug and random
  digits would all have to agree. The text is UTF-8 with `\n` line ends and ends with exactly one newline, and it starts with
  the heading `### YYYY-MM-DD -- <title>` (`STRICT_HEADING`), a blank line, then the body, which is free-form Markdown
  (bullets of what was wrong, what changed, the commit or pull request, how it was verified).
- **Fields of the name:** `date` (`YYYY-MM-DD`; equals the date in the heading), `time` (`HHMMSS`, 00-23, 00-59, 00-59),
  `slug` (`[a-z0-9]+` words joined by single hyphens, made by `slugify()` from the title), `rand` (`[0-9a-f]{6}`).
- **One entry per file:** the body must not contain a line that `split_entries()` would take as the start of another entry
  (a `## ` heading, or a `### ` heading that starts with a date), outside a code fence. Use `#### ` for sub-headings.
- **Entries migrated from the old files (dated `LEGACY_THROUGH`, 2026-11-08, or earlier):** the text is the old entry
  byte for byte, so its heading is whatever the old file had (`## ...`, wrapped over several lines, no date) and is
  only required to be one entry heading. Their file names use the date of the heading (else of the nearest dated entry
  before it in the same old file) and, in place of a time of day, `00` and a four digit number that keeps the order the
  old files had on that day; the random digits come from a hash of the entry. `check` does not require a real time of day,
  the dated heading or the blank line for them. Every entry dated later must follow the format exactly.
- **Invariants and concerns:** the constants are read-only. Do not rename or edit an existing entry file to "correct" it
  after the pull request that added it is merged: the history is the record of what was true when it was written. The name
  is not a secret and carries no identity; put the branch or pull request number in the text.
- **Tests:** `tools/test_night_shift_history.py`, `EntryProblemsTests`.

### `[@ANCHOR: tools:night_shift_history_stub_text]` `STUB_TEXT`

- **What it is:** the only content `night_shift_history.md` may have once `night_shift_history/` exists: a short Markdown
  note saying where the history lives and how to read, add and check entries.
- **Why exact:** a stale branch that still appends an entry to the old file, or recreates it, produces a file that differs
  from the stub, so `check_tree()` reports it. A text that merely "starts with" the stub would let an appended entry slip by.
- **Invariants and concerns:** a change to the stub text is a change to this constant and, in the repository that keeps the
  log, to `night_shift_history.md`, in the same pull request, or every run of `check` fails there.
- **Tests:** `tools/test_night_shift_history.py`, `CheckTreeTests`.

### `[@ANCHOR: tools:night_shift_history_split_entries]` `split_entries(text)`

- **Purpose:** cuts the text of an old-style history file into its entries. It is how the entries were migrated from the
  single files into the directory, how `check` decides that a file is exactly one entry, and how `latest` reads the old
  layout.
- **Call interface:**

  | name | type | meaning |
  | --- | --- | --- |
  | `text` | `str` | the complete text of a history file or entry file |
  | returns | `(str, list[str])` | `(preamble, entries)` with `preamble + "".join(entries) == text` exactly: nothing is dropped, added or re-wrapped, and every entry keeps its own trailing blank lines |

  No exceptions. A text without any entry heading returns `(text, [])`.
- **The rule:** an entry starts at a line `## ...`, or `### YYYY-MM-DD...`, that is not inside a code fence (a line starting
  with up to three spaces and then three backticks or three tildes toggles the fence). A heading line directly under a
  heading line of the same level is the rest of a heading wrapped over several lines, not a new entry. All other headings
  (`# `, undated `### `, `#### `) are body. The old files used `###` for newest entries and `##` for older ones, and wrapped
  long titles by repeating the `## ` marker, which is why the rule is this one.
- **Special concerns:** pure; reads nothing, no locks; linear in the text size.
- **Example:** `split_entries("### 2026-11-08 -- A\n\nx\n\n## B\n\ny\n")` returns `("", ["### 2026-11-08 -- A\n\nx\n\n", "## B\n\ny\n"])`.
- **See also:** `heading_text()`, `entry_problems()`. **Tests:** `SplitEntriesTests`.

### `[@ANCHOR: tools:night_shift_history_heading_text]` `heading_text(entry)`

- **Purpose:** the title of an entry on one line, for `latest --titles`, for file-name slugs and for the date of an
  old entry.
- **Call interface:** `entry` (`str`, text that starts with an entry heading, as returned by `split_entries()`). Returns a
  `str`: the heading line and its wrapped continuation lines (same `#` marks) without the marks, stripped and joined by a
  single space; `""` when the text does not start with a heading. No exceptions. Pure.
- **See also:** `split_entries()`, `slugify()`. **Tests:** `SplitEntriesTests`.

### `[@ANCHOR: tools:night_shift_history_slug]` `slugify(title)`

- **Purpose:** the slug part of an entry file name, so a directory listing says what each entry is about.
- **Call interface:** `title` (`str`, heading text; a leading `YYYY-MM-DD` is dropped). Returns a `str`: lower case ASCII
  letters and digits, runs of anything else collapsed into one hyphen, no leading or trailing hyphen, at most `SLUG_MAX`
  characters (cut at a hyphen when there is one in the second half), `"entry"` when nothing is left (all punctuation or all
  non-ASCII). No exceptions. Pure.
- **Example:** `slugify("2026-11-08 -- Fix the foo() bug")` is `"fix-the-foo-bug"`. **Tests:** `SlugTests`.

### `[@ANCHOR: tools:night_shift_history_entry_problems]` `entry_problems(name, text)`

- **Purpose:** the rules for one entry file, shared by `check_tree()` (every file) and `new_entry()` (before it writes).
- **Call interface:**

  | name | type | meaning |
  | --- | --- | --- |
  | `name` | `str` | the file name only, no directory |
  | `text` | `str` | the file contents, read with `newline=""` so a carriage return is seen |
  | returns | `list[str]` | one message per problem, each starting with the file name; empty when the entry is well formed |

  What it reports: a name that does not match `NAME_PATTERN`; a date that is not on the calendar; (entries dated after
  `LEGACY_THROUGH`) a time of day that is not one; a carriage return or NUL byte; a text that does not end with exactly one
  newline; text before the heading; more or fewer than one entry in the file; (entries dated after `LEGACY_THROUGH`) a
  first line that is not `### <date> -- <title>` with the date of the name, or a heading not followed by a blank line and
  a first line of text. No exceptions. Pure.
- **See also:** `NAME_PATTERN`, `split_entries()`. **Tests:** `EntryProblemsTests`.

### `[@ANCHOR: tools:night_shift_history_check_tree]` `check_tree(root)`

- **Purpose:** the linter. Decides which layout `root` has and reports everything that makes it invalid.
- **Call interface:** `root` (`str`, repository root). Returns `list[str]` of messages, empty when fine. With no
  `root/night_shift_history/` directory it returns `[]` whatever else exists (the old layout, or a repository that keeps no
  log). With the directory: the old file must equal `STUB_TEXT` (missing, edited or appended-to are all reported, with
  the command that adds an entry), `root/night_shift_history_archive` must not exist, and every entry in the
  directory (except `README.md`) must pass `entry_problems()`; a subdirectory, a symbolic link, a file that is not
  UTF-8 text or a name that is not an entry name is reported. Messages name the path from `root`. Reads the directory, never
  writes.
- **Special concerns:** reads every entry file (about 1,900 at the migration), well under a second; no locks.
- **See also:** `STUB_TEXT`, `entry_problems()`. **Tests:** `CheckTreeTests`, `CommandLineTests`.

### `[@ANCHOR: tools:night_shift_history_latest]` `latest_entries(root, count)`

- **Purpose:** the "what happened recently" read of the log, for an agent that starts work or writes a report.
- **Call interface:** `root` (`str`, repository root), `count` (`int`, at least 1). Returns `list[(str, str)]` of
  `(label, text)`, newest first: the last `count` file names of `night_shift_history/` that match `NAME_PATTERN` (label =
  the file name), or, in the old layout, the first `count` entries of `night_shift_history.md` (label = the file name and
  the entry number). Raises `ValueError` when `count < 1`, `FileNotFoundError` when there is neither layout.
- **Special concerns:** "newest" is the last name in string order, which is the newest date and time in the name; a
  migrated entry sorts by the date of its heading. Reads only the entries it returns.
- **Tests:** `LatestEntriesTests`.

### `[@ANCHOR: tools:night_shift_history_new_entry]` `new_entry(root, title, body, now=None, date=None)`

- **Purpose:** the one correct way to add an entry: it picks the name, writes the heading and refuses a malformed entry
  before anything is on disk.
- **Call interface:**

  | name | type | meaning |
  | --- | --- | --- |
  | `root` | `str` | repository root; `root/night_shift_history/` must exist |
  | `title` | `str` | heading text without the date; whitespace is collapsed to single spaces |
  | `body` | `str` | the text of the entry; leading and trailing blank lines are removed; must not be empty or contain a line that starts another entry |
  | `now` | `datetime` or `None` | the clock used for the file name; default the local time now (tests pass a fixed value) |
  | `date` | `str` or `None` | `YYYY-MM-DD` for the heading and the name instead of today |
  | returns | `str` | the path of the new file |

  Raises `FileNotFoundError` when the directory does not exist (old layout), `ValueError` (message from
  `entry_problems()`) when the entry would be malformed, `OSError` on a write failure. Writes with mode `x`, so it never
  replaces an existing file; it re-draws the random digits when the name is taken.
- **Special concerns:** touches nothing else; does not run git. Add the file with `git add night_shift_history/<name>`.
- **Example:** see the command line at the top of this section. **Tests:** `NewEntryTests`.

### `[@ANCHOR: tools:night_shift_history_main]` `main(argv=None)`

- **Purpose:** the command line: `latest [-n N] [--titles] [--root DIR]`, `new --title T [--body-file F] [--date D] [--root DIR]`
  (text from standard input without `--body-file`), `check [ROOT]`. `--root` and `ROOT` default to the git top level of the
  current directory, else the current directory.
- **Returns:** the exit status. 0 success; 1 `check` found problems (printed to standard error, one per line) or `latest`/`new`
  could not do the work (message on standard error); 2 bad usage (`argparse`). `new` prints the path of the file it made,
  relative to the root; `latest` prints each entry preceded by a comment line with its file name.
- **Tests:** `CommandLineTests`.

## `provision.py --test-environment`: a whole test host in one command

Bruce, 2026-11-06/07: one command that turns any test host (a LAN machine, a fresh Debian 13 VM, the lima VM, a Claude
Code cloud container) into a host where `test.py` and `run_linters.py` work, including the git repositories that are
missing and their push credentials. The code is `provision_test_environment.py`; `provision.py` parses the flags and
calls `start()` and `finish()`.

```bash
# from any checkout: hams_open/hams_shared, hams_com/tools, or a standalone hams_shared clone
sudo python3 hams_shared/tools/provision.py --test-environment             # cloud container, LAN host, VM
sudo python3 hams_shared/tools/provision.py --test --test-environment      # plus the full `--test` provisioning
python3 hams_shared/tools/provision.py --test-environment --plan           # print every action, change nothing
```

| Flag | Meaning |
|---|---|
| `--test-environment` | Run the steps below. Combines with `--test` (the full test provisioning runs between the clones and the test-runner steps) and `--plan`; refused with `--fetch-models`, `--only-host-class`, `--daemon-family`, `--units`. |
| `--git-key PATH` | An SSH deploy key, mode 600, used when the host has no cloud git proxy and no authenticated gh. Set as `core.sshCommand` on these clones only. Never opened, read, copied or printed. |
| `--workspace DIR` | The directory holding (or to hold) `hams_com` and `hams_open`. Default: see `discover_layout`. |
| `--no-self-check` | Skip the closing `run_linters.py --changed` and `test.py -u zero_sudo`. |

The steps, in order. Every step checks before it acts, so a second run changes nothing (the summary line says
`0 change(s)`); after a cloud container restart a run only restarts PostgreSQL.

1. **Production refusal** before the sudo re-exec (marker files) and again as root (the `hams_prod` database): exit 2,
   no override. The same markers as `test.py`. Then the run lock: one run per host at a time.
2. **Repositories**: clone the missing ones (`git clone --depth 1`, never `gh repo clone`, whose GraphQL a cloud
   proxy refuses), initialise the `hams_shared` submodule only when it is empty (then at hams_shared's `main`), run `fix_worktree_symlinks.py` in a
   hams_open worktree. An existing checkout is never checked out, reset or pulled (ADR 0102).
3. **Push credentials**, first that applies: the cloud git proxy; an authenticated `gh` (`gh auth setup-git`); the
   `--git-key`; otherwise read-only HTTPS, and the run says pushing is not set up.
4. **Cloud only**: `python3`, `pip3`, `pip` point at the distro's interpreter (apt's compiled modules need it).
5. **apt**: MANIFEST `apt_packages` (early_prod, minus odoo) plus `test_environment_apt_packages`, pgvector, each only
   when missing, through `_apt_install_missing` (simulation first; nothing removed, protected packages never
   upgraded). **pip**: `RUNTIME_PIP_PACKAGES`, `TEST_PIP_PACKAGES`, PyMuPDF 1.25 and mypy when absent.
6. **Odoo 19** as a Debian package: nightly.odoo.com where reachable, else built from github.com/odoo/odoo 19.0.
   Under systemd the `odoo` service is disabled: a test host never runs it.
7. **kopia** (pinned release), **cargo-deny**, the **Playwright** headless Chromium, **`npm ci`** in hams_shared.
8. **Services**: the MANIFEST test `/etc/hosts` service aliases (`redis`, `postgres`, ...) when missing; under systemd
   postgresql, redis-server and rabbitmq-server are enabled and started, nothing else; without systemd PostgreSQL
   with `pg_ctlcluster`, Redis and RabbitMQ started as their own users. No MANIFEST `external_fetch` unit is ever enabled or started by this path. PostgreSQL roles `odoo` and
   the runner's, an empty `hams_test` database.
9. **Profile** `/etc/profile.d/hams_test_environment.sh`: a random per-host `HAMS_CRYPTO_KEY`, `HAMS_BATCH_TREE_DIR`
   when the runner's home is closed to others, `HAMS_LINT_REMOTE=local` in a cloud session; sourced from `~/.bashrc`.
10. **Self-check**: `run_linters.py --changed` in a clean hams_com and `test.py -u zero_sudo -d hams_test`; exit 1
    when either fails. Skipped steps are listed with their reason.

The runner user is `SUDO_USER` (else `USER`, else root). Clones, `gh`, `cargo install`, `npm ci` and the self-check
run as that user (`runuser`), so the checkouts belong to them; apt, pip, Odoo, services and files under `/etc` run
as root. Not tested on a Debian 13 VM or the lima VM by the session that wrote it (2026-11-07, a cloud container);
the coordinator runs those.

### `[@ANCHOR: infrastructure:test_environment_apt_packages]` `MANIFEST["test_environment_apt_packages"]`

Purpose: the apt packages a test runner needs beyond MANIFEST `apt_packages`: socat (HamsHttpCase's TLS proxy),
iproute2, nodejs and npm, chromium and fonts, golang-go, the Rust crates' and the relay's build dependencies,
python3-lxml-html-clean and the daemons' test libraries. Kept out of `apt_packages` on purpose: `test.py`'s pre-flight
requires every `apt_packages` entry on every host (the dev box included), and production needs none of these.

| Field | Type | Meaning |
|---|---|---|
| `name` | str | The apt package name (Ubuntu, and Debian unless `debian_name` is given). |
| `debian_name` | str, optional | The name on Debian when it differs. None of the current entries needs one. |
| `why` | str | Non-empty: who needs it. Printed nowhere; read by people deciding whether an entry may go. |
| `skip_if_command` | str, optional | A command; when it is on PATH the package is not installed (the cloud image's `google-chrome`, Node 22 `node`/`npm`, `go`), so a distro package never shadows the tool the host already has. |

Invariants: no name also appears in `apt_packages` (`AptSelectionTests` checks); only `select_apt_packages()` reads
it; nothing writes it at run time. Adding an entry installs it on every `--test-environment` host. Replaces the lists
hand-installed by hams_com `devbox_tools/lima/provision_hams_test_vm.py` and `devbox_tools/cloud_session_setup.sh`.

### `[@ANCHOR: infrastructure:runtime_pip_packages]` `RUNTIME_PIP_PACKAGES`

Purpose: the pip distributions every provisioned host gets because no Debian package exists: pgeocode, telnetlib3,
mcp, adif-io, playwright (the ACMA scraper), curl_cffi (`smart_download`'s browser TLS fingerprint), google-cloud-kms
(the CA signer). A tuple of str at module level in `infrastructure.py`. `provision_environment()` installs it with
`pip3 install <names> --ignore-installed typing_extensions --break-system-packages` and then removes pip's
cryptography, cffi and pycparser so Debian's python3-cryptography wins; `provision_pip_packages()` does the same
when any one is missing. Read-only at run time. Tests: `PipTests`.

### `[@ANCHOR: infrastructure:test_pip_packages]` `TEST_PIP_PACKAGES`

Purpose: the test-only pip distributions, `("hypothesis", "pip-audit")`: hypothesis for the property-based tests,
pip-audit for `check_pip_audit`. Installed system-wide (`--ignore-installed --break-system-packages`) so the odoo
user test.py runs Odoo as sees them; by `provision_environment(is_test=True)` and by `provision_pip_packages()`.
Production never gets them. Tests: `PipTests`.

### `[@ANCHOR: provision_test_environment:repositories]` `REPOSITORIES`

A tuple of str, `("hams_open", "hams_com")`: the GitHub repositories (owner `GITHUB_OWNER`, `BrucePerens`) cloned
into the workspace under these names. hams_shared is not listed: it is hams_open's submodule, and hams_com tracks
`hams_shared` as the symlink `../hams_open/hams_shared`. Order matters only for the order of the clones. Read by
`missing_repositories()` and `provision_repositories()`. Tests: `RepositoryTests`.

### `[@ANCHOR: provision_test_environment:production_markers]` `PRODUCTION_MARKER_FILES`, `PRODUCTION_DATABASE_NAME`

`("/opt/hams/src/DEPLOY_LOG", "/opt/hams/src/DEPLOYED_COMMITS")` (written by deploy_to_production.py on the
production host only) and `"hams_prod"`. Equal to `test.py`'s `PRODUCTION_HOST_MARKER_FILES` and
`PRODUCTION_DATABASE_NAME`; `ProductionRefusalTests.test_the_markers_are_the_ones_test_py_refuses_on` fails when they
drift. Read at call time by `refuse_production()`.

### `[@ANCHOR: provision_test_environment:report]` `Report`

What one run did, printed as it happens and summarised at the end.

| Field / method | Type | Meaning |
|---|---|---|
| `Report(plan=False)` | constructor | `plan` True prints changes as `WOULD CHANGE`. |
| `changes` | list of str | Steps that changed the host. Empty on a second run: this is how idempotence is shown. |
| `skipped` | list of str | Steps left out, each with its reason. |
| `notes` | list of str | Everything else worth printing (workspace, runner, the push credential). |
| `changed(text)`, `skip(text)`, `note(text)` | method -> None | Append and print one `[test-environment] ...` line. |

Never holds a secret: callers pass the names of profile variables, never their values. Lives for one run. Tests:
`SmallPieceTests.test_report`.

### `[@ANCHOR: provision_test_environment:production_refusal_reasons]` `production_refusal_reasons(marker_files=None, database_names=())`

Purpose: why this host looks like production; `[]` means it does not.

| Argument | Type | Meaning |
|---|---|---|
| `marker_files` | iterable of str, or None | Paths whose existence marks production. None reads `PRODUCTION_MARKER_FILES` now. |
| `database_names` | iterable of str | The databases on the local PostgreSQL (`list_database_names()`). |

Returns a list of str, one sentence per signal. Raises nothing. Only `os.path.exists`; no other I/O. Tests:
`ProductionRefusalTests`.

### `[@ANCHOR: provision_test_environment:refuse_production]` `refuse_production(marker_files=None, database_names=())`

Purpose: stops the run before anything is changed on a production host. Arguments as
`production_refusal_reasons()`. Returns None when the host is not production; otherwise prints every reason and
raises `SystemExit(2)`. There is no override. `provision.py` calls it with no arguments before the sudo re-exec
(marker files only, no root needed), and `start()` calls it again as root with the database names. Tests:
`ProductionRefusalTests`, `ProvisionPyTestEnvironmentFlagTests.test_production_refusal_happens_before_the_sudo_reexec`.

### `[@ANCHOR: provision_test_environment:list_database_names]` `list_database_names(socket_dir="/var/run/postgresql", runner=subprocess.run)`

Purpose: the database names for the production check.

| Argument | Type | Meaning |
|---|---|---|
| `socket_dir` | str | Where the local server's `.s.PGSQL.<port>` sockets live. |
| `runner` | callable like `subprocess.run` | Injected by tests. |

Returns a tuple of str; `()` when no socket exists (no server running, so there is nothing to protect yet). When a
server is running, runs `runuser -u postgres -- psql ... SELECT datname FROM pg_database` (read-only, as root); if that
fails it prints the error and raises `SystemExit(2)`, so the check is never silently skipped. Tests:
`ListDatabaseNamesTests`.

### `[@ANCHOR: provision_test_environment:discover_layout]` `discover_layout(tools_dir, repo_root, workspace=None)`

Purpose: where the checkouts are or will be.

| Argument | Type | Meaning |
|---|---|---|
| `tools_dir` | str | The directory of the running provision.py. Used only when `repo_root` is None. |
| `repo_root` | str or None | `provision._find_repo_root()`: hams_open, hams_com, or the directory holding a standalone hams_shared clone. |
| `workspace` | str or None | `--workspace`; wins when given. |

Returns a dict `{workspace, hams_open, hams_com, hams_shared}` of absolute paths. The workspace is `workspace`, else
the parent of `repo_root` when `repo_root` is a git checkout, else `repo_root` itself (it holds a standalone clone),
else the parent of the clone holding `tools_dir`. `hams_shared` is always `<hams_open>/hams_shared`. Pure apart from
one `os.path.exists`. Tests: `LayoutTests`.

### `[@ANCHOR: provision_test_environment:check_git_key]` `check_git_key(path)`

Purpose: validates the `--git-key` file without touching its contents. `path` (str) may be relative. Returns the
absolute path. Raises `ValueError` when it does not exist, is not a regular file (a directory, a device), or its mode
is not exactly 600. Security: only `os.stat()` is called; the key is never opened, read, copied, logged or printed
(`test_the_key_is_never_opened` fails if it is opened). Tests: `CredentialTests`.

### `[@ANCHOR: provision_test_environment:choose_credentials]` `choose_credentials(environ, gh_authenticated, git_key=None)`

Purpose: picks the push credential, in the order Bruce set: what the host already has, never a new one.

| Argument | Type | Meaning |
|---|---|---|
| `environ` | mapping | The environment; `CLAUDE_CODE_REMOTE == "true"` marks a cloud session (its git proxy authenticates pushes). |
| `gh_authenticated` | bool | `gh_authenticated()` for the runner user. |
| `git_key` | str or None | `--git-key`. |

Returns a dict: `method` (`"cloud_proxy"`, `"gh"`, `"ssh_key"`, `"read_only"`), `url_format` (str with `{owner}` and
`{repo}`: HTTPS, or `git@github.com:{owner}/{repo}.git` for a key), `ssh_command` (`ssh -i <quoted key> -o
IdentitiesOnly=yes`, or None) and `note` (the line printed; for read-only it says pushing is not set up). Raises
`ValueError` from `check_git_key()` when the key is chosen and invalid. Pure otherwise. Tests: `CredentialTests`.

### `[@ANCHOR: provision_test_environment:gh_authenticated]` `gh_authenticated(as_user, runner=subprocess.run)`

Purpose: whether the runner user's gh can authenticate. `as_user` is `as_user_command()`'s wrapper. Returns False
when `gh` is not on PATH, else whether `gh auth status` exits 0. Read-only; prints nothing (its output is captured).
Tests: `GhAuthenticatedTests`.

### `[@ANCHOR: provision_test_environment:missing_repositories]` `missing_repositories(layout)`

Purpose: which `REPOSITORIES` need a clone. `layout` from `discover_layout()`. Returns a list of `(name, path)`
whose `<path>/.git` does not exist (an empty directory counts as missing). Raises `ValueError` when a path exists, is
not a checkout and is not empty: somebody's files are never overwritten. Read-only. Tests: `RepositoryTests`.

### `[@ANCHOR: provision_test_environment:retry]` `retry(action, tries=3, delay=30, sleep=time.sleep)`

Purpose: retries a network step (a clone through a flaky proxy). Calls `action()` (no arguments) until it returns,
at most `tries` times, sleeping `delay` seconds (int) between attempts, and returns its result. Only
`subprocess.CalledProcessError` is retried; after the last attempt it is re-raised, so a real failure still fails
the run. Blocks for up to `(tries - 1) * delay` seconds. Tests: `RetryTests`.

### `[@ANCHOR: provision_test_environment:provision_repositories]` `provision_repositories(layout, credentials, run, as_user, report, plan=False)`

Purpose: step 2 and 3 of the list above.

| Argument | Type | Meaning |
|---|---|---|
| `layout` | dict | `discover_layout()`. |
| `credentials` | dict | `choose_credentials()`. |
| `run` | callable(argv, **kw) | provision.py's `run_sys` (raises `CalledProcessError` on failure; prints only under `--plan`). |
| `as_user` | callable(argv) -> argv | `as_user_command()`: clones and gh run as the runner user. |
| `report` | `Report` | Changes are recorded here. |
| `plan` | bool | Print the clones instead of running them. |

For `gh`, runs `gh auth setup-git` first. Each missing repository is cloned shallow into `<path>.partial` (a stale
one is removed first) and renamed, so an interrupted clone never looks finished; clones are retried (`retry()`). For
an SSH key, `core.sshCommand` is set in each checkout's own `.git/config` (never globally). The submodule is
initialised (`git submodule update --init --depth 1`) only when `<hams_shared>/tools/test.py` is missing, and the
checkout it just made is moved to hams_shared's `main` (`fetch --depth 1 origin main`, `checkout --detach FETCH_HEAD`):
hams_open's pin can lag main, and `run_linters.py` refuses a hams_shared behind `origin/main`. In a hams_open worktree
`fix_worktree_symlinks.py` runs instead. Never checks out, resets, pulls or edits an existing
working tree. Returns None. Raises `ValueError` (occupied path) or `CalledProcessError`. Tests: `RepositoryTests`
(real clones from local origins, including an existing dirty checkout left untouched and a second run with no
changes).

### `[@ANCHOR: provision_test_environment:_set_git_config]` `_set_git_config(repo, key, value, run, as_user, report, plan)`

Sets `key` to `value` (str) in `repo`'s local git config through `run`, as the runner user, only when it differs;
records the change. A path that is not a checkout is skipped (under `--plan` the command is printed). The value is
printed by `run_sys` as part of the command: callers pass only non-secret values (the SSH command names the key's
path, never its contents). Tests: `RepositoryTests.test_ssh_key_is_scoped_to_these_clones_as_core_ssh_command`,
`PrivateHelperTests`.

### `[@ANCHOR: provision_test_environment:select_apt_packages]` `select_apt_packages(manifest, os_id, have_command)`

Purpose: the apt package list for this host.

| Argument | Type | Meaning |
|---|---|---|
| `manifest` | dict | `infrastructure.MANIFEST` (only `apt_packages` and `test_environment_apt_packages` are read). |
| `os_id` | str | `get_os_identifier()`; `"debian"` uses `debian_name` where given. |
| `have_command` | callable(str) -> bool | Whether a command is on PATH. |

Returns `(wanted, skipped)`: `wanted` the sorted list of names (`apt_packages` entries for `early_prod` except `odoo`,
plus every `test_environment_apt_packages` entry whose `skip_if_command` is absent), `skipped` a dict name -> reason.
Pure. Tests: `AptSelectionTests`.

### `[@ANCHOR: provision_test_environment:_apt_candidate]` `_apt_candidate(pkg)`

Whether `apt-cache policy <pkg>` shows a candidate (anything but `(none)`). Read-only. Returns bool. Tests:
`PrivateHelperTests`.

### `[@ANCHOR: provision_test_environment:_apt_update_if_stale]` `_apt_update_if_stale(run, report, plan, now=None)`

Runs `apt-get update -q` (retried) unless `APT_UPDATE_STAMP` (`/var/lib/hams-test-environment.apt-updated`) is younger
than `APT_UPDATE_MAX_AGE` (12 hours), then touches the stamp (not under `--plan`). `now` (seconds since the epoch)
is for tests. The update is a note, not a change: it changes no installed package. Tests: `PrivateHelperTests`.

### `[@ANCHOR: provision_test_environment:_install_apt]` `_install_apt(run, packages, label, report)`

Installs the missing `packages` (iterable of str) through `infrastructure._apt_install_missing()` with
`-q --no-install-recommends` (simulation first; refuses removals and protected upgrades; never upgrades). A refusal
there is recorded as a hook failure, which this function turns into `RuntimeError`: a test host with half its
packages is not a test host. Records the installed names. Tests: `AptProvisionTests`, `PrivateHelperTests`.

### `[@ANCHOR: provision_test_environment:provision_apt_packages]` `provision_apt_packages(run, os_id, report, plan=False)`

Purpose: step 5's apt part. `_apt_update_if_stale()`, then `select_apt_packages()`; a name with no candidate on this
release is reported (and, when it is a `PIP_FALLBACK_APT_PACKAGES` key, left to pip) instead of failing the whole
install; then `_install_apt()` for the rest and for `postgresql-<newest installed major>-pgvector`. Returns the list
of names that had no candidate (for `provision_pip_packages()`). Raises `RuntimeError` on an apt refusal,
`CalledProcessError` from apt. Tests: `AptProvisionTests`.

### `[@ANCHOR: provision_test_environment:missing_pip_distributions]` `missing_pip_distributions(names, installed)`

Returns the members of `names` (iterable of str) for which `installed(name)` is False, in order. Pure. Tests:
`PipTests`.

### `[@ANCHOR: provision_test_environment:_pip_installed]` `_pip_installed(python)`

Returns a probe `name -> bool`: whether `importlib.metadata.version(name)` succeeds in the interpreter `python` (a
path), run as a subprocess so the answer is the system interpreter's, not this process's. Read-only. Tests:
`PrivateHelperTests`.

### `[@ANCHOR: provision_test_environment:provision_pip_packages]` `provision_pip_packages(run, no_apt_candidate, report, python="/usr/bin/python3")`

Purpose: step 5's pip part, as `provision_environment()` installs it. `no_apt_candidate` is
`provision_apt_packages()`'s result. Each group only when one of its members is missing: the pip fallbacks of
`no_apt_candidate`; `RUNTIME_PIP_PACKAGES` (then pip's cryptography, cffi and pycparser are removed so Debian's
python3-cryptography wins); `TEST_PIP_PACKAGES`; `pymupdf>=1.25,<1.26` (`--no-deps`) when `import pymupdf` fails
(Ubuntu 24.04's python3-fitz has no such module; Debian 13 ships 1.25); `mypy` when `python -m mypy` fails. All with
`--break-system-packages`, system-wide, so the odoo user sees them. Raises `CalledProcessError` from pip. Tests:
`PipTests`.

### `[@ANCHOR: provision_test_environment:distro_default_python]` `distro_default_python(debian_defaults_text)`

Returns the interpreter name (`"python3.12"`) on the `default-version = ...` line of the text of Debian's
`/usr/share/python3/debian_defaults`. Raises `ValueError` without one. Pure. (`py3versions -d` reads the same file but
is a python3 script and fails while python3 points elsewhere.) Tests: `CloudPythonTests`.

### `[@ANCHOR: provision_test_environment:cloud_python_links]` `cloud_python_links(distro_python)`

Returns the list of `(link, target)` that make `python3`, `pip3` and `pip` the distro's: `/usr/bin/python3 ->
<python3.N>`, `/usr/local/bin/python3 -> /usr/bin/<python3.N>`, `/usr/local/bin/pip3` and `/usr/local/bin/pip ->
/usr/bin/pip3`. `distro_python` is a name or path. Pure. Tests: `CloudPythonTests`.

### `[@ANCHOR: provision_test_environment:provision_cloud_python]` `provision_cloud_python(environ, run, report, plan=False)`

Purpose: step 4. Does nothing unless `environ["CLAUDE_CODE_REMOTE"] == "true"`: the dev box, LAN hosts and
production keep their python3 (this is why the old bootstrap refused to run outside the cloud). The cloud image
points python3 at 3.13 while apt's compiled python3-* modules and test.py's Odoo need the distro's 3.12. Reads
`distro_default_python()`, then for each `cloud_python_links()` entry that points elsewhere runs `ln -sfn` (and, for
`/usr/bin/python3` managed by update-alternatives, `update-alternatives --remove-all python3` first). A
`/usr/local/bin` link that does not exist is not created. Raises `RuntimeError` when the default interpreter is not
installed. The running provision.py keeps the interpreter it started with. Tests: `CloudPythonTests`.

### `[@ANCHOR: provision_test_environment:odoo_changelog_entry]` `odoo_changelog_entry(sha, day, date_rfc2822, branch="19.0")`

Returns the `debian/changelog` stanza (str, ending in a blank line) for an Odoo package built from source: version
`19.0.0~git<day>.<sha>-1`, which sorts below every nightly `19.0.<date>` release, so a later apt upgrade replaces
it. `sha` is 12 hex digits, `day` `YYYYMMDD`, `date_rfc2822` from `date -R`. Pure. Tests: `OdooTests`
(`dpkg --compare-versions` proves the ordering).

### `[@ANCHOR: provision_test_environment:url_reachable]` `url_reachable(url, opener=urllib.request.urlopen, timeout=15)`

Whether `url` (str) answers a HEAD request with 2xx or 3xx within `timeout` seconds. Returns False on any `URLError`
or `OSError`: a cloud session's proxy answers a refused host with 403, which is one. `opener` is injected by tests.
Blocks up to `timeout`. Used for nightly.odoo.com (`ODOO_KEY_URL`) and the Playwright CDN (`PLAYWRIGHT_CDN_URL`).
Tests: `OdooTests`.

### `[@ANCHOR: provision_test_environment:acquire_lock]` `acquire_lock(path=LOCK_PATH, wait=True)`

Purpose: only one `--test-environment` run per host. Two at once raced each other's Odoo package build in the same
`/opt/odoo19` (found 2026-11-07, the run that wrote this). Opens `path` (`/var/lib/hams-test-environment.lock`,
created mode 600) and takes an exclusive `flock`. Returns the open file object; the caller keeps it referenced
(`start()` stores it in the state as `lock`), and the lock is released when the process exits, so a killed run never
leaves it held. When another run holds it: `wait` True prints a waiting line and blocks until it is free; `wait` False
closes the file and raises `RuntimeError`. Not taken under `--plan`, which changes nothing. Tests: `LockTests`.

### `[@ANCHOR: provision_test_environment:provision_odoo]` `provision_odoo(run, report, systemd, plan=False, reachable=<url_reachable(ODOO_KEY_URL)>)`

Purpose: step 6. Nothing when dpkg has `odoo`. Otherwise, when `reachable()`: the key (through `provision_cache`,
trust on first use, as provisioning) and the MANIFEST's `/etc/apt/sources.list.d/odoo.list`, `apt-get update`,
`apt-get install odoo` (the dev box's package). When not: `_build_odoo_from_github()`. `systemd` (bool): the package
starts the `odoo` service; a test host never runs it, so it is disabled and stopped. Raises `CalledProcessError`.
Tests: `OdooTests`.

### `[@ANCHOR: provision_test_environment:_build_odoo_from_github]` `_build_odoo_from_github(run, report, plan)`

Installs `ODOO_BUILD_APT_PACKAGES` (debhelper, dh-python, rsync, python3-setuptools, git), shallow-clones
github.com/odoo/odoo `ODOO_BRANCH` into `ODOO_DIR/src` (`/opt/odoo19/src`) unless it is there, and, unless an
`odoo_*_all.deb` already exists in `ODOO_DIR`, restores `debian/changelog`, prepends `odoo_changelog_entry()` and runs
`dpkg-buildpackage -b -us -uc -d` (several minutes; output in `ODOO_DIR/build.log`). Then `apt-get install` of the
newest `.deb`, which pulls Odoo's dependencies from the distro. The same source and packaging as the nightly, built
locally because the nightly host is unreachable. Raises `CalledProcessError`. Tests: `PrivateHelperTests`.

### `[@ANCHOR: provision_test_environment:provision_kopia]` `provision_kopia(run, report, plan=False)`

Purpose: the b2_backup tests need `kopia`, which provisioning installs on production only. When none is on PATH:
fetches the MANIFEST's pinned release for this CPU (`_kopia_release_arch`) through `provision_cache.fetch()` with the
pinned SHA-256 (a different file is refused), copies it to `/tmp/hams-kopia.tar.gz` and runs
`hook_install_kopia_binary` (`/usr/bin/kopia`). Network: GitHub release assets. Raises `provision_cache.HashRejected` or
`Unavailable`. Tests: `KopiaAndUserToolTests`.

### `[@ANCHOR: provision_test_environment:provision_user_tools]` `provision_user_tools(layout, run, as_user, home, report, reachable=url_reachable)`

Purpose: step 7, as the runner user. `cargo install --locked cargo-deny` when cargo exists and `cargo deny --version`
fails (reported as skipped when there is no cargo); the Playwright headless Chromium (`playwright_test_browser_commands`:
`install-deps` as root, `install` as the user without `PLAYWRIGHT_BROWSERS_PATH`) when `<home>/.cache/ms-playwright`
has no `chromium_headless_shell-*`, reported as a skipped step (with the tests that then fail) when
`reachable(PLAYWRIGHT_CDN_URL)` is False, as in a cloud session; `npm ci --no-audit --no-fund` in hams_shared when its `node_modules/.bin/eslint`
is missing. `home` (str) is the runner's home. Network: crates.io, the Playwright CDN, npm. Raises
`CalledProcessError`. Tests: `KopiaAndUserToolTests`.

### `[@ANCHOR: provision_test_environment:provision_ml_assets]` `provision_ml_assets(layout, run, report, plan=False, reachable=url_reachable, manifest_exists=os.path.isfile)`

Purpose: step 6, the browser ML assets `ham_shack/controllers/offline.py` serves from `/var/lib/odoo/hams_ml_assets`, which
production provisions by hand and a test host never had (`TestMlAssetsRoute` answered 404 for `manifest.json`). Does nothing
when the root has a `manifest.json` (the script writes it last, so an interrupted run is repeated). Otherwise creates the root
owned by `odoo`, runs `<hams_com>/ham_shack/tools/provision_ml_assets.py` as `odoo` under `nice -n 10`, then its
`--verify-relay-pins`: the real files, no test-only route or placeholder. A missing script, or an unreachable npm or HuggingFace,
is a reported skip. `plan` prints and changes nothing. Network: registry.npmjs.org and huggingface.co, about 1.8 GB the first
time. Raises `CalledProcessError`. Tests: `MlAssetsTests`.

### `[@ANCHOR: provision_test_environment:offline_clusters]` `offline_clusters(pg_lsclusters_output)`

Returns `[(version, cluster)]` (str, str) of the lines of `pg_lsclusters -h` whose status column is not `online`.
Pure. Tests: `ServiceAndDatabaseTests`.

### `[@ANCHOR: provision_test_environment:wait_until]` `wait_until(probe, timeout, interval=2, sleep=time.sleep, clock=time.monotonic)`

Calls `probe()` (no arguments, returns bool) until it returns True, sleeping `interval` seconds between calls, for at
most `timeout` seconds by `clock()`. Returns True as soon as the probe does, False when the time is up (the caller
decides whether that is fatal; `provision_services()` raises). Blocks. `sleep` and `clock` are injected by tests.
Tests: `ServiceAndDatabaseTests.test_wait_until`.

### `[@ANCHOR: provision_test_environment:host_alias_line]` `host_alias_line(manifest)`

Returns the `127.0.0.1 postgres redis rabbitmq odoo powerdns daemon_dx_firehose` line of the MANIFEST static file
`/etc/hosts` for the `test` environment: the service names the test suites connect to (the gdpr_csv_export tests
reach Redis as `redis`). Raises `StopIteration` when there is no such template and `ValueError` when it has no
`127.0.0.1` line naming `redis`. Pure. Tests: `ServiceAndDatabaseTests.test_host_aliases`.

### `[@ANCHOR: provision_test_environment:missing_host_aliases]` `missing_host_aliases(hosts_text, alias_line)`

Returns the names of `alias_line` (after its address) that no `127.0.0.1` line of `hosts_text` lists, in order.
Comments after `#` are ignored. Pure. Tests: `ServiceAndDatabaseTests.test_host_aliases`.

### `[@ANCHOR: provision_test_environment:provision_host_aliases]` `provision_host_aliases(report, plan=False, path="/etc/hosts")`

Purpose: the service aliases every provisioned test host has (provisioning writes the whole MANIFEST `/etc/hosts`
template; a cloud container had none, so 4 gdpr_csv_export tests failed with "Name or service not known" for
`redis`). Appends one line `127.0.0.1 <the missing names>` when any is missing; never rewrites or reorders the file,
which on a cloud container carries its own entries (and is reset on a container restart, when the next run adds the
line again). Under `--plan` it prints the line. Raises `OSError` when the file cannot be read or written. Tests:
`ServiceAndDatabaseTests.test_host_aliases_are_appended_once_and_nothing_is_rewritten`.

### `[@ANCHOR: provision_test_environment:provision_services]` `provision_services(run, report, systemd, plan=False)`

Purpose: step 8's services. With `systemd` True: `systemctl enable --now` each of postgresql, redis-server,
rabbitmq-server that is not both active and enabled, and nothing else, so no external-fetch unit (the MANIFEST's
`external_fetch` rule) is ever enabled or started here. With False (a container without systemd):
`pg_ctlcluster <v> <name> start` for each `offline_clusters()`; `redis-server /etc/redis/redis.conf --daemonize yes`
as the `redis` user when `redis-cli ping` does not answer PONG; `rabbitmq-server -detached` as the `rabbitmq` user
when `rabbitmqctl -q status` fails, then `wait_until()` `rabbitmqctl await_startup` succeeds (120 s, else
`RuntimeError`; `-detached` returns before the node registers). The daemon test suites `run_linters.py` runs need
host Redis and RabbitMQ; test.py starts its own in its namespace. Restart safety: after a container restart these
service starts (and the host aliases) are the only changes a run makes. Raises `CalledProcessError`. Tests:
`ServiceAndDatabaseTests`.

### `[@ANCHOR: provision_test_environment:role_sql]` `role_sql(runner_user)`

Returns the SQL (one `DO $$ ... $$` block) that creates, when missing, the role `odoo` (SUPERUSER LOGIN, password
`odoo`, as test.py's namespace provisioning does; a test-only password) and the runner's role (SUPERUSER LOGIN, no
password; peer authentication). No runner role for `odoo` or `postgres`. Raises `ValueError` when `runner_user` is
not a plain lower-case identifier (`[a-z_][a-z0-9_-]*`): it is interpolated into SQL. Pure. Tests:
`ServiceAndDatabaseTests`.

### `[@ANCHOR: provision_test_environment:_psql]` `_psql(sql)`

Returns the argv that runs `sql` (str) with psql as the postgres OS user against the `postgres` database
(`-v ON_ERROR_STOP=1 -At -c`). Pure. Tests: `PrivateHelperTests`.

### `[@ANCHOR: provision_test_environment:provision_database]` `provision_database(run, runner_user, report)`

Purpose: step 8's database. Lists the roles and databases (read-only psql as postgres); runs `role_sql()` when `odoo`
or the runner's role is missing, and `createdb -O odoo hams_test` when `hams_test` is missing. test.py drops and
recreates `hams_test` on every run; the empty one only proves the server and roles work. Needs PostgreSQL running
(`provision_services()` first). Raises `CalledProcessError`, `ValueError` (from `role_sql`). Tests:
`ServiceAndDatabaseTests`.

### `[@ANCHOR: provision_test_environment:provision_test_host_requirements]` `provision_test_host_requirements(run, report, plan=False)`

Purpose: step 8, what `provision.py --test` provides and a test run inside `sudo -u odoo env HAMS_ISOLATED_NS=1` does not (that
invocation skips the namespaced provisioning). It calls the production functions unchanged: `provision_system_accounts()`
and `apply_production_directories()` for `prod` and `test` (the hamsd_* accounts; `/var/lib/odoo/backups` and the key
directories), `_provision_cache_manager_role()` for `hams_test` (the `cache_manager_ro` role and
`/opt/hams/etc/keys/cache_manager/cache_manager_db.env`, whose absence made tests fail with `DB_USER and DB_PASS must
both be set`), `_allow_cache_manager_socket_password_for_test()` followed by `pg_reload_conf()` when it changed a
pg_hba.conf, and `git config --system --add safe.directory '*'` when git does not already trust every directory (the
odoo user does not own the checkouts). Compares `host_requirements.missing_requirements(check_crypto_key=False)` before
and after: the codes missing before are the one `Report.changed` line, and anything still missing afterwards raises
`RuntimeError`. The profile's HAMS_CRYPTO_KEY is not checked: it reaches test.py through `sudo -u odoo env`. Under `plan`
it prints the account and directory plans and touches neither PostgreSQL nor git. Needs `provision_database()` first.
Idempotent. Tests: `TestHostRequirementsTests`.

### `[@ANCHOR: provision_test_environment:parse_profile]` `parse_profile(text)`

Returns `{name: value}` for each `export NAME=value` line of a profile written by `profile_text()` (the value is
shell-unquoted with `shlex`). Other lines are ignored. Pure. Tests: `ProfileTests`.

### `[@ANCHOR: provision_test_environment:profile_text]` `profile_text(values)`

Returns the text of `/etc/profile.d/hams_test_environment.sh`: two comment lines (written by whom; the key is a
throwaway test key) and one `export NAME=<shlex-quoted value>` per item of `values` (dict str -> str), sorted by name,
so the same values always give the same bytes (which is what makes a second run a no-op). Pure. Tests:
`ProfileTests`.

### `[@ANCHOR: provision_test_environment:profile_values]` `profile_values(existing, environ, layout, home_mode)`

Purpose: the variables a test host's shells need.

| Argument | Type | Meaning |
|---|---|---|
| `existing` | dict | `parse_profile()` of the current file (`{}` on a first run). |
| `environ` | mapping | The environment (`CLAUDE_CODE_REMOTE`). |
| `layout` | dict | `discover_layout()`; only `workspace` is read. |
| `home_mode` | int | Permission bits of the runner's home. |

Returns a dict: `HAMS_CRYPTO_KEY` (the existing one, so a re-run never changes it; else
`base64(secrets.token_bytes(32))`, the format provisioning generates; a per-host key for throwaway test databases,
never a real secret, which test.py's provisioning prefers to its all-zero default); `HAMS_BATCH_TREE_DIR =
<workspace>/workspace/tmp` when `home_mode` lacks o+x (test.py's batch tree under `~/workspace/tmp` would be unreadable
to the odoo user: "Permission denied" opening tools/test.py); `HAMS_LINT_REMOTE=local` in a cloud session (the lint
machines are on Bruce's LAN). Uses the OS random source. Tests: `ProfileTests`.

### `[@ANCHOR: provision_test_environment:provision_profile]` `provision_profile(layout, environ, home, runner_user, report, plan=False, path=PROFILE_PATH)`

Purpose: step 9. Reads `path`, computes `profile_values()`, writes `path` (mode 644) only when the text changes,
creates `HAMS_BATCH_TREE_DIR` (and its parent) mode 755 owned by `runner_user` when set and missing, and appends
`[ -f <path> ] && . <path>` to `<home>/.bashrc` once (non-login shells do not read `/etc/profile.d`). Returns the
values, which `finish()` adds to the self-check's environment. Security: the key's value is never printed or logged
(the report names the variables only); the file is world-readable because every shell on a test host needs it and the
key protects nothing real. Under `--plan` it only prints the variable names. Tests: `ProfileTests`.

### `[@ANCHOR: provision_test_environment:self_check_commands]` `self_check_commands(layout, hams_com_clean)`

Returns `(commands, skipped)`: `commands` a list of `(label, argv, cwd)`, `run_linters.py --changed` in hams_com when
`hams_com_clean` (bool) and always `hams_shared/tools/test.py -u zero_sudo -d hams_test` in hams_com; `skipped` the
reasons for what was left out (a dirty tree would lint someone's work in progress). Pure. Tests: `SelfCheckTests`.

### `[@ANCHOR: provision_test_environment:run_self_check]` `run_self_check(layout, env, as_user, report, runner=subprocess.run)`

Purpose: step 10. Asks `git status --porcelain` whether hams_com is clean, then runs `self_check_commands()` as the
runner user with `env` (dict: the run's environment plus the profile values and the runner's HOME). Returns the
labels that failed (list of str). Blocks for the length of both runs: minutes for test.py, longer when the linters
have much to do. The linters follow the profile's `HAMS_LINT_REMOTE`; test.py takes its own locks. Tests:
`SelfCheckTests`.

### `[@ANCHOR: provision_test_environment:as_user_command]` `as_user_command(user, home)`

Returns a function `argv -> argv` that prefixes `runuser -u <user> -- env HOME=<home>` when this process is root
and `user` is set and not root, and returns a copy of `argv` otherwise. Used so clones, gh, cargo, npm and the
self-check belong to the person who ran `sudo provision.py`, not to root. Pure (reads `os.geteuid()` at call time).
Tests: `SmallPieceTests`.

### `[@ANCHOR: provision_test_environment:start]` `start(run, env_vars, orig_user, tools_dir, repo_root, workspace=None, git_key=None, plan=False)`

Purpose: the part of `--test-environment` that runs before provision.py's own `--test` provisioning: the production
refusal with the database check (exit 2), the run lock (`acquire_lock()`, not under `--plan`), the layout, the runner user (`orig_user`, else root) and its home, whether
systemd runs (`/run/systemd/system`), the credential, the clones. `env_vars` is provision.py's copy of the
environment. Returns the state dict for `finish()`: `report`, `layout`, `runner_user`, `home`, `as_user`, `systemd`,
`lock` (the open lock file, or None under `--plan`).
Raises `ValueError` (bad key, occupied path; provision.py prints it and exits 1) and `CalledProcessError`. Tests:
`StartFinishTests`.

### `[@ANCHOR: provision_test_environment:finish]` `finish(state, run, env_vars, os_id, self_check=True, plan=False)`

Purpose: steps 4 to 10 (step 8 is `provision_test_host_requirements()`), in that order, on `start()`'s state. `os_id` is `"debian"` or `"ubuntu"`. Under `plan` the
database and the self-check are not run and every change is printed as a PLAN line (provision.py wraps the call in
`infrastructure.planning()`). Prints `<n> change(s) to this host; <m> step(s) skipped` and the skipped steps. Returns
the `Report`; raises `SystemExit(1)` when the self-check fails. Tests: `StartFinishTests`.

### `[@ANCHOR: host_requirements:constants]` `CRYPTO_KEY_VARIABLE`, `CACHE_MANAGER_LOGIN_FILE`, `BACKUPS_DIRECTORY`, `SERVICE_HOST_NAME`, `FIX_COMMAND`, `PROFILE_FILE`

Module `host_requirements.py`: what a test run inside `sudo -u odoo env HAMS_ISOLATED_NS=1` needs from the host.

| Name | Value | Meaning |
|---|---|---|
| `CRYPTO_KEY_VARIABLE` | `"HAMS_CRYPTO_KEY"` | The variable that must reach test.py through `sudo -u odoo env`. |
| `CACHE_MANAGER_LOGIN_FILE` | `/opt/hams/etc/keys/cache_manager/cache_manager_db.env` | Holds DB_USER and DB_PASS for cache_manager.py; `_provision_cache_manager_role()` writes it. |
| `BACKUPS_DIRECTORY` | `/var/lib/odoo/backups` | A MANIFEST directory the backup tests use. |
| `SERVICE_HOST_NAME` | `"redis"` | Must resolve (`/etc/hosts`: `127.0.0.1 redis`). |
| `FIX_COMMAND` | `sudo python3 hams_shared/tools/provision.py --test-environment` | What the refusal tells the operator to run. |
| `PROFILE_FILE` | `/etc/profile.d/hams_test_environment.sh` | Where the per-host HAMS_CRYPTO_KEY is written. |

Read at call time by the functions below; never written. Tests: `MissingRequirementsTests`.

### `[@ANCHOR: host_requirements:test_account_names]` `test_account_names(manifest=None)`

Purpose: the system accounts a test host has: the MANIFEST `system_accounts` entries for the `test` environment (an
entry with no `environments` counts) that `infrastructure._spec_selected()` accepts for this host's classes. Returns a
list of str, in MANIFEST order. Pure apart from reading the host classes. Tests: `TestAccountNamesTests`.

### `[@ANCHOR: host_requirements:missing_requirements]` `missing_requirements(environ=None, check_crypto_key=True, ...probes)`

Purpose: `[(code, text)]` of what this host lacks, in a fixed order, `[]` when it lacks nothing. Codes: `crypto_key`
(`HAMS_CRYPTO_KEY` absent or empty in `environ`, default `os.environ`; skipped when `check_crypto_key` is False),
`accounts` (names from `test_account_names()` that `pwd` does not know; the text lists only the missing ones),
`backups_directory`, `cache_manager_login` (the file is unreadable by this user, or has no non-empty DB_USER or DB_PASS),
`git_safe_directory` (`git config --get-all safe.directory` lacks `*`), `service_host` (`redis` does not resolve). The
keyword arguments `account_exists`, `is_directory`, `login_keys`, `git_trusts_all`, `resolves` and `account_names` are the
probes, replaced in tests. Reads only; runs one `git config` subprocess. Tests: `MissingRequirementsTests`.

### `[@ANCHOR: host_requirements:refusal_text]` `refusal_text(missing)`

Purpose: the message for a non-empty `missing`: a first line, one `   - <text>` line per item, then `NEXT:` lines.
Anything but `crypto_key` gets the `FIX_COMMAND` line; `crypto_key` gets the `source /etc/profile.d/...` line with the
`sudo -n -u odoo env HAMS_ISOLATED_NS=1 HAMS_CRYPTO_KEY="$HAMS_CRYPTO_KEY" GIT_DISCOVERY_ACROSS_FILESYSTEM=1 ...` form
(sudo's `env_reset` drops variables placed before it). Pure. Tests: `RefusalTests`.

### `[@ANCHOR: host_requirements:require_test_host]` `require_test_host(environ=None, missing=None)`

Purpose: the first thing `test.py`'s `run_preflight()` does when `HAMS_ISOLATED_NS=1` (the namespaced path provisions these
itself and is not checked): prints `refusal_text()` to stderr and exits 1 when `missing_requirements(environ)` (or the
given `missing`) is not empty; returns None otherwise. Starts nothing and changes nothing, so it is safe before the
lock queue. Tests: `RefusalTests`, `TestPyWiringTests`.

### `[@ANCHOR: run_linters:eslint_node_env]` `eslint_node_env(base_env)`

Purpose: gives the ESLint step of `run_linters.py` a 4 GB node heap whatever the caller's shell exported. The type-aware
ESLint project needs more than node's default heap (about 2 GB); a `--changed` run died with "JavaScript heap out of
memory" on 2026-11-02. The first fix used `setdefault("NODE_OPTIONS", ...)`, which does nothing when the variable exists
but is an empty string, and the agents' shells exported it empty, so ESLint kept the small heap and failed again
(2026-11-03). The function replaces every "no real value" case, so nobody has to `unset NODE_OPTIONS` by hand any more.
Called once, where the ESLint step builds its environment; no other tool in `tools/` starts node with a heap option
(the small `node --check` and scanner helpers need none).

| Argument | Type | Meaning |
|---|---|---|
| `base_env` | mapping of str to str | The environment to start from, normally `os.environ`. Borrowed and never changed. |

Returns: a new `dict` copy of `base_env`. Its `NODE_OPTIONS` is `ESLINT_NODE_OPTIONS` when the variable was missing,
empty, or only whitespace; a non-empty value is returned as it was (a caller who set their own heap size or other
node options keeps them, and no option is merged into it). Every other variable is copied unchanged.

Errors: none of its own (a non-string `NODE_OPTIONS` value raises `AttributeError`; the environment never holds one).

Special concerns: pure, not blocking, no files, no network, no locks, idempotent (calling it on its own result changes
nothing). It handles no secret and reads nothing but its argument.

Data: `ESLINT_NODE_OPTIONS` (module constant, str, default `"--max-old-space-size=4096"`, V8 heap limit in megabytes) is
the only value the function inserts; it is read only by `eslint_node_env`. To raise the limit change that constant.

Example:

```python
env = eslint_node_env({"NODE_OPTIONS": "", "PATH": "/usr/bin"})
# env["NODE_OPTIONS"] == "--max-old-space-size=4096"
```

See also: tests `EslintNodeEnvTests` in `test_run_linters.py` (`# Tests [@ANCHOR: run_linters:eslint_node_env]`); the
out-of-memory cause from `daemons/*/target` is handled by `write_eslint_project()`, whose generated tsconfig now also carries
`exclude` globs (`<repo>/**/target/**`, `<repo>/**/node_modules/**`, absolute because tsconfig patterns are relative to the
generated file); test `test_the_generated_project_excludes_cargo_target_and_node_modules`.

## External programs of the relay are inventoried, pinned and watched (`check_external_programs_documented.py`, ADR 0114)

`check_external_programs_documented.py <repo_root>` is the linter step of the same name in `run_linters.py`. It reads the tables of
`docs/adrs/0114_*.md` and `tools/dependency_watch.json` and fails when the relay's source, its installers or the watch disagree with the ADR (the list
of checks is in the module docstring). No baseline file exists; findings go to zero and stay there. Exit 0 passes, 1 fails with one line per finding and a
`FIX:` line, 2 is a usage error or a missing ADR. A repository without `daemons/hams_local_relay` (hams_open) is checked only for the watch rules.
Limits: a program an installer starts without first testing for it (`cp`, `tar`, `curl`) is not scanned; a program started through a variable is covered
only through the discovery functions (`DISCOVERY_FUNCTIONS`, constant, dict of function name to relay-relative file). Tests: `test_check_external_programs_documented.py`.

### `[@ANCHOR: ext_programs:code_mask]` `code_mask(text)`

Purpose: lets the other functions look for braces and keywords in Rust source without being fooled by comments and literals. Argument `text` (str): Rust source,
borrowed. Returns a str of the same length in which comments (line, and nested block) and the inside of string, raw string and character literals are
replaced by spaces; newlines stay, so offsets and line numbers match `text`. A lifetime (`'a`) is not a character literal. Never raises; an unterminated
literal blanks to the end of the text. Pure, no files.

### `[@ANCHOR: ext_programs:test_spans]` `test_spans(masked)`

Purpose: finds the code that exists only in test builds. Argument `masked` (str): the result of `code_mask`. Returns a list of `(start, end)` offsets, one per
`#[cfg(test)]` (or `#[cfg(all(test, ...))]`) attribute: from the attribute to the end of the item after it (the closing brace of a module, function or
impl, or the `;` of `mod name;`), with other attributes between skipped. Never raises. Pure.

### `[@ANCHOR: ext_programs:rust_launches]` `rust_launches(text)`

Purpose: the programs a Rust file starts in production. Argument `text` (str): the file. Returns `(literal_names, dynamic_count)`: a `set` of the names in
`Command::new("name")` (also as `std::process::Command::new`) and the number of `Command::new(<expression>)` calls, ignoring comments and the spans from
`test_spans`. A name containing a backslash is not recognised. Pure, never raises.

### `[@ANCHOR: ext_programs:script_dependencies]` `script_dependencies(text, powershell=False)`

Purpose: the programs an installer or release script depends on. Arguments: `text` (str) the script; `powershell` (bool, default False) selects
`Get-Command name` instead of `command -v name`. Returns a `set` of names; lines starting with `#` are ignored. Pure, never raises.

### `[@ANCHOR: ext_programs:adr_tables]` `adr_tables(adr_text)`

Purpose: reads the ADR's markdown tables. Argument `adr_text` (str). Returns a list of `(header_cells, rows)`; each row is a list of stripped cell strings;
separator rows (`|---|`) are dropped; a table ends at the first line that does not start with `|`. Pure.

### `[@ANCHOR: ext_programs:documented_names]` `documented_names(adr_text)`

Purpose: the set of program names the ADR names. Argument `adr_text` (str). Returns a `set` of lower-case str: each word inside backticks of any table cell
(split at whitespace, `/`, `,` and `;`, a trailing `.exe` removed) and each word of the first cell of every row (so `Pat` counts as `pat`). Pure.

### `[@ANCHOR: ext_programs:watch_findings]` `watch_findings(adr_text, watch)`

Purpose: keeps the ADR and the weekly watch (`check_dependency_releases.py`) in step. Arguments: `adr_text` (str); `watch` (dict) the parsed
`dependency_watch.json`. Returns a list of finding strings: a table row that has a `Watched` column and a backticked `owner/repo` in its first cell
needs that repo in `watch` and a `Watched` cell starting with `yes` (bold or underscore marks ignored); each `watch` entry with a `repo` must appear in the
ADR by repo or by its key as a documented name. Pure.

### `[@ANCHOR: ext_programs:find_findings]` `find_findings(repo_root, adr_path, watch_path)`

Purpose: the whole check. Arguments: `repo_root` (str) the checkout; `adr_path` (str) the ADR file; `watch_path` (str) the watch JSON. Returns a list of finding
strings, empty when all agree. Reads `src/**/*.rs` of the relay (not `vendor`, `target`, test files named `*_test(s).rs`, `tests.rs`, `test_*.rs`, or under a
`tests` directory), the relay's `install_*.sh`, `packaging/**/*.sh`, `postinst`, `postrm`, `tools/*.sh` (not `test_*`), and `packaging/install_windows.ps1`.
Raises `OSError` or `json.JSONDecodeError` when the ADR or the watch file cannot be read. Reads files only; no network.

### `[@ANCHOR: ext_programs:main]` `main(argv=None)`

Purpose: the command line. Argument `argv` (list of str, default `sys.argv[1:]`): exactly one item, the repository root. Returns 0 when there are no findings, 1
after printing them, 2 for a wrong argument count or when no `docs/adrs/0114_*.md` is found beside `tools`.

## Watching and stopping processes (`agent_procs.py`)

Bruce, 2026-11-08: "Should we make utilities for agents to watch processes and kill things, so that they don't make their common mistakes like
watching the watcher?" Four mistakes happened that night. (1) `sudo kill -9 $(pgrep -f "[h]ams_pf_probe")` also matched a PostgreSQL backend whose
process title held the database name; SIGKILL of one backend crashed the shared cluster and cost the whole-system gate about 100 minutes. (2) Waiters
such as `until ! pgrep -f "tools/test.py"; do sleep 3; done` inside `bash -c '...'` match their own shell and never end, or kill their own shell
(exit 144). (3) A pid list from an earlier command named a different process later. (4) Background output was polled instead of waiting on the exact
process or exit marker.

`agent_procs.py` is the replacement (pure `/proc`, standard library only; guide for agents: `agents/skills/process-watching/SKILL.md`). A process is named by its **pid and
start time** (`ticks=` in `find` output, field 22 of `/proc/PID/stat`), so a reused pid is detected. A long job is a **named job** in its own session with a record in
`~/.cache/hams-jobs/NAME.json` (`HAMS_JOBS_DIR` overrides the directory). A search never lists the searcher, its ancestors or its descendants.

```bash
python3 hams_shared/tools/agent_procs.py find --cmd-contains hams_pf_probe          # lists, flags PROTECTED ones; never signals
python3 hams_shared/tools/agent_procs.py run build --tag agent-7 -- nice -n 10 cargo build   # named job, log in ~/.cache/hams-jobs/build.log
python3 hams_shared/tools/agent_procs.py wait --job build --timeout 1800              # 0 job exited 0, 1 failed, 75 timeout
python3 hams_shared/tools/agent_procs.py wait --pid 4242 --start-time 98765           # 0 gone (or reused pid), 75 timeout
python3 hams_shared/tools/agent_procs.py wait --file out.log --contains 'RESULT=' --fail-contains FATAL --while-pid 4242
python3 hams_shared/tools/agent_procs.py stop build            # SIGTERM to the job's process group; --force adds SIGKILL on that verified group
python3 hams_shared/tools/agent_procs.py kill-pid 4242 --start-time 98765     # one exact pid, never a list or a pattern
```

Exit codes: 0 done, 1 failed (or nothing found, for `find`), 2 usage, 3 REFUSED (a protected process, a reused pid, your own ancestry), 75 timeout.
**Protected, always refused** (no flag lifts it except `--i-am-the-owner-and-checked` for your own recorded job): a process of another user, root or `postgres`; a command containing
`postgres postmaster redis-server rabbitmq beam.smp epmd systemd sshd gdm gnome claude-desktop`; the `claude` application; a `test.py` run whose directory is not under your
own worktree; a chrome/chromium whose `--user-data-dir` (or directory) is not under your `TMPDIR` (no `TMPDIR`: all chrome); pid 1; your own ancestry. Tests: `test_agent_procs.py` (real processes: a
decoy whose arguments hold the pattern, `exec -a postgres sleep` as the fake PostgreSQL, a wrong start time as pid reuse, a waiter loop whose own text holds the pattern).

### `[@ANCHOR: agent_procs:ProcInfo]` `ProcInfo`

Purpose: one process as read from `/proc` at one instant; returned by `read_proc`, `snapshot`, used by every check. A frozen dataclass in `agent_procs.py`; never refreshed, read again to see a change.

| Field | Type | Meaning |
|---|---|---|
| `pid`, `ppid`, `pgid`, `sid` | int | Process, parent, process-group and session ids. |
| `uid`, `user` | int, str | Owner (`stat` of `/proc/PID`) and its login name (the number as text when unknown). |
| `state` | str | `R S D T Z X ...` from `/proc/PID/stat`; `Z` (zombie) and `X` count as ended. |
| `start_ticks` | int | Field 22: start time in clock ticks since boot. With `pid` it identifies a process; a reused pid has a different value. |
| `comm` | str | Kernel command name (15 characters). |
| `argv` | tuple of str | Full command line (a kernel thread: `("[comm]",)`). PostgreSQL backends set a title here that contains the database name. |
| `cwd` | str or None | Working directory; `None` when unreadable. |

### `[@ANCHOR: agent_procs:JobRecord]` `JobRecord`

Purpose: the file `<jobs dir>/<name>.json` of a named job. Written by `start_job` (before the command runs), completed by the wrapper when the command ends, marked by `stop_job`. A mutable dataclass; serialized as JSON with the same field names; adding a field must keep old files loadable (a file with unknown fields is ignored as unreadable).

| Field | Type | Meaning |
|---|---|---|
| `name` | str | The job name (`check_job_name`); also the file name. |
| `tag` | str | The owning agent, from `run --tag`; free text. |
| `pid`, `pgid`, `sid` | int | The wrapper's pid, its process group and session (all equal: it is the session leader). |
| `start_ticks` | int | The wrapper's start time; with `pid` it proves the record still names that process. |
| `cwd`, `argv`, `log` | str, list, str | Directory the command ran in, the command, the log path. |
| `started_at`, `ended_at` | float, float or None | Epoch seconds. |
| `user` | str | The owner. |
| `exit_code` | int or None | `None` while running; the command's code, 128+N after signal N, 127 when the program was not found. |
| `stopped` | str or None | `"term"` or `"kill"` when `stop` ended it. |

Invariants: the wrapper writes `exit_code` only while the record's `pid` is its own (a newer job of the same name is never overwritten). Concurrency: `start_job` holds `<name>.lock` (flock) while checking and starting.

### `[@ANCHOR: agent_procs:WaitResult]` `WaitResult`

Purpose: the outcome of `wait_pid`, `wait_job` and `wait_file`. Frozen dataclass: `code` (int: the process exit code to use) and `message` (str: one line for a person).

### `[@ANCHOR: agent_procs:user_name]` `user_name(uid)`

Purpose: login name for a uid. Returns the name, or the number as text when the account is unknown. Never raises; reads the password database.

### `[@ANCHOR: agent_procs:boot_time]` `boot_time()`

Purpose: lets `format_start` turn clock ticks into a date. Returns the epoch seconds of boot (`btime` of `/proc/stat`) as float, `0.0` when unreadable. Reads one file.

### `[@ANCHOR: agent_procs:format_start]` `format_start(start_ticks)`

Purpose: the human start time printed by `find`, `jobs` and the ps line. Argument `start_ticks` int from `ProcInfo`. Returns `YYYY-MM-DDTHH:MM:SS` in local time. The ticks, not this text, are what is compared.

### `[@ANCHOR: agent_procs:read_proc]` `read_proc(pid)`

Purpose: read one process. Argument `pid` int. Returns a `ProcInfo`, or `None` when the process does not exist, vanished while being read or cannot be parsed. The start time is taken after the **last** `)` of `/proc/PID/stat` because the command name may itself contain parentheses. Never raises; reads four `/proc` entries; works for other users' processes (cwd is `None` when unreadable).

### `[@ANCHOR: agent_procs:snapshot]` `snapshot()`

Purpose: a consistent-enough table for family and group questions. Returns `{pid: ProcInfo}` for every readable process; processes that exit during the pass are omitted. Cost: one `/proc` pass (a few thousand small reads).

### `[@ANCHOR: agent_procs:ancestry]` `ancestry(table, pid)`

Purpose: the pids that must never be signalled or waited for. Arguments: `table` from `snapshot`, `pid` int. Returns the set holding `pid` and each parent up to pid 1 (stops at a pid missing from the table or a loop).

### `[@ANCHOR: agent_procs:descendants]` `descendants(table, pid)`

Purpose: the other half of "watching the watcher". Returns the set of all children, grandchildren, ... of `pid` in `table`, not `pid` itself.

### `[@ANCHOR: agent_procs:self_family]` `self_family(table, me=None)`

Purpose: the exclusion set of `find`: `ancestry(me) | descendants(me)`, `me` defaulting to this process. The `bash -c '...'` that holds a search pattern is an ancestor of the process that searches, so it is never listed. Returns a set of pids.

### `[@ANCHOR: agent_procs:is_under]` `is_under(path, root)`

Purpose: the "inside this directory" test of `--cwd-under`, the worktree rule and the TMPDIR rule. Returns True when `realpath(path)` equals or is inside `realpath(root)`; False when either is empty or `None`. A sibling with a common prefix (`/a/bc` for `/a/b`) is not inside.

### `[@ANCHOR: agent_procs:caller_worktree]` `caller_worktree(environ=None, cwd=None)`

Purpose: what "the caller's own worktree" means for the `test.py` rule. Returns `HAMS_AGENT_WORKTREE` if set, else the nearest directory at or above `cwd` (default the current directory) that holds `.git` (a worktree has a `.git` file), else `None` (then every `test.py` run counts as someone else's).

### `[@ANCHOR: agent_procs:chrome_data_dir]` `chrome_data_dir(argv)`

Purpose: where a chrome keeps its profile, to tell a test run's chrome from anyone else's. Returns the value of the `--user-data-dir=` argument, or `None`.

### `[@ANCHOR: agent_procs:protected_reason]` `protected_reason(info, caller_uid=None, worktree=None, tmpdir=None, family=())`

Purpose: the single place that says "an agent must never signal this". Called by `find` (flag), `stop_job` (every group member) and `kill_pid`.

| Argument | Type | Meaning |
|---|---|---|
| `info` | `ProcInfo` | The process to judge. |
| `caller_uid` | int | The signalling user; default the real uid. |
| `worktree` | str or None | The caller's worktree root (`caller_worktree`); `None` protects every test.py run. |
| `tmpdir` | str or None | The caller's `TMPDIR`; `None` protects every chrome. |
| `family` | set of int | The caller's ancestry (`ancestry`). |

Returns a sentence naming the rule, or `None` when it is the caller's ordinary process. Order of the rules: pid 1; own ancestry; another user, root, or the `postgres` account; a command or name containing `postgres postmaster redis-server rabbitmq beam.smp epmd systemd sshd gdm gnome claude-desktop`;
the `claude` application; a `test.py` run whose directory is not under `worktree`; a chrome/chromium whose profile or directory is not under `tmpdir`. Pure function of its arguments.

### `[@ANCHOR: agent_procs:ps_line]` `ps_line(info)`

Purpose: the verification print before any signal, and the `find` output. Returns one line: `pid= user= start= ticks= pgid= cwd= args=` (full arguments).

### `[@ANCHOR: agent_procs:find_procs]` `find_procs(table, me=None, user=None, any_user=False, cwd_under=None, contains=None, regex=None, pids=None, caller_uid=None, worktree=None, tmpdir=None)`

Purpose: the search behind `find`. Returns `[(ProcInfo, protected_reason_or_None), ...]` sorted by pid.

| Argument | Type | Meaning |
|---|---|---|
| `table` | dict | From `snapshot`. |
| `me` | int or None | The searcher; it, its ancestors and descendants are never returned. Default this process. |
| `user`, `any_user` | str, bool | Default: only the caller's own processes. Another user's name needs `any_user=True`, which lists all owners (they are flagged protected). |
| `cwd_under` | str | Keep processes whose directory is under it. |
| `contains` | str | A LITERAL substring of the space-joined command line (no pattern characters). |
| `regex` | str | A regular expression searched in the command line. |
| `pids` | dict | `{pid: start_ticks or None}` (see `read_pid_file`); a given start time must match. |

All filters must hold. Errors: `ProcError` (code 2) for a bad regex or another user without `any_user`. It never signals anything and prints nothing.

### `[@ANCHOR: agent_procs:read_pid_file]` `read_pid_file(path)`

Purpose: the `--pid-file` of `find`. Lines are `PID` or `PID:START_TICKS`; blank lines and `#` comments are ignored. Returns `{pid: ticks or None}`. Errors: `ProcError` for an unreadable file or a malformed line. A `PID:TICKS` entry whose process now has other ticks matches nothing (pid reuse).

### `[@ANCHOR: agent_procs:jobs_dir]` `jobs_dir(environ=None)`

Purpose: where records, logs and locks live. Returns `HAMS_JOBS_DIR` or `~/.cache/hams-jobs`, created with mode 0700. Side effect: creates the directory.

### `[@ANCHOR: agent_procs:check_job_name]` `check_job_name(name)`

Purpose: a job name becomes a file name, so it is validated. Returns `name` when it is letters, digits, `.`, `_`, `-`, 1 to 64 characters, alphanumeric first; else raises `ProcError` (code 2). No `/`, no leading dot.

### `[@ANCHOR: agent_procs:record_path]` `record_path(name, directory=None)`

Purpose: `<jobs dir>/<name>.json`, validating `name` (raises as `check_job_name`).

### `[@ANCHOR: agent_procs:write_record]` `write_record(rec, directory=None)`

Purpose: persist a `JobRecord`. Writes a temporary file in the same directory, then `os.replace`, so a reader never sees half a file. Raises `OSError` when the directory is not writable. Several processes may call it for different jobs at once; two writers of the same job (the wrapper and `stop`) both write whole, valid files.

### `[@ANCHOR: agent_procs:read_record]` `read_record(name, directory=None)`

Purpose: load a job. Returns the `JobRecord`, or `None` when the file is missing, not JSON or has unexpected fields.

### `[@ANCHOR: agent_procs:list_records]` `list_records(directory=None)`

Purpose: every readable record (`jobs`, and the "recorded job" proof of `kill_pid`). Returns a list sorted by `started_at`. Ignores unreadable files.

### `[@ANCHOR: agent_procs:pid_alive]` `pid_alive(pid, start_ticks)`

Purpose: the pid-reuse-safe liveness test used by every wait and stop. Returns True only when the process exists, is not a zombie or dead, and its start time equals `start_ticks`; a reused pid is False.

### `[@ANCHOR: agent_procs:job_state]` `job_state(rec)`

Purpose: classify a job. Returns `"exited"` (an exit code was recorded; it has priority), `"running"` (the recorded pid is alive with the recorded start time) or `"lost"` (gone without an exit code: SIGKILLed or the machine restarted).

### `[@ANCHOR: agent_procs:forward_signal]` `forward_signal(holder, signum, _frame)`

Purpose: the wrapper's signal handler (installed with `functools.partial(forward_signal, holder)` for SIGTERM, SIGINT, SIGHUP). `holder[0]` is the `Popen` of the command or `None`; the signal is passed to the command if it still runs, errors from a command that just ended are ignored. The wrapper itself keeps running, so it always records the exit code. Runs in signal-handler context; touches no lock.

### `[@ANCHOR: agent_procs:wrapper_main]` `wrapper_main(name, argv, directory=None)`

Purpose: the tiny process that leads a job's session (`agent_procs.py _wrap NAME -- COMMAND...`, started only by `start_job`). Waits for the line `go` on standard input (so the record exists before the command runs; EOF or anything else returns 126 without running it), runs `argv` with standard input from `/dev/null` and its output inherited (the log), waits, writes `exit_code` and `ended_at` into the record if the record still names this process, and returns the code (128+N after signal N, 127 when the program cannot be run, the reason on standard error). A job killed with SIGKILL leaves no exit code (`lost`).

### `[@ANCHOR: agent_procs:start_job]` `start_job(name, argv, tag="", cwd=None, directory=None)`

Purpose: start a named job; the engine of `run`. Returns the `JobRecord` (pid, process group and session of the wrapper, log path). Side effects: creates `<name>.lock`, truncates and writes `<name>.log`, starts `python3 agent_procs.py _wrap` with `start_new_session=True` (its own session and process group, so `stop` can signal the group and a signal to your shell cannot reach it), sets `AGENT_PROCS_JOB=<name>` in its environment, writes the record. Locks: an exclusive `flock` on `<name>.lock` while checking and starting. Errors: `ProcError` code 2 for a bad name or empty command, code 1 when a job of that name is still running.

### `[@ANCHOR: agent_procs:wait_pid]` `wait_pid(pid, start_ticks=None, timeout=1800, interval=0.5, sleep=time.sleep, clock=time.monotonic, table=None)`

Purpose: wait for one exact process, never by pattern. Returns a `WaitResult`: code 0 when it ended, was already gone, or the pid now belongs to a different process (start time differs from `start_ticks`: reuse); 75 on timeout; 3 when `pid` is this command's own ancestry (watching the watcher). `start_ticks=None` reads the start time at the first look. A plain pid's exit status is not knowable (it is not our child), so 0 means "done", not "succeeded": use `wait_job` or a marker file when success matters. Blocking; polls `/proc` every `interval` seconds; `sleep` and `clock` are for tests.

### `[@ANCHOR: agent_procs:wait_job]` `wait_job(name, timeout=1800, interval=0.5, sleep=..., clock=..., directory=None)`

Purpose: wait for a named job. Returns code 0 when its recorded exit code is 0, 1 for any other code or for a job that vanished without one (`lost`; the message names the log), 75 on timeout. Errors: `ProcError` code 2 for an unknown job. Blocking; reads the record each poll.

### `[@ANCHOR: agent_procs:wait_file]` `wait_file(path, contains=None, fail_contains=None, while_pid=None, timeout=1800, interval=0.5, sleep=..., clock=...)`

Purpose: wait for an output file or exit marker. Code 0 when `path` exists and (if `contains`, a regular expression whose `^` and `$` match at every line, is given) matches; 1 when `fail_contains` matches first or when `while_pid` (a pid, or `(pid, ticks)`) ends before the condition holds; 75 on timeout. The file is read whole at each poll: keep it a log or marker, not a data file.

### `[@ANCHOR: agent_procs:group_members]` `group_members(table, pgid, sid)`

Purpose: the verified membership of a job's group. Returns the live (not zombie) processes with that process group **and** session. A reused pid cannot be a member, because a process-group id cannot be reused while the group has members.

### `[@ANCHOR: agent_procs:stop_job]` `stop_job(name, grace=10, force=False, owner_checked=False, out=sys.stdout, directory=None, sleep=..., clock=...)`

Purpose: stop a NAMED job and nothing else.

| Argument | Type | Meaning |
|---|---|---|
| `name` | str | A job with a record. |
| `grace` | float | Seconds to wait after SIGTERM. |
| `force` | bool | Send SIGKILL to the verified group if SIGTERM was not enough. |
| `owner_checked` | bool | `--i-am-the-owner-and-checked`: skip the protected-process refusal for your own job. |

Order: read the record; find members by group and session (none: "not running", return 0); refuse a group that holds the caller's own shell; print the ps line of every member; refuse (code 3) a member of another user or a protected one; `killpg(SIGTERM)`; wait up to `grace`; only with `force`, re-verify the members and `killpg(SIGKILL)`. Returns 0 when the group is gone, 1 when it is still running (the message says `--force`). The record gets `stopped = term/kill`. Side effects: signals, record write. Errors: `ProcError` code 2 for an unknown job, 3 for a refusal.

### `[@ANCHOR: agent_procs:authorize_kill]` `authorize_kill(info, start_ticks, cwd_under, directory=None)`

Purpose: the identity proof for `kill_pid`. Returns `"recorded job"` (a record names this pid and start time), `"start time confirmed"` (the caller's `start_ticks` equals the process's) or `"cwd under <path>"`. Raises `ProcError` code 3 when `start_ticks` differs (the pid was reused) or when there is no proof at all.

### `[@ANCHOR: agent_procs:kill_pid]` `kill_pid(pid, start_ticks=None, cwd_under=None, grace=10, force=False, owner_checked=False, sig="TERM", out=sys.stdout, directory=None, sleep=..., clock=...)`

Purpose: signal one exact process. Never a list, never a pattern. Order: refuse `pid <= 1` and the caller's ancestry; a process that does not exist is "already gone" (0); refuse another user's process; `authorize_kill`; refuse a protected process (`owner_checked` lifts this only for a recorded job); print the ps line; send `sig` (TERM INT HUP QUIT USR1 USR2) to that pid only; wait up to `grace`; with `force` and the same process (same start time) still alive, SIGKILL it. Returns 0 when gone, 1 when still alive. Errors: `ProcError` code 3 for refusals, 2 for a bad signal name.

### `[@ANCHOR: agent_procs:cmd_find]` `cmd_find(args, out=sys.stdout)`

Purpose: the `find` subcommand. Prints one `ps_line` per match, with `  PROTECTED: <reason>` appended when a signal would be refused, or JSON (`--json`: ProcInfo fields plus `protected`), or nothing (`--quiet`). Returns 0 when something matched, 1 when not (so `until ! agent_procs.py find --quiet --cmd-contains X; do sleep 3; done` is a safe waiter: the shell holding X is the searcher's ancestor and is excluded). It prints no bare pid list on purpose.

### `[@ANCHOR: agent_procs:cmd_wait]` `cmd_wait(args, out=sys.stdout)`

Purpose: the `wait` subcommand: exactly one of `--pid [--start-time]`, `--job`, `--file [--contains --fail-contains --while-pid]`, with `--timeout` (default 1800 s) and `--interval`. Prints the result message; returns its code. Raises `ProcError` code 2 when not exactly one target.

### `[@ANCHOR: agent_procs:cmd_run]` `cmd_run(args, out=sys.stdout)`

Purpose: the `run` subcommand: `run NAME [--tag AGENT] [--cwd DIR] -- COMMAND...`. Prints the job's pid, process group, log path and the wait and stop commands. Returns 0.

### `[@ANCHOR: agent_procs:cmd_jobs]` `cmd_jobs(args, out=sys.stdout)`

Purpose: the `jobs` subcommand: one line per record (`name state [code=N] pid= started= tag= cwd= cmd=`), or JSON with `--json`. Returns 0.

### `[@ANCHOR: agent_procs:cmd_stop]` `cmd_stop(args, out=sys.stdout)`

Purpose: the `stop` subcommand (`stop NAME [--grace S] [--force] [--i-am-the-owner-and-checked]`); returns `stop_job`'s code.

### `[@ANCHOR: agent_procs:cmd_kill_pid]` `cmd_kill_pid(args, out=sys.stdout)`

Purpose: the `kill-pid` subcommand (`kill-pid PID [--start-time T | --cwd-under P] [--signal NAME] [--grace S] [--force] [--i-am-the-owner-and-checked]`). Exactly one pid is accepted (argparse refuses a second one with code 2); returns `kill_pid`'s code.

### `[@ANCHOR: agent_procs:build_parser]` `build_parser()`

Purpose: the argparse parser: subcommands `find wait run jobs stop kill-pid` and the internal `_wrap`. It does not know the command after `--`; `parse_args` splits that off. Returns the `ArgumentParser`.

### `[@ANCHOR: agent_procs:parse_args]` `parse_args(argv=None)`

Purpose: parse the command line, giving `run` and `_wrap` everything after the first `--` as `args.command` (a list) so that a command's own options (`-c`, `--tag`) are never taken for ours. Exits with argparse status 2 on a usage error.

### `[@ANCHOR: agent_procs:main]` `main(argv=None)`

Purpose: the command-line entry. Returns the exit code (0 done, 1 failed or nothing found, 2 usage, 3 refused, 75 timeout); a `ProcError` prints `agent_procs: <message>` on standard error.

## Guard hook for signals (`claude_hooks/guard_signals.py`)

A `PreToolUse` hook for the Bash tool, wired in the `.claude/settings.json` of `hams_com` and `hams_open`. It reads the hook JSON on standard input, parses the command text into
commands (so a word inside a quoted string, a commit message, a data heredoc or a `grep` pattern is not a command) and **denies**: `kill -9` and every other spelling of SIGKILL, `pkill`, `killall`,
`skill`, `fuser -k`, `systemctl kill`, `pgrep -f` / `--full` (the `[x]yz` trick does not help: it matched a PostgreSQL backend), `kill` whose pids are not literal numbers (`$(...)`, backticks, variables, `xargs`),
and the same inside `bash -c`, `ssh host '...'`, `eval`, `find -exec`, `watch`, shell heredocs and `python -c` one-liners. It **allows** `kill -TERM 123`, `kill 123`, `kill -0 123`, `pgrep NAME` (name only), `ps`, `grep`, `cargo`, `git`, sleep loops
and `agent_procs.py`. A denial prints JSON `permissionDecision: deny` whose reason tells the agent to use `agent_procs.py`. The escape **for a human** is `HAMS_ALLOW_SIGNALS=1` inside the command itself;
it is logged with the command to `~/.cache/hams-jobs/guard.log`, as is every denial and hook error. A crash inside the hook allows a command without a signal-looking word and denies one with such a word; the command in
settings.json also allows everything when the script file is missing. The hook always exits 0. Tests: `test_guard_signals.py` (a table of about 130 commands, the exact command of 2026-11-08 12:18:33, the real script on standard input).

### `[@ANCHOR: guard_signals:Word]` `Word`

Purpose: one shell word after quote removal. Frozen dataclass: `text` (str, without quotes or backslashes) and `expands` (bool: an unquoted or double-quoted `$` expansion or a backtick was seen, so its value is unknown when the hook runs).

### `[@ANCHOR: guard_signals:Command]` `Command`

Purpose: one simple command. Dataclass: `words` (list of `Word`, including leading assignments and wrappers) and `bodies` (list of str: heredoc bodies attached to it; only parsed when the command is a shell or an interpreter).

### `[@ANCHOR: guard_signals:Verdict]` `Verdict`

Purpose: what `analyze` found. Dataclass: `reasons` (list of str; empty means allowed) and `notes` (list of str: reminders for allowed constructs, such as a literal-pid kill).

### `[@ANCHOR: guard_signals:find_paren_end]` `find_paren_end(text, start)`

Purpose: find the end of `$( ... )`. Returns the index of the `)` that closes the `(` before `start`, scanning from `start` and honouring nesting, single and double quotes and backslashes; `len(text)` when never closed (the rest is taken as the inside). Pure.

### `[@ANCHOR: guard_signals:find_backtick_end]` `find_backtick_end(text, start)`

Purpose: find the end of a backtick substitution. Returns the index of the closing backtick (a backslash escapes the next character) or `len(text)`.

### `[@ANCHOR: guard_signals:read_heredoc]` `read_heredoc(text, start, delimiter, strip_tabs)`

Purpose: consume a heredoc body that starts at `text[start]`. Returns `(body, next_index)`: the lines before the line equal to `delimiter` (leading tabs ignored when `strip_tabs`, the `<<-` form), and the index after that line. An unterminated heredoc takes the rest of the text.

### `[@ANCHOR: guard_signals:tokenize]` `tokenize(text, commands, depth=0)`

Purpose: the shell reader. Splits `text` into simple commands and appends them (`Command`) to the list `commands`, including those inside `$(...)`, backticks, `<(...)` and `>(...)` (parsed recursively). Handles single and double quotes, backslashes, `#` comments, the boundaries `; & | && || ( )` and newline, redirections (the file word and a leading fd digit are dropped) and heredocs. Returns `commands`. Raises `ValueError` when nesting is deeper than 8 (the caller then fails according to the crash policy). Pure; not a full shell parser: it errs towards seeing more commands, never fewer.

### `[@ANCHOR: guard_signals:skip_options]` `skip_options(words, start, value_options)`

Purpose: step over a wrapper's options (`sudo -n -u odoo`). Returns the index of the first word at or after `start` that is not an option; words starting with `-` are options, those in `value_options` also consume the next word, `--` ends the options.

### `[@ANCHOR: guard_signals:resolve]` `resolve(words)`

Purpose: find the real command behind assignments, reserved words (`until`, `do`, `!`, `{`) and wrappers (`sudo env nice ionice timeout stdbuf nohup setsid command exec chrt taskset flock xargs time`). Returns `(name, rest, via_xargs)`: the program's base name, its words (name first), and whether an `xargs` took the arguments from a pipe. `name` is `None` when nothing is left, or for `command -v NAME` (only a lookup).

### `[@ANCHOR: guard_signals:signal_is_kill]` `signal_is_kill(spec)`

Purpose: recognise SIGKILL. True for `9`, `KILL` and `SIGKILL` in any case with or without a leading `-`.

### `[@ANCHOR: guard_signals:check_kill]` `check_kill(words, via_xargs, verdict)`

Purpose: judge a `kill` command (`words[0]` is kill). Adds a reason to `verdict` for SIGKILL in any option form (`-9 -KILL -s KILL -n 9 --signal=9`), for a pid that is not a literal number (or `%job`, or `$!`) and for pids supplied by `xargs`. `kill -l` and a `kill` with no pid are fine. A literal pid adds the note to print its ps line first.

### `[@ANCHOR: guard_signals:check_pgrep]` `check_pgrep(words, verdict)`

Purpose: deny `pgrep -f` (also in a cluster such as `-af` or `-lf`) and `pgrep --full`; the pattern would be matched against every command line, including the shell that holds it. `pgrep NAME` (name only) passes.

### `[@ANCHOR: guard_signals:shell_script]` `shell_script(words)`

Purpose: the script text of `sh -c SCRIPT`, `bash -lc SCRIPT`, `su -c SCRIPT`; `None` when there is no `-c` word.

### `[@ANCHOR: guard_signals:check_command]` `check_command(command, verdict, depth)`

Purpose: judge one `Command`: names `pkill killall skill killall5`, `kill`, `pgrep`, `fuser -k`, `systemctl kill`; recurses (`depth` guards recursion) into `bash -c`, `su -c`, shell heredoc bodies, `eval`, `ssh` and `watch` (every suffix of the words, since options precede the command), `find -exec`, and interpreter (`python perl ruby node php`) `-c`/`-e` strings and heredoc bodies that use SIGKILL, `os.killpg` or call `pkill`, `killall` or `pgrep -f`. Adds reasons to `verdict`.

### `[@ANCHOR: guard_signals:analyze]` `analyze(text, depth=0, verdict=None)`

Purpose: parse and judge a whole command text. Returns the `Verdict` (the object passed in, or a new one). Raises `ValueError` for nesting deeper than 8.

### `[@ANCHOR: guard_signals:log_path]` `log_path(environ=None)`

Purpose: `<jobs dir>/guard.log` (`HAMS_JOBS_DIR` or `~/.cache/hams-jobs`).

### `[@ANCHOR: guard_signals:log_event]` `log_event(kind, command, detail, session_id="", environ=None)`

Purpose: append one JSON line (`time kind session detail command`, the command cut to 2000 characters) for an `override`, a `denied` command or an `error`. Creates the directory with mode 0700. Never raises: a log that cannot be written must not block or crash the hook.

### `[@ANCHOR: guard_signals:deny_message]` `deny_message(reasons)`

Purpose: the text an agent reads on a denial: the reasons (deduplicated), the replacements (`wait --pid/--job/--file`, `run`, `stop`, `kill-pid`, `find`, with the absolute path of `agent_procs.py`) and the statement that only a human may use `HAMS_ALLOW_SIGNALS=1`.

### `[@ANCHOR: guard_signals:decide]` `decide(command, session_id="", environ=None)`

Purpose: the decision for one command. Returns `None` (allow, print nothing), the deny JSON (`hookSpecificOutput.permissionDecision = deny` with `deny_message`), or an `additionalContext` reminder for a literal-pid kill (no permission decision, so the normal permission flow is untouched). `HAMS_ALLOW_SIGNALS=1` as a word of the command allows a would-be denial and logs `override`. A crash in `analyze` is logged; a command without a signal-looking word (`kill pgrep pidof xargs -9 SIGKILL fuser`) is then allowed, one with such a word is denied.

### `[@ANCHOR: guard_signals:main]` `main(stdin=None, stdout=None, environ=None)`

Purpose: the hook entry point. Reads the hook JSON (`tool_name`, `tool_input.command`, `session_id`) from `stdin`, prints the decision JSON on `stdout`, returns 0 always. Anything that is not a Bash call with a text command (or not JSON) is allowed.

## Template-database benchmark (`benchmark_template_db.py`)

Measures whether a pre-built template database speeds up an Odoo test run (Bruce, 2026-11-08). It imports `test.py`'s own
`main()` and changes nothing in it; the clone arms replace exactly two things (`rebuild_db` and the `-i` argument). Findings and the
design of a production implementation: `docs/TEMPLATE_DATABASE_FOR_TEST_RUNS.md`. Tests: `test_benchmark_template_db.py`.
Every entry below is for `benchmark_template_db.py`; arguments not listed are not used. Nothing here blocks except `run_measured`,
`timed_clone`, `RamTablespace`, `clone_for_run`, `stub_main`, `Bench` and `run_command`, which run subprocesses or SQL and take a test slot
(`run_command` only) for the length of a measurement. No function handles a secret; `HAMS_CRYPTO_KEY` enters the key only as a
double-hashed fingerprint.

### `[@ANCHOR: tools:template_db_identifier]` `SAFE_IDENTIFIER_RE`

Constant: the regular expression `^[A-Za-z_][A-Za-z0-9_]*$` (the rule of `test.py`'s `_SAFE_DB_NAME_RE`). Read only by `check_identifier`;
every database, template and tablespace name interpolated into SQL goes through it, because PostgreSQL has no placeholder for an identifier.

### `[@ANCHOR: tools:template_db_check_identifier]` `check_identifier(name)`

Purpose: the one gate in front of every identifier put in SQL. Returns `name` if it is a `str` matching `SAFE_IDENTIFIER_RE` and at most
63 bytes (PostgreSQL's limit, longer names are silently truncated and could collide). Raises `ValueError` otherwise, including for `None`.
Pure, idempotent. Tests: `IdentifierAndListTests`.

### `[@ANCHOR: tools:template_db_parse_module_list]` `parse_module_list(text)`

Purpose: reads a module list such as `~/workspace/tmp/wholesys/modules.txt`. `text`: str or `None`; separators are commas and any whitespace.
Returns a list of names in first-seen order without duplicates (`[]` for empty or `None`). Pure.

### `[@ANCHOR: tools:template_db_find_modules]` `find_module_dirs(addons_dirs)`

Purpose: maps module names to directories. `addons_dirs`: iterable of directory paths (the runner's addons path); unreadable ones are skipped.
Returns `{name: realpath}` for every `<dir>/<name>/__manifest__.py`; the first directory wins on a duplicate name. Reads directory listings only.

### `[@ANCHOR: tools:template_db_dependency_closure]` `dependency_closure(names, module_dirs)`

Purpose: the modules whose files decide a template's content. `names`: starting modules; `module_dirs`: result of `find_module_dirs`. Returns a
sorted list of `names` and everything they transitively `depends` on that is in `module_dirs`; dependencies outside it are Odoo's own addons,
covered by the Odoo version in the key. Raises `SyntaxError`/`ValueError` for a manifest that is not a Python literal: a key must never be
built from a guess. Reads manifests; pure otherwise.

### `[@ANCHOR: tools:template_db_hash_sources]` `hash_module_sources(module_dirs, names, include_tests=True)`

Purpose: the source-tree part of the key. Returns the sha256 hex over, for each module in sorted order, the relative path and the bytes of every
file (data XML/CSV, Python, migrations, static files), walked in sorted order so directory order never matters; a symbolic link is hashed by its
target text and never followed. Skips `__pycache__`, `.git`, `.pytest_cache`, `node_modules`, `*.pyc`, `*.pyo`.

| Argument | Type | Meaning |
|---|---|---|
| `module_dirs` | dict | `find_module_dirs` result. |
| `names` | iterable of str | Modules to hash, normally the `dependency_closure`. |
| `include_tests` | bool | `True` (default, safest): a test edit changes the key. `False`: a module's top-level `tests` directory is skipped; only safe while `tests_define_models` is empty. |

Cost: reads every byte; about 0.5 s for the 45 modules of the `parking` closure on this box, one pass per run. Not a lock holder.

### `[@ANCHOR: tools:template_db_tests_define_models]` `tests_define_models(module_dirs, names)`

Purpose: finds modules whose tests define ORM models. Such a model's table is only created when the module is installed with tests enabled, so a
template built without tests would lack it. Returns a sorted-by-module list of names (empty = the key may ignore test files). A heuristic: a
file under `tests/` that mentions `models.Model`, `TransientModel` or `AbstractModel` and has an `_name =` line.

### `[@ANCHOR: tools:template_db_key]` `TemplateKey`

Frozen dataclass: everything a template's content depends on. Lives in `benchmark_template_db.py`; created by `Bench.build_key`, read by `verify_template` and `run_command`.

| Field | Type | Meaning |
|---|---|---|
| `modules` | tuple of str | The install list the runner passes to `-i` (`base` plus the targets and their test companions), in order. |
| `source_hash` | str | `hash_module_sources` of the dependency closure. |
| `odoo_version` | str | `odoo.release.version` (a Debian package version such as `19.0-20261107`; it stands for Odoo's own addons). |
| `postgres_major` | str | Major version from `server_version_num`. |
| `vector_version` | str | Highest available `vector` extension version (the runner creates the extension before installing). |
| `options` | tuple of str | Install options that change data, e.g. `("demo",)`. |
| `crypto_key_fingerprint` | str | First 16 hex digits of `sha256(sha256(HAMS_CRYPTO_KEY))`: data encrypted with the key is in the template. Never the key. |

Methods: `canonical()` (sorted-key compact JSON), `digest()` (sha256 hex of it), `database_name()` (`hams_tpl_` + first 16 digits), `comment()`
(the text stored with `COMMENT ON DATABASE`: `{"hams_template_db": 1, "digest": ..., "key": {...}}`). Invariant: equal fields give an equal name; any
field change gives a different name, so a changed key cannot find an old template. Immutable, safe to share between threads.

### `[@ANCHOR: tools:template_db_verify_template]` `verify_template(comment, key)`

Purpose: the second line of defence behind the content-addressed name. `comment`: the template database's comment or `None`. Returns `(True, "")`
if it was written for exactly `key`, else `(False, reason)` for a missing comment (a build that did not finish writes none), a comment that is not
JSON, not a template comment, or carries another digest. Pure.

### `[@ANCHOR: tools:template_db_summarize]` `summarize(values)`

Purpose: the statistics of repeated measurements. `values`: iterable of numbers; `None` entries are ignored. Returns a dict `n, median, min, max,
mean, stdev, cv_percent` (sample standard deviation, 0.0 for one value; the coefficient of variation in percent), all `None` except `n=0` for no
values. Pure.

### `[@ANCHOR: tools:template_db_parse_loadscreen]` `parse_loadscreen(text)`

Purpose: turns `/dev/shm/loadscreen/stats.txt` into numbers so every measurement carries the box state. Returns a dict with any of `recommendation`
(HOLD, GO...), `cpu_busy_1min/_5min`, `io_wait_1min/_5min`, `postgres_1min/_5min`, `load1`, `test_slots_busy`, `tests_waiting`, `mem_avail_gb`;
fields not found are left out and unexpected text gives `{}`. Never raises. Pure.

### `[@ANCHOR: tools:template_db_read_loadscreen]` `read_loadscreen(path="/dev/shm/loadscreen/stats.txt")`

Reads the file and returns `parse_loadscreen` of it, `{}` if it cannot be read. One small file read.

### `[@ANCHOR: tools:template_db_log_milestones]` `LogMilestones`

Dataclass returned by `parse_odoo_log`. Times are epoch seconds (Odoo logs UTC), `None` when the event did not occur.

| Field | Type | Meaning |
|---|---|---|
| `first_line`, `last_line` | float | First and last Odoo log line. |
| `modules_loaded` | float | The `Modules loaded.` line: install or load finished. |
| `post_tests_start` | float | `Starting post tests`. |
| `first_test` | float | First `Starting Class.test ...` of any kind (an at_install test counts). |
| `first_post_install_test` | float | First such line after `post_tests_start`. |
| `tests_started` | list of str | `Class.test` per start, in order. |
| `failed` | set of str | `Class.test` named in a `FAIL:`/`ERROR:` line. |
| `result` | tuple or `None` | `(failed, errors, tests)` from the `odoo.tests.result` line, the only authority on counts. |
| `install_seconds` | float | The largest `N modules loaded in X s` figure. |
| `error_lines` | int | Count of ERROR/CRITICAL lines. |

### `[@ANCHOR: tools:template_db_parse_odoo_log]` `parse_odoo_log(lines)`

Purpose: reads the runner's output. `lines`: iterable of str. Lines that are not Odoo log lines (banners, traceback continuations) are ignored.
Returns a `LogMilestones`. Pure; one pass.

### `[@ANCHOR: tools:template_db_compare_outcomes]` `compare_outcomes(reference, other, only_post_install=True, post_install_names=None)`

Purpose: "a clone must not change test outcomes". Both arguments are `LogMilestones`. Returns `{"same", "only_in_reference", "only_in_other",
"failed_differ"}`; the lists are sorted test names. `post_install_names`, when given, restricts both sides to those names. Same means: the same
tests started and the same tests failed. (`only_post_install` is reserved for callers that pass no explicit name set; the harness passes the names.)

### `[@ANCHOR: tools:template_db_equality_lines]` `equality_lines(records)`

Purpose: the "a clone must not change test outcomes" check. `records`: list of dicts with `set_name`, `arm`, `rep`, `outcome_names`, `failed_names`, `result`. Returns Markdown bullets, one per
non-reference run, comparing it with the set's first `fresh_ssd` run: `SAME outcome` when the `odoo.tests.result` triple and the sets of started and failed tests (including `setUpClass (...)` errors) are equal, else
`DIFFERENT` with the counts. Sets without a `fresh_ssd` run give no lines; `build` runs are skipped. A `clone_i` run reports DIFFERENT with 0 tests, which is the point of that arm. Pure.

### `[@ANCHOR: tools:template_db_clone_sql]` `clone_database_sql(name, template, tablespace=None, strategy=None)`

Returns one `CREATE DATABASE "name" TEMPLATE "template" [TABLESPACE "ts"] [STRATEGY = WAL_LOG|FILE_COPY];` statement. All names go through `check_identifier`; `strategy`
must be `wal_log` or `file_copy` (case-insensitive) or `None` (server default). Raises `ValueError`. `FILE_COPY` forces a checkpoint but is the only strategy
`file_copy_method = clone` (reflink) applies to. The statement fails if anyone is connected to the template.

### `[@ANCHOR: tools:template_db_rekey_sql]` `rekey_sql()`

Returns the SQL that gives a clone its own `database.uuid`, `database.secret`, `form_shield.signing_key` and `database.create_date` (`SECRET_PARAMETERS`), the
`ir_config_parameter` rows an install fills from a random source. Test databases only (`gen_random_uuid`, `md5(random())`). Idempotent in effect. Run on the clone, never on the template.

### `[@ANCHOR: tools:template_db_proc_cpu_seconds]` `proc_cpu_seconds(stat_text, clock_ticks=100)`

Returns user plus system CPU seconds from the text of `/proc/<pid>/stat` (fields 14 and 15, counted after the last `)` because the command name may contain spaces and
parentheses, as `postgres: odoo db (x)` does), or `None` if the text does not parse. `clock_ticks`: `SC_CLK_TCK`.

### `[@ANCHOR: tools:template_db_postgres_cpu_sampler]` `PostgresCpuSampler(database, interval=0.25, proc_root="/proc", clock_ticks=None)`

Context manager: a thread samples `proc_root` every `interval` seconds for processes whose command line is `postgres: <user> <database> ...` and keeps the largest CPU total per pid.
`cpu_seconds` (after exit) is the sum: a lower bound, it misses the last fraction of a second of each backend and the checkpointer, WAL writer and I/O workers. `sample_once()` does one pass.
Daemon thread; read-only on `/proc`.

### `[@ANCHOR: tools:template_db_as_odoo]` `as_odoo(argv, env=None)`

Returns `argv` prefixed with `sudo -n -u odoo` (and `env K=V ...` when `env` is a dict, since sudo resets the rest). The measured runs execute as `odoo`, like the dev box's documented
`sudo -n -u odoo env HAMS_ISOLATED_NS=1 ...` invocation: `daemon_key_manager` chmods `/opt/hams/etc/keys`, which only its owner may do, and as another user the registry fails to load. Never prompts. Pure.

### `[@ANCHOR: tools:template_db_filestore_bytes]` `filestore_bytes(name)`

Apparent size in bytes of database `name`'s directory under `FILESTORE_ROOT` (`sudo -n -u odoo du -sb`); 0 when absent or unreadable. `name` is checked by `check_identifier`.

### `[@ANCHOR: tools:template_db_remove_filestore]` `remove_filestore(name)`

Removes the filestore directory of a template database (`sudo -n -u odoo rm -rf --`). Refuses any name that does not start with `hams_tpl_` (`ValueError`), so it can never delete a run's or another session's filestore.
Best effort: a failing `rm` is not an error.

### `[@ANCHOR: tools:template_db_arm_plan]` `ArmPlan`

Frozen dataclass: how an arm differs from today's run. `arm` (name), `clone` (replace `rebuild_db` with a clone), `drop_install` (remove `-i <modules>`: a plain load), `update`
(use `-u`), `build` (template build: database named after the key, no tests), `ram` (database in the tmpfs tablespace), `tests` (keep `--test-enable --test-tags`). Built only by `arm_plan`.

### `[@ANCHOR: tools:template_db_arm_plan_for]` `arm_plan(arm)`

Returns the `ArmPlan` for one of `ARMS` (`fresh_ssd`, `fresh_ram`, `build`, `clone_ssd`, `clone_ram`, `clone_update`, `clone_i`); raises `ValueError` for any other name.

### `[@ANCHOR: tools:template_db_transform_command]` `transform_odoo_command(cmd, plan, database=None)`

Purpose: the second of the two replacements. `cmd`: the Odoo command (list) `test.py` built; not modified. Returns a new list: `-d` value replaced when `database` is given; `-i LIST` removed
(`drop_install`) or turned into `-u LIST` without `base` (`update`); `--test-enable` and the `--test-tags` pair removed when `plan.tests` is false. Other arguments keep their order. Raises
`ValueError` if `-i` is absent when it has to be removed.

### `[@ANCHOR: tools:template_db_run_record]` `RunRecord`

Dataclass, one measured run, stored with `dataclasses.asdict` in `results_<set>.json`.

| Field | Type | Meaning |
|---|---|---|
| `set_name`, `arm`, `rep` | str, str, int | Identity. |
| `wall_total` | float s | Rebuild start to Odoo exit. |
| `wall_prepare` | float s | `rebuild_db` or the clone step. |
| `wall_to_modules_loaded`, `wall_to_first_test`, `wall_to_first_post_install_test` | float s or `None` | From rebuild start. |
| `odoo_seconds` | float | Odoo's own module-load figure. |
| `cpu_runner_seconds` | float | User+system CPU of the runner process tree (`wait4`). |
| `cpu_postgres_seconds` | float | Sampled PostgreSQL backend CPU (lower bound). |
| `load_before`, `load_after` | dict | `parse_loadscreen` at both ends. |
| `result` | list or `None` | `odoo.tests.result` triple. |
| `outcome_names`, `failed_names` | lists | For `compare_outcomes`. |
| `error_lines` | int | Lines logged at ERROR or CRITICAL; a build with any is not trusted. |
| `exit_code`, `notes` | int, str | Exit status; `NO odoo.tests.result LINE` when a test arm printed no result (the run proves nothing). |

### `[@ANCHOR: tools:template_db_record_from_run]` `record_from_run(set_name, arm, rep, events, milestones, cpu_runner, cpu_postgres, load_before, load_after, exit_code)`

Builds a `RunRecord`. `events`: `{"rebuild_start", "rebuild_end", "odoo_end"}` epoch seconds from the stub; log-derived times are measured from `rebuild_start`
so all arms share a clock; missing events give `None` fields. Pure.

### `[@ANCHOR: tools:template_db_build_succeeded]` `build_succeeded(record)`

Purpose: decides whether a `build` run made a trustworthy template. The runner exits 1 for a run with no `odoo.tests.result` line, and a build runs no tests, so the exit
status is not the test. Returns True when Odoo logged `Modules loaded.` (`wall_to_modules_loaded` is not `None`) and `error_lines` is 0 (no ERROR or CRITICAL line). `record`: a `RunRecord`. Pure.

### `[@ANCHOR: tools:template_db_valid_records]` `valid_records(records)`

Returns the records (`RunRecord`s or dicts) as dicts, without those whose `notes` start with `INVALID`. A run that something outside the benchmark disturbed (the shared PostgreSQL crash-restarting under it) is marked
by editing its `notes` to `INVALID: <reason>`; it stays in the results file for the record and is left out of `render_report` and `equality_lines`. Pure.

### `[@ANCHOR: tools:template_db_render_report]` `render_report(records)`

Returns Markdown: per set a table of rows (arm, repetitions) and columns (`METRICS`) each `median [min-max]`, `-` when no run has the metric, then the `load1` range over all
runs of the set. `records`: `RunRecord`s or dicts. Sets are ordered small, medium, large. Pure.

### `[@ANCHOR: tools:template_db_load_runner]` `load_runner_module(repo_root)`

Imports `<repo_root>/hams_shared/tools/test.py` (or `<repo_root>/tools/test.py`) as `hams_test_runner` (never `import test`: that is the standard library's), puts its directory first on
`sys.path` and changes the working directory to `repo_root`, which `main()` requires. Importing `test.py` starts its resource-monitor thread. Raises `FileNotFoundError`.

### `[@ANCHOR: tools:template_db_stub_main]` `stub_main(argv)`

The child process of every measured run. `argv` = `[arm, repo_root, template_name, *test.py arguments]`. Loads the runner, replaces `rebuild_db` (a `clone_for_run` for clone arms; the original, then
`ALTER DATABASE ... SET TABLESPACE` for `fresh_ram`; the original on the template's name for `build`) and `run_cmd` (command through `transform_odoo_command`), appends `rebuild_start`, `rebuild_end`,
`odoo_start`, `odoo_end` JSON lines to the file in `HAMS_BENCH_EVENTS`, sets `sys.argv` and calls the runner's `main()`, which ends in `sys.exit`. Run with `HAMS_ISOLATED_NS=1` and
`HAMS_TEST_LOCK_HELD=1` by the parent, which holds the slot.

### `[@ANCHOR: tools:template_db_clone_for_run]` `clone_for_run(runner, db_name, template, plan, redis_flush_db)`

`rebuild_db` for a clone arm: the runner's own production-host refusal and Redis flush; terminate connections and `DROP DATABASE IF EXISTS ... WITH (FORCE)`;
`SET file_copy_method` (`HAMS_BENCH_COPY_METHOD`, default `copy`); `CREATE DATABASE ... TEMPLATE` in `pg_default` or the RAM tablespace (strategy from `HAMS_BENCH_STRATEGY`); the slot marker comment;
`remove_stale_filestore`; `cp -a --reflink=auto` of the template's filestore; `rekey_sql` on the clone. Raises `RuntimeError` when the template does not exist (a clone arm never falls back to a fresh install) and
`ValueError` for an unsafe name, both before any change.

### `[@ANCHOR: tools:template_db_ram_tablespace]` `RamTablespace(directory, size, sql, shell, name=BENCH_TABLESPACE)`

Context manager for the private tmpfs and its tablespace. Enter: refuses an existing tablespace or a directory that is already a mount point; `sudo -n mount -t tmpfs -o size=<size> tmpfs <directory>`,
`chown postgres:postgres`, `chmod 700`, `CREATE TABLESPACE`. Exit: drops every database still in the tablespace, `DROP TABLESPACE`, `sudo -n umount`. `sql(text)` returns stdout, `shell(argv)` returns a
`CompletedProcess` (injectable). Never touches `/etc/fstab` or PostgreSQL configuration. Raises `RuntimeError`.

### `[@ANCHOR: tools:template_db_timed_clone]` `timed_clone(connect, statement, drop_sql, backend_cpu)`

Times one `CREATE DATABASE`. `connect()`: a DB-API autocommit connection to `postgres`; `backend_cpu(pid)`: CPU seconds of a backend or `None`. Returns `{"wall": s, "cpu": s or None}` (`cpu` is the
session's own backend only, not checkpointer or I/O workers), then runs `drop_sql` untimed and closes the connection.

### `[@ANCHOR: tools:template_db_run_measured]` `run_measured(cmd, env, cwd, log_path, database, sampler_factory=PostgresCpuSampler)`

Runs `cmd` to the end; returns `(lines, returncode, cpu_runner_seconds, cpu_postgres_seconds)`. Output (stdout and stderr merged) goes to `log_path` and `lines`. Runner CPU is the `wait4` rusage of the child
and the descendants it waited for. Blocks. Raises `OSError` if the command cannot start.

### `[@ANCHOR: tools:template_db_bench]` `Bench(args)`

One benchmark session against a checkout: loads the runner, `build_key()` (the `TemplateKey` of the set's install list), `run_arm(arm, rep, slot, slots, template, test_tags=None)` (one measured run, appended to
`records` and saved to `results_<set>.json`), `build_template(key, slot, slots, rep)` (a `build` arm run, the key comment, sizes), `clone_microbenchmark(template, ram_template, reps)` (CREATE DATABASE timings per
combination, the filestore copy, the re-key). `args` is the `run` command line. Not thread safe. The parent holds the test slot (`run_command`); children run with `HAMS_TEST_LOCK_HELD=1` and the slot handed over as
`--slots N --slot-held K`, the mechanism the full pipeline itself uses.

### `[@ANCHOR: tools:template_db_run_command]` `run_command(args)`

`run`: builds the key, takes one test slot through the runner's own queue (`_acquire_lock_via_queue`, so it waits its turn behind other sessions), mounts the RAM tablespace if an arm needs it, builds the template
`--build-reps` times, verifies its comment, copies it into RAM, runs the clone micro-benchmark, then `--reps` rounds of the `--arms` interleaved (so box-load drift hits all arms alike). Always drops the templates, their filestores
and the tablespace, and unmounts. Returns 0; raises on a failed build.

### `[@ANCHOR: tools:template_db_key_command]` `key_command(args)`

`key`: prints the template database name, digest, hashing time, closure size and `tests_define_models` for `--modules`. Needs the runner and two version queries; changes nothing.

### `[@ANCHOR: tools:template_db_report_command]` `report_command(args)`

`report`: prints `render_report` of the given results files and, per set, the build sizes and times, the CREATE DATABASE timings per combination, the filestore copy and re-key times. Returns 0.

### `[@ANCHOR: tools:template_db_build_parser]` `build_parser()`

The `argparse` parser: sub-commands `run` (`--repo-root`, `--set small|medium|large`, `--reps`, `--build-reps`, `--arms`, `--test-tags`, `--tmpfs-dir`, `--tmpfs-size`, `--out-dir`, `--strategy`, `--copy-method`,
`--no-clone-micro`, `--keep-template`), `key` and `report`.

### `[@ANCHOR: tools:template_db_main]` `main(argv=None)`

Entry point: `_stub ...` goes to `stub_main` (internal), anything else is parsed and dispatched to `run_command`, `key_command` or `report_command`. Returns the exit status.

## Per-PR targeted test runs (`test_impact.py`)

Why: a branch that touches one module does not need the whole-system run. `test_impact.py` reads the branch's changed files
(against `origin/main`), maps them to Odoo modules, widens the set to every module that depends on one of them (across hams_com and
hams_open) and prints the smallest `test.py -u ...` command that is still safe; when only test files changed it adds the specific
`--test-tags`. It is conservative: a change it cannot map to one module's code (a manifest, `security/`, `data/`, `views/`, any XML or
CSV outside `static/`, a hams_shared tool, a shared library, a daemon, an unknown file type) answers "run the full module list" and says
why. Only documentation-only changes answer "no Odoo test impact". The per-PR run is a shortcut for the author and the merge gate;
the **nightly whole-system run stays the safety net** for anything a targeted run cannot see (cross-module data, shared state, load
order). Run it with `nice -n 10 python3 hams_shared/tools/test_impact.py` (add `--command-only` to paste the line, `--files a b` to
analyse paths without git), then run the printed command through the usual `sudo -n -u odoo env ... test.py --wait` wrapper.

| Changed file | Verdict |
|---|---|
| `<module>/models|controllers|static|...` code or asset (`.py .js .scss` ...) | that module and its reverse dependents |
| `<module>/tests/test_*.py` only | that module, narrowed to `/module:Class` tags |
| `<module>/README.md`, `<module>/docs/` | that module, whole |
| `__manifest__.py`, `security/`, `data/`, `views/`, `demo/`, `i18n/`, `report/`, XML or CSV outside `static/` | full list |
| anything outside every module (hams_shared tools, shared libs, daemons, config, the `hams_shared` gitlink) | full list |
| Markdown or `docs/`, `night_shift_*` outside modules | nothing to run |

### `[@ANCHOR: test_impact:Impact]` `Impact(kind, modules, test_tags, reasons)`

Purpose: the answer `analyze` returns and `build_command` prints. A `collections.namedtuple` in `tools/test_impact.py`; immutable, built
per call, never stored or serialised.

| Field | Type | Meaning |
|---|---|---|
| `kind` | str | `none` (nothing to run), `targeted` (the closure of changed code), `tests` (only test or documentation files changed), `full` (every module). |
| `modules` | list of str | Sorted module names for `-u`; empty when `kind` is `none`. |
| `test_tags` | list of str or None | `/module:Class` selectors when `kind` is `tests` and every changed test file has readable classes, else None. |
| `reasons` | list of str | One line per changed file (verdict and why) and a last `=>` line giving the decision. |

Invariants: `full` always lists every module of the universe; `test_tags` is only set for `tests`.

### `[@ANCHOR: test_impact:module_dirs]` `module_dirs(roots)`

Purpose: the module universe. Finds directories directly below each root that hold a `__manifest__.py`.

| Argument | Type | Meaning |
|---|---|---|
| `roots` | iterable of str | Directories such as the hams_com and hams_open checkouts. A root that cannot be listed is skipped. |

Returns: `{name: real path}`; the first root wins when two roots hold the same name. No errors raised. Reads the directory listing only;
no locks, no side effects, safe to repeat.

### `[@ANCHOR: test_impact:read_depends]` `read_depends(module_path)`

Purpose: the `depends` list of a manifest, parsed with `ast.literal_eval` (the manifest is never executed).

| Argument | Type | Meaning |
|---|---|---|
| `module_path` | str | A module directory. |

Returns: list of names; `[]` when the manifest is missing, unparsable or not a dict. Includes Odoo's own modules (`base`, `web`), which
the closure ignores.

### `[@ANCHOR: test_impact:reverse_closure]` `reverse_closure(changed, modules)`

Purpose: every module that must be retested when `changed` modules change: those modules plus all that depend on them, transitively.

| Argument | Type | Meaning |
|---|---|---|
| `changed` | iterable of str | Module names that changed. |
| `modules` | dict | The `module_dirs` mapping; each manifest is read once to build the reverse graph. |

Returns: sorted list of names. A dependency cycle cannot loop (visited modules are not revisited).

### `[@ANCHOR: test_impact:classify_path]` `classify_path(rel)`

Purpose: the conservative per-file rule inside a module. Pure; no file access.

| Argument | Type | Meaning |
|---|---|---|
| `rel` | str | Path relative to the module directory, `/`-separated. |

Returns `(category, reason)`: `full` (manifest, `security/`, `data/`, `views/`, `demo/`, `i18n/`, `report/`, `templates/`, XML outside `static/`, unknown
types), `code` (code and assets, including `tests/` helpers other than `test_*.py`, since dependents may import them), `test` (`tests/test_*.py`),
`doc` (Markdown, `docs/`).

### `[@ANCHOR: test_impact:classes_in_test_file]` `classes_in_test_file(path)`

Purpose: lets `--test-tags` name test classes. Parses a test file (never imports it) and returns the top-level classes that have a base class.

| Argument | Type | Meaning |
|---|---|---|
| `path` | str | A Python file; a missing, unreadable or unparsable file yields `[]` (a deleted test file), which makes the caller run the module whole. |

### `[@ANCHOR: test_impact:analyze]` `analyze(changes, modules)`

Purpose: the decision. Called by `main` and by tests.

| Argument | Type | Meaning |
|---|---|---|
| `changes` | list of (str, str) | `(repo_root, repo-relative path)` for each changed file; renames and deletions appear under their old and new names. |
| `modules` | dict | The `module_dirs` universe. The owning module is the one with the longest path prefix. |

Returns an `Impact`. Order of precedence: any `full` file makes the result full; else any code change gives the closure (plus any
documentation/test-only modules, whole, because tags would under-test the code modules); else tests-only narrowing; else none. Pure
apart from reading the test files and manifests; no locks, safe to retry.

### `[@ANCHOR: test_impact:build_command]` `build_command(impact)`

Purpose: the line to run. Returns `python3 hams_shared/tools/test.py --wait -u <modules> [--test-tags <tags>]`, or a `#` comment line for
`none`. It does not add the `sudo -n -u odoo env ...` wrapper; take that from `AGENTS.md`.

### `[@ANCHOR: test_impact:changed_files]` `changed_files(repo, base="origin/main")`

Purpose: the branch's changed paths: everything that differs from `merge-base(base, HEAD)` in the work tree, plus untracked files, with renames
reported as delete and add (`--no-renames`).

| Argument | Type | Meaning |
|---|---|---|
| `repo` | str | Any path inside a git checkout. |
| `base` | str | The ref to compare against. |

Returns a sorted list of repo-relative paths. Raises `RuntimeError` when git fails (not a checkout, or no such ref). Runs read-only git commands.

## Admission control: `dispatch_ok.py`

Why: on 2026-11-08 coordinators added agents until lint runs waited 10+ minutes behind each other, which made every agent slower. The
load display (a small user program outside the repositories that reads `/proc` and the lint and test queue directories) now
publishes a `dispatch` verdict, OK or HOLD, in `/dev/shm/loadscreen/stats.json` and `stats.txt`. **Coordinators start a new agent only
when `python3 hams_shared/tools/dispatch_ok.py` says OK** (exit 0); on HOLD (exit 1) they wait and re-check. Work that is already running is
never stopped by it. A missing or stale file (older than 60 s) is HOLD: no verdict, no new agent.

The thresholds live in one `DISPATCH` block at the top of the display program: HOLD when the oldest waiting lint run has waited more than 10
minutes, when more than 4 lint runs are queued, when the oldest waiting test run has waited more than 10 minutes, when effective CPU
(normal-priority plus half of nice-priority, 5-minute average) is at or over 85 %, or when available memory is under 10 GB. The queue
numbers come from the lint and test lock queue directories, read only. The display also lists unblock-priority lint runs
(`docs/LINT_PRIORITY_INTERFACE.md`) in its numbers.

Overflow machines: the verdict is about this server. While it is HOLD, the display makes one read-only ssh `BatchMode` load check per overflow
machine (at most every 5 minutes) and publishes `remote_capacity`; `dispatch_ok.py` prints a line when one is free, so a coordinator may keep
dispatching work that routes there. Machines in the display's `reserved` list are listed and never probed.

### `[@ANCHOR: dispatch_ok:stats_format]` the stats file (`DEFAULT_STATS`)

Purpose: the file contract between the load display (writer, every 5 s, atomically) and `dispatch_ok.py` (reader). Lives in RAM
(`/dev/shm/loadscreen/stats.json`); never persisted; replaced whole on each write.

| Field | Type | Meaning |
|---|---|---|
| `written_epoch` | int | Unix seconds of the write. Older than `--max-age` (60 s) means the display is not running. |
| `dispatch.state` | `"OK"` or `"HOLD"` | The verdict. |
| `dispatch.reasons` | list of str | Why, one per tripped limit (or the single all-clear line). |
| `dispatch.thresholds` | object | The `DISPATCH` block in force. |
| `queues.lint_running`, `queues.lint_waiting`, `queues.lint_oldest_wait_s`, `queues.lint_priority_waiting` | int | Lint runs holding a slot, queued runs, the oldest queued wait in seconds, queued runs marked unblock priority. |
| `queues.test_running`, `queues.test_waiting`, `queues.test_oldest_wait_s` | int | The same for the Odoo test slots. |
| `remote_capacity.hosts` | list of object | `{name, state: free|busy|unreachable|reserved, load1, cores}`. Informational. |
| `remote_capacity.checked_epoch`, `.any_free`, `.note` | int or null, bool, str | When the hosts were last checked (only while the server is HOLD), whether any is free, and how to read it. |

Invariants: the verdict is about the local server only. Concurrency: one writer; readers see either the old or the new file.
Read by `evaluate`.

### `[@ANCHOR: dispatch_ok:evaluate]` `evaluate(path=DEFAULT_STATS, max_age=60.0, now=time.time)`

Purpose: the whole decision of `dispatch_ok.py`, separated from printing so it can be tested and imported.

| Argument | Type | Meaning |
|---|---|---|
| `path` | str | The stats file. |
| `max_age` | float | Seconds a file may be old before it counts as stale. |
| `now` | callable | Clock returning Unix seconds (tests pass a fixed one). |

Returns `(state, lines)`: `"OK"` or `"HOLD"` and the reasons, plus, on HOLD, one line naming free overflow machines. Never raises: a missing,
unreadable, non-object, undated, stale or verdict-less file is HOLD with its own reason. Read-only, no locks, safe to call in a loop.
`main` wraps it: prints the state and reasons (not with `--quiet`) and exits 0 for OK, 1 for HOLD.

## Pass stamps that carry over to a commit of records (`lint_stamp_gate.py`)

Written 2026-11-08. Every branch ends with a night-shift record (a history entry, a to-do claim or its notes, a claim file). The record is a new commit, so the
tip changed and the pass stamp of the run on the code was lost: a full re-lint (4 to 20 minutes queued) for a branch whose code had not moved. `devbox_tools/prmerge.sh`
now asks `lint_stamp_gate.py --inherit TIP PASSED_DIR origin/main` before it refuses a tip that has no stamp. The merge queue merges through `prmerge.sh`, so it follows.

### `[@ANCHOR: lint_stamp_gate:exempt_path]` `exempt_path(path)`

Purpose: the one list of paths whose change cannot alter what the linters concluded (`EXEMPT_PREFIXES`: `night_shift_history/`, `night_shift_todo/`, `claims/`, markdown files only). Argument `path`:
repository-relative, `/`-separated str. Returns bool. A path with `..`, a leading `/`, another directory, another suffix, or any other markdown file (`verify_anchors`, `check_skill_integrity` and the
documentation checkers read those) is not exempt. Pure. Tests: `InheritTests` in `test_lint_stamp_gate.py`.

### `[@ANCHOR: lint_stamp_gate:inherit_stamp]` `inherit_stamp(repo, tip, passed_dir, base="origin/main", log=print)`

Purpose: give `tip` the stamp of the nearest stamped commit below it when only exempt paths differ. Arguments: `repo` git directory; `tip` full commit id; `passed_dir` the stamp directory
(`~/.cache/hams-lint/passed`); `base` the ref the branch started from; `log` receives one line per refusal reason. Walks `base..tip` newest first (at most 60 commits), stops at the first commit with a
stamp. Returns the new stamp text (the old line plus ` inherited-from=<old commit>`; the old step list is still judged by `refusal` against the whole branch diff) or None when: no commit has a stamp,
any non-exempt file differs between the stamped commit and `tip` (a merge of main included), or `night_shift_history.check_tree` rejects the history of `tip` (the one checker that reads these
files). A stamp that already exists for `tip` is returned as it is, never replaced. Writes `passed_dir/<tip>` atomically; runs `git diff`, `git archive` and `tar`. Tests: `InheritTests`.

### `[@ANCHOR: lint_stamp_gate:inherited_note]` `inherited_note(stamp_text)`

Purpose: the loud line the gate prints for an inherited stamp, so the merge output says what the pass rests on. Returns a str naming the first 12 characters of the source commit, or None. Pure. Tests: `InheritTests`.
## Rust crates checked by what changed (`rust_impact.py`)

Written 2026-11-08. `check_cargo_clippy.py` and `check_cargo_fmt.py` checked every crate of both repositories whenever any `.rs`, `.toml` or `.lock` file changed: 17 crates one after another, 139 s on average
and 1,329 s at worst for clippy, the longest step of 25 of 142 measured runs. Under `--changed` the runner passes them the changed paths (`<repo_root> --changed-files PATH...`; no list for a full run, a run of
the checker tooling itself, or more than 5,000 paths) and they check only the crates with a changed file and the crates that depend on one by `path = ...`. Each skipped crate is printed with its reason and the
step prints `N of M crates checked`, so a skip is never silent. Cargo's build-directory lock still makes the crates take turns; the saving is the crates not built.

### `[@ANCHOR: rust_impact:split_argv]` `split_argv(argv)`

Purpose: read a checker's command line. `argv` is a list of str (without the program name). Returns `(positional, changed)`: the arguments before `--changed-files`, and the paths after it made absolute and resolved
(an empty list when the flag has no paths: nothing changed), or None when the flag is absent (check every crate). Pure. Tests: `DependencyTests` in `test_rust_impact.py`.

### `[@ANCHOR: rust_impact:path_dependencies]` `path_dependencies(crate_dir)`

Purpose: the directories a crate depends on by path. Reads `crate_dir/Cargo.toml` and returns the resolved absolute directories named by `path = ...` in `[dependencies]`, `[dev-dependencies]`,
`[build-dependencies]`, their `[target.*]` forms, `[workspace.dependencies]` and `[patch.*]`. Returns None when the file cannot be read or parsed, or the Python has no `tomllib` (before 3.11): the caller checks the
crate. Reads one file; no side effects. Tests: `DependencyTests`.

### `[@ANCHOR: rust_impact:select_crates]` `select_crates(crate_dirs, changed)`

Purpose: decide which crates to check. `crate_dirs`: list of crate directories; `changed`: resolved changed paths or None. Returns `(selected, skipped)`: `selected` is a list of the given directories, `skipped` a list of
`(directory, reason)`. A crate is selected when a changed path lies under it or under a directory it depends on by path (followed through those directories' manifests; cycles end), when a manifest cannot be read, or
for every crate when a changed `.rs` file, `rust-toolchain*` file or `.cargo/` file lies outside every crate (it may be `include!`d or pin the toolchain). `changed` None selects all. Reads manifests only. Tests:
`SelectionTests` and `EndToEndTests` (a fake `cargo` records where it was started) in `test_rust_impact.py`.

## Pytest steps run by what changed (`pytest_impact.py`)

Written 2026-11-08 (Bruce: "not checking unmodified files"). The `pytest tools/[NofM]`, `pytest tools/[host-bound]`, `pytest scripts/` and `pytest daemons/*` steps ran every test file under their root whenever any file under
it changed: the four `tools/` chunks took 80 to 180 s each with 600 s of pytest in a 700 s run (a hams_shared `--changed` run, 2026-11-08). Under `--changed` the runner now keeps the test files that depend on a changed file
(`run_linters._impact_select`) and balances the chunks over those. The base commit passes, so a test whose inputs did not change still passes: the premise of the step selection of `--changed`. A full run, a run scoped by module
names, `HAMS_LINT_TEST_IMPACT=0` and a change list longer than 5,000 paths keep every file. The run prints one line per narrowed step (`[*] pytest impact, tools/: 57 of 145 ...`) and the skipped file names, and the pass stamp
carries `impact=tools:57of145,...`. The dependency is generous: a file depends on a changed file when its code (comments and docstrings removed) holds the changed file's name, module name or top directory; after that first step
only imports and `x.py` script names pass the dependency on. A test that scans a directory from `__file__`, `HERE` or a tree root always runs. Everything runs when a `conftest.py`, `__init__.py`, `requirements*` or pytest
configuration file changed, when a changed file is mentioned by no file under the root (a new script, a deleted one, a data file no test names) and when a changed input of the step lies outside the root.

### `[@ANCHOR: pytest_impact:select_tests]` `select_tests(root, test_files, changed, also=())`

Purpose: split a pytest step's test files into the ones a change can alter and the rest. `root`: the step's directory; `test_files`: its test files (list of paths); `changed`: absolute changed paths (those under `root` or
`also` count); `also`: the step's other input paths (a daemon's loose files). Returns `(selected, skipped, why)`, the first two in the order of `test_files`, `why` one line for the step output. Selects every file, saying why,
for the cases above. Reads every `.py` file under `root` once (about 5 s for `tools/` with its 1,500 stub files). Tests: `SelectionTests` in `test_pytest_impact.py`.

## The relay's structural tests in lint (`relay_structural.py`)

Written 2026-11-08. Lint ran a pytest wrapper for `daemons/hams_local_relay` and none of the relay's cargo tests, so three relay merges turned relay main red that day: the demo_lock route table (#3474), the ELAD
fixture compiled in with `include_str!` (#3502) and the SNTP server (#3626: `sntp_service` missing from `docs/privilege_inventory.json`, `local_api.json` read by a bare name; two scan tests failed until #3654).
The step `relay structural tests` runs the fast group of source-scanning tests (`STRUCTURAL_FILTERS`, one list) with `cargo test --locked --features radioctl,shared_receiver --bin hams_local_relay`. It is selected when a file of the relay,
of a directory it depends on by path, or of a crate that depends on it (`watched_dirs`: `shack_console`, `relay_signer`) changes, so the merge queue's combined run, which is an ordinary `--changed` run, runs it for any such
branch; it is never cached, and the stamp lists it by name. Measured on the dev box (2026-11-08): cold build of all relay test binaries 2 min 6 s; the group warm, 139 tests, 17 s; the same sources under a new
checkout path rebuild the relay crate (2 min 13 s, the cost of the per-path fingerprint), the dependencies stay warm. At `3213a910d` (#3626) the group fails with exactly the two scan tests that failed on main. The whole suite
(`cargo test` of every target) runs nightly in the canary (`gate_canary.py`, `run_relay_suite`) and its failures make the night red.

### `[@ANCHOR: relay_structural:watched_dirs]` `watched_dirs(repo_root)`

Purpose: the directories whose change selects the step. Argument `repo_root`: str. Returns a sorted list of resolved absolute paths: the relay crate, every directory it depends on by `path = ...` (transitively) and
every crate in the repository that depends on it by path (`rust_impact.select_crates`); `[]` when the repository has no `daemons/hams_local_relay/Cargo.toml` (no step). Reads manifests only. Tests: `WatchedDirsTests` in `test_relay_structural.py`.

### `[@ANCHOR: relay_structural:cargo_command]` `cargo_command(filters=STRUCTURAL_FILTERS, list_only=False)`

Purpose: the command line. Returns a list: `cargo test --locked --features radioctl,shared_receiver --bin hams_local_relay -- [--list] <filter>...`. Each filter is a substring of a test path, as `cargo test` filters work. Pure. Tests: `PureTests`.

### `[@ANCHOR: relay_structural:listed_tests]` `listed_tests(text)`

Purpose: the test paths of `cargo test -- --list` output (`path: test` lines). Argument `text`: str. Returns a list of str. Pure. Tests: `PureTests`.

### `[@ANCHOR: relay_structural:unmatched_filters]` `unmatched_filters(tests, filters=STRUCTURAL_FILTERS)`

Purpose: keep the group from shrinking silently. Returns the filters that no test path of `tests` contains. The step lists the tests first and FAILS (exit 1, naming them) when this is not empty, before running anything: a renamed or deleted
module must be fixed in the list. Pure. Tests: `PureTests`, `MainTests`.

### `[@ANCHOR: relay_structural:passed_count]` `passed_count(text)`

Purpose: the number of tests that passed. Returns the sum of the `N passed` of every `test result:` line of `cargo test` output, 0 when there is none. Pure. Tests: `PureTests`.

The step's build goes to `lint_cache.named_cargo_session("relay-structural")`: one warm Cargo directory per lint slot (`-sK`), in RAM when `/dev/shm` has room, under the machine's disk budget, locked while it builds. Exit 0 and `relay structural tests: N tests of M filters
passed` on success; 1 and the test output on failure, on a build failure, or on a shrunken group; 0 and a line saying so when the repository has no relay crate.

### `[@ANCHOR: gate_canary:parse_cargo_failures]` `parse_cargo_failures(text)`

Purpose: read the nightly relay run. Returns `{"failed": [paths], "passed": N}`: each `test <path> ... FAILED` line (sorted, unique) and the sum of the `N passed` counts. Pure. Tests: `RelaySuiteTests` in `test_gate_canary.py`.

### `[@ANCHOR: gate_canary:relay_command]` `relay_command(relay_dir, target_dir)`

Purpose: the shell line of the nightly full suite: `cd <relay> && CARGO_TARGET_DIR=<target> CARGO_INCREMENTAL=0 cargo test --locked --features radioctl,shared_receiver --no-fail-fast` (every test of every target, all failures listed). Paths are shell-quoted. Pure. Tests: `RelaySuiteTests`.

### `[@ANCHOR: gate_canary:run_relay_suite]` `run_relay_suite(com_tree, log_directory, runner=None, now=time.time)`

Purpose: the nightly full relay suite of the canary, run on the snapshot's hams_com tree after the shards (`gate_canary.py run --no-relay` skips it). Returns `{"status", "failed", "passed", "exit_status", "seconds", "log"}`: `green` (exit 0), `red`
(non-zero and at least one `FAILED` test, named in `failed`), `error` (non-zero and none: it did not build or died; never green) or `skipped` with a `reason` (no tree or no relay crate; the note says `NOT RUN`). The build uses the budgeted directory
`lint_cache.named_cargo_session("relay-nightly")`. `aggregate(..., relay=)` records it as `status["relay"]`, adds each failed test as the new failure `relay cargo test: <path>` (so the night is red and the note names it; such ids are not proposed for
the Odoo known-red baseline and are not bisected) and a relay `error` as a shard error. Blocks until cargo ends (a cold build is a few minutes, the suite more); no process is signalled. Tests: `RelaySuiteTests`.
