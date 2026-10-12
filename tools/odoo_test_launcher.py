#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Starts Odoo for test.py exactly as `/usr/bin/odoo` does, plus the runner's third-party warning policy
(see odoo_warning_policy.py for why that has to be installed from inside the process)."""
import odoo.cli
import odoo.netsvc

import odoo_warning_policy


if __name__ == "__main__":
    odoo_warning_policy.install(odoo.netsvc)
    odoo.cli.main()
