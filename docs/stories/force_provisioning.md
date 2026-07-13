# Story: Manual Force Provisioning

As a **DevOps Engineer** setting up a new environment,
I want to **force the generation of all daemon keys immediately**,
so that my external containers can start up without waiting for the next cron cycle.

## Scenario: Infrastructure Bootstrapping

1.  The automated deployment script (e.g., Ansible or a Kubernetes init container) finishes installing Odoo and its modules.
2.  The script executes a shell command to force provisioning [@ANCHOR: daemon_key_manager:COMM_action_force_provision_all_api]:
    ```bash
    odoo-bin shell ... -e "env['daemon.key.registry'].action_force_provision_all(); env.cr.commit()"
    ```
3.  The `daemon_key_manager` iterates through all registered daemons and regenerates their keys and files [@ANCHOR: daemon_key_manager:COMM_force_provision_logic].
4.  Once the command completes, all `.env` files are guaranteed to be present and valid.
5.  The dependent daemon containers are then started, find their keys, and connect to Odoo immediately.

## Error Handling
- If a file cannot be written due to permission issues (e.g., the volume isn't mounted correctly), an informative `UserError` is raised [@ANCHOR: daemon_key_manager:COMM_force_provision_error_handling].
