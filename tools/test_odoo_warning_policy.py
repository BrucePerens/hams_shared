#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for odoo_warning_policy.py: the runner ignores the known third-party pyasn1 alias
DeprecationWarning (raised through ldap3 when pager_duty is loaded) and nothing else."""
import os
import subprocess
import sys
import types
import unittest
import warnings

import odoo_warning_policy as policy

TAGMAP = "tagMap is deprecated. Please use TAG_MAP instead."
TYPEMAP = "typeMap is deprecated. Please use TYPE_MAP instead."


def odoo_style_filters():
    """What Odoo's netsvc.init_logger does to the filter list (the part that matters here)."""
    warnings.simplefilter("default", category=DeprecationWarning)


def emitted(message, module, category=DeprecationWarning):
    """True if a warning with this message, attributed to `module`, would be shown."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.warn_explicit(message, category, "x.py", 1, module=module, registry={})
    return bool(caught)


class PolicyFilterTests(unittest.TestCase):
    def setUp(self):
        self._saved = warnings.filters[:]
        self.addCleanup(lambda: warnings.filters.__setitem__(slice(None), self._saved))
        warnings.resetwarnings()
        odoo_style_filters()

    def test_without_the_policy_odoos_default_filter_shows_the_pyasn1_warning(self):
        # The reproduction: this is why PYTHONWARNINGS=ignore::...pyasn1 never helped inside Odoo.
        self.assertTrue(emitted(TAGMAP, "pyasn1.codec.ber.encoder"))

    def test_the_known_pyasn1_aliases_are_ignored_from_every_codec_module(self):
        policy.apply_third_party_warning_policy()
        for kind in ("ber", "cer", "der", "native"):
            for what in ("encoder", "decoder"):
                for message in (TAGMAP, TYPEMAP):
                    self.assertFalse(emitted(message, f"pyasn1.codec.{kind}.{what}"), (kind, what, message))

    def test_a_first_party_deprecation_with_the_same_text_is_still_shown(self):
        policy.apply_third_party_warning_policy()
        self.assertTrue(emitted(TAGMAP, "odoo.addons.pager_duty.models.incident"))

    def test_other_pyasn1_deprecations_and_other_libraries_are_still_shown(self):
        policy.apply_third_party_warning_policy()
        self.assertTrue(emitted("Something else is deprecated.", "pyasn1.codec.ber.encoder"))
        self.assertTrue(emitted("tagMap is deprecated. Please use TAG_MAP instead. And more.", "pyasn1.codec.ber.encoder"))
        self.assertTrue(emitted(TAGMAP, "ldap3.utils.asn1"))
        self.assertTrue(emitted("datetime thing is deprecated", "somelib.mod"))

    def test_only_deprecation_warnings_are_covered(self):
        policy.apply_third_party_warning_policy()
        self.assertTrue(emitted(TAGMAP, "pyasn1.codec.ber.encoder", category=UserWarning))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._saved = warnings.filters[:]
        self.addCleanup(lambda: warnings.filters.__setitem__(slice(None), self._saved))
        warnings.resetwarnings()

    def fake_netsvc(self):
        module = types.SimpleNamespace(calls=0)

        def init_logger():
            module.calls += 1
            odoo_style_filters()

        module.init_logger = init_logger
        return module

    def test_the_policy_goes_in_after_odoos_own_filters(self):
        netsvc = self.fake_netsvc()
        policy.install(netsvc)
        netsvc.init_logger()
        self.assertEqual(netsvc.calls, 1)
        self.assertFalse(emitted(TAGMAP, "pyasn1.codec.ber.encoder"))
        self.assertTrue(emitted(TAGMAP, "odoo.addons.x"))

    def test_installing_twice_does_not_wrap_twice(self):
        netsvc = self.fake_netsvc()
        policy.install(netsvc)
        wrapped = netsvc.init_logger
        policy.install(netsvc)
        self.assertIs(netsvc.init_logger, wrapped)
        netsvc.init_logger()
        self.assertEqual(netsvc.calls, 1)


class RealOdooAndLdap3Tests(unittest.TestCase):
    """The end-to-end reproduction: real Odoo logging init, then the real `import ldap3`."""

    SCRIPT = (
        "import odoo.netsvc, odoo.tools\n"
        "{install}"
        "odoo.tools.config.parse_config([])\n"
        "odoo.netsvc.init_logger()\n"
        "import ldap3\n"
        "print('IMPORTED')\n"
    )

    def run_script(self, install):
        code = self.SCRIPT.format(install="import odoo_warning_policy; odoo_warning_policy.install(odoo.netsvc)\n" if install else "")
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)), check=False)

    def test_ldap3_import_logs_the_warning_without_the_policy_and_not_with_it(self):
        without = self.run_script(install=False)
        self.assertIn("IMPORTED", without.stdout, without.stderr)
        self.assertIn("tagMap is deprecated", without.stderr)  # the bug, reproduced
        with_policy = self.run_script(install=True)
        self.assertIn("IMPORTED", with_policy.stdout, with_policy.stderr)
        self.assertNotIn("is deprecated. Please use", with_policy.stderr)
        self.assertNotIn("py.warnings", with_policy.stderr)


if __name__ == "__main__":
    unittest.main()
