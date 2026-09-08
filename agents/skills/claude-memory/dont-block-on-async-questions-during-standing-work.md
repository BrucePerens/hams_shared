---
name: dont-block-on-async-questions-during-standing-work
description: "In a session with standing \"never stop working\" instructions, don't use a blocking interactive question tool (AskUserQuestion) when the user won't answer immediately -- write the questions to a durable artifact instead and keep working."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 58453db6-5ee8-4667-a749-b2d8dad1938a
  modified: 2026-08-27T23:14:55.735Z
---

During an autonomous night-shift session on hams.com/hams_open, Bruce explicitly asked Claude to
"frame questions for me to clear those 11 items" from a decision list. Claude did so using the
AskUserQuestion tool, which blocks the turn until the user answers. Bruce did not see or answer the
questions for hours, during which Claude did nothing else -- even though the same session carried a
standing instruction (repeated many times that night) to never stop working and to keep pulling
items from an open backlog whenever anything remained that didn't require Bruce's own input. When
Bruce did answer, he pointed out the problem directly:

> "You also have no shortage of open proposals that you could have worked on instead of stopping to
> ask me a question that I didn't see for hours."

The lesson: being asked to frame or ask questions is not the same as being asked to block on them.
AskUserQuestion (and any other synchronous, blocking interactive-prompt tool) halts the entire turn
until a human responds, which is fine for a quick clarification the user is actively present for, but
is a real cost in an autonomous or long-running session where the user may not see the prompt for
hours -- especially when a standing instruction elsewhere in the same session says to keep working
whenever unblocked work exists. In that situation, the better default is to write the framed
questions into a durable, asynchronous artifact (a running log file, a dedicated section of a status
document, a message) that the user can answer whenever they return, and continue with any other
available work in the meantime, rather than using a tool that blocks the whole session on an answer
that might not come for a long time. Reserve the blocking interactive tool for situations where the
user is actively present and expected to answer promptly, or where truly no other work exists to fill
the gap.
