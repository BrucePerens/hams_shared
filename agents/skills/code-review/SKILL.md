---
name: code-review
description: How to do a code-review.
---

# Code Review

When asked to do a code-review:

Start sub-agents at a rate you are comfortable with. For example: start
5 and then start additional ones as the ones already started finish
until you have performed the entire task, which is a review of about
70 directories (at this writing, the number will change) constituting
the entire code-base. The number of sub-agents to run in parallel is
entirely your choice.

Start a sub-agent for each module in the hams_open and hams_com repositories, and
each of the daemons, and tools and other facilities in hams_shared. The sub-agents
task is to perform a deep code-review of that individual module or directory
only, so that its context window is not saturated. Have them review
the code for:
* Any possible improvements to the linters that would help to avoid issues that
  come up during your review, to improve code quality and Odoo 19 compatibility,
  and to avoid AI foibles.
* Licensing: Code in hams_com must be proprietary _except_ for code that we don't
  own, like radae. Code in hams_shared and hams_open should be AGPL-3, again
  preserving the license of anything we don't own.
* Odoo 19 compliance and repair of deprecated and removed coding patterns. Specifically:
  * Replace legacy `_sql_constraints` with the new `models.Constraint` API defined as direct class attributes.
  * Use `self._has_cycle()` instead of the legacy `_check_recursion()` method.
  * Ensure `@api.constrains` validations are robust, especially when fields are redefined in subclasses. Pay special attention to tests failing with `ValidationError not raised`, which often indicate missing constraints on overridden fields or `IntegrityError` catching gaps between `create` and `write` methods.
  * Move local/function-level imports (like `werkzeug` components) to the module level to satisfy linter rules.
* Quality and lack of conflict in AI directives and documentation - put resolving
  these as open questions for the user in the implementation plan, where
  you are not clear upon the correct action.
* Appropriateness and completeness for use: hams_open is for hams and
  non-hams equally, hams_com is for hams and ham radio aspirants (SWLs), and
  we think first-responders and emergency services personnel as well as hams
  will be interested in ham_auxcomm_training.
* Attractiveness: The site must compel users to join hams.com and continue to
  use it.  Thus, the features themselves must be ones that attract users, and
  you can improve them as necessary. The UI must be easy to use, visually
  attractive, and consistent across the entire system. Think about things that
  could be annoying to the operator as they are currently implemented, and try
  to improve them.
* AI issues (incomplete code, laziness, etc.)
* Code quality and best practices.
* Security
* Performance
* Latency, reduction of bandwidth utilization, and the possibility to
  reduce the number of turn-arounds per operation betwen different
  systems. These systems include, but are not limited to: odoo and the
  database, daemons and the database, the central hams relay daemon and
  local relay daemons, the browser and the hams.com site, the browser and
  local relay daemons directly or through the central hams relay daemon.
  Keep in mind that all of these systems may be halfway around the world
  from each other.
* Coverage and quality of testing
* Coverage and quality of documentation in data/*.html .
* Any thing else you think of.

Have the sub-agents report to you, but don't have them fix anything, because
they would conflict when they break modules for other sub-agents. Fix all of the
issues the sub-agents report, either by yourself or using sub-agents to do it.

Run the linter, using tools/run_linters.py, in hams_open, hams_open/hams_shared,
and hams_com; and fix all linter complaints. Run full tests in both hams_open
and hams_com and fix any remaining problems until the tests 100% pass.

Finally, check all of your changes in to git. Do a git push if you are able.
