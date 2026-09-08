---
name: hams-uncommitted-until-approved
description: "On hams.com/hams_open, do work that needs approval but leave it uncommitted until the user reviews it, rather than committing then asking."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: a5bd8915-8036-4ef1-8db6-088f583dcf5c
  modified: 2026-08-20T20:32:51.723Z
---

On the hams.com/hams_open/hams_shared codebase, when a piece of work is the kind that would normally need the user's sign-off before it's finalized, do the work in the working tree but do not run `git commit` for it until the user has actually approved it — leave it as an uncommitted diff and say so, rather than committing first and asking afterward. The user's stated reason is that this uses time more effectively: it lets them review real diffs directly instead of relitigating a decision from a description after the fact, and it keeps the commit history clean of things that might still get reverted or reworked. This refines the standing "commit as you go" autonomous night-shift convention rather than replacing it — routine, clearly-safe work can still be committed as you go, but anything that specifically needs approval should wait, uncommitted, for that approval before landing in git history.
