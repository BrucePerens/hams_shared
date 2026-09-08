---
name: hams-apt-get-autonomy
description: Standing permission to install dev/test dependencies and major facilities on hams.com/hams_open without asking each time.
metadata: 
  node_type: memory
  pinned: false
  originSessionId: a5bd8915-8036-4ef1-8db6-088f583dcf5c
  modified: 2026-08-21T16:22:35.351Z
---

On the hams.com/hams_open/hams_shared codebase, the user has given blanket, standing permission — explicitly stated to apply across multiple future sessions, not just the one it was granted in — to run `sudo apt-get install` for reasonable development and testing dependencies (e.g. protocol daemons like Direwolf/pat needed to verify Rust binary-discovery code against real binaries, certificate tooling, DSP libraries) without asking for confirmation before each individual package. This was granted after the user was asked directly and chose "blanket OK for multiple sessions" over "ask each time." Still mention what was installed and why when reporting the work, and this doesn't extend to destructive operations, unrelated system configuration changes, or packages outside the reasonable-dev-dependency category — those still warrant asking first per the general safety-and-confirmation practice.

Confirmed and broadened during a later night-shift handoff (2026-08-21): "You have approval to install major facilities." This extends the same standing permission beyond `apt-get` packages to larger installs needed for real dev/test work during an unsupervised session — additional browser engines (e.g. Firefox/WebKit for cross-browser testing), alternate language toolchains, and similar substantial facilities — not just small individual packages. The same reporting expectation applies: mention what was installed and why. The same limits still apply too: this is about development/testing infrastructure, not destructive operations or changes to shared production infrastructure.
