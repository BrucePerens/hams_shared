#!/usr/bin/env python3
# This file is part of hams_open, an open source module.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Builds hams_open/daemons/cloudflared-ffi/libcloudflared.so for this host's CPU.

The `cloudflare` module's tunnel-daemon tests load that library. It is a per-CPU build artifact,
built offline from the in-repo Go source with the OS package golang-1.24-go and a C compiler.
The logic, and the reasons for it, live in infrastructure.py (cffi_build and friends, the
infrastructure:build_cloudflared_ffi anchor), which `provision.py --test` also runs as a hook.

Usage:  python3 hams_shared/tools/build_cloudflared_ffi.py [--ffi-dir DIR] [--check] [--force]
Exit 0 when the library is present and built for this CPU, 1 otherwise.
"""
import argparse
import os
import sys

import infrastructure


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ffi-dir", default=infrastructure.cloudflared_ffi_default_dir())
    parser.add_argument("--check", action="store_true", help="only report; build nothing")
    parser.add_argument("--force", action="store_true", help="rebuild even if up to date")
    args = parser.parse_args()
    lib = os.path.join(args.ffi_dir, infrastructure.CFFI_LIB_NAME)
    machine = infrastructure.cffi_host_machine()
    ok, reason = infrastructure.cffi_library_status(args.ffi_dir)
    if ok and not args.force:
        print(f"{lib} is up to date for {machine}")
        return 0
    if args.check:
        print(f"MISSING: {reason}")
        return 1
    try:
        print(f"built {infrastructure.cffi_build(args.ffi_dir)} for {machine}")
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
