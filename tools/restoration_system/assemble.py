# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assembling the image directory: Debian packages from the network, restored backups, provisioning layer, copy instructions.

The directory is the whole system as it is to be restored: ordinary owners, modes and symlinks, nothing wrapped around it. The
files that describe it (EXCLUDE.txt, RESTORE.md, RESTORATION_META.json) sit at its top and exclude themselves from the copy.
"""
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request

from .runner import RestorationError
from . import accounts as acc

HERE = os.path.dirname(os.path.abspath(__file__))
SHARE_REL = "usr/local/share/hams-restore"
FILES_AT_TOP = ("EXCLUDE.txt", "RESTORE.md", "RESTORATION_META.json")


class Chroot:
    """Runs commands inside the image in a private mount namespace (assemble needs root)."""

    def __init__(self, runner, tree):
        self.runner, self.tree = runner, tree

    def path(self, rel):
        return os.path.join(self.tree, rel.lstrip("/"))

    def run(self, argv, **kw):
        wrapper = ["unshare", "--mount", "--propagation", "private", "python3", os.path.join(HERE, "chroot_exec.py"), self.tree]
        return self.runner.run(wrapper + list(argv), **kw)


def parse_dpkg_status(text):
    """Names of the packages a dpkg status file lists as installed."""
    names = set()
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(" "))
        if fields.get("Status", "").endswith("installed") and "Package" in fields:
            names.add(fields["Package"])
    return names


def package_list(infra, profile, backup_packages=None):
    """{package: [where it came from]}: MANIFEST apt_packages (production), the profile's extras, and the backups' dpkg status if they carry one."""
    out = {}
    if profile.get("use_manifest", True):
        for item in infra.MANIFEST.get("apt_packages", []):
            if "prod" in item.get("environments", []) or "early_prod" in item.get("environments", []):
                out.setdefault(item["debian_name"], []).append("MANIFEST")
    for name in profile.get("extra_packages", []):
        out.setdefault(name, []).append("profile")
    for name in sorted(backup_packages or []):
        out.setdefault(name, []).append("backup dpkg status")
    for name in profile.get("package_not_in_archive", []):
        out.pop(name, None)
    return out


def download(url, timeout=120, tries=4):
    """Bytes of a URL, retrying a flaky network. RestorationError after the last try."""
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except OSError as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RestorationError("cannot download %s: %s" % (url, last))


def setup_apt(profile, tree, runner, fetch=None):
    """Write the profile's extra apt repositories (key dearmored with gpg on the builder, source list) into the image. Needs the network."""
    fetch = fetch or download
    os.makedirs(os.path.join(tree, "usr/share/keyrings"), exist_ok=True)
    os.makedirs(os.path.join(tree, "etc/apt/sources.list.d"), exist_ok=True)
    for repo in profile.get("apt_repositories", []):
        key = fetch(repo["key_url"])
        out = os.path.join(tree, "usr/share/keyrings", repo["keyring"])
        proc = subprocess.run(["gpg", "--dearmor", "--yes", "-o", out], input=key, capture_output=True, check=False)
        if proc.returncode != 0:
            raise RestorationError("gpg could not dearmor the key of %s: %s" % (repo["name"], proc.stderr.decode()[-200:]))
        with open(os.path.join(tree, "etc/apt/sources.list.d", repo["name"] + ".list"), "w") as handle:
            handle.write(repo["line"].format(suite=profile["debian_suite"]) + "\n")


def debootstrap(profile, tree, runner):
    runner.run(["debootstrap", "--variant=minbase", "--include=systemd-sysv,dbus,ca-certificates,python3,gnupg,locales,procps,iproute2",
                profile["debian_suite"], tree, profile["debian_mirror"]])


def install_packages(chroot, packages, runner):
    """apt-get update and one unpinned install of every package; policy-rc.d keeps the packages' own postinst from starting services."""
    policy = chroot.path("usr/sbin/policy-rc.d")
    with open(policy, "w") as handle:
        handle.write("#!/bin/sh\nexit 101\n")
    os.chmod(policy, 0o755)
    try:
        chroot.run(["apt-get", "update"])
        chroot.run(["apt-get", "install", "-y"] + sorted(packages))
    finally:
        os.unlink(policy)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_pinned_downloads(infra, tree, chroot, work, arch_env, fetch=None):
    """MANIFEST entries that download a pinned artifact (kopia binary, wkhtmltopdf): fetched, sha256-checked, installed. Returns [names]."""
    fetch = fetch or download
    done = []
    for spec in infra.MANIFEST["static_files"]:
        if not spec.get("url") or spec.get("apt_only") or "prod" not in spec.get("environments", []):
            continue
        url = spec["url"].format(**arch_env)
        want = infra._pinned_sha256(spec, arch_env)
        local = os.path.join(work, os.path.basename(url))
        with open(local, "wb") as handle:
            handle.write(fetch(url))
        if want and sha256_of(local) != want:
            raise RestorationError("%s: sha256 does not match the MANIFEST pin" % url)
        if "kopia" in url:
            member = "kopia-0.23.1-linux-%s/kopia" % arch_env["KOPIA_ARCH"]
            target = os.path.join(tree, "usr/bin")
            os.makedirs(target, exist_ok=True)
            runner_run = chroot.runner.run
            runner_run(["tar", "-xzf", local, "-C", target, "--strip-components=1", member])
        else:
            inner = "/tmp/" + os.path.basename(local)
            shutil.copy(local, os.path.join(tree, inner.lstrip("/")))
            chroot.run(["apt-get", "install", "-y", inner])
            os.unlink(os.path.join(tree, inner.lstrip("/")))
        done.append(os.path.basename(url))
    return done


def wipe_default_datadir(tree, datadir):
    """The package's empty cluster directory is emptied so the restored cluster takes its place."""
    path = os.path.join(tree, datadir.lstrip("/"))
    if os.path.isdir(path):
        for name in os.listdir(path):
            full = os.path.join(path, name)
            shutil.rmtree(full) if os.path.isdir(full) and not os.path.islink(full) else os.unlink(full)


def chown_tree(path, uid, gid):
    os.lchown(path, uid, gid)
    for current, dirs, files in os.walk(path):
        for name in dirs + files:
            os.lchown(os.path.join(current, name), uid, gid)


def passwd_ids(tree, name):
    """(uid, gid) of account `name` in the image's /etc/passwd."""
    for line in acc.open_text(os.path.join(tree, "etc/passwd")).splitlines():
        parts = line.split(":")
        if parts[0] == name:
            return int(parts[2]), int(parts[3])
    raise RestorationError("account %s is not in the image" % name)


def restore_database(bs, plan, stage, profile, target_time, runner, work):
    """Restore the PostgreSQL cluster into the staging tree with point-in-time recovery settings, WAL included, so the first start needs no backup.

    pgBackRest repository: `pgbackrest restore` (run by the builder against the staged data directory), WAL pre-fetched by `archive-get`
    into <datadir>/../wal_restore and named by restore_command. Sealed streams: the base backup tar and WAL come from kopia and
    recovery.signal plus recovery settings are written. Ownership is set when the staged tree is laid over the image (the account numbers
    are known only then). Returns a dict for RESTORATION_META.json."""
    db = profile["database"]
    datadir_rel = db["datadir"]
    datadir = os.path.join(stage, datadir_rel.lstrip("/"))
    wal_rel = os.path.join(os.path.dirname(datadir_rel), "wal_restore")
    wal_dir = os.path.join(stage, wal_rel.lstrip("/"))
    os.makedirs(datadir, mode=0o700)
    meta = {"kind": plan.kind, "backup": plan.backup_label, "backup_stop": plan.stop_time, "target_time": target_time, "datadir": datadir_rel,
            "wal_dir": wal_rel}
    if plan.kind == "pgbackrest":
        bs.pg.pg_path = datadir
        bs.pg.write_config()
        bs.pg.restore(datadir, target_time, os.path.join("/", wal_rel), plan.backup_label)
        fetched = bs.pg.fetch_wal(bs.pg_backup, bs.pg_info, wal_dir)
        meta.update(wal_segments=len(fetched), wal_first=plan.wal_first, wal_last=plan.wal_last)
    else:
        meta.update(restore_streams(bs, plan, datadir, wal_dir, os.path.join("/", wal_rel), target_time, runner, work))
    return meta


def overlay_stage(stage, tree, runner):
    """Lay the staged restore over the image (backups win over package defaults), moving the files; owners stay numeric."""
    runner.run(["rsync", "-aHAX", "--numeric-ids", "--remove-source-files", stage + "/", tree + "/"])


def chown_database(tree, meta):
    uid, gid = passwd_ids(tree, "postgres")
    for rel in (meta["datadir"], meta["wal_dir"]):
        path = os.path.join(tree, rel.lstrip("/"))
        if os.path.isdir(path):
            chown_tree(path, uid, gid)


def restore_streams(bs, plan, datadir, wal_dir, wal_abs, target_time, runner, work):
    """Sealed base backup (base.tar.zst, pg_wal.tar) and WAL batches from kopia into datadir and wal_dir, recovery settings written."""
    scratch = os.path.join(work, "streams")
    shutil.rmtree(scratch, ignore_errors=True)
    bs.kopia.restore(plan.details["base"]["id"], os.path.join(scratch, "base"))
    base = os.path.join(scratch, "base")
    runner.run(["tar", "--zstd", "-xf", os.path.join(base, "base.tar.zst"), "-C", datadir])
    os.makedirs(os.path.join(datadir, "pg_wal"), mode=0o700, exist_ok=True)
    runner.run(["tar", "-xf", os.path.join(base, "pg_wal.tar"), "-C", os.path.join(datadir, "pg_wal")])
    os.makedirs(wal_dir, exist_ok=True)
    count = 0
    for i, snap in enumerate(plan.details["wal"]):
        part = os.path.join(scratch, "wal%d" % i)
        bs.kopia.restore(snap["id"], part)
        for name in sorted(os.listdir(part)):
            dest = os.path.join(wal_dir, name[:-4] if name.endswith(".zst") else name)
            if name.endswith(".zst"):
                with open(dest, "wb") as out:
                    subprocess.run(["zstd", "-dc", os.path.join(part, name)], stdout=out, check=True)
            else:
                shutil.copy2(os.path.join(part, name), dest)
            count += 1
        shutil.rmtree(part)
    shutil.rmtree(scratch)
    settings = ["archive_mode = off", "restore_command = 'cp %s/%%f %%p'" % wal_abs, "recovery_target_action = promote"]
    if target_time:
        settings.append("recovery_target_time = '%s'" % target_time)
    with open(os.path.join(datadir, "postgresql.auto.conf"), "a") as handle:
        handle.write("\n".join(settings) + "\n")
    open(os.path.join(datadir, "recovery.signal"), "w").close()
    return {"wal_files": count}


def strip_machine_specific(tree, patterns):
    """Delete from the image the paths that belong to the new machine (restored or created by the packages). Returns the removed paths."""
    removed = []
    for pattern in patterns:
        directory, _, base = pattern.rpartition("/")
        parent = os.path.join(tree, directory)
        if not os.path.isdir(parent):
            continue
        for name in sorted(os.listdir(parent)):
            if fnmatch.fnmatch(name, base):
                full = os.path.join(parent, name)
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    os.unlink(full)
                removed.append(os.path.join(directory, name))
    return removed


def exclude_patterns(profile):
    """rsync/tar exclude patterns, anchored at the top of the image: the machine-specific paths, the package database (merged by the post
    step), and the description files."""
    machine = ["/" + p for p in profile["machine_specific"]]
    extra = ["/etc/passwd", "/etc/passwd-", "/etc/group", "/etc/group-", "/etc/shadow", "/etc/shadow-", "/etc/gshadow", "/etc/gshadow-",
             "/etc/subuid", "/etc/subgid", "/var/lib/dpkg", "/var/lib/apt", "/var/cache/apt", "/etc/apt/sources.list", "/etc/apt/sources.list.d/debian.sources",
             "/etc/os-release", "/usr/lib/os-release", "/etc/debian_version", "/etc/issue", "/etc/issue.net", "/etc/motd"]
    top = ["/" + name for name in FILES_AT_TOP]
    return machine + extra + top


EXCLUDE_NOTES = """\
# EXCLUDE.txt: paths that belong to the NEW machine and must not be overwritten by the image.
# Use with:  rsync -aHAX --numeric-ids --exclude-from=EXCLUDE.txt IMAGE/ /
#       or:  tar --exclude-from=EXCLUDE.txt -C IMAGE -cf - . | tar -C / -xpf - --numeric-owner
# Paths are anchored at the top of the image. Account files are excluded because the new Debian has its own login accounts; the
# post step (hams-restore-post) adds the image's accounts with the numbers its files carry. The package database is merged by the
# post step, so the new server's kernel and boot loader stay known to dpkg.
"""


def write_exclude(tree, profile):
    with open(os.path.join(tree, "EXCLUDE.txt"), "w") as handle:
        handle.write(EXCLUDE_NOTES + "\n".join(exclude_patterns(profile)) + "\n")


def snapshot_base_accounts(tree):
    return {l.split(":")[0] for l in acc.open_text(os.path.join(tree, "etc/passwd")).splitlines()}, \
        {l.split(":")[0] for l in acc.open_text(os.path.join(tree, "etc/group")).splitlines()}


def write_post_step_files(tree, base_users, base_groups, runner):
    """Install hams-restore-post, the accounts file for it, and the image's dpkg database copy it merges."""
    share = os.path.join(tree, SHARE_REL)
    os.makedirs(share, exist_ok=True)
    sbin = os.path.join(tree, "usr/local/sbin")
    os.makedirs(sbin, exist_ok=True)
    shutil.copy(os.path.join(HERE, "post_step.py"), os.path.join(sbin, "hams-restore-post"))
    os.chmod(os.path.join(sbin, "hams-restore-post"), 0o755)
    rows = acc.accounts_table(tree)
    wanted = {"groups": [[r[1], int(r[2])] for r in rows if r[0] == "group" and r[1] not in base_groups],
              "users": [[r[1], int(r[2]), int(r[3]), r[4], r[5]] for r in rows if r[0] == "user" and r[1] not in base_users]}
    with open(os.path.join(share, "accounts.json"), "w") as handle:
        json.dump(wanted, handle, indent=1)
    shutil.copy(os.path.join(tree, "var/lib/dpkg/status"), os.path.join(share, "dpkg-status"))
    info_dst = os.path.join(share, "dpkg-info")
    shutil.rmtree(info_dst, ignore_errors=True)
    shutil.copytree(os.path.join(tree, "var/lib/dpkg/info"), info_dst, symlinks=True)
    return wanted


RESTORE_MD = """\
# Restoring this image onto a new Debian server

This directory is the whole system as it was restored from backups (built {built}). It contains decrypted data and EVERY SECRET of the
system; treat the disk and every copy of it accordingly.

1. Install Debian {suite} (amd64) on the new server: a minimal system with ssh, and the same disk layout you want to keep (fstab, boot
   loader, network settings and hostname stay the new server's own).
2. Copy the directory over it as root, keeping owners, modes, links, ACLs and xattrs (numeric ids, never names):

       rsync -aHAX --numeric-ids --exclude-from=EXCLUDE.txt IMAGE/ /

   or with tar:  `tar --exclude-from=EXCLUDE.txt -C IMAGE -cf - . | ssh root@NEW 'tar -C / -xpf - --numeric-owner'`.
   EXCLUDE.txt keeps the machine's own fstab, boot, network, hostname, machine-id, ssh host keys, account files and package database.
3. Run the post step once (safe to repeat): `/usr/local/sbin/hams-restore-post`. It adds the accounts the restored files belong to, merges
   the package database, runs ldconfig and daemon-reload. It stops with a list if an account number is already used by another name.
4. Reboot. Every unit is enabled as on production, including the units that call external services (mail, Cloudflare, regulators):
   the new server starts them as soon as it has a network. PostgreSQL finishes point-in-time recovery from the WAL held in the image
   ({wal_dir}) on its first start (target: {target}).

The new server's pgBackRest configuration is production's; after recovery finishes the cluster is on a new timeline: take a new full
backup before relying on point-in-time recovery again.
"""


def write_restore_md(tree, profile, meta):
    text = RESTORE_MD.format(built=meta["built_utc"], suite=profile["debian_suite"], wal_dir=os.path.join(os.path.dirname(meta["database"]["datadir"]), "wal_restore"),
                             target=meta["database"].get("target_time") or "end of the archived WAL")
    with open(os.path.join(tree, "RESTORE.md"), "w") as handle:
        handle.write(text)


def write_meta(tree, meta):
    with open(os.path.join(tree, "RESTORATION_META.json"), "w") as handle:
        json.dump(meta, handle, indent=1, sort_keys=True)
