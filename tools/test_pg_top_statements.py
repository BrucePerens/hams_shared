import unittest
from unittest.mock import MagicMock

import pg_top_statements as mod


class PgTopStatementsTest(unittest.TestCase):
    def test_tables_in_reads_from_and_join(self):
        q = 'SELECT 1 FROM "ham_callbook" c JOIN res_partner p ON p.id = c.user_id WHERE c.callsign = $1'
        self.assertEqual(mod.tables_in(q), ["ham_callbook", "res_partner"])

    def test_parse_rows_masks_utility_and_keeps_pipes_in_query(self):
        out = "5|12.5|2.500|5|SELECT a || b FROM ham_repeater\n1|0.1|0.100|0|CREATE ROLE x PASSWORD 'p'\n"
        rows = mod.parse_rows(out)
        self.assertEqual(rows[0]["query"], "SELECT a || b FROM ham_repeater")
        self.assertEqual(rows[0]["tables"], ["ham_repeater"])
        self.assertEqual(rows[1]["query"], "<utility>")

    def test_main_reports_a_failing_psql(self):
        run = MagicMock(return_value=MagicMock(returncode=1, stderr="no view", stdout=""))
        self.assertEqual(mod.main([], run=run), 1)

    def test_main_table_filter(self):
        out = "5|12.5|2.500|5|SELECT 1 FROM ham_repeater\n"
        run = MagicMock(return_value=MagicMock(returncode=0, stdout=out, stderr=""))
        self.assertEqual(mod.main(["--table", "ham_callbook"], run=run), 0)
        self.assertIn("pg_stat_statements", run.call_args.args[0][-1])


if __name__ == "__main__":
    unittest.main()
