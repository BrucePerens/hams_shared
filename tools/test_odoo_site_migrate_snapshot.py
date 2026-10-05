#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the offline-snapshot export, the stock-view classification, the form replacement, the
tenant lockdown and site_compare (all against fakes: SQLite for the snapshot, FakeOdoo for the servers)."""

# [@ANCHOR: test_odoo_site_migrate:classify_view]
# Tests [@ANCHOR: odoo_site_migrate:classify_view]
# [@ANCHOR: test_odoo_site_migrate:neutralize_forms]
# Tests [@ANCHOR: odoo_site_migrate:neutralize_forms]
# [@ANCHOR: test_odoo_snapshot_source:read_only]
# Tests [@ANCHOR: odoo_snapshot_source:read_only]
# [@ANCHOR: test_odoo_site_migrate:lockdown]
# Tests [@ANCHOR: odoo_site_migrate:lockdown]
# [@ANCHOR: test_site_compare:compare_pages]
# Tests [@ANCHOR: site_compare:compare_pages]

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import xmlrpc.client

import odoo_site_migrate as mig
import odoo_snapshot_source as snap
import site_compare as sc
from test_odoo_site_migrate import F, FakeOdoo


# --------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------


class ClassifyViewTests(unittest.TestCase):
    GENERIC = {
        "website.header_text_element": {"key": "website.header_text_element", "arch_db": "<t><p>Call us</p></t>", "active": True},
        "website.footer_custom": {"key": "website.footer_custom", "arch_db": "<t><p>x</p></t>", "active": True},
    }

    def view(self, key, arch="<t><p>Call us</p></t>", active=True, view_id=1):
        return {"id": view_id, "key": key, "arch_db": arch, "active": active}

    def test_each_class(self):
        pages = {99}

        def classify(view):
            return mig.classify_view(view, self.GENERIC, pages)

        self.assertEqual(classify(self.view("website.about", view_id=99)), "page")
        self.assertEqual(classify(self.view("theme_odoo_experts.s_banner")), "theme")
        # identical to the generic view, comments and attribute order ignored
        same = self.view("website.header_text_element", "<t ><!-- note --> <p>Call   us</p></t>")
        self.assertEqual(classify(same), "stock_copy")
        self.assertEqual(classify(self.view("website.header_text_element", active=False)), "toggle")
        self.assertEqual(classify(self.view("website.header_text_element", "<t><p>Write to us</p></t>")), "customized")
        self.assertEqual(classify(self.view("website.my_own_view")), "custom")
        self.assertEqual(classify({"id": 5, "key": False, "arch_db": "<t/>", "active": True}), "custom")

    def test_normalize_arch_is_stable_for_equivalent_markup_and_survives_bad_xml(self):
        self.assertEqual(mig.normalize_arch('<t a="1" b="2"><p>x  y</p></t>'), mig.normalize_arch('<t b="2" a="1"> <p> x y </p> </t>'))
        self.assertNotEqual(mig.normalize_arch("<t><p>x</p></t>"), mig.normalize_arch("<t><p>y</p></t>"))
        self.assertEqual(mig.normalize_arch("<t><p>broken"), "<t><p>broken")
        self.assertEqual(mig.normalize_arch(False), "")


class NeutralizeFormsTests(unittest.TestCase):
    FORM = (
        '<t t-name="website.contactus"><div id="wrap"><p>Hello &amp;nbsp;there</p>'
        '<section class="s_website_form" data-snippet="s_website_form"><div class="container">'
        '<form action="/website/form/" method="post" data-model_name="mail.mail"><input name="a"/></form></div></section>'
        '<p>after</p></div></t>'
    )

    def test_a_form_section_becomes_a_static_notice(self):
        new, count = mig.neutralize_forms(self.FORM, "No form here.")
        self.assertEqual(count, 1)
        self.assertNotIn("<form", new)
        self.assertNotIn("s_website_form", new)
        self.assertIn("No form here.", new)
        self.assertIn("Hello &amp;nbsp;there", new)  # the rest of the page is untouched
        self.assertIn("<p>after</p>", new)

    def test_a_bare_form_posting_to_website_form_is_replaced_but_a_search_form_is_not(self):
        arch = ('<t><div><form action="/website/form/" method="post"><input/></form>'
                '<form action="/website/search" method="get"><input/></form></div></t>')
        new, count = mig.neutralize_forms(arch)
        self.assertEqual(count, 1)
        self.assertIn('action="/website/search"', new)
        self.assertNotIn("/website/form", new)
        self.assertIn(mig.DEFAULT_FORMS_NOTICE, new)

    def test_arch_without_a_form_comes_back_byte_for_byte(self):
        arch = '<t><p>no   form</p><!-- keep --></t>'
        self.assertEqual(mig.neutralize_forms(arch), (arch, 0))
        self.assertEqual(mig.neutralize_forms(False), (False, 0))
        self.assertEqual(mig.neutralize_forms("<t><broken s_website_form"), ("<t><broken s_website_form", 0))

    def test_two_forms_are_both_replaced(self):
        arch = "<t>" + "".join('<section class="s_website_form"><form action="/website/form/"/></section>' for _ in range(2)) + "</t>"
        self.assertEqual(mig.neutralize_forms(arch)[1], 2)


class RewriteTests(unittest.TestCase):
    def test_the_old_domain_is_rewritten_in_attributes_but_not_in_visible_text(self):
        html = '<a href="https://postopen.org/documents">x</a><p>See https://postopen.org/zero-contract/ and [https://postopen.org/x].</p>'
        report = {}
        out = mig.rewrite_html(html, ["postopen.org"], report=report)
        self.assertIn('href="/documents"', out)
        self.assertIn("See https://postopen.org/zero-contract/ and [https://postopen.org/x].", out)
        self.assertEqual(report["links_made_relative"], 1)

    def test_a_bare_blog_id_follows_the_blog_map(self):
        report = {}
        self.assertEqual(mig.rewrite_html("/blog/3", [], blog_map={3: 9}, report=report), "/blog/9")
        self.assertEqual(mig.rewrite_html('<a href="/blog/3?x=1">', [], blog_map={3: 9}), '<a href="/blog/9?x=1">')
        self.assertEqual(mig.rewrite_html("/blog/5", [], blog_map={3: 9}, report=report), "/blog/5")
        self.assertEqual(report["blog_refs_unresolved"], 1)

    def test_slugify_matches_odoo(self):
        self.assertEqual(mig.slugify("Bruce's Blog"), "bruce-s-blog")
        self.assertEqual(mig.slugify("Café News!"), "cafe-news")

    def test_module_for_url(self):
        self.assertEqual(mig.module_for_url("/forum"), "website_forum")
        self.assertEqual(mig.module_for_url("/slides/all"), "website_slides")
        self.assertIsNone(mig.module_for_url("/press"))
        self.assertIsNone(mig.module_for_url("/forumlike-page-name"))


# --------------------------------------------------------------------------------------------
# The snapshot source over SQLite
# --------------------------------------------------------------------------------------------


class SqliteDb:
    placeholder = "?"

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.writes = []

    def run(self, sql, params=()):
        self.conn.execute(sql, params)

    def query(self, sql, params=()):
        if not sql.lstrip().upper().startswith("SELECT"):
            raise AssertionError(f"the snapshot source issued a non-SELECT statement: {sql}")
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def columns(self, table):
        return {row[1] for row in self.conn.execute(f'PRAGMA table_info("{table}")')}


def meta(db, model, name, ttype, relation=None, store=True, translate=False, rel_table=None, col1=None, col2=None):
    db.run("INSERT INTO ir_model_fields (model, name, ttype, relation, store, translate, relation_table, column1, column2, field_description) "
           "VALUES (?,?,?,?,?,?,?,?,?,?)",
           (model, name, ttype, relation, store, translate, rel_table, col1, col2, json.dumps({"en_US": name.title()})))


def build_snapshot(filestore):
    db = SqliteDb()
    db.run("CREATE TABLE ir_model_fields (id INTEGER PRIMARY KEY, model, name, ttype, relation, store, translate, relation_table, column1, column2, field_description)")
    db.run("CREATE TABLE ir_module_module (id INTEGER PRIMARY KEY, name, state, latest_version)")
    db.run("INSERT INTO ir_module_module (name, state, latest_version) VALUES "
           "('base','installed','18.0.1.3'),('website','installed','18.0.1.0'),('theme_x','uninstalled','18.0.1.0')")
    db.run("CREATE TABLE website (id INTEGER PRIMARY KEY, name, domain, active, theme_id, logo_x)")
    db.run("INSERT INTO website (id, name, domain, active, theme_id) VALUES (1,'Post Open','https://postopen.org',1,551)")
    for name, ttype, rel in (("name", "char", None), ("domain", "char", None), ("active", "boolean", None), ("theme_id", "many2one", "ir.module.module"),
                             ("logo", "binary", None), ("favicon", "binary", None), ("id", "integer", None)):
        meta(db, "website", name, ttype, rel)
    db.run("CREATE TABLE website_page (id INTEGER PRIMARY KEY, url, website_id, view_id, is_published)")
    db.run("INSERT INTO website_page VALUES (1,'/',NULL,10,1),(2,'/about',1,11,1),(3,'/press',1,12,0)")
    for name, ttype, rel in (("url", "char", None), ("website_id", "many2one", "website"), ("view_id", "many2one", "ir.ui.view"), ("is_published", "boolean", None)):
        meta(db, "website.page", name, ttype, rel)
    db.run("CREATE TABLE ir_ui_view (id INTEGER PRIMARY KEY, name, key, website_id, arch_db, cover)")
    db.run("INSERT INTO ir_ui_view VALUES (11,'About','website.about',1,?,'{\"a\": 1}')",
           (json.dumps({"en_US": "<t>about</t>", "fr_FR": "<t>a propos</t>"}),))
    meta(db, "ir.ui.view", "arch_db", "text", translate=True)
    meta(db, "ir.ui.view", "key", "char")
    meta(db, "ir.ui.view", "cover", "text")
    meta(db, "ir.ui.view", "website_id", "many2one", "website")
    db.run("CREATE TABLE blog_blog (id INTEGER PRIMARY KEY, name)")
    db.run("INSERT INTO blog_blog VALUES (4, ?)", (json.dumps({"en_US": "Bruce's Blog"}),))
    meta(db, "blog.blog", "name", "char", translate=True)
    meta(db, "blog.blog", "website_url", "char", store=False)
    db.run("CREATE TABLE blog_post (id INTEGER PRIMARY KEY, name, blog_id)")
    db.run("INSERT INTO blog_post VALUES (7, ?, 4)", (json.dumps({"en_US": "Hello World"}),))
    meta(db, "blog.post", "name", "char", translate=True)
    meta(db, "blog.post", "blog_id", "many2one", "blog.blog")
    meta(db, "blog.post", "tag_ids", "many2many", "blog.tag", rel_table="blog_post_blog_tag_rel", col1="blog_post_id", col2="blog_tag_id")
    meta(db, "blog.post", "website_url", "char", store=False)
    db.run("CREATE TABLE blog_post_blog_tag_rel (blog_post_id, blog_tag_id)")
    db.run("INSERT INTO blog_post_blog_tag_rel VALUES (7, 3), (7, 2)")
    db.run("CREATE TABLE ir_attachment (id INTEGER PRIMARY KEY, name, type, url, public, website_id, res_model, res_id, res_field, store_fname, db_datas, file_size)")
    os.makedirs(os.path.join(filestore, "ab"))
    with open(os.path.join(filestore, "ab", "abcdef"), "wb") as handle:
        handle.write(b"FILEBYTES")
    db.run("INSERT INTO ir_attachment VALUES (1,'hero.jpg','binary',NULL,1,1,'blog.post',7,NULL,'ab/abcdef',NULL,9)")
    db.run("INSERT INTO ir_attachment VALUES (2,'favicon','binary',NULL,0,NULL,'website',1,'favicon',NULL,?,3)", (b"ICO",))
    db.run("INSERT INTO ir_attachment VALUES (3,'stock','url','/web/static/x.png',1,1,'ir.ui.view',NULL,NULL,NULL,NULL,0)")
    for name, ttype in (("name", "char"), ("type", "char"), ("url", "char"), ("public", "boolean"), ("res_model", "char"), ("store_fname", "char"),
                        ("file_size", "integer")):
        meta(db, "ir.attachment", name, ttype)
    meta(db, "ir.attachment", "website_id", "many2one", "website")
    meta(db, "ir.attachment", "datas", "binary", store=False)
    db.run("CREATE TABLE ir_model_data (id INTEGER PRIMARY KEY, module, name, model, res_id)")
    db.run("CREATE TABLE ir_model (id INTEGER PRIMARY KEY, model, transient)")
    meta(db, "ir.module.module", "name", "char")
    meta(db, "ir.module.module", "state", "char")
    meta(db, "ir.model.data", "res_id", "integer")
    for model in ("website", "website.page", "website.abstract.mixin", "blog.blog", "blog.post", "ir.ui.view", "ir.attachment",
                  "ir.module.module", "ir.model.data"):
        db.run("INSERT INTO ir_model (model, transient) VALUES (?, 0)", (model,))
    return db


class SnapshotSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="snapshot_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = build_snapshot(os.path.join(self.tmp, "filestore"))
        self.src = snap.SnapshotSource(self.db, os.path.join(self.tmp, "filestore"))

    def read(self, model, domain=None, **kw):
        return self.src.call(model, "search_read", [domain or []], dict(kw, context={"active_test": False}))

    def test_version_comes_from_the_base_module(self):
        self.assertEqual(self.src.version()["server_version"], "18.0")
        self.assertEqual(self.src.version()["server_version_info"][:2], [18, 0])

    def test_domains_prefix_operators_and_null_handling(self):
        self.assertEqual([r["id"] for r in self.read("website.page", [("website_id", "!=", False)])], [2, 3])
        self.assertEqual([r["id"] for r in self.read("website.page", [("website_id", "=", False)])], [1])
        self.assertEqual([r["id"] for r in self.read("website.page", ["|", ("url", "=", "/"), ("url", "=like", "/pr%")])], [1, 3])
        self.assertEqual([r["id"] for r in self.read("website.page", ["!", ("is_published", "=", True)])], [3])
        self.assertEqual([r["id"] for r in self.read("website.page", [("id", "not in", [1, 2])])], [3])
        self.assertEqual([r["id"] for r in self.read("website.page", [("id", "in", [])])], [])
        self.assertEqual(self.src.call("website.page", "search_count", [[("is_published", "=", False)]]), 1)
        self.assertEqual(self.src.call("website.page", "search", [[("url", "=", "/about")]]), [2])
        self.assertEqual([r["id"] for r in self.read("website.page", order="id desc", limit=2)], [3, 2])

    def test_negation_keeps_empty_values_like_odoo(self):
        # attachments 1 and 2 have no url at all: Odoo's `!=` and `!` keep them, plain SQL would drop them
        self.assertEqual([r["id"] for r in self.read("ir.attachment", [("url", "!=", "/web/static/x.png")])], [1, 2])
        self.assertEqual([r["id"] for r in self.read("ir.attachment", ["!", ("url", "=like", "/web/%")])], [1, 2])
        self.assertEqual([r["id"] for r in self.read("ir.attachment", [("id", "not in", [1])])], [2, 3])

    def test_values_translations_booleans_and_relations(self):
        row = self.read("website")[0]
        self.assertEqual(row["name"], "Post Open")
        self.assertIs(row["active"], True)
        self.assertEqual(row["theme_id"][0], 551)
        view = self.read("ir.ui.view", fields=["arch_db", "cover", "website_id", "key"])[0]
        self.assertEqual(view["arch_db"], "<t>about</t>")  # en_US out of the jsonb
        self.assertEqual(view["cover"], '{"a": 1}')  # a plain text field that merely looks like JSON stays as it is
        self.assertEqual(view["website_id"][0], 1)
        post = self.read("blog.post", fields=["name", "blog_id", "tag_ids", "website_url"])[0]
        self.assertEqual(post["blog_id"], [4, "Bruce's Blog"])
        self.assertEqual(post["tag_ids"], [2, 3])
        self.assertEqual(post["website_url"], "/blog/bruce-s-blog-4/hello-world-7")
        self.assertEqual(self.read("blog.blog", fields=["website_url"])[0]["website_url"], "/blog/bruce-s-blog-4")

    def test_files_come_from_the_filestore_and_from_the_database(self):
        att = self.src.call("ir.attachment", "read", [[1]], {"fields": ["datas"]})[0]
        self.assertEqual(base64.b64decode(att["datas"]), b"FILEBYTES")
        self.assertEqual(self.src.call("ir.attachment", "read", [[3]], {"fields": ["datas"]})[0]["datas"], False)  # url type
        favicon = self.src.call("website", "read", [[1]], {"fields": ["favicon", "logo"]})[0]
        self.assertEqual(base64.b64decode(favicon["favicon"]), b"ICO")  # a binary field kept as an attachment
        self.assertEqual(favicon["logo"], False)

    def test_a_missing_filestore_file_is_an_error_not_silence(self):
        self.db.run("INSERT INTO ir_attachment VALUES (9,'gone','binary',NULL,1,1,NULL,NULL,NULL,'zz/missing',NULL,1)")
        with self.assertRaises(snap.SnapshotError):
            self.src.call("ir.attachment", "read", [[9]], {"fields": ["datas"]})
        self.db.run("INSERT INTO ir_attachment VALUES (10,'evil','binary',NULL,1,1,NULL,NULL,NULL,'../../etc/passwd',NULL,1)")
        with self.assertRaises(snap.SnapshotError):
            self.src.call("ir.attachment", "read", [[10]], {"fields": ["datas"]})

    def test_fields_get_comes_from_the_snapshots_own_metadata(self):
        fields = self.src.call("blog.post", "fields_get", [], {})
        self.assertEqual(fields["tag_ids"]["type"], "many2many")
        self.assertEqual(fields["website_url"]["store"], False)
        self.assertTrue(fields["name"]["translate"])

    def test_only_selects_are_issued_and_only_read_methods_exist(self):
        self.read("website")
        with self.assertRaises(PermissionError):
            self.src.call("website", "write", [[1], {"name": "x"}])
        with self.assertRaises(PermissionError):
            mig.ReadOnlySource(self.src).call("website", "unlink", [[1]])
        with self.assertRaises(PermissionError):
            mig.ReadOnlySource(self.src).call("website", "create", [{}])

    def test_unsafe_names_and_unsupported_filters_are_refused(self):
        with self.assertRaises(xmlrpc.client.Fault):
            self.src.call("website; DROP TABLE website", "search", [[]])
        with self.assertRaises(xmlrpc.client.Fault):
            self.read("website.page", [("url; DROP TABLE x", "=", "/")])
        with self.assertRaises(xmlrpc.client.Fault):
            self.read("ir.ui.view", [("arch_db", "=", "x")])  # translated: not filterable offline
        with self.assertRaises(xmlrpc.client.Fault):
            self.read("website", [("nonexistent_field", "=", 1)])
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM website")[0]["n"], 1)

    def test_a_model_without_a_table_is_reported_not_exported(self):
        self.assertEqual(self.src.call("website.abstract.mixin", "search_count", [[]]), 0)
        with self.assertRaises(xmlrpc.client.Fault):
            self.read("website.abstract.mixin")


class RestoreTests(unittest.TestCase):
    def runner(self, existing=False):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)

            class Result:
                stdout = "1\n" if existing else ""

            return Result()

        return run, calls

    def test_restore_creates_a_new_scratch_database_only(self):
        run, calls = self.runner()
        snap.restore_snapshot("/dump", "mig_x", runner=run, exists=lambda p: p == "/dump")
        self.assertEqual(calls[1], ["createdb", "mig_x"])
        self.assertEqual(calls[2][:2], ["pg_restore", "--no-owner"])

    def test_refusals(self):
        run, calls = self.runner()
        for name in ("hams_prod", "postgres", "mig_x; drop", "MIG_X"):
            with self.assertRaises(snap.SnapshotError):
                snap.restore_snapshot("/dump", name, runner=run, exists=lambda p: True)
        with self.assertRaises(snap.SnapshotError):  # production host
            snap.restore_snapshot("/dump", "mig_x", runner=run, exists=lambda p: True)
        with self.assertRaises(snap.SnapshotError):  # no dump
            snap.restore_snapshot("/dump", "mig_x", runner=run, exists=lambda p: False)
        run2, _ = self.runner(existing=True)
        with self.assertRaises(snap.SnapshotError):  # exists already
            snap.restore_snapshot("/dump", "mig_x", runner=run2, exists=lambda p: p == "/dump")
        self.assertEqual(calls, [])


class SnapshotExportTests(unittest.TestCase):
    """The exporter, unchanged, over the snapshot: same files, same checksums as a live export."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="snapshot_export_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = build_snapshot(os.path.join(self.tmp, "filestore"))
        # views of the website: a page view, a stock copy and a theme copy
        self.db.run("INSERT INTO ir_ui_view VALUES (12,'Press','website.press',1,?,NULL)", (json.dumps({"en_US": "<t>press</t>"}),))
        self.db.run("INSERT INTO ir_ui_view VALUES (20,'x','theme_x.s_banner',1,?,NULL)", (json.dumps({"en_US": "<t/>"}),))
        self.out = os.path.join(self.tmp, "export")

    def test_export_over_a_snapshot_is_verifiable_and_skips_what_is_not_content(self):
        src = mig.ReadOnlySource(snap.SnapshotSource(self.db, os.path.join(self.tmp, "filestore")))
        logs = []
        exporter = mig.Exporter(src, self.out, log=logs.append)
        manifest = exporter.run()
        self.assertEqual(mig.verify_export(self.out), [])
        views = mig.read_jsonl(os.path.join(self.out, "data", "ir.ui.view.jsonl"))
        self.assertEqual(sorted(v["key"] for v in views), ["website.about", "website.press"])
        self.assertEqual(manifest["models"]["ir.ui.view"]["view_classes"]["theme"], ["theme_x.s_banner"])
        pages = mig.read_jsonl(os.path.join(self.out, "data", "website.page.jsonl"))
        self.assertEqual(sorted(p["url"] for p in pages), ["/about", "/press"])  # the generic "/" is module data
        attachments = mig.read_jsonl(os.path.join(self.out, "data", "ir.attachment.jsonl"))
        self.assertEqual([a["name"] for a in attachments], ["hero.jpg"])  # the url-type stock image is not content
        with open(os.path.join(self.out, "files", "att_1"), "rb") as handle:
            self.assertEqual(handle.read(), b"FILEBYTES")
        with open(os.path.join(self.out, "files", "website__1__favicon"), "rb") as handle:
            self.assertEqual(handle.read(), b"ICO")
        website = mig.read_jsonl(os.path.join(self.out, "data", "website.jsonl"))[0]
        self.assertEqual(website["theme_id"][0], 551)
        inventory = json.load(open(os.path.join(self.out, "inventory.json")))
        self.assertEqual(inventory["websites"][0]["name"], "Post Open")
        self.assertEqual(sorted(p["url"] for p in inventory["pages"]), ["/about", "/press"])

    def test_the_cli_exports_a_snapshot_and_refuses_both_sources(self):
        messages = []
        code = mig.main(["export", "--creds", "x", "--snapshot-db", "mig_x", "--out", self.out], messages.append)
        self.assertEqual(code, 2)
        self.assertTrue(any("not both" in m for m in messages))
        code = mig.main(["export", "--out", self.out], messages.append)
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------------------------
# Stock-view classification inside the exporter, and the importer's handling of each class
# --------------------------------------------------------------------------------------------


def server_fields():
    return {
        "website": F(name="char", domain="char", theme_id="many2one", user_id="many2one", social_twitter="char",
                     auth_signup_uninvited="char", language_ids="many2many"),
        "ir.ui.view": F(name="char", key="key", arch_db="text", website_id="many2one", inherit_id="many2one", type="char",
                        active="boolean", priority="integer"),
        "website.page": F(url="char", name="char", view_id="many2one", website_id="many2one", is_published="boolean"),
        "website.menu": F(name="char", url="char", parent_id="many2one", page_id="many2one", website_id="many2one", sequence="integer"),
        "ir.asset": F(name="char", path="char", target="char", bundle="char", directive="char", website_id="many2one"),
        "ir.attachment": F(name="char", type="char", public="boolean", checksum="char", file_size="integer", url="char",
                           mimetype="char", res_model="char", res_id="integer", website_id="many2one", datas="binary"),
        "ir.module.module": F(name="char", state="char"),
        "blog.blog": F(name="char"),
        "blog.post": F(name="char", blog_id="many2one", content="html", website_url="char"),
    }


class ExporterClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="classify_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        fields = server_fields()
        models = {
            "website": [{"id": 1, "name": "Site", "theme_id": 551, "user_id": 4}],
            "ir.ui.view": [
                {"id": 1, "key": "website.header", "website_id": False, "arch_db": "<t>h</t>", "active": True, "type": "qweb"},
                {"id": 2, "key": "website.footer_x", "website_id": False, "arch_db": "<t>f</t>", "active": True, "type": "qweb"},
                {"id": 3, "key": "website.cookies", "website_id": False, "arch_db": "<t>c</t>", "active": True, "type": "qweb"},
                {"id": 10, "key": "website.header", "website_id": 1, "arch_db": "<t>h</t>", "active": True, "type": "qweb", "inherit_id": 1},
                {"id": 11, "key": "website.footer_x", "website_id": 1, "arch_db": "<t>f</t>", "active": False, "type": "qweb", "inherit_id": 1},
                {"id": 12, "key": "website.cookies", "website_id": 1, "arch_db": "<t>EDITED</t>", "active": True, "type": "qweb", "inherit_id": 1},
                {"id": 13, "key": "theme_x.s_a", "website_id": 1, "arch_db": "<t/>", "active": True, "type": "qweb"},
                {"id": 14, "key": "website.about", "website_id": 1, "arch_db": "<t>a</t>", "active": True, "type": "qweb"},
                {"id": 15, "key": False, "name": "mine", "website_id": 1, "arch_db": "<t>m</t>", "active": True, "type": "qweb"},
            ],
            "website.page": [{"id": 1, "url": "/about", "view_id": 14, "website_id": 1}],
            "ir.module.module": [{"id": 1, "name": "website", "state": "installed"}],
        }
        self.source = FakeOdoo([18, 0, 0, "final", 0, ""], models, fields, writes_allowed=False)
        self.out = os.path.join(self.tmp, "e")

    def test_only_the_site_owners_views_are_exported_with_their_class_and_parent_key(self):
        exporter = mig.Exporter(mig.ReadOnlySource(self.source), self.out, log=lambda m: None)
        manifest = exporter.run()
        views = {v["id"]: v for v in mig.read_jsonl(os.path.join(self.out, "data", "ir.ui.view.jsonl"))}
        self.assertEqual(sorted(views), [11, 12, 14, 15])
        self.assertEqual(views[11]["_class"], "toggle")
        self.assertEqual(views[12]["_class"], "customized")
        self.assertEqual(views[14]["_class"], "page")
        self.assertEqual(views[15]["_class"], "custom")
        self.assertEqual(views[12]["_inherit_key"], "website.header")
        self.assertFalse(views[14]["_inherit_key"])
        classes = manifest["models"]["ir.ui.view"]["view_classes"]
        self.assertEqual(classes["stock_copy"], ["website.header"])
        self.assertEqual(classes["theme"], ["theme_x.s_a"])
        self.assertEqual(mig.verify_export(self.out), [])


class FakeTarget(FakeOdoo):
    """A target that also answers the calls the importer and the lockdown make beyond the basics."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    def call(self, model, method, args, kwargs=None):
        self.calls.append((model, method, args, kwargs or {}))
        if model == "website.assets" and method == "save_asset":
            raise xmlrpc.client.Fault(1, "TypeError: cannot marshal None unless allow_none is enabled")
        if model == "ir.ui.view" and method == "write" and (kwargs or {}).get("context", {}).get("website_id"):
            # Odoo copies the generic view for the website when it is written with the website in the context
            generic = next(r for r in self.rows["ir.ui.view"] if r["id"] == args[0][0] and not r["website_id"])
            copy = dict(generic, id=max(r["id"] for r in self.rows["ir.ui.view"]) + 1, website_id=kwargs["context"]["website_id"])
            copy.update(args[1])
            self.rows["ir.ui.view"].append(copy)
            return True
        if method == "unlink":
            self.rows[model] = [r for r in self.rows[model] if r["id"] not in args[0]]
            return True
        if model == "ir.model.data" and method == "search_read":
            return [{"id": r["id"], "res_id": r["res_id"]} for r in self.rows.get(model, [])
                    if all(r.get(f) == v for f, _o, v in args[0])]
        return super().call(model, method, args, kwargs)


def target_fields():
    fields = server_fields()
    fields["website"]["social_twitter"]["readonly"] = False
    fields["ir.config_parameter"] = F(key="char", value="char")
    fields["ir.mail_server"] = F(name="char", active="boolean")
    fields["mail.template"] = F(name="char", active="boolean")
    fields["ir.cron"] = F(name="char", active="boolean")
    fields["ir.model.data"] = F(module="char", name="char", model="char", res_id="integer")
    fields["website.rewrite"] = F(name="char", url_from="char", url_to="char", redirect_type="char", website_id="many2one")
    return fields


class ImporterClassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="import_class_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.export = os.path.join(self.tmp, "e")
        os.makedirs(os.path.join(self.export, "data"))
        os.makedirs(os.path.join(self.export, "files"))
        self.scss = os.path.join(self.export, "files", "att_777")
        with open(self.scss, "w", encoding="utf-8") as handle:
            handle.write("$x: 1;\n")
        form = ('<t t-name="website.contactus"><div id="wrap"><section class="s_website_form"><form action="/website/form/"/></section></div></t>')
        data = {
            "website": [{"id": 1, "name": "Post Open", "domain": "https://postopen.org", "theme_id": [551, "theme_odoo_experts"],
                         "user_id": [4, "Public user"], "social_twitter": "https://x.example/p", "auth_signup_uninvited": "b2c"}],
            "ir.ui.view": [
                {"id": 10, "key": "website.contactus", "name": "Contact", "website_id": [1, "s"], "arch_db": form, "active": True,
                 "type": "qweb", "_class": "page", "_inherit_key": False},
                {"id": 11, "key": "website.footer_x", "name": "f", "website_id": [1, "s"], "arch_db": "<t>f</t>", "active": False,
                 "type": "qweb", "_class": "toggle", "_inherit_key": "website.layout", "inherit_id": [1, "x"]},
                {"id": 12, "key": "website_forum.forum_all", "name": "forum", "website_id": [1, "s"], "arch_db": "<t>x</t>", "active": True,
                 "type": "qweb", "_class": "customized", "_inherit_key": False},
                {"id": 13, "key": "website.cookies", "name": "c", "website_id": [1, "s"], "arch_db": "<t>EDITED</t>", "active": True,
                 "type": "qweb", "_class": "customized", "_inherit_key": "website.layout", "inherit_id": [1, "x"]},
            ],
            "website.page": [{"id": 1, "url": "/contactus", "name": "Contact", "view_id": [10, "v"], "website_id": [1, "s"], "is_published": True}],
            "website.menu": [
                {"id": 1, "name": "Top Menu for Website 1", "url": "/default-main-menu", "website_id": [1, "s"], "parent_id": False, "sequence": 0},
                {"id": 2, "name": "PostOpen.org", "url": "/", "website_id": [1, "s"], "parent_id": [1, "t"], "sequence": 1},
                {"id": 3, "name": "News", "url": "/blog/3", "website_id": [1, "s"], "parent_id": [1, "t"], "sequence": 2},
                {"id": 4, "name": "Discussions", "url": "/forum", "website_id": [1, "s"], "parent_id": [1, "t"], "sequence": 3},
                {"id": 5, "name": "Hidden", "url": "/secret", "website_id": [1, "s"], "parent_id": [1, "t"], "sequence": 4},
            ],
            "blog.blog": [{"id": 3, "name": "Bruce's Blog"}],
            "blog.post": [],
            "ir.asset": [{"id": 115, "name": "x", "path": "/_custom/web.assets_frontend/website/static/src/scss/options/user_values.scss",
                          "target": "/website/static/src/scss/options/user_values.scss", "bundle": "web._assets_primary_variables",
                          "website_id": [1, "s"]}],
            "ir.attachment": [{"id": 777, "name": "user_values.scss", "type": "binary", "public": False, "website_id": [1, "s"],
                               "url": "/_custom/web.assets_frontend/website/static/src/scss/options/user_values.scss"}],
        }
        for model, rows in data.items():
            with open(os.path.join(self.export, "data", f"{model}.jsonl"), "w", encoding="utf-8") as handle:
                handle.writelines(json.dumps(r) + "\n" for r in rows)
        self.write_manifest()
        models = {
            "website": [{"id": 1, "name": "My Website", "domain": False}],
            "ir.ui.view": [
                {"id": 1, "key": "website.layout", "website_id": False, "arch_db": "<t>layout</t>", "active": True, "type": "qweb"},
                {"id": 2, "key": "website.footer_x", "website_id": False, "arch_db": "<t>f</t>", "active": True, "type": "qweb", "inherit_id": 1},
                {"id": 3, "key": "website.cookies", "website_id": False, "arch_db": "<t>c</t>", "active": True, "type": "qweb", "inherit_id": 1},
            ],
            "website.page": [], "website.rewrite": [],
            "website.menu": [{"id": 1, "name": "Top Menu", "url": "#", "website_id": 1, "parent_id": False},
                             {"id": 2, "name": "Home", "url": "/", "website_id": 1, "parent_id": 1},
                             {"id": 3, "name": "Blog", "url": "/blog", "website_id": 1, "parent_id": 1}],
            "blog.blog": [{"id": 1, "name": "Our blog", "website_id": 1, "active": True}], "blog.post": [],
            "ir.module.module": [{"id": 1, "name": "website", "state": "installed"}, {"id": 2, "name": "website_blog", "state": "installed"}],
            "ir.attachment": [], "ir.asset": [],
        }
        fields = target_fields()
        fields["blog.blog"] = F(name="char", active="boolean")
        self.target = FakeTarget([19, 0, 0, "final", 0, ""], models, fields)

    def write_manifest(self):
        with open(self.scss, "rb") as handle:
            files = {"files/att_777": hashlib.sha256(handle.read()).hexdigest()}
        with open(os.path.join(self.export, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump({"models": {}, "files": files, "skipped": {}}, handle)
        with open(os.path.join(self.export, "CHECKSUMS.sha256"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(f"{d}  {n}" for n, d in files.items()) + "\n")

    def run_import(self, apply=True, **kw):
        importer = mig.Importer(self.export, self.target, apply=apply, source_domains=["postopen.org"], log=lambda m: None, **kw)
        return importer, importer.run()

    def test_website_settings_never_carry_foreign_ids_or_signup_choices(self):
        _imp, _report = self.run_import()
        site = self.target.rows["website"][0]
        self.assertEqual(site["social_twitter"], "https://x.example/p")
        self.assertEqual(site["name"], "Post Open")
        for field in ("theme_id", "user_id", "auth_signup_uninvited"):
            self.assertNotIn(field, site, field)

    def test_the_default_company_name_becomes_the_site_name_but_a_chosen_name_stays(self):
        self.target.fields["res.company"] = F(name="char")
        self.target.fields["website"]["company_id"] = {"type": "many2one", "store": True, "readonly": False, "relation": "res.company"}
        self.target.rows["res.company"] = [{"id": 1, "name": "My Company"}]
        self.target.rows["website"][0]["company_id"] = 1
        self.run_import()
        self.assertEqual(self.target.rows["res.company"][0]["name"], "Post Open")
        self.target.rows["res.company"][0]["name"] = "Chosen Name"
        self.run_import()
        self.assertEqual(self.target.rows["res.company"][0]["name"], "Chosen Name")

    def test_page_forms_are_replaced_and_reported(self):
        _imp, report = self.run_import(forms_notice="No contact form on this site.")
        arch = next(v for v in self.target.rows["ir.ui.view"] if v.get("key") == "website.contactus")["arch_db"]
        self.assertIn("No contact form on this site.", arch)
        self.assertNotIn("<form", arch)
        self.assertEqual(report["forms_replaced"], 1)
        self.assertTrue(any("form(s) replaced" in w for w in report["warnings"]))

    def test_keep_forms_leaves_the_markup(self):
        self.run_import(forms_notice=None)
        arch = next(v for v in self.target.rows["ir.ui.view"] if v.get("key") == "website.contactus")["arch_db"]
        self.assertIn("<form", arch)

    def test_a_switch_is_applied_to_the_targets_own_view_not_as_old_markup(self):
        _imp, report = self.run_import()
        specific = [v for v in self.target.rows["ir.ui.view"] if v.get("key") == "website.footer_x" and v["website_id"] == 1]
        self.assertEqual(len(specific), 1)
        self.assertFalse(specific[0]["active"])
        self.assertEqual(specific[0]["arch_db"], "<t>f</t>")  # the target's own arch, copied by Odoo
        again = mig.Importer(self.export, self.target, apply=True, source_domains=[], log=lambda m: None).run()
        self.assertEqual(again["updated"].get("ir.ui.view (switch)"), None)
        self.assertEqual(again["matched"]["ir.ui.view (switch)"], 1)

    def test_what_cannot_be_applied_is_listed_as_lost(self):
        _imp, report = self.run_import()
        lost = {item["view"]: item["reason"] for item in report["lost"]}
        self.assertIn("module website_forum is not installed", lost["website_forum.forum_all"])
        self.assertNotIn("website.cookies", lost)  # an edit of a stock view the target has is applied
        cookies = next(v for v in self.target.rows["ir.ui.view"] if v.get("key") == "website.cookies" and v["website_id"] == 1)
        self.assertEqual(cookies["arch_db"], "<t>EDITED</t>")
        self.assertEqual(cookies["inherit_id"], 1)  # parent found by key
        self.assertIn("LOST customized website_forum.forum_all", mig.render_report(report, True))

    def test_a_rejected_view_is_lost_not_fatal(self):
        original = self.target._create

        def reject(model, args, kwargs):
            if model == "ir.ui.view" and args[0].get("key") == "website.cookies":
                raise xmlrpc.client.Fault(2, "Invalid view: xpath not found")
            return original(model, args, kwargs)

        self.target._create = reject
        _imp, report = self.run_import()
        self.assertTrue(any("rejected" in i["reason"] and i["view"] == "website.cookies" for i in report["lost"]))
        self.assertTrue(any(v.get("key") == "website.contactus" for v in self.target.rows["ir.ui.view"]))

    def test_menus_root_is_the_targets_root_and_same_address_is_same_entry(self):
        _imp, report = self.run_import(skip_menu_urls=["/secret"], remove_extra_menus=True)
        menus = {m["name"]: m for m in self.target.rows["website.menu"]}
        self.assertNotIn("Top Menu for Website 1", menus)  # the source root is the target's "Top Menu"
        self.assertEqual(menus["PostOpen.org"]["id"], 2)  # the default "Home" at "/" was renamed, not duplicated
        self.assertNotIn("Home", menus)
        self.assertEqual(menus["News"]["parent_id"], 1)
        self.assertNotIn("Discussions", menus)  # website_forum is not installed
        self.assertNotIn("Hidden", menus)
        self.assertNotIn("Blog", menus)  # the target's own default entry the source lacks, removed on request
        reasons = {s["menu"]: s["reason"] for s in report["skipped"]}
        self.assertIn("not installed", reasons["Discussions"])
        self.assertIn("command line", reasons["Hidden"])

    def test_extra_menus_are_only_listed_by_default(self):
        _imp, report = self.run_import()
        self.assertTrue(any("'Blog'" in w and "not in the source" in w for w in report["warnings"]))
        self.assertIn("Blog", {m["name"] for m in self.target.rows["website.menu"]})

    def test_a_blog_menu_follows_the_new_blog_id_and_the_old_blog_url_redirects(self):
        imp, report = self.run_import(archive_extra_blogs=True)
        new_id = imp.idmap["blog.blog"]["3"]
        news = next(m for m in self.target.rows["website.menu"] if m["name"] == "News")
        self.assertEqual(news["url"], f"/blog/{new_id}")
        redirect = next(r for r in self.target.rows["website.rewrite"] if r["url_from"] == "/blog/bruce-s-blog-3")
        self.assertEqual(redirect["url_to"], f"/blog/bruce-s-blog-{new_id}")
        self.assertEqual(redirect["redirect_type"], "301")
        our = next(b for b in self.target.rows["blog.blog"] if b["name"] == "Our blog")
        self.assertFalse(our["active"])  # archived, not deleted

    def test_design_files_go_through_odoos_own_asset_customization(self):
        _imp, report = self.run_import()
        call = next(c for c in self.target.calls if c[0] == "website.assets")
        self.assertEqual(call[2][0], "/website/static/src/scss/options/user_values.scss")
        self.assertEqual(call[2][1], "web.assets_frontend")
        self.assertEqual(call[2][2], "$x: 1;\n")
        self.assertEqual(call[3]["context"]["website_id"], 1)
        self.assertFalse([i for i in report["lost"] if i["kind"] == "asset"])  # the None-marshal fault means success

    def test_a_dry_run_changes_nothing_but_still_reports(self):
        _imp, report = self.run_import(apply=False)
        writes = [c for c in self.target.calls if c[1] in ("create", "write", "unlink", "save_asset")]
        self.assertEqual(writes, [])
        self.assertEqual(report["forms_replaced"], 1)
        self.assertTrue(report["lost"])


class LockdownTests(unittest.TestCase):
    def build(self, scope="b2c"):
        fields = target_fields()
        models = {
            "website": [{"id": 1, "name": "Site", "auth_signup_uninvited": "b2c"}],
            "ir.config_parameter": [{"id": 1, "key": "auth_signup.invitation_scope", "value": scope},
                                    {"id": 2, "key": "auth_signup.reset_password", "value": "True"}],
            "ir.mail_server": [{"id": 1, "name": "smtp", "active": True}],
            "mail.template": [{"id": 1, "name": "a", "active": True}, {"id": 2, "name": "b", "active": True}, {"id": 3, "name": "c", "active": False}],
            "ir.cron": [{"id": 5, "name": "queue", "active": True}, {"id": 6, "name": "gateway", "active": False}],
            "ir.model.data": [{"id": 1, "module": "mail", "name": "ir_cron_mail_scheduler_action", "model": "ir.cron", "res_id": 5},
                              {"id": 2, "module": "mail", "name": "ir_cron_mail_gateway_action", "model": "ir.cron", "res_id": 6}],
        }
        return FakeTarget([19, 0, 0, "final", 0, ""], models, fields)

    def test_dry_run_lists_and_apply_changes_and_a_second_run_changes_nothing(self):
        target = self.build()
        lines = []
        actions = mig.lockdown_target(target, False, lines.append)
        self.assertGreaterEqual(sum(a["changed"] for a in actions), 5)
        self.assertTrue(target.rows["ir.mail_server"][0]["active"])  # a dry run writes nothing
        mig.lockdown_target(target, True, lambda m: None)
        params = {r["key"]: r["value"] for r in target.rows["ir.config_parameter"]}
        self.assertEqual(params["auth_signup.invitation_scope"], "b2b")
        self.assertEqual(params["auth_signup.reset_password"], "False")
        self.assertEqual(target.rows["website"][0]["auth_signup_uninvited"], "b2b")
        self.assertFalse(target.rows["ir.mail_server"][0]["active"])
        self.assertFalse(any(t["active"] for t in target.rows["mail.template"]))
        self.assertFalse(next(c for c in target.rows["ir.cron"] if c["id"] == 5)["active"])
        again = mig.lockdown_target(target, True, lambda m: None)
        self.assertEqual(sum(a["changed"] for a in again), 0)

    def test_nothing_is_ever_deleted(self):
        target = self.build()
        mig.lockdown_target(target, True, lambda m: None)
        self.assertFalse([c for c in target.calls if c[1] in ("unlink", "delete")])

    def test_a_missing_parameter_is_created(self):
        target = self.build()
        target.rows["ir.config_parameter"] = []
        mig.lockdown_target(target, True, lambda m: None)
        self.assertEqual({r["key"] for r in target.rows["ir.config_parameter"]}, set(mig.LOCKDOWN_PARAMS))


# --------------------------------------------------------------------------------------------
# site_compare
# --------------------------------------------------------------------------------------------

PAGE_A = """<html><head><title>Post Open</title><meta name="description" content="D"><meta name="robots" content="noindex"></head>
<body><header><nav aria-label="Main"><a href="/a">A</a><a href="/shop/cart">Cart</a></nav></header>
<main><div id="wrap"><h1>Hello</h1><p>Some   text <a href="https://postopen.org/x">link</a></p><img src="/web/image/1?unique=abc"></div></main>
<footer>Copyright</footer><script>var x = "hidden";</script></body></html>"""

PAGE_B = PAGE_A.replace('noindex', 'all').replace("/shop/cart", "/blog/2").replace("Some   text", "Some text").replace("?unique=abc", "?unique=zzz")


class SiteCompareTests(unittest.TestCase):
    def test_equivalent_pages_have_no_body_differences_and_metadata_differences_are_reported(self):
        diff = sc.compare_pages(PAGE_A, PAGE_B, hosts=["postopen.org"])
        self.assertEqual(diff["text"], [])
        self.assertEqual(diff["images"], [])
        self.assertEqual(diff["links"], [])
        self.assertEqual(len(diff["meta"]), 1)
        self.assertIn("robots", diff["meta"][0])
        self.assertTrue(diff["nav"])  # navigation differences are reported on their own

    def test_a_changed_word_a_lost_image_and_a_lost_link_are_found(self):
        changed = PAGE_A.replace("Some   text", "Other text").replace('<img src="/web/image/1?unique=abc">', "").replace(
            "https://postopen.org/x", "/y")
        diff = sc.compare_pages(PAGE_A, changed, hosts=["postopen.org"])
        self.assertTrue(any("Some" in removed or "text" in removed for removed, _added in diff["text"]))
        self.assertEqual(diff["images"], ["missing /web/image/1"])
        self.assertIn("missing /x", diff["links"])
        self.assertIn("added /y", diff["links"])

    def test_script_text_is_ignored_and_the_title_is_read(self):
        page = sc.parse_page(PAGE_A)
        self.assertNotIn("hidden", page["main"] + page["footer"])
        self.assertEqual(page["title"], "Post Open")
        self.assertEqual(page["main"], "Hello Some text link")

    def test_names_for_paths(self):
        self.assertEqual(sc.name_for_path("/"), "ROOT")
        self.assertEqual(sc.name_for_path("/documents/license"), "documents__license")

    def test_run_over_a_fake_server_reports_status_text_and_images(self):
        tmp = tempfile.mkdtemp(prefix="compare_")
        self.addCleanup(shutil.rmtree, tmp, True)
        for name, html in (("ROOT", PAGE_A), ("press", PAGE_A), ("gone", PAGE_A)):
            with open(os.path.join(tmp, f"{name}.html"), "w", encoding="utf-8") as handle:
                handle.write(html)

        class Fake:
            def __init__(self):
                self.seen = []

            def get(self, path, method="GET"):
                self.seen.append((method, path))
                if path == "/":
                    return 200, PAGE_B.encode(), {}
                if path == "/press":
                    return 200, PAGE_A.replace("Hello", "Goodbye").encode(), {}
                if path.startswith("/web/image"):
                    return 200, b"", {}
                return 404, b"", {}

        fake = Fake()
        lines = []
        rows, problems = sc.run(tmp, fake, ["/", "/press", "/gone", "/missing-original"], ["postopen.org"], lines.append)
        by = {r[0]: r for r in rows}
        self.assertEqual(by["/"][2], "same")
        self.assertEqual(by["/press"][2], "1 difference(s)")
        self.assertIn("404", by["/gone"][1])
        self.assertEqual(by["/missing-original"][1], "no saved original")
        self.assertEqual(problems, 2)
        self.assertIn(("HEAD", "/web/image/1"), fake.seen)  # the cache-busting query is not part of the comparison
        self.assertIn("| / | 200 | same |", sc.render_table(rows))

    def test_the_fetcher_is_polite(self):
        sleeps = []

        class Response:
            status = 200
            headers = {}

            def read(self):
                return b"x"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout=0):
                self.requests.append(request)
                return Response()

        opener = Opener()
        fetcher = sc.Fetcher("http://127.0.0.1:1", host="postopen.org", interval=1.0, sleep=sleeps.append, opener=opener)
        fetcher.get("/a")
        fetcher.get("/b")
        self.assertTrue(sleeps and sleeps[-1] > 0)  # the second request waited
        self.assertEqual(opener.requests[0].get_header("User-agent"), sc.USER_AGENT)
        self.assertEqual(opener.requests[0].get_header("Host"), "postopen.org")


if __name__ == "__main__":
    unittest.main()
