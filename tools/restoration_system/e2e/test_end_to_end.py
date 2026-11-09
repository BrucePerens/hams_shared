# SPDX-License-Identifier: AGPL-3.0-or-later
"""HEAVY end-to-end test of the restoration system on a synthetic backup set (throwaway passphrases, never real backups or keys).

Not collected by the lint (it is not named like the unit tests in tools/ and is marked `heavy`). Run it with the documented target:

    python3 hams_shared/tools/restoration_system/e2e/run_heavy.py

It needs: sudo without a password (debootstrap, chroot, mke2fs -d), the network (Debian archive, apt.postgresql.org), PostgreSQL 18 binaries,
pgbackrest, kopia, QEMU and /dev/kvm. It builds a synthetic PostgreSQL cluster in an encrypted pgBackRest repository and a kopia repository,
builds an image from them (the passphrases are typed at a pseudo-terminal, the production path), verifies it in the isolated VM, and runs the
negative controls: wrong passphrase, corrupt backup file, empty source, an image missing a unit, and a VM that has a route out.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, TOOLS)

from restoration_system.e2e import synthetic  # noqa: E402
from restoration_system.e2e.pty_driver import run_on_tty  # noqa: E402

pytestmark = pytest.mark.heavy
PROFILE = os.path.join(HERE, "synthetic_profile.json")
CLI = os.path.join(TOOLS, "restore_image.py")
BASE = os.environ.get("HAMS_RESTORATION_E2E_DIR", os.path.expanduser("~/.cache/hams-restoration-e2e"))


def cli(*args, answers=(), timeout=3600):
    return run_on_tty(["sudo", "-n", "python3", CLI, "--profile", PROFILE, *args], list(answers), timeout=timeout)


def answers_for(creds):
    return [("pgBackRest repository passphrase", creds["pgbackrest_passphrase"]), ("kopia repository password", creds["kopia_password"])]


@pytest.fixture(scope="module")
def world():
    subprocess.run(["sudo", "-n", "rm", "-rf", BASE], check=False)
    os.makedirs(BASE, mode=0o755)
    creds = synthetic.make(os.path.join(BASE, "backups"))
    yield {"creds": creds, "set": os.path.join(BASE, "backups"), "image": os.path.join(BASE, "image")}
    subprocess.run(["sudo", "-n", "rm", "-rf", BASE], check=False)


def test_01_negative_controls_that_fail_before_any_image_is_made(world):
    creds = dict(world["creds"], pgbackrest_passphrase="wrong-passphrase", kopia_password="wrong-too")
    code, text = cli("build", "--source", "local:" + world["set"], "--out", os.path.join(BASE, "x1"), answers=answers_for(creds))
    assert code != 0 and "wrong passphrase" in text, text[-600:]
    empty = os.path.join(BASE, "empty")
    os.makedirs(empty, exist_ok=True)
    code, text = cli("build", "--source", "local:" + empty, "--out", os.path.join(BASE, "x2"), answers=[])
    assert code != 0 and "missing or empty" in text
    plain = os.path.join(BASE, "plain")
    os.makedirs(os.path.join(plain, "hams1-backup", "db"), exist_ok=True)
    code, text = cli("build", "--source", "local:" + plain, "--out", os.path.join(BASE, "x3"), answers=[])
    assert code != 0 and "no encrypted source" in text


def test_02_corrupt_backup_file_fails_the_build(world):
    bad = os.path.join(BASE, "corrupt")
    shutil.copytree(world["set"], bad)
    skip = (".info", ".history", ".backup", ".manifest", ".copy")
    wal = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(bad, "synth-pgbackrest", "archive")) for f in fs if not f.endswith(skip)]
    assert wal, "the synthetic set holds archived WAL"
    for path in wal:
        with open(path, "r+b") as handle:
            handle.seek(40)
            handle.write(b"\xff\xff\xff\xff")
    code, text = cli("build", "--source", "local:" + bad, "--out", os.path.join(BASE, "x4"), answers=answers_for(world["creds"]))
    assert code != 0, text[-800:]
    assert not os.path.exists(os.path.join(BASE, "x4", "usr")), "no image may be left behind that looks complete"


def test_03_build_the_image(world):
    code, text = cli("build", "--source", "local:" + world["set"], "--out", world["image"], answers=answers_for(world["creds"]))
    assert code == 0, text[-2500:]
    assert "contains decrypted data and every secret" in text
    for rel in ("EXCLUDE.txt", "RESTORE.md", "RESTORATION_META.json", "usr/local/sbin/hams-restore-post", "var/www/synth/index.html"):
        assert os.path.exists(os.path.join(world["image"], rel)), rel
    assert not os.path.exists(os.path.join(world["image"], "etc/fstab")) and not os.path.exists(os.path.join(world["image"], "etc/hostname"))
    auto = subprocess.run(["sudo", "-n", "cat", os.path.join(world["image"], "var/lib/postgresql/18/main/postgresql.auto.conf")], capture_output=True, text=True).stdout
    assert "restore_command" in auto and "wal_restore" in auto


def test_04_verify_the_image_in_the_isolated_vm(world):
    expected = os.path.join(BASE, "expected.json")
    with open(expected, "w") as handle:
        json.dump(world["creds"]["expected"], handle)
    code, text = cli("verify", world["image"], "--expected", expected, "--work", BASE)
    print(text)
    assert code == 0, text[-3000:]
    for line in ("isolation proven before any service started", "host canary saw no packet", "PostgreSQL started and finished recovery",
                 "row counts equal the backup's metadata", "newest transaction time equals", "site answers http://127.0.0.1/"):
        assert any(line in row and row.startswith("ok") for row in text.splitlines()), line


def test_05_image_missing_a_unit_fails_verify(world):
    broken = os.path.join(BASE, "broken")
    subprocess.run(["sudo", "-n", "cp", "-a", world["image"], broken], check=True)
    subprocess.run(["sudo", "-n", "rm", "-f", os.path.join(broken, "etc/systemd/system/multi-user.target.wants/nginx.service")], check=True)
    code, text = cli("verify", broken, "--work", BASE)
    assert code == 1 and "nginx.service is active" in text and "FAIL" in text, text[-1500:]


def test_06_vm_with_a_route_out_fails_verify_before_services_start(world):
    done = subprocess.run(["sudo", "-n", sys.executable, os.path.join(HERE, "route_control.py"), world["image"], PROFILE, BASE], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout[-2500:] + done.stderr[-800:]
