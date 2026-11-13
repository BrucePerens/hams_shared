#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_boundary_hazards.py, exercised against real temp-directory fixtures: every rule has a case that
must be flagged and a case that must stay silent, and the known list and the ignore comment are exercised end to end
through main().
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_boundary_hazards as chk  # noqa: E402


def _write(directory, name, content):
    path = os.path.join(directory, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(textwrap.dedent(content))
    return path


class RuleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rules(self, content, name="main.py"):
        return [f[0] for f in chk.check_file(_write(self.tmp, name, content))]

    def findings(self, content, name="main.py"):
        return chk.check_file(_write(self.tmp, name, content))


class FindFilesTests(RuleTestCase):
    # Tests [@ANCHOR: boundary_hazards:find_files]
    def test_vendored_virtualenvs_are_skipped_whatever_their_outer_name(self):
        _write(self.tmp, "whisper_venv/lib/python3.13/site-packages/numpy/x.py", "x = 1\n")
        _write(self.tmp, "real/x.py", "x = 1\n")
        _write(self.tmp, "real/run.sh", "echo hi\n")
        rels = sorted(os.path.relpath(p, self.tmp) for p in chk.find_files(self.tmp))
        self.assertEqual(rels, [os.path.join("real", "run.sh"), os.path.join("real", "x.py")])


class CredentialRedirectTests(RuleTestCase):
    # Tests [@ANCHOR: boundary_hazards:check_file]
    def test_a_credential_header_sent_with_plain_urlopen_is_flagged(self):
        found = self.findings("""
            import urllib.request
            def call(url, key):
                request = urllib.request.Request(url, headers={"X-API-Key": key})
                return urllib.request.urlopen(request).read()
        """)
        self.assertEqual([f[0] for f in found], ["credential-redirect"])
        self.assertEqual(found[0][1], "call")

    def test_add_header_and_a_subscript_store_are_found_too(self):
        self.assertEqual(self.rules("""
            import urllib.request
            def a(request):
                request.add_header("Authorization", "Bearer x")
                return urllib.request.urlopen(request)
            def b(url):
                headers = {}
                headers["authorization"] = "Bearer y"
                return urllib.request.urlopen(urllib.request.Request(url, headers=headers))
        """), ["credential-redirect", "credential-redirect"])

    def test_a_redirect_refusing_handler_in_the_file_silences_it(self):
        self.assertEqual(self.rules("""
            import urllib.request
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            _OPENER = urllib.request.build_opener(_NoRedirect)
            def call(url, key):
                return _OPENER.open(urllib.request.Request(url, headers={"X-API-Key": key}))
        """), [])

    def test_the_safe_fetch_helper_silences_it(self):
        self.assertEqual(self.rules("""
            import urllib.request
            from ssrf_safe_fetch import safe_fetch
            def call(url, key):
                return urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": key}))
        """), [])

    def test_a_file_that_does_not_use_urllib_is_not_judged(self):
        self.assertEqual(self.rules("""
            import requests
            def call(url, key):
                return requests.get(url, headers={"X-API-Key": key})
        """), [])

    def test_a_non_credential_header_is_not_flagged(self):
        self.assertEqual(self.rules("""
            import urllib.request
            def call(url):
                return urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "text/html"}))
        """), [])

    def test_the_ignore_comment_with_a_reason_silences_it(self):
        self.assertEqual(self.rules("""
            import urllib.request
            def call(url, key):
                request = urllib.request.Request(url, headers={"X-API-Key": key})  # boundary-hazard-ignore: credential-redirect: loopback only
                return urllib.request.urlopen(request).read()
        """), [])

    def test_the_ignore_comment_needs_a_reason(self):
        self.assertEqual(self.rules("""
            import urllib.request
            def call(url, key):
                request = urllib.request.Request(url, headers={"X-API-Key": key})  # boundary-hazard-ignore: credential-redirect:
                return urllib.request.urlopen(request).read()
        """), ["credential-redirect"])

    def test_test_files_are_not_judged(self):
        self.assertEqual(self.rules("""
            import urllib.request
            def call(url, key):
                return urllib.request.urlopen(urllib.request.Request(url, headers={"X-API-Key": key}))
        """, name="tests/test_call.py"), [])


class ArgvLogTests(RuleTestCase):
    def test_a_cmdline_value_in_a_log_call_is_flagged(self):
        self.assertEqual(self.rules("""
            import logging
            logger = logging.getLogger(__name__)
            def scan(proc):
                logger.warning("stray process %s", proc.info["cmdline"])
        """), ["argv-log"])

    def test_the_joined_text_of_a_cmd_list_is_flagged(self):
        self.assertEqual(self.rules("""
            import logging
            logger = logging.getLogger(__name__)
            def run(cmd):
                logger.info("Executing: %s", " ".join(cmd))
        """), ["argv-log"])

    def test_a_bare_cmd_list_and_whole_sys_argv_are_flagged(self):
        self.assertEqual(self.rules("""
            import logging, sys
            _logger = logging.getLogger(__name__)
            def run(cmd):
                _logger.error("failed: %s", cmd)
            def main():
                _logger.info("started with %s", sys.argv[1:])
        """), ["argv-log", "argv-log"])

    def test_the_program_name_alone_is_not_flagged(self):
        self.assertEqual(self.rules("""
            import logging, sys
            logger = logging.getLogger(__name__)
            def run(cmd):
                logger.info("Executing %s with %d arguments", cmd[0], len(cmd) - 1)
                logger.info("usage: %s", sys.argv[0])
        """), [])

    def test_a_print_or_a_non_logger_receiver_is_not_flagged(self):
        self.assertEqual(self.rules("""
            def run(cmd, report):
                print("Executing", cmd)
                report.info("Executing %s", cmd)
        """), [])

    def test_the_ignore_comment_with_a_reason_silences_it(self):
        self.assertEqual(self.rules("""
            import logging
            logger = logging.getLogger(__name__)
            def run(cmd):
                logger.info("Executing: %s", " ".join(cmd))  # boundary-hazard-ignore: argv-log: built from constants
        """), [])


class RpcServiceIdentityTests(RuleTestCase):
    HEADER = (
        "from odoo import api, models\n"
        "from odoo.exceptions import AccessError\n"
        "\n"
        "class Thing(models.Model):\n"
        "    _name = 'x.thing'\n"
    )

    def model(self, body):
        return self.HEADER + textwrap.indent(textwrap.dedent(body).strip("\n") + "\n", "    ")

    def test_a_public_api_model_method_acting_as_a_service_account_is_flagged(self):
        found = self.findings(self.model("""
            @api.model
            def sync_all(self):
                uid = self.env["zero_sudo.security.utils"]._get_service_uid("x.service")
                return self.with_user(uid).search([])
        """))
        self.assertEqual([f[0] for f in found], ["rpc-service-identity"])
        self.assertEqual(found[0][1], "Thing.sync_all")

    def test_sudo_and_a_service_helper_count_as_acting_with_another_identity(self):
        self.assertEqual(self.rules(self.model("""
            @api.model
            def a(self):
                return self.sudo().search([])
            @api.model
            def b(self):
                return self._net_service().search([])
        """)), ["rpc-service-identity", "rpc-service-identity"])

    def test_api_private_a_leading_underscore_and_a_plain_instance_method_are_not_flagged(self):
        self.assertEqual(self.rules(self.model("""
            @api.model
            @api.private
            def a(self):
                return self.sudo().search([])
            @api.model
            def _b(self):
                return self.sudo().search([])
            def c(self):
                return self.sudo().search([])
        """)), [])

    def test_a_caller_check_in_the_body_silences_it(self):
        self.assertEqual(self.rules(self.model("""
            @api.model
            def a(self):
                if not (self.env.su or self.env.user.has_group("base.group_system")):
                    raise AccessError("no")
                return self.sudo().search([])
            @api.model
            def b(self):
                self._require_caller()
                return self.sudo().search([])
            @api.model
            def c(self):
                self.check_access("read")
                return self.sudo().search([])
        """)), [])

    def test_a_method_that_never_changes_identity_is_not_flagged(self):
        self.assertEqual(self.rules(self.model("""
            @api.model
            def a(self):
                return self.search([])
        """)), [])

    def test_a_class_that_is_not_a_model_is_not_judged(self):
        self.assertEqual(self.rules("""
            from odoo import api
            class Helper:
                @api.model
                def a(self):
                    return self.sudo().search([])
        """), [])

    def test_the_ignore_comment_on_the_def_or_a_decorator_line_silences_it(self):
        self.assertEqual(self.rules(self.model("""
            @api.model
            def a(self):  # boundary-hazard-ignore: rpc-service-identity: public text only
                return self.sudo().search([])
            @api.model  # boundary-hazard-ignore: rpc-service-identity: public text only
            def b(self):
                return self.sudo().search([])
        """)), [])

    def test_test_files_are_not_judged(self):
        self.assertEqual(self.rules(self.model("""
            @api.model
            def a(self):
                return self.sudo().search([])
        """), name="tests/test_x.py"), [])


class GhTruncatedListingTests(RuleTestCase):
    def test_json_files_without_changedfiles_is_flagged_in_python(self):
        self.assertEqual(self.rules("""
            import subprocess
            def listing():
                return subprocess.run(["gh", "pr", "list", "--json", "number,files"], capture_output=True)
        """), ["gh-truncated-listing"])

    def test_json_files_with_changedfiles_next_to_it_is_fine(self):
        self.assertEqual(self.rules("""
            import subprocess
            def listing():
                return subprocess.run(["gh", "pr", "list", "--json", "number,files,changedFiles"], capture_output=True)
        """), [])

    def test_an_unrelated_json_field_list_is_fine(self):
        self.assertEqual(self.rules("""
            import subprocess
            def listing():
                return subprocess.run(["gh", "pr", "list", "--json", "number,title,headRefOid"], capture_output=True)
        """), [])

    def test_the_pull_request_files_endpoint_needs_paginate(self):
        self.assertEqual(self.rules("""
            import subprocess
            def one_page(n):
                return subprocess.run(["gh", "api", f"repos/o/r/pulls/{n}/files"], capture_output=True)
            def all_pages(n):
                return subprocess.run(["gh", "api", "--paginate", f"repos/o/r/pulls/{n}/files"], capture_output=True)
        """), ["gh-truncated-listing"])

    def test_a_command_that_is_not_gh_is_not_judged(self):
        self.assertEqual(self.rules("""
            import subprocess
            def listing():
                return subprocess.run(["tool", "--json", "files"], capture_output=True)
        """), [])

    def test_shell_lines_including_continued_ones_are_judged(self):
        found = self.findings("""
            #!/bin/bash
            gh pr view 5 --json files
            gh pr view 6 \\
              --json number,files,changedFiles
            # gh pr view 7 --json files
            gh api repos/o/r/pulls/9/files
            gh api --paginate repos/o/r/pulls/9/files
        """, name="run.sh")
        self.assertEqual([(f[0], f[2]) for f in found], [("gh-truncated-listing", 3), ("gh-truncated-listing", 7)])

    def test_the_rule_applies_in_test_files_too_but_the_ignore_comment_works(self):
        self.assertEqual(self.rules("""
            import subprocess
            def listing():
                return subprocess.run(["gh", "pr", "view", "--json", "files"], capture_output=True)  # boundary-hazard-ignore: gh-truncated-listing: first page is enough
        """, name="tests/test_x.py"), [])


class TimestampRangeTests(RuleTestCase):
    def test_a_timestamp_from_a_json_value_is_flagged(self):
        found = self.findings("""
            import time
            def stamp(item):
                return time.gmtime(item["when"])
        """)
        self.assertEqual([f[0] for f in found], ["timestamp-range"])
        self.assertEqual(found[0][1], "stamp")

    def test_datetime_fromtimestamp_and_utcfromtimestamp_are_flagged(self):
        self.assertEqual(self.rules("""
            import datetime
            def a(value):
                return datetime.datetime.fromtimestamp(float(value), tz=datetime.timezone.utc)
            def b(value):
                return datetime.datetime.utcfromtimestamp(value)
        """), ["timestamp-range", "timestamp-range"])

    def test_the_machines_own_clock_and_file_times_are_not_flagged(self):
        self.assertEqual(self.rules("""
            import os, time, datetime
            def a():
                return time.gmtime(time.time() - 3600)
            def b(path):
                return datetime.datetime.fromtimestamp(os.stat(path).st_mtime)
            def c(path):
                mtime = os.path.getmtime(path)
                return datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc)
            def d():
                return time.gmtime(0)
        """), [])

    def test_a_local_assigned_from_untrusted_data_is_still_flagged(self):
        self.assertEqual(self.rules("""
            import time
            def a(payload):
                when = time.time()
                when = payload["when"]
                return time.gmtime(when)
        """), ["timestamp-range"])

    def test_an_isfinite_check_in_the_function_silences_it(self):
        self.assertEqual(self.rules("""
            import math, time
            def a(item):
                value = float(item["when"])
                if not math.isfinite(value):
                    return None
                return time.gmtime(value)
        """), [])

    def test_a_try_that_covers_all_three_errors_silences_it_but_two_do_not(self):
        self.assertEqual(self.rules("""
            import time
            def covered(value):
                try:
                    return time.gmtime(value)
                except (OverflowError, ValueError, OSError):
                    return None
            def covered_broadly(value):
                try:
                    return time.gmtime(value)
                except Exception:
                    return None
            def not_covered(value):
                try:
                    return time.gmtime(value)
                except (ValueError, OSError):
                    return None
        """), ["timestamp-range"])

    def test_the_ignore_comment_with_a_reason_silences_it(self):
        self.assertEqual(self.rules("""
            import time
            def a(item):
                return time.gmtime(item["when"])  # boundary-hazard-ignore: timestamp-range: validate_report bounds it
        """), [])

    def test_a_wrong_rule_in_the_ignore_comment_does_not_silence_it(self):
        self.assertEqual(self.rules("""
            import time
            def a(item):
                return time.gmtime(item["when"])  # boundary-hazard-ignore: argv-log: not this rule
        """), ["timestamp-range"])


class UnparseableFileTests(RuleTestCase):
    # Tests [@ANCHOR: boundary_hazards:check_file]
    def test_a_file_with_a_syntax_error_has_no_findings(self):
        self.assertEqual(self.findings("def broken(:\n"), [])


class KnownListTests(RuleTestCase):
    # Tests [@ANCHOR: boundary_hazards:load_known]
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "hams_com")
        self.other = os.path.join(self.tmp, "hams_open")
        os.makedirs(self.repo)
        os.makedirs(self.other)
        for root in (self.repo, self.other):  # a scan of zero files exits 2 (check_target)
            _write(root, "clean.py", "VALUE = 1\n")
        self.known = os.path.join(self.tmp, "known.json")
        self._saved = chk.KNOWN_FILE
        chk.KNOWN_FILE = self.known
        self._argv = sys.argv
        os.environ["HAMS_LINT_NO_CACHE"] = "1"

    def tearDown(self):
        chk.KNOWN_FILE = self._saved
        sys.argv = self._argv
        os.environ.pop("HAMS_LINT_NO_CACHE", None)
        super().tearDown()

    def entry(self, **over):
        base = {"rule": "timestamp-range", "path": "mod/a.py", "symbol": "stamp", "owner": "Bruce (mod)",
                "reason": "fix pending"}
        base.update(over)
        return base

    def run_main(self, *roots):
        sys.argv = ["check_boundary_hazards.py", *roots]
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out):
            try:
                chk.main()
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue()

    def write_bad(self):
        _write(self.repo, "mod/a.py", """
            import time
            def stamp(item):
                return time.gmtime(item["when"])
        """)

    def write_known(self, *entries):
        with open(self.known, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "entries": list(entries)}, handle)

    def test_an_unlisted_finding_fails_the_run_and_names_rule_path_and_line(self):
        self.write_bad()
        code, out = self.run_main(self.repo, self.other)
        self.assertEqual(code, 1)
        self.assertIn("timestamp-range mod/a.py:4", out)

    def test_a_listed_finding_passes(self):
        self.write_bad()
        self.write_known(self.entry())
        self.assertEqual(self.run_main(self.repo, self.other)[0], 0)

    def test_an_entry_without_an_owner_or_a_reason_is_an_error_and_does_not_excuse_the_finding(self):
        self.write_bad()
        self.write_known(self.entry(owner=""), self.entry(symbol="other", reason=" "))
        code, out = self.run_main(self.repo, self.other)
        self.assertEqual(code, 1)
        self.assertIn("missing or empty owner", out)
        self.assertIn("missing or empty reason", out)
        self.assertIn("timestamp-range mod/a.py:4", out)

    def test_an_entry_for_an_unknown_rule_is_an_error(self):
        self.write_known(self.entry(rule="no-such-rule"))
        code, out = self.run_main(self.repo, self.other)
        self.assertEqual(code, 1)
        self.assertIn("unknown rule", out)

    def test_an_entry_whose_finding_is_gone_is_stale_and_fails(self):
        _write(self.repo, "mod/a.py", "VALUE = 1\n")
        self.write_known(self.entry())
        code, out = self.run_main(self.repo, self.other)
        self.assertEqual(code, 1)
        self.assertIn("no longer has the finding", out)

    def test_an_entry_for_a_deleted_file_is_stale_only_when_both_repositories_are_scanned(self):
        self.write_known(self.entry(path="gone/a.py"))
        self.assertEqual(self.run_main(self.repo, self.other)[0], 1)
        self.assertEqual(self.run_main(self.repo)[0], 0)

    def test_a_clean_tree_with_no_known_list_passes(self):
        _write(self.repo, "mod/a.py", "VALUE = 1\n")
        self.assertEqual(self.run_main(self.repo, self.other)[0], 0)

    def test_the_shipped_known_list_is_well_formed(self):
        entries, problems = chk.load_known(self._saved)
        self.assertEqual(problems, [])
        for entry in entries:
            self.assertIn(entry["rule"], chk.RULES)


if __name__ == "__main__":
    unittest.main()

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: check_boundary_hazards:check_boundary_hazards__tree_class]
