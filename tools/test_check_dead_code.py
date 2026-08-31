#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_dead_code.py.

Hermetic, tempdir-fixture-based, same pattern as test_verify_anchors.py. Each fixture is a
single, real, minimal Odoo module (a __manifest__.py plus the files under test) rather than a
scan of the real repo, for the same reason test_verify_anchors.py gives: the real repo's false
positives on a brand-new heuristic checker are a separate, tracked concern (see
check_dead_code.py's own docstring), not something a hermetic unit test should assert on.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_dead_code as cdc  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_module(tmp, name):
    _write(
        os.path.join(tmp, name, "__manifest__.py"),
        "{'name': '%s', 'depends': [], 'data': []}\n" % name,
    )


class DeadTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_template_with_no_reference_anywhere_is_flagged(self):
        _make_module(self.tmp, "mod_a")
        _write(
            os.path.join(self.tmp, "mod_a", "views", "dead_templates.xml"),
            '<odoo><template id="orphan_page" name="Orphan">'
            "<div>hi</div></template></odoo>\n",
        )
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged_ids = [tid for _, _, tid in dead_templates]
        self.assertIn("orphan_page", flagged_ids)

    def test_a_template_that_inherits_another_needs_no_reference_to_its_own_id(self):
        """Real false positive found on the first live run against the real repo: a template
        carrying its own inherit_id is auto-applied by Odoo whenever its base loads -- it does
        not need anyone to reference its own id."""
        _make_module(self.tmp, "mod_a")
        _write(
            os.path.join(self.tmp, "mod_a", "views", "nav_templates.xml"),
            '<odoo><template id="nav_inherit_mod_a" inherit_id="website.navbar">'
            '<xpath expr="//nav" position="inside"><a href="/x">X</a></xpath>'
            "</template></odoo>\n",
        )
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged_ids = [tid for _, _, tid in dead_templates]
        self.assertNotIn("nav_inherit_mod_a", flagged_ids)

    def test_a_view_that_inherits_another_needs_no_reference_to_its_own_id(self):
        _make_module(self.tmp, "mod_a")
        _write(
            os.path.join(self.tmp, "mod_a", "views", "inherit_views.xml"),
            '<odoo><record id="view_form_inherit_mod_a" model="ir.ui.view">'
            '<field name="name">mod.a.form.inherit</field>'
            '<field name="model">mod.a</field>'
            '<field name="inherit_id" ref="base.some_form"/>'
            '<field name="arch" type="xml"><xpath expr="//sheet" position="inside"/></field>'
            "</record></odoo>\n",
        )
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged_ids = [vid for _, _, vid in dead_views]
        self.assertNotIn("view_form_inherit_mod_a", flagged_ids)

    def test_a_template_rendered_by_a_controller_is_not_flagged(self):
        _make_module(self.tmp, "mod_a")
        _write(
            os.path.join(self.tmp, "mod_a", "views", "live_templates.xml"),
            '<odoo><template id="live_page" name="Live">'
            "<div>hi</div></template></odoo>\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "controllers", "main.py"),
            "from odoo import http\n"
            "class MainController(http.Controller):\n"
            "    @http.route('/live', auth='public')\n"
            "    def live(self):\n"
            "        return request.render('mod_a.live_page', {})\n",
        )
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged_ids = [tid for _, _, tid in dead_templates]
        self.assertNotIn("live_page", flagged_ids)


class DeadViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_view_with_no_reference_anywhere_is_flagged(self):
        _make_module(self.tmp, "mod_a")
        _write(
            os.path.join(self.tmp, "mod_a", "views", "dead_views.xml"),
            '<odoo><record id="view_orphan_form" model="ir.ui.view">'
            '<field name="name">orphan.form</field>'
            '<field name="model">mod.a</field>'
            "</record></odoo>\n",
        )
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged_ids = [vid for _, _, vid in dead_views]
        self.assertIn("view_orphan_form", flagged_ids)

    def test_a_view_dynamically_opened_from_python_is_not_flagged(self):
        """Matches the real ham_sk_workflow reject-wizard case found tonight: no static menu,
        opened only via a Python method's returned client-action dict referencing the view id
        by name."""
        _make_module(self.tmp, "mod_a")
        _write(
            os.path.join(self.tmp, "mod_a", "views", "wizard_views.xml"),
            '<odoo><record id="view_reject_wizard_form" model="ir.ui.view">'
            '<field name="name">reject.wizard.form</field>'
            '<field name="model">mod.a.wizard</field>'
            "</record></odoo>\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "models", "wizard.py"),
            "from odoo import models\n"
            "class Wizard(models.TransientModel):\n"
            "    def action_open_wizard(self):\n"
            "        return {\n"
            "            'type': 'ir.actions.act_window',\n"
            "            'view_id': self.env.ref('mod_a.view_reject_wizard_form').id,\n"
            "        }\n",
        )
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged_ids = [vid for _, _, vid in dead_views]
        self.assertNotIn("view_reject_wizard_form", flagged_ids)


class DeadJsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_js_file_in_no_bundle_and_never_referenced_is_flagged(self):
        _make_module(self.tmp, "mod_a")
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "orphan.js"),
            "/** @odoo-module **/\nexport function orphanFn() { return 1; }\n",
        )
        _write(os.path.join(self.tmp, "mod_a", "views", "empty.xml"), "<odoo/>\n")
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged = [name for _, _, name in dead_js]
        self.assertIn("orphan.js", flagged)

    def test_a_js_file_listed_in_the_manifest_bundle_is_not_flagged(self):
        _make_module(self.tmp, "mod_a")
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "live.js"),
            "/** @odoo-module **/\nexport function liveFn() { return 1; }\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{'name': 'mod_a', 'depends': [], 'data': [], 'assets': "
            "{'web.assets_frontend': ['mod_a/static/src/js/live.js']}}\n",
        )
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged = [name for _, _, name in dead_js]
        self.assertNotIn("live.js", flagged)

    def test_an_addmodule_loaded_worklet_outside_any_bundle_is_not_flagged(self):
        """Matches the real rx_noise_gate_processor.js case found tonight: deliberately absent
        from every web.assets_* bundle, loaded instead via a raw static URL string passed to
        audioWorklet.addModule()."""
        _make_module(self.tmp, "mod_a")
        _write(
            os.path.join(
                self.tmp, "mod_a", "static", "src", "js", "worklet_processor.js"
            ),
            "/** @odoo-module **/\nclass P extends AudioWorkletProcessor {}\n"
            "registerProcessor('p', P);\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "chain.js"),
            "/** @odoo-module **/\n"
            "async function load(ctx) {\n"
            "    await ctx.audioWorklet.addModule("
            "'/mod_a/static/src/js/worklet_processor.js');\n"
            "}\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "__manifest__.py"),
            "{'name': 'mod_a', 'depends': [], 'data': [], 'assets': "
            "{'web.assets_frontend': ['mod_a/static/src/js/chain.js']}}\n",
        )
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged = [name for _, _, name in dead_js]
        self.assertNotIn("worklet_processor.js", flagged)

    def test_a_test_js_file_is_never_scanned_as_a_declaration(self):
        _make_module(self.tmp, "mod_a")
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "foo.test.js"),
            "/** @odoo-module **/\n// never referenced elsewhere, but it's a test file\n",
        )
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged = [name for _, _, name in dead_js]
        self.assertNotIn("foo.test.js", flagged)


class RealDeadFixtureTests(unittest.TestCase):
    """The two real dead files this checker was built to catch, reconstructed verbatim from git
    history (commit 105f9e68a537ae8a50582c3c6a36e65f8f58f5c9~1) -- not a synthetic
    approximation. A regression here means the checker would miss the exact real case that
    motivated building it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_real_elmer_directory_template_is_flagged(self):
        _make_module(self.tmp, "ham_onboarding")
        _write(
            os.path.join(
                self.tmp, "ham_onboarding", "views", "elmer_directory_templates.xml"
            ),
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<odoo>\n"
            '    <template id="elmer_directory" name="Elmer Directory">\n'
            '        <!-- audit-ignore-view: Verified by [@ANCHOR: test_elmer_directory] -->\n'
            "        <t t-call=\"website.layout\">\n"
            '            <div id="wrap" class="container mt-5">\n'
            '                <div class="row" id="elmer_list"></div>\n'
            "            </div>\n"
            "        </t>\n"
            "    </template>\n"
            "</odoo>\n",
        )
        dead_templates, dead_views, dead_js = cdc.check_dead_code([self.tmp])
        flagged_ids = [tid for _, _, tid in dead_templates]
        self.assertIn("elmer_directory", flagged_ids)


if __name__ == "__main__":
    unittest.main()
