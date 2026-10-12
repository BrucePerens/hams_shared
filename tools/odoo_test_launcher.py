#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Starts Odoo for test.py exactly as `/usr/bin/odoo` does, plus the runner's third-party warning policy
(see odoo_warning_policy.py for why that has to be installed from inside the process)."""
import os
import sys

import odoo.cli
import odoo.netsvc

import odoo_warning_policy

# Running as a script put this tools/ directory first on sys.path, only so the line above could import the
# policy. Odoo and every addon should see the same sys.path they would under /usr/bin/odoo.
if sys.path and os.path.abspath(sys.path[0]) == os.path.dirname(os.path.abspath(__file__)):
    del sys.path[0]


if __name__ == "__main__":
    odoo_warning_policy.install(odoo.netsvc)
    odoo.cli.main()
