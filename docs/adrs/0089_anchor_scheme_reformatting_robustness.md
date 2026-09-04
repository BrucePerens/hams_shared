# ADR 0089: Anchor Scheme Robustness Against Reformatting

## Status
Accepted

## Scope
This ADR governs `check_burn_list.py`'s anchor mechanism -- the `[@ANCHOR: name]` tags that link
a bypass/exception declaration in source code (`# burn-ignore-financial`, `# audit-ignore-*`, an
Odoo XML tour-mandate comment) to a test or documentation citation proving it's justified. It does
not govern the separate `audit-ignore-*`/`burn-ignore-*` XML lookback mechanism
(`_xml_audit_lookback_start`), which already solves a related but distinct problem (a multi-line
XML comment's own start boundary) a different way, and is unaffected by this decision.

## Context
This codebase adopted `cargo fmt` project-wide (2026-09-04, same session), which raised a real
question: would a Python formatter reflow do the same kind of damage to `check_burn_list.py`'s own
anchor/tag-detection logic? Investigating found two distinct, real failure classes, both already
possible today without any new formatter:

1. **Bypass-declaration side.** Several checks (`visit_Try`'s `_get_service_uid` and catch-all
   handler checks, the weak-random import checks, the `hasattr()`/3-arg-`getattr()` introspection
   checks) read `self.lines[node.lineno - 1]` -- exactly one physical line -- looking for the
   matching ignore-tag. `node.lineno` is Python AST's own convention for where a call/statement
   *opens*, not where it closes. Wrapping a long call across multiple lines moves its own trailing
   tag comment onto a different line than `node.lineno` points at, producing a false positive on
   code that was already correctly justified and tagged.
2. **Test/documentation-anchor side.** `_verify_test_ast` and the orphan-bypass resolver both
   searched for `[@ANCHOR: name]` as a literal substring on a single line, then (for the
   test-anchor case) required that line to fall inside some enclosing `FunctionDef`'s own
   `lineno`..`end_lineno` range. This already tolerates internal reformatting of the function
   *around* the anchor comment reasonably well (the AST range moves with the function, not the
   anchor), but had no way to let one anchor explicitly claim a body of code larger or more
   specific than "whichever function this comment happens to sit inside" -- a real, named gap for
   documentation and multi-statement test cases.

## Decision

1. **Bypass-declaration side: extend the single-line check to the flagged AST node's own line
   span (`node.lineno` through `node.end_lineno`, both real Python AST attributes since 3.8), not
   a blanket rewrite of every ignore-tag check in the file.** A new `node_span_text(node)` helper
   on the AST-visitor class replaces `self.lines[node.lineno - 1]` at every site where the flagged
   construct is genuinely a `Call`/`Import`/`ExceptHandler` node whose own span can include a
   wrapped tag comment. The `.sudo()` `Attribute`-node check is deliberately left single-line: that
   node's own span never extends into the outer call chain (`.sudo()._generate(...)`) its
   substring match needs, and a real fix needs parent-statement tracking this visitor doesn't
   currently do -- left as a named, honest remaining gap, not silently assumed fixed alongside the
   others. `add_error`/`add_warning`'s own generic single-line `"burn-ignore" in
   self.lines[lineno-1]` fallback (used by roughly 100 other call sites that pass a bare line
   number with no node reference) is likewise left unchanged -- widening it would mean threading an
   `end_lineno` through every one of those call sites, a much larger, unbounded-scope change not
   taken on here.

2. **Test/documentation-anchor side: add an explicit multi-line BEGIN/END marker pair (open with
   `ANCHOR-BEGIN`, close with `ANCHOR-END`, same `name`), alongside the existing single-line
   `[@ANCHOR: name]` form, which remains fully supported and is not being deprecated.** (Written
   without their own literal brackets here on purpose -- two of them on adjacent lines is exactly
   the "stacked anchors" shape `verify_anchors.py`'s own dummy-test check looks for, a real false
   positive this ADR tripped on itself the first time it was drafted; see `check_burn_list.py`'s
   own source for the real, literal syntax.) BEGIN/END markers are immune to
   reformatting for the same reason any pair of standalone comment lines always is: a formatter
   never rewrites comment text or merges a freestanding comment into the code around it, no matter
   how many lines end up between the two markers. `_find_anchor_line` and
   `_anchor_citation_present` recognize both forms everywhere an anchor is searched for. A new
   `check_anchor_pairing()` validates every BEGIN has exactly one later END with the same name (and
   vice versa) per file, flagging a mismatch as a real `ORPHANED ANCHOR MARKER` error -- an
   authoring mistake, not a style nit, since an unpaired BEGIN would otherwise leave an anchor's
   claimed scope silently unbounded.

3. **The named cost is real and is not being hidden**: two markers instead of one is uglier code.
   Use the single-line form by default; reach for BEGIN/END only when an anchor genuinely needs to
   claim a body of code that doesn't correspond to a single line (a multi-statement test, a
   documentation block spanning several lines) -- not as a universal replacement.

## What this does not do
This ADR does not extend the anchor mechanism into a general code-coverage system -- an anchor,
BEGIN/END or not, is a static, human-authored *claim* that a body of code is tested/justified,
checked against a real but shallow AST-shape heuristic (does the cited test contain an
`assertRaises`, a logging call, etc.), never against whether the flagged lines actually *executed*
during a real test run. Widening the anchor mandate to more (or all) code, or cross-referencing
anchor spans against real `coverage.py` instrumentation to verify the claim is actually true, were
both raised as real, distinct follow-on ideas during this same discussion and are deliberately left
for a future ADR of their own -- each is a real scope-expanding policy/engineering decision, not a
small extension of the fix made here.

## Consequences
Both failure classes were fixed, verified with 13 new tests (all passing, 372 total) plus a full
before/after scan of both `hams_com` (1760 files) and `hams_open` (594 files) confirming
byte-identical output -- zero regressions against every existing anchor/tag usage in either repo.
Future dangerous-pattern checks added to `check_burn_list.py` that flag a `Call`/`Import`/
`ExceptHandler` node should use `node_span_text()` rather than reintroducing a single-line
`self.lines[node.lineno - 1]` read. Anchors that need to cover more than one function or a
non-function-scoped block should use `[@ANCHOR-BEGIN:]`/`[@ANCHOR-END:]` rather than relying on a
single-line anchor's incidental placement inside the right enclosing scope.
