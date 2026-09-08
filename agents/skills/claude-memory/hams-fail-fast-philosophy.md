---
name: hams-fail-fast-philosophy
description: "The hams.com/hams_open codebase's standing \"fail fast\" principle — never let error paths silently fall back to a default that can mask a real bug."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: f258aa64-9187-4d83-b1ce-de32d7cb03b4
  modified: 2026-08-17T14:46:25.045Z
---

Across the hams_com and hams_open (Odoo) repositories, Bruce's stated overall project philosophy is "fail fast": code should avoid fallbacks, silent defaults, and other patterns that hide bugs instead of surfacing them. When a security or correctness invariant is violated, or a caller does something the system doesn't intend to allow, the code should raise loudly rather than quietly substitute a default value and continue.

This was stated explicitly after a concrete case that illustrates why it matters: `ham_base/models/ir_config_parameter.py`'s `get_param()` override blocked service accounts from reading non-whitelisted system parameters (like `database.secret`) by silently returning the caller-supplied `default` instead of raising. Two real call sites (`ham_shack`'s `relay_token` endpoint and `ham_onboarding`'s LoTW transfer-token signing in `lotw_auth.py`) each assumed they'd successfully read the real secret, when they'd actually gotten a placeholder back with no error — one crashed with an `AttributeError` downstream, the other silently signed every authentication token with an empty key, a full auth-bypass vulnerability that shipped with a passing test suite because nothing ever surfaced the substitution. The fix was to make `get_param` raise an `AccessError` in that branch, mirroring the sibling `set_param` override, which already correctly raised instead of silently no-op'ing on a blocked write.

When reviewing or writing code in this codebase, prefer raising an explicit exception over returning a default, `None`, `False`, an empty collection, or otherwise swallowing an error condition — especially in security-relevant code (access control, credential/secret handling, signature verification) where a silent fallback can turn a caught bug into an unnoticed vulnerability.
