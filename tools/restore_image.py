#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""restore_image.py: build, verify and describe a restored-system directory. See docs/runbooks/RESTORATION_SYSTEM.md (hams_com).

    sudo python3 restore_image.py build  --source local:/storage --out /srv/hams-image [--target-time 'YYYY-MM-DD HH:MM:SS+00']
    sudo python3 restore_image.py build  --source b2 --out /srv/hams-image
    sudo python3 restore_image.py verify /srv/hams-image [--expected expected.json]
    python3 restore_image.py coverage --source local|b2

The backup passphrases (and for Backblaze the B2 application key id and key) are asked for on the terminal; there is no option, environment
variable or file for them.
"""
import argparse
import json
import os
import sys
import tempfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from restoration_system import build as bld  # noqa: E402
from restoration_system import coverage as cov  # noqa: E402
from restoration_system import profile as prof  # noqa: E402
from restoration_system import verify as ver  # noqa: E402
from restoration_system.runner import RestorationError, Runner  # noqa: E402
from restoration_system.secrets_input import Secrets  # noqa: E402


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile", help="profile JSON (default: the hams.com profile)")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="backups -> image directory")
    b.add_argument("--source", required=True, help="local:DIRECTORY (for example local:/storage) or b2")
    b.add_argument("--out", required=True, help="new directory to build the image in (must be empty or absent)")
    b.add_argument("--target-time", help="point-in-time recovery target, e.g. '2026-11-09 03:00:00+00' (default: the end of the archived WAL)")
    v = sub.add_parser("verify", help="boot the image in an isolated VM and test it")
    v.add_argument("image")
    v.add_argument("--expected", help="JSON {counts: {table: n}, newest: 'YYYY-MM-DDTHH:MM:SSZ'} from the backup's metadata")
    v.add_argument("--work", help="scratch directory for the VM disk (default: beside the image)")
    pf = sub.add_parser("preflight", help="check this builder: root, programs, /dev/kvm, disk")
    pf.add_argument("--out", help="the directory you will build in (its disk is checked)")
    c = sub.add_parser("coverage", help="what is restored from backups, what from provisioning, what is a gap")
    c.add_argument("--source", choices=["local", "b2"], required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    profile = prof.load(args.profile)
    try:
        if args.command == "preflight":
            checks = bld.preflight(args.out)
            for ok, text in checks:
                print(("ok   " if ok else "MISSING ") + text)
            return 0 if all(ok for ok, _ in checks) else 1
        if args.command == "coverage":
            report = cov.build_coverage(bld.load_infrastructure(), profile, args.source, profile.get("host_classes", []))
            print(cov.render(report, args.source))
            return 0
        if args.command == "build":
            bld.build_image(args.source, os.path.abspath(args.out), profile, Runner(), Secrets(), args.target_time)
            return 0
        image = os.path.abspath(args.image)
        expected = json.load(open(args.expected)) if args.expected else None
        work = tempfile.mkdtemp(prefix=".verify-", dir=args.work or os.path.dirname(image))
        try:
            report = ver.verify_image(image, profile, work, expected)
        finally:
            import shutil
            shutil.rmtree(work, ignore_errors=True)
        print(report.text())
        return 0 if report.ok else 1
    except RestorationError as exc:
        sys.stderr.write("restore_image: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
