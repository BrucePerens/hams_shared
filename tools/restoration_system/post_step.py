#!/usr/bin/python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""hams-restore-post: the one step to run on the new server after the image was copied over it. Idempotent; standard library only.

Copied into the image as /usr/local/sbin/hams-restore-post by the restoration system. It
  1. creates the groups and accounts of the image that the new Debian lacks, with the numbers the restored files carry
     (/usr/local/share/hams-restore/accounts.json); a number already used by another name is a conflict: it stops and says which;
  2. merges the image's package database into the new server's (the image's /var/lib/dpkg is not copied, see EXCLUDE.txt), so dpkg
     knows both the image's packages and the new server's own kernel and boot loader;
  3. runs ldconfig and `systemctl daemon-reload`. Enablement links were made when the image was built.
Exit status 0 when done, 1 on a conflict, 2 when not run as root.
"""
import json
import os
import subprocess
import sys

SHARE = "/usr/local/share/hams-restore"


def read_ids(path, key_index):
    """{name: id string} from /etc/passwd or /etc/group style files."""
    table = {}
    if os.path.exists(path):
        with open(path) as handle:
            for line in handle:
                parts = line.rstrip("\n").split(":")
                if len(parts) > key_index:
                    table[parts[0]] = parts[2]
    return table


def plan_accounts(wanted, etc="/etc"):
    """(group_commands, user_commands, conflicts) that make /etc/group and /etc/passwd under `etc` hold the wanted accounts."""
    groups = read_ids(os.path.join(etc, "group"), 2)
    users = read_ids(os.path.join(etc, "passwd"), 2)
    gid_owner = {v: k for k, v in groups.items()}
    uid_owner = {v: k for k, v in users.items()}
    gcmds, ucmds, conflicts = [], [], []
    for name, gid in wanted["groups"]:
        gid = str(gid)
        if name in groups:
            if groups[name] != gid:
                conflicts.append("group %s has gid %s here, the image's files use %s" % (name, groups[name], gid))
        elif gid in gid_owner:
            conflicts.append("gid %s is used here by %s; the image needs it for %s" % (gid, gid_owner[gid], name))
        else:
            gcmds.append(["groupadd", "-g", gid, name])
            gid_owner[gid] = name
    for name, uid, gid, home, shell in wanted["users"]:
        uid = str(uid)
        if name in users:
            if users[name] != uid:
                conflicts.append("user %s has uid %s here, the image's files use %s" % (name, users[name], uid))
        elif uid in uid_owner:
            conflicts.append("uid %s is used here by %s; the image needs it for %s" % (uid, uid_owner[uid], name))
        else:
            ucmds.append(["useradd", "-M", "-u", uid, "-g", str(gid), "-d", home, "-s", shell, name])
            uid_owner[uid] = name
    return gcmds, ucmds, conflicts


def stanzas(text):
    """{package: stanza text} of a dpkg status file."""
    out = {}
    for block in text.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("Package: "):
                out[line[9:].strip()] = block.strip() + "\n"
                break
    return out


def merge_dpkg(image_status_text, target_status_text):
    """Target status text that holds every stanza of the image (replacing the target's stanza of the same package) and keeps the rest."""
    merged = stanzas(target_status_text)
    merged.update(stanzas(image_status_text))
    return "\n".join(merged[name] for name in sorted(merged)) + "\n"


def merge_dpkg_database(share=SHARE, dpkg_dir="/var/lib/dpkg"):
    """Merge the image's dpkg status and info files (kept under `share`) into `dpkg_dir`. Returns the number of packages in the result."""
    image_status = os.path.join(share, "dpkg-status")
    if not os.path.exists(image_status):
        return 0
    with open(image_status) as handle:
        image_text = handle.read()
    target = os.path.join(dpkg_dir, "status")
    target_text = open(target).read() if os.path.exists(target) else ""
    merged = merge_dpkg(image_text, target_text)
    tmp = target + ".hams-restore.tmp"
    with open(tmp, "w") as handle:
        handle.write(merged)
    os.replace(tmp, target)
    info_src, info_dst = os.path.join(share, "dpkg-info"), os.path.join(dpkg_dir, "info")
    if os.path.isdir(info_src):
        os.makedirs(info_dst, exist_ok=True)
        subprocess.run(["cp", "-a", "--no-preserve=links", info_src + "/.", info_dst + "/"], check=True)
    return len(stanzas(merged))


def main():
    if os.geteuid() != 0:
        sys.stderr.write("hams-restore-post must run as root\n")
        return 2
    with open(os.path.join(SHARE, "accounts.json")) as handle:
        wanted = json.load(handle)
    gcmds, ucmds, conflicts = plan_accounts(wanted)
    if conflicts:
        sys.stderr.write("account conflicts, nothing changed:\n  " + "\n  ".join(conflicts) + "\n")
        return 1
    for cmd in gcmds + ucmds:
        subprocess.run(cmd, check=True)
    print("accounts: %d groups and %d users created" % (len(gcmds), len(ucmds)))
    print("dpkg: %d packages known" % merge_dpkg_database())
    subprocess.run(["ldconfig"], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    print("done: reboot, or start the services")
    return 0


if __name__ == "__main__":
    sys.exit(main())
