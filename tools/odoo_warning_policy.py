#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
The test runner's policy for warnings raised by THIRD-PARTY code inside the Odoo test process,
and the launcher that applies it.

Why this exists (found 2026-10-11): a `test.py -u zero_sudo` run also installs zero_sudo's
dependents, among them pager_duty, whose `import ldap3` raises
    pyasn1/codec/ber/encoder.py:952: DeprecationWarning: tagMap is deprecated. Please use TAG_MAP instead.
(and the same for `typeMap`, from the cer/der/native encoders and decoders too). The cause is the
distro's python3-ldap3 2.9.1 still importing pyasn1's pre-0.6 names from python3-pyasn1 0.6.x; pyasn1 is
doing its job and we cannot patch or pin either apt package from here. test.py had tried to silence it
with `PYTHONWARNINGS=ignore::DeprecationWarning:pyasn1.codec.ber.encoder`, which never worked in the Odoo
process: Odoo's `netsvc.init_logger()` runs `warnings.simplefilter('default', DeprecationWarning)`
AFTER Python has applied PYTHONWARNINGS, and that call inserts at the FRONT of the filter list, so it
outranks the environment's filter. The warning then goes to the log as `WARNING py.warnings` with a stack
dump, in the middle of the module load.

The fix belongs here, in the runner, and is deliberately narrow: only the two known pyasn1 alias
messages, only when raised from a pyasn1 codec module. A first-party DeprecationWarning, a different
pyasn1 deprecation, or a deprecation from any other library still shows. If the distro ships an ldap3 that
uses the new names this becomes dead code; delete it then.

`odoo_test_launcher.py` is what test.py starts instead of `/usr/bin/odoo`: it calls `install()` below so
the filters go in right after Odoo's own, then runs the normal `odoo.cli.main()` with the same arguments.
This module imports no Odoo, so it is cheap to test.
"""
import warnings

# (message regex, module regex). `module` is matched against the module that raised the warning, which for
# pyasn1's `__getattr__` shim (no stacklevel) is pyasn1's own codec module, not the importing ldap3.
THIRD_PARTY_DEPRECATIONS_TO_IGNORE = (
    (
        r"^(tagMap|typeMap) is deprecated\. Please use (TAG_MAP|TYPE_MAP) instead\.$",
        r"^pyasn1\.codec\.(ber|cer|der|native)\.(encoder|decoder)$",
    ),
)

_WRAPPERS = []  # the wrappers install() created, so a second install() does not stack another


def apply_third_party_warning_policy():
    """Ignore the known, unfixable third-party DeprecationWarnings. Call after Odoo's own filters."""
    for message, module in THIRD_PARTY_DEPRECATIONS_TO_IGNORE:
        warnings.filterwarnings("ignore", message=message, category=DeprecationWarning, module=module)


def install(netsvc_module):
    """Make `netsvc_module.init_logger` apply the policy after Odoo's own filter setup. Idempotent."""
    original = netsvc_module.init_logger
    if original in _WRAPPERS:
        return

    def init_logger():
        original()
        apply_third_party_warning_policy()

    _WRAPPERS.append(init_logger)
    netsvc_module.init_logger = init_logger

