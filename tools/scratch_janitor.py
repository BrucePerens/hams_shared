#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Start-of-run janitor for a test slot's own scratch directory (its TMPDIR).

A test run that is killed (timeout, out-of-memory, a lost terminal) never reaches its clean-up, so the temporary directories its
tests made (`tempfile.mkdtemp`, Postgres clusters, relay export trees, cargo scratch) stay in the slot's TMPDIR for good. On a
machine whose TMPDIR is a memory file system they filled it (2026-11-17: /tmp 100%, a round's gate and lint failed three times).

`sweep_stale_scratch(directory)` removes the top-level entries of ONE directory, the slot's own TMPDIR, that are older than
`max_age_hours` and that no live process references (its working directory, command line or environment names the entry's path:
a running `postgres -D <entry>/live` keeps its directory). Names are compared as literal text, never as a pattern. Nothing is
signalled, nothing outside `directory` is touched, a symbolic link is removed as a link and never followed, and the `slot-N`
directories of the other slots (which live inside the first slot's TMPDIR) are left to their own slots.
"""

import os
import re
import shutil
import time

DEFAULT_MAX_AGE_HOURS = 12
# Shared system directories, never a slot's own: a run with TMPDIR=/tmp has no directory of its own to sweep.
SHARED_ROOTS = frozenset({"/", "/tmp", "/var/tmp", "/dev/shm", "/var/lib/odoo/tmp"})
_SLOT_DIRECTORY_RE = re.compile(r"^slot-[0-9]+\Z")


# [@ANCHOR: scratch_janitor:process_texts]
def process_texts(proc_root="/proc"):
    """One text per live process: its working directory, command line and environment. Unreadable processes are skipped."""
    texts = []
    for pid in os.listdir(proc_root):
        if not pid.isdigit():
            continue
        base = os.path.join(proc_root, pid)
        try:
            with open(os.path.join(base, "cmdline"), "rb") as cmdline, open(os.path.join(base, "environ"), "rb") as environ:
                texts.append(os.readlink(os.path.join(base, "cwd")) + cmdline.read().decode("utf-8", "replace")
                             + environ.read().decode("utf-8", "replace"))
        except OSError:
            continue
    return texts


# [@ANCHOR: scratch_janitor:sweep_stale_scratch]
def sweep_stale_scratch(directory, max_age_hours=DEFAULT_MAX_AGE_HOURS, texts=None, now=None, say=print):
    """Remove the stale unreferenced top-level entries of `directory`; returns the list of removed paths.

    `texts`: the process texts (default: `process_texts()`); `now`: the clock in seconds. A missing or relative `directory`,
    and a shared system directory such as /tmp, is ignored."""
    if not directory or os.path.realpath(directory) in SHARED_ROOTS or not os.path.isabs(directory) or not os.path.isdir(directory) or os.path.islink(directory):
        return []
    now = time.time() if now is None else now
    if texts is None:
        texts = process_texts()
    removed = []
    for name in sorted(os.listdir(directory)):
        if _SLOT_DIRECTORY_RE.match(name):
            continue
        path = os.path.join(directory, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if now - info.st_mtime <= max_age_hours * 3600:
            continue
        if any(path in text for text in texts):
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError as error:
            say(f"⚠️  scratch janitor: could not remove {path}: {error}")
            continue
        removed.append(path)
        say(f"[*] scratch janitor: removed stale {path}")
    return removed
