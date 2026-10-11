#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for odoo_site_wp_cleanup.py against an in-memory fake Odoo (no network, no real site)."""

# [@ANCHOR: test_odoo_site_wp_cleanup:run_cleanup]
# Tests [@ANCHOR: odoo_site_wp_cleanup:run_cleanup]

import json
import os
import shutil
import tempfile
import unittest

import odoo_site_wp_cleanup as cleanup
import wordpress_to_odoo as wp

ARCH = wp.Converter.page_arch(None, {"ID": 7, "post_name": "about"}, "About", "{body}")


def arch(body):
    return ARCH.replace("{body}", body)


class FakeOdoo:
    """search_read / read / write for the four models the tool uses."""

    def __init__(self):
        self.rows = {
            "website": [{"id": 2, "name": "Bruce Perens", "domain": "https://perens.com"},
                        {"id": 1, "name": "hams.com", "domain": "https://hams.com"}],
            "blog.post": [
                {"id": 301, "name": "Plain", "website_id": 2, "active": True, "content": "<p>Fine</p>"},
                {"id": 302, "name": "Leftovers", "website_id": 2, "active": True,
                 "content": '<!-- wp:paragraph --><p class="wp-block-paragraph">Hello</p><!-- /wp:paragraph --><p>&nbsp;</p>[gallery ids="1"]'},
                {"id": 303, "name": "Ocean", "website_id": 2, "active": True,
                 "content": '<p>Trip.</p><figure class="wp-block-image"><img class="wp-image-4" src="/wp-content/uploads/2018/OceanAvenueToSLC-4.jpg">'
                            '<figcaption>The ocean</figcaption></figure><p>Home again.</p>'},
                {"id": 304, "name": "Other site", "website_id": 1, "active": True, "content": '<p class="wp-block-x">no</p>'},
            ],
            "website.page": [{"id": 11, "url": "/about", "view_id": [71, "About"], "website_id": 2},
                             {"id": 12, "url": "/built", "view_id": [72, "Built"], "website_id": 2},
                             {"id": 13, "url": "/hams", "view_id": [73, "Hams"], "website_id": 1}],
            "ir.ui.view": [
                {"id": 71, "arch_db": arch('<p>See the <a href="/wp-content/uploads/sites/10/2018/07/OpenResearchInstitute_18_86.pdf">annual report</a>.</p>'
                                           '<p class="aligncenter" style="x">Body</p>')},
                {"id": 72, "arch_db": '<t t-name="website.built"><div class="wp-block-x">edited in the builder</div></t>'},
                {"id": 73, "arch_db": arch('<p class="wp-block-x">other website</p>')},
            ],
        }
        self.writes = []

    def call(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        if method == "read":
            return [dict(r) for r in self.rows[model] if r["id"] in args[0]]
        if method == "search_read":
            domain = args[0]
            rows = [r for r in self.rows[model] if all(self._term(r, t) for t in domain)]
            rows = rows[kwargs.get("offset", 0):][:kwargs.get("limit") or None]
            return [dict(r) for r in rows]
        if method == "write":
            self.writes.append((model, args[0][0], args[1]))
            next(r for r in self.rows[model] if r["id"] == args[0][0]).update(args[1])
            return True
        raise AssertionError(method)

    @staticmethod
    def _term(row, term):
        field, op, value = term
        if op == "=":
            return row.get(field) == value
        if op == "in":
            return row.get(field) in value
        raise AssertionError(op)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp_cleanup_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.backup = os.path.join(self.tmp, "backup.jsonl")
        self.odoo = FakeOdoo()
        self.log = []

    def run_it(self, apply, dead=None):
        return cleanup.run_cleanup(cleanup.Target(self.odoo, 2), apply, self.backup,
                                   cleanup.DEAD_REFERENCES if dead is None else dead, False, self.log.append)

    def test_a_dry_run_reports_and_writes_nothing(self):
        changed = self.run_it(False)
        self.assertEqual(changed, 3)  # two posts and one page; the builder page, the plain post and the other website stay
        self.assertEqual(self.odoo.writes, [])
        self.assertFalse(os.path.exists(self.backup))
        text = "\n".join(self.log)
        self.assertIn("DRY RUN", text)
        self.assertIn("blog.post 302", text)
        self.assertNotIn("blog.post 304", text)
        self.assertNotIn("ir.ui.view 73", text)

    def test_apply_saves_the_old_values_cleans_only_this_website_and_removes_the_two_dead_references(self):
        self.run_it(True)
        posts = {r["id"]: r["content"] for r in self.odoo.rows["blog.post"]}
        self.assertEqual(posts[301], "<p>Fine</p>")
        self.assertEqual(posts[302], "<p>Hello</p>")
        self.assertEqual(posts[303], "<p>Trip.</p><p>Home again.</p>")  # the figure and its caption went, the text stayed
        self.assertEqual(posts[304], '<p class="wp-block-x">no</p>')  # another website: untouched
        views = {r["id"]: r["arch_db"] for r in self.odoo.rows["ir.ui.view"]}
        self.assertIn("<p>See the annual report.</p>", views[71])
        self.assertIn('<p class="text-center">Body</p>', views[71])
        self.assertIn('<t t-call="website.layout">', views[71])
        self.assertEqual(views[72], '<t t-name="website.built"><div class="wp-block-x">edited in the builder</div></t>')
        self.assertIn("wp-block-x", views[73])
        with open(self.backup, encoding="utf-8") as handle:
            saved = [json.loads(line) for line in handle]
        self.assertEqual(sorted((r["model"], r["id"]) for r in saved), [("blog.post", 302), ("blog.post", 303), ("ir.ui.view", 71)])

    def test_a_second_run_finds_nothing_and_a_restore_puts_every_old_value_back(self):
        before = json.dumps(self.odoo.rows, sort_keys=True)
        self.run_it(True)
        writes = len(self.odoo.writes)
        self.assertEqual(self.run_it(True), 0)
        self.assertEqual(len(self.odoo.writes), writes)
        cleanup.run_restore(self.odoo, self.backup, True, self.log.append)
        self.assertEqual(json.dumps(self.odoo.rows, sort_keys=True), before)

    def test_a_restore_without_apply_writes_nothing(self):
        self.run_it(True)
        writes = len(self.odoo.writes)
        cleanup.run_restore(self.odoo, self.backup, False, self.log.append)
        self.assertEqual(len(self.odoo.writes), writes)

    def test_a_dead_reference_found_in_zero_or_several_records_is_not_removed(self):
        self.odoo.rows["blog.post"].append(
            {"id": 305, "name": "Ocean again", "website_id": 2, "active": True,
             "content": '<p><img src="/wp-content/uploads/OceanAvenueToSLC-4.jpg"></p>'})
        self.run_it(True)
        text = "\n".join(self.log)
        self.assertIn("NOT REMOVED OceanAvenueToSLC-4.jpg", text)
        self.assertIn("found in 2 record(s)", text)
        self.assertIn("OceanAvenueToSLC-4", next(r for r in self.odoo.rows["blog.post"] if r["id"] == 303)["content"])
        self.assertNotIn("OpenResearchInstitute", next(r for r in self.odoo.rows["ir.ui.view"] if r["id"] == 71)["arch_db"].split("annual")[0])
        self.log.clear()
        self.run_it(False, dead={"missing": r"NoSuchFile\.png"})
        self.assertIn("found in 0 record(s)", "\n".join(self.log))

    def test_the_website_must_exist(self):
        with self.assertRaises(cleanup.mig.MigrateError):
            cleanup.Target(self.odoo, 99).website()


if __name__ == "__main__":
    unittest.main()
