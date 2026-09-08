---
name: hams-do-all-viable-alternatives-not-just-one
description: "On hams.com/hams_open, when multiple alternatives are all viable, do all of them instead of picking one and routing the rest as a question."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-08T17:15:37.412Z
---

On a 2026-09-08 night-shift session on hams.com/hams_open, Claude root-caused a real Odoo-core bug
(hoot's own `navigator` mock in `hoot/mock/navigator.js` omits `navigator.platform` from its
safe-delegation list, so any application code reading `.platform` during a hoot test run throws
`TypeError: Illegal invocation`) and logged it in `BRUCE_ACTION_ITEMS.md` as a question: "your call
whether to carry a local patch to the vendored Odoo package... or search Odoo's own issue tracker
and file/track it upstream instead." Bruce's correction, verbatim:

> "Claude, you often defer something to a question when performing all of the alternatives is a
> perfectly viable answer. This latest example is the question of patching the Odoo mock tour bug
> or filing an error upstream. Do both. Do the patch to fix the current problem so we have no tour
> errors, and create a bug report document for me to file. Memorize this approach so that you can
> be more self-guided next time."

The generalized lesson: when Claude finds itself framing a decision as "A or B, your call" and both
A and B are independently safe and valuable to do (neither forecloses or undermines the other, and
neither requires information only Bruce has), the default should be to just do both rather than
present it as a single-choice question. A patch-now-and-file-upstream-too is the clearest shape of
this: applying a local fix doesn't stop the upstream report from being useful, and filing upstream
doesn't require waiting on the local fix. Genuine either/or decisions -- where doing one materially
forecloses, contradicts, or wastes the other, or where the choice depends on a business/product
judgment only Bruce can make (e.g. real product-direction calls, not engineering execution
choices) -- still belong to Bruce and should still be surfaced as a real question. But "which of
these several independently-fine actions should I take" is usually not that kind of question; it's
usually an invitation to do all of them. This sits alongside (not a replacement for) the existing
`hams-dont-be-too-cautious-about-originating-work` memory: that one is about not waiting for
permission to originate work at all; this one is specifically about not artificially narrowing a
multi-option situation down to one option when nothing forces that narrowing.
