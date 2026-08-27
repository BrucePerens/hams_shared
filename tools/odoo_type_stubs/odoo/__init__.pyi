# This software is distributed under the terms of the GNU General Public License, version 3.
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Minimal, original stub package root. `models` (models.pyi), `fields` (fields.pyi, Phase 2 step
# 2) and `api` (api.pyi, Phase 2 step 4) are now real, if deliberately partial, stubs -- see each
# file's own comment for why only a narrow, deliberately-chosen slice of each is modeled instead
# of a complete Odoo interface surface. `exceptions`, `tools`, `_` (the gettext callable) remain
# plain `Any`, same as before -- nothing in this plugin currently needs a real symbol for them.
from typing import Any

from . import models as models
from . import fields as fields
from . import api as api

exceptions: Any
tools: Any
_: Any
