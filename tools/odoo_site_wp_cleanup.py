#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""odoo_site_wp_cleanup: remove WordPress leftovers from the posts and pages of a website already in Odoo.

    odoo_site_wp_cleanup.py --target-url URL --target-db DB --target-password-file FILE --website-id N
                            [--target-login admin] [--target-transport json2] [--show-diff]
                            [--backup FILE] [--apply]
    odoo_site_wp_cleanup.py --target-url URL --target-db DB --target-password-file FILE --restore FILE [--apply]

The first form reads the blog posts (`blog.post.content`) and pages (the body inside the content column of the
page's `ir.ui.view.arch_db`) of ONE website and shows what would change. Nothing is written unless `--apply` is
given. With `--apply` the old value of every record about to change is first saved to the backup file (JSON
lines; the default name carries the time), then written. The second form puts the saved values back.

What is removed: injected `<script>` blocks, `<!-- wp:... -->` block comments, `[caption]` (kept as a figure
with its caption) `[gallery]` `[embed]` shortcodes, `wp-*` `size-*` `align*` `has-*` classes (alignment becomes
Bootstrap's), inline `style` and `on*` attributes, emoji images (replaced by the character), links into
wp-admin/wp-login/wp-json/attachment pages (the text stays) and the empty paragraphs these leave
(wp_leftovers.strip_wordpress_leftovers).

Dead references: Bruce authorised removing two links whose files the old site no longer had, and nothing else:
the picture `OceanAvenueToSLC-4.jpg` (WordPress post 11) and the link to `OpenResearchInstitute_18_86.pdf`
(WordPress post 55). WordPress ids do not survive the import (the importer remaps them), so each is found by
its file name. A figure holding the reference goes whole, a link around a picture goes with the picture, a text
link loses the link and keeps its words. Each reference must be found in exactly ONE record; zero or several
is reported and nothing is removed for it.

Idempotent: a second run finds nothing to change. The credentials file is read by this tool and never printed.
"""

import argparse
import collections
import difflib
import json
import re
import sys
import time

import odoo_site_migrate as mig  # noqa: E402
import wp_leftovers as wp  # noqa: E402

# name -> regular expression matched inside a src/href value.
DEAD_REFERENCES = {
    "OceanAvenueToSLC-4.jpg (WordPress post 11)": r"OceanAvenueToSLC-4(?:-\d+x\d+)?\.jpe?g",
    "OpenResearchInstitute_18_86.pdf (WordPress post 55)": r"OpenResearchInstitute_18_86\.pdf",
}
PAGE_SIZE = 100


class Target:
    """The records of one website, read and written through an odoo_site_migrate transport."""

    def __init__(self, transport, website_id):
        self.transport, self.website_id = transport, website_id

    def website(self):
        rows = self.transport.call("website", "read", [[self.website_id]], {"fields": ["name", "domain"]})
        if not rows:
            raise mig.MigrateError(f"website {self.website_id} does not exist")
        return rows[0]

    def _paged(self, model, domain, fields):
        offset = 0
        while True:
            rows = self.transport.call(model, "search_read", [domain, fields],
                                       {"offset": offset, "limit": PAGE_SIZE, "order": "id"})
            yield from rows
            if len(rows) < PAGE_SIZE:
                return
            offset += PAGE_SIZE

    def records(self):
        """[(model, record id, label, field, value)] of every post and page body of the website."""
        found = []
        for row in self._paged("blog.post", [("website_id", "=", self.website_id), ("active", "in", [True, False])],
                               ["id", "name", "content"]):
            found.append(("blog.post", row["id"], row["name"], "content", row.get("content") or ""))
        pages = list(self._paged("website.page", [("website_id", "=", self.website_id)], ["id", "url", "view_id"]))
        views = {}
        for start in range(0, len(pages), PAGE_SIZE):
            ids = [p["view_id"][0] for p in pages[start:start + PAGE_SIZE] if p["view_id"]]
            for view in self.transport.call("ir.ui.view", "read", [ids], {"fields": ["id", "arch_db"]}):
                views[view["id"]] = view.get("arch_db") or ""
        for page in pages:
            if page["view_id"]:
                found.append(("ir.ui.view", page["view_id"][0], page["url"], "arch_db", views.get(page["view_id"][0], "")))
        return found

    def write(self, model, record_id, field, value):
        self.transport.call(model, "write", [[record_id], {field: value}])


def sweep(model, value, stats):
    """The value with WordPress leftovers removed. A page's arch is swept only inside the body column the
    converter wrote; a page of any other shape (edited in the website builder) is returned unchanged."""
    if model == "ir.ui.view":
        parts = wp.split_page_arch(value)
        if not parts:
            stats["pages_other_shape_left_alone"] += 1
            return value
        head, body, tail = parts
        return head + wp.strip_wordpress_leftovers(body, stats) + tail
    return wp.strip_wordpress_leftovers(value, stats)


def remove_dead(model, value, pattern, stats):
    if model == "ir.ui.view":
        parts = wp.split_page_arch(value)
        if not parts:
            return value
        head, body, tail = parts
        return head + wp.remove_references(body, [pattern], stats) + tail
    return wp.remove_references(value, [pattern], stats)


def plan(records, dead_references):
    """({(model, id): (label, field, old, new)}, report lines, stats). Pure: no I/O."""
    stats = collections.Counter()
    current = {}
    for model, record_id, label, field, value in records:
        current[(model, record_id)] = [label, field, value, sweep(model, value, stats)]
    lines = []
    for name, pattern in dead_references.items():
        hits = [key for key, (_label, _field, _old, new) in current.items()
                if re.search(r"\b(?:src|href)\s*=\s*[\"'][^\"']*(?:%s)" % pattern, new, re.I)]
        if len(hits) != 1:
            lines.append(f"NOT REMOVED {name}: found in {len(hits)} record(s), exactly one is required"
                         + (": " + ", ".join(f"{m} {i}" for m, i in hits) if hits else ""))
            continue
        entry = current[hits[0]]
        before = collections.Counter()
        entry[3] = remove_dead(hits[0][0], entry[3], pattern, before)
        stats.update(before)
        lines.append(f"REMOVE {name}: {hits[0][0]} {hits[0][1]} ({entry[0]!r}) {dict(before)}")
    changes = {key: tuple(entry) for key, entry in current.items() if entry[3] != entry[2]}
    return changes, lines, stats


# [@ANCHOR: odoo_site_wp_cleanup:run_cleanup]
# Verified by [@ANCHOR: test_odoo_site_wp_cleanup:run_cleanup]
def run_cleanup(target, apply, backup_path, dead_references, show_diff, out):
    site = target.website()
    out(f"website {target.website_id}: {site.get('name')!r} ({site.get('domain') or 'no domain'})")
    records = target.records()
    out(f"{len(records)} post/page bodies read")
    changes, lines, stats = plan(records, dead_references)
    for line in lines:
        out("  " + line)
    for (model, record_id), (label, field, old, new) in sorted(changes.items()):
        out(f"  {model} {record_id} {label!r}: {len(old)} -> {len(new)} characters")
        if show_diff:
            for diff_line in difflib.unified_diff(re.split(r"(?<=>)", old), re.split(r"(?<=>)", new), "before", "after", n=0, lineterm=""):
                out("      " + diff_line.rstrip())
    out("  totals: " + (json.dumps(dict(stats), sort_keys=True) if stats else "nothing to remove"))
    if not apply:
        out(f"DRY RUN: {len(changes)} record(s) would change; nothing was written (use --apply)")
        return len(changes)
    if not changes:
        out("APPLIED: nothing to change")
        return 0
    with open(backup_path, "x", encoding="utf-8") as handle:  # audit-ignore-path
        for (model, record_id), (label, field, old, _new) in sorted(changes.items()):
            handle.write(json.dumps({"model": model, "id": record_id, "field": field, "label": label, "old": old}) + "\n")
    out(f"old values saved to {backup_path} (restore with --restore)")
    for (model, record_id), (_label, field, _old, new) in sorted(changes.items()):
        target.write(model, record_id, field, new)
    out(f"APPLIED: {len(changes)} record(s) changed")
    return len(changes)


def run_restore(target_transport, backup_path, apply, out):
    count = 0
    with open(backup_path, "r", encoding="utf-8") as handle:  # audit-ignore-path
        for line in handle:
            row = json.loads(line)
            out(f"  restore {row['model']} {row['id']} {row.get('label')!r}")
            if apply:
                target_transport.call(row["model"], "write", [[row["id"]], {row["field"]: row["old"]}])
            count += 1
    out(f"{'RESTORED' if apply else 'DRY RUN: would restore'} {count} record(s)")
    return count


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--target-login", default="admin")
    parser.add_argument("--target-password-file", required=True, help="the admin password (xmlrpc) or an API key (json2)")
    parser.add_argument("--target-transport", choices=("xmlrpc", "json2"), default="json2")
    parser.add_argument("--website-id", type=int, help="the website whose posts and pages are cleaned (required unless --restore)")
    parser.add_argument("--no-dead-references", action="store_true", help="do not remove the two authorised dead links")
    parser.add_argument("--show-diff", action="store_true")
    parser.add_argument("--backup", help="where --apply saves the old values (default: wp_cleanup_backup_<time>.jsonl)")
    parser.add_argument("--restore", metavar="BACKUP", help="put the values saved in BACKUP back")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    return parser


def main(argv=None, out=print):
    args = build_parser().parse_args(argv)
    if not args.restore and not args.website_id:
        raise SystemExit("--website-id is required (the website whose content is cleaned)")
    try:
        with open(args.target_password_file, "r", encoding="utf-8") as handle:  # audit-ignore-path
            secret = handle.read().strip()
        creds = mig.Credentials(args.target_url, args.target_db, args.target_login, secret)
        transport = mig.make_transport(args.target_transport, creds, min_interval=0.0)
        if args.restore:
            run_restore(transport, args.restore, args.apply, out)
            return 0
        backup = args.backup or time.strftime("wp_cleanup_backup_%Y%m%d_%H%M%S.jsonl")
        dead = {} if args.no_dead_references else DEAD_REFERENCES
        run_cleanup(Target(transport, args.website_id), args.apply, backup, dead, args.show_diff, out)
    except (mig.MigrateError, OSError) as exc:
        out(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
