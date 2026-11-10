# SPDX-License-Identifier: AGPL-3.0-or-later
"""`build`: backups -> image directory. The stages, in order, each logged; any failure stops the build (nothing is skipped silently)."""
import datetime
import os
import shutil
import sys
import tempfile

from .runner import RestorationError
from . import accounts as acc
from . import assemble as asm
from . import code_layer as code
from . import provision_layer as pl
from . import sourceset as ss
from . import units as un


def load_infrastructure():
    """hams_shared/tools/infrastructure.py (the provisioning MANIFEST). Imported lazily: it is large."""
    tools = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import infrastructure
    return infrastructure


REQUIRED_PROGRAMS = {"debootstrap": "debootstrap", "kopia": "kopia (https://kopia.io, the version the backups were made with)", "pgbackrest": "pgbackrest",
                     "zstd": "zstd", "rsync": "rsync", "gpg": "gnupg", "unshare": "util-linux", "tar": "tar", "mke2fs": "e2fsprogs", "debugfs": "e2fsprogs",
                     "qemu-system-x86_64": "qemu-system-x86", "useradd": "passwd", "systemctl": "systemd"}


def preflight(out=None, need_vm=True):
    """[(ok, text)] about this builder: root, programs, /dev/kvm, the host kernel files the VM boots, free disk. Changes nothing."""
    import shutil as sh
    checks = [(os.geteuid() == 0, "running as root (build and verify need it)")]
    for program, package in REQUIRED_PROGRAMS.items():
        checks.append((sh.which(program) is not None or os.path.exists("/usr/sbin/" + program), "%s present (apt package %s)" % (program, package)))
    release = os.uname().release
    for path in ("/boot/vmlinuz-" + release, "/boot/initrd.img-" + release):
        checks.append((os.access(path, os.R_OK), "%s readable (the VM boots the builder's own kernel)" % path))
    kvm = os.access("/dev/kvm", os.W_OK)
    checks.append((kvm, "/dev/kvm usable (a real VM; without it verify falls back to slow emulation)"))
    where = os.path.dirname(os.path.abspath(out)) if out else "."
    free = sh.disk_usage(where).free / 2 ** 30
    checks.append((free >= 40, "%.0f GiB free beside the output directory (need about 40 GiB plus twice the image size for verify)" % free))
    return checks


def open_source(spec, runner, secrets, profile, work):
    if spec == "b2":
        return ss.open_b2(runner, secrets, profile, work)
    if spec.startswith("local:"):
        return ss.open_local(spec[len("local:"):], runner, secrets, profile, work)
    raise RestorationError("source must be local:DIRECTORY or b2, not %r" % spec)


def check_output_dir(out):
    if os.path.exists(out) and os.listdir(out):
        raise RestorationError("output directory %s exists and is not empty: refusing to build over it" % out)
    os.makedirs(out, exist_ok=True)
    os.chmod(out, 0o755)


def config_member_text(runner, archive, member):
    """Text of one member of a config archive, or None when it is not in it."""
    listing = runner.run(["tar", "--zstd", "-tf", archive], capture=True).stdout.splitlines()
    if member not in listing:
        return None
    return runner.run(["tar", "--zstd", "-xOf", archive, member], capture=True).stdout


def restore_files(bs, items, stage, work, runner, say):
    """Restore every FileItem into the staging tree (a mirror of /). Config archives are extracted there too, except the package database and
    the account files, which stay the image's own. Returns the archive paths (their member lists are read again for owners)."""
    archives = []
    for item in items:
        if item.kind == "config_tar":
            dest = os.path.join(work, "config-" + str(len(archives)))
            bs.kopia.restore(item.snapshot["id"], dest)
            found = sorted(f for f in os.listdir(dest) if f.startswith("config-") and f.endswith(".tar.zst"))
            if not found:
                raise RestorationError("%s holds no config archive" % item.label)
            archive = os.path.join(dest, found[-1])
            ex = ["--exclude=./var/lib/dpkg", "--exclude=./etc/passwd", "--exclude=./etc/group", "--exclude=./etc/shadow", "--exclude=./etc/gshadow",
                  "--exclude=var/lib/dpkg", "--exclude=etc/passwd", "--exclude=etc/group", "--exclude=etc/subuid", "--exclude=etc/subgid"]
            runner.run(["tar", "--zstd", "-xpf", archive, "--numeric-owner", "--xattrs", "--same-owner", *ex, "-C", stage])
            archives.append(archive)
            say("config archive %s extracted" % found[-1])
        else:
            say(ss.restore_item(item, bs, stage, work, runner))
    return archives


def build_image(source, out, profile, runner, secrets, target_time=None, say=print, work_parent=None, fetch=None):
    """Build the image directory `out` from `source`. Returns the RESTORATION_META.json dict.

    Needs root (debootstrap, chroot, ownership), the network (Debian archive, third-party apt repositories) and the passphrases (asked on the
    terminal when the source is opened). The directory holds decrypted data and every secret: it is the restored system."""
    if os.geteuid() != 0:
        raise RestorationError("build needs root (debootstrap, chroot, restoring owners): run it with sudo")
    infra = load_infrastructure()
    check_output_dir(out)
    work = tempfile.mkdtemp(prefix=".restoration-work-", dir=work_parent or os.path.dirname(os.path.abspath(out)))
    os.chmod(work, 0o700)
    bs = None
    try:
        say("stage 1/9: open and check the backups (read-only)")
        bs = open_source(source, runner, secrets, profile, work)
        plan = bs.db_plan()
        say("  database backup %s, stopped %s" % (plan.backup_label, plan.stop_time))
        stage = os.path.join(work, "stage")
        os.makedirs(stage)
        say("stage 2/9: restore database and files from the backups into staging (every backup read happens here, before the network work)")
        archives = restore_files(bs, bs.file_items(), stage, work, runner, say)
        db_meta = asm.restore_database(bs, plan, stage, profile, target_time, runner, work)
        backup_status = config_member_text(runner, archives[-1], "var/lib/dpkg/status") if archives else None
        backup_packages = asm.parse_dpkg_status(backup_status) if backup_status else None
        passwd_text, group_text = (config_member_text(runner, archives[-1], m) if archives else None for m in ("etc/passwd", "etc/group"))
        deployed_text = config_member_text(runner, archives[-1], "opt/hams/src/DEPLOYED_COMMITS") if archives else None
        say("stage 3/9: Debian %s base system into %s" % (profile["debian_suite"], out))
        chroot = asm.Chroot(runner, out)
        asm.debootstrap(profile, out, runner)
        base_users, base_groups = asm.snapshot_base_accounts(out)
        shutil.copy("/etc/resolv.conf", os.path.join(out, "etc/resolv.conf"))
        say("stage 4/9: accounts with the numbers the restored files carry")
        use_manifest = profile.get("use_manifest", True)
        claims = acc.manifest_owner_claims(infra.MANIFEST) if use_manifest else []
        if passwd_text and group_text:
            users, groups = acc.captured_ids(passwd_text, group_text)
        else:
            observed = acc.tree_owners(stage, [c[0] for c in claims])
            users, groups, conflicts = acc.derive_ids(claims, observed)
            if conflicts:
                raise RestorationError("account numbers in the backups conflict: " + "; ".join(conflicts))
        commands, unresolved = acc.account_commands(infra.MANIFEST if use_manifest else {}, users, groups)
        acc.create_accounts(chroot, commands)
        say("stage 5/9: Debian packages from the network (unpinned)")
        asm.setup_apt(profile, out, runner, fetch)
        packages = asm.package_list(infra, profile, backup_packages)
        asm.install_packages(chroot, packages, runner)
        code_report = code.install_code(profile, out, deployed_text, say)
        pinned = asm.install_pinned_downloads(infra, out, chroot, work, {"KOPIA_ARCH": "x64", "DEB_TARGET_ARCH_CPU": "amd64"}) if use_manifest else []
        say("stage 6/9: the restored data and configuration over the package defaults")
        asm.wipe_default_datadir(out, profile["database"]["datadir"])
        asm.overlay_stage(stage, out, runner)
        asm.chown_database(out, db_meta)
        code_report["built"] = code.build_artifacts(infra, out, chroot, say) if code_report["installed"] else []
        say("stage 7/9: provisioning layer, units and enablement")
        classes = un.host_class_list(out, profile.get("host_classes", []))
        env_values = {"DOMAIN": profile.get("domain", "hams.com"), "DB_NAME": profile["database"]["name"],
                      "HAMS_COM_DIR": "/opt/hams/src/hams_com", "HAMS_COMMUNITY_DIR": "/opt/hams/src/hams_open",
                      "DEB_CODENAME": profile["debian_suite"], "DEB_TARGET_ARCH_CPU": "amd64", "KOPIA_ARCH": "x64"}
        env_values.update(pl.read_env_values(out))
        uids, gids = pl.id_lookup(out)
        report = pl.new_report()
        report["units"] = {}
        if use_manifest:
            pl.provision_directories(infra, out, classes, uids, gids, report)
            pl.provision_files(infra, out, classes, uids, gids, env_values, report)
            infra.provision_systemd_override(None, env_values, environment="prod", dest_dir=out)
            expected = un.expected_units(infra, classes)
            un.link_units(out, expected)
            to_enable, kept = un.enablement_plan(out, expected)
            for unit in to_enable:
                runner.run(["systemctl", "--root=%s" % out, "enable", unit])
            report["units"] = {"expected": len(expected), "enabled_by_builder": to_enable, "enabled_as_restored": kept,
                               "external_fetch": sorted(u for u, i in expected.items() if i["external_fetch"])}
        say("stage 8/9: machine-specific paths out, post step, package database")
        say("stage 9/9: EXCLUDE.txt, RESTORE.md, RESTORATION_META.json")
        os.unlink(os.path.join(out, "etc/resolv.conf"))
        removed = asm.strip_machine_specific(out, profile["machine_specific"])
        wanted = asm.write_post_step_files(out, base_users, base_groups, runner)
        chroot.run(["apt-get", "clean"])
        meta = {"built_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "profile": profile["name"],
                "source": bs.describe(), "database": db_meta, "packages": packages, "pinned_downloads": pinned, "code": code_report,
                "accounts": {"unresolved_ids": unresolved, "image_accounts": len(wanted["users"])}, "provision": report,
                "removed_machine_specific": removed, "host_classes": classes}
        asm.write_exclude(out, profile)
        asm.write_restore_md(out, profile, meta)
        asm.write_meta(out, meta)
        say("image ready: %s\nNOTE: this directory contains decrypted data and every secret of the restored system." % out)
        return meta
    finally:
        if bs:
            bs.close()
        shutil.rmtree(work, ignore_errors=True)
