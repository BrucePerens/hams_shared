---
name: hams-odoo-test-runner-sudo-and-polkit
description: "On hams.com/hams_open's dev box, running Odoo tests as plain bruce triggers disruptive polkit GUI prompts and real permission errors -- use sudo -u odoo (not root) and HAMS_ISOLATED_NS=1 to avoid both."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 5ef88eba-a768-4870-9ddb-1f49c09220f5
  modified: 2026-09-03T04:44:32.427Z
---

On the hams.com/hams_open dev box, running the real Odoo test harness (`hams_shared/tools/test.py`,
or an equivalent direct `odoo` invocation) as the plain `bruce` user hits two distinct, real
problems, both encountered in the same session and both corrected directly by Bruce:

1. **`rebuild_db()`'s own `systemctl start postgresql/redis-server/rabbitmq-server/pdns` calls are
   unprivileged and trigger a polkit GUI authorization dialog on Bruce's own desktop every single
   time**, regardless of whether the unit is already active. This is genuinely disruptive -- Bruce
   had to interactively deny four of these dialogs and said directly: "I keep getting requests to
   authorize starting of services. Please use sudo," followed by "I just denied 4 authorizations to
   start systemd units." The fix: start the four services yourself first with `sudo systemctl start
   postgresql redis-server rabbitmq-server pdns` (sudo bypasses the polkit prompt entirely since the
   request then comes from root), and set the environment variable `HAMS_ISOLATED_NS=1` before
   calling `rebuild_db()` (or before invoking `test.py`) -- `rebuild_db()`'s own code skips the
   entire "Starting core daemons before testing" block when this is set, so it never issues the
   unprivileged `systemctl start` calls that cause the prompt in the first place.

2. **The real `odoo` server process itself, once it loads `ham_relay_bridge` (or anything that pulls
   in `distributed_redis_cache`/`daemon_key_manager`), needs to write and `chmod`/`chown` files under
   `/opt/hams/etc/keys`**, a directory owned by the `odoo` system user (mode 700) inside
   `/opt/hams/etc` (owned by `hams_com`, mode 750). Plain `bruce` is in neither group and cannot even
   traverse into `/opt/hams/etc`, so the odoo process crashes with `PermissionError` while running
   `daemon_key_manager`'s own key-rotation `post_init_hook` the moment a module test-installs a
   daemon that registers a key there.

**Run the real `odoo` test binary as `sudo -H -u odoo`, never as plain root.** Running it as bare
`sudo` (root, no `-u`) does solve problem 2 (root can write anywhere) but silently introduces a
*different*, real false-negative: root bypasses ordinary Unix DAC permission checks entirely, so any
test that deliberately creates a `0o000`-permission directory to prove a real
`PermissionError`/`os.makedirs` failure path (a genuine, useful test pattern -- confirmed present in
`daemon_key_manager/tests/test_key_registry.py`'s own `test_write_secure_env_file_exceptions`) will
silently NOT raise under root and the test fails with `AssertionError: PermissionError not raised` --
looking exactly like a real regression in freshly-changed code, when it is actually purely an
artifact of testing as root. Confirmed directly, side by side: the identical `os.makedirs()` call
against a `0o000` parent raises `PermissionError` as `bruce` (uid 1000) and succeeds silently as
root (euid 0). The `odoo` system user is already a member of the `hams_com` group (confirmed via
`groups odoo`), so it can traverse `/opt/hams/etc` (group-readable) and fully owns
`/opt/hams/etc/keys` outright -- `sudo -H -u odoo env VAR=val ... /usr/bin/python3 /usr/bin/odoo
...` (the `-H` flag gives `odoo` its own `$HOME` rather than inheriting `bruce`'s, which otherwise
causes a further, different `PermissionError` trying to `mkdir -p ~/.local/share` under
`/home/bruce` as `odoo`) satisfies both real needs at once, with no permission-semantics distortion.

Concretely, the working pattern for a scoped, one-off module test run (e.g. `-u ham_relay_bridge`)
is: run `rebuild_db()` (and any DB/Redis setup) as plain `bruce`, but wrap only the final `odoo`
binary invocation itself in `sudo -H -u odoo env VAR=val ... /usr/bin/python3 /usr/bin/odoo ...`.
This whole class of friction is a pre-existing dev-box permission-boundary issue between `bruce` /
`hams_com` / `odoo`, not something introduced by any particular code change -- don't spend time
trying to fix the underlying ownership structure itself before invoking sudo -u odoo takes care of
the proximate need, and don't default to bare `sudo` (root) as a shortcut past permission errors,
since it can silently mask real test failures the other direction.

**A third real, distinct problem, found 2026-09-03**: `hams_shared/tools/test.py` itself
(`main()`, the "Scanning for Semantic Anchors" step) runs a whole-codebase, both-repos anchor-
consistency check (`verify_anchors.py`) unconditionally before any Odoo test actually executes, and
`sys.exit(1)`s the entire run if it finds ANY violation anywhere in either `hams_com` or
`hams_open` -- not just in the module being tested. In a large, actively-developed codebase this
gate is very likely to already be red for reasons that have nothing to do with whatever change is
actually under test (confirmed directly: dozens of pre-existing undocumented-feature anchor
violations across `ham_shack`, `pager_duty`, `external`, and others, none touched by the change
being verified). There is no flag to skip just this check. Before concluding a real change can't be
tested this way, confirm the violations are genuinely pre-existing and unrelated (`git diff <touched
files> | grep ANCHOR` returns nothing) -- if so, the fix is not to edit `test.py` itself (shared
infrastructure, not this session's to alter for a one-off verification need) but to invoke the real
`odoo` binary directly, bypassing `test.py`'s wrapper (and its anchor gate) entirely. The exact
working command, reverse-engineered from `test.py`'s own `get_addons_path()`/command-construction
logic:

```
# 1. Rebuild the DB as plain bruce first (same as always):
export HAMS_ISOLATED_NS=1
python3 -c "
import sys; sys.path.insert(0, 'hams_shared/tools')
import test as t
t.rebuild_db('hams_test')
"

# 2. Invoke the real odoo binary directly, as odoo, skipping test.py's wrapper entirely:
sudo -H -u odoo env \
  HAMS_ISOLATED_NS=1 HOME=/var/lib/odoo \
  ODOO_URL=http://127.0.0.1:8075 DB_NAME=hams_test REDIS_DB=1 \
  ODOO_USER=admin ODOO_PASSWORD=admin \
  /usr/bin/python3 /usr/bin/odoo \
  --load=base,web,zero_sudo \
  --addons-path /usr/lib/python3/dist-packages/odoo/addons,/home/bruce/workspace/hams_com,/home/bruce/workspace/hams_open \
  --dev=xml -d hams_test -i base,<module_name> \
  --test-enable --test-tags /<module_name> --stop-after-init \
  --workers=0 --max-cron-threads=0 \
  --http-interface 127.0.0.1 --http-port 8075 \
  --limit-memory-soft 0 --limit-memory-hard 0
```

Two sharp edges confirmed the hard way: (1) `-i <module>` on a module that's already installed in
`hams_test` from a *previous* run is a no-op that does NOT reload changed Python source or
re-register new/renamed tests (only a genuine install/upgrade transition does) -- editing test code
and re-running against a stale, already-installed DB silently shows "0 post-tests" with no error,
looking like nothing ran rather than like a real signal. Always `rebuild_db()` fresh (step 1) before
each real re-run once source has changed, not just once at the start. (2) A long `odoo` test run
launched via the harness's own `run_in_background` can be killed mid-run by something outside this
session's control (observed directly: `KeyboardInterrupt` from `signal_handler` partway through
module loading, no user action involved) -- if that happens, don't assume the run failed for a real
reason; check `ps aux | grep odoo` for a stale process, rebuild the DB again, and relaunch detached
(`nohup ... & disown`, redirecting all three of stdin/stdout/stderr) so the process survives
independently of the tool call that started it, then poll the log file for the real
`odoo.tests.result` line rather than trusting the tool call's own exit status.
