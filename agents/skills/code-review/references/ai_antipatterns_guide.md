# AI Laziness and Anti-Patterns Guide

When reviewing code or attempting to fix linter errors, sub-agents often fall into "lazy" patterns that silence errors instead of fixing the underlying architectural issues. Reviewers MUST actively flag these, and fixers MUST avoid them.

## 1. The "hasattr / getattr" Fallback
**Anti-Pattern:** Using `hasattr(obj, 'prop')` or `getattr(obj, 'prop', default)` to defensively handle missing attributes.
**Why it's bad:** It masks architectural type uncertainties and broken schema contracts.
**The Fix:** Attempt to access the property directly and explicitly catch `AttributeError` if a fallback is strictly necessary. Let it fail loudly otherwise.
```python
# BAD
val = getattr(record, 'my_field', None)

# GOOD
try:
    val = record.my_field
except AttributeError:
    val = None # or raise an explicit custom error
```

## 2. Empty Exception Handlers & Catch-Alls
**Anti-Pattern:** Using `except Exception:` or `contextlib.suppress()` without logging.
**Why it's bad:** It creates a black hole where critical system failures are silently swallowed.
**The Fix:** Catch the *specific* exception (e.g., `OSError`, `KeyError`). If an empty handler is required by design, log a warning using `_logger.warning(...)`. Use `# audit-ignore-catch-all` ONLY when an operation absolutely must continue past an unresolvable failure.

## 3. Privilege Escalation (`sudo`)
**Anti-Pattern:** Calling `.sudo()` to bypass record rules.
**Why it's bad:** It completely bypasses the Zero-Sudo architecture and allows unchecked privilege escalation.
**The Fix:** Use the service account architecture. Retrieve the correct service UID via `_get_service_uid()` and use `.with_user(svc_uid)`. You MUST NOT fall back to `base.user_admin` (UID 2), even in tests.

## 4. Unbounded Searches
**Anti-Pattern:** Calling `self.env['model'].search([...])` without a limit on large tables.
**Why it's bad:** Can cause massive memory spikes and OOM kills on multi-tenant production databases.
**The Fix:** Always specify a `limit`. E.g., `search([...], limit=10000)` or `limit=len(keys)`.

## 5. Phantom Semantic Anchors
**Anti-Pattern:** Adding an `# audit-ignore-sql` or `# audit-ignore-view` tag just to silence the linter, without actually writing the test it anchors to.
**Why it's bad:** The linter will fail in Phase 3 because the anchor `[@ANCHOR: ...]` won't exist in the test suite. 
**The Fix:** If you bypass an architectural mandate, you MUST write the corresponding test that proves the bypass is safe.
