# ADR 0091: Function-Level Claims Directory and Bug-Hunt Integration

## Status
Accepted

## Scope
This ADR extends `MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md`'s existing anchor-traceability mandate,
`verify_anchors.py`'s existing enforcement of it, and ADR 0090's universal test-anchor ratchet,
with a new, optional artifact type: a per-function "claim." It also amends
`hams_shared/agents/skills/bug-hunt/SKILL.md` to read and write claims as part of its own method.

## Context
The 2026-09-08 patent-disclosure re-verification found that stating a precise, falsifiable claim
about what a function does, then adversarially checking that claim against the real code and the
real test suite, found real production bugs a normal review pass had missed -- documented in the
bug-hunt skill's own "Origin" section. That skill currently has to re-derive a claim for a function
from scratch on every pass, from whatever comments, docstrings, or test names happen to exist.
Bruce's own direction, given while reviewing the skill: establish a real "claims" directory in each
software module, extend the anchor system to track these claims against the functions they
document, keep them in phase with the actual code, and use them for bug-hunt review -- so a claim
survives across passes and sessions instead of being reinvented and discarded each time, the same
compounding-value reasoning ADR 0090's test-anchor ratchet and the bug-hunt skill's own growing
bug-class checklist already rest on.

## Decisions

1. **A claim lives at `<module>/claims/<anchor_name>.md`**, one file per anchored function, not
   one aggregate file per module. One file per anchor keeps the git history and any future
   automated diffing meaningful per-function, and avoids the exact class of shared-file
   concurrent-edit risk the same night's patent-portfolio work hit directly (a single large
   `MASTER_CLAIMS.md`-style file corrupted by a racing edit) -- claims for different functions
   never need to touch the same file.
2. **A claim's own frontmatter is two flat fields, not a general YAML document**: `anchor` (the
   base anchor name it documents, matching a real `# [@ANCHOR: name]` in source) and `code_hash`
   (a `sha256:`-prefixed hex digest of the anchored function's own source span, as it existed the
   last time a human or AI actually confirmed the claim's prose still matches the code). Kept
   deliberately minimal -- two scalars, parsed with a small regex rather than a real YAML library
   dependency, matching this tool family's existing dependency-free style.
3. **A claim's body is written in the same falsifiable, precise, numbered-statement style this
   portfolio's own patent claims use** -- not legal claim language, but the same discipline: state
   exactly what is guaranteed (inputs, outputs, side effects, error behavior, invariants), not what
   the function is "for" or how it's implemented. A prototype claim
   (`hams_com/ham_logbook/claims/compute_callsign_stripped.md`, documenting
   `ham_logbook:compute_callsign_stripped`) is committed alongside this ADR as the worked example.
4. **Mechanical freshness checking, not semantic verification.** `hams_shared/tools/
   check_claims_freshness.py` (paired with `test_check_claims_freshness.py`) recomputes each
   claimed anchor's current source-span hash and compares it to the claim's own recorded
   `code_hash`. A mismatch means the function's text changed since the claim was last confirmed --
   it does NOT mean the claim's prose is wrong, only that nobody has looked since the code moved.
   Actually confirming a claim's prose still holds requires a reader (human or AI) comparing the
   two; this script only makes it impossible for that comparison to be silently skipped forever.
   An orphaned claim (its own anchor no longer exists anywhere in the codebase) is flagged
   separately, as a real error, not a mismatch.
5. **Python only, for now**, matching every other layer of this same anchor system's own
   incremental rollout (ADR 0090's test-anchor ratchet shipped Python-only before JS and Rust
   sub-tracks followed as separate, later work). `check_claims_freshness.py` reuses
   `check_function_test_anchors.py`'s own `_direct_functions`/`_function_span` so this check's
   notion of "the function's own text" never drifts from the existing test-anchor ratchet's.
   JS and Rust claims support (the latter has real function-span infrastructure already, via
   `rust_function_scan`, that a Rust claims checker could reuse the same way
   `check_rust_function_test_anchors.py` does) are real, named, not-yet-done follow-ons.
6. **No baseline or ratchet is needed, unlike ADR 0090's test-anchor mandate.** This is a brand
   new, opt-in mechanism with zero pre-existing claims at the time it ships -- there is no backlog
   to grandfather. Every claim ever committed is expected to have an accurate hash from the moment
   it lands; `check_claims_freshness.py` can fail hard on any mismatch from day one.
7. **Claims are opt-in per function, not mandated for every anchored function the way a test
   anchor is under ADR 0090.** Writing a genuinely precise, falsifiable claim takes real
   judgment and is worth the cost mainly for functions with real behavioral subtlety (the ones a
   bug-hunt pass would actually want to adversarially check) -- not for every trivial getter or
   compute method in the codebase. The bug-hunt skill (decision 8, below) is expected to be the
   main source of new claims, added where a pass judges one worthwhile, not a blanket sweep.
8. **The bug-hunt skill's own method is amended**: its step 2 ("State the claim") now checks
   first whether the unit being reviewed already has a `claims/*.md` file for the function in
   question. If one exists, read it (and check whether `check_claims_freshness.py` already flags
   it stale before trusting it) instead of re-deriving a claim from scratch. If a pass reviews a
   function worth having a durable claim and none exists yet, write one in the module's own
   `claims/` directory, in the same numbered-statement style, with an accurate `code_hash` at the
   time of writing -- so the value compounds across future passes the same way the bug-class
   checklist already does.

## Consequences
A new, real bug was found and fixed while prototyping this mechanism against the real codebase,
itself a small demonstration of the method: `check_claims_freshness.py`'s first draft hashed
*any* function whose span contained matching anchor text, which silently included a test file's
own `# Tests [@ANCHOR: name]` line -- a reference to the anchor, not a declaration of it -- and
let a later-scanned test function's body clobber the real source function's hash under the same
key. Fixed by reimplementing `verify_anchors.py`'s own base-declaration-vs-link prefix
classification (`_is_base_anchor_declaration`) rather than trusting a bare pattern match, with a
regression test (`test_a_tests_link_to_an_anchor_does_not_clobber_the_base_declarations_hash`)
added before this ADR was considered done.

`check_claims_freshness.py` is not yet wired into `run_linters.py` as of this ADR -- that file had
live, concurrent edits from another session the same night; wiring it in (as its own numbered
step, following the existing `check_shebang.py`/`check_function_test_anchors.py` invocation
pattern) is a real, named follow-on, not forgotten. `verify_anchors.py`'s own docstring should
gain an eighth rule documenting the optional CLAIM LINK, mirroring rule 4's DOC LINK phrasing, as
a real, named follow-on alongside the `run_linters.py` wiring. JS and Rust claims support, and any
future tooling to help a pass find which functions most need a claim, remain unstarted.
