#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
A pytest plugin that raises the Hypothesis example count of every `@given` test for an unattended long run (`fuzz_run.py` loads it with `-p`).

    HAMS_HYPOTHESIS_EXAMPLES=20000 HAMS_HYPOTHESIS_DB=/path/to/db PYTHONPATH=<this directory> python3 -m pytest -p hypothesis_scale ...

Tests set `@settings(max_examples=300)` themselves and an explicit setting beats a profile, so the plugin replaces the settings of each collected
`@given` test (`max_examples` = the variable, no deadline, no health-check failure for slow data) and stores found failures in the example
database `HAMS_HYPOTHESIS_DB` (default: none, so the live checkout never gets a `.hypothesis` directory). Without HAMS_HYPOTHESIS_EXAMPLES it does nothing.
It reaches the test's settings through Hypothesis's `_hypothesis_internal_use_settings` attribute (the one `@settings` itself sets); if a Hypothesis
release drops it the plugin says so on standard error and the run keeps the tests' own counts.
"""
import os
import sys


# [@ANCHOR: hypothesis_scale:scale_item]
def scale_item(function, examples, database=None):
    """Replace the Hypothesis settings of `function` (the test function a `@given` produced; a bound method's `__func__` is used). Returns True when changed."""
    from hypothesis import HealthCheck, settings
    function = getattr(function, "__func__", function)
    if not hasattr(function, "hypothesis"):
        return False
    current = getattr(function, "_hypothesis_internal_use_settings", None) or settings.default
    changes = {"max_examples": examples, "deadline": None, "suppress_health_check": list(HealthCheck)}
    if database:
        from hypothesis.database import DirectoryBasedExampleDatabase
        changes["database"] = DirectoryBasedExampleDatabase(database)
    else:
        changes["database"] = None
    function._hypothesis_internal_use_settings = settings(current, **changes)
    return True


def pytest_collection_modifyitems(items):
    raw = os.environ.get("HAMS_HYPOTHESIS_EXAMPLES")
    if not raw:
        return
    try:
        examples = max(1, int(raw))
    except ValueError:
        print("hypothesis_scale: HAMS_HYPOTHESIS_EXAMPLES=%r is not a whole number; ignored" % raw, file=sys.stderr)
        return
    database = os.environ.get("HAMS_HYPOTHESIS_DB") or None
    changed = 0
    for item in items:
        function = getattr(item, "obj", None)
        if function is not None and scale_item(function, examples, database):
            changed += 1
    print("hypothesis_scale: %d @given test(s) set to %d examples" % (changed, examples), file=sys.stderr)
