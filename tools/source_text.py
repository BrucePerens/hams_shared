# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read one source file for a checker without letting a bad file crash or silently pass the run."""

import os


# [@ANCHOR: source_text:read_source]
def read_source(path):
    """`(text, None)` for a readable UTF-8 file, or `(None, message)` naming why it is not: a broken symlink, a permission error,
    a directory, bytes that are not UTF-8. A checker that meets the message reports it as a finding of its own and goes on to the
    next file, so one bad file neither crashes the run with a traceback nor passes unseen."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read(), None
    except (OSError, UnicodeDecodeError) as error:
        kind = "broken symlink" if os.path.islink(path) and not os.path.exists(path) else type(error).__name__
        return None, "cannot read %s (%s): the file was not checked" % (path, kind)
