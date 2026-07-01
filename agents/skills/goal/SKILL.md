---
name: goal
description: The goal in a file, so Antigravity won't forget.
---

# Antigravity Goal

Run the linters in hams_open and hams_com, using the command
tools/run_linters.py Fix all of the linter complaints.

Then, run full tests in hams_open and hams_com. Iteratively fix issues
and run tests until all tests are 100% pass.

Then, do to-dos: There are two to-do skills:
./hams_com/agents/skills/hams-todo-list/SKILL.md and
./hams_open/hams_shared/agents/skills/todo-list/SKILL.md . Proposals are
in hams_com/docs/proposals. Implement the to-dos in easiest-to-hardest
order. After each one, run the linters and tests, fixing any issues,
before going on to the next.

Then, again run the linters in hams_open and hams_com, using the command
tools/run_linters.py Fix all of the linter complaints.

Then, again run full tests in hams_open and hams_com. Iteratively fix issues
and run tests until all tests are 100% pass.

Then, perform your code-review skill. You are able to choose how to run this
skill so that you do not overrun limits, for example you can choose to run only
5 sub-agents at a time.

Then again run the linters in hams_open and hams_com, using the command
tools/run_linters.py Fix all of the linter complaints.

Then, again run full tests in hams_open and hams_com. Iteratively fix issues
and run tests until all tests are 100% pass.

Then, again perform your code-review skill. You are able to choose how to run this
skill so that you do not overrun limits, for example you can choose to run only
5 sub-agents at a time.

Do this entire task non-interactively as the user is asleep until around 7 AM.
Don't pause for the user to evaluate anything. The user would like to see
a lot of work done when he returns. If you really run out of things to do, do
linting and testing of both hams_open and hams_com until all linter complaints
are fixed and all tests in hams_open and hams_com pass. If you still run out of
things to do, perform the code-review skill.
