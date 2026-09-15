#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Install an apt hook that re-applies patch_odoo_hoot_navigator_mock.py after every Odoo upgrade
-----------------------------------------------------------------------------------------------
The nightly Odoo package (nightly.odoo.com) upgrades often, and each upgrade replaces
`hoot/mock/navigator.js`, silently dropping the one-line fix that script applies. Bruce asked for
the patch to be applied to the next upgrade automatically (2026-09-15).

What this installs (needs root):
- `/usr/local/sbin/hams-patch-odoo-hoot-navigator-mock`: a root-owned copy of the patch script.
  The hook runs this copy, not the git checkout under a user's home directory, so root never
  executes a file an unprivileged account can edit. Re-run this installer after changing the
  patch script.
- `/etc/apt/apt.conf.d/99hams-odoo-hoot-navigator-patch`: a `DPkg::Post-Invoke` entry. apt (and
  PackageKit, which uses the same libapt code) runs it after every dpkg run. The script returns
  immediately when navigator.js is already patched or Odoo isn't installed.

Idempotent. Run it again to refresh the installed copy:
    sudo python3 hams_shared/tools/install_odoo_hoot_patch_apt_hook.py
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATCH_SCRIPT = os.path.join(HERE, "patch_odoo_hoot_navigator_mock.py")
INSTALLED_SCRIPT = "/usr/local/sbin/hams-patch-odoo-hoot-navigator-mock"
APT_CONF = "/etc/apt/apt.conf.d/99hams-odoo-hoot-navigator-patch"

APT_CONF_CONTENT = f"""// Installed by hams_shared/tools/install_odoo_hoot_patch_apt_hook.py.
// Re-applies the hoot navigator.platform mock fix after every dpkg run, because each Odoo
// package upgrade replaces the patched file. A failure here means Odoo changed the mock's
// structure: see patch_odoo_hoot_navigator_mock.py.
DPkg::Post-Invoke {{"if [ -x {INSTALLED_SCRIPT} ]; then /usr/bin/python3 {INSTALLED_SCRIPT} --apt-hook; fi"; }};
"""


def main():
    if os.geteuid() != 0:
        print("🛑 Run as root: sudo python3 " + os.path.relpath(__file__))
        return 1

    shutil.copyfile(PATCH_SCRIPT, INSTALLED_SCRIPT)
    os.chown(INSTALLED_SCRIPT, 0, 0)
    os.chmod(INSTALLED_SCRIPT, 0o755)

    with open(APT_CONF, "w", encoding="utf-8") as f:
        f.write(APT_CONF_CONTENT)
    os.chown(APT_CONF, 0, 0)
    os.chmod(APT_CONF, 0o644)

    # Confirm apt parses the new entry, then apply the patch now rather than waiting for the next
    # dpkg run.
    dump = subprocess.run(["apt-config", "dump", "DPkg::Post-Invoke"], capture_output=True, text=True, check=True)
    if INSTALLED_SCRIPT not in dump.stdout:
        print(f"🛑 apt-config does not show the hook from {APT_CONF}:\n{dump.stdout}")
        return 1
    result = subprocess.run(["/usr/bin/python3", INSTALLED_SCRIPT], check=False)
    if result.returncode != 0:
        return result.returncode

    print(f"[+] Installed {INSTALLED_SCRIPT} and {APT_CONF}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
