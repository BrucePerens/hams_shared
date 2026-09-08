---
name: debug-silent-async-js-sync-and-async-both
description: "For a mystery interaction inside a large body of code (especially third-party/framework code), directly instrument the actual code rather than guessing hypotheses from outside — plus specific traps found doing this on hams.com/hams_open."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-07T18:09:21.006Z
---

On hams.com/hams_open, a component (`ham_shack.web_shack`, an Owl/Odoo component) silently failed
to mount across multiple separate sessions before the real root cause was found — a QWeb template
compile error. A 2026-09-03 session had already ruled out six hypotheses and explicitly named the
real next step in its own write-up, then stopped there (correctly, per its own `advisor()`
guidance); a later session re-derived several of the same already-ruled-out hypotheses from scratch
before finally finding the answer by directly instrumenting Odoo/Owl's own core source files. When
the user was told the real chain of events, his own summary of the bottom line is the lesson to
keep, stated in his own words: **when there's a mystery interaction inside a large body of code,
instrumenting the big piece of code directly is a good way to go** — this applies even when "the
big piece of code" is third-party or framework code (Odoo core, Owl itself), not just this
project's own files. Don't default to reasoning about unfamiliar framework internals from outside
via hypothesis after hypothesis; open the actual source, find the real call chain, and add real
`console.error`/try-catch instrumentation directly into it, treating it the same as any other code
worth debugging.

**Before instrumenting anything, search this project's own accumulated history for prior attempts
at the same symptom.** A prior session's own honest "still open, here's exactly what to check next"
note (in `night_shift_history.md` on this project, or wherever a given project's own session-to-
session institutional memory lives) is not narrative color — it can name the exact next diagnostic
step, and skipping past it is how the same ground gets re-covered session after session. Grep for
the component/file/symptom names involved before forming a single hypothesis of your own.

**When actually instrumenting a suspected async call chain**, bracket every suspected call with
*both* a synchronous `try/catch` around the call itself *and* a `.then(onOk, onErr)` on whatever it
returns — not just one half. A function can throw synchronously before it ever returns a promise to
attach a rejection handler to; if nothing several frames up the chain awaits or catches that
rejection either, the real exception is silently absorbed with no visible trace. This was the
single mistake that cost the most real time in this investigation: checking only the async
resolution path and missing that the real throw was synchronous, several layers before any promise
existed to reject.

**Don't route large diagnostic payloads through `console.log`/`console.error` when instrumenting a
page over Chrome DevTools Protocol.** CDP's own console-message transport silently truncates large
string arguments before they reach a `Runtime.consoleAPICalled` event, with no indication
truncation happened — this can look exactly like "the underlying data itself is incomplete" and
send a debugging session chasing a phantom. A first workaround attempt (chunking a large string
into many indexed `console.error()` calls) also failed non-obviously, since concurrent console
events don't reliably preserve order in the resulting log even when each chunk carries its own
index. The real fix: use `Runtime.evaluate` with `returnByValue: true` and have the instrumented
code `return` the data directly as the call's own result — complete, untruncated, no console
transport, no ordering ambiguity.

**Once a real root cause is found, always ask whether it deserves a permanent, fast, always-on
linter rule, not just a one-off fix** — the user explicitly asked for "a separate rule for speed"
even after a slower, fully-general verification tool already existed for the same bug class. A
fast, narrow tripwire for the specific known mistake and a slower, thorough general-purpose check
are complementary, not redundant; build both when a real bug is found. The full, detailed playbook
(including the exact safe procedure for temporarily editing root-owned system/framework files:
back up first, edit a writable copy, validate syntax, deploy with ownership restored, then restore
and diff-confirm the original afterward) lives in this project's own
`hams_shared/agents/skills/debugging-silent-js-failures/SKILL.md`, not duplicated here.
