---
name: hams-codec2-test-wav-provenance-cleared
description: "Bruce has cleared the Codec2/FreeDV team's own donated voice-sample WAVs as fine to use/derive fixtures from in hams_open."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-04T04:09:58.125Z
---

On hams.com/hams_open, during work on an independent Rust port of Codec2's 3200bps mode
(`daemons/ham_digital_modes/src/codec2_3200/`), a set of five local 8kHz test WAV files was used to
generate committed test fixtures (`tests/fixtures/codec2_3200/`): `brian_g8sez.wav`,
`david_vk5dgr.wav`, `k0pfx_mel.wav`, `mooneer.wav`, `peter.wav`. Their filenames match the callsigns
of the Codec2/FreeDV project's own leadership team -- David Rowe VK5DGR, Mooneer Salem K6AQ, Mel
Whitten K0PFX, Brian Morrison G8SEZ, Peter Marks VK3TPM. A check of `drowe67/codec2`'s own `raw/`
directory on GitHub found it does NOT contain these five files (that directory uses a different,
unnamed sample set), so their exact origin repo/path and license terms could not be independently
confirmed by searching, and this was flagged to Bruce as an open question before committing derived
data from them to the public hams_open repo.

Bruce's own resolution, based on his personal knowledge of the project and its people (verbatim):
"I'm sure they are fine. They are samples of team members that would not protect their own samples
to the detriment of the project." He separately noted that David Rowe also maintains a longer-standing,
separate sample set with generic names like "male"/"female" -- distinct from these five
personally-named files, so that other set's own provenance doesn't transfer to these.

The generalized lesson: when a fixture/data provenance question in hams_open turns out to hinge on
Bruce's own personal knowledge of specific people or a specific project's norms (rather than
something checkable via public search), ask him directly rather than treating an inconclusive web
search as the final word -- he may have context (personal acquaintance, project history) no search
can surface. For this specific set of five Codec2/FreeDV team-member WAVs, the provenance question is
resolved and does not need to be re-flagged in future hams_open sessions.
