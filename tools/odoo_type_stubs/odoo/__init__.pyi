# This software is distributed under the terms of the GNU General Public License, version 3.
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Minimal, original stub package root. Only `models` (models.pyi, this
# directory) is a real stub -- everything else Odoo's own real `odoo`
# package exports (fields, api, _, Constraint, ...) is deliberately left
# as `Any` here rather than stubbed, so mypy still checks Model-derived
# classes' *methods* (this plugin's actual job right now) without also
# needing a complete Odoo interface surface, which is real, separate,
# not-yet-attempted work (see ODOO_AWARE_TYPE_CHECKING.md).
from typing import Any

from . import models as models

fields: Any
api: Any
exceptions: Any
tools: Any
_: Any
