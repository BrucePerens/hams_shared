#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print the exact text provisioning would write for one infrastructure.MANIFEST `static_files` entry (read-only).

    python3 hams_shared/tools/render_static_file.py /etc/logrotate.d/odoo | sudo tee /etc/logrotate.d/odoo
    python3 hams_shared/tools/render_static_file.py /opt/hams/systemd/foo.service --env DOMAIN=hams.com

Use this, never a copy of the MANIFEST text, to install an entry by hand: MANIFEST content is a str.format template, so a
literal brace is stored doubled and a hand copy writes "{{ ... }}" (found 2026-10-11 in /etc/logrotate.d/odoo on hams1).
Placeholders the entry uses must be given with --env KEY=VALUE; an unknown one is an error, as in provisioning.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import infrastructure  # noqa: E402


def render(path, env_vars=None):
    for spec in infrastructure.MANIFEST["static_files"]:
        if spec["path"] == path and "content" in spec:
            return infrastructure.render_static_content(spec, dict(env_vars or {}))
    raise KeyError(f"no static_files entry with inline content for {path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="the entry's path, as in the MANIFEST (e.g. /etc/logrotate.d/odoo)")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="a placeholder value (repeatable)")
    args = parser.parse_args(argv)
    env_vars = dict(item.split("=", 1) for item in args.env)
    try:
        sys.stdout.write(render(args.path, env_vars))
    except (KeyError, ValueError) as error:
        print(f"render_static_file: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
