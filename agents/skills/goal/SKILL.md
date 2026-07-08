---
name: goal
description: The goal in a file, so Antigravity won't forget.
---

# Antigravity Goal

Run the linters in hams_open and hams_com, using the command
tools/run_linters.py Fix all of the linter complaints.

Then, run full tests in hams_open and hams_com, using the command
tools/test.py . Iteratively fix issues
and run tests until all tests are 100% pass.

Then, perform your code-review skill. You are able to choose how to run this
skill so that you do not overrun limits, for example you can choose to run only
5 sub-agents at a time.

Create a second version of hams_com/ham_base/data/portal_features.html,
called hams_com/ham_base/data/portal_features_order_of_interest.html and
organize this one by order of interest of the features to radio amateurs,
most interesting module headings first, most interesting features first within
each module heading. This will be a more compelling read for them than
hams_com/ham_base/data/portal_features.html, which starts with compliance, which
they will mostly be bored with.

Then again run the linters in hams_open and hams_com, using the command
tools/run_linters.py Fix all of the linter complaints.

Then, again run full tests in hams_open and hams_com. Iteratively fix issues
and run tests until all tests are 100% pass.

Do this entire task non-interactively as the user is busy and not
available to consult with you.  Don't pause for the user to evaluate
anything. The user would like to see a lot of work done when he returns.
