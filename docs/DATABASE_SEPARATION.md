# Database separation: peer authentication and a secret schema the web role cannot read

The Odoo web process runs as one OS account (`odoo`) and logs in to PostgreSQL as one role. Whatever that role can
read, a bug in any route, controller or RPC method can expose. Three settings keep the damage small, for every
installer:

1. The `odoo` role is **not a superuser** (a superuser web role turns SQL injection into command execution as the
   `postgres` OS account and reads every database on the cluster).
2. The role has **no password** and logs in by **peer authentication over the unix socket**: there is no database
   password in `odoo.conf`, in an environment file or in the process to steal, and a password login over TCP fails.
3. Secret-bearing tables live in a separate schema, `hams_secret`, owned by the role `hams_secret_broker` (the OS
   account of the secret broker, hams_open `zero_sudo/daemon/secret_broker`). The `odoo` role has no privilege on it
   and cannot grant itself one, even though it owns the database.

## With hams_shared provisioning

`tools/provision.py` does all three on a new production install (`tools/infrastructure.py`:
`_create_odoo_role_if_missing`, `ODOO_ROLE_ATTRIBUTES`, `_provision_secret_schema`). The Debian `odoo` package already
creates the role without a password and ships `odoo.conf` with `db_host = False` and `db_password = False`, which is
peer over the socket; provisioning keeps both. A role that already exists is never changed by a provisioning run (see
"Existing installs" below). Test hosts differ on purpose: there the `odoo` role is a superuser with a password,
because `tools/test.py` creates and drops databases as `odoo` over TCP.

The tests are `ProvisionSecretSchemaTests` and `SecretSchemaOnARealClusterTests` in `tools/test_infrastructure.py`; the
second runs the real statements on the local cluster with scratch roles (permission denied on the schema, no
self-grant, TCP password login refused, peer on the socket).

## Stock install without our provisioning

Run as the `postgres` OS account (`sudo -u postgres psql`). `hams_prod` is your Odoo database.

```sql
-- 1 and 2: the web role, no superuser, no password
CREATE ROLE odoo WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
-- (for an existing role instead: ALTER ROLE odoo NOSUPERUSER NOCREATEDB NOCREATEROLE; ALTER ROLE odoo PASSWORD NULL;)
CREATE DATABASE hams_prod OWNER odoo;
\c hams_prod
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- created by postgres, so the web role needs no superuser
CREATE EXTENSION IF NOT EXISTS vector;
-- 3: the secret schema
CREATE ROLE hams_secret_broker WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE SCHEMA IF NOT EXISTS hams_secret AUTHORIZATION hams_secret_broker;
REVOKE ALL ON SCHEMA hams_secret FROM PUBLIC;
```

`pg_hba.conf`: the stock Debian file already has `local all all peer` before any password rule; keep it. No `trust`
line anywhere. `/etc/odoo/odoo.conf`:

```ini
db_host = False
db_port = False
db_user = odoo
db_password = False
```

Odoo must run as the OS account `odoo` (peer authentication matches the OS account name to the role name).

## Existing installs (documented, reversible; never forced by provisioning)

Order: first remove superuser (hams_com `docs/runbooks/ODOO_DB_ROLE_DEMOTION.md` is the rehearsed procedure), then:

1. Move every other client that logs in as `odoo` with a password over TCP to its own role or to the socket. In this
   project: `dx_firehose` (hams_com, `DB_USER`/`DB_PASS` from `db_app.env`) and `database_management`'s vacuum
   (hams_open, `PGHOST`/`PGPASSWORD`). Until they move, step 2 stops them.
2. Remove `db_host`, `db_port` and `db_password` values from `odoo.conf` (set them to `False`), restart Odoo, check
   it serves, then `ALTER ROLE odoo PASSWORD NULL;`.
3. Create the secret schema with the statements above.

Reverse: `ALTER ROLE odoo PASSWORD :'p';` (psql, with `-v p=...` from your saved password), put the old `odoo.conf`
lines back, restart. Dropping the empty schema reverses step 3: `DROP SCHEMA hams_secret; DROP ROLE hams_secret_broker;`.
Backups: a dump of the database includes the new schema; nothing about backup keys or encryption changes.

## Check

```sql
SELECT rolsuper, rolpassword IS NULL FROM pg_authid WHERE rolname = 'odoo';                      -- f | t
SELECT has_schema_privilege('odoo', 'hams_secret', 'USAGE'), has_schema_privilege('odoo', 'hams_secret', 'CREATE');  -- f | f
```

`psql -h 127.0.0.1 -U odoo hams_prod` with any password: `password authentication failed`.
`sudo -u odoo psql hams_prod -c 'SELECT 1 FROM hams_secret.anything'`: `permission denied for schema hams_secret`.
