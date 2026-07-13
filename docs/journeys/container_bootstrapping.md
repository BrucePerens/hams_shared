# Journey: Bootstrapping a Containerized Environment

This journey covers the steps required to provision credentials in a modern, containerized Odoo deployment.

1.  **Deployment**
    - Orchestrator (e.g., Docker Compose) mounts `/opt/hams/etc/keys/` as a shared volume between Odoo and the daemons.

2.  **Initialization**
    - Odoo container starts and performs database migrations.
    - `daemon_key_manager` is installed.

3.  **Proactive Provisioning**
    - CI/CD runner executes `action_force_provision_all()` [@ANCHOR: daemon_key_manager:COMM_action_force_provision_all_api].
    - This ensures all `.env` files are created *before* the daemons try to read them [@ANCHOR: daemon_key_manager:COMM_force_provision_logic].

4.  **Daemon Startup**
    - Daemon containers start.
    - They find their respective `.env` files already present in the shared volume.
    - Daemons start working immediately without any "key not found" errors.

5.  **Steady State**
    - Odoo's cron job maintains the keys over the long term [@ANCHOR: daemon_key_manager:COMM_cron_rotation_logic].
