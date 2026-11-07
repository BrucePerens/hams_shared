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

### `[@ANCHOR: test:missing_host_apt_packages]` `missing_host_apt_packages(pkg_specs, os_id, dpkg_installed, pip_installed)`

Purpose: decides which MANIFEST apt packages a host lacks. A pure function so the decision can be tested without dpkg;
`check_host_apt_packages()` is its only production caller.

| Argument | Type | Meaning |
|---|---|---|
| `pkg_specs` | list of dict | `MANIFEST["apt_packages"]` entries: `name`, optional `debian_name`. |
| `os_id` | str | `get_os_identifier()`: `"debian"` selects `debian_name` (falling back to `name`); anything else uses `name`. |
| `dpkg_installed` | callable(str) -> bool | True when dpkg reports the package `install ok installed`. |
| `pip_installed` | callable(str) -> bool | True when the pip distribution is installed for the running interpreter. |

Returns the missing package names as a list of str, in manifest order (empty when nothing is missing). A package counts as
present when dpkg has it, or when it is a key of `PIP_FALLBACK_APT_PACKAGES` and `pip_installed(value)` is true; pip is never
consulted for any other package. Raises whatever the callables raise (`check_host_apt_packages()` treats `FileNotFoundError`
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
`missing_host_apt_packages()`.

Special concerns: blocking, about 60 short subprocesses; read-only; no locks. Before 2026-11-07 it demanded `awscli` from apt,
so every non-isolated run on Ubuntu 24.04 stopped here even after provisioning had installed the pip `awscli`. See also
`MissingHostAptPackagesTests` in `test_test.py`.

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
