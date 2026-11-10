# Hams Test Companions (`hams_test_depends`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

A module lists, under `test_depends` in its `__manifest__.py`, the modules its **tests** need installed although its runtime
`depends` does not include them. With `hams_test_depends` installed, a plain `odoo -i/-u <module> --test-enable` run marks those
companions (and their own companions) "to install" so the tests find them; no Hams runner is needed. Outside `--test-enable` the
module does nothing. It is not `depends_cycle`, the unrelated runtime mechanism for dependency cycles.

```python
'depends': ['website'],
# Why the tests need it: the contrast test measures /my/dns with theme_hams' stylesheet.
'test_depends': ['theme_hams'],
```

Entries are refused with a `UserError` when they are the module itself, already in `depends`, unavailable, or close a cycle of
`test_depends` edges alone. A companion that depends on the module at runtime is allowed (installed on top of it); tag the tests
that need it `post_install` and `-at_install`, because the companion loads after or beside the module that lists it.

The Hams tooling reads the same key: `hams_shared/tools/test_impact.py` (a change to the companion selects the dependent's tests),
`hams_shared/tools/check_test_depends.py` (lint auditor) and `devbox_tools/asset_gate.py` (install-only companions in remote runs).
See the module's `README.md` for the mechanism and the function anchors.
