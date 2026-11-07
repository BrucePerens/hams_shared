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

Purpose: steps 4 to 10, in that order, on `start()`'s state. `os_id` is `"debian"` or `"ubuntu"`. Under `plan` the
database and the self-check are not run and every change is printed as a PLAN line (provision.py wraps the call in
`infrastructure.planning()`). Prints `<n> change(s) to this host; <m> step(s) skipped` and the skipped steps. Returns
the `Report`; raises `SystemExit(1)` when the self-check fails. Tests: `StartFinishTests`.
