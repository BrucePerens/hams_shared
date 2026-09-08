---
name: hams-secrets-directory-convention
description: "On hams.com/hams_open, credential/token files (API keys, service credentials) live under ~/.secrets, outside any git repo."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 5ef88eba-a768-4870-9ddb-1f49c09220f5
  modified: 2026-09-01T00:44:43.682Z
---

On hams.com/hams_open, Bruce's standing convention for where credential and token files live on the dev box is `~/.secrets/` (e.g. `~/.secrets/cloudflare_hams_com.env`, mode 400, `KEY=value` shape) — outside any git repository, never committed. When looking for whether a given third-party service's credentials already exist on the box (an AWS key, a Cloudflare token, an API key for some other integration), check `~/.secrets/` first before concluding none exists or that a new one needs to be created. Bruce stated this directly: "Remember ~/.secrets as a home for these tokens," confirming it as the intended standing location, not just an incidental prior choice.
