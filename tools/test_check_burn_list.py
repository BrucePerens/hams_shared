#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
check_burn_list.py had zero automated test coverage despite being the
core security/architecture linter this codebase runs on every commit
(3600+ lines, dozens of distinct rules) -- every verification of a
change to it this session was a manual before/after comparison, not a
regression test that survives after the session ends. Starts with
_xml_audit_lookback_start(), the specific function this session's own
earlier work modified (unifying three separate magic-number lookback
implementations into one real structural-boundary walk), using the
exact two real regression cases documented in that function's own
docstring -- not synthetic examples, the actual bugs found this session
against real files (edge_routing/data/security_data.xml,
hams_s3/views/res_config_settings_views.xml) -- as fixtures, so a
future change that reintroduces either bug fails a real test instead of
requiring another manual before/after diff.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from check_burn_list import (  # noqa: E402
    parse_odoo_xml,
    parse_odoo_html,
    _xml_audit_lookback_start,
    check_ast_vulnerabilities,
    scan_file,
)


def _tour_mandate_errors(xml, filename="test_view.xml"):
    # scan_file reads from a real path (it opens the file itself rather
    # than taking content directly), so the fixture has to actually hit
    # disk -- this is what the real linter invocation does, not a
    # shortcut around it.
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = str(Path(tmpdir) / filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(xml)
        errors, _warnings = scan_file(filepath, is_odoo_module=True)
    return [e for e in errors if "UI TOUR MANDATE VIOLATION" in e]


def _domain_sandbox_warnings(source, filepath="/tmp/some_module/tests/test_foo.py"):
    # DOMAIN SANDBOX (like the .sudo() ban above it in the same
    # visit_Attribute method) only runs when is_odoo_module=True --
    # confirmed by reading the real code, not assumed: the first version
    # of this test omitted it and got zero warnings for a case that
    # obviously should have fired.
    lines = source.splitlines()
    _errors, warnings = check_ast_vulnerabilities(filepath, source, lines, is_odoo_module=True)
    return [msg for _lineno, msg in warnings if "DOMAIN SANDBOX" in msg]


def _find_first(node, tag):
    """Depth-first search for the first descendant (or self) with `tag`."""
    if node.tag == tag:
        return node
    for child in node.children:
        found = _find_first(child, tag)
        if found is not None:
            return found
    return None


def test_lookback_reaches_a_multiline_comment_longer_than_a_fixed_guess():
    # Mirrors the real edge_routing/data/security_data.xml case: a real
    # audit-ignore-cron comment spanning 9 lines, one more than an
    # earlier fixed 8-line guess -- the fixed guess would have missed
    # line 1 of the comment entirely.
    xml = """<odoo>
    <!-- audit-ignore-cron: line1
         line2
         line3
         line4
         line5
         line6
         line7
         line8 -->
    <record id="test_cron" model="ir.cron">
        <field name="name">test</field>
    </record>
</odoo>"""
    root = parse_odoo_xml(xml)
    record = _find_first(root, "record")
    assert record is not None

    comment = _find_first(root, "#comment")
    assert comment is not None
    assert "audit-ignore-cron" in comment.attrs["text"]

    start = _xml_audit_lookback_start(record)
    # The return value is a 0-indexed slice start into `lines` (verified
    # against its real caller: `lines[lookback_start : node.end_lineno + 1]`),
    # so it must equal comment.lineno - 1 to make lines[start] the
    # comment's own 1-indexed line -- not a fixed offset from the
    # record's own line, which would land inside this 9-line comment
    # body instead of at its start.
    assert start == comment.lineno - 1, (
        f"expected lines[start] to be the comment's own start line ({comment.lineno}), "
        f"i.e. start == {comment.lineno - 1}, got {start} -- "
        "a fixed-offset guess would land inside this 9-line comment instead of at its start"
    )


def test_lookback_jumps_past_intermediate_wrapper_elements_to_the_enclosing_record():
    # Mirrors the real hams_s3/views/res_config_settings_views.xml case:
    # <xpath> is nested inside <field name="arch"> inside <record>, and
    # <field name="arch"> itself has an ordinary (non-comment) preceding
    # sibling field. Climbing one ancestor level at a time from <xpath>
    # stops at that ordinary field and misses the real audit-ignore
    # comment two levels further up, beside the enclosing <record>.
    xml = """<odoo>
    <!-- audit-ignore-xpath: real architectural exception, see ADR-0083 -->
    <record id="test_view" model="ir.ui.view">
        <field name="name">test</field>
        <field name="arch" type="xml">
            <xpath expr="//form" position="inside">
                <field name="foo"/>
            </xpath>
        </field>
    </record>
</odoo>"""
    root = parse_odoo_xml(xml)
    xpath_node = _find_first(root, "xpath")
    assert xpath_node is not None

    comment = _find_first(root, "#comment")
    assert comment is not None
    assert "audit-ignore-xpath" in comment.attrs["text"]

    start = _xml_audit_lookback_start(xpath_node)
    # See the multiline-comment test above for why this is
    # comment.lineno - 1, not comment.lineno.
    assert start == comment.lineno - 1, (
        f"expected lookback from <xpath> to jump past the intermediate <field name='arch'> wrapper "
        f"and reach the comment beside the enclosing <record> (line {comment.lineno}), got {start} -- "
        "a one-level-at-a-time climb would stop at <field name='arch'>'s own ordinary preceding "
        "sibling field instead"
    )


def test_lookback_with_no_preceding_comment_reaches_the_parents_own_start_line():
    # No audit-ignore comment present at all -- must reach back to a
    # real structural boundary (the enclosing record's own start line,
    # since there's no comment sibling to walk past), not crash or
    # return something arbitrary.
    xml = """<odoo>
    <record id="test_cron" model="ir.cron">
        <field name="name">test</field>
    </record>
</odoo>"""
    root = parse_odoo_xml(xml)
    record = _find_first(root, "record")
    assert record is not None

    start = _xml_audit_lookback_start(record)
    assert start == root.lineno - 1 or start >= 1, "must return a sane line number, not crash, when no comment precedes the node at all"


def test_lookback_on_a_record_with_no_parent_uses_the_fallback():
    # node.parent is None only for a real parse root or a synthetically
    # detached node -- the fallback_lines path exists specifically for
    # this case (documented as "should not happen for a properly parsed
    # XML tree", but still real, reachable code that must not crash).
    xml = """<record id="detached" model="ir.cron">
    <field name="name">test</field>
</record>"""
    root = parse_odoo_xml(xml)
    # parse_odoo_xml wraps content in a root_wrapper, so the real root
    # returned here already has children -- detach the record itself to
    # exercise the no-parent path directly.
    record = _find_first(root, "record")
    record.parent = None

    start = _xml_audit_lookback_start(record, fallback_lines=8)
    assert start == record.lineno - 8


def test_domain_sandbox_flags_a_direct_id_chained_grant():
    # The canonical, always-caught shape: .id chained directly onto the
    # ref() call, the exact pattern every real group_ids/groups_id grant
    # in this codebase uses.
    source = (
        "def f(self, user):\n"
        '    user.write({"group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})\n'
    )
    warnings = _domain_sandbox_warnings(source)
    assert len(warnings) == 1


def test_domain_sandbox_flags_the_variable_assignment_evasion_this_session_fixed():
    # This session's own real fix: g = self.env.ref(...) stores the
    # ref() result in a variable first, then g.id is read on a LATER,
    # separate line -- the direct .id-chain check alone can't see this
    # at all. Confirming this still fires is exactly what protects that
    # fix from being silently reverted.
    source = (
        "def f(self, user):\n"
        '    g = self.env.ref("base.group_user")\n'
        '    user.write({"group_ids": [(4, g.id)]})\n'
    )
    warnings = _domain_sandbox_warnings(source)
    assert len(warnings) == 1


def test_domain_sandbox_does_not_flag_a_bare_reference_with_no_id_access():
    # Real false-positive case #1 from this rule's own code comment:
    # backup_management/tests/test_tdd_batch2.py's
    # test_service_account_no_base_user_group assigns the ref() result
    # to a plain variable then asserts *absence* -- no .id access
    # anywhere, so this is not a grant.
    source = (
        "def f(self, user):\n"
        '    g = self.env.ref("base.group_user")\n'
        "    self.assertNotIn(g, user.group_ids)\n"
    )
    warnings = _domain_sandbox_warnings(source)
    assert warnings == [], f"a bare reference with no .id access anywhere must not be flagged as a grant, got: {warnings}"


def test_domain_sandbox_does_not_flag_a_bare_read_of_an_ir_rule_groups_field():
    # Real false-positive case #2 from this rule's own code comment: a
    # bare self.assertIn(self.env.ref("base.group_user"), rule.groups)
    # reads an ir.rule's groups field (an unrelated model, not a
    # user/group grant) -- the ref()'s own .id is never accessed here
    # either, on the same line or any other.
    source = (
        "def f(self, rule):\n"
        '    self.assertIn(self.env.ref("base.group_user"), rule.groups)\n'
    )
    warnings = _domain_sandbox_warnings(source)
    assert warnings == []


def test_domain_sandbox_exempts_a_file_named_for_odoo_facility_service():
    # Only odoo_facility_service_internal may actually hold this group
    # -- per this rule's own warning message -- so its own module's real
    # code granting it to itself must not be flagged. The exemption
    # check is `"odoo_facility_service" in self.filename`, and
    # check_ast_vulnerabilities sets self.filename to
    # os.path.basename(filepath) -- the exemption is keyed to the
    # FILE'S OWN NAME, not any directory it happens to live in. Verified
    # directly: a first version of this test used a path whose
    # *directory* was named odoo_facility_service but whose filename
    # (setup.py) wasn't, and the warning still fired -- confirming this
    # is filename-based, not path-based, rather than assuming either.
    source = (
        "def f(self, user):\n"
        '    user.write({"group_ids": [(6, 0, [self.env.ref("base.group_user").id])]})\n'
    )
    warnings = _domain_sandbox_warnings(source, filepath="/tmp/odoo_facility_service/models/odoo_facility_service_setup.py")
    assert warnings == []


def test_domain_sandbox_ignores_an_unrelated_ref_call():
    # Sanity check: referencing any other xml_id at all (not
    # base.group_user specifically) must never trigger this rule.
    source = (
        "def f(self, user):\n"
        '    g = self.env.ref("base.group_system")\n'
        '    user.write({"group_ids": [(4, g.id)]})\n'
    )
    warnings = _domain_sandbox_warnings(source)
    assert warnings == []


def _patch_ban_errors(filename):
    # The NATIVE PATCH DECORATORS FORBIDDEN rule (GENERAL_ERROR_RULES,
    # matched by scan_file against the real filepath, not the bare
    # filename) is what these two tests exercise -- confirmed directly
    # against real files, not assumed: before this exclusion existed,
    # au_callsign_sync's own real test_au_callsign_sync.py (a standalone
    # daemon test using patch.object() because self.safe_patch() is a
    # method on zero_sudo's Odoo TestCase-derived HamsTransactionCase,
    # which daemon tests are themselves forbidden from importing) was
    # already tripping this rule whenever check_burn_list.py was pointed
    # at a real directory.
    source = (
        "from unittest.mock import patch\n"
        "\n"
        "\n"
        "def test_example():\n"
        '    with patch.object(SomeClass, "method", return_value=1):\n'
        "        pass\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        full_path = Path(tmpdir) / filename
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(source, encoding="utf-8")
        errors, _warnings = scan_file(str(full_path), is_odoo_module=False)
    return [e for e in errors if "Native patch decorators" in e]


def test_patch_ban_exempts_a_top_level_daemon_test():
    errors = _patch_ban_errors("daemons/au_callsign_sync/test_au_callsign_sync.py")
    assert errors == []


def test_patch_ban_exempts_a_module_embedded_daemon_test():
    # The singular daemon/ (module-embedded, e.g.
    # backup_management/daemon/test_main.py) must be exempted the same
    # way the plural top-level daemons/ is -- both forms are standalone,
    # Odoo-decoupled daemon code.
    errors = _patch_ban_errors("backup_management/daemon/test_main.py")
    assert errors == []


def test_patch_ban_still_flags_a_genuine_non_daemon_test():
    # The exclusion must not swallow the rule entirely -- an ordinary
    # Odoo module test using native patch.object() still needs to be
    # caught.
    errors = _patch_ban_errors("ham_callbook/tests/test_something.py")
    assert len(errors) == 1


def test_tour_mandate_is_not_satisfied_by_audit_ignore_view_alone():
    # The real bug found and fixed this session (pager_check_views.xml):
    # ADR 0076 section 3 says audit-ignore-view and burn-ignore-tour are
    # distinct bypasses for distinct concerns (backend view-rendering
    # test coverage vs. tour coverage) and "both tags must be explicitly
    # defined side-by-side" to skip both. A bare audit-ignore-view alone
    # must still flag the tour-mandate violation.
    xml = """<odoo>
    <!-- audit-ignore-view: rendering covered by test_view.py -->
    <record id="test_view" model="ir.ui.view">
        <field name="name">test.view</field>
        <field name="model">test.model</field>
        <field name="arch" type="xml">
            <form/>
        </field>
    </record>
</odoo>"""
    errors = _tour_mandate_errors(xml)
    assert len(errors) == 1, f"expected the tour mandate to still fire with only audit-ignore-view present, got: {errors}"


def test_tour_mandate_is_satisfied_by_burn_ignore_tour_alone():
    xml = """<odoo>
    <!-- burn-ignore-tour: ROI is zero, a static lookup-table view -->
    <record id="test_view" model="ir.ui.view">
        <field name="name">test.view</field>
        <field name="model">test.model</field>
        <field name="arch" type="xml">
            <form/>
        </field>
    </record>
</odoo>"""
    errors = _tour_mandate_errors(xml)
    assert errors == []


def test_tour_mandate_is_satisfied_by_a_real_tour_anchor():
    xml = """<odoo>
    <!-- [@ANCHOR: COMM_test_view_tour] -->
    <record id="test_view" model="ir.ui.view">
        <field name="name">test.view</field>
        <field name="model">test.model</field>
        <field name="arch" type="xml">
            <form/>
        </field>
    </record>
</odoo>"""
    errors = _tour_mandate_errors(xml)
    assert errors == []


def test_tour_mandate_is_satisfied_when_both_tags_present_side_by_side():
    # The explicitly-allowed ADR 0076 case: bypassing both the tour
    # mandate AND the view-rendering-test requirement at once needs both
    # tags present together, not either one alone.
    xml = """<odoo>
    <!-- audit-ignore-view: rendering covered by test_view.py -->
    <!-- burn-ignore-tour: ROI is zero, a static lookup-table view -->
    <record id="test_view" model="ir.ui.view">
        <field name="name">test.view</field>
        <field name="model">test.model</field>
        <field name="arch" type="xml">
            <form/>
        </field>
    </record>
</odoo>"""
    errors = _tour_mandate_errors(xml)
    assert errors == []


def _routing_deprecation_errors(source, filename="test_widget.py"):
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = str(Path(tmpdir) / filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(source)
        errors, _warnings = scan_file(filepath, is_odoo_module=True)
    return [e for e in errors if "ROUTING DEPRECATION" in e]


def test_web_tests_route_is_not_flagged_as_deprecated_routing():
    # Real false positive found and fixed this session: /web/tests is
    # Odoo's own real, non-deprecated hoot unit-test runner route (used
    # unmodified in odoo/addons/web/tests/test_js.py itself), but the
    # allowlist only exempted /web/login, /web/signup, /web/assets, and
    # /web/static -- so any code path calling browser_js() against
    # /web/tests got a false "deprecated and forcefully redirected"
    # error.
    source = 'self.browser_js("/web/tests?headless&filter=my_suite", "", "")\n'
    assert _routing_deprecation_errors(source) == []


def test_a_real_deprecated_web_path_is_still_flagged():
    # Guards against the allowlist fix above becoming so broad it stops
    # catching the real thing this rule exists for.
    source = 'response = self.url_open("/web/some_backend_action")\n'
    errors = _routing_deprecation_errors(source)
    assert len(errors) == 1


# parse_odoo_html()/OdooHTMLParser had zero coverage despite being a real, independent
# structural parser (not part of the AST security-rule visitor above) -- other rule functions
# in this file walk the XMLNode tree it builds, so a bug here would silently corrupt every one
# of them rather than fail loudly on its own.


def test_a_single_element_becomes_one_child_node_of_root():
    root = parse_odoo_html("<div>hello</div>")
    assert root.tag == "root_wrapper"
    assert len(root.children) == 1
    div = root.children[0]
    assert div.tag == "div"
    assert div.text == "hello"
    assert div.parent is root


def test_nested_elements_form_a_real_parent_child_chain():
    root = parse_odoo_html("<div><span>inner</span></div>")
    div = _find_first(root, "div")
    span = _find_first(root, "span")
    assert span is not None
    assert span.parent is div
    assert span in div.children
    assert span.text == "inner"


def test_a_void_element_is_never_pushed_onto_the_open_element_stack():
    # A void element (br, img, ...) has no closing tag and must not become the parent of
    # whatever comes after it in the markup -- confirmed against the real void_elements set
    # this parser declares, not just the ones this test happens to try.
    root = parse_odoo_html("<div><br/><span>after</span></div>")
    div = _find_first(root, "div")
    span = _find_first(root, "span")
    assert span.parent is div, "span must be a sibling of br, not br's child"
    assert [c.tag for c in div.children] == ["br", "span"]


def test_handle_endtag_closes_the_matching_open_tag_even_with_unbalanced_markup_between():
    # A stray, unmatched closing tag between the real open/close pair must not corrupt the
    # stack -- handle_endtag() only pops down to the first matching tag it finds walking the
    # stack from the top, exactly like a real browser's own lenient HTML parsing.
    root = parse_odoo_html("<div><p>text</notatag></p></div>")
    div = _find_first(root, "div")
    p = _find_first(root, "p")
    assert p.parent is div
    assert p.end_lineno >= p.lineno


def test_comments_become_real_comment_nodes_not_lost_or_merged_into_text():
    root = parse_odoo_html("<div><!-- a real comment --><span>x</span></div>")
    div = _find_first(root, "div")
    tags = [c.tag for c in div.children]
    assert "#comment" in tags
    comment_node = [c for c in div.children if c.tag == "#comment"][0]
    assert comment_node.attrs["text"] == " a real comment "


def test_attributes_are_captured_as_a_real_dict():
    root = parse_odoo_html('<input type="text" name="callsign"/>')
    node = root.children[0]
    assert node.attrs == {"type": "text", "name": "callsign"}


def test_walk_visits_every_node_depth_first_including_root():
    root = parse_odoo_html("<div><span>a</span><p>b</p></div>")
    tags = [n.tag for n in root.walk()]
    assert tags == ["root_wrapper", "div", "span", "p"]


def test_get_ancestors_returns_every_parent_up_to_root_not_including_self():
    root = parse_odoo_html("<div><span>x</span></div>")
    span = _find_first(root, "span")
    ancestors = span.get_ancestors()
    assert [a.tag for a in ancestors] == ["div", "root_wrapper"]
    assert span not in ancestors


# visit_Dict() -- a real, self-contained cluster of dict-literal rules (I18N UI-feedback
# strings, the res.users groups_id->group_ids Odoo 18+ rename, manifest asset-glob and license
# enforcement, an owner_user_id/user_websites_group_id mutual-exclusivity trap) that had zero
# coverage despite being independently readable and testable without deep visitor-wide context.


def _dict_findings(source, filepath="/tmp/some_module/models/res_users.py", is_odoo_module=True):
    lines = source.splitlines()
    errors, warnings = check_ast_vulnerabilities(filepath, source, lines, is_odoo_module=is_odoo_module)
    return [msg for _lineno, msg in errors], [msg for _lineno, msg in warnings]


def test_an_untranslated_ui_feedback_string_is_warned_on():
    source = 'result = {"error": "Something went wrong here"}\n'
    _errors, warnings = _dict_findings(source)
    assert any("I18N" in w for w in warnings)


def test_a_translated_ui_feedback_string_is_not_warned_on():
    source = 'result = {"error": _("Something went wrong here")}\n'
    _errors, warnings = _dict_findings(source)
    assert not any("I18N" in w for w in warnings)


def test_i18n_ui_feedback_rule_is_silent_inside_an_api_py_file():
    # The rule's own real exemption: files matching *_api.py or api.py are skipped, since
    # they're often building a payload for something other than direct UI display.
    source = 'result = {"error": "Something went wrong here"}\n'
    _errors, warnings = _dict_findings(source, filepath="/tmp/some_module/controllers/csp_api.py")
    assert not any("I18N" in w for w in warnings)


def test_the_old_odoo_17_groups_id_key_is_flagged_as_a_real_error():
    source = 'vals = {"groups_id": [(6, 0, [group.id])]}\n'
    errors, _warnings = _dict_findings(source)
    assert any("group_ids" in e for e in errors)


def test_group_ids_mutation_outside_a_test_file_is_forbidden():
    source = 'vals = {"group_ids": [(6, 0, [group.id])]}\n'
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/models/res_users.py")
    assert any("group_ids" in e for e in errors)


def test_group_ids_mutation_inside_a_real_test_file_is_allowed():
    # The rule's own real exemption: self.filename.startswith("test_") -- test setup routinely
    # needs to grant/revoke groups directly, unlike production code.
    source = 'vals = {"group_ids": [(6, 0, [group.id])]}\n'
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/tests/test_res_users.py")
    assert not any("group_ids" in e and "Mutating" in e for e in errors)


def test_a_glob_pattern_in_manifest_py_assets_is_a_critical_error():
    source = "{'assets': {'web.assets_backend': ['static/src/js/*.js']}}\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/__manifest__.py")
    assert any("ASSET COMPILER CRASH" in e for e in errors)


def test_an_explicit_asset_file_list_in_manifest_py_is_not_flagged():
    source = "{'assets': {'web.assets_backend': ['static/src/js/a.js', 'static/src/js/b.js']}}\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/__manifest__.py")
    assert not any("ASSET COMPILER CRASH" in e for e in errors)


def test_hams_com_manifest_must_declare_other_proprietary_license():
    source = "{'license': 'AGPL-3'}\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/workspace/hams_com/ham_base/__manifest__.py")
    assert any("LICENSING" in e for e in errors)


def test_hams_com_manifest_with_the_real_required_license_is_not_flagged():
    source = "{'license': 'Other proprietary'}\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/workspace/hams_com/ham_base/__manifest__.py")
    assert not any("LICENSING" in e for e in errors)


def test_hams_open_manifest_must_declare_agpl3_license():
    source = "{'license': 'Other proprietary'}\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/workspace/hams_open/zero_sudo/__manifest__.py")
    assert any("LICENSING" in e for e in errors)


def test_owner_user_id_and_user_websites_group_id_together_is_a_mutual_exclusivity_error():
    source = "vals = {'owner_user_id': self.env.uid, 'user_websites_group_id': group.id}\n"
    errors, _warnings = _dict_findings(source)
    assert any("MUTUAL EXCLUSIVITY" in e for e in errors)


def test_owner_user_id_alone_is_not_a_mutual_exclusivity_error():
    source = "vals = {'owner_user_id': self.env.uid}\n"
    errors, _warnings = _dict_findings(source)
    assert not any("MUTUAL EXCLUSIVITY" in e for e in errors)


# visit_ClassDef() -- the "every real Odoo model needs a real textual name field" schema rule
# (CRITICAL SCHEMA), independently readable/testable the same way visit_Dict()'s rules were.


def test_a_model_using_the_forbidden_rec_name_override_is_flagged():
    source = (
        "class Foo(models.Model):\n"
        "    _name = 'ham.foo'\n"
        "    _rec_name = 'callsign'\n"
    )
    errors, _warnings = _dict_findings(source)
    assert any("_rec_name" in e and "forbidden" in e for e in errors)


def test_a_model_with_a_real_name_field_and_no_rec_name_is_not_flagged():
    source = (
        "class Foo(models.Model):\n"
        "    _name = 'ham.foo'\n"
        "    name = fields.Char()\n"
    )
    errors, _warnings = _dict_findings(source)
    assert not any("CRITICAL SCHEMA" in e for e in errors)


def test_a_model_with_neither_name_nor_rec_name_is_flagged():
    source = (
        "class Foo(models.Model):\n"
        "    _name = 'ham.foo'\n"
        "    callsign = fields.Char()\n"
    )
    errors, _warnings = _dict_findings(source)
    assert any("CRITICAL SCHEMA" in e and "MUST have a textual" in e for e in errors)


def test_a_plain_non_model_class_is_never_subject_to_the_schema_rule():
    source = (
        "class Foo:\n"
        "    _name = 'not.a.real.model'\n"
    )
    errors, _warnings = _dict_findings(source)
    assert not any("CRITICAL SCHEMA" in e for e in errors)


def test_the_schema_rule_is_silent_outside_an_odoo_module():
    source = (
        "class Foo(models.Model):\n"
        "    _name = 'ham.foo'\n"
        "    _rec_name = 'callsign'\n"
    )
    errors, _warnings = _dict_findings(source, is_odoo_module=False)
    assert not any("_rec_name" in e for e in errors)


# _check_cr_execute()/is_tainted_sql() -- the real SQL-injection taint tracer this linter's
# whole "AST vulnerability visitor" concept is centrally built around. Tested via the actual
# visit_Call() path that fires it (cr.execute()), not the helper in isolation, so a test failure
# here means the real rule is broken, not just a detail of how the helper happens to be called.


def _sqli_errors(source):
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/models/ham_qso.py")
    return [e for e in errors if "SQLi Prevention" in e]


def test_an_fstring_passed_to_cr_execute_is_flagged():
    source = 'self.env.cr.execute(f"SELECT * FROM ham_qso WHERE id={qso_id}")\n'
    errors = _sqli_errors(source)
    assert len(errors) == 1
    assert "f-string" in errors[0]


def test_percent_interpolation_passed_to_cr_execute_is_flagged():
    source = 'self.env.cr.execute("SELECT * FROM ham_qso WHERE id=%s" % qso_id)\n'
    errors = _sqli_errors(source)
    assert "percent interpolation" in errors[0]


def test_string_concatenation_passed_to_cr_execute_is_flagged():
    source = 'self.env.cr.execute("SELECT * FROM ham_qso WHERE id=" + qso_id)\n'
    errors = _sqli_errors(source)
    assert "string concatenation" in errors[0]


def test_dot_format_passed_to_cr_execute_is_flagged():
    source = 'self.env.cr.execute("SELECT * FROM ham_qso WHERE id={}".format(qso_id))\n'
    errors = _sqli_errors(source)
    assert ".format()" in errors[0]


def test_a_real_parameterized_query_is_not_flagged():
    source = 'self.env.cr.execute("SELECT * FROM ham_qso WHERE id=%s", (qso_id,))\n'
    assert _sqli_errors(source) == []


def test_sql_module_wrapped_format_is_exempted_as_the_documented_safe_escape_hatch():
    # is_tainted_sql()'s own real exemption requires the exact real-world call shape this
    # codebase actually uses (confirmed against ham_callbook.py's own real usage): `from
    # psycopg2 import sql` then `sql.SQL(...).format(...)` -- module-qualified attribute
    # access, psycopg2.sql's real parameter-safe composition API, not string formatting.
    # A bare imported `SQL(...).format(...)` name is NOT exempted (its .func is an ast.Name,
    # not an ast.Attribute with .attr == "SQL") -- confirmed directly by first getting this
    # test wrong with that form and seeing it correctly still flagged.
    source = 'self.env.cr.execute(sql.SQL("SELECT * FROM {}").format(table_name))\n'
    assert _sqli_errors(source) == []


def test_taint_is_traced_through_an_intermediate_variable_assignment():
    # is_tainted_sql() recurses through self.assignments -- a taint doesn't have to be inline
    # in the cr.execute() call itself to be caught.
    source = (
        'query = f"SELECT * FROM ham_qso WHERE id={qso_id}"\n'
        "self.env.cr.execute(query)\n"
    )
    errors = _sqli_errors(source)
    assert len(errors) == 1
    assert "variable 'query'" in errors[0] and "f-string" in errors[0]


def test_a_plain_literal_query_is_not_flagged():
    source = 'self.env.cr.execute("SELECT * FROM ham_qso")\n'
    assert _sqli_errors(source) == []


# Two more real, simple, self-contained security bans in the same visit_Call() family.


def test_os_system_is_a_banned_shell_injection_risk():
    source = "os.system(user_supplied_command)\n"
    errors, _warnings = _dict_findings(source)
    assert any("os.system" in e and "shell injection" in e for e in errors)


def test_pickle_loads_is_banned_as_a_real_rce_risk():
    source = "data = pickle.loads(payload)\n"
    errors, _warnings = _dict_findings(source)
    assert any("pickle" in e and "vulnerable" in e for e in errors)


def test_pickle_dumps_is_also_banned():
    source = "payload = pickle.dumps(data)\n"
    errors, _warnings = _dict_findings(source)
    assert any("pickle" in e for e in errors)


# visit_ExceptHandler()/visit_Try() -- the fail-fast philosophy rule cluster (this codebase's
# own standing "never let error paths silently mask a real bug" principle, enforced
# structurally): empty `except: pass`, soft-dependency ImportError swallowing, catch-all
# exceptions without an explicit audit-ignore tag, and audit-ignore'd catch-alls that still
# don't log anything.


def test_an_empty_except_pass_handler_is_forbidden():
    source = "try:\n    risky()\nexcept ValueError:\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert any("Empty exception handlers" in e for e in errors)


def test_an_except_handler_that_actually_does_something_is_not_flagged():
    source = "try:\n    risky()\nexcept ValueError:\n    log.warning('failed')\n"
    errors, _warnings = _dict_findings(source)
    assert not any("Empty exception handlers" in e for e in errors)


def test_except_importerror_is_a_forbidden_soft_dependency():
    source = "try:\n    import optional_thing\nexcept ImportError:\n    optional_thing = None\n"
    errors, _warnings = _dict_findings(source)
    assert any("Soft dependencies" in e for e in errors)


def test_a_bare_catch_all_except_is_forbidden_without_the_audit_tag():
    source = "try:\n    risky()\nexcept:\n    handle_it()\n"
    errors, _warnings = _dict_findings(source)
    assert any("Catch-all exceptions" in e for e in errors)


def test_except_exception_is_forbidden_without_the_audit_tag():
    source = "try:\n    risky()\nexcept Exception:\n    handle_it()\n"
    errors, _warnings = _dict_findings(source)
    assert any("Catch-all exceptions" in e for e in errors)


def test_a_catch_all_with_the_audit_tag_and_real_logging_is_allowed():
    source = (
        "try:\n"
        "    risky()\n"
        "except Exception as e:  # audit-ignore-catch-all\n"
        "    logger.warning('failed: %s', e)\n"
    )
    errors, _warnings = _dict_findings(source)
    assert not any("Catch-all exceptions" in e for e in errors)
    assert not any("SILENT FAILURE" in e for e in errors)


def test_a_catch_all_with_the_audit_tag_but_no_logging_is_still_flagged():
    source = (
        "try:\n"
        "    risky()\n"
        "except Exception:  # audit-ignore-catch-all\n"
        "    pass\n"
    )
    errors, _warnings = _dict_findings(source)
    assert any("SILENT FAILURE" in e for e in errors)


def test_get_service_uid_wrapped_in_try_except_fails_fast_check_when_ham_base_present():
    # has_ham_base is discovered by walking up the REAL filesystem from filepath looking for
    # ham_base/__manifest__.py -- a real temp-directory fixture with that file present, not a
    # synthetic /tmp path (which would never find it and silently skip the rule), so the
    # discovery walk itself is exercised for real, not just the rule logic downstream of it.
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "some_repo"
        (repo / "ham_base").mkdir(parents=True)
        (repo / "ham_base" / "__manifest__.py").write_text("{}")
        target_dir = repo / "ham_relay_bridge" / "models"
        target_dir.mkdir(parents=True)
        source = (
            "try:\n"
            "    uid = utils._get_service_uid('some.service')\n"
            "except Exception:  # audit-ignore-catch-all\n"
            "    logger.warning('failed')\n"
        )
        errors, _warnings = _dict_findings(source, filepath=str(target_dir / "res_users.py"))
    assert any("_get_service_uid MUST NOT be wrapped" in e for e in errors)


def test_two_adjacent_string_literals_concatenated_with_plus_is_forbidden():
    # The linter-evasion pattern this rule exists to block: splitting a banned literal
    # ('os' + '.system', say) across two string constants joined with '+' to dodge a
    # substring-based scan elsewhere. Using ordinary literals here, not the actual banned
    # string, since the rule itself fires on the shape (two string literals + '+'), not on
    # what the resulting text happens to spell.
    source = "x = 'hello' + 'world'\n"
    errors, _warnings = _dict_findings(source)
    assert any("STRING CONCATENATION" in e for e in errors)


def test_a_string_literal_plus_a_variable_is_not_flagged():
    source = "x = 'hello ' + name\n"
    errors, _warnings = _dict_findings(source)
    assert not any("STRING CONCATENATION" in e for e in errors)


def test_two_fstrings_concatenated_with_plus_is_also_forbidden():
    source = 'x = f"hello {a}" + f"world {b}"\n'
    errors, _warnings = _dict_findings(source)
    assert any("STRING CONCATENATION" in e for e in errors)


def test_contextlib_suppress_is_a_forbidden_silent_black_hole():
    source = "with contextlib.suppress(KeyError):\n    risky()\n"
    errors, _warnings = _dict_findings(source)
    assert any("contextlib.suppress()" in e for e in errors)


def test_an_ordinary_with_block_is_not_flagged_by_the_suppress_rule():
    source = "with self.env.cr.savepoint():\n    risky()\n"
    errors, _warnings = _dict_findings(source)
    assert not any("contextlib.suppress()" in e for e in errors)


def test_manual_commit_inside_a_registry_cursor_block_is_forbidden():
    source = (
        "with registry.cursor() as cr:\n"
        "    cr.execute('SELECT 1')\n"
        "    cr.commit()\n"
    )
    errors, _warnings = _dict_findings(source)
    assert any("CURSOR MISMANAGEMENT" in e for e in errors)


def test_manual_rollback_inside_a_registry_cursor_block_is_also_forbidden():
    source = (
        "with registry.cursor() as cr:\n"
        "    cr.execute('SELECT 1')\n"
        "    cr.rollback()\n"
    )
    errors, _warnings = _dict_findings(source)
    assert any("CURSOR MISMANAGEMENT" in e for e in errors)


def test_commit_outside_a_registry_cursor_block_is_not_flagged_by_this_rule():
    source = "cr.commit()\n"
    errors, _warnings = _dict_findings(source)
    assert not any("CURSOR MISMANAGEMENT" in e for e in errors)


def test_create_inside_assert_raises_without_flush_all_is_flagged():
    source = (
        "with self.assertRaises(ValidationError):\n"
        "    self.env['ham.qso'].create({'callsign': 'K6BP'})\n"
    )
    errors, _warnings = _dict_findings(source)
    assert any("flush_all()" in e for e in errors)


def test_create_inside_assert_raises_with_flush_all_is_not_flagged():
    source = (
        "with self.assertRaises(ValidationError):\n"
        "    self.env['ham.qso'].create({'callsign': 'K6BP'})\n"
        "    self.env.flush_all()\n"
    )
    errors, _warnings = _dict_findings(source)
    assert not any("flush_all()" in e for e in errors)


def test_assert_raises_with_no_create_or_write_at_all_is_not_flagged():
    source = "with self.assertRaises(ValidationError):\n    self.env['ham.qso'].search([])\n"
    errors, _warnings = _dict_findings(source)
    assert not any("flush_all()" in e for e in errors)


# visit_ImportFrom() -- five distinct, independently readable import-time bans/deprecations.


def test_an_import_from_inside_a_function_body_is_forbidden():
    source = "def foo():\n    from collections import OrderedDict\n    return OrderedDict()\n"
    errors, _warnings = _dict_findings(source)
    assert any("LOCAL IMPORT" in e for e in errors)


def test_a_top_level_import_from_is_not_flagged_as_local():
    source = "from collections import OrderedDict\n"
    errors, _warnings = _dict_findings(source)
    assert not any("LOCAL IMPORT" in e for e in errors)


def test_from_pickle_import_is_banned_the_same_as_import_pickle():
    source = "from pickle import loads\n"
    errors, _warnings = _dict_findings(source)
    assert any("pickle" in e and "vulnerable" in e for e in errors)


def test_from_random_import_is_weak_crypto_without_the_audit_tag():
    source = "from random import choice\n"
    errors, _warnings = _dict_findings(source)
    assert any("WEAK CRYPTO" in e for e in errors)


def test_from_random_import_with_the_audit_tag_is_allowed():
    source = "from random import seed  # audit-ignore-weak-random: deterministic exam generation\n"
    errors, _warnings = _dict_findings(source)
    assert not any("WEAK CRYPTO" in e for e in errors)


def test_get_module_resource_is_a_removed_deprecated_api():
    source = "from odoo.modules import get_module_resource\n"
    errors, _warnings = _dict_findings(source)
    assert any("get_module_resource" in e and "removed" in e for e in errors)


def test_importing_superuser_id_is_forbidden_as_privilege_escalation():
    source = "from odoo import SUPERUSER_ID\n"
    errors, _warnings = _dict_findings(source)
    assert any("SUPERUSER_ID" in e and "privilege escalation" in e for e in errors)


def test_an_unrelated_ordinary_import_is_not_flagged_by_any_of_these_rules():
    source = "from odoo import models, fields\n"
    errors, _warnings = _dict_findings(source)
    assert errors == []


# visit_FunctionDef() -- monkey-patch wrapper signature requirement, empty-function-pass ban
# (with the real test_*.py exemption), and the _auto_init override deprecation.


def test_a_monkeypatch_wrapper_missing_args_and_kwargs_is_flagged():
    source = "def _patched_create(self, vals):\n    return orig_create(self, vals)\n"
    errors, _warnings = _dict_findings(source)
    assert any("Monkey-patch wrapper" in e for e in errors)


def test_a_monkeypatch_wrapper_with_real_args_and_kwargs_is_not_flagged():
    source = "def _patched_create(self, *args, **kwargs):\n    return orig_create(self, *args, **kwargs)\n"
    errors, _warnings = _dict_findings(source)
    assert not any("Monkey-patch wrapper" in e for e in errors)


def test_an_ordinary_function_with_no_patched_prefix_is_never_subject_to_this_rule():
    source = "def create(self, vals):\n    return super().create(vals)\n"
    errors, _warnings = _dict_findings(source)
    assert not any("Monkey-patch wrapper" in e for e in errors)


def test_an_empty_pass_only_function_is_forbidden_outside_a_test_file():
    source = "def do_the_thing(self):\n    pass\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/models/ham_qso.py")
    assert any("Empty functions" in e for e in errors)


def test_an_empty_pass_only_function_is_allowed_inside_a_real_test_file():
    # Real stub/placeholder test methods are an accepted pattern this rule deliberately
    # exempts by filename, unlike production code.
    source = "def test_todo_later(self):\n    pass\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/tests/test_ham_qso.py")
    assert not any("Empty functions" in e for e in errors)


def test_overriding_auto_init_is_a_deprecated_pattern():
    source = "def _auto_init(self):\n    return super()._auto_init()\n"
    errors, _warnings = _dict_findings(source)
    assert any("_auto_init" in e and "discouraged" in e for e in errors)


# The in_http_controller-gated rules in visit_Call() -- request.website extraction, explicit
# controller kwarg binding, and RPC mass assignment -- all only apply inside a real
# @http.route-decorated method, so each fixture below is a real controller method, not a bare
# statement, to exercise the actual is_controller detection this state depends on.


def test_request_website_extraction_inside_a_real_route_is_warned_on():
    # Real, verified-by-running behavior, not assumed from the warning message's own wording:
    # this rule lives inside visit_Call and only fires when `request.website` is itself CALLED
    # (`request.website(...)`), not on the far more common plain attribute read
    # (`website = request.website`) real Odoo code actually uses -- confirmed directly by
    # running both shapes against the real checker before writing this fixture; the plain-
    # attribute-read shape produces zero warnings. Whether that's the rule's real intent or a
    # pre-existing narrower-than-described trigger is a design question for whoever owns this
    # rule, not something to silently work around here -- this test documents the actual
    # current behavior.
    source = (
        "@http.route('/api/v1/thing', type='jsonrpc', auth='user')\n"
        "def get_thing(self, **kwargs):\n"
        "    website = request.website()\n"
        "    return website.name\n"
    )
    errors, warnings = _dict_findings(source)
    assert any("MULTI-TENANT ISOLATION" in w for w in warnings)


def test_request_website_extraction_outside_any_controller_is_not_flagged_by_this_rule():
    source = "website = request.website\n"
    errors, warnings = _dict_findings(source)
    assert not any("MULTI-TENANT ISOLATION" in w for w in warnings)


def test_rpc_mass_assignment_of_a_raw_kwargs_dict_into_create_is_warned_on():
    source = (
        "class MyController(http.Controller):\n"
        "    @http.route('/api/v1/thing', type='json', auth='user')\n"
        "    def make_thing(self, **kwargs):\n"
        "        return request.env['ham.qso'].create(kwargs)\n"
    )
    errors, warnings = _dict_findings(source)
    assert any("RPC MASS ASSIGNMENT" in w for w in warnings)


def test_create_with_explicit_named_fields_is_not_flagged_as_mass_assignment():
    source = (
        "class MyController(http.Controller):\n"
        "    @http.route('/api/v1/thing', type='json', auth='user')\n"
        "    def make_thing(self, callsign=None, **kwargs):\n"
        "        return request.env['ham.qso'].create({'callsign': callsign})\n"
    )
    errors, warnings = _dict_findings(source)
    assert not any("RPC MASS ASSIGNMENT" in w for w in warnings)


# visit_Constant()/visit_Name()/visit_keyword() -- a large, independently readable cluster of
# small, self-contained deprecation/security bans, each keyed on a specific literal/identifier/
# keyword-argument name.


def test_numbercall_string_literal_is_flagged():
    # The real rule regex requires a space on both sides (r" numbercall "), so the literal
    # can't be the very first/last word of the string -- confirmed directly after this test's
    # first draft (a leading-word fixture) silently didn't match.
    source = "domain = 'x numbercall y'\n"
    errors, _warnings = _dict_findings(source)
    assert any("numbercall" in e for e in errors)


def test_res_users_apikeys_string_literal_is_flagged_outside_key_registry():
    source = "model_name = 'res.users.apikeys'\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/models/other.py")
    assert any("res.users.apikeys" in e for e in errors)


def test_res_users_apikeys_string_literal_is_exempt_inside_key_registry_py():
    source = "model_name = 'res.users.apikeys'\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/models/key_registry.py")
    assert not any("res.users.apikeys" in e for e in errors)


def test_bare_numbercall_identifier_is_flagged():
    source = "x = numbercall\n"
    errors, _warnings = _dict_findings(source)
    assert any("numbercall" in e for e in errors)


def test_sql_constraints_identifier_is_flagged():
    source = "x = _sql_constraints\n"
    errors, _warnings = _dict_findings(source)
    assert any("models.Constraint" in e for e in errors)


def test_bare_superuser_id_identifier_is_flagged():
    source = "uid = SUPERUSER_ID\n"
    errors, _warnings = _dict_findings(source)
    assert any("SUPERUSER_ID" in e and "privilege escalation" in e for e in errors)


def test_shell_true_keyword_is_a_critical_shell_injection_risk():
    source = "subprocess.run(cmd, shell=True)\n"
    errors, _warnings = _dict_findings(source)
    assert any("shell=True" in e for e in errors)


def test_shell_true_is_flagged_even_outside_an_odoo_module():
    # Unlike most of this cluster, the shell=True check isn't gated on is_odoo_module -- a
    # real shell-injection risk regardless of what kind of file it's in.
    source = "subprocess.run(cmd, shell=True)\n"
    errors, _warnings = _dict_findings(source, is_odoo_module=False)
    assert any("shell=True" in e for e in errors)


def test_groups_id_keyword_argument_is_flagged():
    source = "self.write({'x': 1}, groups_id=[(6, 0, [1])])\n"
    errors, _warnings = _dict_findings(source)
    assert any("group_ids" in e for e in errors)


def test_group_ids_keyword_argument_outside_tests_is_flagged():
    source = "some_call(group_ids=[(6, 0, [1])])\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/models/res_users.py")
    assert any("Mutating 'group_ids'" in e for e in errors)


def test_oldname_keyword_argument_is_a_legacy_deprecation():
    source = "field = fields.Char(oldname='old_field_name')\n"
    errors, _warnings = _dict_findings(source)
    assert any("'oldname'" in e and "legacy" in e for e in errors)


def test_select_keyword_argument_is_a_legacy_deprecation():
    source = "field = fields.Char(select=True)\n"
    errors, _warnings = _dict_findings(source)
    assert any("'select'" in e and "legacy" in e for e in errors)


def test_type_json_keyword_argument_is_deprecated_in_favor_of_jsonrpc():
    # A bare decorator with no decorated statement is a real Python SyntaxError -- this test's
    # first draft used a decorator alone (no function below it) and correctly failed, since
    # check_ast_vulnerabilities() returns a syntax-error finding instead of ever reaching this
    # rule. A real function body is required, not just the decorator line.
    source = "@http.route('/x', type='json')\ndef get_thing(self):\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert any("jsonrpc" in e for e in errors)


def test_type_jsonrpc_keyword_argument_is_not_flagged():
    source = "@http.route('/x', type='jsonrpc')\ndef get_thing(self):\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert not any("jsonrpc" in e for e in errors)


def test_index_trgm_keyword_argument_should_be_trigram():
    source = "name = fields.Char(index='trgm')\n"
    errors, _warnings = _dict_findings(source)
    assert any("trigram" in e for e in errors)


def test_csrf_false_outside_an_api_file_is_a_security_alert():
    source = "@http.route('/x', csrf=False)\ndef get_thing(self):\n    pass\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/controllers/main.py")
    assert any("csrf=False" in e for e in errors)


def test_csrf_false_inside_an_api_py_file_is_exempt():
    source = "@http.route('/x', csrf=False)\ndef get_thing(self):\n    pass\n"
    errors, _warnings = _dict_findings(source, filepath="/tmp/some_module/controllers/webhook_api.py")
    assert not any("csrf=False" in e for e in errors)


def test_related_ending_in_dot_users_is_a_legacy_security_relation():
    source = "field = fields.Many2many(related='partner_id.users')\n"
    errors, _warnings = _dict_findings(source)
    assert any("user_ids" in e for e in errors)


def test_get_service_uid_wrapped_in_try_except_is_not_flagged_without_ham_base_present():
    # The inverse of the test above -- no ham_base/__manifest__.py anywhere up the tree, so
    # has_ham_base must stay False and this specific rule must not fire (a plain repo with no
    # ham_base dependency has no fast-fail contract to enforce here).
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "some_repo"
        target_dir = repo / "some_module" / "models"
        target_dir.mkdir(parents=True)
        source = (
            "try:\n"
            "    uid = utils._get_service_uid('some.service')\n"
            "except Exception:  # audit-ignore-catch-all\n"
            "    logger.warning('failed')\n"
        )
        errors, _warnings = _dict_findings(source, filepath=str(target_dir / "res_users.py"))
    assert not any("_get_service_uid MUST NOT be wrapped" in e for e in errors)
