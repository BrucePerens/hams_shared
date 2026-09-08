---
name: hams-lgpl-license-ok
description: "On hams.com/hams_open, LGPL-licensed third-party code is fine to use/vendor -- only GPL (and stronger copyleft) is the real concern."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 5ef88eba-a768-4870-9ddb-1f49c09220f5
  modified: 2026-09-01T04:33:03.682Z
---

On hams.com/hams_open (the proprietary, trade-secret-licensed codebase, `hams_com`/`hams_open`/the
`hams_local_relay` daemon), the user has explicitly stated: "We don't have a problem using LGPL
code." This came up while investigating whether a permissively-licensed WSPR decoder implementation
existed as an alternative to vendoring GPL-licensed reference code (matching the reasoning already
established in `daemons/ham_digital_modes/src/wspr.rs`'s own module doc, which treats GPL as
"incompatible with vendoring into this proprietary, trade-secret-licensed codebase"). The real
candidate found was Phil Karn's (KA9Q) `libfec`, LGPL-licensed -- Claude had been treating this as an
open legal question needing Bruce's own judgment call (since LGPL's obligations differ meaningfully
from GPL, especially around static vs. dynamic linking, and involve real license-compliance
nuance a coding session shouldn't resolve unilaterally). Bruce's answer settles that specific
category of question going forward: LGPL dependencies are acceptable to vendor/link into this
codebase without treating it as a blocking legal question each time. This does not by itself
resolve GPL (still treated as incompatible, per `wspr.rs`'s own existing precedent) or stronger
copyleft licenses -- the distinction that matters is specifically GPL-and-up vs. LGPL-and-more-
permissive, not "any copyleft is fine" or "only fully permissive licenses are fine."
