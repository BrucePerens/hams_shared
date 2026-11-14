# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run Odoo's tests in reverse order (test.py --test-order reverse).

Loaded as a sitecustomize through PYTHONPATH. Once odoo.tests.loader is
imported, make_suite() returns its tests reversed. A test that passes in the
normal order and fails reversed leaks state or depends on an earlier test.
"""
import importlib.abc
import importlib.util
import os
import sys


class _ReverseFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "odoo.tests.loader":
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        orig_exec = spec.loader.exec_module

        def exec_module(module):
            orig_exec(module)
            orig_make_suite = module.make_suite

            def make_suite(module_names, position="at_install"):
                suite = orig_make_suite(module_names, position)
                suite._tests = list(reversed(list(suite._tests)))
                return suite

            module.make_suite = make_suite

        spec.loader.exec_module = exec_module
        return spec


if os.environ.get("HAMS_TEST_ORDER") == "reverse":
    sys.meta_path.insert(0, _ReverseFinder())
