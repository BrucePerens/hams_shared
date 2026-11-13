# Daemon Key Manager (`daemon_key_manager`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

The **Daemon Key Manager** is the centralized authority for managing Odoo API keys for external background services (daemons). It implements the **Service Account Pattern** (a dedicated, narrowly scoped `res.users` record per daemon, flagged `is_service_account`; see [zero_sudo.md](zero_sudo.md) and [service_accounts.md](../service_accounts.md)) by generating native Odoo API keys and exporting them to highly restricted local `.env` files, eliminating the need for hardcoded credentials or manual token rotation. This module is a critical component of the **Zero-Sudo Architecture**, ensuring that background processes operate with minimum privilege and without human intervention for credential management.

## 🚀 Quick Start: Integration API

Other modules should request daemon credentials during their installation (e.g., in a `post_init_hook`) or via a configuration wizard.

```python
def setup_daemon_credentials(env):
    # Idempotent registration and synchronous key generation
    # This call ensures the daemon is registered for 60-day rotations.
    env['daemon.key.registry'].register_daemon(
        daemon_name="My External Daemon",
        user_xml_id="my_module.my_service_account",
        env_file_path="/opt/hams/etc/keys/my_daemon.env"
    )
```

## 🛡️ Security Architecture

### Zero-Sudo Compliance
The module operates under the `user_daemon_key_manager_service` account and does not use `.sudo()`. API-key allocation and revocation, which Odoo normally restricts to administrators, run through `with_user()` as that service account (and, for the daemon's own account, its explicit ACLs), as `key_registry.py` states: "with_user and explicit ACLs remove the need for sudo".

### OS-Level Sandboxing
* **Strict Permissions:** `.env` files are created with `0600` (read/write only for the Odoo server process user).
* **Directory Isolation:** Subdirectories are created with `0700` to prevent other users on the system from traversing into the key storage area. The root `/opt/hams/etc/keys` is `0710` (its group may traverse but not list it). A key file may also be handed to one OS group: a registry's optional `os_group` (which must match `hamsd_<family>`, and which only a Daemon Key Manager, an administrator or the superuser may set) makes the file `0640` with that group, kept in its own family directory `/opt/hams/etc/keys/<family>` (`0750`), so that daemon's account, and no other, can read it.
* **Path Validation:** All paths MUST start with `/opt/hams/etc/keys/`. The module strictly blocks directory traversal (`..`) and symlink attacks by resolving the `os.path.realpath` of the requested path before performing any file operations `security_constraints_path`.

* **System Directory Protection:** There is no list of forbidden system directories. The file writer re-checks, on the symlink-resolved path, the same `/opt/hams/etc/keys/` prefix rule, so a path under `/etc`, `/root` or anywhere else outside that tree is refused for that reason alone [@ANCHOR: write_secure_env_file_logic].

### Automated Key Rotation
An `ir.cron` job runs daily and rotates each registered key that is more than 59 days old, so a key is replaced about every 60 days `cron_rotation_trigger`. A registry marked Remote Self-Rotation (a daemon on another machine whose key file Odoo cannot write) is skipped by the cron, by `action_force_provision_all()` and by `action_rotate_key()`; that daemon calls `rotate_own_key(daemon_name, current_key)` itself, in two phases: it is issued a new key once its key is more than 59 days old, and when it presents the new key the old one is revoked.

* **Graceful Failure:** Stateless batching (the cron processes 10 records at a time and re-triggers itself) ensures that one failed file-write or database error does not block other rotations. Failures are logged, and the system attempts to continue with the next daemon `cron_rotation_logic`.
* **Buffer Period:** New keys are generated with a 90-day expiration by default (a registry's `key_lifetime_days` may set 90 to 400 days; rotation is then due 31 days before expiry), providing a margin of about 30 days for the rotation to succeed in case of transient server issues.
* **Self-Healing Daemons:** Daemons utilizing these keys MUST be designed to catch `AccessError` responses from Odoo, re-read their assigned `.env` file from the disk, and retry the request. This ensures continuous operation across key rotations `daemon_self_healing`.

---

## 🛠️ Technical Reference

### 1. Storage & Orchestration Mandate
All credentials **MUST** be written to `/opt/hams/etc/keys/`.
In containerized/orchestrated environments:
* **Odoo Container:** Mount the volume as **Read/Write**.
* **Daemon Containers:** Mount the volume as **Read-Only**.

### 2. Core API Methods

#### `register_daemon(daemon_name, user_xml_id, env_file_path)` [@ANCHOR: register_daemon_api]
* **`daemon_name`**: A unique string identifier for the external service.
* **`user_xml_id`**: The XML ID of the service account record (e.g., `pager_duty.user_pager_service_internal`). This account must have `is_service_account` set to `True`.
* **`env_file_path`**: The absolute path where the `.env` file should be written. It must reside within `/opt/hams/etc/keys/`.
* **`os_group`** (optional): the `hamsd_<family>` OS group that may read the key file (see Directory Isolation above).
* **Behavior**: This method is idempotent. If a daemon with the same name exists, its service account and path are updated. It immediately triggers the generation of the first API key and writes the file `register_daemon_logic` `register_daemon_idempotency`.

#### `action_rotate_key()`
* **Use Case**: Manually rotate one daemon's key from the UI or code. It revokes the existing key and generates a new one at once. Restricted to the `Daemon Key Management / Manager` group.

#### `action_force_provision_all()` [@ANCHOR: action_force_provision_all_api]
* **Use Case**: Used during system bootstrapping (e.g., via systemd or Kubernetes init containers) to ensure all keys are present on disk before daemons start. Also used for emergency rotation of all keys.
* **Shell Invocation**:
  ```bash
  odoo-bin shell -d hams --no-http -e "env['daemon.key.registry'].action_force_provision_all(); env.cr.commit()"
  ```
* **Security**: Only accessible to users in the `Daemon Key Management / Manager` group. Internally, it elevates to the service account to perform the privileged key generation `force_provision_logic`.

### 3. File Format (.env) [@ANCHOR: write_secure_env_file_logic]
The file holds the service account's login and its API key; the daemon reads both at start and again after an `AccessError`.

**Purpose.** `_write_secure_env_file(path, login, key, group=None)` is the one place that writes a daemon's credential to disk. It is called by `_rotate_key_and_write_file` after a new key is minted, and by every registration, rotation and provisioning path.

**Arguments.** `path`: target file, which must resolve under `/opt/hams/etc/keys/`. `login`: the service account's login, written as `ODOO_RPC_LOGIN`. `key`: the raw API key, written as `ODOO_RPC_KEY`. `group`: optional OS group name (`hamsd_<family>`); when given the file is mode 0640 with that group and must live in that group's own key directory, otherwise it is 0600.

**Returns.** Nothing.

**Errors.** `UserError` when the path is outside the key directory, when a group's file is not in its own directory, when the Odoo server user cannot chown the directory or file to the group (the message says provisioning adds it to the group and the services need a restart), or when permissions cannot be enforced. `PermissionError` and `OSError` are logged and re-raised.

**Special concerns.** Creates the directory if missing (root 0710, other directories 0700, group directories 0750) and re-applies the mode. The file is written to a temporary file created in the same directory with `mkstemp` and mode 0600 from the start, filled, flushed, and moved into place with `os.rename`, so a reader never sees a partial file and the key is never world-readable; on any failure the temporary file is removed. The key is never logged. Writes to this host's disk only.

**See also.** Tests `test_register_daemon_api`, `test_write_secure_env_file_exceptions` and `test_write_secure_env_file_refuses_to_write_a_credential_when_fchmod_fails`.

File layout:
```env
# Auto-generated by daemon.key.registry
ODOO_RPC_LOGIN=service_account_login
ODOO_RPC_KEY=12345abcd...
```


---

## 📖 Stories & Journeys

* [Registering a New External Daemon](hams_shared/docs/stories/daemon_registration.md)
* [Manual Force Provisioning](hams_shared/docs/stories/force_provisioning.md)
* [Automated 60-Day Key Rotation](hams_shared/docs/stories/key_rotation.md)
* [Lifecycle of a Daemon API Key](hams_shared/docs/journeys/api_key_lifecycle.md)
* [Zero-Sudo Refactoring Story](hams_shared/docs/stories/zero_sudo_refactoring.md)

---

## 🧪 Verification
* **register_daemon_api**: Verified by `COMM_test_register_daemon_api`

* **force_provision_all**: Verified by `COMM_test_force_provisioning`

* **security_constraints**: Verified by `COMM_test_security_constraints`

* **ui_tour**: Verified by `COMM_test_daemon_key_manager_tour`

* **unauthorized_access**: Verified by `COMM_test_unauthorized_access`

* **key_ownership**: Verified by `COMM_test_key_ownership`

* **documentation_installed**: Verified by `COMM_documentation_installed` `test_documentation_installed`

## Anchor reference

### `security_constraints_path` [@ANCHOR: security_constraints_path]

**Purpose.** The constraint on `daemon.key.registry.env_file_path` that confines key files to the key directory and refuses directory traversal. A key file is a credential, so where it may be written is a security decision.

**Interface.** An `@api.constrains("env_file_path")` method on each record, run on create and write. No arguments; it returns nothing.

**Rules.** An empty path is skipped. A path with a `..` component raises `UserError` ("Security Alert: Directory traversal detected in path."). The path is normalised and resolved with `os.path.realpath`, so symbolic links are followed, and the real path must start with `/opt/hams/etc/keys/`, otherwise `UserError` ("The environment file path must start with ...", naming the resolved path).

**Special concerns.** Synchronous and read-only (it touches the filesystem only to resolve links). The same prefix is checked again in `_write_secure_env_file` when the file is written.

**See also.** Tests `test_security_constraints` and `test_env_file_path_rejects_literal_directory_traversal` in `daemon_key_manager/tests/test_key_registry.py`.

### `cron_rotation_trigger` [@ANCHOR: cron_rotation_trigger]

**Purpose.** The scheduled action that rotates daemon API keys automatically, so no daemon key lives longer than about two months.

**Definition.** `daemon_key_manager/data/cron.xml`, `ir.cron` record `ir_cron_rotate_daemon_keys`, named "Daemon Vault: Rotate API Keys". It runs as the module's service account `user_daemon_key_manager_service`, state `code`, with code `model._cron_rotate_all_keys()`, every 1 day, first call one day after installation, active. It is `noupdate="1"`, so an administrator's edits survive upgrades.

**Behaviour.** Each daily run rotates the registries that are due (see `cron_rotation_logic`); the work is batched, and the cron re-triggers itself while a full batch was processed. The docs say "every 60 days"; the code's threshold is `ROTATION_AGE_DAYS = 59`, so a key is rotated by the first run after 59 days.

**See also.** Test `test_cron_rotate_all_keys`; story `key_rotation`.

### `cron_rotation_logic` [@ANCHOR: cron_rotation_logic]

**Purpose.** `_cron_rotate_all_keys()` on `daemon.key.registry`: the body of the scheduled rotation.

**Interface.** `@api.model`, no arguments, returns nothing. It switches to the service account (`_get_service_uid` of `daemon_key_manager.user_daemon_key_manager_service`) and selects up to 10 registries that are not marked `remote_self_rotation`, whose service account is active, and whose `last_rotated` is empty or older than 59 days, oldest first. It fetches their existing API keys once, then rotates each with `_rotate_key_and_write_file(pre_fetched_keys=...)` and commits after each.

**Errors.** A failure for one daemon (`OSError`, `UserError`, `ValidationError`, `AccessError`) is logged as a managed failure after a rollback; any other exception is logged with a traceback, also after a rollback. Neither stops the batch.

**Special concerns.** Stateless batching: when exactly 10 were selected it calls `_trigger()` on the cron record to run again at once. It commits inside the loop, which is why a failure only loses one daemon's work. Writes key files on this host.

**See also.** `cron_rotation_trigger`, test `test_cron_rotate_all_keys`.

### `daemon_self_healing` [@ANCHOR: daemons:daemon_self_healing]

**Purpose.** The rule that a daemon using a registry key must survive that key being rotated while it runs.

**Rule.** Because rotation revokes the old key and writes a new one to the daemon's environment file, a daemon that receives an `AccessError` (or an authentication failure) from Odoo must re-read its `.env` file from disk and retry the request. This lets rotation happen without a restart.

**Interface.** None; it is a convention on every daemon that consumes a key, not a function. It has no arguments and returns nothing.

**Unknown.** No code in `daemon_key_manager` carries this anchor; each daemon implements the behaviour itself. To-do: list the daemons that do, and add a shared helper and a test.

**See also.** `cron_rotation_logic`, `COMM_action_rotate_key_api`.

### `register_daemon_logic` [@ANCHOR: register_daemon_logic]

**Purpose.** The body of `register_daemon(daemon_name, user_xml_id, env_file_path, os_group=None)`, the API other modules use to enrol a daemon and get its first key at once.

**Arguments.** `daemon_name`: registry record name (unique per company). `user_xml_id`: XML id of the service account, or its login as a fallback. `env_file_path`: where the key file is written (must be under `/opt/hams/etc/keys/`). `os_group`: optional `hamsd_<family>` group so the file is written 0640 with that group (otherwise 0600); `None` leaves an existing value, an empty string clears it.

**Returns.** `True`.

**Errors.** `UserError` for an unknown service account. `AccessError` when the caller is not a key manager, administrator or superuser and is not a service account registering itself; when a service account registers a different account; and when someone other than a key manager, administrator or superuser tries to set `os_group`.

**Special concerns.** `@api.private`, so it is not reachable over RPC. It runs as the module's service account, creates or updates the registry, flushes, then calls `_rotate_key_and_write_file()`: it revokes the old key, mints a new one and writes the file. Idempotent (see `register_daemon_idempotency`).

**See also.** Test `test_register_daemon_api`.

### `register_daemon_idempotency` [@ANCHOR: register_daemon_idempotency]

**Purpose.** Registering the same daemon again must update the existing registry record and not create a duplicate, so modules can call `register_daemon` from every install and upgrade.

**Behaviour.** `register_daemon` searches for a registry with the same name and company as the service account. If found it is written with the new user, path and company (and `os_group` when given); if not, one is created. Either way the key is rotated and written, so each call yields a new valid key.

**Special concerns.** The search is by `(name, company_id)`, so the same daemon name in two companies is two records. Not safe to call concurrently for the same name (a race could create two); calls come from module install hooks, which are serial.

**See also.** `register_daemon_logic`, test `test_register_daemon_api`.

### `force_provision_logic` [@ANCHOR: force_provision_logic]

**Purpose.** The body of `action_force_provision_all()`: synchronously create or refresh the key of every registered daemon, intended to run from `odoo-bin shell` while systemd is bootstrapping, so daemons find their key files at start-up and do not race the scheduled rotation.

**Interface.** No arguments. Only the superuser or a Daemon Key Manager may call it (`AccessError` "Only Daemon Key Managers can provision keys." otherwise). It runs as the service account, takes up to 1000 registries that are not `remote_self_rotation`, skips those whose service account is archived (logged), fetches existing keys once, and rotates each inside its own savepoint.

**Returns.** `None` when all succeeded; when some failed, a sticky `display_notification` action titled "Some keys were not provisioned" naming the failed daemons and the count of successes.

**Special concerns.** A failure of one daemon (`UserError`, `ValidationError`, `AccessError`, `OSError`) is logged and does not undo or hide the others. Writes key files on this host.

**See also.** `COMM_action_force_provision_all_api`, test `test_force_provisioning`.

### `COMM_test_register_daemon_api` [@ANCHOR: COMM_test_register_daemon_api]

**Purpose.** The test of the public registration call that other modules use to enrol a daemon in the key registry.

**Scenario.** `test_register_daemon_api` calls `register_daemon(name, service_user_xml_id, env_file_path)` as a key manager with the name "API Test Daemon" and a path under `/opt/hams/etc/keys/`. It asserts a truthy result, a registry record with that path, that the environment file now exists on disk, and that the service user was put in the usage group `daemon_key_manager.group_daemon_key_usage`.

**What it guards.** `COMM_register_daemon_api`, `COMM_register_daemon_logic`, `COMM_register_daemon_idempotency`, `COMM_write_secure_env_file_logic` and `COMM_privilege_escalation_bypass`.

**Special concerns.** The test writes a real file under the key directory, so the test box needs that directory writable. It also supports the UI tour test.

### `COMM_test_force_provisioning` [@ANCHOR: COMM_test_force_provisioning]

**Purpose.** The test of the bootstrap call that provisions a key for every registered daemon before the daemons start.

**Scenario.** `test_force_provisioning` registers "Force Provision Test" with `env_file_path=/opt/hams/etc/keys/force_provision.env`, removes any old file, calls `action_force_provision_all()` as a key manager, and asserts the file now exists.

**What it guards.** `COMM_action_force_provision_all_api`, `COMM_force_provision_logic` and `COMM_force_provision_error_handling`. Sibling tests check that one failing daemon neither undoes nor hides the daemons that succeeded, and that a removed daemon's archived account is skipped.

**Special concerns.** Writes a real file under the key directory; the call is meant for `odoo-bin shell` during systemd start-up.

### `COMM_test_security_constraints` [@ANCHOR: COMM_test_security_constraints]

**Purpose.** The test that proves a registry record cannot name a human user as the key owner and cannot use an environment file outside the permitted key directory.

**Scenario.** `test_security_constraints` (in `daemon_key_manager/tests/test_key_registry.py`) creates a `daemon.key.registry` as a manager, with a regular, non-service user as `user_id`, and expects a `UserError`. Sibling tests cover directory traversal in `env_file_path` (`test_env_file_path_rejects_literal_directory_traversal`) and a narrow service account (`test_check_user_is_service_account_as_a_narrow_service_account`).

**What it guards.** The constraint `COMM_security_constraints_user` (the key owner must be a service account) and `COMM_security_constraints_path` (the path must resolve under `/opt/hams/etc/keys/` with no `..` component).

**Special concerns.** Runs as an Odoo `TransactionCase`: it needs the test database and writes no real key.

### `COMM_test_daemon_key_manager_tour` [@ANCHOR: COMM_test_daemon_key_manager_tour]

**Purpose.** The browser (tour) test that drives the Daemon Key Manager screens as an administrator, the user-interface counterpart to the model tests.

**Scenario.** `test_daemon_key_manager_tour` gives the admin user the Technical Features group and the Daemon Key Manager group, sets the language to `en_US`, then runs the JavaScript tour `daemon_key_manager_tour` with `start_tour`. It is the end-to-end check of registration and force provisioning.

**What it guards.** `COMM_register_daemon_api` and `COMM_action_force_provision_all_api` from the UI.

**Special concerns.** Needs a headless browser and the HTTP test server, so it is slower than the model tests and cannot run in a plain unit run.

### `COMM_test_unauthorized_access` [@ANCHOR: COMM_test_unauthorized_access]

**Purpose.** The test that a user without the Daemon Key Manager group cannot rotate or provision keys.

**Scenario.** `test_unauthorized_access` creates a registry as a manager, then as a regular user expects an `AccessError` from `_rotate_key_and_write_file()` and from `action_force_provision_all()`.

**What it guards.** The group checks in the rotation and force-provision paths (`COMM_action_rotate_key_api`, `COMM_action_force_provision_all_api`). The superuser, used by the systemd shell call, is the only other permitted caller.

**Special concerns.** No key is produced when the check fails, so nothing is written to disk. Runs as an Odoo `TransactionCase`.

### `COMM_test_key_ownership` [@ANCHOR: COMM_test_key_ownership]

**Purpose.** The test that a generated API key belongs to the daemon's service account and not to the superuser.

**Scenario.** `test_key_ownership` creates a service account `test_ownership_svc` (`is_service_account=True`), registers it, runs `_rotate_key_and_write_file` as a manager, then reads `res_users_apikeys` by SQL for the row named "Ownership Test Daemon_key". It asserts the row exists, that its `user_id` is the service account, and that it is not `base.user_root`.

**Why it matters.** A key owned by the superuser would give the daemon superuser rights over the JSON-2 API; ownership by the narrow service account keeps its authority minimal (the Zero-Sudo rule).

**See also.** `COMM_generate_new_key_logic`, `COMM_revoke_old_keys_logic`, `test_rotating_again_revokes_the_previous_key`.

### `COMM_documentation_installed`

**Purpose.** A coverage marker for the `daemon_key_manager` module's documentation-installed check: the module's own documentation lists it as verified by the test anchor `test_documentation_installed`, meaning the module's help pages are expected to be present after installation.

**Interface.** There is no function behind this anchor in the open-source tree; it names a behaviour, not a callable, and it takes no arguments and returns nothing.

**Unknown.** No code carrying this tag and no test carrying `test_documentation_installed` exists in `daemon_key_manager`. To-do: either add the install hook and its test, or retire the anchor from the module documentation.

**See also.** `hams_shared/docs/modules/daemon_key_manager.md`.

### `test_documentation_installed` [@ANCHOR: test_documentation_installed]

**Purpose.** The test anchor named by the module documentation as verifying `COMM_documentation_installed`.

**Unknown.** No test in `daemon_key_manager/tests` carries this tag, and the code anchor it verifies is also absent. To-do: add a test that the module's help article exists after installation, or remove both anchors.

**See also.** `COMM_documentation_installed`.

### `security_constraints_path` [@ANCHOR: security_constraints_path]

**Purpose.** The constraint on `daemon.key.registry.env_file_path` that confines key files to the key directory and refuses directory traversal. A key file is a credential, so where it may be written is a security decision.

**Interface.** An `@api.constrains("env_file_path")` method on each record, run on create and write. No arguments; it returns nothing.

**Rules.** An empty path is skipped. A path with a `..` component raises `UserError` ("Security Alert: Directory traversal detected in path."). The path is normalised and resolved with `os.path.realpath`, so symbolic links are followed, and the real path must start with `/opt/hams/etc/keys/`, otherwise `UserError` ("The environment file path must start with ...", naming the resolved path).

**Special concerns.** Synchronous and read-only (it touches the filesystem only to resolve links). The same prefix is checked again in `_write_secure_env_file` when the file is written.

**See also.** Tests `test_security_constraints` and `test_env_file_path_rejects_literal_directory_traversal` in `daemon_key_manager/tests/test_key_registry.py`.

### `cron_rotation_trigger` [@ANCHOR: cron_rotation_trigger]

**Purpose.** The scheduled action that rotates daemon API keys automatically, so no daemon key lives longer than about two months.

**Definition.** `daemon_key_manager/data/cron.xml`, `ir.cron` record `ir_cron_rotate_daemon_keys`, named "Daemon Vault: Rotate API Keys". It runs as the module's service account `user_daemon_key_manager_service`, state `code`, with code `model._cron_rotate_all_keys()`, every 1 day, first call one day after installation, active. It is `noupdate="1"`, so an administrator's edits survive upgrades.

**Behaviour.** Each daily run rotates the registries that are due (see `cron_rotation_logic`); the work is batched, and the cron re-triggers itself while a full batch was processed. The docs say "every 60 days"; the code's threshold is `ROTATION_AGE_DAYS = 59`, so a key is rotated by the first run after 59 days.

**See also.** Test `test_cron_rotate_all_keys`; story `key_rotation`.

### `cron_rotation_logic` [@ANCHOR: cron_rotation_logic]

**Purpose.** `_cron_rotate_all_keys()` on `daemon.key.registry`: the body of the scheduled rotation.

**Interface.** `@api.model`, no arguments, returns nothing. It switches to the service account (`_get_service_uid` of `daemon_key_manager.user_daemon_key_manager_service`) and selects up to 10 registries that are not marked `remote_self_rotation`, whose service account is active, and whose `last_rotated` is empty or older than 59 days, oldest first. It fetches their existing API keys once, then rotates each with `_rotate_key_and_write_file(pre_fetched_keys=...)` and commits after each.

**Errors.** A failure for one daemon (`OSError`, `UserError`, `ValidationError`, `AccessError`) is logged as a managed failure after a rollback; any other exception is logged with a traceback, also after a rollback. Neither stops the batch.

**Special concerns.** Stateless batching: when exactly 10 were selected it calls `_trigger()` on the cron record to run again at once. It commits inside the loop, which is why a failure only loses one daemon's work. Writes key files on this host.

**See also.** `cron_rotation_trigger`, test `test_cron_rotate_all_keys`.

### `daemon_self_healing` [@ANCHOR: daemon_self_healing]

**Purpose.** The rule that a daemon using a registry key must survive that key being rotated while it runs.

**Rule.** Because rotation revokes the old key and writes a new one to the daemon's environment file, a daemon that receives an `AccessError` (or an authentication failure) from Odoo must re-read its `.env` file from disk and retry the request. This lets rotation happen without a restart.

**Interface.** None; it is a convention on every daemon that consumes a key, not a function. It has no arguments and returns nothing.

**Unknown.** No code in `daemon_key_manager` carries this anchor; each daemon implements the behaviour itself. To-do: list the daemons that do, and add a shared helper and a test.

**See also.** `cron_rotation_logic`, `COMM_action_rotate_key_api`.

### `register_daemon_logic` [@ANCHOR: register_daemon_logic]

**Purpose.** The body of `register_daemon(daemon_name, user_xml_id, env_file_path, os_group=None)`, the API other modules use to enrol a daemon and get its first key at once.

**Arguments.** `daemon_name`: registry record name (unique per company). `user_xml_id`: XML id of the service account, or its login as a fallback. `env_file_path`: where the key file is written (must be under `/opt/hams/etc/keys/`). `os_group`: optional `hamsd_<family>` group so the file is written 0640 with that group (otherwise 0600); `None` leaves an existing value, an empty string clears it.

**Returns.** `True`.

**Errors.** `UserError` for an unknown service account. `AccessError` when the caller is not a key manager, administrator or superuser and is not a service account registering itself; when a service account registers a different account; and when someone other than a key manager, administrator or superuser tries to set `os_group`.

**Special concerns.** `@api.private`, so it is not reachable over RPC. It runs as the module's service account, creates or updates the registry, flushes, then calls `_rotate_key_and_write_file()`: it revokes the old key, mints a new one and writes the file. Idempotent (see `register_daemon_idempotency`).

**See also.** Test `test_register_daemon_api`.

### `register_daemon_idempotency` [@ANCHOR: register_daemon_idempotency]

**Purpose.** Registering the same daemon again must update the existing registry record and not create a duplicate, so modules can call `register_daemon` from every install and upgrade.

**Behaviour.** `register_daemon` searches for a registry with the same name and company as the service account. If found it is written with the new user, path and company (and `os_group` when given); if not, one is created. Either way the key is rotated and written, so each call yields a new valid key.

**Special concerns.** The search is by `(name, company_id)`, so the same daemon name in two companies is two records. Not safe to call concurrently for the same name (a race could create two); calls come from module install hooks, which are serial.

**See also.** `register_daemon_logic`, test `test_register_daemon_api`.

### `force_provision_logic` [@ANCHOR: force_provision_logic]

**Purpose.** The body of `action_force_provision_all()`: synchronously create or refresh the key of every registered daemon, intended to run from `odoo-bin shell` while systemd is bootstrapping, so daemons find their key files at start-up and do not race the scheduled rotation.

**Interface.** No arguments. Only the superuser or a Daemon Key Manager may call it (`AccessError` "Only Daemon Key Managers can provision keys." otherwise). It runs as the service account, takes up to 1000 registries that are not `remote_self_rotation`, skips those whose service account is archived (logged), fetches existing keys once, and rotates each inside its own savepoint.

**Returns.** `None` when all succeeded; when some failed, a sticky `display_notification` action titled "Some keys were not provisioned" naming the failed daemons and the count of successes.

**Special concerns.** A failure of one daemon (`UserError`, `ValidationError`, `AccessError`, `OSError`) is logged and does not undo or hide the others. Writes key files on this host.

**See also.** `COMM_action_force_provision_all_api`, test `test_force_provisioning`.

### `test_documentation_installed` [@ANCHOR: test_documentation_installed]

**Purpose.** The test anchor named by the module documentation as verifying `COMM_documentation_installed`.

**Unknown.** No test in `daemon_key_manager/tests` carries this tag, and the code anchor it verifies is also absent. To-do: add a test that the module's help article exists after installation, or remove both anchors.

**See also.** `COMM_documentation_installed`.
