# SPDX-License-Identifier: AGPL-3.0-or-later
"""Durable whole-file writes for state, cache and stamp files (docs/reviews/DURABILITY_AUDIT_2026-11-12.md).

`atomic_write` leaves either the old file or the complete new one, also after a power loss: the data is
written to a temporary file in the same directory, flushed to disk, renamed over the target, and the
directory is flushed so the rename itself survives.
"""

import os
import tempfile


# [@ANCHOR: atomic_io:atomic_write]
def atomic_write(path, data, mode=None):
    """Writes `data` (str as UTF-8, or bytes) to `path` atomically and durably. `mode` sets the file's
    permission bits (default: what mkstemp gives, 0600, so pass 0o644 for a world-readable file). An
    existing target keeps its old content when anything fails before the rename."""
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    if isinstance(data, str):
        data = data.encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".%s." % os.path.basename(path), suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
