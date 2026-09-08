---
name: hams-tmp-disk-space-test-logs
description: /tmp on the hams.com/hams_open dev box is a small (2.7G) dedicated partition; hams_shared/tools/test.py runs redirected to a log file there produce ~70MB each and silently fill it up if not cleaned up promptly.
metadata: 
  node_type: memory
  pinned: false
  originSessionId: f258aa64-9187-4d83-b1ce-de32d7cb03b4
  modified: 2026-08-19T02:02:13.802Z
---

The hams.com/hams_open dev environment's `/tmp` is mounted as its own small partition (2.7G, not
shared with the much larger `/`), and every invocation of `hams_shared/tools/test.py` (the Odoo
test harness) redirected to a log file (the normal pattern for a backgrounded run, e.g. `python3
hams_shared/tools/test.py -u some_module > /tmp/.../some_run.log 2>&1 &`) produces a log around
70MB, regardless of how few tests actually ran, because it captures the full verbose Odoo server
boot/module-load output. Across a long session running many scoped test verifications, these
accumulate fast and silently -- one session left both stale top-level `/tmp/test_*.log` files from
an earlier segment and many completed-but-never-deleted logs in the scratchpad, together consuming
over 1.2GB and dropping `/tmp` to 81MB free (3% of the partition), which had already caused one
real failure earlier in that same session (a `go build` ran out of disk space mid-build until its
cache was redirected elsewhere). The user noticed the low-disk-space symptom and asked whether test
runs were the cause; they were.

The standing practice this implies: after extracting whatever's needed from a `test.py` run's log
(pass/fail summary, specific tracebacks), delete the log file promptly rather than leaving it as a
just-in-case artifact, and periodically sweep `/tmp` for other sessions' abandoned `test_*.log`
files when disk space looks tight. Don't assume `/tmp` has the same headroom as `/` or `/home` on
this box -- check `df -h /tmp` specifically before large builds (compilers, `go build`, etc.) that
might use it for their own cache/temp files, since it fills up faster than its size might suggest.
