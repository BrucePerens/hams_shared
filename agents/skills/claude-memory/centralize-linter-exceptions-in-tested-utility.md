---
name: centralize-linter-exceptions-in-tested-utility
description: "When a strict lint/policy rule blocks an otherwise-legitimate pattern, the user's preferred fix is one small, tested, centralized utility function bearing the exception, not a loosened rule or scattered bypass tags."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: a5bd8915-8036-4ef1-8db6-088f583dcf5c
  modified: 2026-08-22T00:05:33.362Z
---

When a codebase's strict linting or policy rule blocks a coding pattern that is genuinely
legitimate in a specific, narrow case, the user has a clear standing preference for how to resolve
the conflict, expressed directly on hams.com/hams_open: write one small, well-named, narrowly-scoped
utility function that internally does the "impure" thing exactly once, wrap it with a real test
proving it behaves correctly (including the failure/fallback path), and have every other call site
in the codebase call that clean wrapper instead of repeating the banned pattern itself. The concrete
case that prompted this: `check_burn_list.py` bans `except AttributeError`, `hasattr()`, and 3-arg
`getattr()` outright (aimed at stopping Gemini-era code from using them to mask uncertainty about
this codebase's own models), which left six identical, scattered `sys.stdout.reconfigure()`
feature-detection sites in `ingest/*.py` with no compliant way to exist -- legitimate stdlib
feature-detection, not architectural laziness. The user's proposed fix, which they then generalized
explicitly ("let's make that the general solution for issues of this class"): rather than teaching
the linter's AST pass to distinguish "our code" from "stdlib" (fragile and easy to get subtly
wrong) or granting a bypass tag at each of the six sites (harder to audit, and doesn't actually
eliminate the risky pattern -- just tags it six times), centralize the pattern into one shared
utility (e.g. `try_enable_line_buffering(stream) -> bool`), grant the bypass exactly once at that
single definition, and have all six call sites use the clean wrapper with zero linter friction.
This is the standing, generalizable approach to reach for whenever a similar tension comes up
again -- on this codebase or elsewhere -- not just a one-off fix for this specific case.
