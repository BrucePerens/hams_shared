#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
# Copyright © Bruce Perens K6BP. All Rights Reserved.

"""
Utility to list all registered web routes in the operating Odoo environment.
Extracts the Werkzeug routing map from the `ir.http` model.
"""

import argparse
import logging
import sys
import os
import threading

# Real fix, 2026-09-12 (hams_shared/tools/ 326-finding discovery, LOCAL
# IMPORT + CRITICAL FAST FAIL overlap): these four used to be a
# try/except ImportError block inside main(), run only after the
# config-existence check. That wasn't a genuine optional-dependency
# pattern (contrast odoo_registry_builder.py's find_odoo_core_addons_path(),
# which really does degrade gracefully to "core coverage unavailable
# here") -- the except branch here still did a hard sys.exit(1) either
# way, just with a hand-written message instead of Python's own
# ImportError traceback, which is strictly more diagnostic. This tool's
# entire purpose is reading a live Odoo routing map, so odoo is a real,
# unconditional dependency; importing it plainly at module scope is the
# actual fail-fast behavior the project's "no soft dependencies" rule
# asks for. See test_list_routes.py's own updated comment for what this
# changes (and doesn't change) about --help/missing-config behavior.
import odoo
import odoo.tools.config
from odoo import api
import odoo.modules.registry


def main():
    parser = argparse.ArgumentParser(description="List all active Odoo web routes.")
    parser.add_argument(
        "-d",
        "--database",
        required=False,
        default="hams_prod",
        help="Odoo Database Name",
    )
    parser.add_argument(
        "-c",
        "--config",
        required=False,
        default="/opt/hams/etc/odoo.conf",
        help="Path to odoo.conf",
    )
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Error: Configuration file not found at {args.config}")
        sys.exit(1)

    # Bootstrap the Odoo Environment
    odoo.tools.config.parse_config(["-c", args.config, "-d", args.database])

    try:
        # Explicitly initialize the Registry class to avoid the attribute error
        registry = odoo.modules.registry.Registry(args.database)
    except Exception as e: # audit-ignore-catch-all
        logging.getLogger(__name__).error(f"Error initializing registry for database '{args.database}': {e}")
        print(f"Error initializing registry for database '{args.database}': {e}")
        sys.exit(1)

    # FIX: Odoo's internal cache lookups expect the thread to carry the dbname
    threading.current_thread().dbname = args.database

    with registry.cursor() as cr:
        # Real fix, 2026-09-14 (night_shift_todo.md's "OPEN QUESTION for Bruce" ->
        # "FIXED" entry): this used to do `env = api.Environment(cr, SUPERUSER_ID, {})`
        # and use THAT env for the real routing-map read below -- a CRITICAL ZERO-SUDO
        # VIOLATION (check_burn_list.py). The complication that left this unfixed for a
        # while: `zero_sudo.security.utils._get_service_uid(xml_id)` (the only sanctioned
        # way to resolve a real, scoped service account's uid) is itself an `@api.model`
        # method on a real Odoo model, so calling it at all requires SOME existing
        # Environment to look the account up through -- there's no way to bootstrap a
        # scoped-uid Environment without first having *an* Environment. Bruce's own
        # explicit decision, 2026-09-14: keep a transient SUPERUSER_ID environment used
        # ONLY to resolve the real uid, discard it immediately, then build a second, real
        # environment with that resolved uid for all actual work.
        #
        # `zero_sudo.odoo_facility_service_internal` is the right account for this job,
        # not a new one: it's this codebase's own already-established "generic internal
        # tooling / framework-metadata read" service account (see
        # zero_sudo/models/ir_module_module.py's own `ir.module.module` reads and
        # check_burn_list.py's own "CRITICAL FRAMEWORK ACL" rule, which names this exact
        # account for framework-table reads that fail without at least `base.group_user`)
        # -- `ir.http.routing_map()` builds the Werkzeug routing map from registered
        # controllers, not from any row-level-ACL-guarded table, so no MORE privileged
        # account is needed; a bare, ungrouped service account holding no groups at all
        # would already be sufficient in principle, but reusing this established account
        # (rather than registering a new one for a one-line read) keeps the service-
        # account roster from growing for no real capability gain.
        bootstrap_env = api.Environment(cr, api.SUPERUSER_ID, {})  # audit-ignore-superuser-bootstrap-for-service-uid-resolution: transient bootstrap ONLY, used once immediately below to resolve zero_sudo.odoo_facility_service_internal's real uid via _get_service_uid(), then discarded on the next line -- never used for any other read or write. See list_routes.py's own comment above this line for the full rationale.
        svc_uid = bootstrap_env["zero_sudo.security.utils"]._get_service_uid(
            "zero_sudo.odoo_facility_service_internal"
        )
        del bootstrap_env  # discard immediately -- must never be used again past this point

        env = api.Environment(cr, svc_uid, {})
        assert not env.su, "the real working environment must not be superuser-mode"

        print(f"\n{'='*110}")
        print(f"{'ROUTE PATH':<65} | {'METHODS':<15} | {'ENDPOINT FUNCTION'}")
        print(f"{'='*110}")

        # Extract the routing map from ir.http
        routing_map = env["ir.http"].routing_map()

        # Sort rules alphabetically by path for readability
        rules = sorted(routing_map.iter_rules(), key=lambda r: r.rule)

        for rule in rules:
            methods = ",".join(rule.methods) if rule.methods else "ALL"
            # Real fix, 2026-09-12 (hams_shared/tools/ 326-finding discovery, CRITICAL AI
            # LAZINESS: Catch-all AttributeError): rule.endpoint is heterogeneous (a plain
            # function has __name__; a bound method, functools.partial, or Controller
            # instance may not) -- getattr() with a default expresses that directly, same
            # end result, no except block to mask an unrelated AttributeError from
            # elsewhere in the expression.
            endpoint_name = getattr(rule.endpoint, "__name__", None) or str(rule.endpoint)
            print(f"{rule.rule:<65} | {methods:<15} | {endpoint_name}")

        print(f"{'='*110}\n")
        print(f"Total Routes: {len(rules)}")


if __name__ == "__main__":
    main()
