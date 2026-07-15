# Journey: Lifecycle of a Daemon API Key

This journey describes the full lifecycle of a daemon's credentials, from registration to continuous rotation.

1.  **Birth: Registration**
    - A module calls `register_daemon()` [@ANCHOR: daemon_key_manager:COMM_register_daemon_api].

    - `daemon_key_manager` creates a record and generates the first key [@ANCHOR: daemon_key_manager:COMM_register_daemon_logic].

    - The key is written to a secure `.env` file [@ANCHOR: daemon_key_manager:COMM_write_secure_env_file_logic].

2.  **Usage: Consumption**
    - The external daemon boots and reads `ODOO_RPC_KEY` from the file.
    - It uses this key for JSON-RPC authentication to Odoo.
    - Odoo validates the key against the service account.

3.  **Renewal: Periodic Rotation**
    - After 60 days, the cron job triggers [@ANCHOR: daemon_key_manager:COMM_cron_rotation_trigger].

    - `daemon_key_manager` revokes the old key and generates a new one [@ANCHOR: daemon_key_manager:COMM_revoke_old_keys_logic] [@ANCHOR: daemon_key_manager:COMM_generate_new_key_logic].
    - The `.env` file is updated with the new key.

4.  **Transition: Handover**
    - The daemon makes a call with the old key and receives an `AccessError`.
    - The daemon catches the error, re-reads the `.env` file [@ANCHOR: daemon_key_manager:COMM_daemon_self_healing].
    - The daemon retries with the new key and continues its work.

5.  **End of Life: Uninstallation**
    - If the daemon's module is uninstalled, its registry entry may be removed.
    - (Optional) A manual cleanup of the `.env` file can be performed by an admin.
