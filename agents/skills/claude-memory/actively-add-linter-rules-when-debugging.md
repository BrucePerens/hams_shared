---
name: actively-add-linter-rules-when-debugging
description: "Bruce wants Claude to proactively turn bug patterns found while debugging into new linter rules, not just fix the one instance."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-06T19:19:40.504Z
---

While closing test-coverage gaps in hams_com's `ham_logbook` module, Claude found and fixed two
distinct, real bug classes purely by actually running tests rather than reviewing code statically:
a QWeb `<form method="post">` template missing its `csrf_token` hidden input (which would make every
real submission fail Odoo's CSRF check), and a `RealTransactionCase` test that read back a record
created via a real HTTP round-trip without first calling `self.env.cr.commit()` (which raised a real
`MissingError` because the test's own cursor hadn't started a fresh transaction to see the HTTP
worker's committed write). In both cases Claude's own initiative was to generalize the fix into a new
`check_burn_list.py` rule with its own test coverage, rather than only patching the one instance.

When Claude asked whether the second case ("is that something you can check for in a linter rule?")
was linter-detectable, Bruce's response was to make this a standing instruction rather than a one-off
answer: "Let's put this in your memory: actively add linter rules as you debug things."

The generalized lesson: whenever Claude diagnoses and fixes a bug during any debugging or
test-verification work on this codebase, it should pause and ask whether the underlying mistake is a
recognizable, mechanically-detectable *pattern* (a missing template input, a missing commit before a
cross-connection read, a wrong access-control grant shape, etc.) rather than a one-off typo or
business-logic error specific to that call site. If it is a pattern that could recur elsewhere or in
future code, the expected behavior is to proactively add a new rule to `hams_shared/tools/
check_burn_list.py` (or the appropriate linter) — including real test coverage for the new rule
itself, and a verification pass confirming zero false positives across both repos before committing —
rather than treating the fix as complete once the one instance is patched. This mirrors, and should be
applied with the same proactive spirit as, the existing `hams-dont-be-too-cautious-about-originating-
work` standing preference: don't wait to be asked before generalizing a real, freshly-found bug class
into permanent tooling.
