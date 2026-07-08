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
        os.execvp(
            "sudo", ["sudo", "-H", "-E", sys.executable, os.path.abspath(__file__)]
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
    args, _ = parser.parse_known_args()

    if os_id not in ("ubuntu", "debian"):
        _logger.error(
            f"[!] Unsupported OS: {os_id}. Only Debian and Ubuntu are currently supported."
        )
        sys.exit(1)

    infrastructure.load_and_prompt_env(env_vars, args.test)

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


if __name__ == "__main__":
    provision()
