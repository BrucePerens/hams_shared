---
name: hams-python-io-flush-reminder
description: Always flush higher-level buffered Python IO explicitly in hams.com/hams_open daemon and IPC code.
metadata: 
  node_type: memory
  pinned: false
  originSessionId: a5bd8915-8036-4ef1-8db6-088f583dcf5c
  modified: 2026-08-20T20:07:47.771Z
---

When writing or reviewing Python code for the hams.com/hams_open ingestion daemons or any inter-process communication in that codebase (sockets, subprocess pipes, log files another process tails), always flush explicitly whenever using higher-level, buffered Python IO — text-mode file objects, `socket.makefile()`, `print()` to a stream, or anything similar. Buffering can silently delay or drop data that another process (a daemon, a subagent, a peer LLM session) is actively blocked waiting to see, producing exactly the kind of silent, hard-to-diagnose desync bug this codebase's ingestion pipelines have repeatedly suffered from. Raw `socket.send()`/`sendall()` calls are already unbuffered and don't need this, but anything wrapping a socket or file in a higher-level Python IO layer does.
