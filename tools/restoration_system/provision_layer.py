# SPDX-License-Identifier: AGPL-3.0-or-later
"""The provisioning layer: what the MANIFEST says a server of this kind has, for everything the backups did not restore.

A path the backups restored is kept as restored (the backup is the truth about production). A MANIFEST directory or file that is
missing is created from the MANIFEST: directories with their owner and mode, files from `render_static_content`. Entries that cannot be
produced here (a value only a running host has, a copy of code, a binary built on the host) are listed, not guessed.
"""
import os

from . import accounts as acc


def read_env_values(tree):
    """{NAME: value} from the restored /opt/hams/etc/*.env files, for rendering MANIFEST content. Values stay in memory."""
    values = {}
    base = os.path.join(tree, "opt/hams/etc")
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            if name.endswith(".env") and os.path.isfile(full):
                for line in acc.open_text(full).splitlines():
                    key, sep, value = line.partition("=")
                    if sep and key.strip().isidentifier():
                        values[key.strip()] = value.strip()
    return values


def id_lookup(tree):
    """(user name -> uid, group name -> gid) from the image's own files."""
    users = {row.split(":")[0]: int(row.split(":")[2]) for row in acc.open_text(os.path.join(tree, "etc/passwd")).splitlines() if row}
    groups = {row.split(":")[0]: int(row.split(":")[2]) for row in acc.open_text(os.path.join(tree, "etc/group")).splitlines() if row}
    return users, groups


def apply_owner(path, owner, mode, users, groups):
    uid = gid = -1
    if owner and ":" in owner:
        user, group = owner.split(":", 1)
        uid, gid = users.get(user, -1), groups.get(group, -1)
    os.lchown(path, uid, gid)
    if mode is not None and not os.path.islink(path):
        os.chmod(path, mode)


def provision_directories(infra, tree, classes, users, groups, report):
    for spec in infra.MANIFEST["directories"]:
        if "prod" not in spec["environments"] or not infra._in_host_class(spec, classes):
            continue
        full = os.path.join(tree, spec["path"].lstrip("/"))
        if os.path.lexists(full):
            report["directories_from_backup_or_packages"].append(spec["path"])
            continue
        os.makedirs(full, mode=int(spec["provision_mode"], 8), exist_ok=True)
        apply_owner(full, spec.get("owner"), int(spec["provision_mode"], 8), users, groups)
        report["directories_created"].append(spec["path"])


def provision_files(infra, tree, classes, users, groups, env_values, report):
    for spec in infra.MANIFEST["static_files"]:
        path = spec["path"]
        if "prod" not in spec.get("environments", []) or not infra._in_host_class(spec, classes) or infra.is_apt_only(spec):
            continue
        full = os.path.join(tree, path.lstrip("/"))
        if os.path.lexists(full):
            report["files_from_backup"].append(path)
            continue
        if spec.get("url"):
            report["files_downloaded_or_built_at_provisioning"].append(path)
            continue
        if "content" not in spec:
            report["files_needing_code"].append(path)
            continue
        try:
            text = infra.render_static_content(spec, dict(env_values))
        except (KeyError, ValueError) as exc:
            report["files_unrenderable"].append("%s (%s)" % (path, exc))
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)
        apply_owner(full, spec.get("owner"), int(spec["mode"], 8), users, groups)
        report["files_written_from_manifest"].append(path)


def new_report():
    keys = ("directories_from_backup_or_packages", "directories_created", "files_from_backup", "files_downloaded_or_built_at_provisioning",
            "files_needing_code", "files_unrenderable", "files_written_from_manifest")
    return {key: [] for key in keys}
