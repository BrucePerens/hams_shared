# ADR 0078: Model Validation and Database Integrity

## Status
Accepted

## Context
To guarantee data integrity across the platform, we require a defense-in-depth approach that combines application-layer Python validation with strict PostgreSQL database constraints. However, implementing these validations must not violate the platform's core performance mandates (preventing OOM crashes and N+1 queries) or its security architecture (Zero-Sudo). Furthermore, Odoo 19 introduces modernized syntactical requirements for defining database constraints that our AST linters strictly enforce.

## Decision
We mandate a two-tiered validation architecture for all data models, adhering to the following strict implementation rules:

### 1. Database-Level Rules (Strictly Enforced)
You are physically **FORBIDDEN** from using Odoo's legacy `_sql_constraints = [...]` list paradigm. The CI/CD pipeline will instantly fail any code utilizing this pattern.
You **MUST** define database constraints using the `models.Constraint` class attribute natively supported in Odoo 19.

**A. Uniqueness and Race Condition Defense:**
Structural identifiers (slugs, UUIDs, target IPs) must be mathematically locked using `UNIQUE` constraints to prevent Time-Of-Check to Time-Of-Use (TOCTOU) race conditions during concurrent transactions.

**B. Data Corruption Defense (`CHECK` Constraints):**
Odoo's `required=True` parameter only prevents `NULL` database values; it does not prevent an API or script from inserting empty strings (`""` or `"   "`) or illogical numerics. You must actively defend critical fields using PostgreSQL `CHECK` constraints:
* **Empty String Prevention:** `models.Constraint("CHECK(LENGTH(TRIM(name)) > 0)", "Name cannot be empty.")`
* **Numeric Boundaries:** `models.Constraint("CHECK(interval > 0)", "Interval must be greater than zero.")`

**Implementation Example:**
```python
from odoo import models, fields

class HamExample(models.Model):
    _name = "ham.example"

    name = fields.Char(string="Name", required=True)
    code = fields.Char(string="Code", required=True)
    interval = fields.Integer(string="Interval", required=True)

    # AST-Compliant Odoo 19 Constraints
    _code_uniq = models.Constraint("UNIQUE(code)", "The code must be mathematically unique.")
    _name_not_empty = models.Constraint("CHECK(LENGTH(TRIM(name)) > 0)", "Name cannot be empty.")
    _interval_positive = models.Constraint("CHECK(interval > 0)", "Interval must be > 0.")
```

### 2. Python-Level Model Validation (`@api.constrains` or `write` / `create`)
When adding application-layer validation, developers must respect the core performance and security mandates:

* **O(1) Memory Mapping (The N+1 Ban):** You MUST NOT execute `.search()`, `.search_count()`, or `.read_group()` inside a `for record in self:` loop during validation. If you need to validate uniqueness against existing records or check related states across a recordset, you MUST pre-fetch the data into a memory-mapped dictionary *before* entering the iteration loop.
* **Zero-Sudo Confinement:** If your validation logic requires evaluating records outside the current user's standard Access Control List (ACL), you MUST NOT use `.sudo()` to bypass the read restriction. You must query a Shadow Profile Index View (`_auto = False`) or utilize a specifically mapped micro-service account (via `.with_user()`) to verify the required state.
* **Fail-Fast Execution:** Validate preconditions at the absolute top of your method. Utilize early returns or raise `odoo.exceptions.ValidationError` immediately to avoid deep architectural nesting and wasted CPU cycles.

## Consequences
* **System Stability:** Enforcing O(1) memory mapping during validations prevents Out-Of-Memory (OOM) worker crashes during bulk data imports (e.g., ADIF uploads).
* **Mathematical Integrity:** Data corruption (empty strings or negative system timers) is physically rejected at the lowest level, protecting background daemons from division-by-zero or empty-path execution crashes.
* **Security Alignment:** Prevents developers from inadvertently introducing privilege escalation vectors via careless `.sudo()` usage in validation routines.
* **AST Compliance:** Ensures all new data models pass the strict syntax checks enforced by the CI/CD pipeline.
