# SPDX-License-Identifier: AGPL-3.0-or-later
"""The profile: every hams.com-specific name (bucket, paths, stanza, database) lives in a JSON file, not in the engine."""
import json
import os

from .runner import RestorationError

DEFAULT_PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hams_com_profile.json")
REQUIRED = ("name", "debian_suite", "debian_mirror", "database", "b2", "local", "config_tar_scope", "machine_specific", "verify")


def load(path=None):
    """The profile dict. RestorationError when the file is unreadable or lacks a required section."""
    path = path or DEFAULT_PROFILE
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RestorationError("cannot read profile %s: %s" % (path, exc))
    missing = [key for key in REQUIRED if key not in data]
    if missing:
        raise RestorationError("profile %s lacks: %s" % (path, ", ".join(missing)))
    return data
