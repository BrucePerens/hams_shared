#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Odoo Module Dependency Pre-Flight Check
---------------------------------------
Parses a module's __manifest__.py and verifies that all listed
dependencies exist within the provided addons paths.
"""

import os
import sys
import ast
import argparse

# Core Odoo modules this project does not itself own/tier -- shared by both
# the missing-dependency check and the tier-violation check below (moved to
# a single module-level constant, 2026-09-10 bug-hunt fix: these used to be
# only a local variable inside the missing-dependency check further down,
# so the tier-violation loop above it had no way to exempt them, see that
# loop's own comment for why that matters).
CORE_MODULES = {
    "base",
    "web",
    "website",
    "mail",
    "portal",
    "calendar",
    "bus",
    "website_blog",
    "website_sale",
    "contacts",
    "board",
    "auth_signup",
}


def parse_manifest(manifest_path):
    if not os.path.exists(manifest_path):
        print(f"❌ Error: Manifest file not found at '{manifest_path}'.")
        sys.exit(1)

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest_content = f.read()
            return ast.literal_eval(manifest_content)
    except Exception as e: # audit-ignore-catch-all
        import logging
        logging.getLogger(__name__).error(f"❌ Error parsing manifest at '{manifest_path}': {e}")
        print(f"❌ Error parsing manifest at '{manifest_path}': {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Pre-flight dependency check for Odoo modules."
    )
    parser.add_argument(
        "-m",
        "--module",
        required=True,
        help="Path to the target module directory",
    )
    parser.add_argument(
        "--addons-path", required=True, help="Comma-separated list of addons paths"
    )

    args = parser.parse_args()

    module_path = os.path.abspath(args.module)
    manifest_path = os.path.join(module_path, "__manifest__.py")

    manifest = parse_manifest(manifest_path)
    dependencies = manifest.get("depends", [])

    tier_config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tier_config.json"
    )
    TIERS = {}
    if os.path.exists(tier_config_path):
        import json

        with open(tier_config_path, "r", encoding="utf-8") as f:
            loaded_tiers = json.load(f)
            TIERS = {int(k): v for k, v in loaded_tiers.items()}
    else:
        # Bug-hunt fix (2026-09-10, bug class 5: silent-failure gate):
        # confirmed empirically that no `tier_config.json` exists anywhere
        # in either repo today, so this whole architecture-tier check has
        # always been dormant in the real, deployed environment -- every
        # invocation of this script silently behaves as though the check
        # ran and found nothing, with no signal that it is not actually
        # configured at all. This notice makes that state visible instead
        # of silent; it does not change has_errors/the exit code.
        print(
            f"[*] Module-tier architecture check skipped: {tier_config_path} "
            "not found (tiers not configured)."
        )

    def get_tier(mod_name):
        if not TIERS:
            return 99
        for tier, mods in TIERS.items():
            if mod_name in mods:
                return tier
        return 99

    module_name = os.path.basename(module_path)
    module_tier = get_tier(module_name)

    has_errors = False
    tier_violations = []
    for dep in dependencies:
        if dep in CORE_MODULES:
            # Bug-hunt fix (2026-09-10, latent false-positive): a core Odoo
            # module (base, web, mail, ...) is never itself entered in
            # tier_config.json (it isn't one of this project's own tiered
            # modules), so get_tier() would fall back to 99 for it --
            # `99 > module_tier` for any real tiered module, meaning the
            # single most common dependency in the entire codebase would
            # be flagged as an "architecture violation" the moment anyone
            # actually populates tier_config.json. Not live today (TIERS
            # is always {} in the real repo, so this loop never runs for
            # real yet -- see the dormancy notice above), but confirmed via
            # a fixture test that reproduces the false positive against
            # this exact code path. Mirrors the missing-dependency check
            # below, which already exempts CORE_MODULES for the same
            # reason.
            continue
        dep_tier = get_tier(dep)
        if dep_tier > module_tier and module_tier != 99:
            tier_violations.append(f"`{dep}` (Tier {dep_tier})")

    if tier_violations:
        print(
            f"\n❌ ARCHITECTURE VIOLATION: `{module_name}` (Tier {module_tier}) cannot depend on higher-tier modules:"
        )
        for v in tier_violations:
            print(f"   - {v}")
        has_errors = True

    if not dependencies:
        if has_errors:
            sys.exit(1)
        sys.exit(0)

    addons_paths = [p.strip() for p in args.addons_path.split(",") if p.strip()]

    missing_dependencies = []

    for dep in dependencies:
        if dep in CORE_MODULES:
            continue
        found = False
        for path in addons_paths:
            dep_path = os.path.join(path, dep)
            if os.path.isdir(dep_path) and os.path.exists(
                os.path.join(dep_path, "__manifest__.py")
            ):
                found = True
                break

        if not found:
            missing_dependencies.append(dep)

    if missing_dependencies:
        missing_formatted = [f"`{dep}`" for dep in missing_dependencies]
        paths_formatted = [f"`{p}`" for p in addons_paths]

        print(f"\n❌ PRE-FLIGHT CHECK FAILED for '{os.path.basename(module_path)}'")
        print(
            "   The following dependencies are missing from the provided addons paths:\n   - "
            + "\n   - ".join(missing_formatted)
        )
        print("\n   Searched Paths:\n   - " + "\n   - ".join(paths_formatted))
        has_errors = True

    if has_errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
