# Story: Registering a New External Daemon

As a **Developer** of an external Odoo integration (like a monitoring service or a backup manager),
I want to **register my daemon** (a long-running background program) with the Odoo instance,
so that it receives a secure, auto-rotating API key without me having to manage credentials manually.

## Scenario: Initial Module Installation

1.  The developer creates a new Odoo module for their daemon.
2.  In the module's `post_init_hook` (code Odoo runs once after the module is installed) or a configuration wizard, they call the `register_daemon` API `daemon_key_manager:COMM_register_daemon_api`.

3.  The `daemon_key_manager` verifies the service account and the requested file path `daemon_key_manager:COMM_register_daemon_logic`.
4.  A new entry is created in the `daemon.key.registry`.
5.  An initial API key is generated and securely written to the specified `.env` file (a text file of environment variables) on disk `daemon_key_manager:COMM_write_secure_env_file_logic`.
6.  The daemon can now boot and read its credentials from `/opt/hams/etc/keys/my_daemon.env`.

## Key Logic
- The registration is idempotent; calling it again updates the existing registration `daemon_key_manager:COMM_register_daemon_idempotency`.

- Security constraints ensure that only service accounts can be used `daemon_key_manager:COMM_security_constraints_user` and files are written to allowed paths `daemon_key_manager:COMM_security_constraints_path`.

- Automated privilege assignment ensures the daemon has the correct permissions for long-lived keys `daemon_key_manager:COMM_privilege_escalation_bypass`. ("Long-lived" means the 90-day keys that [key_rotation.md](key_rotation.md) replaces every 60 days; "service account" is the Odoo user kind catalogued in [service_accounts.md](../service_accounts.md).)

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

### `daemon_key_manager:COMM_register_daemon_idempotency` [@ANCHOR: daemon_key_manager:COMM_register_daemon_idempotency]

**Purpose.** Registration can be repeated safely: a module's `post_init_hook` runs again on every upgrade and must not fail or create duplicates.

**Behaviour.** In `register_daemon`, when a registry with the same `name` and company already exists, the method calls `write()` on it with the new `user_id`, `env_file_path`, `company_id` (and `os_group` when given) instead of `create()`. The name is also unique per company at the database level. Afterwards the normal rotation runs, so repeating the call issues a fresh key, revokes the earlier one and rewrites the file.

**Special concerns.** Because a repeat call rotates the key, a daemon already running with the old key recovers through its self-healing retry. Changing `env_file_path` moves the key file but the old file is left on disk for an operator to remove.

**See also.** `register_daemon_logic`; test `test_register_daemon_api`.

### `daemon_key_manager:COMM_security_constraints_user` [@ANCHOR: daemon_key_manager:COMM_security_constraints_user]

**Purpose.** A model constraint guaranteeing that a daemon registry is only ever bound to a service account, never to a human user or an administrator.

**Where.** `_check_user_is_service_account`, an `@api.constrains("user_id")` method on `daemon.key.registry`.

**Behaviour.** For each record in the set, if `user_id.is_service_account` is false it raises `UserError("The selected user must be a service account.")`. It runs at create and write, and `register_daemon` flushes before writing any file so the error aborts the whole transaction before credentials reach disk.

**Why.** A long-lived 90-day key must never be given to an account with human privileges.

**See also.** Test `test_security_constraints` (also covers `security_constraints_path`); `register_daemon_logic`.

### `daemon_key_manager:COMM_security_constraints_path` [@ANCHOR: daemon_key_manager:COMM_security_constraints_path]

**Purpose.** A model constraint that keeps every key file inside the managed key tree, blocking path traversal and symlink tricks.

**Where.** `_check_env_file_path`, an `@api.constrains("env_file_path")` method.

**Behaviour.** An empty path is skipped. A path containing a `..` component raises `UserError("Security Alert: Directory traversal detected in path.")`. Otherwise the path is normalised and resolved with `os.path.realpath`; the resolved path must start with `/opt/hams/etc/keys/` or `UserError` names the required prefix and the resolved path. `_write_secure_env_file` repeats the check at write time.

**Special concerns.** No file is touched by the check. The key tree is mode 0710, so only the Odoo user can list it.

**See also.** Test `test_security_constraints`; `write_secure_env_file_logic`; `security_constraints_user`.

### `daemon_key_manager:COMM_privilege_escalation_bypass` [@ANCHOR: daemon_key_manager:COMM_privilege_escalation_bypass]

**Purpose.** Grants the service account the key-usage group that allows long-lived API keys, without needing an administrator or `sudo`.

**Where.** `_grant_usage_group(user, usage_group)`, called by `_ensure_usage_group` on every rotation.

**Behaviour.** If the group is not already in `user.group_ids`, it runs `INSERT INTO res_groups_users_rel (uid, gid) VALUES (%s, %s) ON CONFLICT DO NOTHING`, then invalidates the user's recordset and clears the registry cache. Raw SQL is used because writing `group_ids` through the ORM needs `base.group_erp_manager`, which the key-manager service account deliberately lacks (the zero-sudo rule).

**Special concerns.** It is the only place that bypasses ORM access control; the group it grants is only `group_daemon_key_usage` (90-day) and `group_daemon_key_usage_long`. The insert lives in the caller's transaction, so a rejected rotation rolls it back.

**See also.** `generate_new_key_logic`; tests `test_register_daemon_api`, `test_force_provisioning`.
