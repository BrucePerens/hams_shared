---
name: hams-no-bricking-philosophy
description: "hams.com/hams_open safety mechanisms (kill-switches, revocation checks, forced-safe-mode logic) must prefer auto-update-and-warn over hard-bricking the user's hardware."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 538e9d2e-733b-4d9f-9fb3-a905717d8c6a
  modified: 2026-08-20T03:15:00.275Z
---

When designing a safety or security mechanism for hams.com/hams_open that can refuse to let a
user's software keep running -- e.g. a relay daemon's revocation kill-switch that checks a
known-bad-versions list at update time -- the standing preference, stated directly by the user, is
that it must never simply brick the user's system as its default failure mode. The correct order
of behavior is: first attempt to auto-update to a known-safe version; if that update itself fails
(no network, no safe version published, etc.), warn the operator as loudly and visibly as possible
rather than silently or quietly degrading; only fall back to blocking/refusing to run as the last
resort when there is no way to get the user to a safe, working state or make them aware of the
problem. This reflects a broader "we don't brick the user's system if we can avoid it" philosophy
for any hams.com feature that can refuse to operate for safety reasons -- the goal is a loud,
informed operator with a working (or clearly-marked-unsafe) radio, not a silently non-functional
one.
