# SPDX-License-Identifier: AGPL-3.0-or-later
"""COVERAGE: what the image gets from the backups, what from provisioning and packages, and what from neither (a disaster-recovery GAP).

The report is computed from the profile (what each kind of source carries) and the provisioning MANIFEST (what a server of this kind has).
It reads no backup and changes no backup configuration. Each GAP is a fact to be filed as a to-do by a person or the coordinator.
"""

# GAPs that follow from the structure of the backups and the provisioning, with the evidence that makes them gaps.
STRUCTURAL = [
    ("accounts_not_in_backups", "/etc/passwd, /etc/group (user and group numbers) are in no backup scope",
     "Restored files carry numeric owners. The builder infers each MANIFEST account's number from the owner of a path the MANIFEST says it "
     "owns; accounts with no such path get new numbers and their files would be owned by the wrong account."),
    ("package_list_not_in_backups", "No package list (/var/lib/dpkg/status or equivalent) in any backup scope",
     "The image's packages come from the MANIFEST and the profile, installed unpinned. A package installed by hand on production, and any "
     "version difference, is not reproduced."),
    ("code_not_in_backups", "/opt/hams/src (hams_com, hams_open, hams_shared code) is in no backup scope",
     "Units and daemons run code from /opt/hams/src. The repository bundles under repos/ of the bucket are not read by the builder."),
    ("built_artifacts_not_in_backups", "Binaries built on the host (Rust daemons, libcloudflared.so) are in no backup scope",
     "MANIFEST hooks build them at provisioning (hook_build_rust_daemons, hook_build_cloudflared_ffi); neither the backups nor a package hold them."),
]
B2_ONLY = [
    ("b2_no_system_config", "The Backblaze set carries no /etc configuration (odoo.conf, postgresql, nginx, systemd units, sudoers, cron)",
     "Only /opt/hams/etc, the filestore, the static tree, the QSO recordings, tenant archives and hams_crypto.secret are in the kopia files "
     "repository. The rest comes from the MANIFEST where it has content, and is a gap where it does not (postgresql.conf, pgbackrest.conf, "
     "odoo.conf values set by hand)."),
]


def covered(path, prefixes):
    path = "/" + path.lstrip("/")
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def backup_prefixes(profile, kind):
    if kind == "b2":
        return list(profile["b2_kopia_paths"]) + list(profile["b2_kopia_files"].values())
    return ["/" + p for p in profile["config_tar_scope"]] + list(profile["local_files_sources"].values())


def build_coverage(infra, profile, kind, classes=(), unresolved_accounts=()):
    """{"from_backups": [...], "from_provisioning": [...], "gaps": [(id, title, detail)]} for a source of `kind` ("local" or "b2")."""
    prefixes = backup_prefixes(profile, kind)
    from_backups, from_prov, gaps, needs_code = [], [], [], []
    specs = [(s["path"], s, "file") for s in infra.MANIFEST["static_files"]] + [(d["path"], d, "directory") for d in infra.MANIFEST["directories"]]
    for path, spec, what in specs:
        if "prod" not in spec.get("environments", []) or not infra._in_host_class(spec, list(classes)) or infra.is_apt_only(spec):
            continue
        if covered(path, prefixes):
            from_backups.append(path)
        elif what == "directory" or "content" in spec:
            from_prov.append(path)
        elif spec.get("url"):
            from_prov.append(path + " (pinned download)")
        else:
            needs_code.append(path)
    if needs_code:
        sample = ", ".join(sorted(needs_code)[:5])
        gaps.append(("deployed_code_copies", "%d MANIFEST paths are copies of code (for example %s) that is itself in no backup" % (len(needs_code), sample),
                     "Provisioning copies them from the code checkout; the backups hold neither."))
    gaps = [g for g in STRUCTURAL] + (B2_ONLY if kind == "b2" else []) + gaps
    if unresolved_accounts:
        gaps.append(("accounts_without_id_evidence", "Accounts with no recoverable number: %s" % ", ".join(sorted(unresolved_accounts)[:12]),
                     "No path owned by them was found in the backups."))
    packages = [a["debian_name"] for a in infra.MANIFEST["apt_packages"] if "prod" in a.get("environments", []) or "early_prod" in a.get("environments", [])]
    return {"from_backups": sorted(from_backups), "from_provisioning": sorted(from_prov), "packages_from_manifest": sorted(packages), "gaps": gaps}


def render(report, kind):
    lines = ["COVERAGE of the %s backup set" % kind, "  restored from the backups: %d MANIFEST paths" % len(report["from_backups"]),
             "  produced by provisioning (MANIFEST) or pinned download: %d paths" % len(report["from_provisioning"]),
             "  installed as Debian packages (unpinned): %d" % len(report["packages_from_manifest"]), "  GAPS (in neither): %d" % len(report["gaps"])]
    for gid, title, detail in report["gaps"]:
        lines.append("    GAP %s: %s\n        %s" % (gid, title, detail))
    return "\n".join(lines)
