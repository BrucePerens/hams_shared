# Story: Automated 60-Day Key Rotation

As a **System Administrator**,
I want the **API keys for all registered daemons to rotate automatically**,
so that the security risk of a leaked key is limited in time and I don't have to perform manual rotations.

## Scenario: Periodic Maintenance

1.  Odoo's scheduled actions runner (cron) triggers the `ir_cron_rotate_daemon_keys` job every day `daemon_key_manager:COMM_cron_rotation_trigger`.

2.  The job identifies all daemons whose keys were last rotated more than 59 days ago `daemon_key_manager:COMM_cron_rotation_logic`.
3.  For each eligible daemon, it:
    - Revokes the existing API key `daemon_key_manager:COMM_revoke_old_keys_logic`.

    - Generates a fresh 90-day API key `daemon_key_manager:COMM_generate_new_key_logic`.

    - Overwrites the existing `.env` file with the new key `daemon_key_manager:COMM_write_secure_env_file_logic`.
    - Updates the `last_rotated` timestamp.
4.  The external daemon, upon its next JSON-RPC (remote procedure call carrying JSON messages) call, may receive an `AccessError` (Odoo's permission-denied error) (it still holds the old key, which step 3 revoked).
5.  The daemon's error handler re-reads the `.env` file, acquires the new key, and retries the request successfully `zero_sudo:COMM_json_rpc_self_healing_retry`.

## Manual Rotation

If a specific daemon's credentials are suspected of being compromised, a manager can manually trigger a rotation for that specific daemon via the "Rotate Key" button on the registry form `daemon_key_manager:COMM_test_action_rotate_key`.

## Safety Safeguards

The system strictly prevents key rotation for service accounts (Odoo users that are programs, not people) that have been archived or disabled to prevent accidental reactivation of unauthorized access `daemon_key_manager:COMM_rotation_safety_archived_user`.

## Security Benefits
- Even if a backup of the `.env` file is stolen, the key will expire and be revoked within 60 days.
- The 90-day expiration on the Odoo side provides a 30-day buffer for the rotation to succeed.

## Additional Tests
- `daemon_key_manager:COMM_test_ui_rendering`

- `daemon_key_manager:COMM_test_action_rotate_key`

- `daemon_key_manager:COMM_test_rotation_safety_archived_user`

- `daemon_key_manager:COMM_test_cron_rotate_all_keys`

- `daemon_key_manager:COMM_test_key_ownership`

- `daemon_key_manager:COMM_test_security_constraints`

- `daemon_key_manager:COMM_test_register_daemon_api`

- `daemon_key_manager:COMM_test_force_provisioning`

- `daemon_key_manager:COMM_test_unauthorized_access`

- `daemon_key_manager:COMM_test_daemon_key_manager_tour`

## Anchor reference

### `daemon_key_manager:COMM_cron_rotation_trigger` [@ANCHOR: daemon_key_manager:COMM_cron_rotation_trigger]

**Purpose.** The scheduled action `ir_cron_rotate_daemon_keys` (file `daemon_key_manager/data/cron.xml`) is the clock that starts automatic key rotation. It calls `daemon.key.registry._cron_rotate_all_keys()` once a day, so every non-remote daemon key is renewed before the 90-day Odoo expiry. The anchor sits on the cron record, not in Python.

**Arguments and returns.** None: a cron record has no arguments and its method returns nothing.

**Special concerns.** The cron only starts the work; which registries are due is decided by `cron_rotation_logic` (threshold `ROTATION_AGE_DAYS = 59`). The method re-triggers the cron itself while a full batch of 10 remains. Tested by `test_cron_rotate_all_keys`. See also `revoke_old_keys_logic`, `generate_new_key_logic`.

### `daemon_key_manager:COMM_cron_rotation_logic` [@ANCHOR: daemon_key_manager:COMM_cron_rotation_logic]

**Purpose.** `daemon.key.registry._cron_rotate_all_keys()` is the body of the daily cron. It finds registries whose key is due and rotates each one, so a leaked key is useful for at most about 60 days.

**Signature.** `@api.model _cron_rotate_all_keys(self)`; no arguments, returns nothing.

**Selection.** Runs as the `user_daemon_key_manager_service` account (`with_user`, no `sudo`). It selects registries that are not Remote Self-Rotation, whose service account is active, and whose `last_rotated` is empty or older than `now - ROTATION_AGE_DAYS` (59 days). It takes at most 10, oldest first, and fetches their existing API keys in one query.

**Errors and side effects.** Each registry is rotated with `_rotate_key_and_write_file` and committed on its own. `OSError`, `UserError`, `ValidationError` and `AccessError` roll back that registry, are logged, and leave `last_rotated` untouched so the same registry is retried first on the next run; any other exception is also rolled back and logged with a traceback. A full batch of 10 re-triggers the cron through `_trigger()`. Writes key files on this host.

**See also.** `test_cron_rotate_all_keys`, `cron_rotation_trigger`, `revoke_old_keys_logic`.

### `daemon_key_manager:COMM_revoke_old_keys_logic` [@ANCHOR: daemon_key_manager:COMM_revoke_old_keys_logic]

**Purpose.** The first half of a rotation: remove the daemon's existing API keys so the old credential stops working. It is the block in `_rotate_key_and_write_file` that runs before a new key is minted.

**Behaviour.** The keys searched are the `res.users.apikeys` rows owned by the registry's service account whose name equals `<daemon name>_key` (at most 100). When the cron passes `pre_fetched_keys` the method filters that recordset instead of querying, after `exists()`, because an earlier registry in the same batch may already have unlinked rows. The rows are deleted with `unlink()`.

**Special concerns.** Access to other users' key rows comes from an `ir.model.access` rule and a record rule for the Manager group, not from `sudo`. The daemon holding the old key gets an `AccessError`/HTTP 401 on its next call and reloads its file (`zero_sudo:COMM_json_rpc_self_healing_retry`). Revocation and the new key are in one database transaction.

**See also.** Tests `test_cron_rotate_all_keys`, `test_key_ownership`, `test_rotating_again_revokes_the_previous_key`.

### `daemon_key_manager:COMM_generate_new_key_logic` [@ANCHOR: daemon_key_manager:COMM_generate_new_key_logic]

**Purpose.** The second half of a rotation: create a fresh API key for the daemon's service account, written to its `.env` file. It lives in `_rotate_key_and_write_file`, after the old keys are revoked.

**Behaviour.** It calls `_mint_key("<daemon name>_key")`, which runs `res.users.apikeys._generate("rpc", name, expiration)` as the service account itself, so the key is owned by that account and never by the superuser. The expiry is `now + key_lifetime_days` (default 90 days). The raw key is passed straight to `_write_secure_env_file`, then `last_rotated` is set to the current time.

**Special concerns.** The raw key exists only in memory and the file, and is never logged. The 90-day limit works because `_ensure_usage_group` first puts the account in the `group_daemon_key_usage` group (or the `_long` group for lifetimes over 90 days).

**See also.** Tests `test_key_ownership`, `test_cron_rotate_all_keys`; `revoke_old_keys_logic`, `write_secure_env_file_logic`.

### `daemon_key_manager:COMM_write_secure_env_file_logic` [@ANCHOR: daemon_key_manager:COMM_write_secure_env_file_logic]

**Purpose.** `_write_secure_env_file(path, login, key, group=None)` writes the daemon's credentials to disk. Every registration, rotation and provisioning path ends here.

**Arguments.** `path` (str): target file, resolved with `os.path.realpath` and required to lie under `/opt/hams/etc/keys/`. `login` (str): written as `ODOO_RPC_LOGIN`. `key` (str): raw API key, written as `ODOO_RPC_KEY`. `group` (str or None): a `hamsd_<family>` OS group; if given the file is mode 0640 with that group and must be in the family's own directory, otherwise it is 0600.

**Returns and errors.** Returns nothing. Raises `UserError` for a path outside the key tree, a group file in the wrong directory, a failed chown or failed permission enforcement; `OSError` is logged and re-raised.

**Special concerns.** Writes to a temporary file in the same directory (created with mode 0600 from the start) and renames it into place, so a reader never sees a partial or world-readable file. Directories are created 0710 (root), 0700 or 0750 (group). The key is never logged. Full details are in `hams_shared/docs/modules/daemon_key_manager.md`.

**See also.** Test `test_register_daemon_api`.

### `zero_sudo:COMM_json_rpc_self_healing_retry` [@ANCHOR: zero_sudo:COMM_json_rpc_self_healing_retry]

**Purpose.** Lets a daemon survive key rotation without a restart. When Odoo rejects the daemon's bearer key, the client reloads its `.env` file and repeats the request once.

**Where.** `SecureJSONRPCClient.call()` in `zero_sudo/daemon/json_rpc_client.py`.

**Behaviour.** If the JSON-2 response status is 401 or 403, it logs a warning, calls `_load_credentials()` to re-read `ODOO_RPC_LOGIN` and `ODOO_RPC_KEY` from the file, and sends the same request again with the new `Authorization: Bearer` key. Only the status code triggers the retry, never message text, so an unrelated 422 business error is not retried (it could duplicate a create or write). A second failure is raised as `RuntimeError`.

**Special concerns.** At most one retry per call; reading a file only, no secrets logged.

**See also.** Tests `test_call_self_healing` (zero_sudo) and `test_json_rpc_client.py`; `revoke_old_keys_logic`.

### `daemon_key_manager:COMM_rotation_safety_archived_user` [@ANCHOR: daemon_key_manager:COMM_rotation_safety_archived_user]

**Purpose.** A safety guard: rotation refuses to mint a key for a service account that has been archived. It stops a rotation or provisioning run from quietly reactivating access that an administrator switched off.

**Where.** `_assert_account_may_hold_key()` in `daemon_key_manager/models/key_registry.py`, called by `_rotate_key_and_write_file` before anything is revoked or written.

**Behaviour.** If `user_id.active` is false it raises `UserError("Cannot rotate key for archived service account: <login>")`. The same method also refuses the `__system__` user and any account in `base.group_system`. The cron skips archived accounts in its search, and `action_force_provision_all` logs and skips them (`force_provision_skips_archived`), so the error appears only on an explicit single rotation or registration.

**See also.** Test `test_rotation_safety_archived_user` in `tests/test_key_registry.py` creates an archived account and expects the `UserError`.

### `daemon_key_manager:COMM_test_ui_rendering` [@ANCHOR: daemon_key_manager:COMM_test_ui_rendering]

**Kind.** Odoo `TransactionCase` test `test_ui_rendering` in `daemon_key_manager/tests/test_key_registry.py`.

**What it checks.** It asks `ir.ui.view.get_view` for the `list` and `form` views of `daemon.key.registry` and asserts both come back non-empty. That proves the module's view XML (the list of daemons with their last-rotated date, and the form with the "Rotate Key" button) parses and loads under Odoo 19.

**Setup and cost.** No fixtures beyond the module being installed; no files are written and no keys are minted. It is fast.

**Failure meaning.** A broken view definition, a missing field referenced by a view, or a renamed model. It does not click anything; the interactive path is covered by `test_daemon_key_manager_tour`.

**See also.** `action_rotate_key_api`, `test_action_rotate_key`.

### `daemon_key_manager:COMM_test_action_rotate_key` [@ANCHOR: daemon_key_manager:COMM_test_action_rotate_key]

**Kind.** Odoo test `test_action_rotate_key` in `daemon_key_manager/tests/test_key_registry.py`; it tests `action_rotate_key_api`.

**What it checks.** A manager creates a registry "Single Rotation Test" for the fixture service account with the path `/opt/hams/etc/keys/single_rotation.env`, makes sure the file does not exist, calls `action_rotate_key()` as the manager and asserts the file now exists. A later test, `test_action_rotate_key_self_heals_a_service_account_missing_the_usage_group`, covers the usage-group regrant (`ensure_usage_group`).

**Failure meaning.** The manual rotation did not mint a key or write the file, or the group check rejected a legitimate manager.

**Cost.** One real file under the key directory (removed in teardown); no network.

**See also.** `test_unauthorized_access`, `test_ui_rendering`.

### `daemon_key_manager:COMM_test_rotation_safety_archived_user` [@ANCHOR: daemon_key_manager:COMM_test_rotation_safety_archived_user]

**Kind.** Odoo test `test_rotation_safety_archived_user` in `daemon_key_manager/tests/test_key_registry.py`; it tests `rotation_safety_archived_user`.

**What it checks.** It creates a service account `archived_svc` with `active = False`, a registry for it, and calls `_rotate_key_and_write_file()` as the manager. The test expects `UserError` whose message contains "archived".

**Failure meaning.** The guard in `_assert_account_may_hold_key` was removed or moved after the key was minted, so rotation could revive a disabled account.

**Cost.** Rows only. See also `force_provision_skips_archived` for the bulk path, where archived accounts are skipped rather than failing.

### `daemon_key_manager:COMM_test_cron_rotate_all_keys` [@ANCHOR: daemon_key_manager:COMM_test_cron_rotate_all_keys]

**Kind.** Odoo test `test_cron_rotate_all_keys` in `daemon_key_manager/tests/test_key_registry.py`; it tests `cron_rotation_logic` and `cron_rotation_trigger` (and, through the rotation, `revoke_old_keys_logic` and `generate_new_key_logic`).

**What it checks.** It creates a registry "Cron Test Daemon", runs `_cron_rotate_all_keys()` directly, then calls `_trigger()` on the `ir_cron_rotate_daemon_keys` record as the manager, and finally removes the registry. The assertions are that neither call raises; the selection rules (59-day threshold, batch of 10) are not asserted here.

**Cost.** One real file under the key directory. It needs the key directory to be writable by the test user.

**Failure meaning.** The cron method or the cron record is broken. TODO: add assertions on threshold selection and the failure-retry rule.

### `daemon_key_manager:COMM_test_key_ownership` [@ANCHOR: daemon_key_manager:COMM_test_key_ownership]

**Kind.** Odoo test `test_key_ownership` in `daemon_key_manager/tests/test_key_registry.py`; it tests `generate_new_key_logic`.

**What it checks.** It creates a service account `test_ownership_svc` and a registry "Ownership Test Daemon", rotates it as the manager, then selects `user_id` from `res_users_apikeys` where the name is `Ownership Test Daemon_key` and asserts it is the service account and is not the superuser (`base.user_root`). The companion `test_rotating_again_revokes_the_previous_key` rotates again and checks that the old key row is gone (`revoke_old_keys_logic`), which depends on the Manager record rule for service-account keys.

**Failure meaning.** Keys minted as the wrong user (a security defect) or old keys accumulating.

**Cost.** One real file, a few rows.

### `daemon_key_manager:COMM_test_security_constraints` [@ANCHOR: daemon_key_manager:COMM_test_security_constraints]

**Kind.** Odoo test `test_security_constraints` in `daemon_key_manager/tests/test_key_registry.py`; it tests `security_constraints_user` and `security_constraints_path`.

**What it checks.** It expects `UserError` for a regular (non-service) user as the target; for the path `/opt/jules/test.env`; for a symlink `evil.env` inside `/opt/hams/etc/keys/trusted` that points to `/etc/passwd` (message must contain "Security Alert"); and for a list of other forbidden locations such as `/usr/local/bin`, `/bin` and `/var/log`.

**Failure meaning.** A constraint was weakened: credentials could be written outside the key tree or bound to a human account.

**Cost.** Database rows; the symlink and directory are created in the real key tree and removed by teardown. The OS-group constraints have their own tests, for example `test_os_group_registry_constraint`.

### `daemon_key_manager:COMM_test_register_daemon_api` [@ANCHOR: daemon_key_manager:COMM_test_register_daemon_api]

**Kind.** Odoo test `test_register_daemon_api` in `daemon_key_manager/tests/test_key_registry.py`; it tests `register_daemon_api`, `register_daemon_logic`, `register_daemon_idempotency`, `write_secure_env_file_logic` and `privilege_escalation_bypass`.

**What it checks.** As a manager it calls `register_daemon("API Test Daemon", "daemon_key_manager.user_daemon_key_manager_service", "/opt/hams/etc/keys/api_test.env")`, asserts a true result, a registry row with that path, the `.env` file on disk, and that the service account is now in `group_daemon_key_usage`.

**Failure meaning.** Registration, the file write or the group grant broke.

**Cost.** One file in the real key directory, removed in teardown. Authorization failures are covered by `test_unauthorized_access`.

### `daemon_key_manager:COMM_test_force_provisioning` [@ANCHOR: daemon_key_manager:COMM_test_force_provisioning]

**Kind.** Odoo test `test_force_provisioning` in `daemon_key_manager/tests/test_key_registry.py`; it tests `action_force_provision_all_api`, `force_provision_logic` and `force_provision_error_handling`.

**What it checks.** A registry "Force Provision Test" is created for the fixture account, its key file is removed, a manager calls `action_force_provision_all()`, and the file must exist again. Companion tests cover a partial failure (`test_force_provisioning_partial_failure`: one daemon fails, the others still get keys, and the result is reported without a rollback) and archived accounts (`test_force_provision_skips_archived`).

**Failure meaning.** Bootstrapping would leave daemons without credentials.

**Cost.** Real files under the key directory, removed in teardown.

### `daemon_key_manager:COMM_test_unauthorized_access` [@ANCHOR: daemon_key_manager:COMM_test_unauthorized_access]

**Kind.** Odoo test `test_unauthorized_access` in `daemon_key_manager/tests/test_key_registry.py`; it tests the authorization checks of `action_force_provision_all_api` and `_rotate_key_and_write_file`.

**What it checks.** A manager creates a registry "Unauthorized Test". An ordinary user (`regular_user`) then calls `_rotate_key_and_write_file()` and `action_force_provision_all()`; each must raise `AccessError`.

**Failure meaning.** A user without the Daemon Key Management group could mint or revoke credentials, which would let any employee take over a daemon identity.

**Cost.** One row; no file is written because the check fails first.

### `daemon_key_manager:COMM_test_daemon_key_manager_tour` [@ANCHOR: daemon_key_manager:COMM_test_daemon_key_manager_tour]

**Kind.** Browser tour test `test_daemon_key_manager_tour` in `daemon_key_manager/tests/test_key_registry.py` (`HttpCase.start_tour`), tour `daemon_key_manager_tour`.

**What it checks.** The admin user, given debug mode and the Daemon Key Manager group, opens the Daemon Key Registry action in the web client and drives the registry list and form (including the "Rotate Key" button), exercising `register_daemon_api` and `action_force_provision_all_api` through the real UI.

**Failure meaning.** The menu, action, list/form views or button are missing or broken in the browser, even if the Python tests pass.

**Cost.** Slow: starts a headless browser; run with `test.py --wait -u daemon_key_manager`. It needs the test box key directory writable.

**See also.** `test_ui_rendering` (static view check).
