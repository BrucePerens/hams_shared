# Journey: Lifecycle of a Daemon API Key

This journey describes the full lifecycle of a daemon's credentials, from registration to continuous rotation.

1.  **Birth: Registration**
    - A module calls `register_daemon()` `daemon_key_manager:COMM_register_daemon_api`.

    - `daemon_key_manager` creates a record and generates the first key `daemon_key_manager:COMM_register_daemon_logic`.

    - The key is written to a secure `.env` file `daemon_key_manager:COMM_write_secure_env_file_logic`.

2.  **Usage: Consumption**
    - The external daemon boots and reads `ODOO_RPC_KEY` from the file.
    - It uses this key for JSON-RPC (remote procedure call carrying JSON messages) authentication to Odoo.
    - Odoo validates the key against the service account (the Odoo user the daemon acts as).

3.  **Renewal: Periodic Rotation**
    - After 60 days, the cron job (Odoo's scheduled action) triggers `daemon_key_manager:COMM_cron_rotation_trigger`.

    - `daemon_key_manager` revokes the old key and generates a new one `daemon_key_manager:COMM_revoke_old_keys_logic` `daemon_key_manager:COMM_generate_new_key_logic`.
    - The `.env` file is updated with the new key.

4.  **Transition: Handover**
    - The daemon makes a call with the old key and receives an `AccessError` (Odoo's permission-denied error).
    - The daemon catches the error, re-reads the `.env` file `zero_sudo:COMM_json_rpc_self_healing_retry`.
    - The daemon retries with the new key and continues its work.

5.  **End of Life: Uninstallation**
    - If the daemon's module is uninstalled, its registry entry may be removed.
    - (Optional) A manual cleanup of the `.env` file can be performed by an admin.

## Anchor reference

### `daemon_key_manager:COMM_register_daemon_api` [@ANCHOR: daemon_key_manager:COMM_register_daemon_api]

**Purpose.** `daemon.key.registry.register_daemon()` is the API a module calls (usually from `post_init_hook`) to get a self-rotating API key for its daemon.

**Signature.** `@api.private @api.model register_daemon(self, daemon_name, user_xml_id, env_file_path, os_group=None)`.

| name | type | meaning |
|---|---|---|
| `daemon_name` | str | unique registry name; the key is named `<name>_key` |
| `user_xml_id` | str | XML id (`module.xmlid`) or login of the service account |
| `env_file_path` | str | absolute `.env` path under `/opt/hams/etc/keys/` |
| `os_group` | str or None | `hamsd_<family>` group allowed to read the file; None leaves it unchanged, empty string clears it |

**Returns.** `True`. **Errors.** `AccessError` when the caller is neither a Daemon Key Manager, an administrator or superuser, nor the service account itself, or when a non-manager passes `os_group`; `UserError` when the login is unknown or a constraint fails.

**Special concerns.** Runs as the key-manager service account (`with_user`, no `sudo`), flushes so constraints run before any file I/O, then mints the first key synchronously. See `register_daemon_logic`, `register_daemon_idempotency`; test `test_register_daemon_api`.

### `daemon_key_manager:COMM_register_daemon_logic` [@ANCHOR: daemon_key_manager:COMM_register_daemon_logic]

**Purpose.** The body of `register_daemon`: resolve the service account, find or create the registry row, then provision the first key and file.

**Steps.** (1) The account is found by XML id through `zero_sudo.security.utils._get_service_uid` or, when `user_xml_id` has no dot, by login. (2) The authorization check: a Daemon Key Manager may register anything; any other service account only itself; otherwise an administrator or superuser is required. (3) The registry is searched by name and company of the account, created if missing, written if present. (4) `env.flush_all()` triggers the `@api.constrains` checks (service account, path, OS group) before any file is touched. (5) `_rotate_key_and_write_file()` mints and writes the key.

**Errors.** `AccessError`, `UserError` as in `register_daemon_api`. Multi-company: the row belongs to the account's company.

**See also.** `register_daemon_idempotency`, `security_constraints_user`, `security_constraints_path`; test `test_register_daemon_api`.

### `daemon_key_manager:COMM_write_secure_env_file_logic` [@ANCHOR: daemon_key_manager:COMM_write_secure_env_file_logic]

**Purpose.** `_write_secure_env_file(path, login, key, group=None)` writes the daemon's credentials to disk. Every registration, rotation and provisioning path ends here.

**Arguments.** `path` (str): target file, resolved with `os.path.realpath` and required to lie under `/opt/hams/etc/keys/`. `login` (str): written as `ODOO_RPC_LOGIN`. `key` (str): raw API key, written as `ODOO_RPC_KEY`. `group` (str or None): a `hamsd_<family>` OS group; if given the file is mode 0640 with that group and must be in the family's own directory, otherwise it is 0600.

**Returns and errors.** Returns nothing. Raises `UserError` for a path outside the key tree, a group file in the wrong directory, a failed chown or failed permission enforcement; `OSError` is logged and re-raised.

**Special concerns.** Writes to a temporary file in the same directory (created with mode 0600 from the start) and renames it into place, so a reader never sees a partial or world-readable file. Directories are created 0710 (root), 0700 or 0750 (group). The key is never logged. Full details are in `hams_shared/docs/modules/daemon_key_manager.md`.

**See also.** Test `test_register_daemon_api`.

### `daemon_key_manager:COMM_cron_rotation_trigger` [@ANCHOR: daemon_key_manager:COMM_cron_rotation_trigger]

**Purpose.** The scheduled action `ir_cron_rotate_daemon_keys` (file `daemon_key_manager/data/cron.xml`) is the clock that starts automatic key rotation. It calls `daemon.key.registry._cron_rotate_all_keys()` once a day, so every non-remote daemon key is renewed before the 90-day Odoo expiry. The anchor sits on the cron record, not in Python.

**Arguments and returns.** None: a cron record has no arguments and its method returns nothing.

**Special concerns.** The cron only starts the work; which registries are due is decided by `cron_rotation_logic` (threshold `ROTATION_AGE_DAYS = 59`). The method re-triggers the cron itself while a full batch of 10 remains. Tested by `test_cron_rotate_all_keys`. See also `revoke_old_keys_logic`, `generate_new_key_logic`.

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

### `zero_sudo:COMM_json_rpc_self_healing_retry` [@ANCHOR: zero_sudo:COMM_json_rpc_self_healing_retry]

**Purpose.** Lets a daemon survive key rotation without a restart. When Odoo rejects the daemon's bearer key, the client reloads its `.env` file and repeats the request once.

**Where.** `SecureJSONRPCClient.call()` in `zero_sudo/daemon/json_rpc_client.py`.

**Behaviour.** If the JSON-2 response status is 401 or 403, it logs a warning, calls `_load_credentials()` to re-read `ODOO_RPC_LOGIN` and `ODOO_RPC_KEY` from the file, and sends the same request again with the new `Authorization: Bearer` key. Only the status code triggers the retry, never message text, so an unrelated 422 business error is not retried (it could duplicate a create or write). A second failure is raised as `RuntimeError`.

**Special concerns.** At most one retry per call; reading a file only, no secrets logged.

**See also.** Tests `test_call_self_healing` (zero_sudo) and `test_json_rpc_client.py`; `revoke_old_keys_logic`.
