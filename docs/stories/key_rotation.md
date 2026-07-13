# Story: Automated 60-Day Key Rotation

As a **System Administrator**,
I want the **API keys for all registered daemons to rotate automatically**,
so that the security risk of a leaked key is limited in time and I don't have to perform manual rotations.

## Scenario: Periodic Maintenance

1.  Odoo's scheduled actions runner (cron) triggers the `ir_cron_rotate_daemon_keys` job every day [@ANCHOR: daemon_key_manager:COMM_cron_rotation_trigger].
2.  The job identifies all daemons whose keys were last rotated more than 59 days ago [@ANCHOR: daemon_key_manager:COMM_cron_rotation_logic].
3.  For each eligible daemon, it:
    - Revokes the existing API key [@ANCHOR: daemon_key_manager:COMM_revoke_old_keys_logic].
    - Generates a fresh 90-day API key [@ANCHOR: daemon_key_manager:COMM_generate_new_key_logic].
    - Overwrites the existing `.env` file with the new key [@ANCHOR: daemon_key_manager:COMM_write_secure_env_file_logic].
    - Updates the `last_rotated` timestamp.
4.  The external daemon, upon its next JSON-RPC call, may receive an `AccessError`.
5.  The daemon's error handler re-reads the `.env` file, acquires the new key, and retries the request successfully [@ANCHOR: daemon_key_manager:COMM_daemon_self_healing].

## Manual Rotation

If a specific daemon's credentials are suspected of being compromised, a manager can manually trigger a rotation for that specific daemon via the "Rotate Key" button on the registry form [@ANCHOR: daemon_key_manager:COMM_test_action_rotate_key].

## Safety Safeguards

The system strictly prevents key rotation for service accounts that have been archived or disabled to prevent accidental reactivation of unauthorized access [@ANCHOR: daemon_key_manager:COMM_rotation_safety_archived_user].

## Security Benefits
- Even if a backup of the `.env` file is stolen, the key will expire and be revoked within 60 days.
- The 90-day expiration on the Odoo side provides a 30-day buffer for the rotation to succeed.

## Additional Tests
- [@ANCHOR: daemon_key_manager:COMM_test_ui_rendering]
- [@ANCHOR: daemon_key_manager:COMM_test_action_rotate_key]
- [@ANCHOR: daemon_key_manager:COMM_test_rotation_safety_archived_user]
- [@ANCHOR: daemon_key_manager:COMM_test_cron_rotate_all_keys]
- [@ANCHOR: daemon_key_manager:COMM_test_key_ownership]
- [@ANCHOR: daemon_key_manager:COMM_test_security_constraints]
- [@ANCHOR: daemon_key_manager:COMM_test_register_daemon_api]
- [@ANCHOR: daemon_key_manager:COMM_test_force_provisioning]
- [@ANCHOR: daemon_key_manager:COMM_test_unauthorized_access]
- [@ANCHOR: daemon_key_manager:COMM_test_daemon_key_manager_tour]
