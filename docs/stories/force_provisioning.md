# Story: Manual Force Provisioning

As a **DevOps Engineer** setting up a new environment,
I want to **force the generation of all daemon keys immediately**,
so that my external containers can start up without waiting for the next scheduled-job (cron) cycle.

## Scenario: Infrastructure Bootstrapping

1.  The automated deployment script (e.g., Ansible, a deployment automation tool, or a Kubernetes init container, a setup step that runs before the application container) finishes installing Odoo and its modules.
2.  The script executes a shell command to force provisioning `daemon_key_manager:COMM_action_force_provision_all_api`:
    ```bash
    odoo-bin shell ... -e "env['daemon.key.registry'].action_force_provision_all(); env.cr.commit()"
    ```
3.  The `daemon_key_manager` iterates through all registered daemons and regenerates their keys and files `daemon_key_manager:COMM_force_provision_logic`.
4.  Once the command completes, all `.env` files are guaranteed to be present and valid.
5.  The dependent daemon containers are then started, find their keys, and connect to Odoo immediately.

## Error Handling
- If a file cannot be written due to permission issues (e.g., the volume isn't mounted correctly), an informative `UserError` (an Odoo error message shown to the user) is raised `daemon_key_manager:COMM_force_provision_error_handling`.

## Anchor reference

### `daemon_key_manager:COMM_action_force_provision_all_api` [@ANCHOR: daemon_key_manager:COMM_action_force_provision_all_api]

**Purpose.** `action_force_provision_all()` regenerates every daemon key and file at once, for deployment bootstrap (before daemon containers start) and emergency rotation.

**Signature.** `@public_rpc action_force_provision_all(self, *args, **kwargs)`. Returns an `ir.actions.client` `display_notification` dictionary: a "Success" notice, or a sticky danger notice naming the daemons that failed.

**Authorization.** Only the superuser (an `odoo-bin shell` session) or a member of `Daemon Key Management / Manager`; others get `AccessError("Only Daemon Key Managers can provision keys.")`. It then switches to the key-manager service account.

**Special concerns.** Synchronous; it does not commit, so the shell caller must `env.cr.commit()`. Registries with Remote Self-Rotation are excluded and archived accounts are skipped with a log line. Writes key files on this host; at most 1000 registries per call. Old keys are revoked, so run it when no daemon is mid-request.

**See also.** `force_provision_logic`, `force_provision_error_handling`; tests `test_force_provisioning`, `test_unauthorized_access`.

### `daemon_key_manager:COMM_force_provision_logic` [@ANCHOR: daemon_key_manager:COMM_force_provision_logic]

**Purpose.** The body of `action_force_provision_all`: the loop that regenerates each registered daemon's key and file.

**Behaviour.** It searches `daemon.key.registry` for rows with `remote_self_rotation = False` (limit 1000), removes those whose service account is archived (logged once), prefetches the existing API keys in one query, then calls `_rotate_key_and_write_file(pre_fetched_keys=...)` for each registry under its own company and database savepoint. Every registry is attempted even if an earlier one fails; failures are collected and reported together (`force_provision_error_handling`).

**Special concerns.** Remote self-rotating registries are left alone because re-provisioning would revoke the key the remote daemon holds; this matters because the same call runs at every start of `hams.daemon.keys.service`. Runs as the key-manager service account with no `sudo`.

**See also.** Tests `test_force_provisioning`, `test_force_provisioning_partial_failure`.

### `daemon_key_manager:COMM_force_provision_error_handling` [@ANCHOR: daemon_key_manager:COMM_force_provision_error_handling]

**Purpose.** Defines what happens when one daemon cannot be provisioned during `action_force_provision_all`, for example when the keys volume is not mounted read/write or a directory has the wrong permissions.

**Behaviour.** Each registry is rotated inside its own database savepoint. A `UserError`, `ValidationError`, `AccessError` or `OSError` undoes only that registry's database changes (its old key is restored), is logged with the daemon name, and the loop moves on. The method does not raise at the end: raising would roll back the daemons that had already succeeded while their new key files were already on disk, leaving files and database in disagreement. It returns a sticky danger notification, "Provisioned keys for N daemon(s); FAILED for: <names>", and the caller commits. The systemd bootstrap script turns that result into a failed unit.

**Special concerns.** Nothing is silently skipped except archived accounts (`force_provision_skips_archived`). See the real error in the server log per daemon.

**See also.** Tests `test_force_provisioning` and `test_a_failing_daemon_does_not_undo_or_hide_the_daemons_that_succeeded` (`test_force_provisioning_partial_failure`).
