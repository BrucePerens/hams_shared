# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests of image assembly: package list, accounts, machine-specific exclusion, the post step, units and enablement."""
import os
import subprocess


import infrastructure as infra
from restoration_system import accounts as acc
from restoration_system import assemble as asm
from restoration_system import post_step as post
from restoration_system import profile as prof
from restoration_system import units as un

STATUS = "Package: nginx\nStatus: install ok installed\nVersion: 1\n\nPackage: gone\nStatus: deinstall ok config-files\nVersion: 2\n"


def test_parse_dpkg_status_lists_installed_only():
    assert asm.parse_dpkg_status(STATUS) == {"nginx"}


def test_package_list_unions_manifest_profile_backup_and_reports_origin():
    profile = {"extra_packages": ["kopia"], "package_not_in_archive": ["python3-pypdf2"]}
    packages = asm.package_list(infra, profile, {"extra-from-backup"})
    assert "odoo" in packages["odoo"] or packages["odoo"] == ["MANIFEST"]
    assert packages["kopia"] == ["profile"] and packages["extra-from-backup"] == ["backup dpkg status"]
    assert "python3-pypdf2" not in packages


def test_derive_ids_from_owners_and_conflicts():
    claims = [("/a", "odoo", "odoo"), ("/b", "hams_com", "hams_com"), ("/c", "odoo", "odoo")]
    users, groups, conflicts = acc.derive_ids(claims, {"/a": (110, 111), "/b": (990, 990), "/c": (110, 111)})
    assert users["odoo"] == 110 and groups["hams_com"] == 990 and not conflicts
    _, _, conflicts = acc.derive_ids(claims, {"/a": (110, 111), "/c": (120, 111)})
    assert any("odoo is 110 on one path and 120" in c for c in conflicts)
    _, _, conflicts = acc.derive_ids([("/a", "u1", "g"), ("/b", "u2", "g")], {"/a": (5, 5), "/b": (5, 5)})
    assert any("uid 5 is used by both" in c for c in conflicts)


def test_account_commands_carry_recovered_numbers_and_report_unresolved():
    manifest = {"system_accounts": [{"user": "a", "group": "a", "home": "/h"}, {"user": "b", "group": "b"}]}
    commands, unresolved = acc.account_commands(manifest, {"a": 777}, {"a": 778})
    argvs = [c[2] for c in commands]
    assert ["groupadd", "--system", "-g", "778", "a"] in argvs
    assert any(a[:4] == ["useradd", "--system", "-g", "a"] and "-u" in a and "777" in a for a in argvs)
    assert unresolved == ["b"]


def test_strip_machine_specific_and_exclude_file(tmp_path):
    for rel in ("etc/fstab", "etc/hostname", "etc/ssh/ssh_host_rsa_key", "etc/ssh/ssh_host_rsa_key.pub", "etc/ssh/sshd_config", "usr/bin/keep"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    removed = asm.strip_machine_specific(str(tmp_path), ["etc/fstab", "etc/hostname", "etc/ssh/ssh_host_*"])
    assert sorted(removed) == ["etc/fstab", "etc/hostname", "etc/ssh/ssh_host_rsa_key", "etc/ssh/ssh_host_rsa_key.pub"]
    assert (tmp_path / "etc/ssh/sshd_config").exists() and (tmp_path / "usr/bin/keep").exists()


def test_rsync_with_exclude_file_leaves_the_new_machines_own_files_alone(tmp_path):
    profile = prof.load()
    image, target = tmp_path / "image", tmp_path / "target"
    for rel, text in (("etc/fstab", "IMAGE"), ("etc/hostname", "IMAGE"), ("etc/passwd", "IMAGE"), ("var/lib/dpkg/status", "IMAGE"),
                      ("etc/ssh/ssh_host_ed25519_key", "IMAGE"), ("opt/hams/etc/core.env", "IMAGE"), ("etc/odoo/odoo.conf", "IMAGE")):
        (image / rel).parent.mkdir(parents=True, exist_ok=True)
        (image / rel).write_text(text)
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        (target / rel).write_text("NEW")
    asm.write_exclude(str(image), profile)
    (target / "etc/odoo/odoo.conf").unlink()
    subprocess.run(["rsync", "-aHAX", "--numeric-ids", "--exclude-from=" + str(image / "EXCLUDE.txt"), str(image) + "/", str(target) + "/"], check=True)
    for kept in ("etc/fstab", "etc/hostname", "etc/passwd", "var/lib/dpkg/status", "etc/ssh/ssh_host_ed25519_key"):
        assert (target / kept).read_text() == "NEW", kept
    assert (target / "opt/hams/etc/core.env").read_text() == "IMAGE"
    assert (target / "etc/odoo/odoo.conf").read_text() == "IMAGE"
    assert not (target / "EXCLUDE.txt").exists() and not (target / "RESTORE.md").exists()


def test_post_step_accounts_create_conflict_and_idempotence(tmp_path):
    etc = tmp_path
    (etc / "group").write_text("root:x:0:\nsshd:x:100:\n")
    (etc / "passwd").write_text("root:x:0:0::/root:/bin/bash\nsshd:x:100:100::/run:/usr/sbin/nologin\n")
    wanted = {"groups": [["odoo", 110], ["hams", 100]], "users": [["odoo", 110, 110, "/var/lib/odoo", "/usr/sbin/nologin"]]}
    gcmds, ucmds, conflicts = post.plan_accounts(wanted, str(etc))
    assert ["groupadd", "-g", "110", "odoo"] in gcmds and ucmds[0][:3] == ["useradd", "-M", "-u"]
    assert any("gid 100 is used here by sshd" in c for c in conflicts)
    (etc / "group").write_text("root:x:0:\nodoo:x:110:\n")
    (etc / "passwd").write_text("root:x:0:0::/root:/bin/bash\nodoo:x:110:110::/var/lib/odoo:/usr/sbin/nologin\n")
    assert post.plan_accounts({"groups": [["odoo", 110]], "users": [["odoo", 110, 110, "/var/lib/odoo", "/x"]]}, str(etc)) == ([], [], [])


def test_post_step_merges_package_database_keeping_new_machines_kernel(tmp_path):
    share, dpkg = tmp_path / "share", tmp_path / "dpkg"
    (share / "dpkg-info").mkdir(parents=True)
    (share / "dpkg-info" / "nginx.list").write_text("/usr/sbin/nginx\n")
    dpkg.mkdir()
    (share / "dpkg-status").write_text("Package: nginx\nStatus: install ok installed\nVersion: 2\n")
    (dpkg / "status").write_text("Package: linux-image\nStatus: install ok installed\nVersion: 6\n\nPackage: nginx\nStatus: install ok installed\nVersion: 1\n")
    assert post.merge_dpkg_database(str(share), str(dpkg)) == 2
    text = (dpkg / "status").read_text()
    assert "linux-image" in text and "Version: 2" in text and "Version: 1" not in text
    assert (dpkg / "info" / "nginx.list").exists()
    assert post.merge_dpkg_database(str(share), str(dpkg)) == 2


def test_expected_units_follow_provisioning_rules():
    units = un.expected_units(infra, ["b2_backup"])
    assert units, "the MANIFEST has production units"
    external = {u: i for u, i in units.items() if i["external_fetch"]}
    assert external, "the MANIFEST marks external-fetch units"
    assert all(i["enabled"] for u, i in external.items() if u.endswith(".timer") and not i["opt_in"])
    assert not any(i["enabled"] for i in units.values() if i["opt_in"])


def test_enablement_plan_keeps_restored_links_and_enables_the_rest(tmp_path):
    (tmp_path / "etc/systemd/system/timers.target.wants").mkdir(parents=True)
    os.symlink("/opt/hams/systemd/a.timer", tmp_path / "etc/systemd/system/timers.target.wants/a.timer")
    units = {"a.timer": {"enabled": True, "path": "/opt/hams/systemd/a.timer"}, "b.timer": {"enabled": True, "path": "/opt/hams/systemd/b.timer"},
             "c.service": {"enabled": False, "path": "/opt/hams/systemd/c.service"}}
    to_enable, kept = un.enablement_plan(str(tmp_path), units)
    assert to_enable == ["b.timer"] and kept == ["a.timer"]

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: assemble:assemble_chroot_class]
