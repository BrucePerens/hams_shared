---
name: dont-overspawn-bounded-research
description: "Bruce prefers Claude to research bounded technical questions directly with its own WebSearch/WebFetch tools rather than reflexively spawning a subagent or fork, even when a request is framed as \"in the background.\""
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-05T19:22:33.511Z
---

While working on hams_local_relay, Claude raised a "what if we added a serial loopback so HRD/N1MM+
could share the radio port" idea from Bruce, did one direct WebSearch itself, and gave a confident,
well-supported answer (Windows has no driver-free way to create a new virtual COM port visible to
another program; Linux/macOS can do it cleanly via a PTY). Bruce then asked to build the Linux/macOS
version now and, in the same answer, "research a Windows driver-free option further first" as a
parallel option alongside building. Claude interpreted "further" plus its own prior framing of the
choice (a dedicated question offering "spawn a subagent to research" as one of the options) as license
to launch a background fork for what was really 2-3 more targeted searches on sub-questions Claude
was already equipped to run directly (a newer Windows API, whether HRD/N1MM+ accept named pipes, real
driver-signing burden). Bruce's correction, verbatim: "Claude, sometimes you really can answer these
things yourself."

The lesson: a bounded, well-defined research question -- even one with several sub-parts, even one
Bruce explicitly asks to have investigated "further" or "in the background" -- does not by itself
justify spawning a subagent or fork. Claude has the same WebSearch/WebFetch tools a subagent would use;
if the question is answerable with a handful of direct searches and Claude's own domain judgment
(as this one was), the right move is to just run those searches and answer in the same turn, not to
delegate. Reserve spawning for genuinely large, open-ended, or context-isolating tasks -- not as a
default response to "keep looking into this," and not just because a question was framed as
"background" work. When offering the user a choice that includes a "research this" option, don't
build in an implicit assumption that "research" means "delegate" -- direct, inline research is the
default; delegation is the exception that needs its own justification (a task large enough to bury
useful context in tool-call noise, or explicitly requested as multiple parallel angles).
