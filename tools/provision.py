#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Standalone Environment Provisioning Script
Must be run as root.
"""
import os
import re
import sys
import subprocess
import logging

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, repo_root)
import infrastructure  # noqa: E402

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

    import argparse
    parser = argparse.ArgumentParser(description="Standalone Environment Provisioning Script")
    parser.add_argument("--test", action="store_true", help="Smoke-test all daemons and stop them after")
    parser.add_argument("--force-reset", action="store_true", help="Completely destroy the existing database, filestore, and cache before provisioning")
    args, _ = parser.parse_known_args()

    if os_id not in ("ubuntu", "debian"):
        _logger.error(
            f"[!] Unsupported OS: {os_id}. Only Debian and Ubuntu are currently supported."
        )
        sys.exit(1)

    infrastructure.load_and_prompt_env(env_vars, args.test)

    if args.force_reset:
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
        _logger.info(f"[*] Running: {' '.join(cmd)}")
        if "env" not in kw:
            kw["env"] = env_vars
        return subprocess.run(cmd, check=True, **kw)

    if os_id == "debian":
        _logger.info("[*] Generating dummy python3-pypdf2 package for Debian compatibility")
        run_sys(["apt-get", "update", "-y"])
        run_sys(["apt-get", "install", "-y", "equivs"])
        equivs_config = (
            "Section: python\n"
            "Priority: optional\n"
            "Standards-Version: 4.1.4\n"
            "Package: python3-pypdf2\n"
            "Version: 1.0\n"
            "Depends: python3-pypdf\n"
            "Description: Dummy python3-pypdf2 package for Ubuntu compatibility\n"
        )
        with open("/tmp/python3-pypdf2.control", "w") as f:
            f.write(equivs_config)
        run_sys(["equivs-build", "python3-pypdf2.control"], cwd="/tmp")
        run_sys(["dpkg", "-i", "/tmp/python3-pypdf2_1.0_all.deb"])

    infrastructure.provision_environment(run_sys, env_vars, orig_user, os_id, is_test=args.test)

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
