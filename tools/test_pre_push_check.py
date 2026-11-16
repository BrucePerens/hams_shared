"""pre_push_check.py: which direct pushes to main are allowed and the time budget of the branch check."""
import subprocess
import unittest

import pre_push_check as ppc


class PrePushCheckTests(unittest.TestCase):
    # [@ANCHOR: test_a_main_push_is_allowed_only_for_notes_or_with_the_override]
    def test_a_main_push_is_allowed_only_for_notes_or_with_the_override(self):
        notes = ["night_shift_todo/high/a.md", "night_shift_history/b.md", "night_shift_questions/c.md"]
        self.assertIsNone(ppc.main_push_problem(notes, {}))
        self.assertIn("merge_pr.py", ppc.main_push_problem(notes + ["ham_base/models/x.py"], {}))
        self.assertIsNone(ppc.main_push_problem(["x.py"], {"HAMS_ALLOW_MAIN_PUSH": "1"}))
        self.assertIsNotNone(ppc.main_push_problem(["night_shift_todo_evil/x.py"], {}))

    # [@ANCHOR: test_the_runner_gives_up_when_the_time_is_spent]
    def test_the_runner_gives_up_when_the_time_is_spent(self):
        calls = []

        def run(argv, cwd=None, timeout=0):
            calls.append(timeout)
            return subprocess.CompletedProcess(argv, 124 if argv[0] == "slow" else 0, "", "")

        now = [100.0]
        runner = ppc.deadline_runner(run, 160.0, clock=lambda: now[0])
        self.assertEqual(runner(["fast"]).returncode, 0)
        self.assertEqual(calls, [60.0])
        # Tests [@ANCHOR: pre_push_check_out_of_time]
        with self.assertRaises(ppc.OutOfTime):
            runner(["slow"])
        now[0] = 161.0
        with self.assertRaises(ppc.OutOfTime):
            runner(["fast"])
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
