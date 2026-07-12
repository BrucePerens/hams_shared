# Finding Severity Taxonomy

This document defines the severity levels used by code-review sub-agents.
"master" uses these levels to prioritize fix application.

---

## CRITICAL

**Meaning:** The finding represents an immediate risk to security, data
integrity, or system availability. Must be fixed before any release.

**Examples:**
- SQL injection via string interpolation in `cr.execute()`
- Privilege escalation (use of `sudo()`, `su=True`, `SUPERUSER_ID`)
- Path traversal in RPC/controller endpoints (CWE-22)
- Unvalidated user input passed to `eval()`, `exec()`, or `pickle`
- Missing access control on write/unlink operations
- Crash bugs that halt the Odoo registry or server startup

**Fix priority:** Immediate. Block all other work until resolved.

---

## ERROR

**Meaning:** A functional bug, linter violation, or test failure that will
cause the CI/CD pipeline to reject the code or cause incorrect behavior
in production.

**Examples:**
- Flake8 violations (unused imports, E741, F541, E712)
- AST linter violations (deprecated APIs, banned patterns)
- Broken semantic anchor linkage (missing test ↔ source anchor)
- Deprecated Odoo 19 patterns (`<tree>`, `t-esc`, `groups_id`, etc.)
- Missing `__manifest__.py` description field
- Test failures

**Fix priority:** High. Must be resolved before the linter/test pass.

---

## WARNING

**Meaning:** A code quality issue that does not break the build but
degrades performance, maintainability, or test coverage.

**Examples:**
- N+1 query loops (`.search()` inside `for` loop)
- Unbounded `.search()` without `limit=`
- Missing test coverage for a code path
- Poor variable naming or excessive nesting
- Unnecessary database round-trips between systems
- Missing `aria-label` or `alt` attributes (WCAG)

**Fix priority:** Medium. Fix during the review if time permits;
otherwise document for a future pass.

---

## INFO

**Meaning:** A suggestion for improvement that does not affect
correctness, security, or performance. Purely advisory.

**Examples:**
- Better docstrings or inline comments
- Documentation improvements in `data/*.html`
- UI/UX polish (spacing, color consistency, wording)
- Linter rule suggestions (new rules that could prevent future issues)
- Architectural suggestions for future refactoring

**Fix priority:** Low. Document in the implementation plan. Do not
fix under time pressure; defer to a dedicated improvement sprint.
