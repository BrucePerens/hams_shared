---
name: patent-disclosure-pairs
description: "Bruce's patent material lives as paired disclosure and provisional-spec files that must both be updated together."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: c0284e30-6ccb-4ad4-9b91-456b9fb13774
  modified: 2026-09-08T19:03:51.392Z
---

# Patent disclosures and provisional specs are updated as a pair

In Bruce Perens's `hams_com` repository, the patent material under
`docs/proposals/patent_disclosures/` is kept as two parallel files per invention:
the numbered *disclosure* at the top level (for example
`29_unified_multi_mode_signal_id_and_closed_loop_correction.md`, written in an
explanatory engineering voice for counsel review) and the matching *provisional
specification* under `provisional_specs/` with the same number and name, written
in formal patent-specification style with a Claims section appended. Filed output
lives under `filed_applications/<number>_<name>/`.

When Bruce accepts a proposed change to patent material, he wants it applied to
**both** files, not just one. He stated this directly on 2026-09-08 ("Put it in
the disclosure and application"), so treat updating only one of the pair as an
incomplete job.

Two habits that follow from this:

- Design changes to the shipping code can invalidate limitations recited in these
  specs, so when a design change lands it is worth checking whether a spec's
  claims still read on the implementation. On 2026-09-08 a change gating the mode
  recognizers to a post-retune burst directly undercut spec 29's recitation of
  "continuously operating identification processes … executing primarily for a
  purpose other than supplying the correction computation." Bruce wanted that
  flagged before filing.
- Prefer adding an *alternative embodiment* over rewriting existing claim
  language. A provisional's value is disclosure breadth, so covering the new
  variant preserves priority without disturbing what is already drafted and
  reviewed.
