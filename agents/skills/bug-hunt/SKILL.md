---
name: bug-hunt
description: >-
  Find real software bugs across hams.com/hams_open at Sonnet/Opus budget instead of running Fable
  over the whole codebase. Packages the claim-then-verify method that found real production bugs
  during the 2026-09-08 patent re-verification passes (a security gap, silent-failure gates, a false
  regression test, an unenforced DB invariant, a missing score normalization, a WCAG violation, a
  credential-custody gap, a clock-skew bug), plus a growing checklist of the specific bug classes
  found so far and candidate linter rules. Triggers: bug hunt, code audit, find bugs, adversarial code
  review, code integrity pass.
version: 1
---

# Bug Hunt: the claim-then-verify method, at Sonnet/Opus budget

## Origin

During an overnight adversarial re-verification of `hams_com`'s patent-disclosure portfolio
(2026-09-08), a strong reviewer model (Fable) was pointed at one bounded disclosure at a time and
told to independently re-derive whether the filed patent claims actually matched what the real code
does. Every single pass found at least one real, previously-unnoticed defect -- and several found
genuine production bugs having nothing to do with patent law: a network-exposed credential-enrollment
route accepting connections from the whole LAN instead of just loopback; a periodic UI refresh that
silently reset the user's pan/zoom every 60 seconds; a "regression test" that passed identically
whether the bug it was meant to catch was present or not; a claimed database constraint that didn't
actually exist in the schema; a scoring function missing a normalization that let a near-empty input
win an argmax it shouldn't; an `aria-hidden` container that still left its own focusable controls
keyboard-reachable; a `.p12` credential file and its password created with default (world/group
readable) permissions before being `chmod`'d afterward, and never zeroed on disk; a watermark that
advanced to "now" with no allowance for clock skew against the remote server it was tracking.

Fable's own strength wasn't really the mechanism here. What found these bugs was: **state a precise,
falsifiable claim about what the code does, then adversarially verify that claim against the actual
code, trusting neither the comments nor a linter's own clean verdict.** That loop runs fine on
Sonnet. Running Fable over the whole codebase would burn the token budget for
one pass; this skill exists to get comparable bug-finding power at Sonnet/Opus cost by (a) packaging
the method itself, (b) carrying forward the specific bug-class checklist those passes already
discovered so a cheaper model doesn't have to rediscover each pattern from scratch, and (c) pushing
recurring bug classes out into cheap, durable, mechanical linter checks instead of relying on a future
LLM pass to catch the same thing again.

## The method

1. **Pick one bounded unit.** One Odoo module, one daemon/crate, one JS component cluster, one file
   cluster around a single feature -- never "the whole codebase" in one pass. This is what let each
   patent-disclosure pass go deep instead of skimming.
2. **State the claim.** First check whether the function already has a durable one: if its own
   base anchor (`# [@ANCHOR: name]`) has a file at `<module>/claims/<name>.md` (ADR 0091), read it
   -- that's this step already done by an earlier pass, not something to re-derive. Check whether
   `check_claims_freshness.py` flags it stale (the anchored function's source changed since the
   claim's own `code_hash` was last confirmed) before trusting it uncritically; a stale claim still
   tells you what the function *used to* guarantee, which is itself useful context for spotting
   what changed. If no claims file exists, state the claim the old way -- from a comment/docstring,
   implied by the function's name, or asserted by a test -- or infer the intended behavior from
   context if nothing states one (you can't adversarially verify against nothing). If the function
   turns out to have real behavioral subtlety worth a durable record, write a claims file for it
   before finishing the pass (same numbered-statement style as an existing one, or as this
   portfolio's own patent claims; see the worked example at
   `hams_com/ham_logbook/claims/compute_callsign_stripped.md`), with an accurate `code_hash` -- so
   the next pass gets this step for free too.
3. **Independently re-derive, don't trust the account.** Check the claim against the actual code, not
   against what a comment, docstring, spec, or prior review said about the code. Every real bug found
   tonight was found by someone re-deriving the fact from source rather than trusting an existing
   description of it. Do this by reading -- tracing call sites, checking a comment's own factual claim
   against the file it cites, following a value from where it's set to where it's used -- not by
   running anything.
4. **Do not run the test suite as part of this method.** This project already has ample, separate
   test-running coverage (CI, the existing Odoo/Rust/JS test-runner conventions) -- re-running tests
   is not this skill's job, and a bug-hunt pass that reaches for a test runner first is doing someone
   else's already-covered work instead of its own. This skill's own value is the claim-then-verify
   *reading*: stating a precise claim and checking it against the code. If a claim's own truth
   genuinely can't be settled by reading alone, say so explicitly in the report and name what running
   the existing test suite would need to confirm -- don't spend the pass's own effort standing up a
   test environment to do it yourself. (If you do encounter a test directly, e.g. because a claim
   review requires reading one, never make a failing test pass by weakening it -- deleting, skipping,
   disabling, or mocking around logic that's supposed to be genuinely exercised. This project has a
   real, documented incident behind that rule -- see `hams_shared/tools/linter_rules.md`'s
   anti-test-avoidance rules, added after a prior AI agent gamed a "make tests pass" instruction by
   deleting the failing tests instead of fixing the code.)
5. **Classify every mismatch found** against the Known Bug Classes list below. If it fits, cite the
   class. If it's genuinely new, add a new entry (see "Growing this list").
6. **For every mismatch, ask whether a cheap mechanical check could catch this class in the future.**
   See "Linter-rule candidates" below. Propose or add the check rather than only fixing the one
   instance -- this project's own standing convention is to turn a found bug pattern into a durable
   linter rule, not just patch the instance.
7. **Report findings concretely**: file, one-sentence summary, a concrete failure scenario (inputs/
   state that trigger it). Use the caller's `ReportFindings` tool if available.

## Known bug classes

A living list. Each entry: a short name, what to look for, and where it was actually found (so a
future pass has a concrete anchor, not just an abstract description).

1. **Vacuous/dead conditional branches** (generalizes *Ex parte Schulhauser* from the patent-claim
   world). A branch or step is gated on a condition that, given how the code is actually invoked in
   practice, may never occur or is never checked at all -- so the branch's own distinguishing logic
   carries no real weight and can be silently absent without anything failing. *Found*: a sideband/
   frequency-correction step whose "verify the receiver's actual mode first" branch was never actually
   reached by the code path that mattered; a claim requiring a live mode query that the real code
   never performed for its own anchor case.
2. **A test whose only assertion can't distinguish the bug from its absence.** The test "passes"
   whether the guarded behavior is present or the bug it's meant to catch is reintroduced -- usually
   because an error is silently swallowed (e.g., into `console.error`) and the assertion checks a side
   effect that's identical either way. Recognizable by reading alone in most cases: trace what the
   assertion actually checks back to the code path it's meant to guard, and ask whether that value
   would differ if the guard were deleted -- if not, the test can't be discriminating. Confirming it
   for certain means running the test under both conditions, which is the existing test-running
   infrastructure's job (see method step 4), not this skill's own -- name the suspicion and what
   running it would need to show, rather than standing up a test run yourself. *Found*:
   `ham_world_map_view.test.js` -- a test asserting `state.satellites.length === 0` passed identically
   whether an accessibility-gating check existed or was deleted, since the fetch failure it triggered
   was caught and logged, not surfaced.
3. **Claimed test/verification coverage that doesn't actually exist.** A comment, docstring, or spec
   claims something is "tested," "verified," or "covered," for a code path no actual test exercises.
   Confirm by grepping the real test files for the referenced function/branch -- don't trust the claim.
   *Found*: a specification said a receiver-tuning command path was "tested directly at every stage";
   in reality only the pure helper functions were unit-tested, not the command-dispatch code itself.
4. **A stated invariant that isn't actually enforced in the data layer.** A comment or business-logic
   assumption says "exactly one of A or B is set," "these are mutually exclusive," etc., but the
   schema has no constraint enforcing it -- verify against the actual schema (`required=`, `CHECK`,
   `models.Constraint`, unique constraints), not just the field definitions or a comment. *Found*: two
   foreign keys on a progress-tracking model were both `required=False` with no constraint, despite the
   feature's own correctness argument resting on exactly one being populated.
5. **Silent-failure gates.** A feature quietly no-ops, rather than erroring or logging, when a
   dependency fails to initialize -- e.g., an NLP/ML model failing to load silently disabling a whole
   downstream feature with no user-visible signal. *Found*: a callsign-extraction NLP gate in
   `web_shack.js` that, on model-load failure, silently disabled extraction on both audio paths while
   an unrelated auto-fill feature kept working, with nothing surfacing the failure.
6. **Cross-document/cross-section self-contradiction.** Two places describing the same mechanism (a
   summary vs. a detailed section; a doc vs. the code) disagree, usually because one was updated and
   the other wasn't. Diff both descriptions against each other explicitly rather than reading each in
   isolation. *Found*: a specification's own Summary of the Invention still described a bandwidth-
   search mechanism its own Detailed Description, four paragraphs later, said had been tested and found
   not to work.
7. **A resource teardown-and-rebuild that discards user state unnecessarily.** A periodic refresh path
   recreates a stateful UI object (a map, a chart, a form) from scratch on every tick, discarding
   interaction state (pan/zoom, scroll position, an open dropdown) a smarter incremental update
   wouldn't need to touch. *Found*: a propagation-forecast dashboard rebuilding its entire Leaflet map
   instance every 60 seconds, resetting the user's own pan/zoom each time.
8. **A first-render path that never actually fires.** Code inside a lifecycle hook that runs before the
   DOM/target element exists silently no-ops on the very first render; the bug hides because later,
   interval-triggered renders work fine and mask that the initial one never did anything. *Found*: a
   map-drawing call made from inside `onWillStart`'s own fetch, before the map container existed in the
   DOM, meaning the first fetch's result was never drawn.
9. **An accessibility attribute placed on the wrong element (WCAG 4.1.2 class).** `aria-hidden="true"`
   (or similar) on a container that is an ancestor of the library's own focusable controls -- the
   controls remain keyboard-reachable despite being marked hidden from assistive technology. Check any
   `aria-hidden` next to a third-party widget's own init call (map libraries, chart libraries, embedded
   players) for exactly this. *Found*: Leaflet map containers in two modules with `aria-hidden="true"`
   while Leaflet's own zoom/attribution controls remained focusable descendants.
10. **Missing normalization in a scoring/comparison function.** A length- or scale-dependent quantity
    feeds directly into an argmax/threshold comparison without normalization, letting a degenerate
    input (near-empty, all-zero, very short) win by default. *Found*: a phonotactic-plausibility scorer
    that returned `0.0` -- the maximum attainable score -- for any decode under two tokens, letting a
    near-silent or noise decode win a search outright.
11. **A credential/secret handled with a permission or erasure gap.** Two related sub-patterns: (a) a
    secret file created with the process's default (permissive) umask and only `chmod`'d to restrictive
    permissions *after* creation, leaving a real window of exposure -- the fix is to create the file
    already-restricted (open with the target mode set atomically), not create-then-restrict; (b) a
    secret's bytes (a `.p12`, a password, a private key) left un-zeroed in memory or on disk after use,
    readable from a core dump or a forensic disk read even after "deletion." *Found*: a `.p12` export
    and its one-time password both created with default permissions before a later `chmod(0o600)`, and
    neither zeroed before deletion.
12. **A network-exposed handler with weaker access restriction than its own purpose implies.** A route
    meant to deliver a credential or perform a sensitive action to/for the operator's own machine
    accepts requests from the wider network instead of restricting to loopback or another narrow scope
    matching its actual intended caller. *Found*: a browser-credential-enrollment route accepting
    connections from the whole local network (RFC-1918 plus an externally-reachable NAT-token bearer)
    instead of loopback-only.
13. **A time-based watermark/cursor with no clock-skew safety margin.** A polling or sync mechanism
    advances its own "last seen" watermark to local "now" after a successful poll, with no allowance
    for skew between this machine's clock and the remote server's -- a same-day event the remote server
    timestamps by its own clock can permanently fall behind a watermark this machine already advanced
    past. Fix: trail "now" by a safety margin (a day, an hour -- whatever bounds plausible skew plus
    remote-side processing delay) rather than advancing to the exact current time. *Found*: a
    confirmation-poll watermark for an external service that advanced to this machine's own "today"
    after every successful poll with no margin at all.
14. **An overbroad rule/check that bans a whole construct instead of the specific failure mode it
    was actually written to prevent.** Applies the same claim-then-verify method to a rule or linter
    check itself, not application code: ask what concrete incident or defect the rule exists to
    prevent, then check whether the rule as written is narrowly targeted at that failure mode or bans
    something broader (and often more useful) along with it. *Found*: `linter_rules.md`/
    `check_burn_list.py` banned *any* assertion inside a `for`/`while` loop in a test, written after a
    real incident (an AI agent gaming a "make tests pass" instruction). The actual failure mode was a
    loop that could silently iterate zero times, skipping the assertion inside it entirely -- not the
    loop construct itself. A loop that provably iterates (its own non-emptiness asserted first) is
    legitimate and often better coverage than a single hand-picked assertion. Refined the rule (and its
    AST enforcement) to require the exception be earned -- an explicit assertion that the loop's own
    iterable is non-empty -- rather than banning loops outright.
15. **A doc's claimed enforcement doesn't match what the actual checker implements.** A rules/style
    document (a linter guide, an ADR, a README) asserts a checker "physically blocks" or "strictly
    enforces" some pattern; verify by reading the actual checker script, not by trusting the doc's own
    description of itself -- this is the same failure mode as bug class 3, applied to tooling docs
    instead of code specs. Not yet empirically checked against `hams_shared/tools/linter_rules.md`'s
    own claims about `check_burn_list.py`/`verify_anchors.py` as of this writing -- a good first target
    for a future pass using this skill.

16. **A naive text/pattern match conflates a declaration with a reference to it.** A marker syntax
    used for more than one purpose (a base declaration vs. a link/citation of that same marker
    elsewhere) gets matched by a single pattern with no regard for which role it's playing in
    context -- silently attributing a reference's own content to the thing it merely points at.
    *Found*: prototyping `check_claims_freshness.py` (ADR 0091), a naive scan hashed *any* function
    whose span contained `[@ANCHOR: name]` text, including a test file's own
    `# Tests [@ANCHOR: name]` citation of that anchor -- letting the test function's body silently
    clobber the real source function's hash under the same key, since both scanned as "contains
    this anchor." Fixed by reimplementing `verify_anchors.py`'s own prefix-based declaration-vs-link
    classification rather than trusting a bare pattern match.

### Growing this list

When a bug-hunt pass using this skill finds a genuinely new bug class -- not a fresh instance of one
already listed above -- **add it to this file before finishing the pass**, in the same format: a short
name, what to look for in general terms, and the concrete instance that was actually found. Edit
`hams_shared/agents/skills/bug-hunt/SKILL.md` directly (this file -- moved here from hams_com's own
`.claude/skills/` in September 2026 since the method applies equally to hams_open). Don't just
mention the new class in a report that nobody reads later -- the value of this list is that it
compounds across passes and across sessions, which only works if it's actually kept current. If two
entries turn out to describe the same underlying pattern, merge them rather than leaving
near-duplicates.

## Linter-rule candidates

For each bug class where a cheap, mechanical, low-false-positive check is plausible, add a real check
under `hams_shared/tools/check_*.py` (paired with `test_check_*.py`, wired into `run_linters.py` --
follow the existing pattern, e.g. `check_shebang.py`) instead of relying on a future LLM pass to catch
it again. Already implemented, from tonight's findings:

- **`check_window_fetch_reassignment.py`** (bug class 2's specific trigger): flags `window.fetch =
  ...`/`globalThis.fetch = ...` direct reassignment in `*.test.js` files, which throws under the real
  `@odoo/hoot` test harness (hoot's mocked `window` makes `fetch` read-only) -- the sanctioned
  alternative is this repo's own `mockFetch()` helper. This exact pattern was found independently in
  17+ tests across four modules the same night, none of which had ever actually been run. Now wired
  into `run_linters.py` (a separate, concurrent session finished the wiring, added `globalThis.fetch`
  coverage the original regex missed, and excluded stale `.claude/worktrees/` checkouts from the walk
  so it stops flagging frozen, already-fixed historical copies).
- **`check_hoot_runner_coverage.py`** (bug class 2's *structural* enabler, not just its trigger): the
  window.fetch bug above didn't just happen to go unnoticed -- in 4 of the modules it hit, the
  `*.test.js` file was registered in `__manifest__.py`'s `web.assets_unit_tests` bundle (syntactically
  valid, bundled, loadable by hoot) with *nothing in the Python test suite ever calling `browser_js()`
  to actually run it*. A hoot suite that's never executed can carry any bug indefinitely, not just this
  one. This check flags any module with a registered `*.test.js` file and no `tests/test_*.py` runner
  (a `browser_js(...)` call with a `"[HOOT]"` success_signal) anywhere in the module -- module-level,
  not per-suite, so a module with ten hoot files and a runner covering only one tag still passes; that
  finer-grained gap is real but needs a JS-side tag cross-reference this check doesn't attempt. Wired
  into `run_linters.py`. 13 modules already had this gap when the check was added (`ham_classifieds`,
  `ham_club_management`, `ham_dns`, `ham_events`, `ham_forum_extension`, `ham_moderation`,
  `ham_onboarding`, `ham_propagation`, `ham_testing`, `ham_training`, `ics_forms`, `caching`,
  `knowledge`) -- grandfathered via a `# burn-ignore-hoot-runner-coverage` marker in each
  `__manifest__.py` rather than blocking on fixing all 13 in one session; each marker must be removed
  once that module gets a real runner.
- **`check_burn_list.py`'s loop-wrapped-assertion rule, refined rather than replaced** (bug class 14):
  the existing AST check banning any assertion inside a `for`/`while` loop in a test was narrowed to
  its actual failure mode (a loop that can silently iterate zero times) via a new
  `_loop_iteration_is_asserted()` helper, rather than leaving the overbroad ban in place or removing it
  outright. A worked example of bug class 14 itself, found by asking what concrete incident the
  existing rule was written to prevent rather than accepting its stated scope at face value.

- **`check_claims_freshness.py`** (ADR 0091, supports bug class 6 and this skill's own claim-reuse
  step 2): mechanically flags a function claim (`<module>/claims/<anchor_name>.md`) whose recorded
  `code_hash` no longer matches its anchored function's current source -- the code changed since
  the claim was last confirmed, so the claim needs a fresh read before being trusted. Not a
  semantic check (it can't tell you the claim's prose is wrong, only that nobody's looked since the
  code moved); not yet wired into `run_linters.py` as of this writing, since that file had live
  concurrent edits from another session the same night this was built -- finish wiring it in as its
  own numbered step, same pattern as `check_shebang.py`, when next touching that file.

Candidates not yet implemented, evaluated and left as future work because a reliable low-false-positive
mechanical check isn't obviously cheap:

- Bug class 4 (unenforced mutual-exclusivity invariant): a grep for field pairs whose names/docstrings
  use exclusivity language ("exactly one of," "mutually exclusive") without a matching
  `models.Constraint`/`_sql_constraints` nearby could flag *candidates* for human/LLM review, but can't
  reliably confirm a real violation on its own.
- Bug class 9 (`aria-hidden` on a focusable-control ancestor): a check that flags `aria-hidden="true"`
  co-occurring with a known map/chart-library init call in the same template/component would have real
  false-positive risk without deeper DOM analysis; flag for manual review rather than hard-fail.
- Bug class 3 / bug class 14 (claimed-but-unverified test coverage, in code comments or in
  `linter_rules.md`'s own claims): the patent pipeline already has a working pattern for this
  (`verify_filed.py`'s `DRAFTING_TERMS` word list, extended this session to catch narration/anticipation
  leakage) -- the same word-list approach, generalized to source-code comments claiming "tested"/
  "verified"/"covered by X," is plausible but needs its own false-positive tuning before shipping (the
  `DRAFTING_TERMS` list itself already collided once with legitimate text tonight -- see
  `verify_filed.py`'s own history -- so test any new word-list rule against the whole codebase before
  committing it, not just the file that motivated it).

When a bug-hunt pass adds a new linter rule, record it in this section (implemented vs. candidate),
following the same format.

## Cost control / model routing

- **Chunk by bounded unit**, matching natural boundaries (one module, one daemon, one crate, one JS
  component cluster). Never review "the whole codebase" in a single pass.
- **Default to Sonnet** for the per-unit claim-then-verify pass. This is where most of the volume goes,
  and the method above -- not raw model strength -- is what did the actual bug-finding tonight.
- **Escalate to Opus** for: (a) triage of Sonnet's own flagged candidates before spending fix effort on
  them, (b) units that are security-sensitive (network-exposed handlers, credential/auth code, anything
  touching real money or exam/certification integrity), or (c) a unit where Sonnet's own pass reports
  genuine uncertainty rather than a clean verdict.
- **Reserve Fable** (or another top-tier reviewer) for the narrowest, highest-stakes slice -- an actual
  patent claim about to be filed, a security boundary about to ship, or a unit already flagged twice by
  cheaper passes as still uncertain. Not for blanket coverage.
- **Isolate parallel passes.** Launching several review/fix agents in parallel against the same shared
  working tree risks exactly the kind of concurrent-edit corruption a plain adversarial-review pass hit
  earlier the same night (a naive text-replacement script corrupted a 4,883-line file to 37 million
  lines when two edits raced). Either run bounded-unit passes one at a time, or launch them with true
  git-worktree isolation (`Agent`'s `isolation: "worktree"`) and merge each branch back deliberately,
  watching for shared files (a central claims/status doc, a shared linter-rules doc, a README) that
  more than one unit's pass might want to touch -- have each pass write proposed changes to those into
  its own report instead of editing them directly, and apply them centrally afterward. Note that a
  symlinked sibling repo (e.g. `hams_shared`, symlinked into both `hams_com` and `hams_open`) is *not*
  isolated by a git worktree even though the rest of the checkout is -- any pass touching files there is
  touching the one real shared copy, no matter how many worktrees are running in parallel.
