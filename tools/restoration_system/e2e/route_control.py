# SPDX-License-Identifier: AGPL-3.0-or-later
"""Negative control: boot the image in a VM that HAS a network device (QEMU user networking) and show verify fails closed.

Run as root by test_end_to_end.py. Exit 0 when the control behaved: isolation not proven, the production services were never started, and the
verdict is FAIL. This uses the function interface of verify (a parameter the command line does not expose)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from restoration_system import profile as prof  # noqa: E402
from restoration_system import verify as ver  # noqa: E402
from restoration_system.runner import RestorationError  # noqa: E402


def main(image, profile_path, base):
    profile = prof.load(profile_path)
    work = tempfile.mkdtemp(prefix=".route-control-", dir=base)
    report = ver.verify_image(image, profile, work, network="user", timeout=900)
    print(report.text())
    names = {c.name: c for c in report.checks}
    iso = names.get("isolation proven before any service started")
    not_started = names.get("production services were NOT started (fail closed)")
    ok = (not report.ok) and iso is not None and iso.status == "FAIL" and not_started is not None and not_started.status == "ok"
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main(*sys.argv[1:4]))
    except RestorationError as exc:
        print(exc)
        sys.exit(1)
