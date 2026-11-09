# SPDX-License-Identifier: AGPL-3.0-or-later
"""The production unit set and its enablement, derived from the provisioning MANIFEST exactly as provisioning does.

The image keeps every unit as production has it: external-fetch timers and services included, enabled and started. The builder never
alters enablement. When the backups carry the production enablement links (etc/systemd/system/*.wants) they are kept as restored;
otherwise the links are made by the same rule provisioning uses (`_activation_units_to_enable`, `boot_service_unit_names`).
"""
import os

SYSTEMD_DIR = "/opt/hams/systemd"
UNIT_SUFFIXES = (".service", ".timer", ".path")


def host_class_list(tree, default):
    """Host classes recorded in the restored /opt/hams/etc/host_classes, else `default`."""
    path = os.path.join(tree, "opt/hams/etc/host_classes")
    if os.path.isfile(path):
        with open(path) as handle:
            return sorted({word for line in handle for word in line.replace(",", " ").split() if not line.startswith("#")})
    return sorted(default)


def expected_units(infra, classes):
    """{unit name: {"path", "enabled", "external_fetch", "opt_in"}} for the production units of a host with `classes`.

    `enabled` follows provisioning: a .timer or .path is enabled unless it is opt-in; a long-running service is enabled when
    boot_service_unit_names() names it; a oneshot or an opt-in unit is only linked."""
    infra_classes = list(classes)
    opt_in = infra.opt_in_unit_names()
    boot = infra.boot_service_unit_names()
    out = {}
    for spec in infra.MANIFEST.get("static_files", []):
        path = spec.get("path", "")
        name = os.path.basename(path)
        if os.path.dirname(path) != SYSTEMD_DIR or not name.endswith(UNIT_SUFFIXES):
            continue
        if "prod" not in spec.get("environments", []) or not infra._in_host_class(spec, infra_classes):
            continue
        enabled = (name.endswith((".timer", ".path")) and name not in opt_in) or name in boot
        out[name] = {"path": path, "enabled": enabled, "external_fetch": spec.get("external_fetch"), "opt_in": bool(spec.get("opt_in"))}
    return out


def enabled_in_tree(tree, unit):
    """True when some *.wants / *.requires directory under /etc/systemd/system of the image links `unit`."""
    base = os.path.join(tree, "etc/systemd/system")
    if not os.path.isdir(base):
        return False
    for entry in os.listdir(base):
        if entry.endswith((".wants", ".requires")) and os.path.lexists(os.path.join(base, entry, unit)):
            return True
    return False


def unit_file_present(tree, unit):
    return os.path.lexists(os.path.join(tree, "etc/systemd/system", unit)) or os.path.lexists(os.path.join(tree, SYSTEMD_DIR.lstrip("/"), unit))


def link_units(tree, units):
    """Symlink /etc/systemd/system/<unit> -> /opt/hams/systemd/<unit> where the image has no such link (as provisioning does)."""
    made = []
    for unit, info in units.items():
        dst = os.path.join(tree, "etc/systemd/system", unit)
        if not os.path.lexists(dst) and os.path.lexists(os.path.join(tree, info["path"].lstrip("/"))):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(info["path"], dst)
            made.append(unit)
    return made


def enablement_plan(tree, units):
    """(to_enable, kept): the units that should be enabled and are not yet in the image, and those already enabled as restored."""
    to_enable, kept = [], []
    for unit, info in sorted(units.items()):
        if not info["enabled"]:
            continue
        (kept if enabled_in_tree(tree, unit) else to_enable).append(unit)
    return to_enable, kept
