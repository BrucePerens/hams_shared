#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for tenant_site_harden.py against an in-memory Odoo (no network, no real site)."""

# [@ANCHOR: test_tenant_site_harden:hardener]
# Tests [@ANCHOR: tenant_site_harden:hardener]
# [@ANCHOR: test_tenant_site_harden:run_checks]
# Tests [@ANCHOR: tenant_site_harden:run_checks]

import base64
import copy
import unittest

import tenant_site_harden as harden


class FakeOdoo:
    """Just enough ORM: search_read/search/search_count with =, in, != domains, write, create, unlink, fields_get."""

    def __init__(self):
        self.log = []
        self.rows = {
            "ir.config_parameter": [{"id": 1, "key": "auth_signup.invitation_scope", "value": "b2c"},
                                    {"id": 2, "key": "auth_signup.reset_password", "value": "True"}],
            "ir.cron": [{"id": 1, "cron_name": "Base: Auto-vacuum internal data", "active": True},
                        {"id": 2, "cron_name": "Mail: Email Queue Manager", "active": True},
                        {"id": 3, "cron_name": "Digest Emails", "active": True},
                        {"id": 4, "cron_name": "Publisher: Update Notification", "active": True},
                        {"id": 5, "cron_name": "Notification: Delete Notifications older than 6 Months", "active": True}],
            "website.page": [{"id": 1, "url": "/contactus", "is_published": True},
                             {"id": 2, "url": "/contactus-thank-you", "is_published": True},
                             {"id": 3, "url": "/about", "is_published": True}],
            "website.menu": [{"id": 1, "name": "Home", "url": "/"}, {"id": 2, "name": "Contact us", "url": "/contactus"},
                             {"id": 3, "name": "Contact us", "url": "/contactus"}],
            "ir.model": [{"id": 1, "model": "mail.mail", "website_form_access": True},
                         {"id": 2, "model": "blog.blog", "website_form_access": False}],
            "website": [{"id": 1, "name": "My Website", "logo": base64.b64encode(b"<svg/>").decode()}],
            "blog.blog": [{"id": 1, "name": "Our blog"}],
            "res.users": [{"id": 2, "login": "admin", "partner_id": [3, "Administrator"], "active": True}],
            "res.partner": [{"id": 3, "name": "Administrator", "active": True}],
            "ir.ui.view": [{"id": 1, "key": "website.header_call_to_action", "active": True, "arch_db": ""},
                           {"id": 2, "key": "website.footer_custom", "active": True, "arch_db": ""},
                           {"id": 3, "key": "website.footer_copyright_company_name", "active": True,
                            "arch_db": "<span>Copyright &amp;copy; Company name</span>"}],
            "res.company": [{"id": 1, "name": "My Company"}],
            "ir.mail_server": [{"id": 1, "active": True}], "mail.template": [{"id": 1, "active": True}], "mail.mail": [],
        }

    @staticmethod
    def matches(row, domain):
        for field, op, value in domain:
            have = row.get(field)
            if op == "=" and have != value:
                return False
            if op == "!=" and have == value:
                return False
            if op == "in" and have not in value:
                return False
        return True

    def call(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        self.log.append((model, method))
        if model == "ir.model" and method == "fields_get":
            return {"website_form_access": {"type": "boolean"}}
        if model == "website" and method == "fields_get":
            return {"name": {"type": "char"}}
        if model == "ir.model" and method == "search_count" and args[0][0][0] == "model":
            return 1 if args[0][0][2] in ("blog.blog", "mail.mail", "ir.mail_server", "mail.template") else 0
        if model == "ir.model.data":
            return []
        rows = self.rows.setdefault(model, [])
        if method in ("search_read", "search", "search_count"):
            domain = args[0] if args else []
            found = [r for r in rows if self.matches(r, domain)]
            if method == "search_count":
                return len(found)
            if method == "search":
                return [r["id"] for r in found]
            limit = kwargs.get("limit")
            found = found[:limit] if limit else found
            fields = kwargs.get("fields")
            return [{k: copy.deepcopy(v) for k, v in r.items() if not fields or k in fields or k == "id"} for r in found]
        if method == "read":
            return [copy.deepcopy(r) for r in rows if r["id"] in args[0]]
        if method == "write":
            for row in rows:
                if row["id"] in args[0]:
                    row.update(args[1])
            return True
        if method == "create":
            new = dict(args[0], id=max([r["id"] for r in rows] + [0]) + 1)
            rows.append(new)
            return new["id"]
        if method == "unlink":
            self.rows[model] = [r for r in rows if r["id"] not in args[0]]
            return True
        raise AssertionError(f"unexpected call {model}.{method}")


class HardenerTests(unittest.TestCase):
    def run_it(self, odoo, write=True):
        return harden.Hardener(odoo, write, lambda *_: None).run("Bruce Perens", "Bruce Perens' Blog", "Bruce Perens")

    def test_a_dry_run_changes_nothing_and_lists_every_step(self):
        odoo = FakeOdoo()
        before = copy.deepcopy(odoo.rows)
        changes = self.run_it(odoo, write=False)
        self.assertEqual(odoo.rows, before)
        self.assertGreaterEqual(len(changes), 12)
        self.assertFalse([c for c in odoo.log if c[1] in ("write", "create", "unlink")])

    def test_apply_closes_signup_mail_and_the_contact_form_and_cleans_the_theme(self):
        odoo = FakeOdoo()
        self.run_it(odoo)
        params = {r["key"]: r["value"] for r in odoo.rows["ir.config_parameter"]}
        self.assertEqual(params["auth_signup.invitation_scope"], "b2b")
        self.assertEqual(params["auth_signup.reset_password"], "False")
        self.assertFalse(odoo.rows["ir.mail_server"][0]["active"])
        self.assertFalse(odoo.rows["mail.template"][0]["active"])
        active = {r["cron_name"] for r in odoo.rows["ir.cron"] if r["active"]}
        self.assertEqual(active, {"Base: Auto-vacuum internal data",
                                  "Notification: Delete Notifications older than 6 Months"})
        self.assertFalse([p for p in odoo.rows["website.page"] if p["url"].startswith("/contactus") and p["is_published"]])
        self.assertEqual([m["name"] for m in odoo.rows["website.menu"]], ["Home"])
        self.assertFalse(odoo.rows["ir.model"][0]["website_form_access"])
        views = {v["key"]: v for v in odoo.rows["ir.ui.view"]}
        self.assertFalse(views["website.header_call_to_action"]["active"])
        self.assertFalse(views["website.footer_custom"]["active"])
        self.assertIn("Copyright &amp;copy; Bruce Perens", views["website.footer_copyright_company_name"]["arch_db"])
        self.assertEqual(odoo.rows["blog.blog"][0]["name"], "Bruce Perens' Blog")
        self.assertEqual(odoo.rows["res.partner"][0]["name"], "Bruce Perens")
        self.assertEqual(odoo.rows["res.company"][0]["name"], "Bruce Perens")
        logo = base64.b64decode(odoo.rows["website"][0]["logo"]).decode()
        self.assertIn(">Bruce Perens</text>", logo)

    def test_a_second_run_changes_nothing(self):
        odoo = FakeOdoo()
        self.run_it(odoo)
        self.assertEqual(self.run_it(odoo), [])

    def test_the_site_name_cannot_inject_markup_into_the_footer_or_logo(self):
        odoo = FakeOdoo()
        harden.Hardener(odoo, True, lambda *_: None).run('A <b>&"x', None, None)
        footer = next(v for v in odoo.rows["ir.ui.view"] if v["key"] == "website.footer_copyright_company_name")["arch_db"]
        self.assertNotIn("<b>", footer)
        logo = base64.b64decode(odoo.rows["website"][0]["logo"]).decode()
        self.assertNotIn("<b>", logo)
        self.assertIn("&lt;b&gt;", logo)

    def test_a_blog_that_was_already_renamed_or_has_company_is_left_alone(self):
        odoo = FakeOdoo()
        odoo.rows["blog.blog"] = [{"id": 1, "name": "Mine"}, {"id": 2, "name": "Our blog"}]
        self.run_it(odoo)
        self.assertEqual([b["name"] for b in odoo.rows["blog.blog"]], ["Mine", "Our blog"])


class FakeHttp:
    """What a hardened site answers; `leaky` makes the contact form and signup work."""

    def __init__(self, leaky=False):
        self.leaky = leaky
        self.requests = []

    def __call__(self, url, method="GET", data=None, headers=None):
        self.requests.append((method, url))
        path = url.split("127.0.0.1:1", 1)[-1].split("/", 1)[-1]
        if path.startswith("web/login"):
            return 200, '<form class="oe_login_form"><input name="csrf_token" value="tok123"></form>', {}
        if self.leaky and path in ("web/signup", "contactus"):
            return 200, '<form action="/web/signup"><input type="password"></form>', {}
        return 404, "not found", {}


class ChecksTests(unittest.TestCase):
    def hardened(self):
        odoo = FakeOdoo()
        harden.Hardener(odoo, True, lambda *_: None).run("S", None, None)
        return odoo

    def test_a_hardened_site_passes_every_check_and_the_probes_carry_a_csrf_token(self):
        odoo, http = self.hardened(), FakeHttp()
        failures = harden.run_checks(odoo, "http://127.0.0.1:18101", lambda *_: None, fetch=http)
        self.assertEqual(failures, [])
        self.assertIn(("GET", "http://127.0.0.1:18101/web/login"), http.requests)
        self.assertTrue([r for r in http.requests if r[0] == "POST"])

    def test_a_stock_site_fails_the_configuration_checks(self):
        failures = harden.run_checks(FakeOdoo(), "http://127.0.0.1:18101", lambda *_: None, fetch=FakeHttp())
        joined = " ".join(failures)
        for expected in ("invitation only", "password reset", "scheduled action", "public form", "/contactus"):
            self.assertIn(expected, joined)

    def test_a_site_that_serves_the_signup_or_contact_form_fails_even_when_configured(self):
        failures = harden.run_checks(self.hardened(), "http://127.0.0.1:18101", lambda *_: None, fetch=FakeHttp(leaky=True))
        self.assertTrue([f for f in failures if "/web/signup" in f or "/contactus" in f])

    def test_a_probe_that_creates_a_record_fails_the_check(self):
        odoo = self.hardened()

        class Creating(FakeHttp):
            def __call__(self, url, method="GET", data=None, headers=None):
                if method == "POST":
                    odoo.rows["mail.mail"].append({"id": 1})
                return super().__call__(url, method, data, headers)

        failures = harden.run_checks(odoo, "http://127.0.0.1:18101", lambda *_: None, fetch=Creating())
        self.assertIn("no mail.mail record was created by the probes", failures)

    def test_main_exit_codes_and_the_password_never_appears_in_output(self):
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            pw = os.path.join(tmp, "pw")
            with open(pw, "w", encoding="utf-8") as handle:
                handle.write("s3cret-pw-value\n")
            lines = []
            with mock.patch.object(harden, "connect", return_value=FakeOdoo()):
                code = harden.main(["apply", "--url", "http://127.0.0.1:1", "--db", "d", "--password-file", pw], out=lines.append)
            self.assertEqual(code, 0)
            self.assertIn("pending (dry run", lines[-1])
            self.assertNotIn("s3cret-pw-value", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
