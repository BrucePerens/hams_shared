---
name: patent-drafting-scope-traps
description: "Two specific \"scope trap\" patterns to avoid when drafting patent invention disclosures or provisional specifications -- required safety/oversight features and unnecessary multiplicity limits."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 58453db6-5ee8-4667-a749-b2d8dad1938a
  modified: 2026-08-26T01:18:04.056Z
---

When drafting a patent invention disclosure or provisional specification (as on hams.com's
`docs/proposals/patent_disclosures/` work), the user, Bruce Perens, identified two specific
patterns that unnecessarily and gratuitously narrow claim scope, discovered mid-session while
drafting a disclosure for an AI radio/ICS-forms assistant that originally described human approval
of AI-drafted content as a mandatory gate before any consequential action.

**Trap 1: describing a safety, oversight, or supervision feature as a required element of the
invention.** If a disclosure says a mechanism "requires human approval before any action" or
"never acts autonomously," a competitor can design around the resulting patent simply by omitting
that gate and making the system fully autonomous -- and equally, it would box out the patent
holder's own system from covering a future, more autonomous version of the same product. Bruce's
own words on why this matters: "We don't want this to be circumvented by someone making AI
autonomous, that is the wave of the future anyway." The fix is to describe such a feature as a
*configurable posture* spanning a range (for instance, from fully autonomous to fully human-gated,
selectable per deployment, per content class, or per significance level), with the recommended or
default posture stated as a deployment choice rather than an architectural requirement. The
invention should be the underlying mechanism (extraction, verification, provenance, whatever the
core technical contribution is), not any single point on the autonomy spectrum.

**Trap 2: unnecessary multiplicity limits.** Phrases like "monitors multiple channels" or "fuses
several heterogeneous sources" inadvertently require plurality when a single instance already
embodies the same inventive mechanism -- a competitor implementing the mechanism for exactly one
channel or one source would fall outside a claim drawn that narrowly. Bruce's framing: "nothing
should be specific to a number of things, one or many." The fix is to write "one or more" (or
equivalent) wherever a specific count was used descriptively rather than because the mechanism
genuinely requires plurality to make sense (a peer-to-peer handshake requiring at least two
parties, for example, is a structural necessity, not an arbitrary multiplicity choice -- that kind
of requirement is fine to keep).

Both patterns are worth actively scanning for near the end of drafting any new disclosure or
provisional spec, not just fixing when pointed out: read back through for "requires," "must,"
"never," "always," "mandatory," and "multiple/several/many" language, and ask in each case whether
the limitation is essential to the invention's function or just how the first embodiment happened
to be described.
