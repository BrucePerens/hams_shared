---
name: hams-gemini-ingestion-concurrent-process
description: "A separate Gemini-driven pipeline does ham radio and ICS story/course ingestion for hams.com/hams_open and runs concurrently with Claude sessions -- don't treat its live file churn as suspicious or build on top of it."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: f258aa64-9187-4d83-b1ce-de32d7cb03b4
  modified: 2026-08-20T03:07:24.095Z
---

On the hams.com/hams_open codebase, a separate automation pipeline driven by Gemini performs ham radio and ICS training story/course ingestion, and the user tunes and runs it independently of any Claude session. It runs concurrently with Claude's own work, not sequentially, so its processes and file changes can appear mid-session with no connection to anything Claude did.

In practice this shows up as live, actively-running Python processes such as `ingest/accessory_daemon.py`, `ingest/forms_daemon.py`, `ingest/curriculum_daemon.py`, `ingest/visual_daemon.py`, and `ingest/narrative_daemon.py`, continuously rewriting large data files like `ham_training/data/course_HAM_TECH.json` and `ics_training/data/course_ICS.json`, plus related `agents/skills/*_training/*` skill files. When a Claude session observes uncommitted changes or modification timestamps in these paths, that is this pipeline's normal, expected operation, not stray or suspicious state to investigate or clean up.

The practical implications for a Claude session: never edit, stash, or build new work on top of files this pipeline is actively touching (doing so risks losing its in-flight progress in a write race), and don't be alarmed when a repo-wide tool (like a pre-commit size-delta hook) flags one of these files as suspiciously changed mid-session -- that is very likely this pipeline's own legitimate churn, confirmable by checking whether the relevant `ingest/*_daemon.py` processes are currently running via `ps aux`.

The user has confirmed this pipeline's output can be committed without review when they explicitly say so -- don't assume its uncommitted changes are automatically off-limits to commit forever, or that a commit message elsewhere describing the pipeline as "stopped" is still accurate later in the same session; the pipeline can resume, and the user's own word on its current state at the time takes precedence over an earlier commit message's snapshot of it.
