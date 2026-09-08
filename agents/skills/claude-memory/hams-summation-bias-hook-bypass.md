---
name: hams-summation-bias-hook-bypass
description: "Standing authorization to bypass hams.com's anti-summation-bias pre-commit hook once a flagged file-size reduction is verified to be a genuine refactor, not lost logic."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: f258aa64-9187-4d83-b1ce-de32d7cb03b4
  modified: 2026-08-18T15:37:56.390Z
---

The hams.com/hams_open codebase has a pre-commit hook that flags commits where a file's size dropped sharply (its message reads "SUMMATION BIAS DETECTED... file size reduced by X%"). It exists as a heuristic guard against an AI quietly dropping rules, nuance, or logic while claiming to just be cleaning up a file. The hook cannot distinguish that failure mode from a legitimate refactor -- e.g. extracting a controller's inline logic into a reusable model method shrinks the original file exactly the same way deleting the logic outright would.

The user has explicitly authorized bypassing this hook (`git commit --no-verify`) once the flagged reduction has been personally verified to be a genuine refactor rather than an actual loss of logic, rules, or nuance -- for example by confirming the removed code now lives elsewhere in the diff and by running the real tests that cover it. The bypass is conditional on that verification, not a blanket license to skip the check: state plainly in the response to the user why the reduction is safe whenever bypassing. This authorization is also recorded in the `night-shift` skill (`hams_shared/agents/skills/night-shift/SKILL.md`) so it applies during autonomous sessions as well as interactive ones.
