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
mirror (`hams_open/hams_shared` synced from the worktree) is the same shape. Batching (`lint_batch.py`) stays off for hams_shared
(`is_shared_checkout`): a combined tree of hams_shared would be linted by the same code, one tree per run.

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
of the process (the lock goes with the process, however it ends); a file whose lock was not taken is closed. The lock file is opened with
`O_NOFOLLOW`, falling back to read-only when `fs.protected_regular` refuses a write open (`_open_lock_file`). With `slots` of 1 this is the
single flock `test.py` always took.

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
| `base_tmpdir` | str or `None` | The TMPDIR whose `slot-K` directories are swept. |
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
