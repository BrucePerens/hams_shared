#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for bulk_explanation_manager.py's export_pending() and
import_completed().

Both functions take `client` as a plain parameter -- clean dependency
injection, so a MagicMock stands in for the real Odoo client and no
live Odoo connection is needed at all. The obstacle is the module's
own top-level import block: it does
`sys.path.append("../../../hams_com/daemons")` then
`from hams_config import get_odoo_client`, guarded with
`except ImportError: get_odoo_client = None` -- with a comment
claiming this makes a bare `import bulk_explanation_manager` safe in a
hams_open-only checkout. On this machine, though, a sibling hams_com
checkout genuinely exists, so the import doesn't raise ImportError --
it finds hams_config.py, which then raises `KeyError: 'ODOO_URL'` at
its own module level (real env var it expects to be set), and that
propagates straight through: confirmed empirically that a plain
`import bulk_explanation_manager` crashes uncaught on this dev
machine, contradicting the guard comment's claim for any environment
where hams_com is present but not yet configured. Documented as a
real, environment-dependent finding, not fixed here (this test suite
lives in hams_shared/hams_open and has no business depending on
hams_com's hams_config.py either way).

To test export_pending()/import_completed() without inheriting any of
that fragility, this extracts just their two function definitions by
regex from the real source (stopping before the `if __name__` block,
which is the only place get_odoo_client is actually used) and exec()s
them in an isolated namespace -- the same technique test_run_linters.py
and test_fix_manifests.py already use for a real source snippet with
side-effecting or environment-fragile code around it that a test has
no reason to depend on.
"""

import json
import logging
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bulk_explanation_manager.py")


def _extract_functions():
    with open(_SCRIPT, encoding="utf-8") as f:
        source = f.read()
    match = re.search(r"(def export_pending\(.*?)\nif __name__", source, re.DOTALL)
    if not match:
        raise AssertionError(
            "Could not locate export_pending()/import_completed() in "
            "bulk_explanation_manager.py -- its shape changed; update this "
            "test's extraction regex."
        )
    namespace = {
        "logging": logging,
        "os": os,
        "json": json,
        "logger": logging.getLogger("test_bulk_explanation_manager"),
    }
    exec(match.group(1), namespace)  # real source, not user input
    return namespace["export_pending"], namespace["import_completed"]


class ExportPendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.export_pending, self.import_completed = _extract_functions()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _out(self):
        return os.path.join(self.tmp, "out.json")

    def test_an_a_dot_formatted_answer_is_parsed_into_a_lettered_choice(self):
        client = MagicMock()
        client.execute.side_effect = [
            [{"id": 1, "ncvec_code": "T1A01", "title": "Q1", "suggested_answer_ids": [10, 11]}],
            [
                {"id": 10, "value": "A. Correct answer", "is_correct": True},
                {"id": 11, "value": "B. Wrong answer", "is_correct": False},
            ],
        ]
        out = self._out()
        self.export_pending(client, out)
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(
            data,
            [{"id": 1, "code": "T1A01", "text": "Q1", "correct": "A", "choices": {"A": "Correct answer", "B": "Wrong answer"}}],
        )

    def test_an_unformatted_answer_falls_back_to_the_answer_id_as_the_choice_key(self):
        client = MagicMock()
        client.execute.side_effect = [
            [{"id": 2, "ncvec_code": "T1A02", "title": "Q2", "suggested_answer_ids": [20]}],
            [{"id": 20, "value": "unformatted answer", "is_correct": False}],
        ]
        out = self._out()
        self.export_pending(client, out)
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data[0]["choices"], {"20": "unformatted answer"})
        self.assertIsNone(data[0]["correct"])

    def test_a_client_error_is_caught_and_logged_not_raised_and_no_output_file_is_written(self):
        client = MagicMock()
        client.execute.side_effect = RuntimeError("boom")
        out = self._out()
        self.export_pending(client, out)  # must not raise
        self.assertFalse(os.path.exists(out))

    def test_no_pending_questions_still_writes_an_empty_json_array(self):
        client = MagicMock()
        client.execute.return_value = []
        out = self._out()
        self.export_pending(client, out)
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data, [])


class ImportCompletedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.export_pending, self.import_completed = _extract_functions()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _input(self, content):
        p = os.path.join(self.tmp, "completed.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_a_missing_input_file_logs_and_returns_without_calling_the_client(self):
        client = MagicMock()
        self.import_completed(client, os.path.join(self.tmp, "does_not_exist.json"))
        client.execute.assert_not_called()

    def test_explanations_are_stripped_and_sent_via_the_bulk_daemon_method(self):
        client = MagicMock()
        p = self._input('[{"id": 5, "explanation": "  because reasons  "}]')
        self.import_completed(client, p)
        client.execute.assert_called_once_with(
            "survey.question", "daemon_write_questions", q_dict={"5": {"explanation": "because reasons"}}
        )

    def test_an_item_with_no_explanation_is_skipped(self):
        client = MagicMock()
        p = self._input('[{"id": 6, "explanation": ""}]')
        self.import_completed(client, p)
        client.execute.assert_not_called()

    def test_when_the_bulk_method_fails_it_falls_back_to_one_write_call_per_question(self):
        client = MagicMock()

        def side_effect(model, method, *args, **kwargs):
            if method == "daemon_write_questions":
                raise RuntimeError("no such method")
            return None

        client.execute.side_effect = side_effect
        p = self._input('[{"id": 5, "explanation": "because reasons"}]')
        self.import_completed(client, p)
        self.assertEqual(client.execute.call_count, 2)
        client.execute.assert_any_call("survey.question", "write", [5], {"explanation": "because reasons"})

    def test_malformed_json_input_is_caught_and_logged_not_raised(self):
        client = MagicMock()
        p = self._input("not valid json")
        self.import_completed(client, p)  # must not raise
        client.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
