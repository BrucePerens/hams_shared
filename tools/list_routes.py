#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright © Bruce Perens K6BP. All Rights Reserved.
# This software is proprietary and confidential.

"""
Utility to list all registered web routes in the operating Odoo environment.
Extracts the Werkzeug routing map from the `ir.http` model.
"""

import argparse
import sys
import os
import threading

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

    try:
        import odoo
        import odoo.tools.config
        from odoo import api, SUPERUSER_ID
        import odoo.modules.registry
    except ImportError as e:
        print(f"Error importing odoo: {e}")
        print("Ensure you are running this with the PYTHONPATH bridge set.")
        sys.exit(1)

    # Bootstrap the Odoo Environment
    odoo.tools.config.parse_config(["-c", args.config, "-d", args.database])

    try:
        # Explicitly initialize the Registry class to avoid the attribute error
        registry = odoo.modules.registry.Registry(args.database)
    except Exception as e:
        print(f"Error initializing registry for database '{args.database}': {e}")
        sys.exit(1)

    # FIX: Odoo's internal cache lookups expect the thread to carry the dbname
    threading.current_thread().dbname = args.database

    with registry.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})

        print(f"\n{'='*110}")
        print(f"{'ROUTE PATH':<65} | {'METHODS':<15} | {'ENDPOINT FUNCTION'}")
        print(f"{'='*110}")

        # Extract the routing map from ir.http
        routing_map = env["ir.http"].routing_map()

        # Sort rules alphabetically by path for readability
        rules = sorted(routing_map.iter_rules(), key=lambda r: r.rule)

        for rule in rules:
            methods = ",".join(rule.methods) if rule.methods else "ALL"
            endpoint_name = (
                rule.endpoint.__name__
                if hasattr(rule.endpoint, "__name__")
                else str(rule.endpoint)
            )
            print(f"{rule.rule:<65} | {methods:<15} | {endpoint_name}")

        print(f"{'='*110}\n")
        print(f"Total Routes: {len(rules)}")


if __name__ == "__main__":
    main()
