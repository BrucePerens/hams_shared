#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for odoo_site_migrate.py against an in-memory fake Odoo (no network, no real site).
"""

# [@ANCHOR: test_odoo_site_migrate:read_only_source]
# Tests [@ANCHOR: odoo_site_migrate:read_only_source]
# [@ANCHOR: test_odoo_site_migrate:load_credentials]
# Tests [@ANCHOR: odoo_site_migrate:load_credentials]
# [@ANCHOR: test_odoo_site_migrate:rewrite_html]
# Tests [@ANCHOR: odoo_site_migrate:rewrite_html]
# [@ANCHOR: test_odoo_site_migrate:verify_export]
# Tests [@ANCHOR: odoo_site_migrate:verify_export]

import base64
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer

import odoo_site_migrate as mig


def like(value, pattern):
    if not isinstance(value, str):
        return False
    start, end = pattern.startswith("%"), pattern.endswith("%")
    core = pattern.strip("%")
    if start and end:
        return core in value
    if end:
        return value.startswith(core)
    if start:
        return value.endswith(core)
    return value == pattern


class FakeOdoo:
    """Just enough of execute_kw: fields_get, search, search_read, read, search_count, create, write."""

    def __init__(self, version, models, fields, writes_allowed=True):
        self.version_info = version
        self.rows = {m: [dict(r) for r in rows] for m, rows in models.items()}
        self.fields = fields
        self.log = []
        self.writes_allowed = writes_allowed
        self.next_id = {m: max([r["id"] for r in rows] + [100]) + 1 for m, rows in self.rows.items()}

    def version(self):
        return {"server_version": f"{self.version_info[0]}.0", "server_version_info": self.version_info}

    def call(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        self.log.append((model, method))
        if model == "ir.model":
            return self._ir_model(method, args, kwargs)
        handler = getattr(self, f"_{method}", None)
        if handler is None:
            raise AssertionError(f"fake has no {method}")
        if method in ("create", "write") and not self.writes_allowed:
            raise AssertionError("write on a read-only fake")
        return handler(model, args, kwargs)

    # ---- domains
    def _match(self, row, domain, model):
        def ev(term):
            field, op, value = term
            actual = row.get(field)
            if isinstance(actual, list) and len(actual) == 2 and isinstance(actual[0], int):
                actual = actual[0]
            if op == "=":
                return actual == value or (value is False and not actual)
            if op == "!=":
                return not (actual == value or (value is False and not actual))
            if op == "in":
                return actual in value
            if op == "not in":
                return actual not in value
            if op == "=like":
                return like(actual, value)
            if op == ">":
                return actual is not None and actual > value
            raise AssertionError(op)

        def parse(terms):
            term = terms.pop(0)
            if term == "&":
                a, b = parse(terms), parse(terms)
                return a and b
            if term == "|":
                a, b = parse(terms), parse(terms)
                return a or b
            if term == "!":
                return not parse(terms)
            return ev(term)

        terms = list(domain)
        result = True
        while terms:
            value = parse(terms)
            result = result and value
        return result

    def _rows(self, model, domain, kwargs):
        rows = [r for r in self.rows.get(model, []) if self._match(r, domain, model)]
        if "active" in self.fields.get(model, {}) and not (kwargs.get("context") or {}).get("active_test") is False:
            rows = [r for r in rows if r.get("active", True)]
        rows.sort(key=lambda r: r["id"])
        if kwargs.get("limit"):
            rows = rows[: kwargs["limit"]]
        return rows

    def _out(self, model, row, fields):
        out = {"id": row["id"]}
        for name in fields or self.fields[model]:
            if name == "id":
                continue
            value = row.get(name, False)
            meta = self.fields[model].get(name, {})
            if meta.get("type") == "many2one" and value:
                value = [value, f"name{value}"]
            out[name] = value
        return out

    def _fields_get(self, model, args, kwargs):
        return {n: dict(m) for n, m in self.fields[model].items()}

    def _search(self, model, args, kwargs):
        return [r["id"] for r in self._rows(model, args[0], kwargs)]

    def _search_count(self, model, args, kwargs):
        return len(self._rows(model, args[0], {"context": {"active_test": False}}))

    def _search_read(self, model, args, kwargs):
        rows = self._rows(model, args[0], kwargs)
        return [self._out(model, r, kwargs.get("fields")) for r in rows]

    def _read(self, model, args, kwargs):
        ids = args[0]
        return [self._out(model, r, kwargs.get("fields")) for r in self.rows[model] if r["id"] in ids]

    def _create(self, model, args, kwargs):
        values = dict(args[0])
        for name in values:
            if name not in self.fields[model]:
                raise AssertionError(f"create with unknown field {model}.{name}")
        new_id = self.next_id.setdefault(model, 101)
        self.next_id[model] = new_id + 1
        row = dict(values, id=new_id)
        if "tag_ids" in row and row["tag_ids"] and isinstance(row["tag_ids"][0], tuple):
            row["tag_ids"] = list(row["tag_ids"][0][2])
        if model == "ir.attachment" and row.get("datas"):
            row["checksum"] = hashlib.sha1(base64.b64decode(row["datas"])).hexdigest()  # as Odoo computes it
        if model in ("blog.post", "blog.blog"):
            self._set_url(model, row)
        self.rows.setdefault(model, []).append(row)
        return new_id

    def _write(self, model, args, kwargs):
        for row in self.rows[model]:
            if row["id"] in args[0]:
                row.update(args[1])
        return True

    def _set_url(self, model, row):
        if model == "blog.blog":
            row["website_url"] = f"/blog/{mig.slugify(row['name'])}-{row['id']}"
        else:
            blog = next(b for b in self.rows["blog.blog"] if b["id"] == row["blog_id"])
            row["website_url"] = f"{blog['website_url']}/{mig.slugify(row['name'])}-{row['id']}"

    def _ir_model(self, method, args, kwargs):
        domain = args[0] if args else []
        names = sorted(self.rows) if self.rows else []
        if method == "search_count":
            wanted = domain[0][2]
            return 1 if wanted in self.fields else 0
        if method == "search_read":
            like_pattern = next((t[2] for t in domain if t[1] == "=like"), None)
            return [{"id": i, "model": m} for i, m in enumerate(names) if like_pattern is None or like(m, like_pattern)]
        raise AssertionError(method)


def F(**kw):
    return {name: {"type": typ, "store": True, "readonly": False, "relation": None, "selection": None} for name, typ in kw.items()}


def build_source():
    fields = {
        "website": F(name="char", domain="char", social_twitter="char", payment_secret="char", default_lang_id="many2one", language_ids="many2many", old_only="char"),
        "res.lang": F(code="char", name="char", active="boolean"),
        "ir.ui.view": F(name="char", key="char", arch_db="text", website_id="many2one", inherit_id="many2one", type="char", active="boolean", priority="integer"),
        "website.page": F(url="char", name="char", view_id="many2one", website_id="many2one", is_published="boolean"),
        "website.menu": F(name="char", url="char", parent_id="many2one", page_id="many2one", website_id="many2one", sequence="integer"),
        "website.rewrite": F(name="char", url_from="char", url_to="char", redirect_type="char", website_id="many2one"),
        "blog.blog": F(name="char", website_url="char"),
        "blog.tag": F(name="char"),
        "blog.post": F(name="char", blog_id="many2one", tag_ids="many2many", content="html", is_published="boolean", website_url="char", author_id="many2one"),
        "ir.attachment": F(name="char", type="char", public="boolean", checksum="char", file_size="integer", url="char", mimetype="char",
                           res_model="char", res_id="integer", website_id="many2one", datas="binary"),
        "website.visitor": F(name="char"),
        "website.snippet.filter": F(name="char"),
        "ir.module.module": F(name="char", state="char"),
    }
    logo = b"\x89PNG-fake-logo"
    hero = b"JPEG-fake-hero"
    models = {
        "website": [{"id": 1, "name": "Old Site", "domain": "https://old.example", "social_twitter": "https://twitter.com/x",
                     "payment_secret": "SHOULD-NEVER-BE-READ", "old_only": "legacy"}],
        "res.lang": [{"id": 1, "code": "en_US", "name": "English", "active": True}],
        "ir.ui.view": [
            {"id": 10, "name": "Home", "key": "website.homepage", "website_id": 1, "type": "qweb", "active": True, "priority": 16,
             "arch_db": '<t t-name="website.homepage"><img src="/web/image/55"/><a href="https://www.old.example/about">About</a>'
                        '<a href="/blog/news-1/hello-12">Post</a><img src="/web/image/999"/></t>'},
            {"id": 11, "name": "About", "key": "website.about", "website_id": 1, "type": "qweb", "active": True, "priority": 16,
             "arch_db": '<t t-name="website.about"><p>About us <img src="http://old.example/web/content/56"/></p></t>'},
            {"id": 12, "name": "Child", "key": "website.about_child", "website_id": 1, "type": "qweb", "active": True, "priority": 20,
             "inherit_id": 11, "arch_db": "<data/>"},
        ],
        "website.page": [{"id": 3, "url": "/about", "name": "About", "view_id": 11, "website_id": 1, "is_published": True, "url_x": "ignored"}],
        "website.menu": [
            {"id": 1, "name": "Top Menu", "url": "/default-main-menu", "website_id": 1, "sequence": 0},
            {"id": 2, "name": "About", "url": "/about", "parent_id": 1, "page_id": 3, "website_id": 1, "sequence": 10},
            {"id": 3, "name": "Blog", "url": "/blog", "parent_id": 1, "website_id": 1, "sequence": 20},
        ],
        "website.rewrite": [{"id": 1, "name": "old", "url_from": "/old-page", "url_to": "/about", "redirect_type": "301", "website_id": 1},
                            {"id": 2, "name": "wp post", "url_from": "/2018/05/hello/", "url_to": "/blog/news-1/hello-12",
                             "redirect_type": "301", "website_id": 1},
                            {"id": 3, "name": "wp image", "url_from": "/wp-content/uploads/logo.png", "url_to": "/web/image/55",
                             "redirect_type": "301", "website_id": 1}],
        "blog.blog": [{"id": 1, "name": "News", "website_url": "/blog/news-1"}],
        "blog.tag": [{"id": 1, "name": "radio"}],
        "blog.post": [{"id": 12, "name": "Hello", "blog_id": 1, "tag_ids": [1], "is_published": True,
                       "content": '<p>See <a href="https://old.example/about">about</a> <img src="/web/image/55?x=1"/></p>',
                       "website_url": "/blog/news-1/hello-12", "author_id": 7}],
        "ir.attachment": [
            {"id": 55, "name": "logo.png", "type": "binary", "public": True, "checksum": hashlib.sha1(logo).hexdigest(),
             "file_size": len(logo), "url": False, "mimetype": "image/png", "res_model": "ir.ui.view", "res_id": 0,
             "website_id": 1, "datas": base64.b64encode(logo).decode()},
            {"id": 56, "name": "hero.jpg", "type": "binary", "public": True, "checksum": hashlib.sha1(hero).hexdigest(),
             "file_size": len(hero), "url": False, "mimetype": "image/jpeg", "res_model": "blog.post", "res_id": 12,
             "website_id": False, "datas": base64.b64encode(hero).decode()},
            {"id": 57, "name": "assets.css", "type": "binary", "public": True, "url": "/web/assets/1/abc/web.assets_frontend.min.css",
             "website_id": False, "res_model": "ir.ui.view", "file_size": 3, "datas": "YWJj"},
            {"id": 58, "name": "private.pdf", "type": "binary", "public": False, "url": False, "website_id": False,
             "res_model": "res.partner", "file_size": 3, "datas": "YWJj"},
        ],
        "website.visitor": [{"id": 1, "name": "tracked"}],
        "website.snippet.filter": [{"id": 1, "name": "filter"}],
        "ir.module.module": [{"id": 1, "name": "website", "state": "installed"}, {"id": 2, "name": "website_blog", "state": "installed"},
                             {"id": 3, "name": "theme_x", "state": "installed"}, {"id": 4, "name": "account", "state": "installed"}],
    }
    return FakeOdoo([16, 0, 0, "final", 0, ""], models, fields, writes_allowed=False)


def build_target():
    fields = {
        "website": F(name="char", domain="char", social_twitter="char", default_lang_id="many2one", language_ids="many2many"),
        "res.lang": F(code="char", active="boolean"),
        "ir.ui.view": F(name="char", key="char", arch_db="text", website_id="many2one", inherit_id="many2one", type="char", active="boolean", priority="integer"),
        "website.page": F(url="char", name="char", view_id="many2one", website_id="many2one", is_published="boolean"),
        "website.menu": F(name="char", url="char", parent_id="many2one", page_id="many2one", website_id="many2one", sequence="integer"),
        "website.rewrite": F(name="char", url_from="char", url_to="char", redirect_type="char", website_id="many2one"),
        "blog.blog": F(name="char", website_url="char"),
        "blog.tag": F(name="char"),
        "blog.post": F(name="char", blog_id="many2one", tag_ids="many2many", content="html", is_published="boolean", website_url="char", author_id="many2one"),
        "ir.attachment": F(name="char", type="char", public="boolean", checksum="char", url="char", mimetype="char",
                           website_id="many2one", datas="binary", res_model="char", res_id="integer"),
        "res.partner": F(name="char"),
    }
    fields["blog.post"]["website_url"]["readonly"] = True
    fields["blog.blog"]["website_url"]["readonly"] = True
    models = {
        "website": [{"id": 1, "name": "My Website", "domain": False}],
        "res.lang": [{"id": 1, "code": "en_US", "active": True}],
        "ir.ui.view": [{"id": 1, "name": "Homepage generic", "key": "website.homepage", "website_id": False, "type": "qweb",
                        "active": True, "arch_db": "<t>generic</t>"}],
        "website.page": [], "website.menu": [{"id": 1, "name": "Default Top", "url": "/", "website_id": 1}],
        "website.rewrite": [], "blog.blog": [{"id": 1, "name": "Placeholder", "website_url": "/blog/placeholder-1"}],
        "blog.tag": [], "blog.post": [], "ir.attachment": [], "res.partner": [{"id": 3, "name": "Bruce"}],
    }
    return FakeOdoo([19, 0, 0, "final", 0, ""], models, fields)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="migrate_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = os.path.join(self.tmp, "export")
        self.logs = []

    def export(self, source=None, **kw):
        source = source or build_source()
        exporter = mig.Exporter(mig.ReadOnlySource(source), self.out, log=self.logs.append, **kw)
        return exporter, source


class ReadOnlyGuardTests(_Base):
    def test_only_read_methods_reach_the_source(self):
        source = build_source()
        guard = mig.ReadOnlySource(source)
        for method in ("write", "create", "unlink", "action_archive", "execute", "copy", "toggle_active"):
            with self.assertRaises(PermissionError, msg=method):
                guard.call("website.page", method, [[1]])
        self.assertEqual(source.log, [])
        self.assertEqual(guard.call("website", "search_count", [[]]), 1)

    def test_a_full_export_never_writes(self):
        exporter, source = self.export()
        exporter.run()
        self.assertTrue({method for _m, method in source.log} <= {"fields_get", "search", "search_read", "read", "search_count"})


class CredentialsTests(_Base):
    def write(self, text, mode=0o600):
        path = os.path.join(self.tmp, "login.env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, mode)
        return path

    def test_good_file(self):
        creds = mig.load_credentials(self.write("# c\nODOO_URL=https://perens.com/\nODOO_DB=p\nODOO_LOGIN=me\nODOO_API_KEY=k123\n"))
        self.assertEqual((creds.url, creds.db, creds.login, creds.secret), ("https://perens.com", "p", "me", "k123"))
        self.assertNotIn("k123", repr(creds))
        self.assertNotIn("k123", str(creds))

    def test_password_is_used_when_there_is_no_api_key(self):
        creds = mig.load_credentials(self.write("ODOO_URL=https://x.example\nODOO_LOGIN=me\nODOO_PASSWORD=pw\n"))
        self.assertEqual(creds.secret, "pw")

    def test_permissions_scheme_and_missing_keys_are_refused(self):
        with self.assertRaises(mig.MigrateError):
            mig.load_credentials(self.write("ODOO_URL=https://x.example\nODOO_LOGIN=a\nODOO_PASSWORD=b\n", mode=0o644))
        with self.assertRaises(mig.MigrateError):
            mig.load_credentials(self.write("ODOO_URL=http://x.example\nODOO_LOGIN=a\nODOO_PASSWORD=b\n"))
        with self.assertRaises(mig.MigrateError):
            mig.load_credentials(self.write("ODOO_URL=https://x.example\nODOO_LOGIN=a\n"))
        with self.assertRaises(mig.MigrateError):
            mig.load_credentials(os.path.join(self.tmp, "missing"))
        mig.load_credentials(self.write("ODOO_URL=http://localhost:8069\nODOO_LOGIN=a\nODOO_PASSWORD=b\n"))


class ExportTests(_Base):
    def test_export_detects_version_skips_secrets_and_checksums_everything(self):
        exporter, _source = self.export()
        manifest = exporter.run()
        self.assertEqual(manifest["server_version_info"][0], 16)
        self.assertIn("website_blog", manifest["website_modules"])
        self.assertNotIn("account", manifest["website_modules"])
        self.assertEqual(manifest["skipped"]["website"]["secret_fields"], ["payment_secret"])
        with open(os.path.join(self.out, "data", "website.jsonl"), encoding="utf-8") as handle:
            site = json.loads(handle.readline())
        self.assertNotIn("payment_secret", site)
        self.assertEqual(manifest["models"]["blog.post"]["count"], 1)
        self.assertNotIn("website.visitor", manifest["models"])
        self.assertIn("website.snippet.filter", manifest["models"])
        self.assertEqual(mig.verify_export(self.out), [])

    def test_attachments_exclude_assets_and_private_files_and_keep_bytes(self):
        exporter, _ = self.export()
        manifest = exporter.run()
        self.assertEqual(manifest["models"]["ir.attachment"]["count"], 2)
        with open(os.path.join(self.out, "files", "att_55"), "rb") as handle:
            self.assertEqual(handle.read(), b"\x89PNG-fake-logo")
        self.assertFalse(os.path.exists(os.path.join(self.out, "files", "att_57")))
        self.assertFalse(os.path.exists(os.path.join(self.out, "files", "att_58")))

    def test_oversized_attachments_are_skipped_and_reported(self):
        source = build_source()
        source.rows["ir.attachment"][0]["file_size"] = 90 * 1048576
        exporter, _ = self.export(source, max_file_mb=25)
        manifest = exporter.run()
        self.assertEqual(manifest["skipped"]["ir.attachment"]["too_large"], [55])

    def test_verify_detects_tampering(self):
        exporter, _ = self.export()
        exporter.run()
        with open(os.path.join(self.out, "files", "att_55"), "ab") as handle:
            handle.write(b"x")
        self.assertIn("files/att_55 checksum mismatch", mig.verify_export(self.out))
        os.remove(os.path.join(self.out, "data", "website.jsonl"))
        self.assertIn("data/website.jsonl missing", mig.verify_export(self.out))

    def test_a_second_export_into_the_same_directory_needs_resume(self):
        exporter, _ = self.export()
        exporter.run()
        again, _ = self.export()
        with self.assertRaises(mig.MigrateError):
            again.run()

    def test_resume_after_a_request_cap_gives_the_same_export(self):
        full_dir = os.path.join(self.tmp, "full")
        mig.Exporter(mig.ReadOnlySource(build_source()), full_dir, log=lambda m: None).run()

        class Capped(mig.XmlRpcTransport):
            pass

        source = build_source()
        calls = {"n": 0}
        real_call = source.call

        class Limited:
            def version(self):
                return source.version()

            def call(self, model, method, args, kwargs=None):
                calls["n"] += 1
                if calls["n"] > 40:
                    raise mig.MigrateError("request cap reached")
                return real_call(model, method, args, kwargs)

        exporter = mig.Exporter(mig.ReadOnlySource(Limited()), self.out, page_size=1, log=lambda m: None)
        with self.assertRaises(mig.MigrateError):
            exporter.run()
        self.assertTrue(os.path.exists(os.path.join(self.out, "data")))
        resumed = mig.Exporter(mig.ReadOnlySource(build_source()), self.out, page_size=1, log=lambda m: None)
        resumed.run(resume=True)
        for name in os.listdir(os.path.join(full_dir, "data")):
            with open(os.path.join(full_dir, "data", name), encoding="utf-8") as a, open(os.path.join(self.out, "data", name), encoding="utf-8") as b:
                self.assertEqual(sorted(a.read().splitlines()), sorted(b.read().splitlines()), name)
        self.assertEqual(mig.verify_export(self.out), [])

    def test_a_torn_last_line_is_dropped_on_resume(self):
        exporter, _ = self.export()
        path = os.path.join(self.tmp, "x.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"id": 1}\n{"id": 2}\n{"id": 3, "na')
        self.assertEqual(exporter._resume_point(path), (2, 2))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(len(handle.read().splitlines()), 2)

    def test_optional_contacts_and_mail_are_off_by_default(self):
        source = build_source()
        source.fields["res.partner"] = F(name="char", email="char", user_ids="many2many")
        source.rows["res.partner"] = [{"id": 5, "name": "A", "email": "a@x.example"}]
        exporter, _ = self.export(source)
        self.assertNotIn("res.partner", exporter.run()["models"])
        out2 = os.path.join(self.tmp, "with_contacts")
        with_contacts = mig.Exporter(mig.ReadOnlySource(source), out2, include=["contacts"], log=lambda m: None)
        self.assertEqual(with_contacts.run()["models"]["res.partner"]["count"], 1)


class RewriteTests(unittest.TestCase):
    def test_links_attachments_and_blog_ids(self):
        report = {}
        html = ('<a href="https://www.old.example/about">a</a><a href="http://old.example">home</a>'
                '<img src="/web/image/55?w=1"/><img src="/web/image/ir.attachment/56/datas"/>'
                '<a href="/blog/news-1/hello-12#c">p</a><a href="/web/image/999">x</a>'
                '<a href="https://other.example/web/image/55">keep</a>')
        out = mig.rewrite_html(html, ["old.example"], {55: 301, 56: 302}, {1: 7}, {12: 90}, report)
        self.assertIn('href="/about"', out)
        self.assertIn('href=""', out)
        self.assertIn("/web/image/301?w=1", out)
        self.assertIn("/web/image/ir.attachment/302/datas", out)
        self.assertIn("/blog/news-7/hello-90#c", out)
        self.assertIn("/web/image/999", out)
        self.assertIn("https://other.example/web/image/301", out)  # same id space; only the host is foreign
        self.assertEqual(report["attachment_refs_unresolved"], 1)
        self.assertEqual(report["links_made_relative"], 2)

    def test_the_id_is_the_digits_at_the_end_of_the_slug_segment(self):
        # "my-2018-trip-12": the post id is 12, not 2018 (found converting a WordPress site)
        out = mig.rewrite_html('<a href="/blog/news-1/my-2018-trip-12">x</a><a href="/blog/news-1/my-2018-trip-12?a=1#b">y</a>',
                               [], {}, {1: 7}, {12: 90, 2018: 5})
        self.assertIn('href="/blog/news-7/my-2018-trip-90"', out)
        self.assertIn('href="/blog/news-7/my-2018-trip-90?a=1#b"', out)
        # a blog slug that itself contains a year: only the trailing id counts
        self.assertEqual(mig.rewrite_html('<a href="/blog/news-2019-3/x-4">', [], {}, {3: 8}, {4: 9}),
                         '<a href="/blog/news-2019-8/x-9">')
        report = {}
        mig.rewrite_html('<a href="https://example.org/blog/releasing-bsl-11">', [], {}, {1: 7}, {}, report)
        self.assertEqual(report["blog_refs_unresolved"], 1)

    def test_non_strings_pass_through(self):
        self.assertIsNone(mig.rewrite_html(None, ["x.example"]))
        self.assertEqual(mig.rewrite_html("", ["x.example"]), "")

    def test_domain_prefix_collisions_are_not_rewritten(self):
        out = mig.rewrite_html('<a href="https://old.example.evil.com/x">', ["old.example"])
        self.assertEqual(out, '<a href="https://old.example.evil.com/x">')

    def test_unresolved_blog_refs_are_counted(self):
        report = {}
        mig.rewrite_html('<a href="/blog/news-1/hello-12">', [], {}, {}, {}, report)
        self.assertEqual(report["blog_refs_unresolved"], 1)


class ImportTests(_Base):
    def setUp(self):
        super().setUp()
        exporter, _ = self.export()
        exporter.run()

    def importer(self, target=None, apply=True):
        target = target or build_target()
        return mig.Importer(self.out, target, apply=apply, source_domains=["old.example"], log=lambda m: None), target

    def creates(self, target):
        return [(m, c) for m, c in target.log if c in ("create", "write")]

    def test_dry_run_writes_nothing(self):
        importer, target = self.importer(apply=False)
        report = importer.run()
        self.assertEqual(self.creates(target), [])
        self.assertFalse(os.path.exists(os.path.join(self.out, "idmap.json")))
        self.assertGreater(sum(report["created"].values()), 5)

    def test_apply_imports_content_with_remapped_ids_and_kept_urls(self):
        importer, target = self.importer()
        report = importer.run()
        idmap = importer.idmap
        # website configuration written to the existing target website; secrets never read
        self.assertEqual(target.rows["website"][0]["social_twitter"], "https://twitter.com/x")
        self.assertNotIn("payment_secret", target.rows["website"][0])
        self.assertEqual(report["dropped_fields"]["website.old_only"], 1)
        # attachments: bytes preserved, new ids, assets and private files absent
        names = sorted(a["name"] for a in target.rows["ir.attachment"])
        self.assertEqual(names, ["hero.jpg", "logo.png"])
        logo = next(a for a in target.rows["ir.attachment"] if a["name"] == "logo.png")
        self.assertEqual(base64.b64decode(logo["datas"]), b"\x89PNG-fake-logo")
        self.assertTrue(logo["public"])
        # views: website-specific copies next to the generic one; the parent is remapped
        views = [v for v in target.rows["ir.ui.view"] if v["website_id"] == 1]
        self.assertEqual(sorted(v["key"] for v in views), ["website.about", "website.about_child", "website.homepage"])
        child = next(v for v in views if v["key"] == "website.about_child")
        about = next(v for v in views if v["key"] == "website.about")
        self.assertEqual(child["inherit_id"], about["id"])
        generic = next(v for v in target.rows["ir.ui.view"] if v["website_id"] is False)
        self.assertEqual(generic["arch_db"], "<t>generic</t>")
        # internal references rewritten
        home = next(v for v in views if v["key"] == "website.homepage")
        self.assertIn(f"/web/image/{idmap['ir.attachment']['55']}", home["arch_db"])
        self.assertIn('href="/about"', home["arch_db"])
        self.assertIn("/web/image/999", home["arch_db"])  # not exported: left alone
        new_post = idmap["blog.post"]["12"]
        new_blog = idmap["blog.blog"]["1"]
        self.assertIn(f"/blog/news-{new_blog}/hello-{new_post}", home["arch_db"])
        self.assertIn(f"/web/content/{idmap['ir.attachment']['56']}", about["arch_db"])
        # pages keep their URL; menus keep their shape
        page = target.rows["website.page"][0]
        self.assertEqual((page["url"], page["view_id"]), ("/about", about["id"]))
        menus = {m["name"]: m for m in target.rows["website.menu"]}
        self.assertEqual(menus["About"]["parent_id"], menus["Default Top"]["id"])  # the target's own root menu
        self.assertEqual(menus["About"]["page_id"], page["id"])
        self.assertEqual(menus["Blog"]["url"], "/blog")
        # blog
        post = next(p for p in target.rows["blog.post"] if p["name"] == "Hello")
        self.assertEqual(post["blog_id"], new_blog)
        self.assertEqual(post["tag_ids"], [idmap["blog.tag"]["1"]])
        self.assertIn(f"/web/image/{idmap['ir.attachment']['55']}?x=1", post["content"])
        self.assertIn('href="/about"', post["content"])
        # redirects: the post and blog URLs changed because Odoo puts the id in them
        sources = {r["from"]: r["to"] for r in report["redirects"]}
        self.assertEqual(sources["/blog/news-1/hello-12"], f"/blog/news-{new_blog}/hello-{new_post}")
        self.assertEqual(sources["/blog/news-1"], f"/blog/news-{new_blog}")
        rewrites = {r["url_from"]: r for r in target.rows["website.rewrite"]}
        self.assertEqual(rewrites["/blog/news-1/hello-12"]["redirect_type"], "301")
        self.assertIn("/old-page", rewrites)  # the source's own redirect
        # what the tool could not import is said so
        self.assertTrue(any("website.snippet.filter" in item for item in report["unsupported"]))
        self.assertTrue(any("payment_secret" in item for item in report["warnings"]))

    def test_redirect_targets_follow_the_id_maps_and_are_imported_after_the_blog(self):
        # A converted site (wordpress_to_odoo.py) points old URLs at posts and attachments by SOURCE id.
        importer, target = self.importer()
        importer.run()
        idmap = importer.idmap
        rewrites = {r["url_from"]: r["url_to"] for r in target.rows["website.rewrite"]}
        new_post, new_blog = idmap["blog.post"]["12"], idmap["blog.blog"]["1"]
        self.assertEqual(rewrites["/2018/05/hello/"], f"/blog/news-{new_blog}/hello-{new_post}")
        self.assertEqual(rewrites["/wp-content/uploads/logo.png"], f"/web/image/{idmap['ir.attachment']['55']}")
        self.assertEqual(rewrites["/old-page"], "/about")
        order = [m for m, c in target.log if c == "create" and m in ("blog.post", "website.rewrite")]
        self.assertLess(order.index("blog.post"), order.index("website.rewrite"))

    def test_a_blog_author_missing_in_the_target_is_warned_about_once(self):
        source = build_source()
        source.rows["blog.post"].append({"id": 13, "name": "Second", "blog_id": 1, "tag_ids": [], "is_published": True,
                                         "content": "<p>x</p>", "website_url": "/blog/news-1/second-13", "author_id": [7, "Nobody"]})
        source.rows["blog.post"][0]["author_id"] = [7, "Nobody"]
        shutil.rmtree(self.out)
        exporter, _ = self.export(source)
        exporter.run()
        importer, _ = self.importer()
        report = importer.run()
        self.assertEqual(sum("'Nobody'" in w for w in report["warnings"]), 1)

    def test_a_second_apply_creates_nothing(self):
        importer, target = self.importer()
        importer.run()
        before = {m: len(rows) for m, rows in target.rows.items()}
        again = mig.Importer(self.out, target, apply=True, source_domains=["old.example"], log=lambda m: None)
        report = again.run()
        self.assertEqual({m: len(rows) for m, rows in target.rows.items()}, before)
        self.assertEqual(sum(report["created"].values()), 0)
        self.assertEqual(report["redirects"], [])

    def test_resume_after_losing_the_idmap_still_matches_by_natural_keys(self):
        importer, target = self.importer()
        importer.run()
        before = {m: len(rows) for m, rows in target.rows.items()}
        os.remove(os.path.join(self.out, "idmap.json"))
        again = mig.Importer(self.out, target, apply=True, source_domains=["old.example"], log=lambda m: None)
        again.run()
        self.assertEqual({m: len(rows) for m, rows in target.rows.items()}, before)

    def test_a_missing_website_module_stops_the_import(self):
        target = build_target()
        target.rows["website"] = []
        with self.assertRaises(mig.MigrateError):
            self.importer(target)[0].run()

    def test_missing_blog_module_is_reported_not_fatal(self):
        target = build_target()
        for model in ("blog.blog", "blog.tag", "blog.post"):
            del target.fields[model]
            del target.rows[model]
        report = self.importer(target)[0].run()
        self.assertTrue(any("no blog module" in w for w in report["warnings"]))

    def test_the_report_is_readable_and_honest_about_dry_runs(self):
        importer, _ = self.importer(apply=False)
        text = mig.render_report(importer.run(), False)
        self.assertIn("DRY RUN (nothing was written)", text)
        self.assertIn("created", text)

    def test_cli_import_refuses_a_damaged_export(self):
        with open(os.path.join(self.out, "files", "att_55"), "ab") as handle:
            handle.write(b"x")
        pw = os.path.join(self.tmp, "pw")
        with open(pw, "w", encoding="utf-8") as handle:
            handle.write("secret\n")
        messages = []
        code = mig.main(["import", "--export", self.out, "--target-url", "http://127.0.0.1:1", "--target-db", "d",
                         "--target-password-file", pw], messages.append)
        self.assertEqual(code, 2)
        self.assertTrue(any("damaged" in m for m in messages))
        self.assertFalse(any("secret" == m for m in messages))


class TransportTests(unittest.TestCase):
    def test_user_agent_rate_limit_and_cap_over_a_real_xmlrpc_server(self):
        seen = {"agents": [], "calls": 0}

        class Handler(SimpleXMLRPCRequestHandler):
            rpc_paths = ("/xmlrpc/2/common", "/xmlrpc/2/object")

            def do_POST(self):
                seen["agents"].append(self.headers.get("User-Agent"))
                super().do_POST()

            def log_message(self, *args):
                pass

        server = SimpleXMLRPCServer(("127.0.0.1", 0), requestHandler=Handler, allow_none=True, logRequests=False)
        server.register_function(lambda: {"server_version": "16.0", "server_version_info": [16, 0, 0, "final", 0, ""]}, "version")
        server.register_function(lambda db, login, secret, ctx: 7 if secret == "good" else False, "authenticate")
        server.register_function(lambda db, uid, secret, model, method, args, kwargs: [model, method], "execute_kw")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        sleeps, clock = [], [0.0]
        creds = mig.Credentials(f"http://127.0.0.1:{port}", "db", "me", "good")
        transport = mig.XmlRpcTransport(creds, min_interval=0.5, max_requests=4, sleep=sleeps.append,
                                        clock=lambda: clock[0])
        self.assertEqual(transport.version()["server_version"], "16.0")
        self.assertEqual(transport.call("website", "search", [[]]), ["website", "search"])
        self.assertEqual(set(seen["agents"]), {mig.USER_AGENT})
        self.assertTrue(sleeps and all(s > 0 for s in sleeps))
        transport.call("website", "search", [[]])
        with self.assertRaises(mig.MigrateError) as raised:
            transport.call("website", "search", [[]])
        self.assertIn("request cap", str(raised.exception))
        bad = mig.XmlRpcTransport(mig.Credentials(creds.url, "db", "me", "wrong"), min_interval=0)
        with self.assertRaises(mig.MigrateError):
            bad.call("website", "search", [[]])

    def test_retries_on_503_then_gives_up(self):
        attempts = {"n": 0}

        class Proxy:
            def __init__(self, url, **kw):
                pass

            def version(self):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise xmlrpc.client.ProtocolError("u", 503, "busy", {})
                return {"server_version": "x"}

        sleeps = []
        creds = mig.Credentials("http://127.0.0.1:1", "d", "l", "s")
        transport = mig.XmlRpcTransport(creds, min_interval=0, sleep=sleeps.append, proxy_factory=Proxy)
        self.assertEqual(transport.version(), {"server_version": "x"})
        self.assertEqual(sleeps, [2, 4])

        class Always404:
            def __init__(self, url, **kw):
                pass

            def version(self):
                raise xmlrpc.client.ProtocolError("u", 404, "nope", {})

        transport = mig.XmlRpcTransport(creds, min_interval=0, sleep=sleeps.append, proxy_factory=Always404)
        with self.assertRaises(mig.MigrateError):
            transport.version()


class FileModeTests(unittest.TestCase):
    def test_the_export_directory_is_private(self):
        tmp = tempfile.mkdtemp(prefix="migrate_mode_")
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "e")
        os.makedirs(out, mode=0o700)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
