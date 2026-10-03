#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests [@ANCHOR: parking_ctl:parse_domain_file] [@ANCHOR: parking_ctl:diff] for parking_ctl.py."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import parking_ctl as ctl


class FakeClient:
    def __init__(self, rows=None):
        self.rows = {r["name"]: dict(r) for r in (rows or [])}
        self.created = []
        self.written = []

    def search_read(self, domain, fields):
        return list(self.rows.values())

    def create(self, vals_list):
        self.created.extend(vals_list)
        for vals in vals_list:
            self.rows[vals["name"]] = dict(vals, id=len(self.rows) + 1, active=True)

    def write(self, ids, vals):
        self.written.append((ids, vals))


def row(name, **kw):
    base = ctl.default_values()
    base.update(kw)
    return dict(base, name=name, id=7, active=True)


class ParseTests(unittest.TestCase):
    def parse(self, text, **kw):
        return ctl.parse_domain_file(text, **kw)

    def test_plain_list_with_comments_and_blank_lines(self):
        entries, problems = self.parse("# c\n\nExample.COM  # trailing\nbücher.example\n")
        self.assertEqual(problems, [])
        self.assertEqual(sorted(entries), ["example.com", "xn--bcher-kva.example"])
        self.assertEqual(entries["example.com"]["behavior"], "parked")
        self.assertTrue(entries["example.com"]["noindex"])

    def test_per_line_options_override_defaults(self):
        text = 'a.example behavior=redirect url=https://t.example/x code=302 preserve_path=1\n' \
               'b.example title="For sale now" price="$5,000" behavior=for_sale noindex=0 ttl=60 www=0\n'
        entries, problems = self.parse(text)
        self.assertEqual(problems, [])
        self.assertEqual(entries["a.example"]["redirect_code"], "302")
        self.assertTrue(entries["a.example"]["preserve_path"])
        self.assertEqual(entries["b.example"]["title"], "For sale now")
        self.assertFalse(entries["b.example"]["noindex"])
        self.assertFalse(entries["b.example"]["include_www"])
        self.assertEqual(entries["b.example"]["cache_ttl"], 60)

    def test_every_problem_is_reported_with_its_line(self):
        text = "\n".join([
            "good.example",
            "hams.com",
            "x.hams.com",
            "localhost",
            "10.1.2.3",
            "dup.example",
            "dup.example",
            "r.example behavior=redirect",
            "s.example behavior=redirect url=javascript:alert(1)",
            "t.example behavior=redirect url=https://t.example/",
            "u.example behavior=redirect url=https://user:pw@v.example/",
            "v.example behavior=nonsense",
            "w.example surprise=1",
            "y.example ttl=abc",
            'z.example title="unterminated',
            "tenant.example",
        ])
        entries, problems = self.parse(text, reserved={"tenant.example"})
        self.assertEqual(list(entries), ["good.example", "dup.example"])
        self.assertEqual(len(problems), 14)
        joined = "\n".join(problems)
        for needle in ("line 2", "line 3", "duplicate domain dup.example", "redirect needs url",
                       "No closing quotation", "Odoo tenant", "itself", "credentials"):
            self.assertIn(needle, joined)

    def test_defaults_from_the_command_line_apply(self):
        entries, problems = self.parse("a.example\nb.example behavior=gone\n",
                                       defaults={"behavior": "redirect", "redirect_url": "https://t.example/"})
        self.assertEqual(problems, [])
        self.assertEqual(entries["a.example"]["behavior"], "redirect")
        self.assertEqual(entries["b.example"]["behavior"], "gone")


class DiffTests(unittest.TestCase):
    def test_create_update_same(self):
        entries, _ = ctl.parse_domain_file("new.example\nsame.example\nchanged.example behavior=gone\n")
        existing = {"same.example": row("same.example"), "changed.example": row("changed.example")}
        create, update, same = ctl.diff(entries, existing)
        self.assertEqual([d for d, _ in create], ["new.example"])
        self.assertEqual(same, ["same.example"])
        self.assertEqual(update[0][1:], ("changed.example", {"behavior": "gone"}))

    def test_an_archived_record_is_reactivated(self):
        entries, _ = ctl.parse_domain_file("old.example\n")
        existing = {"old.example": dict(row("old.example"), active=False)}
        _create, update, _same = ctl.diff(entries, existing)
        self.assertEqual(update[0][2], {"active": True})

    def test_false_and_empty_compare_equal(self):
        entries, _ = ctl.parse_domain_file("a.example\n")
        existing = {"a.example": row("a.example", title=False, message="")}
        self.assertEqual(ctl.diff(entries, existing)[1], [])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="parking_ctl_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = os.path.join(self.tmp, "domains.txt")
        self.specs = os.path.join(self.tmp, "specs")
        os.makedirs(self.specs)
        with open(os.path.join(self.specs, "perens_com.json"), "w", encoding="utf-8") as handle:
            json.dump({"name": "perens_com", "domains": ["perens.com"], "http_port": 18101}, handle)

    def write(self, text):
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write(text)

    def run_ctl(self, argv, client=None, api=None):
        out = []
        base = ["--spec-dir", self.specs]
        patches = [mock.patch.object(ctl, "connect", return_value=client or FakeClient())]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        code = ctl.main(base + argv, out.append) if api is None else None
        return code, out

    def test_validate_reports_tenant_conflicts(self):
        self.write("park.example\nperens.com\n")
        code, out = self.run_ctl(["validate", self.file])
        self.assertEqual(code, 1)
        self.assertTrue(any("Odoo tenant" in line for line in out))

    def test_apply_is_a_dry_run_by_default(self):
        self.write("a.example\nb.example\n")
        client = FakeClient()
        code, out = self.run_ctl(["apply", self.file], client)
        self.assertEqual(code, 0)
        self.assertEqual(client.created, [])
        self.assertIn("DRY RUN: 2 create", out[0])

    def test_apply_creates_in_batches_and_a_second_run_changes_nothing(self):
        self.write("\n".join(f"d{n}.example" for n in range(450)) + "\n")
        client = FakeClient()
        self.run_ctl(["apply", self.file, "--apply"], client)
        self.assertEqual(len(client.created), 450)
        self.assertTrue(all(v["behavior"] == "parked" and v["noindex"] for v in client.created))
        client.created.clear()
        code, out = self.run_ctl(["apply", self.file, "--apply"], client)
        self.assertEqual(client.created, [])
        self.assertEqual(client.written, [])
        self.assertIn("0 create, 0 update, 450 unchanged", out[0])

    def test_a_file_with_problems_is_never_applied(self):
        self.write("ok.example\nbad domain\n")
        client = FakeClient()
        code, out = self.run_ctl(["apply", self.file, "--apply"], client)
        self.assertEqual(code, 1)
        self.assertEqual(client.created, [])
        self.assertTrue(any("refusing" in line for line in out))

    def test_command_line_defaults_make_a_bulk_redirect(self):
        self.write("a.example\nb.example\n")
        client = FakeClient()
        self.run_ctl(["apply", self.file, "--apply", "--behavior", "redirect", "--redirect-url",
                      "https://main.example/", "--preserve-path"], client)
        self.assertTrue(all(v["behavior"] == "redirect" and v["preserve_path"] for v in client.created))

    def test_archive_deactivates_without_deleting(self):
        self.write("a.example\nmissing.example\n")
        client = FakeClient([row("a.example")])
        code, out = self.run_ctl(["archive", self.file], client)
        self.assertEqual(client.written, [])
        self.run_ctl(["archive", self.file, "--apply"], client)
        self.assertEqual(client.written, [([7], {"active": False})])

    def test_plan_lists_cloudflare_steps_and_compares_records(self):
        self.write("a.example\n")
        code, out = self.run_ctl(["plan", self.file, "--tunnel-id", "abc"], FakeClient())
        text = "\n".join(out)
        self.assertIn("1 to create", text)
        self.assertIn("a.example -> abc.cfargotunnel.com", text)
        self.assertIn("www.a.example -> abc.cfargotunnel.com", text)
        self.assertIn("100 free", text)
        self.assertIn("same account", text)

    def test_dns_records_include_www_unless_disabled_and_skip_a_www_name(self):
        entries, _ = ctl.parse_domain_file("a.example\nb.example www=0\nwww.c.example\n")
        names = [r["name"] for r in ctl.dns_records(entries, "t")]
        self.assertEqual(names, ["a.example", "www.a.example", "b.example", "www.c.example"])

    def test_dns_command_never_changes_existing_records_and_reports_foreign_zones(self):
        self.write("mine.example\nforeign.example\n")
        responses = {
            "/zones?name=mine.example": [{"id": "z1"}],
            "/zones/z1/dns_records?name=mine.example": [{"type": "A", "content": "1.2.3.4"}],
            "/zones/z1/dns_records?name=www.mine.example": [],
        }
        calls = []

        class Api:
            def call(self, method, path, body=None):
                calls.append((method, path))
                return responses.get(path, [])

        out = []
        args = ctl.build_parser().parse_args(["--spec-dir", self.specs, "dns", self.file, "--tunnel-id", "t", "--apply"])
        code = ctl.cmd_dns(args, out.append, api=Api())
        self.assertEqual(code, 2)
        self.assertIn(("POST", "/zones/z1/dns_records"), calls)  # www.mine.example only
        self.assertEqual([c for c in calls if c[0] == "POST"], [("POST", "/zones/z1/dns_records")])
        text = "\n".join(out)
        self.assertIn("CONFLICT, not touched: mine.example", text)
        self.assertIn("NOT IN THIS ACCOUNT: foreign.example", text)

    def test_export_and_report(self):
        client = FakeClient([row("a.example"), row("b.example", behavior="gone")])
        code, out = self.run_ctl(["report"], client)
        self.assertIn("2 domain(s)", out[0])
        code, out = self.run_ctl(["export"], client)
        self.assertEqual([r["name"] for r in json.loads(out[0])], ["a.example", "b.example"])

    def test_the_password_file_is_read_by_the_tool_and_never_printed(self):
        path = os.path.join(self.tmp, "pw")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("S3CRET-PASSWORD\n")
        seen = {}

        class Proxy:
            def __init__(self, url, allow_none=False):
                seen.setdefault("urls", []).append(url)

            def authenticate(self, db, login, password, ctx):
                seen["password"] = password
                return 5

            def execute_kw(self, *args):
                return []

        args = ctl.build_parser().parse_args(["--password-file", path, "report"])
        with mock.patch.object(ctl.xmlrpc.client, "ServerProxy", Proxy):
            client = ctl.connect(args)
        self.assertEqual(seen["password"], "S3CRET-PASSWORD")
        self.assertNotIn("S3CRET-PASSWORD", " ".join(seen["urls"]))
        self.assertIsNotNone(client)


if __name__ == "__main__":
    unittest.main()
