# ADR 0107: One OS Account Per Daemon Family

## Status
Accepted (Bruce, 2026-10-03, decision 86). Phase 1 implemented 2026-10-04; later phases open.

## Context

`daemon_key_manager` issues each daemon its own API key and writes it to its own `0600` file under
`/opt/hams/etc/keys/`. That isolation holds only if the daemons run as different operating-system accounts. On
2026-10-02 a live check of `hams1` found about 40 systemd units running as `User=odoo`, so every one of them could read every
other daemon's key file, and `/proc/<pid>/environ` of every other `odoo` process. Nearly all of them also load `core.env`,
`db.env`, `odoo.env` and more through `EnvironmentFile=`, which puts `HAMS_CRYPTO_KEY`, `POSTGRES_PASSWORD`,
`ODOO_ADMIN_PASSWORD` and `CLOUDFLARE_API_TOKEN` into the environment of daemons that use none of them; no change of account
affects that. `pdns` and `localhost_cert` (ADR 0100) already run under their own accounts, so the pattern exists.

## Decision

**1. One account and group per daemon family**, named `hamsd_<family>`, a family being the units that share a key file and a trust
level. Provisioned from `MANIFEST["system_accounts"]` in `tools/infrastructure.py`, so every user of the code gets it, not only our hosts.

**2. The key file is handed to the family's group.** `daemon.key.registry.os_group` names the group; the key file is `odoo:<group> 0640`.
The key manager runs as the unprivileged `odoo` user, which can change a file's group to a group it belongs to and cannot change its
owner, so the file stays `odoo`-owned (the only writer) and the group is the reader. Provisioning puts `odoo` in each family group.
`/opt/hams/etc/keys` is `odoo:hams_com 0710` (traverse, no listing), so an account can open the one file named for it and no other.

**3. A migrated unit narrows what it can touch.** `ReadWritePaths=` names only directories the family's account owns; it loads only the
environment files its daemon uses (non-secret values are in `common.env`); and it adds `ProtectProc=invisible` and the kernel and namespace
`Protect*`/`Restrict*` directives. A ratchet in `tools/test_infrastructure.py` (`FAMILY_ACCOUNT_UNITS`, `SHARED_ODOO_ACCOUNT_UNITS`)
fails when a unit under a `hamsd_` account is not held to these rules and when a unit still on `odoo` is not on the reviewed list, so
the list can only shrink.

**4. Phased, each phase rehearsed on a restored-dump host before any live change.** Phase 1 moved `ncvec.sync`. The plan, the family table, the
ordering on a live host, rollback and the rehearsal procedure are in hams_com `docs/proposals/DAEMON_OS_ISOLATION_PLAN.md`.

## Consequences

- A compromised family daemon can read its own key and nothing else in the key directory; with its environment trimmed it also holds none of
  the shared secrets. Until a family is migrated and its environment trimmed it has neither protection.
- Phase 1 accounts join `hams_com` to reach `/opt/hams` (as `pdns` does). The unit's read-only sandbox keeps them out of the `hams_com`-writable
  directories; Phase 2 replaces the membership with a traverse-only grant.
- Deploying a phase needs the key-manager upgrade (`odoo -u daemon_key_manager`), a provision run, and a restart of `odoo.service` (a process
  gets its supplementary groups when it starts).

## Alternatives considered

- `DynamicUser=`: no stable owner for files that Odoo and the daemon both touch; rejected for the key-file flow.
- `systemd LoadCredential=` for the oneshot units: needs no group or directory change but is a snapshot at start, so a long-running daemon would not see a
  rotated key. Left as an option for the oneshot families (open question to Bruce).
- A root helper that chowns key files to the consuming account: the literal reading of "owned by", at the cost of more privilege than the
  group form needs.
- An accepted-risk decision: rejected by Bruce.
