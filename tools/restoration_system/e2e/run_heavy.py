#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The documented target for the heavy end-to-end tests of the restoration system (the lint never runs them). Takes 10 to 25 minutes."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-v", "-s", "-p", "no:cacheprovider", "-m", "heavy", os.path.join(HERE, "test_end_to_end.py")] + sys.argv[1:]))
