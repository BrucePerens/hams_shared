#!/usr/bin/env python3
"""
Standalone Environment Provisioning Script
Must be run as root.
"""
import os
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
        os.execvp("sudo", ["sudo", "-H", "-E", sys.executable, os.path.abspath(__file__)])

    orig_user = os.environ.get("SUDO_USER") or os.environ.get("USER")
    env_vars = dict(os.environ)
    env_vars["DEBIAN_FRONTEND"] = "noninteractive"
    env_vars["REPO_ROOT"] = repo_root

    os_id = infrastructure.get_os_identifier()
    _logger.info(f"[*] Discovered OS: {os_id}")

    if os_id not in ("ubuntu", "debian"):
        _logger.error(f"[!] Unsupported OS: {os_id}. Only Debian and Ubuntu are currently supported.")
        sys.exit(1)

    def run_sys(cmd, **kw):
        _logger.info(f"[*] Running: {' '.join(cmd)}")
        if "env" not in kw:
            kw["env"] = env_vars
        return subprocess.run(cmd, check=True, **kw)

    infrastructure.provision_environment(run_sys, env_vars, orig_user, os_id)

if __name__ == "__main__":
    provision()
