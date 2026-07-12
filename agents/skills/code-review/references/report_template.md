# Sub-Agent Review Report Template

Use this exact structure when reporting findings back to "master".

---

## Review Report: [module_name] — [Role Name]

**Reviewer Role:** Compliance_and_Quality_Reviewer | Architecture_and_Security_Reviewer | Product_and_UX_Reviewer
**Module Path:** `[absolute path to module root]`
**Files Reviewed:** [count]
**Total Findings:** [count by severity]

### Summary

[1-2 sentence high-level assessment of the module's health.]

### Findings

| # | Severity | File | Line | Issue Description | TargetContent | ReplacementContent |
|---|----------|------|------|-------------------|---------------|--------------------|
| 1 | CRITICAL | `models/foo.py` | 42 | SQL injection via f-string in `cr.execute()` | `cr.execute(f"SELECT * FROM {table}")` | `cr.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))` |
| 2 | ERROR | `views/bar.xml` | 18 | Deprecated `<tree>` tag; must use `<list>` | `<tree string="Items">` | `<list string="Items">` |

### Areas Reviewed With No Issues

- [List specific files or subsystems that were reviewed and found clean.]
- Example: `controllers/main.py` — routing, auth, input validation all correct.
- Example: `security/ir.model.access.csv` — permissions properly scoped.

---

## Formatting Rules

1. **Every finding MUST have all 7 columns filled.** If a fix cannot be
   expressed as simple TargetContent/ReplacementContent (e.g., a missing
   file or a structural refactor), put `[MANUAL]` in both content columns
   and describe the required action in the Issue Description.

2. **Severities** are strictly:
   - `CRITICAL` — Security vulnerabilities, data corruption, crash bugs
   - `ERROR` — Functional bugs, linter violations, test failures
   - `WARNING` — Performance issues, code smells, missing tests
   - `INFO` — Style suggestions, documentation improvements, UX ideas

3. **File paths** must be relative to the repository root (e.g.,
   `edge_routing/models/domain.py`, not the absolute path).

4. **Do not invent findings.** If the module is clean, report:
   > No issues found. All [N] files reviewed; code is compliant with
   > project standards.
