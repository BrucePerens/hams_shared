"""Advisory lint tier (2026-11-23): an advisory failure is reported but does not block; a blocking failure still does."""
import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lint_advisory
import lint_batch


def result(name, code, output=""):
    return types.SimpleNamespace(name=name, returncode=code, output=output)


class AdvisoryTierTest(unittest.TestCase):
    # [@ANCHOR: test_advisory_failure_does_not_block_but_blocking_failure_does]
    def test_advisory_failure_does_not_block_but_blocking_failure_does(self):
        blocking, advisory = lint_advisory.split_failures([
            result("check_claims_freshness", 1, "\nstale claim x\nmore"), result("flake8", 0), result("check_anchor_docs_standard", 1, "short")])
        self.assertEqual(blocking, [])
        self.assertEqual([r.name for r in advisory], ["check_claims_freshness", "check_anchor_docs_standard"])
        self.assertTrue(lint_advisory.report_lines(advisory)[0].startswith("ADVISORY FAIL: check_claims_freshness: stale claim x"))
        blocking, advisory = lint_advisory.split_failures([result("check_claims_freshness", 1), result("verify_anchors", 1), result("flake8", 1)])
        self.assertEqual([r.name for r in blocking], ["verify_anchors", "flake8"])
        self.assertEqual([r.name for r in advisory], ["check_claims_freshness"])

    def test_anchor_and_security_checks_are_never_advisory(self):
        for name in ("verify_anchors", "check_function_test_anchors", "flake8", "mypy", "check_pip_audit"):
            self.assertFalse(lint_advisory.is_advisory(name))

    def test_batch_read_results_ignores_advisory_failures(self):
        doc = {"selected": ["check_claims_freshness", "verify_anchors"],
               "steps": [{"name": "check_claims_freshness", "returncode": 1}, {"name": "verify_anchors", "returncode": 1}],
               "outputs": {"check_claims_freshness": "stale", "verify_anchors": "missing"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(doc, handle)
            _, failed = lint_batch.read_results(path)
        self.assertEqual(failed, {"verify_anchors": "missing"})


if __name__ == "__main__":
    unittest.main()
