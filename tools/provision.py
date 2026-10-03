#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Standalone Environment Provisioning Script
Must be run as root.
"""
import argparse
import os
import re
import sys
import subprocess
import logging

# Bug-hunt fix (2026-09-23, found while preparing a real production release):
# this script lives at hams_shared/tools/provision.py, and infrastructure.py is
# its own direct sibling in that same tools/ directory -- but `repo_root` used
# to be computed as ONE level up from here (dirname(__file__) + "/.."), landing
# on hams_shared itself, not hams_open. That broke two different things that
# both used to share this one value:
#   1. `sys.path.insert(0, repo_root)` needs the directory infrastructure.py
#      actually lives in (this file's own directory, zero levels up) for
#      `import infrastructure` to succeed at all -- confirmed directly: running
#      this script standalone (not under pytest, which happens to also put
#      this same directory on sys.path via its own test collection and so
#      masked the bug) raised a real ModuleNotFoundError.
#   2. `env_vars["REPO_ROOT"]`, which infrastructure.provision_environment()
#      expects to be hams_open's own root (it checks
#      `os.path.exists(os.path.join(repo_root, "hams_shared"))` as its PRIMARY
#      test for "this repo_root IS hams_open") -- that needs TWO levels up from
#      here (tools -> hams_shared -> hams_open), not one. The single wrong
#      value happened to still often locate hams_com_dir/hams_community_dir
#      correctly in practice, purely by falling through to this same
#      function's own `../..`-relative fallback branches for the specific,
#      real `/opt/hams/src/{hams_com,hams_open}` sibling layout -- coincidence
#      that masked the deeper problem, not evidence the original value was
#      right.
_provision_tools_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _provision_tools_dir)
import infrastructure  # noqa: E402


# Bug-hunt fix (2026-10-01, found via a full tools/run_linters.py sweep): this script is
# deployed as an identical copy at two genuinely different nesting depths --
# `hams_com/tools/provision.py` (one level above hams_com's own root, which holds `hams_shared`
# as a symlink into hams_open) and `hams_open/hams_shared/tools/provision.py` (two levels above
# hams_open's own root, which holds `hams_shared` as a real directory). The previous fixed
# `"..", ".."` walk was only ever correct for the second copy -- the first copy landed on the
# PARENT of hams_com (e.g. `~/workspace`), where `hams_shared` does not exist at all, breaking
# every caller of `repo_root` for that copy. A fixed-depth walk cannot serve both real layouts by
# construction, so this walks upward from this file's own location until it finds the first
# ancestor directory whose own `hams_shared` subdirectory exists (real directory or symlink,
# `os.path.exists` follows both) -- the one property both deployed copies actually share.
def _find_repo_root(start_dir):
    current = start_dir
    for _ in range(5):  # five levels is already far more than either real layout ever needs
        if os.path.exists(os.path.join(current, "hams_shared")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise RuntimeError(
        f"Could not find a repo root containing 'hams_shared' above {start_dir!r} -- "
        "this script must live under either <hams_com>/tools/ or "
        "<hams_open>/hams_shared/tools/."
    )


repo_root = _find_repo_root(_provision_tools_dir)

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger(__name__)


def provision():
    os.chdir(repo_root)

    if os.geteuid() != 0:
        _logger.info("[*] Elevating privileges (sudo) to provision environment...")
        # Bug-hunt fix (2026-09-10): this re-exec previously dropped every
        # one of this process's own CLI arguments (sys.argv[1:], e.g.
        # --test/--force-reset) -- confirmed by direct comparison against
        # test.py's own equivalent self-re-exec-via-sudo pattern
        # (`["sudo", "-H", "-E", sys.executable] + sys.argv`), which does
        # forward them. A caller running `provision.py --force-reset` as a
        # non-root user (the common case) had that flag silently vanish
        # the moment this script escalated to root: the re-exec'd,
        # actually-provisioning process ran with NO CLI args at all, so
        # neither --force-reset nor --test ever took effect unless the
        # caller happened to already be root. Fixed by forwarding
        # sys.argv[1:] explicitly, matching test.py's own pattern.
        os.execvp(
            "sudo",
            ["sudo", "-H", "-E", sys.executable, os.path.abspath(__file__)]
            + sys.argv[1:],
        )

    orig_user = os.environ.get("SUDO_USER") or os.environ.get("USER")
    env_vars = dict(os.environ)
    env_vars["DEBIAN_FRONTEND"] = "noninteractive"
    env_vars["REPO_ROOT"] = repo_root

    os_id = infrastructure.get_os_identifier()
    _logger.info(f"[*] Discovered OS: {os_id}")

    parser = argparse.ArgumentParser(description="Standalone Environment Provisioning Script")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Provision a test host: smoke-test the local daemons and stop them after, even on "
        "failure. Units that fetch from third-party servers (MANIFEST external_fetch) are "
        "linked but never enabled or started",
    )
    parser.add_argument("--force-reset", action="store_true", help="Completely destroy the existing database, filestore, and cache before provisioning")
    parser.add_argument(
        "--hold-odoo",
        action="store_true",
        help="Prepare the box (packages, accounts, env files, code, empty database, units) but do NOT "
        "install the Odoo modules into the database and do NOT start Odoo or any daemon",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print every action provisioning would take (accounts, directories, files and config "
        "writes, apt installs, units linked/enabled/restarted, database steps) and change nothing. "
        "Read-only probes still run. Secret values are never printed",
    )
    parser.add_argument(
        "--enable-opt-in",
        action="append",
        default=[],
        metavar="UNIT",
        help="Also enable (and smoke-test) this MANIFEST opt_in unit, e.g. code.review.sweep.timer, "
        "which calls the paid Gemini API. Repeatable. Without it opt-in units are linked only",
    )
    args, _ = parser.parse_known_args()

    if os_id not in ("ubuntu", "debian"):
        _logger.error(
            f"[!] Unsupported OS: {os_id}. Only Debian and Ubuntu are currently supported."
        )
        sys.exit(1)

    infrastructure.load_and_prompt_env(env_vars, args.test)

    if args.force_reset and args.plan:
        _logger.warning(
            "[*] PLAN: --force-reset would stop odoo, drop the database, wipe its filestore and flush redis"
        )
    elif args.force_reset:
        db_name = env_vars.get("DB_NAME", "hams_test")
        # Bug-hunt fix (2026-09-10): db_name is interpolated below into a
        # filesystem path that a subsequent `rm -rf` deletes outright
        # (`filestore_path`). DB_NAME is normally an operator-controlled
        # config value (a .env file or an interactive prompt in
        # infrastructure.load_and_prompt_env), not external input, so this
        # guards against a typo'd/copy-pasted DB_NAME (e.g. one containing
        # "../") causing this force-reset to `rm -rf` something outside
        # the intended filestore directory, not against a remote attacker.
        # Mirrors test.py's own rebuild_db() identifier check (same
        # underlying risk shape: a config value interpolated unvalidated
        # into a destructive operation).
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", db_name):
            _logger.error(
                f"[!] DB_NAME {db_name!r} is not a safe database/directory "
                "name (must match ^[A-Za-z_][A-Za-z0-9_]*$) -- refusing to "
                "use it in a --force-reset teardown."
            )
            sys.exit(1)
        _logger.warning(f"[*] Force-reset enabled! Tearing down old environment for '{db_name}'...")
        
        _logger.info("[*] Stopping odoo service...")
        subprocess.run(["systemctl", "stop", "odoo"], check=False)
        
        _logger.info(f"[*] Dropping database {db_name}...")
        subprocess.run(["sudo", "-u", "postgres", "dropdb", "--if-exists", db_name], check=False)
        
        filestore_path = f"/var/lib/odoo/filestore/{db_name}"
        _logger.info(f"[*] Wiping filestore at {filestore_path}...")
        subprocess.run(["rm", "-rf", filestore_path], check=False)
        
        _logger.info("[*] Flushing redis cache...")
        subprocess.run(["redis-cli", "flushall"], check=False)

    def run_sys(cmd, **kw):
        printable = " ".join(infrastructure.redact_command(cmd))
        if args.plan:
            # --plan: never execute; record the command (passwords masked) and report success.
            print(f"PLAN run: {printable}", flush=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        _logger.info(f"[*] Running: {printable}")
        if "env" not in kw:
            kw["env"] = env_vars
        return subprocess.run(cmd, check=True, **kw)

    if os_id == "debian" and "python3-pypdf2" in infrastructure._installed_apt_packages():
        # Already there (every re-run on a provisioned box): rebuilding and re-installing it would
        # only churn dpkg on a live host.
        _logger.info("[*] Dummy python3-pypdf2 package already installed; skipping its build")
    elif os_id == "debian":
        _logger.info("[*] Generating dummy python3-pypdf2 package for Debian compatibility")
        run_sys(["apt-get", "update", "-y"])
        run_sys(["apt-get", "install", "-y", "--no-upgrade", "equivs"])
        # Real, previously-masked ordering bug found 2026-09-13 hardware-qualifying pi500-1
        # (Raspberry Pi 500, a genuinely fresh Debian 12 bookworm box): the equivs package
        # built just below declares `Depends: python3-pypdf`, but that real package is only
        # installed later, inside infrastructure.provision_environment()'s own apt_packages
        # pass -- so `dpkg -i` on a fresh box fails outright with "python3-pypdf2 depends on
        # python3-pypdf; however: Package python3-pypdf is not installed." This was never
        # caught on the dev box because it already had python3-pypdf installed incidentally
        # from unrelated prior work, masking the real ordering bug the same way this
        # project's own install_debian_deps.sh has already documented for python3-numpy/
        # autoconf/automake/libtool on a bare CI image. Install the real dependency here,
        # explicitly, before building/installing the dummy compatibility package that needs
        # it -- not duplicated later, since infrastructure.py's own MANIFEST-driven install of
        # python3-pypdf is naturally a no-op once apt already has it satisfied.
        run_sys(["apt-get", "install", "-y", "--no-upgrade", "python3-pypdf"])
        equivs_config = (
            "Section: python\n"
            "Priority: optional\n"
            "Standards-Version: 4.1.4\n"
            "Package: python3-pypdf2\n"
            "Version: 1.0\n"
            "Depends: python3-pypdf\n"
            "Description: Dummy python3-pypdf2 package for Ubuntu compatibility\n"
        )
        if not args.plan:
            with open("/tmp/python3-pypdf2.control", "w") as f:
                f.write(equivs_config)
        run_sys(["equivs-build", "python3-pypdf2.control"], cwd="/tmp")
        run_sys(["dpkg", "-i", "/tmp/python3-pypdf2_1.0_all.deb"])

    infrastructure.provision_environment(
        run_sys, env_vars, orig_user, os_id, is_test=args.test, hold_odoo=args.hold_odoo,
        plan=args.plan, opt_in_units=tuple(args.enable_opt_in),
    )
    if args.plan:
        return

    domain = env_vars.get("DOMAIN", "hams.com")
    _logger.info(f"""
======================================================================
[!] EMAIL REPUTATION ACTION REQUIRED
======================================================================
To ensure high deliverability and stay on the good side of mailing 
services (SES, Mailgun, etc.), you must add the following DNS records 
to your domain ({domain}):

1. SPF Record (TXT):
   Name: @
   Value: v=spf1 include:mailgun.org ~all  (replace with your provider)

2. DMARC Record (TXT):
   Name: _dmarc
   Value: v=DMARC1; p=quarantine; rua=mailto:admin@{domain};

3. DKIM Record (TXT):
   (Fetch this specific value from your email provider's dashboard)

The system is now configured to automatically append 'List-Unsubscribe' 
headers and compliance footers to all outgoing mail.
======================================================================
""")

if __name__ == "__main__":
    provision()
