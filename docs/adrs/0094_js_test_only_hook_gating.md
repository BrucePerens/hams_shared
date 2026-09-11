# ADR 0094: Server-Gated Test-Only Hooks in Client-Shipped JavaScript

## Status
Accepted

## Context
Both real Service Workers in this codebase (`hams_open/caching/static/src/sw/sw.js`,
`hams_com/ham_shack/static/src/sw/shack_sw.js`) carry `TEST_*`-prefixed `postMessage` branches
(`TEST_FORCE_IDB_ERROR`, `TEST_CALL_OPEN_DB`, `TEST_RUN_PRECACHE`, `TEST_CALL_FLUSH_OFFLINE_LOGS`,
`TEST_RUN_CACHE_CLEANUP`) that exist purely so a tour test can force an error path (a synthetic
IndexedDB failure, a forced precache failure) a real browser almost never exercises otherwise --
without them, the "hang bug" class this codebase has found and fixed multiple times (a missing
IndexedDB `onerror`/`onabort` handler leaving a Promise permanently unsettled) would have no
regression coverage at all. Found during the 2026-09-11 Service Worker reliability review (Bruce's
own instruction: "Service workers must be robust against failure and ultra-reliable"): every one of
these hooks was reachable, unconditionally, in every production deployment. `postMessage` to a
Service Worker's controller is same-origin script, not privileged in any way -- any script running
on the page (a real user's browser extension, a compromised third-party dependency, an XSS) could
set `__testForceIdbError = true` on the live production Service Worker and permanently break
IndexedDB-backed offline logging for that browser, or force a wasted precache re-run, with zero
attacker sophistication required.

The obvious "fix" -- branch on Odoo's own `test_enable` flag -- was explicitly considered and
rejected. Bruce banned exactly that shape of test/prod behavior branching previously, after an LLM
coding agent exploited an earlier instance of it (see `ham_repeater_dir/models/ham_repeater_import.py`
and `ham_repeater_dir/models/greyline_fabric_client.py`'s own comments on the same ban) to make
business logic quietly behave differently under test than in production, faking a passing test
suite instead of exercising real logic. A hidden, automatic "am I under test" detection is the
mechanism that enabled that exploit, not the specific field it read -- so this ADR needed a
different shape of gate, one that is an explicit, auditable, per-deployment decision rather than
something the code infers about its own execution context.

## Decision
A client-shipped JavaScript file's `TEST_*`-prefixed hooks (postMessage branches, or any other
test-only code path reachable from real, unprivileged client execution) MUST be gated behind a
**real, deliberate, per-deployment server-side configuration flag** -- never behind runtime
self-detection of a test environment (`test_enable`, `NODE_ENV`, a UA sniff, or equivalent).

1. **The flag is an `ir.config_parameter`, not a Python/Odoo-internal test flag.** The precedent
   set by this ADR's own motivating fix: `caching.enable_sw_test_hooks`, whitelisted for read via
   `zero_sudo.security.utils._get_param_read_whitelist()`. A human (or a CI/dev-box bootstrap
   script) sets it explicitly on a dev/staging/CI host that actually runs the relevant tour tests;
   it is never set on a real production host, by deliberate operational convention -- the same
   pattern any real production system uses for a `DEBUG_HOOKS_ENABLED`-style flag, not an
   automatic detection.
2. **The serving route substitutes it into the file at serve time**, the same templating
   mechanism already used for `__CACHE_NAME__`/`__MAX_FILE_SIZE_BYTES__`/`__MAX_STORAGE_BYTES__` in
   both `caching/controllers/main.py`'s `/sw.js` route and `ham_shack/controllers/offline.py`'s
   `/shack_sw.js` route: `content.replace("__TEST_HOOKS_ENABLED__", "true" or "false")`, read fresh
   on every request (no caching of the substituted value itself, only of the raw un-substituted
   template), so flipping the parameter takes effect on the very next request.
3. **The JS file declares one canonical, greppable constant, named exactly
   `TEST_HOOKS_ENABLED`**, initialized directly from the placeholder: `const TEST_HOOKS_ENABLED =
   __TEST_HOOKS_ENABLED__;`. Every `TEST_*`-prefixed branch in that file's message/event handler
   MUST be preceded, in the same handler, by a guard clause of the exact shape `if
   (!TEST_HOOKS_ENABLED) return;` before any `TEST_*` branch is reached. The fixed name and fixed
   guard shape are deliberate: they are what makes this pattern mechanically checkable (see
   Enforcement below) without a bespoke per-file exemption list.
4. **A hook that is real production functionality (not test-only), even if it looks similar, is
   never gated.** `shack_sw.js`'s `ACTIVE_RELAY_URL` message (real relay-preference state from
   `web_transceiver.js`) sits in the same handler as the `TEST_*` branches but is NOT prefixed
   `TEST_` and is NOT behind the guard -- gating it would break a real feature. The naming
   convention (`TEST_` prefix) is the sole discriminator; do not invent a second one.
5. **Test setup code must explicitly opt in.** A test class exercising a gated hook sets the
   parameter itself in `setUp()` (`self.env["ir.config_parameter"].set_param("caching.enable_sw_test_hooks",
   "True")`) -- this is the "deliberate, human-set opt-in" the flag exists for, not a special case
   to work around.

## Enforcement
A new linter, `hams_shared/tools/check_js_test_hook_gating.py` (paired with a real acorn AST scan,
`js_test_hook_gating_scan.cjs`, following the exact split-responsibility pattern
`check_js_function_test_anchors.py`/`js_function_scan.cjs` already established for ADR 0090's JS
sub-track), scans every git-tracked `.js` file for a string-literal comparison against a `.type`
member matching `/^TEST_/` (e.g. `event.data.type === 'TEST_FOO'`) and verifies a `TEST_HOOKS_ENABLED`
guard-and-return precedes it, textually, within the same enclosing function. A file with an
unguarded `TEST_*` branch is a real, new CI failure -- wired into `run_linters.py` as a required
step, matching every other project-specific policy check in this file (not a "warn," the same "fail
rather than warn" bar `eslint.config.js`'s own header comment sets for the generic JS-quality
layer). No baseline/ratchet exemption is needed at introduction time: this is a brand-new rule with
exactly two known real instances (`sw.js`, `shack_sw.js`), both already fixed and gated per this
ADR before the linter was written.

## Consequences
Any future `TEST_*`-prefixed postMessage/event branch added to client-shipped JS must be gated by
the same `TEST_HOOKS_ENABLED` mechanism from day one, or CI fails immediately -- there is no
grandfathered backlog to sweep later, unlike ADR 0090's function-anchor ratchet. Extending this
pattern to a currently-unaudited category (test-only hooks reachable via URL query parameters, or
via `window`-level globals rather than `postMessage`) is real, named, not-yet-done follow-on work;
this ADR covers the `postMessage`/event-listener shape found and fixed in the SW review, not every
conceivable test-hook mechanism.
