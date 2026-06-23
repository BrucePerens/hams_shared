# Daemon Key Manager (`daemon_key_manager`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).*

The **Daemon Key Manager** is the centralized authority for managing Odoo API keys for external background services (daemons). It implements the **Service Account Pattern** by generating native Odoo API keys and exporting them to highly restricted local `.env` files, eliminating the need for hardcoded credentials or manual token rotation. This module is a critical component of the **Zero-Sudo Architecture**, ensuring that background processes operate with minimum privilege and without human intervention for credential management.

## 🚀 Quick Start: Integration API

Other modules should request daemon credentials during their installation (e.g., in a `post_init_hook`) or via a configuration wizard.

```python
def setup_daemon_credentials(env):
    # Idempotent registration and synchronous key generation
    # This call ensures the daemon is registered for 60-day rotations.
    env['daemon.key.registry'].register_daemon(
        daemon_name="My External Daemon",
        user_xml_id="my_module.my_service_account",
        env_file_path="/var/lib/odoo/daemon_keys/my_daemon.env"
    )
```

## 🛡️ Security Architecture

### Zero-Sudo Compliance
The module operates under the `user_daemon_key_manager_service` account. It uses `.sudo()` only for the strictly necessary administrative tasks of API key allocation and revocation, which are restricted operations in Odoo that normally require administrative privileges. This specific module is granted an exemption to use `.sudo()` for these operations to maintain the security of the broader system while enabling automated service account management.

### OS-Level Sandboxing
* **Strict Permissions:** `.env` files are created with `0600` (read/write only for the Odoo server process user).
* **Directory Isolation:** Parent directories are created with `0700` to prevent other users on the system from traversing into the key storage area.
* **Path Validation:** All paths MUST start with `/var/lib/odoo/daemon_keys/`. The module strictly blocks directory traversal (`..`) and symlink attacks by resolving the `os.path.realpath` of the requested path before performing any file operations [@ANCHOR: security_constraints_path].
* **System Directory Protection:** Writing to sensitive system directories (like `/etc`, `/root`, `/boot`, `/home`, `/usr`, `/bin`, `/lib`, `/var/log`) is explicitly forbidden regardless of the prefix check [@ANCHOR: write_secure_env_file_logic].

### Automated Key Rotation
Keys are automatically rotated every 60 days via an `ir.cron` job [@ANCHOR: cron_rotation_trigger].
* **Graceful Failure:** Stateless batching (processing 10 records at a time and re-triggering) ensures that one failed file-write or database error does not block other rotations. Failures are logged, and the system attempts to continue with the next daemon [@ANCHOR: cron_rotation_logic].
* **Buffer Period:** New keys are generated with a 90-day expiration, providing a 30-day "grace period" for the 60-day rotation cycle to succeed in case of transient server issues.
* **Self-Healing Daemons:** Daemons utilizing these keys MUST be designed to catch `AccessError` responses from Odoo, re-read their assigned `.env` file from the disk, and retry the request. This ensures continuous operation across key rotations [@ANCHOR: daemon_self_healing].

---

## 🛠️ Technical Reference

### 1. Storage & Orchestration Mandate
All credentials **MUST** be written to `/var/lib/odoo/daemon_keys/`.
In containerized/orchestrated environments:
* **Odoo Container:** Mount the volume as **Read/Write**.
* **Daemon Containers:** Mount the volume as **Read-Only**.

### 2. Core API Methods

#### `register_daemon(daemon_name, user_xml_id, env_file_path)` [@ANCHOR: register_daemon_api]
* **`daemon_name`**: A unique string identifier for the external service.
* **`user_xml_id`**: The XML ID of the service account record (e.g., `pager_duty.user_pager_service_internal`). This account must have `is_service_account` set to `True`.
* **`env_file_path`**: The absolute path where the `.env` file should be written. It must reside within `/var/lib/odoo/daemon_keys/`.
* **Behavior**: This method is idempotent. If a daemon with the same name exists, its service account and path are updated. It immediately triggers the generation of the first API key and writes the file [@ANCHOR: register_daemon_logic] [@ANCHOR: register_daemon_idempotency].

#### `action_force_provision_all()` [@ANCHOR: action_force_provision_all_api]
* **Use Case**: Used during system bootstrapping (e.g., via systemd or Kubernetes init containers) to ensure all keys are present on disk before daemons start. Also used for emergency rotation of all keys.
* **Shell Invocation**:
  ```bash
  odoo-bin shell -d hams --no-http -e "env['daemon.key.registry'].action_force_provision_all(); env.cr.commit()"
  ```
* **Security**: Only accessible to users in the `Daemon Key Management / Manager` group. Internally, it elevates to the service account to perform the privileged key generation [@ANCHOR: force_provision_logic].

### 3. File Format (.env) [@ANCHOR: write_secure_env_file_logic]
```env
# Auto-generated by daemon.key.registry
ODOO_RPC_LOGIN=service_account_login
ODOO_RPC_KEY=12345abcd...
```

---

## 📖 Stories & Journeys

* [Registering a New External Daemon](docs/stories/daemon_registration.md)
* [Manual Force Provisioning](docs/stories/force_provisioning.md)
* [Automated 60-Day Key Rotation](docs/stories/key_rotation.md)
* [Lifecycle of a Daemon API Key](docs/journeys/api_key_lifecycle.md)
* [Bootstrapping a Containerized Environment](docs/journeys/container_bootstrapping.md)
* [Zero-Sudo Refactoring Story](docs/stories/zero_sudo_refactoring.md)

---

## 🧪 Verification
* **register_daemon_api**: Verified by [@ANCHOR: test_register_daemon_api]
* **force_provision_all**: Verified by [@ANCHOR: test_force_provisioning]
* **security_constraints**: Verified by [@ANCHOR: test_security_constraints]
* **ui_tour**: Verified by [@ANCHOR: test_daemon_key_manager_tour]
* **unauthorized_access**: Verified by [@ANCHOR: test_unauthorized_access]
* **key_ownership**: Verified by [@ANCHOR: test_key_ownership]
* **documentation_installed**: Verified by [@ANCHOR: documentation_installed] [@ANCHOR: test_documentation_installed]
