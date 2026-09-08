---
name: playwright-cdp-process-identification
description: "To profile or measure CPU of a Playwright-launched browser, identify its real OS process via the browser's own CDP session, not a system-wide `ps`/`pgrep` pattern match."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 44fa3b1d-b189-4e0a-bf9c-d06127ace94d
  modified: 2026-09-08T14:36:19.012Z
---

When measuring the real CPU cost of a page loaded via Playwright (e.g. investigating a suspected performance bug), never identify the browser's OS process by pattern-matching `ps aux` or `pgrep` against a string like `chrome-headless-shell.*type=renderer`. In any session that has launched more than one Playwright browser instance over its lifetime — routine in a long testing session — multiple `chrome-headless-shell` processes of various types (renderer, GPU, zygote, network/audio utility processes) can be alive simultaneously, including leftover renderers from earlier, unrelated test runs that haven't been cleaned up yet. A `ps`-based pattern match has no way to tell which renderer belongs to the specific browser instance actually under test, so it can silently attribute another test's real CPU usage to the page currently being investigated.

The reliable way to identify the correct process is to ask the browser instance itself, via its own Chrome DevTools Protocol session: `browser.new_browser_cdp_session()` then `send("SystemInfo.getProcessInfo")` returns each of that specific browser's real child processes with their type (`browser`, `renderer`, `GPU`, `network.mojom.NetworkService`, `audio.mojom.AudioService`) and OS pid. Only measure against pids returned this way — e.g. reading `/proc/<pid>/stat` utime+stime deltas over a sampling window, or comparing against `ps`'s own reported `%CPU` for that exact pid — never a broader pattern match.

This also means a suspected "sustained high CPU" finding from `ps`-based process identification should be treated as unconfirmed until re-measured this way. On hams.com/hams_open, a finding of "the Web Shack page sustains 106-117% CPU with zero user interaction" (originally investigated via `ps` pattern-matching) was retracted after re-measuring against the CDP-verified correct renderer pid, which showed a brief page-load spike settling to ~3% CPU at rest — three independent measurement methods (CDP `Profiler.start()`/`stop()`, `SystemInfo.getProcessInfo`, and direct `/proc` sampling against the verified pid) agreed, and none showed the originally reported sustained cost. The original number most likely came from a stale or unrelated renderer process elsewhere on the system matching the same `ps` pattern.

A related, more general lesson from the same session: Chrome DevTools Protocol's `Profiler` domain (real JS CPU profiling with per-function self-time) works fully in headless mode — it does not require a headed/interactive browser session, contrary to an initial assumption. Playwright exposes it directly via `page.context.new_cdp_session(page)` then `send("Profiler.enable")` / `send("Profiler.start")` / `send("Profiler.stop")`.
