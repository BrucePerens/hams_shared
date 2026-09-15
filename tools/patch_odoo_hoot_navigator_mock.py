#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Patch Odoo core's hoot navigator mock to cover navigator.platform
---------------------------------------------------------------------
Real, confirmed Odoo-core bug (root-caused 2026-09-08, see hams_com's night_shift_todo.md
"RESOLVED: root cause of the hoot JS unit-test harness failure found" entry for the full trace):
`odoo/addons/web/static/lib/hoot/mock/navigator.js`'s `createMock(navigator, {...})` builds a
look-alike mock object installed as `window.navigator` for the whole hoot test session
(`hoot/mock/window.js`'s `WINDOW_MOCK_DESCRIPTORS.navigator`). Its override list --
`clipboard`, `maxTouchPoints`, `permissions`, `sendBeacon`, `serviceWorker`, `userAgent`,
`vibrate` -- omits `platform`. `createMock()`'s own "copy original descriptors" loop only copies
an object's OWN enumerable keys, and `platform` is defined on `Navigator.prototype` (or a mixin
prototype), not as an own key of whatever level `createMock()`'s "walk up while no own keys"
search lands on -- so `mockNavigator.platform` falls through to the real native
`Navigator.prototype.platform` getter, invoked with the mock as `this`. Chrome's branded-accessor
check on that native getter rejects a non-genuine receiver: `TypeError: Illegal invocation`.

Any application code that reads `navigator.platform` while hoot's window mock is active throws
this -- which is deterministic and unconditional for `@barcodes/barcode_service` specifically
(Odoo core's own `barcode_service.js` calls `isMobileOS()` -> `isIOS()` -> reads
`browser.navigator.platform` at module top level, so hoot's own dry-run phase, which loads every
addon's test-adjacent modules regardless of which test tag is requested, hits this on literally
every hoot run in either repo). Not a hams bug (both the mock and the application code it breaks
are unmodified Odoo core), not reachable in a real user's browser (the mock only exists inside
hoot's own test harness) -- but it currently blocks verifying ANY hoot-based JS unit test.

This script applies the one-line fix hoot's own mock override list is missing (an explicit
`platform: { get: () => navigator.platform }` entry, delegating to the real navigator exactly the
way the existing `userAgent`/`sendBeacon`/`vibrate` overrides already do) directly to the
installed Odoo package. Idempotent: running it again when already patched is a clean no-op, not an
error. This is a real edit to a system file outside both hams git repos (Odoo's own installed
package, not vendored into either repo), so an Odoo package reinstall/upgrade replaces it. The
nightly package upgrades often, so `install_odoo_hoot_patch_apt_hook.py` (next to this file)
installs a copy of this script as an apt `DPkg::Post-Invoke` hook that re-applies the patch after
every dpkg run (`--apt-hook` mode below). Re-run that installer after changing this script, since
the hook runs the installed copy, not this file. Kept here, in git,
specifically so the fix is reproducible on every dev environment rather than being a one-off
manual edit only this session remembers.

Run directly (needs root to write into the installed Odoo package, same as
diagnose_hoot_module_load_error.py's write_as_root()):
    python3 hams_shared/tools/patch_odoo_hoot_navigator_mock.py
"""

import importlib.util
import os
import subprocess
import sys

_TARGET_LINE = "    vibrate: { get: () => mockValues.vibrate },"
_PATCH_LINE = "    platform: { get: () => navigator.platform },"


def find_navigator_mock_path():
    """Returns navigator.js's path, or None when no Odoo package is installed. Uses find_spec
    rather than importing odoo, so the apt hook doesn't pay for an Odoo import on every dpkg run."""
    spec = importlib.util.find_spec("odoo")
    if spec is None or not spec.submodule_search_locations:
        return None
    odoo_pkg_dir = list(spec.submodule_search_locations)[0]
    return "/".join([odoo_pkg_dir, "addons", "web", "static", "lib", "hoot", "mock", "navigator.js"])


def already_patched(content):
    return _PATCH_LINE in content


def apply_patch(content):
    """Returns the patched content. Raises ValueError if the expected anchor line isn't found
    (a real, useful failure mode if a future Odoo version restructures this mock -- silently
    patching nothing, or patching the wrong spot, would be worse than erroring here)."""
    if already_patched(content):
        return content
    count = content.count(_TARGET_LINE)
    if count != 1:
        raise ValueError(
            f"expected exactly one occurrence of {_TARGET_LINE!r} in navigator.js, found {count} "
            "-- hoot's own mock structure may have changed since this patch was written"
        )
    return content.replace(_TARGET_LINE, _TARGET_LINE + "\n" + _PATCH_LINE)


def write_as_root(path, content):
    if os.geteuid() == 0:
        # The apt hook already runs as root.
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return
    subprocess.run(["sudo", "tee", path], input=content, capture_output=True, text=True, check=True)


def main():
    # --apt-hook: run from DPkg::Post-Invoke after every dpkg run. A machine without Odoo, or a
    # web addon without hoot, is a silent no-op there. A changed mock structure still fails loudly
    # (apt reports "Problem executing scripts DPkg::Post-Invoke"), so the patch can't silently stop
    # being applied.
    apt_hook = "--apt-hook" in sys.argv[1:]
    path = find_navigator_mock_path()
    if path is None or not os.path.exists(path):
        if apt_hook:
            return 0
        print(f"🛑 Odoo's hoot navigator.js not found (looked for {path}).")
        return 1
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    if already_patched(content):
        if not apt_hook:
            print(f"[+] {path} already patched -- nothing to do.")
        return 0

    try:
        patched = apply_patch(content)
    except ValueError as e:
        print(f"🛑 hams hoot navigator patch: {e}", file=sys.stderr)
        return 1
    write_as_root(path, patched)

    with open(path, "r", encoding="utf-8") as f:
        verify = f.read()
    if not already_patched(verify):
        print(f"🛑 WARNING: {path} does not verify as patched after writing -- check it by hand.")
        return 1

    print(f"[+] Patched {path}: navigator.platform now safely delegates to the real navigator "
          "in hoot's own window mock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
