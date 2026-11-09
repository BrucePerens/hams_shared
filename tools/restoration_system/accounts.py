# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accounts of the image: user and group numbers recovered from the backups' own files, then created from the MANIFEST.

The backups carry owners as numbers. The production /etc/passwd is in no backup scope, so the number of each MANIFEST account is
inferred from the owner of a path the MANIFEST says that account owns (a directory or a static file). Two accounts on one number, or
one account on two numbers, are conflicts and stop the build. Accounts with no evidence get ordinary new numbers and are reported.
"""
import os
import re
import subprocess


TAR_OWNER_RE = re.compile(r"^\S+\s+(\d+)/(\d+)\s+\d+\s+\S+\s+\S+\s+(.*)$")


def list_tar_owners(archive, runner):
    """{path (absolute, no trailing slash): (uid, gid)} from `tar -tvf` of a --numeric-owner archive of paths relative to /."""
    out = runner.run(["tar", "--zstd", "-tvf", archive, "--numeric-owner"], capture=True).stdout
    owners = {}
    for line in out.splitlines():
        m = TAR_OWNER_RE.match(line)
        if m:
            name = m.group(3).split(" -> ")[0].rstrip("/")
            owners["/" + name.lstrip("./")] = (int(m.group(1)), int(m.group(2)))
    return owners


def tree_owners(tree, paths):
    """{path: (uid, gid)} of the given absolute paths that exist inside `tree` (not following links)."""
    out = {}
    for path in paths:
        full = os.path.join(tree, path.lstrip("/"))
        if os.path.lexists(full):
            st = os.lstat(full)
            out[path] = (st.st_uid, st.st_gid)
    return out


def manifest_owner_claims(manifest):
    """[(path, user, group)] from MANIFEST directories and static_files whose owner is "user:group" (root excluded)."""
    claims = []
    for spec in list(manifest.get("directories", [])) + list(manifest.get("static_files", [])):
        owner = spec.get("owner")
        if owner and ":" in owner and "prod" in " ".join(spec.get("environments", ["prod"])):
            user, group = owner.split(":", 1)
            claims.append((spec["path"], user, group))
    return claims


def derive_ids(claims, observed):
    """(users {name: uid}, groups {name: gid}, conflicts [text]) from claims and the numeric owners seen in the backups.

    Root is fixed at 0. A name seen with two different numbers, or two names (other than root) with one number, are conflicts."""
    users, groups, conflicts = {"root": 0}, {"root": 0}, []
    for path, user, group in claims:
        if path not in observed:
            continue
        uid, gid = observed[path]
        for table, name, number, kind in ((users, user, uid, "user"), (groups, group, gid, "group")):
            if name in table and table[name] != number:
                conflicts.append("%s %s is %d on one path and %d on %s" % (kind, name, table[name], number, path))
            else:
                table[name] = number
    for table, kind in ((users, "uid"), (groups, "gid")):
        seen = {}
        for name, number in table.items():
            if number in seen and seen[number] != name:
                conflicts.append("%s %d is used by both %s and %s" % (kind, number, seen[number], name))
            seen[number] = name
    return users, groups, sorted(set(conflicts))


def account_commands(manifest, users, groups, environment="prod"):
    """The `groupadd`/`useradd`/`usermod` argument lists that create every MANIFEST account of `environment`, with the recovered numbers
    where known. Returns (commands, unresolved account names)."""
    commands, unresolved = [], []
    for acc in manifest.get("system_accounts", []):
        if environment not in acc.get("environments", ["prod", "test"]):
            continue
        user, group = acc["user"], acc["group"]
        gcmd = ["groupadd", "--system"] + (["-g", str(groups[group])] if group in groups else []) + [group]
        ucmd = ["useradd", "--system", "-g", group, "-d", acc.get("home", "/opt/hams"), "-s", acc.get("shell", "/bin/bash")]
        if user in users:
            ucmd += ["-u", str(users[user])]
        else:
            unresolved.append(user)
        commands.append(("group", group, gcmd))
        commands.append(("user", user, ucmd + [user]))
        for extra in acc.get("member_of", []):
            commands.append(("member", user, ["usermod", "-a", "-G", extra, user]))
        for extra_user in acc.get("add_to_users", []):
            commands.append(("addto", extra_user, ["usermod", "-a", "-G", group, extra_user]))
    return commands, unresolved


def create_accounts(chroot, commands):
    """Run the commands in the image; an account that already exists is skipped (the commands are repeatable)."""
    existing_users = {l.split(":")[0] for l in open_text(chroot.path("etc/passwd")).splitlines() if l}
    existing_groups = {l.split(":")[0] for l in open_text(chroot.path("etc/group")).splitlines() if l}
    for kind, name, argv in commands:
        if kind == "group" and name in existing_groups:
            continue
        if kind == "user" and name in existing_users:
            continue
        if kind == "addto" and name not in existing_users and name not in {c[1] for c in commands if c[0] == "user"}:
            continue
        chroot.runner.run([argv[0], "--root", chroot.tree] + argv[1:])
        if kind == "group":
            existing_groups.add(name)
        if kind == "user":
            existing_users.add(name)


def open_text(path):
    with open(path) as handle:
        return handle.read()


def accounts_table(tree):
    """Every account and group of the image as lines for the post step: ("user", name, uid, gid, home, shell) / ("group", name, gid)."""
    rows = []
    for line in open_text(os.path.join(tree, "etc/group")).splitlines():
        name, _, gid, members = (line.split(":") + [""])[:4]
        rows.append(["group", name, gid, members])
    for line in open_text(os.path.join(tree, "etc/passwd")).splitlines():
        name, _, uid, gid, _gecos, home, shell = line.split(":")
        rows.append(["user", name, uid, gid, home, shell])
    return rows


def run_ok(argv):
    return subprocess.run(argv, check=False, capture_output=True).returncode == 0
