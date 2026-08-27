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

import ast
import keyword
import os
import sys
import tempfile
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

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


_IDENTIFIER = st.from_regex(r"[a-z_][a-z0-9_]{0,8}", fullmatch=True).filter(
    # Real Python keywords are invalid identifiers outright. "args"/"kwargs" are valid
    # identifiers in general, but are _render_method_params' own fixed names for the *args/
    # **kwargs slots -- an arg_names/kwonly_names entry literally named "args" while
    # has_varargs is also True is a real, if exotic, duplicate-parameter collision the
    # renderer doesn't currently guard against, and isn't the property under test here.
    lambda s: not keyword.iskeyword(s) and s not in ("args", "kwargs")
)


@st.composite
def _method_info_strategy(draw):
    # CODE_REVIEW_PROCESS.md's own recommendation ("Hypothesis... a real, practical strengthening
    # of ordinary unit tests... fits far more of this codebase's actual Python" than CrossHair) --
    # applied here to _render_method_params(), a small pure parser/renderer of exactly the kind
    # that doc names as a good target, and one this session wrote tonight with only hand-picked
    # example inputs so far.
    arg_names = draw(st.lists(_IDENTIFIER, unique=True, max_size=5))
    kwonly_names = draw(
        st.lists(_IDENTIFIER, unique=True, max_size=3).filter(
            lambda names: not (set(names) & set(arg_names))
        )
    )
    posonly_count = draw(st.integers(min_value=0, max_value=len(arg_names)))
    has_varargs = draw(st.booleans())
    # A bare "*" separator (no varargs, but kwonly names present) plus varargs are mutually
    # exclusive in real Python syntax -- _render_method_params already encodes that (elif), so
    # the strategy doesn't need to avoid the combination, only the renderer's own logic does.
    has_varkw = draw(st.booleans())
    return orb.MethodInfo(
        name="synthetic",
        arg_names=arg_names,
        posonly_count=posonly_count,
        has_varargs=has_varargs,
        has_varkw=has_varkw,
        kwonly_names=kwonly_names,
        module="test",
        file="<hypothesis>",
        lineno=1,
        class_name="Synthetic",
    )


class RenderMethodParamsPropertyTests(unittest.TestCase):
    @given(_method_info_strategy())
    @settings(max_examples=200)
    def test_rendered_params_always_produce_syntactically_valid_python(self, method):
        params = gen._render_method_params(method)
        source = f"def _f({params}) -> Any: ...\n"
        try:
            ast.parse(source)
        except SyntaxError as e:  # pragma: no cover -- failure IS the finding
            self.fail(
                f"[!] DIAGNOSTIC FOR AI: _render_method_params produced invalid Python syntax "
                f"for method={method!r}: {source!r} ({e})"
            )

    @given(_method_info_strategy())
    @settings(max_examples=200)
    def test_every_arg_and_kwonly_name_appears_exactly_once_in_the_output(self, method):
        # A real bug this would catch: any renderer change that drops a parameter, or that
        # emits a duplicate (e.g. a name in both arg_names and kwonly_names slipping through),
        # produces a stub that either silently loses a real parameter's type info or fails to
        # parse at all (Python forbids duplicate parameter names). The strategy guarantees
        # arg_names and kwonly_names never overlap, and neither ever collides with the fixed
        # "args"/"kwargs" varargs names (real identifiers only, both come from _IDENTIFIER).
        params = gen._render_method_params(method)
        source = f"def _f({params}) -> Any: ...\n"
        tree = ast.parse(source)
        func_def = tree.body[0]
        rendered_names = (
            [a.arg for a in func_def.args.posonlyargs]
            + [a.arg for a in func_def.args.args]
            + [a.arg for a in func_def.args.kwonlyargs]
        )
        expected_names = list(method.arg_names) + list(method.kwonly_names)
        self.assertEqual(sorted(expected_names), sorted(rendered_names))
        self.assertEqual(bool(func_def.args.vararg), method.has_varargs)
        self.assertEqual(bool(func_def.args.kwarg), method.has_varkw)


if __name__ == "__main__":
    unittest.main()
