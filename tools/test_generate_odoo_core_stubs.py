#!/usr/bin/env python3
# This software is distributed under the terms of the GNU General Public License, version 3.
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Unit tests for generate_odoo_core_stubs.py (ODOO_AWARE_TYPE_CHECKING.md's
2026-08-27 "build the general stub-generator" decision).

Covers the one real regression this generator's own base-less first draft
caused (see ODOO_AWARE_TYPE_CHECKING.md's dated update): a generated stub
class with no declared base loses every framework-level BaseModel method
(search, with_context, with_user, ...) that odoo_type_stubs/models.pyi
provides, because those methods are never re-declared per addon in real
Odoo source -- they come from the real base class. _find_model_base and
_render_stub together are what fixed it; these tests pin that fix so a
future edit can't silently drop the base class again.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import generate_odoo_core_stubs as gen  # noqa: E402
import odoo_registry_builder as orb  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class FindModelBaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="generate_odoo_core_stubs_test_")
        gen._ast_cache.clear()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        gen._ast_cache.clear()

    def test_finds_transient_model_base(self):
        path = os.path.join(self._tmp, "res_config_settings.py")
        _write(path, "from odoo import models\n\n\nclass ResConfigSettings(models.TransientModel):\n    _inherit = 'res.config.settings'\n")
        self.assertEqual(gen._find_model_base(path, "ResConfigSettings"), "TransientModel")

    def test_finds_abstract_model_base(self):
        path = os.path.join(self._tmp, "ir_http.py")
        _write(path, "from odoo import models\n\n\nclass IrHttp(models.AbstractModel):\n    _inherit = 'ir.http'\n")
        self.assertEqual(gen._find_model_base(path, "IrHttp"), "AbstractModel")

    def test_finds_plain_model_base(self):
        path = os.path.join(self._tmp, "res_partner.py")
        _write(path, "from odoo import models\n\n\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n")
        self.assertEqual(gen._find_model_base(path, "ResPartner"), "Model")

    def test_defaults_to_model_when_class_not_found(self):
        path = os.path.join(self._tmp, "empty.py")
        _write(path, "x = 1\n")
        self.assertEqual(gen._find_model_base(path, "DoesNotExist"), "Model")

    def test_defaults_to_model_for_unparseable_file(self):
        path = os.path.join(self._tmp, "broken.py")
        _write(path, "class Foo(models.Model:\n")  # deliberately invalid syntax
        self.assertEqual(gen._find_model_base(path, "Foo"), "Model")

    def test_against_the_real_installed_core_addon_source(self):
        # Real-code confirmation, not synthetic-only, per this codebase's own
        # standard: the actual bug (env['ir.model.data'].search() false-
        # positiving as "has no attribute") traced to this exact real file
        # generating a base-less stub for IrHttp, which real Odoo declares
        # models.AbstractModel.
        core_addons_path = orb.find_odoo_core_addons_path()
        if not core_addons_path:
            self.skipTest("Odoo core addons not installed in this environment")
        real_file = os.path.join(core_addons_path, "base", "models", "ir_http.py")
        if not os.path.isfile(real_file):
            self.skipTest("base/models/ir_http.py not found at the expected real path")
        self.assertEqual(gen._find_model_base(real_file, "IrHttp"), "AbstractModel")


class RenderStubTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="generate_odoo_core_stubs_render_test_")
        gen._ast_cache.clear()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        gen._ast_cache.clear()

    def test_generated_class_declares_the_real_base_not_a_bare_class(self):
        # Pins the actual fix: without this, "class Foo:" loses BaseModel's
        # own framework methods entirely (see this module's own docstring).
        path = os.path.join(self._tmp, "res_config_settings.py")
        _write(path, "from odoo import models\n\n\nclass ResConfigSettings(models.TransientModel):\n    _inherit = 'res.config.settings'\n")
        field = orb.FieldInfo(name="website_id", field_type="Many2one", comodel="website",
                               module="website", file=path, lineno=5, class_name="ResConfigSettings")
        rendered = gen._render_stub(path, {"ResConfigSettings": ([field], [])})
        self.assertIn("class ResConfigSettings(models.TransientModel):", rendered)
        self.assertNotIn("class ResConfigSettings:", rendered)
        self.assertIn("website_id: Any", rendered)

    def test_required_and_defaulted_args_render_correctly(self):
        path = os.path.join(self._tmp, "ir_http.py")
        _write(path, "from odoo import models\n\n\nclass IrHttp(models.AbstractModel):\n    _inherit = 'ir.http'\n")
        method = orb.MethodInfo(
            name="_post_dispatch", arg_names=["cls", "response"], posonly_count=2,
            has_varargs=False, has_varkw=False, kwonly_names=[],
            module="base", file=path, lineno=10, class_name="IrHttp",
        )
        rendered = gen._render_stub(path, {"IrHttp": ([], [method])})
        self.assertIn("def _post_dispatch(cls: Any, response: Any) -> Any: ...", rendered)

    def test_a_defaulted_arg_gets_an_ellipsis_default_in_the_stub(self):
        path = os.path.join(self._tmp, "ir_http.py")
        _write(path, "from odoo import models\n\n\nclass IrHttp(models.AbstractModel):\n    _inherit = 'ir.http'\n")
        method = orb.MethodInfo(
            name="get_frontend_session_info", arg_names=["self", "silent"], posonly_count=1,
            has_varargs=False, has_varkw=False, kwonly_names=[],
            module="web", file=path, lineno=20, class_name="IrHttp",
        )
        rendered = gen._render_stub(path, {"IrHttp": ([], [method])})
        self.assertIn("def get_frontend_session_info(self: Any, silent: Any = ...) -> Any: ...", rendered)

    def test_duplicate_field_and_method_names_within_one_class_are_not_repeated(self):
        path = os.path.join(self._tmp, "res_partner.py")
        _write(path, "from odoo import models\n\n\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n")
        f1 = orb.FieldInfo(name="x", field_type="Char", comodel=None, module="a", file=path, lineno=1, class_name="ResPartner")
        f2 = orb.FieldInfo(name="x", field_type="Char", comodel=None, module="a", file=path, lineno=2, class_name="ResPartner")
        rendered = gen._render_stub(path, {"ResPartner": ([f1, f2], [])})
        self.assertEqual(rendered.count("x: Any"), 1)


if __name__ == "__main__":
    unittest.main()
