---
name: no-clock-floors-in-software
description: "Bruce's absolute policy against clock floors: there is to be no clock floor anywhere in the software."
metadata: 
  node_type: memory
  pinned: true
  modified: 2026-10-09T14:54:00.000Z
---

# Absolute Policy: No Clock Floors Anywhere in the Software

On 2026-10-09, Bruce established an explicit, non-negotiable directive:

> **"Remove the clock-floor PR and software, and remove the to-do that calls for it. I want to be clear: there is to be no clock floor anywhere in the software. Make sure there is no to-do that causes the clock floor to be built again."**

### Architectural Directives:
1. **NO clock floor anywhere in the software**: Never implement a persisted "clock floor", "highest time seen", or monotonic wall-clock enforcement.
2. **Never gate operation on clock floor**: Relays, daemons, and client software must never refuse to start, transmit, verify credentials, or operate because the local clock is behind a stored timestamp or earlier than a recorded floor.
3. **No to-dos proposing clock floors**: No to-do item may ever propose, reopen, or specify a clock floor. All evaluation findings (such as S-14) suggesting clock floors are rejected by owner decision.
