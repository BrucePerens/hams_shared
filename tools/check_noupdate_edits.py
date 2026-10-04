#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
noupdate-block Edit Checker (warning-only)
-------------------------------------------------------------------------------------------
Odoo's XML loader skips every `<record>`, `<template>` and `<menuitem>` that sits inside a
`<data noupdate="1">` (or `<odoo noupdate="1">`) block on each `-u` module upgrade after the
first install. The decision is made from the XML file's own structure at parse time, not from
the `ir_model_data.noupdate` column. So an edit to such a record reaches a fresh database and
silently never reaches an existing one: no error, no warning, the page or rule just stays as it
was. Found by hand three times (ham_init contact templates, `hams_minimal_footer`, and
`compliance.protects_hams_template`), plus seven more in a sweep; see
night_shift_history.md and the retired to-do `audit-other-noupdate-block-templates-for-same-
latent-risk-f291b8d3`.

This tool compares the XML data files changed between a base ref and HEAD. For every record,
template or menuitem that was inside a noupdate block at the base ref and exists at HEAD with
different content, it prints a warning. A moved record is judged by where it sits now: if it
left the noupdate block the edit does apply, so it is not reported; if it entered one it is
(the next upgrade will not rewrite it).

Not reported:
  - records only added (created on upgrade, so unaffected) or removed;
  - changes to comments and whitespace only;
  - records whose local id is mentioned in a migration script (`<module>/migrations/...`) that
    is itself added or changed in the same diff: that script is the way to rewrite an
    `ir.rule` or `res.groups`, which `check_burn_list.py` requires to stay in noupdate.

It is a warning, not a gate: exit status 0 unless --strict is given. There is no CI that runs
checks here (2026-10-02 decision), so run it before merging a change to an XML data file:

    python3 hams_shared/tools/check_noupdate_edits.py [repo_dir ...] [--base origin/main] [--head HEAD]

With no repo_dir it checks the current directory's repository. The base ref for a deploy
review is the commit in `/opt/hams/src/DEPLOYED_COMMITS` instead of origin/main.
"""

import argparse
import os
import re
import subprocess
import sys

from lxml import etree

RECORD_TAGS = ("record", "template", "menuitem")
TRUE_VALUES = {"1", "true", "True"}
SKIP_PARTS = {"node_modules", "__pycache__", ".git", "demo", "tests", "static", "i18n"}


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=False
    )


def _show(repo, ref, path):
    result = _git(repo, "show", f"{ref}:{path}")
    return result.stdout if result.returncode == 0 else None


def _noupdate(element):
    """True if the nearest ancestor-or-self carrying a noupdate attribute says so."""
    for node in element.iterancestors():
        value = node.get("noupdate")
        if value is not None:
            return value in TRUE_VALUES
    return False


def _canonical(element):
    """The element as text with comments, attribute order and whitespace between tags removed."""
    clone = etree.fromstring(etree.tostring(element))
    for node in clone.iter():
        if isinstance(node.tag, str):
            node.text = re.sub(r"\s+", " ", node.text).strip() if node.text else None
            node.tail = None
    return etree.tostring(clone, method="c14n").decode()


def noupdate_records(xml_text):
    """{local id: (inside a noupdate block, canonical text)} for one XML data file."""
    parser = etree.XMLParser(remove_comments=True, resolve_entities=False)
    try:
        root = etree.fromstring(xml_text.encode(), parser)
    except etree.XMLSyntaxError:
        return {}
    found = {}
    for tag in RECORD_TAGS:
        for element in root.iter(tag):
            record_id = element.get("id")
            if record_id:
                found[record_id.rpartition(".")[2]] = (_noupdate(element), _canonical(element))
    return found


def _module_of(path):
    return path.split("/", 1)[0] if "/" in path else ""


def _changed_files(repo, base, head):
    """[(status, old path, new path)] for XML data files modified or renamed base...head."""
    result = _git(repo, "diff", "--name-status", "-M", f"{base}...{head}")
    changed = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        if status not in ("M", "R"):
            continue
        old, new = parts[1], parts[-1]
        if not new.endswith(".xml") or SKIP_PARTS & set(new.split("/")):
            continue
        changed.append((status, old, new))
    return changed


def _migration_text(repo, base, head):
    """{module: text of every migration script added or changed base...head}."""
    result = _git(repo, "diff", "--name-status", "-M", f"{base}...{head}")
    texts = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        path = parts[-1]
        if parts[0][0] in ("A", "M", "R") and "/migrations/" in path and path.endswith(".py"):
            body = _show(repo, head, path) or ""
            texts[_module_of(path)] = texts.get(_module_of(path), "") + body
    return texts


def check_repo(repo, base="origin/main", head="HEAD"):
    """[(path, record id, message)] for records whose noupdate edit will not reach existing databases."""
    warnings = []
    migrations = _migration_text(repo, base, head)
    for _status, old, new in _changed_files(repo, base, head):
        before = _show(repo, base, old)
        after = _show(repo, head, new)
        if before is None or after is None:
            continue
        old_records = noupdate_records(before)
        new_records = noupdate_records(after)
        for record_id, (now_noupdate, now_text) in sorted(new_records.items()):
            if record_id not in old_records or not now_noupdate:
                continue
            _was_noupdate, old_text = old_records[record_id]
            if old_text == now_text:
                continue
            module = _module_of(new)
            if re.search(rf"\b{re.escape(record_id)}\b", migrations.get(module, "")):
                continue
            warnings.append(
                (
                    new,
                    record_id,
                    f"{new}: '{record_id}' sits inside a noupdate block and its content changed "
                    f"since {base}. `-u {module}` will not apply the edit to an existing "
                    "database. Move the record out of the noupdate block (with a migration that "
                    "clears the stale flag), or add a migration script that rewrites it.",
                )
            )
    return warnings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("repos", nargs="*", default=["."])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--strict", action="store_true", help="exit 1 when there is a warning")
    args = parser.parse_args(argv)
    total = 0
    for repo in args.repos:
        root = _git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo
        for _path, _record_id, message in check_repo(root, args.base, args.head):
            print(f"WARNING: {message}")
            total += 1
    if total:
        print(f"{total} edit(s) inside noupdate blocks will not reach existing databases.")
    return 1 if (total and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
