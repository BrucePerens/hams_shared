# ADR 0090: Universal Function Test-Anchor Ratchet

## Status
Accepted

## Scope
This ADR governs the five real open questions `ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md` (a
`hams_com/docs/proposals/` implementation plan, not a standing policy document itself) left for
Bruce's own decision, all resolved in the same conversation as ADR 0089. It extends, rather than
replaces, `MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md`'s existing anchor-traceability mandate and
`verify_anchors.py`'s existing enforcement of it.

## Context
`verify_anchors.py` already enforces *consistency* of anchors that exist (a declared anchor must
be tested, documented, and bidirectionally linked) but never required an anchor on a function in
the first place -- anchoring was an authoring judgment call ("core features," per the tool's own
error text). `ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md` named five real decisions this scope
question, and four adjacent ones, needed before any real sweep could start.

## Decisions

1. **Scope: every function gets a real test anchor.** Bruce's own words: "I think we should test
   every function, we can afford to have an AI write such tests." Documentation is a separate
   question from testing (see decision 1b) -- this decision is about the test-traceability half
   (ADR-0054's bidirectional Tests/Verified-by link) applying universally, not about every
   function needing a `docs/stories/` entry too.

   **1b. Documentation is routed by audience, not exempted by function.** Bruce's own correction to
   an earlier, wrong design in this same conversation (a proposed `INFRA_` prefix that would have
   *exempted* infrastructure functions from documentation entirely): "we've still documented both"
   -- an infrastructure function's anchor should be cited in the module's own `README.md` (already
   a recognized "contract" location in `verify_anchors.py`, exempting it from the
   `docs/stories/`/`docs/journeys/` requirement specifically, confirmed directly against the real
   tool rather than assumed); a user-visible feature still belongs in `docs/stories/`/
   `docs/journeys/`, or `data/documentation.html` for a `UX_`-prefixed anchor per the existing rule
   5. Both are real documentation, routed to the audience that actually reads each one -- neither
   is skipped.
2. **Coverage instrumentation (Stage 2 of the plan): build all three sub-tracks (Python, Rust,
   JS) together, in no particular order.** Bruce's own words: "the order doesn't matter, again
   this is just AI time." Not yet started as of this ADR.
3. **AI-assisted anchor-matching (Stage 1's own real risk): no mandatory human review.** Bruce's
   own words: "I'm going to trust the AI to do this without human review, if there are problems,
   we will hit them. AI can do this much better than a human, I would get bored and defocused
   before I was well through the task." A wrong anchor is still a real risk (see the plan's own
   "do not auto-insert a plausible-but-unverified link" step) -- this decision is that the
   mitigation is catching problems as they surface, not a review gate in front of every link.
4. **Coverage threshold (Stage 3): binary ("was this span touched at all by any test") for the
   first real gate, with branch coverage as the real, named follow-on once Stage 2 produces actual
   data to check it against.** A fixed percentage threshold (e.g. 80%) was considered and
   deliberately not chosen -- it penalizes normal defensive/fail-fast code without
   `# pragma: no cover` hygiene and is the most arbitrary of the options weighed. Not yet built as
   of this ADR (depends on Stage 2's own coverage instrumentation existing first).
5. **A CI-enforced ratchet for new code starts now; the backlog sweep is deferred to when
   `ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md`'s own Stage 1 is actually executed.** Bruce's own
   words: "institute the CI requirement for new code now, we will do the sweep when we get to that
   proposal." Built as `hams_shared/tools/check_function_test_anchors.py`: walks every module-level
   function and class method in git-tracked, non-test, non-tools/scripts `.py` files (nested
   closures excluded -- not independently testable units); a baseline JSON snapshot, generated once
   from the real tree's current state (422 pre-existing gaps in `hams_open`, 771 in `hams_com`,
   both committed alongside this ADR), grandfathers in every function that was already unanchored;
   anything unanchored that is NOT in the baseline -- new code, or existing code edited in a way
   that lost its anchor -- is a real, new CI failure. Wired into `run_linters.py` as step 38.
   **Python only for now**: `verify_anchors.py`'s own anchor recognition (which this ratchet reuses,
   `ANCHOR_PATTERN`) already covers `.py`/`.js`/`.xml`/`.html`; Rust (`.rs`) functions are not
   scanned by any anchor mechanism yet -- a real, named, not-yet-done follow-on, not silently
   assumed included.

## Consequences
New Python functions, or existing ones edited without preserving their anchor, now fail CI
(`check_function_test_anchors.py`, run_linters.py step 38) unless a real base anchor plus a real
`# Tests [@ANCHOR: ...]` link exists. The pre-existing backlog (1193 functions across both repos,
combined) does not need to be closed before this lands -- it's grandfathered into the committed
baseline files (`hams_shared/tools/function_test_anchor_baseline_{hams_open,hams_com}.json`) and
tracked as the real, sized starting point for whenever `ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md`'s
own Stage 1 sweep actually runs. Documentation routing (README.md for infrastructure,
docs/stories/journeys or data/documentation.html for user-visible features) uses mechanisms
`verify_anchors.py` already had -- no new exemption class, no new prefix convention, and no change
to `verify_anchors.py`'s own documentation-gap logic was needed once the routing was understood
correctly. Rust and JS function-level test-anchor coverage, and all of Stages 2-4 of the plan
itself, remain real, unstarted follow-on work.
