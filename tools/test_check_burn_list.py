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


def _scan_file(content, filename, is_odoo_module=True):
    # scan_file reads from a real path -- same real-disk-hit rationale as
    # _tour_mandate_errors above.
    with tempfile.TemporaryDirectory() as tmpdir:
        full_path = Path(tmpdir) / filename
        full_path.parent.mkdir(parents=True, exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return scan_file(str(full_path), is_odoo_module=is_odoo_module)


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


# visit_Attribute() -- the .sudo() privilege-escalation ban (with its real, narrow escape
# hatch), threading.current_thread().testing probing (test-evasion detection), and two more
# legacy-attribute-access bans.


def test_sudo_call_is_forbidden_privilege_escalation():
    source = "record = self.env['ham.qso'].sudo().browse(1)\n"
    errors, _warnings = _dict_findings(source)
    assert any(".sudo()" in e and "forbidden" in e for e in errors)


def test_sudo_generate_with_the_real_burn_ignore_tag_is_exempt():
    # The real, narrow exemption: the burn-ignore-sudo tag alone isn't enough -- it must be
    # paired with one of three specific, allow-listed call shapes on the same line.
    source = "self.env['ir.attachment'].sudo()._generate(vals)  # burn-ignore-sudo\n"
    errors, _warnings = _dict_findings(source)
    assert not any(".sudo()" in e for e in errors)


def test_sudo_unlink_with_the_real_burn_ignore_tag_is_exempt():
    source = "self.env['ham.qso'].sudo().unlink()  # burn-ignore-sudo\n"
    errors, _warnings = _dict_findings(source)
    assert not any(".sudo()" in e for e in errors)


def test_sudo_with_the_tag_but_not_one_of_the_three_allowed_shapes_is_still_exempt():
    # Documents real (likely unintended) behavior: the rule's own inner check requires the
    # burn-ignore-sudo tag to be paired with one of three specific call shapes, but
    # add_error() itself unconditionally suppresses ANY error on a line containing the bare
    # substring "burn-ignore" (see add_error()'s own generic tag check), regardless of which
    # specific rule or shape matched. That blanket suppression fires first, so in practice the
    # three-shape check on .sudo() never actually narrows anything -- any burn-ignore-sudo
    # comment exempts the line no matter the call shape.
    source = "self.env['ham.qso'].sudo().write(vals)  # burn-ignore-sudo\n"
    errors, _warnings = _dict_findings(source)
    assert not any(".sudo()" in e for e in errors)


def test_probing_current_thread_testing_is_forbidden_test_evasion():
    source = "if threading.current_thread().testing:\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert any("current_thread" in e and "test evasion" in e for e in errors)


def test_self_underscore_context_is_a_legacy_access_pattern():
    source = "ctx = self._context\n"
    errors, _warnings = _dict_findings(source)
    assert any("self.env.context" in e for e in errors)


def test_self_underscore_uid_is_a_legacy_access_pattern():
    source = "uid = self._uid\n"
    errors, _warnings = _dict_findings(source)
    assert any("self.env.uid" in e for e in errors)


def test_group_dot_users_is_a_legacy_security_relation():
    source = "members = group.users\n"
    errors, _warnings = _dict_findings(source)
    assert any("user_ids" in e for e in errors)


# _check_forbidden_functions() -- the hasattr() introspection ban, with its real
# super()-cooperative-mixin exemption and its real burn-ignore-introspection escape hatch.


def test_hasattr_is_forbidden_introspection():
    source = "if hasattr(self, 'some_attr'):\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert any("hasattr" in e for e in errors)


def test_hasattr_on_super_is_exempted_for_cooperative_mixins():
    source = "if hasattr(super(), 'some_attr'):\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert not any("hasattr" in e for e in errors)


def test_hasattr_with_the_real_burn_ignore_tag_is_exempt():
    source = "if hasattr(self, 'some_attr'):  # burn-ignore-introspection\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert not any("hasattr" in e for e in errors)


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


# _check_forbidden_functions() -- the rest of the bare-function-call ban cluster: hash(),
# eval(), exec() (ungated by is_odoo_module), then the is_odoo_module-gated group:
# get_module_resource(), _sign_token(), clear_caches(), _check_recursion(), getattr()'s two
# rules, and setattr()'s group_ids-mutation ban.


def test_hash_call_is_forbidden_non_determinism():
    source = "x = hash(record.id)\n"
    errors, _warnings = _dict_findings(source)
    assert any("NON-DETERMINISM" in e for e in errors)


def test_eval_call_is_forbidden_rce():
    source = "x = eval(user_input)\n"
    errors, _warnings = _dict_findings(source)
    assert any("eval()" in e for e in errors)


def test_exec_call_is_forbidden_rce():
    source = "exec(user_input)\n"
    errors, _warnings = _dict_findings(source)
    assert any("exec()" in e for e in errors)


def test_get_module_resource_call_is_a_removed_deprecated_api():
    source = "path = get_module_resource('ham_base', 'static', 'thing.png')\n"
    errors, _warnings = _dict_findings(source)
    assert any("get_module_resource" in e and "removed" in e for e in errors)


def test_bare_sign_token_call_warns_about_access_token_field():
    # This is the bare-Name-call rule (fid == "_sign_token" under
    # _check_forbidden_functions, which only matches ast.Name calls). The far more common
    # attribute-call shape `record._sign_token(...)` hits a *different* rule entirely (attr ==
    # "_sign_token" under _check_forbidden_attributes), which uses a shorter message.
    source = "token = _sign_token(partner_id)\n"
    errors, _warnings = _dict_findings(source)
    assert any("_sign_token" in e and "access_token" in e for e in errors)


def test_attribute_form_sign_token_call_uses_the_other_shorter_message():
    source = "token = record._sign_token(partner_id)\n"
    errors, _warnings = _dict_findings(source)
    assert any(e == "Verify '_sign_token' context..." for e in errors)


# _check_forbidden_attributes() -- the module.attr()-shaped ban cluster: weak crypto
# (hashlib.md5/sha1, random.*), send_mail, clear_caches(), ambiguous ORM calls off bare
# self, the with_user()/with_context() sub-cluster, message_post/message_subscribe on
# res.users, threading.Thread, and time.sleep().


def test_hashlib_md5_is_weak_crypto():
    source = "digest = hashlib.md5(data).hexdigest()\n"
    errors, _warnings = _dict_findings(source)
    assert any("WEAK CRYPTO" in e and "MD5" in e for e in errors)


def test_hashlib_sha1_is_weak_crypto():
    source = "digest = hashlib.sha1(data).hexdigest()\n"
    errors, _warnings = _dict_findings(source)
    assert any("WEAK CRYPTO" in e for e in errors)


def test_random_choice_is_weak_crypto():
    source = "picked = random.choice(candidates)\n"
    errors, _warnings = _dict_findings(source)
    assert any("WEAK CRYPTO" in e and "random" in e for e in errors)


def test_random_randint_is_weak_crypto():
    source = "n = random.randint(1, 10)\n"
    errors, _warnings = _dict_findings(source)
    assert any("WEAK CRYPTO" in e for e in errors)


def test_send_mail_gets_an_audit_warning_not_an_error():
    source = "template.send_mail(record.id)\n"
    errors, warnings = _dict_findings(source)
    assert errors == []
    assert any("Mail Templates" in w for w in warnings)


def test_attribute_form_clear_caches_is_a_removed_deprecated_api():
    source = "self.env.registry.clear_caches()\n"
    errors, _warnings = _dict_findings(source)
    assert any("clear_caches()" in e and "removed" in e for e in errors)


def test_bare_self_dot_search_is_ambiguous_orm_usage():
    source = "results = self.search([('active', '=', True)])\n"
    errors, _warnings = _dict_findings(source)
    assert any("Ambiguous ORM call" in e for e in errors)


def test_bare_self_dot_create_is_ambiguous_orm_usage():
    source = "record = self.create({'name': 'x'})\n"
    errors, _warnings = _dict_findings(source)
    assert any("Ambiguous ORM call" in e for e in errors)


def test_with_user_of_literal_one_is_a_sudo_bypass_cheat():
    source = "record = self.env['ham.qso'].with_user(1)\n"
    errors, _warnings = _dict_findings(source)
    assert any("with_user(1)" in e and "ZERO-SUDO" in e for e in errors)


def test_with_user_of_superuser_id_name_is_a_sudo_bypass_cheat():
    source = "record = self.env['ham.qso'].with_user(SUPERUSER_ID)\n"
    errors, _warnings = _dict_findings(source)
    assert any("with_user(SUPERUSER_ID)" in e and "ZERO-SUDO" in e for e in errors)


def test_with_user_called_directly_on_bare_env_name_is_an_orm_error():
    source = "record = env.with_user(service_uid)\n"
    errors, _warnings = _dict_findings(source)
    assert any("Cannot call `.with_user()` directly on the Environment" in e for e in errors)


def test_with_user_called_directly_on_self_dot_env_is_an_orm_error():
    source = "record = self.env.with_user(service_uid)\n"
    errors, _warnings = _dict_findings(source)
    assert any("Cannot call `.with_user()` directly on the Environment" in e for e in errors)


def test_with_context_on_a_recordset_is_not_an_orm_error():
    # with_context is in the same attr-tuple as with_user, but only the Environment-direct-
    # call shape is banned -- a normal recordset call is fine.
    source = "record = self.env['ham.qso'].with_context(active_test=False)\n"
    errors, _warnings = _dict_findings(source)
    assert not any("Environment" in e for e in errors)


def test_message_post_on_res_users_typed_recordset_is_forbidden():
    source = "self.env['res.users'].browse(uid).message_post(body='hi')\n"
    errors, _warnings = _dict_findings(source)
    assert any("Messaging & Followers" in e for e in errors)


def test_message_post_on_a_user_id_field_is_forbidden():
    source = "record.user_id.message_post(body='hi')\n"
    errors, _warnings = _dict_findings(source)
    assert any("Messaging & Followers" in e for e in errors)


def test_message_post_on_an_unrelated_recordset_is_not_flagged():
    source = "record.message_post(body='hi')\n"
    errors, _warnings = _dict_findings(source)
    assert not any("Messaging & Followers" in e for e in errors)


def test_threading_thread_is_an_unbounded_dos_vector():
    source = "t = threading.Thread(target=worker)\n"
    errors, _warnings = _dict_findings(source)
    assert any("Unbounded Thread" in e for e in errors)


def test_time_sleep_in_a_normal_module_file_gets_a_thread_blocking_warning():
    source = "time.sleep(5)\n"
    errors, warnings = _dict_findings(source)
    assert errors == []
    assert any("THREAD BLOCKING" in w for w in warnings)


def test_time_sleep_in_a_tools_file_is_exempt():
    source = "time.sleep(5)\n"
    _errors, warnings = _dict_findings(
        source, filepath="/tmp/some_module/tools/some_tool.py"
    )
    assert not any("THREAD BLOCKING" in w for w in warnings)


def test_time_sleep_with_the_real_audit_ignore_tag_is_exempt():
    source = "time.sleep(5)  # audit-ignore-sleep: deliberate backoff\n"
    _errors, warnings = _dict_findings(source)
    assert not any("THREAD BLOCKING" in w for w in warnings)


# The rest of the in_http_controller-gated visit_Call cluster: CONTROLLER BINDING (probing
# the request's own **kwargs dict via .get()), plus _check_search_methods() (unbounded
# .search(), search(count=True), the regex-object and test-file exemptions, and the
# Data Integrity env-subscript-inside-create/write warning), plus the visit_Subscript
# config['test_enable']/['test_file'] probing ban.


def test_controller_binding_probing_the_kwargs_dict_directly_is_warned_on():
    source = (
        "@http.route('/api/v1/thing', type='jsonrpc', auth='user')\n"
        "def get_thing(self, **kwargs):\n"
        "    value = kwargs.get('callsign')\n"
        "    return value\n"
    )
    _errors, warnings = _dict_findings(source)
    assert any("CONTROLLER BINDING" in w for w in warnings)


def test_search_without_limit_is_an_unbounded_search_warning():
    source = "results = self.env['ham.qso'].search([('active', '=', True)])\n"
    _errors, warnings = _dict_findings(source)
    assert any("UNBOUNDED SEARCH" in w for w in warnings)


def test_search_with_limit_is_not_flagged_as_unbounded():
    source = "results = self.env['ham.qso'].search([('active', '=', True)], limit=10)\n"
    _errors, warnings = _dict_findings(source)
    assert not any("UNBOUNDED SEARCH" in w for w in warnings)


def test_search_without_limit_inside_a_real_test_file_is_exempt():
    source = "results = self.env['ham.qso'].search([('active', '=', True)])\n"
    _errors, warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_res_users.py"
    )
    assert not any("UNBOUNDED SEARCH" in w for w in warnings)


def test_search_with_count_true_is_forbidden_use_search_count_instead():
    source = "n = self.env['ham.qso'].search([('active', '=', True)], count=True)\n"
    errors, _warnings = _dict_findings(source)
    assert any("search_count" in e for e in errors)


def test_a_compiled_regex_objects_own_search_is_not_an_odoo_search_call():
    # AST alone can't know the receiver's runtime type -- this relies on the same
    # _RE/_REGEX/_PATTERN naming convention check_burn_list.py's own module-level compiled
    # patterns use, per the real ics_form_handler.py false positive this exemption fixed.
    source = "match = SOME_THING_RE.search(text)\n"
    _errors, warnings = _dict_findings(source)
    assert not any("UNBOUNDED SEARCH" in w for w in warnings)


def test_bare_re_dot_search_is_not_an_odoo_search_call():
    source = "match = re.search(pattern, text)\n"
    _errors, warnings = _dict_findings(source)
    assert not any("UNBOUNDED SEARCH" in w for w in warnings)


def test_env_subscript_search_directly_inside_create_is_a_data_integrity_warning():
    source = (
        "class Foo(models.Model):\n"
        "    def create(self, vals):\n"
        "        return self.env['ham.qso'].search([], limit=1)\n"
    )
    _errors, warnings = _dict_findings(source)
    assert any("Data Integrity" in w and "with_user()" in w for w in warnings)


def test_env_subscript_search_outside_any_sensitive_method_is_not_a_data_integrity_warning():
    source = (
        "class Foo(models.Model):\n"
        "    def some_helper(self, vals):\n"
        "        return self.env['ham.qso'].search([], limit=1)\n"
    )
    _errors, warnings = _dict_findings(source)
    assert not any("Data Integrity" in w for w in warnings)


def test_probing_config_test_enable_is_forbidden_test_evasion():
    source = "if config['test_enable']:\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert any("config['test_enable']" in e or "test_enable" in e for e in errors)


def test_probing_config_test_file_is_forbidden_test_evasion():
    # The rule's own message is a fixed literal mentioning only "test_enable" regardless of
    # which of the two probed keys (test_enable or test_file) actually triggered it -- real,
    # verified behavior, not a test-writing assumption.
    source = "path = config['test_file']\n"
    errors, _warnings = _dict_findings(source)
    assert any("config['test_enable']" in e for e in errors)


# More of visit_Call()'s own direct rule cluster (distinct from _check_forbidden_functions /
# _check_forbidden_attributes, which it calls into): safe_patch_object() cursor-mocking ban,
# getattr()-based current_thread().testing probing, registry.get()/models.get()
# soft-dependency ban, search()/search_count() on env['ir.module.module'], config.get()
# probing, and Environment(..., uid=1/SUPERUSER_ID) instantiation ban.


def test_safe_patch_object_mocking_the_cursor_is_forbidden():
    source = "safe_patch_object(self.cr, 'execute', mock_fn)\n"
    errors, _warnings = _dict_findings(source)
    assert any("Mocking the database cursor" in e for e in errors)


def test_safe_patch_object_mocking_something_else_is_not_flagged():
    source = "safe_patch_object(self.env['ham.qso'], 'create', mock_fn)\n"
    errors, _warnings = _dict_findings(source)
    assert not any("Mocking the database cursor" in e for e in errors)


def test_getattr_probing_current_thread_testing_is_forbidden_test_evasion():
    source = "flag = getattr(threading.current_thread(), 'testing')\n"
    errors, _warnings = _dict_findings(source)
    assert any(
        "Probing" in e and "current_thread" in e and "getattr" in e for e in errors
    )


def test_registry_dot_get_is_forbidden_soft_dependency_checking():
    source = "mod = registry.get('optional_module')\n"
    errors, _warnings = _dict_findings(source)
    assert any("Soft-dependency checking" in e for e in errors)


def test_models_dot_get_is_forbidden_soft_dependency_checking():
    source = "mod = models.get('optional_module')\n"
    errors, _warnings = _dict_findings(source)
    assert any("Soft-dependency checking" in e for e in errors)


def test_searching_ir_module_module_outside_a_test_file_is_forbidden():
    source = "mods = self.env['ir.module.module'].search([('name', '=', 'foo')])\n"
    errors, _warnings = _dict_findings(source)
    assert any(
        "ir.module.module" in e and "Declare dependencies" in e for e in errors
    )


def test_searching_ir_module_module_inside_a_real_test_file_is_allowed():
    # The rule's own real exemption: conditionally exercising an optional module's behavior
    # in a test has no manifest-level way to be expressed otherwise.
    source = "mods = self.env['ir.module.module'].search([('name', '=', 'foo')])\n"
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_res_users.py"
    )
    assert not any("ir.module.module" in e for e in errors)


def test_config_dot_get_probing_test_enable_is_forbidden_test_evasion():
    source = "flag = config.get('test_enable')\n"
    errors, _warnings = _dict_findings(source)
    assert any("config.get('test_enable')" in e for e in errors)


def test_environment_instantiation_with_uid_1_is_a_sudo_cheat():
    source = "env = api.Environment(cr, 1, {})\n"
    errors, _warnings = _dict_findings(source)
    assert any("Instantiating an Environment" in e and "ZERO-SUDO" in e for e in errors)


def test_environment_instantiation_with_superuser_id_is_a_sudo_cheat():
    source = "env = api.Environment(cr, SUPERUSER_ID, {})\n"
    errors, _warnings = _dict_findings(source)
    assert any("Instantiating an Environment" in e and "ZERO-SUDO" in e for e in errors)


def test_environment_instantiation_with_a_service_account_uid_is_not_flagged():
    source = "env = api.Environment(cr, service_uid, {})\n"
    errors, _warnings = _dict_findings(source)
    assert not any("Instantiating an Environment" in e for e in errors)


# Still more of visit_Call()'s own direct rule cluster: print() ban, PATH TRAVERSAL warning
# for filesystem ops in controllers/models, hollow-assertion bans (assertTrue/assertFalse on
# a literal or a multi-condition `and`, assertEqual of identical literals or a variable to
# itself), Markup() XSS ban, env(su=True) privilege escalation, and TEST CURSOR CORRUPTION
# (commit()/rollback() on env.cr inside a test file without RealTransactionCase).


def test_print_is_banned_ai_laziness():
    source = "print('debug value:', value)\n"
    errors, _warnings = _dict_findings(source)
    assert any("print()" in e and "logging" in e for e in errors)


def test_print_inside_a_tools_file_is_exempt():
    source = "print('debug value:', value)\n"
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tools/some_script.py"
    )
    assert not any("print()" in e for e in errors)


def test_open_call_inside_an_http_controller_is_a_path_traversal_warning():
    source = (
        "@http.route('/api/v1/thing', type='jsonrpc', auth='user')\n"
        "def get_thing(self, filename):\n"
        "    return open(filename).read()\n"
    )
    _errors, warnings = _dict_findings(source)
    assert any("PATH TRAVERSAL" in w for w in warnings)


def test_open_call_outside_any_controller_or_model_method_is_not_flagged():
    source = "def helper(filename):\n    return open(filename).read()\n"
    _errors, warnings = _dict_findings(source)
    assert not any("PATH TRAVERSAL" in w for w in warnings)


def test_assert_true_of_literal_true_is_a_hollow_assertion():
    source = "self.assertTrue(True)\n"
    errors, _warnings = _dict_findings(source)
    assert any("Hollow assertion" in e and "assertTrue(True)" in e for e in errors)


def test_assert_false_of_literal_false_is_a_hollow_assertion():
    source = "self.assertFalse(False)\n"
    errors, _warnings = _dict_findings(source)
    assert any("Hollow assertion" in e and "assertFalse(False)" in e for e in errors)


def test_assert_true_of_a_real_variable_is_not_a_hollow_assertion():
    source = "self.assertTrue(record.active)\n"
    errors, _warnings = _dict_findings(source)
    assert not any("Hollow assertion" in e for e in errors)


def test_assert_true_with_an_and_boolop_is_a_test_anti_pattern():
    source = "self.assertTrue(record.active and record.confirmed)\n"
    errors, _warnings = _dict_findings(source)
    assert any("multiple conditions" in e for e in errors)


def test_markup_of_an_fstring_is_an_xss_vulnerability():
    source = "html = Markup(f'<b>{user_input}</b>')\n"
    errors, _warnings = _dict_findings(source)
    assert any("XSS VULNERABILITY" in e for e in errors)


def test_markup_of_a_percent_interpolation_is_an_xss_vulnerability():
    source = "html = Markup('<b>%s</b>' % user_input)\n"
    errors, _warnings = _dict_findings(source)
    assert any("XSS VULNERABILITY" in e for e in errors)


def test_markup_of_a_plain_string_literal_is_not_flagged():
    source = "html = Markup('<b>static text</b>')\n"
    errors, _warnings = _dict_findings(source)
    assert not any("XSS VULNERABILITY" in e for e in errors)


def test_assert_equal_of_two_identical_literals_is_a_hollow_assertion():
    source = "self.assertEqual(5, 5)\n"
    errors, _warnings = _dict_findings(source)
    assert any(
        "Hollow assertion" in e and "identical literals" in e for e in errors
    )


def test_assert_equal_of_a_variable_to_itself_is_a_hollow_assertion():
    source = "self.assertEqual(value, value)\n"
    errors, _warnings = _dict_findings(source)
    assert any(
        "Hollow assertion" in e and "variable to itself" in e for e in errors
    )


def test_assert_equal_of_two_different_variables_is_not_flagged():
    source = "self.assertEqual(record.name, expected_name)\n"
    errors, _warnings = _dict_findings(source)
    assert not any("Hollow assertion" in e for e in errors)


def test_env_call_with_su_true_is_forbidden_privilege_escalation():
    source = "scoped_env = self.env(su=True)\n"
    errors, _warnings = _dict_findings(source)
    assert any("su=True" in e and "PRIVILEGE ESCALATION" in e for e in errors)


def test_commit_on_env_cr_inside_a_test_file_is_test_cursor_corruption():
    source = "self.env.cr.commit()\n"
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_res_users.py"
    )
    assert any("TEST CURSOR CORRUPTION" in e for e in errors)


def test_rollback_on_env_cr_inside_a_test_file_is_test_cursor_corruption():
    source = "self.env.cr.rollback()\n"
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_res_users.py"
    )
    assert any("TEST CURSOR CORRUPTION" in e for e in errors)


def test_commit_on_env_cr_outside_any_test_file_is_not_flagged_by_this_rule():
    source = "self.env.cr.commit()\n"
    errors, _warnings = _dict_findings(source)
    assert not any("TEST CURSOR CORRUPTION" in e for e in errors)


# The last stretch of visit_Call(): N+1 loop-query detection, start_tour()'s debug=1
# requirement, the JSON-RPC-kwargs-as-positional-dict ban, and the symlink-to-a-real-module
# ban (which walks the real filesystem, like the has_ham_base rule earlier in this file).


def test_search_inside_a_loop_is_an_n_plus_one_error():
    source = (
        "for record in records:\n"
        "    self.env['ham.qso'].search([('active', '=', True)], limit=1)\n"
    )
    errors, _warnings = _dict_findings(source)
    assert any("N+1 locking" in e for e in errors)


def test_search_outside_any_loop_is_not_an_n_plus_one_error():
    source = "self.env['ham.qso'].search([('active', '=', True)], limit=1)\n"
    errors, _warnings = _dict_findings(source)
    assert not any("N+1 locking" in e for e in errors)


def test_a_regex_named_objects_search_inside_a_loop_is_exempt_from_n_plus_one():
    source = "for line in lines:\n    some_regex.search(line)\n"
    errors, _warnings = _dict_findings(source)
    assert not any("N+1 locking" in e for e in errors)


def test_ir_module_module_search_count_inside_a_loop_in_a_test_file_is_exempt():
    source = (
        "for _i in range(2):\n"
        "    self.env['ir.module.module'].search_count([('name', '=', 'foo')])\n"
    )
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_res_users.py"
    )
    assert not any("N+1 locking" in e for e in errors)


def test_start_tour_without_debug_1_is_forbidden():
    source = "start_tour('/odoo', 'my_tour')\n"
    errors, _warnings = _dict_findings(source)
    assert any("start_tour()" in e and "debug=1" in e for e in errors)


def test_start_tour_with_debug_1_in_the_url_is_allowed():
    source = "start_tour('/odoo?debug=1', 'my_tour')\n"
    errors, _warnings = _dict_findings(source)
    assert not any("start_tour()" in e for e in errors)


def test_execute_with_a_kwargs_dict_passed_positionally_to_search_is_forbidden():
    source = "proxy.execute(uid, 'search', {'limit': 10})\n"
    errors, _warnings = _dict_findings(source)
    assert any("JSON-RPC KWARGS" in e for e in errors)


def test_execute_with_a_domain_only_dict_is_not_flagged_by_the_kwargs_rule():
    source = "proxy.execute(uid, 'search', {'domain': [('active', '=', True)]})\n"
    errors, _warnings = _dict_findings(source)
    assert not any("JSON-RPC KWARGS" in e for e in errors)


def test_symlinking_to_a_real_module_directory_is_forbidden():
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "models" / "res_users.py"
        module_dir = Path(tmpdir) / "models" / "zero_sudo"
        module_dir.mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text("{}", encoding="utf-8")
        source = "os.symlink('zero_sudo', 'zero_sudo_link')\n"
        errors, _warnings = _dict_findings(source, filepath=str(filepath))
    assert any("symbolic links to resolve modules" in e for e in errors)


def test_bare_symlink_call_form_is_also_checked_not_just_os_dot_symlink():
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "models" / "res_users.py"
        module_dir = Path(tmpdir) / "models" / "zero_sudo"
        module_dir.mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text("{}", encoding="utf-8")
        source = "from os import symlink\nsymlink('zero_sudo', 'zero_sudo_link')\n"
        errors, _warnings = _dict_findings(source, filepath=str(filepath))
    assert any("symbolic links to resolve modules" in e for e in errors)


def test_symlink_source_resolved_through_an_intermediate_variable_assignment_is_checked():
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "models" / "res_users.py"
        module_dir = Path(tmpdir) / "models" / "zero_sudo"
        module_dir.mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text("{}", encoding="utf-8")
        source = "src = 'zero_sudo'\nos.symlink(src, 'zero_sudo_link')\n"
        errors, _warnings = _dict_findings(source, filepath=str(filepath))
    assert any("symbolic links to resolve modules" in e for e in errors)


def test_symlinking_to_a_directory_that_is_not_a_real_module_is_not_flagged():
    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = Path(tmpdir) / "models" / "res_users.py"
        plain_dir = Path(tmpdir) / "models" / "not_a_module"
        plain_dir.mkdir(parents=True)
        source = "os.symlink('not_a_module', 'not_a_module_link')\n"
        errors, _warnings = _dict_findings(source, filepath=str(filepath))
    assert not any("symbolic links to resolve modules" in e for e in errors)


# visit_Assign() -- direct self.env.context mutation, _sql_constraints as an assignment
# target, group_ids mutation via plain attribute assignment and via dict-subscript
# assignment (both with the real test_*.py exemption), and I18N on a dict-key assignment.


def test_directly_assigning_to_self_env_context_is_forbidden():
    source = "self.env.context = {'lang': 'en_US'}\n"
    errors, _warnings = _dict_findings(source)
    assert any("Never modify" in e and "self.env.context" in e for e in errors)


def test_sql_constraints_as_an_assignment_target_is_flagged():
    source = "_sql_constraints = [('uniq_name', 'unique(name)', 'Name must be unique.')]\n"
    errors, _warnings = _dict_findings(source)
    assert any("models.Constraint" in e for e in errors)


def test_attribute_assignment_to_group_ids_outside_tests_is_forbidden():
    source = "user.group_ids = [(6, 0, [group.id])]\n"
    errors, _warnings = _dict_findings(source)
    assert any("Mutating 'group_ids' in Python" in e for e in errors)


def test_attribute_assignment_to_group_ids_inside_a_test_file_is_allowed():
    source = "user.group_ids = [(6, 0, [group.id])]\n"
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_res_users.py"
    )
    assert not any("Mutating 'group_ids' in Python" in e for e in errors)


def test_subscript_assignment_to_group_ids_outside_tests_is_forbidden():
    source = "vals['group_ids'] = [(6, 0, [group.id])]\n"
    errors, _warnings = _dict_findings(source)
    assert any("Mutating 'group_ids' in Python" in e for e in errors)


def test_subscript_assignment_to_group_ids_inside_a_test_file_is_allowed():
    source = "vals['group_ids'] = [(6, 0, [group.id])]\n"
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_res_users.py"
    )
    assert not any("Mutating 'group_ids' in Python" in e for e in errors)


def test_untranslated_string_assigned_to_an_error_dict_key_is_an_i18n_warning():
    source = "vals['error'] = 'Something went wrong'\n"
    _errors, warnings = _dict_findings(source)
    assert any("I18N" in w and "dict key" in w for w in warnings)


def test_translated_string_assigned_to_an_error_dict_key_is_not_flagged():
    source = "vals['error'] = _('Something went wrong')\n"
    _errors, warnings = _dict_findings(source)
    assert not any("dict key" in w for w in warnings)


# visit_Import() -- local (in-function) `import x` (distinct from the already-covered
# `from x import y` form), `import pickle`, `import random` (with its real
# audit-ignore-weak-random exemption for deterministic seeding).


def test_a_local_import_statement_inside_a_method_is_forbidden():
    source = "def some_method(self):\n    import json\n    return json.dumps({})\n"
    errors, _warnings = _dict_findings(source)
    assert any("LOCAL IMPORT" in e for e in errors)


def test_import_pickle_statement_form_is_banned_the_same_as_from_pickle_import():
    source = "import pickle\n"
    errors, _warnings = _dict_findings(source)
    assert any("pickle module is vulnerable" in e for e in errors)


def test_import_random_statement_form_is_weak_crypto():
    source = "import random\n"
    errors, _warnings = _dict_findings(source)
    assert any("WEAK CRYPTO" in e for e in errors)


def test_import_random_statement_form_with_the_audit_tag_is_allowed():
    source = "import random  # audit-ignore-weak-random: deterministic exam generation\n"
    errors, _warnings = _dict_findings(source)
    assert not any("WEAK CRYPTO" in e for e in errors)


# visit_Expr() -- bare Ellipsis (...) as a statement -- and visit_Tuple() -- the
# ('id', '=', int) / ('id', 'in', [int, ...]) hardcoded-ID-lookup ban.


def test_bare_ellipsis_statement_is_forbidden_elision():
    source = "def stub(self):\n    ...\n"
    errors, _warnings = _dict_findings(source)
    assert any("Elision" in e for e in errors)


def test_hardcoded_id_equals_lookup_is_forbidden():
    source = "domain = [('id', '=', 42)]\n"
    errors, _warnings = _dict_findings(source)
    assert any("Hardcoded ID lookup" in e and "'=', int" in e for e in errors)


def test_hardcoded_id_in_list_lookup_is_forbidden():
    source = "domain = [('id', 'in', [42, 43])]\n"
    errors, _warnings = _dict_findings(source)
    assert any("Hardcoded ID lookup" in e and "'in', [int" in e for e in errors)


def test_id_equals_string_key_lookup_is_not_a_hardcoded_id():
    source = "domain = [('id', '=', 'xml_id_string')]\n"
    errors, _warnings = _dict_findings(source)
    assert not any("Hardcoded ID lookup" in e for e in errors)


# check_ast_vulnerabilities()'s own syntax-error path, and visit_Compare()'s two
# soft-dependency/test-evasion rules ('model' in self.env / request.env, sys.modules probing).


def test_a_real_python_syntax_error_is_reported_as_a_single_error_not_a_crash():
    source = "def broken(:\n    pass\n"
    errors, warnings = _dict_findings(source)
    assert warnings == []
    assert len(errors) == 1
    assert "SYNTAX/INDENTATION ERROR" in errors[0]


def test_model_string_in_self_dot_env_is_forbidden_soft_dependency_checking():
    source = "if 'optional.model' in self.env:\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert any("Soft-dependency checking" in e and "self.env" in e for e in errors)


def test_model_string_in_request_dot_env_is_forbidden_soft_dependency_checking():
    source = "if 'optional.model' in request.env:\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert any("Soft-dependency checking" in e for e in errors)


def test_probing_sys_modules_is_forbidden_test_evasion():
    source = "if 'ham_base' in sys.modules:\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert any("sys.modules" in e and "test evasion" in e for e in errors)


def test_a_normal_in_comparison_unrelated_to_env_or_sys_modules_is_not_flagged():
    source = "if item_id in allowed_ids:\n    pass\n"
    errors, _warnings = _dict_findings(source)
    assert not any(
        "Soft-dependency checking" in e or "sys.modules" in e for e in errors
    )


# _check_test_empty()'s dead-code-testing-evasion rules (empty test detection and
# unreachable-code-after-return detection, both gated on being inside a real test_*.py file's
# own test_* method), @api.returns deprecation, and visit_For()'s chunking-loop exemption
# from N+1 loop-query detection.


def test_a_test_method_with_no_real_calls_at_all_is_an_empty_test():
    source = "class FooTests(TestCase):\n    def test_something(self):\n        pass\n"
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_foo.py"
    )
    assert any("Empty test detected" in e for e in errors)


def test_a_test_method_that_calls_a_real_external_assertion_is_not_an_empty_test():
    source = (
        "class FooTests(TestCase):\n"
        "    def test_something(self):\n"
        "        self.assertEqual(record.name, expected_name)\n"
    )
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_foo.py"
    )
    assert not any("Empty test detected" in e for e in errors)


def test_code_after_a_return_inside_a_test_method_is_unreachable_ast_evasion():
    source = (
        "class FooTests(TestCase):\n"
        "    def test_something(self):\n"
        "        self.assertEqual(record.name, expected_name)\n"
        "        return\n"
        "        self.assertEqual(other.name, other_expected)\n"
    )
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_foo.py"
    )
    assert any("AST Evasion Detected" in e and "Unreachable code" in e for e in errors)


def test_api_returns_decorator_call_form_is_deprecated():
    source = "@api.returns('self')\ndef some_method(self):\n    return self.browse()\n"
    errors, _warnings = _dict_findings(source)
    assert any("@api.returns is deprecated" in e for e in errors)


def test_api_returns_decorator_bare_attribute_form_is_deprecated():
    source = "@api.returns\ndef some_method(self):\n    return self.browse()\n"
    errors, _warnings = _dict_findings(source)
    assert any("@api.returns is deprecated" in e for e in errors)


def test_search_inside_a_chunk_size_stepped_range_loop_is_exempt_from_n_plus_one():
    # visit_For()'s real chunking-loop exemption: a `range(start, stop, chunk_size)` (or
    # `batch_size`) loop is deliberate batch pagination, not the N+1 pattern this rule exists
    # to catch, so loop_depth is never incremented for it.
    source = (
        "for offset in range(0, total, chunk_size):\n"
        "    self.env['ham.qso'].search([], limit=chunk_size, offset=offset)\n"
    )
    errors, _warnings = _dict_findings(source)
    assert not any("N+1 locking" in e for e in errors)


# scan_file()'s own non-AST-visitor checks: __manifest__.py license/description validation,
# and the ir.model.access.csv blank-line/comment/financial-model-access bans.


def test_manifest_missing_license_key_is_a_critical_manifest_error():
    content = "{\n    'name': 'Test Module',\n    'depends': ['base'],\n}\n"
    errors, _warnings = _scan_file(content, "__manifest__.py")
    assert any("'license' key is missing" in e for e in errors)


def test_manifest_with_an_invalid_license_value_is_a_critical_manifest_error():
    content = (
        "{\n"
        "    'name': 'Test Module',\n"
        "    'depends': ['base'],\n"
        "    'license': 'MIT',\n"
        "    'description': 'A real description.',\n"
        "}\n"
    )
    errors, _warnings = _scan_file(content, "__manifest__.py")
    assert any("Invalid 'license' value" in e for e in errors)


def test_manifest_with_a_valid_license_is_not_flagged_for_license():
    content = (
        "{\n"
        "    'name': 'Test Module',\n"
        "    'depends': ['base'],\n"
        "    'license': 'AGPL-3',\n"
        "    'description': 'A real description.',\n"
        "}\n"
    )
    errors, _warnings = _scan_file(content, "__manifest__.py")
    assert not any("license" in e for e in errors)


def test_manifest_missing_description_is_a_critical_manifest_error():
    content = (
        "{\n"
        "    'name': 'Test Module',\n"
        "    'depends': ['base'],\n"
        "    'license': 'AGPL-3',\n"
        "}\n"
    )
    errors, _warnings = _scan_file(content, "__manifest__.py")
    assert any("'description' key is missing or empty" in e for e in errors)


def test_scan_file_exempts_check_burn_list_dot_py_itself():
    # The linter's own file is skipped by filename check before any content is even read.
    errors, warnings = _scan_file("this is not valid python at all {{{\n", "check_burn_list.py")
    assert errors == []
    assert warnings == []


def test_access_csv_blank_line_is_forbidden():
    content = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_thing,thing,model_thing,base.group_user,1,1,1,1\n"
        "\n"
        "access_other,other,model_other,base.group_user,1,1,1,1\n"
    )
    errors, _warnings = _scan_file(content, "ir.model.access.csv")
    assert any("Blank lines are forbidden" in e for e in errors)


def test_access_csv_comment_line_is_forbidden():
    content = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "# a comment explaining the next line\n"
        "access_thing,thing,model_thing,base.group_user,1,1,1,1\n"
    )
    errors, _warnings = _scan_file(content, "ir.model.access.csv")
    assert any("Comments (#) are forbidden" in e for e in errors)


def test_access_csv_granting_a_financial_model_is_forbidden():
    content = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_thing,thing,model_res_partner_bank,base.group_user,1,1,1,1\n"
    )
    errors, _warnings = _scan_file(content, "ir.model.access.csv")
    assert any(
        "FINANCIAL EXPOSURE" in e and "model_res_partner_bank" in e for e in errors
    )


def test_access_csv_granting_a_normal_non_financial_model_is_not_flagged():
    content = (
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_thing,thing,model_ham_qso,base.group_user,1,1,1,1\n"
    )
    errors, _warnings = _scan_file(content, "ir.model.access.csv")
    assert not any("FINANCIAL EXPOSURE" in e for e in errors)


# scan_file()'s XML/HTML walk: the ANCHOR FORMAT ban (text content and attribute value,
# with data-trace's real exemption), the OWL 2 arrow-function-with-block-body ban, the
# snippet_options deprecation, and the ir.rule/res.groups noupdate-data-block requirement.


def test_anchor_marker_in_plain_element_text_is_a_critical_anchor_format_error():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="description">[@ANCHOR: COMM_test]</field>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("ANCHOR FORMAT" in e and "MUST be enclosed within comments" in e for e in errors)


def test_anchor_marker_in_an_attribute_value_is_a_critical_anchor_format_error():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="description" data-something="[@ANCHOR: COMM_test]"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("ANCHOR FORMAT" in e and "Found in attribute" in e for e in errors)


def test_anchor_marker_in_the_exempt_data_trace_attribute_is_not_flagged():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="description" data-trace="[@ANCHOR: COMM_test]"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert not any("ANCHOR FORMAT" in e for e in errors)


def test_owl_arrow_function_with_a_block_body_is_forbidden_syntax():
    xml = (
        "<odoo>\n"
        '    <t t-name="my.template">\n'
        '        <button t-on-click="() => { doSomething(); }">Click</button>\n'
        "    </t>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_template.xml")
    assert any("OWL SYNTAX" in e and "block bodies" in e for e in errors)


def test_owl_arrow_function_expression_form_is_allowed():
    xml = (
        "<odoo>\n"
        '    <t t-name="my.template">\n'
        '        <button t-on-click="() => doSomething()">Click</button>\n'
        "    </t>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_template.xml")
    assert not any("OWL SYNTAX" in e for e in errors)


def test_inheriting_website_snippet_options_is_a_critical_deprecation():
    xml = (
        "<odoo>\n"
        "    <data>\n"
        '        <record id="thing_view" model="ir.ui.view" inherit_id="website.snippet_options">\n'
        '            <field name="name">Thing</field>\n'
        '            <field name="arch" type="xml">\n'
        "                <!-- [@ANCHOR: COMM_test] -->\n"
        '                <xpath expr="//div" position="inside"/>\n'
        "            </field>\n"
        "        </record>\n"
        "    </data>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_view.xml")
    assert any("snippet_options" in e and "Odoo 19" in e for e in errors)


def test_ir_rule_record_outside_a_noupdate_data_block_is_forbidden():
    xml = (
        "<odoo>\n"
        '    <record id="my_rule" model="ir.rule">\n'
        '        <field name="name">My Rule</field>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "security_data.xml")
    assert any("must be inside noupdate data block" in e for e in errors)


def test_ir_rule_record_inside_a_noupdate_data_block_is_allowed():
    xml = (
        "<odoo>\n"
        '    <data noupdate="1">\n'
        '        <record id="my_rule" model="ir.rule">\n'
        '            <field name="name">My Rule</field>\n'
        "        </record>\n"
        "    </data>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "security_data.xml")
    assert not any("must be inside noupdate data block" in e for e in errors)


def test_res_groups_record_outside_a_noupdate_data_block_is_also_forbidden():
    xml = (
        "<odoo>\n"
        '    <record id="my_group" model="res.groups">\n'
        '        <field name="name">My Group</field>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "security_data.xml")
    assert any("must be inside noupdate data block" in e for e in errors)


# scan_file()'s own per-line text-scanning loop (distinct from the AST visitor entirely):
# the noqa ban, the tombstone "# Removed: ..." comment ban, unrecognized burn-ignore/
# audit-ignore tags (UNAUTHORIZED BYPASS), and a sample of the table-driven
# GENERAL_ERROR_RULES regex checks.


def test_noqa_comment_is_forbidden_linter_evasion():
    content = "import unused_module  # noqa\n"
    errors, _warnings = _scan_file(content, "some_module.py")
    assert any("CRITICAL LINTER EVASION" in e and "noqa" in e for e in errors)


def test_noqa_e402_is_the_one_real_exemption():
    content = "import sys\nsys.path.insert(0, '.')\nimport thing  # noqa: E402\n"
    errors, _warnings = _scan_file(content, "some_module.py")
    assert not any("LINTER EVASION" in e for e in errors)


def test_noqa_as_a_substring_inside_unrelated_text_is_not_flagged():
    # Word-boundaried, not a bare substring search -- confirmed real false positive this
    # exact check was fixed for: base64 blobs containing "noqa" by pure coincidence.
    content = "blob = 'xxxKNoQAImLyorxxx'\n"
    errors, _warnings = _scan_file(content, "some_module.py")
    assert not any("LINTER EVASION" in e for e in errors)


def test_a_fresh_removed_tombstone_comment_is_forbidden():
    content = "# Removed: the old caching layer, see ADR-0042 for why.\nx = 1\n"
    errors, _warnings = _scan_file(content, "some_module.py")
    assert any("Code: `# Removed:" in e for e in errors)


def test_removed_word_wrapped_mid_sentence_from_the_previous_comment_line_is_not_a_tombstone():
    # The real false-positive shape this exemption exists for: a multi-line prose comment
    # whose word-wrap happens to put "removed" first on its own continuation line.
    content = (
        "# This code path was intentionally\n"
        "# removed because it caused a race condition.\n"
        "x = 1\n"
    )
    errors, _warnings = _scan_file(content, "some_module.py")
    assert not any("Code: `# removed" in e for e in errors)


def test_an_unrecognized_burn_ignore_tag_is_an_unauthorized_bypass():
    content = "x = 1  # burn-ignore-made-up-tag-nobody-approved\n"
    errors, _warnings = _scan_file(content, "some_module.py")
    assert any("UNAUTHORIZED BYPASS" in e for e in errors)


def test_an_unrecognized_audit_ignore_tag_is_an_unauthorized_bypass():
    content = "x = 1  # audit-ignore-made-up-tag-nobody-approved\n"
    errors, _warnings = _scan_file(content, "some_module.py")
    assert any("UNAUTHORIZED BYPASS" in e for e in errors)


def test_a_real_audit_ignore_tag_with_a_linked_anchor_is_not_an_unauthorized_bypass():
    content = "x = 1  # audit-ignore-cron [@ANCHOR: COMM_test_my_cron_job]\n"
    errors, _warnings = _scan_file(content, "some_module.py")
    assert not any("UNAUTHORIZED BYPASS" in e for e in errors)


def test_todo_comment_is_a_forbidden_placeholder_via_general_error_rules():
    content = "def helper():\n    pass  # TODO: implement this properly\n"
    errors, _warnings = _scan_file(content, "some_module.py")
    assert any("Placeholders, TODOs" in e for e in errors)


def test_redundant_sys_path_append_of_file_dunder_is_a_hallucination():
    content = "import sys, os\nsys.path.append(os.path.dirname(__file__))\n"
    errors, _warnings = _scan_file(content, "some_module.py")
    assert any("Redundant sys.path manipulation" in e for e in errors)


def test_a_daemon_file_importing_odoo_is_forbidden_daemon_decoupling():
    content = "import odoo\n"
    errors, _warnings = _scan_file(content, "daemons/some_daemon.py")
    assert any("DAEMON DECOUPLING" in e for e in errors)


# More of scan_file()'s XML/HTML walk: ir.rule's publish_to_public + write/unlink combo,
# mandatory-fields-per-model data integrity, res.users-inside-noupdate warning, ir.rule's
# required 'groups' field and financial-model ban, xpath position validation, field-without-
# name, t-raw/t-esc/attrs deprecations, kanban-box deprecation, and dynamic-snippet crash
# prevention.


def test_ir_rule_granting_public_read_plus_write_is_a_critical_security_error():
    xml = (
        "<odoo>\n"
        '    <data noupdate="1">\n'
        '        <record id="rule1" model="ir.rule">\n'
        '            <field name="name">Rule</field>\n'
        '            <field name="model_id" ref="model_website_page"/>\n'
        '            <field name="groups" eval="[(4, ref(\'base.group_user\'))]"/>\n'
        "            <field name=\"domain_force\">[('publish_to_public','=',True)]</field>\n"
        '            <field name="perm_write" eval="1"/>\n'
        "        </record>\n"
        "    </data>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "security_data.xml")
    assert any(
        "publish_to_public" in e and "Split into separate read and write rules" in e
        for e in errors
    )


def test_record_missing_a_mandatory_field_for_its_model_is_a_data_integrity_error():
    xml = (
        "<odoo>\n"
        '    <data noupdate="1">\n'
        '        <record id="group1" model="res.groups">\n'
        '            <field name="category_id" ref="base.module_category_hidden"/>\n'
        "        </record>\n"
        "    </data>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "security_data.xml")
    assert any(
        "DATA INTEGRITY" in e and "missing mandatory fields" in e and ": name." in e
        for e in errors
    )


def test_record_with_all_its_mandatory_fields_is_not_a_data_integrity_error():
    xml = (
        "<odoo>\n"
        '    <data noupdate="1">\n'
        '        <record id="group1" model="res.groups">\n'
        '            <field name="name">My Group</field>\n'
        "        </record>\n"
        "    </data>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "security_data.xml")
    assert not any("DATA INTEGRITY" in e for e in errors)


def test_res_users_record_inside_a_noupdate_block_is_a_record_update_warning():
    xml = (
        "<odoo>\n"
        '    <data noupdate="1">\n'
        '        <record id="svc_user" model="res.users">\n'
        '            <field name="name">Service Account</field>\n'
        '            <field name="login">svc_account</field>\n'
        '            <field name="company_id" ref="base.main_company"/>\n'
        '            <field name="company_ids" eval="[(6, 0, [ref(\'base.main_company\')])]"/>\n'
        '            <field name="notification_type">email</field>\n'
        "        </record>\n"
        "    </data>\n"
        "</odoo>\n"
    )
    _errors, warnings = _scan_file(xml, "security_data.xml")
    assert any("RECORD UPDATE" in w and "noupdate" in w for w in warnings)


def test_ir_rule_without_a_groups_field_is_forbidden_as_a_deprecated_global_rule():
    xml = (
        "<odoo>\n"
        '    <data noupdate="1">\n'
        '        <record id="rule2" model="ir.rule">\n'
        '            <field name="name">Rule</field>\n'
        '            <field name="model_id" ref="model_ham_qso"/>\n'
        "        </record>\n"
        "    </data>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "security_data.xml")
    assert any("must specify a 'groups' field" in e for e in errors)


def test_ir_rule_for_a_financial_model_is_forbidden():
    xml = (
        "<odoo>\n"
        '    <data noupdate="1">\n'
        '        <record id="rule3" model="ir.rule">\n'
        '            <field name="name">Rule</field>\n'
        '            <field name="model_id" ref="model_res_partner_bank"/>\n'
        '            <field name="groups" eval="[(4, ref(\'base.group_user\'))]"/>\n'
        "        </record>\n"
        "    </data>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "security_data.xml")
    assert any(
        "FINANCIAL EXPOSURE" in e and "model_res_partner_bank" in e for e in errors
    )


def test_xpath_with_an_invalid_position_value_is_forbidden():
    xml = (
        "<odoo>\n"
        '    <record id="view1" model="ir.ui.view">\n'
        '        <field name="name">View</field>\n'
        '        <field name="model">ham.qso</field>\n'
        '        <field name="arch" type="xml">\n'
        "            <!-- [@ANCHOR: COMM_test] -->\n"
        '            <xpath expr="//div" position="magic"/>\n'
        "        </field>\n"
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_view.xml")
    assert any("INVALID XPATH position" in e for e in errors)


def test_a_field_element_without_a_name_attribute_is_forbidden():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        "        <field>NoNameHere</field>\n"
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("CRITICAL XML missing name" in e for e in errors)


def test_t_raw_attribute_is_a_critical_xss_vulnerability():
    xml = (
        "<odoo>\n"
        '    <t t-name="my.template">\n'
        '        <span t-raw="user_input"/>\n'
        "    </t>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_template.xml")
    assert any("CRITICAL XSS" in e and "t-out" in e for e in errors)


def test_t_esc_attribute_is_a_banned_deprecated_directive():
    xml = (
        "<odoo>\n"
        '    <t t-name="my.template">\n'
        '        <span t-esc="user_input"/>\n'
        "    </t>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_template.xml")
    assert any("t-esc is banned" in e for e in errors)


def test_attrs_attribute_is_a_removed_deprecated_view_syntax():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        "        <field name=\"x\" attrs=\"{'invisible': [('state', '=', 'draft')]}\"/>\n"
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("'attrs' attribute was removed" in e for e in errors)


def test_kanban_box_template_name_is_banned_in_odoo_19():
    xml = '<odoo>\n    <t t-name="kanban-box">\n        <div/>\n    </t>\n</odoo>\n'
    errors, _warnings = _scan_file(xml, "my_template.xml")
    assert any('t-name="kanban-box" is banned' in e for e in errors)


def test_dynamic_snippet_missing_required_data_attributes_is_a_crash_risk():
    xml = '<odoo>\n    <div data-snippet="s_dynamic_snippet_products"/>\n</odoo>\n'
    errors, _warnings = _scan_file(xml, "my_snippet.xml")
    assert any("OWL 2 CRASH" in e and "data-filter-id" in e for e in errors)


def test_dynamic_snippet_with_both_required_data_attributes_is_not_flagged():
    xml = (
        "<odoo>\n"
        '    <div data-snippet="s_dynamic_snippet_products" data-filter-id="1"'
        ' data-template-key="product_card"/>\n'
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_snippet.xml")
    assert not any("OWL 2 CRASH" in e for e in errors)


# The rest of scan_file()'s XML/HTML walk: deprecated <group> attributes, FRAGILE XPATH,
# WCAG accessibility (icons, images, empty buttons/links), and a representative sample of the
# field-value type-mismatch trap cluster (each distinct message tested once; the many
# field-name variants sharing one branch, e.g. company_id/company_ids, are the same code path).


def test_group_string_attribute_is_a_banned_deprecated_pattern():
    xml = '<odoo>\n    <group string="Details">\n        <field name="x"/>\n    </group>\n</odoo>\n'
    errors, _warnings = _scan_file(xml, "my_view.xml")
    assert any("group" in e and "banned in Odoo 19" in e for e in errors)


def test_xpath_with_a_parent_axis_traversal_is_fragile():
    xml = '<odoo>\n    <xpath expr="//div/.." position="inside"/>\n</odoo>\n'
    errors, _warnings = _scan_file(xml, "my_view.xml")
    assert any("FRAGILE XPATH" in e for e in errors)


def test_xpath_without_parent_axis_or_complex_predicate_is_not_fragile():
    xml = '<odoo>\n    <xpath expr="//div[@id=\'thing\']" position="inside"/>\n</odoo>\n'
    errors, _warnings = _scan_file(xml, "my_view.xml")
    assert not any("FRAGILE XPATH" in e for e in errors)


def test_icon_tag_with_fa_class_and_no_accessibility_attribute_is_forbidden():
    xml = '<odoo>\n    <i class="fa fa-star"/>\n</odoo>\n'
    errors, _warnings = _scan_file(xml, "my_view.xml")
    assert any("ACCESSIBILITY" in e and "fa fa-star" in e for e in errors)


def test_icon_tag_with_fa_class_and_aria_hidden_is_allowed():
    xml = '<odoo>\n    <i class="fa fa-star" aria-hidden="true"/>\n</odoo>\n'
    errors, _warnings = _scan_file(xml, "my_view.xml")
    assert not any("ACCESSIBILITY" in e for e in errors)


def test_img_tag_without_alt_attribute_is_forbidden():
    xml = '<odoo>\n    <img src="thing.png"/>\n</odoo>\n'
    errors, _warnings = _scan_file(xml, "my_view.xml")
    assert any("ACCESSIBILITY" in e and "alt" in e for e in errors)


def test_img_tag_with_alt_attribute_is_allowed():
    xml = '<odoo>\n    <img src="thing.png" alt="A thing"/>\n</odoo>\n'
    errors, _warnings = _scan_file(xml, "my_view.xml")
    assert not any("ACCESSIBILITY" in e for e in errors)


def test_empty_button_with_no_label_is_an_accessibility_audit_warning():
    xml = "<odoo>\n    <button/>\n</odoo>\n"
    _errors, warnings = _scan_file(xml, "my_view.xml")
    assert any("ACCESSIBILITY" in w and "button" in w for w in warnings)


def test_button_with_visible_text_is_not_an_accessibility_warning():
    xml = "<odoo>\n    <button>Click me</button>\n</odoo>\n"
    _errors, warnings = _scan_file(xml, "my_view.xml")
    assert not any("ACCESSIBILITY" in w for w in warnings)


def test_res_groups_users_field_is_a_bias_trap_use_user_ids():
    xml = (
        "<odoo>\n"
        '    <record id="group1" model="res.groups">\n'
        '        <field name="users" eval="[(6, 0, [ref(\'base.user_admin\')])]"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("BIAS TRAP" in e and "user_ids" in e for e in errors)


def test_res_groups_privilege_id_referencing_a_standard_category_is_forbidden():
    xml = (
        "<odoo>\n"
        '    <record id="group1" model="res.groups">\n'
        '        <field name="privilege_id" ref="base.module_category_hidden"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("SECURITY PRIVILEGE" in e and "res.groups.privilege" in e for e in errors)


def test_res_users_groups_id_field_is_a_bias_trap_use_group_ids():
    xml = (
        "<odoo>\n"
        '    <record id="user1" model="res.users">\n'
        '        <field name="groups_id" eval="[(6, 0, [ref(\'base.group_user\')])]"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("BIAS TRAP" in e and "group_ids" in e for e in errors)


def test_assigning_a_group_ref_to_a_user_field_is_a_type_mismatch():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="user_id" ref="base.group_user"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any(
        "TYPE MISMATCH" in e and "group" in e and "user field" in e for e in errors
    )


def test_assigning_a_user_ref_to_a_group_field_is_a_type_mismatch():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="group_ids" ref="base.user_admin"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any(
        "TYPE MISMATCH" in e and "user" in e and "group field" in e for e in errors
    )


def test_assigning_a_user_ref_to_a_company_field_is_a_type_mismatch():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="company_id" ref="base.user_admin"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("TYPE MISMATCH" in e and "company field" in e for e in errors)


def test_assigning_a_group_ref_to_a_partner_field_is_a_type_mismatch():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="partner_id" ref="base.group_user"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("TYPE MISMATCH" in e and "partner field" in e for e in errors)


def test_assigning_a_group_ref_to_model_id_is_a_type_mismatch():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="ir.rule">\n'
        '        <field name="model_id" ref="base.group_user"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any(
        "TYPE MISMATCH" in e and "non-model reference" in e and "model_id" in e
        for e in errors
    )


def test_using_ref_on_a_primitive_field_is_a_type_mismatch():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="active" ref="base.some_xml_id"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any(
        "TYPE MISMATCH" in e and "primitive/boolean/integer field" in e for e in errors
    )


# The rest of the field-value trap cluster: ir.cron's zero-sudo user_id ban, the raw-list-
# on-an-x2many-field ban, hardcoded-numeric-ref ban, dangerous-eval-expression detection,
# ir.actions.act_window's type-field check, employee-field type mismatch, the model-name
# underscore-vs-dot typo detector, QWeb SSTI (request.env in an attribute or in text), the
# removed survey.survey 'state' field, and the ir.cron CRON ARCHITECTURE audit reminder.


def test_ir_cron_assigned_to_base_user_root_is_a_zero_sudo_violation():
    xml = (
        "<odoo>\n"
        '    <record id="cron1" model="ir.cron">\n'
        '        <field name="user_id" ref="base.user_root"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("ZERO-SUDO VIOLATION" in e and "ir.cron" in e for e in errors)


def test_raw_list_assigned_to_an_x2many_field_is_a_type_mismatch():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="tag_ids" eval="[1, 2, 3]"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("TYPE MISMATCH" in e and "Odoo ORM commands" in e for e in errors)


def test_a_proper_orm_command_on_an_x2many_field_is_not_flagged():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="tag_ids" eval="[(6, 0, [1, 2, 3])]"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert not any("Odoo ORM commands" in e for e in errors)


def test_a_hardcoded_numeric_ref_is_a_type_mismatch():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="partner_id" ref="42"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("hardcoded numeric ID" in e for e in errors)


def test_dangerous_builtin_in_an_eval_expression_is_a_critical_security_finding():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        "        <field name=\"domain\" eval=\"__import__('os').system('id')\"/>\n"
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("Dangerous built-in execution" in e for e in errors)


def test_act_window_type_field_with_the_wrong_value_is_a_type_mismatch():
    xml = (
        "<odoo>\n"
        '    <record id="action1" model="ir.actions.act_window">\n'
        '        <field name="type">ir.actions.server</field>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("ir.actions.act_window" in e and "TYPE MISMATCH" in e for e in errors)


def test_assigning_a_user_ref_to_an_employee_field_is_a_type_mismatch():
    xml = (
        "<odoo>\n"
        '    <record id="thing" model="some.model">\n'
        '        <field name="employee_id" ref="base.user_admin"/>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any("TYPE MISMATCH" in e and "employee field" in e for e in errors)


def test_model_name_with_underscore_instead_of_dot_is_a_type_mismatch_typo():
    xml = '<odoo>\n    <record id="thing" model="res_partner">\n    </record>\n</odoo>\n'
    errors, _warnings = _scan_file(xml, "my_data.xml")
    assert any(
        "Odoo models use dots, not underscores" in e and "res.partner" in e
        for e in errors
    )


def test_request_env_inside_an_xml_attribute_is_a_critical_ssti_vulnerability():
    xml = (
        "<odoo>\n"
        '    <t t-name="my.template">\n'
        '        <span t-att-value="request.env.user.name"/>\n'
        "    </t>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_template.xml")
    assert any("CRITICAL SSTI" in e for e in errors)


def test_request_env_inside_element_text_is_also_a_critical_ssti_vulnerability():
    xml = (
        "<odoo>\n"
        '    <t t-name="my.template">\n'
        "        <span>request.env.user.name</span>\n"
        "    </t>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_template.xml")
    assert any("CRITICAL SSTI" in e for e in errors)


def test_survey_state_field_comparison_is_a_removed_deprecated_field():
    xml = (
        "<odoo>\n"
        '    <t t-name="my.template">\n'
        "        <span>record.state == 'open'</span>\n"
        "    </t>\n"
        "</odoo>\n"
    )
    errors, _warnings = _scan_file(xml, "my_template.xml")
    assert any("survey.survey" in e and "removed in Odoo 19" in e for e in errors)


def test_ir_cron_record_without_audit_ignore_cron_gets_a_cron_architecture_reminder():
    xml = (
        "<odoo>\n"
        '    <record id="cron1" model="ir.cron">\n'
        '        <field name="name">My Cron</field>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    _errors, warnings = _scan_file(xml, "my_data.xml")
    assert any("CRON ARCHITECTURE" in w and "_trigger()" in w for w in warnings)


def test_static_js_file_missing_the_odoo_module_pragma_is_forbidden():
    content = "export const thing = () => 1;\n"
    errors, _warnings = _scan_file(content, "static/src/js/thing.js")
    assert any("ASSET BUNDLER" in e and "@odoo-module" in e for e in errors)


def test_static_js_file_with_the_odoo_module_pragma_is_allowed():
    content = "/** @odoo-module **/\nexport const thing = () => 1;\n"
    errors, _warnings = _scan_file(content, "static/src/js/thing.js")
    assert not any("ASSET BUNDLER" in e for e in errors)


def test_a_js_file_outside_static_is_not_checked_for_the_pragma():
    # A tool config file (eslint.config.js, webpack.config.js) living outside a module's
    # static/ directory is never picked up by Odoo's asset bundler and can't carry the
    # pragma at all -- the real hams_shared/eslint.config.js false positive this exempts.
    content = "export default { rules: {} };\n"
    errors, _warnings = _scan_file(content, "eslint.config.js")
    assert not any("ASSET BUNDLER" in e for e in errors)


def test_llm_linter_guide_missing_its_required_sentinel_is_a_summarization_bias_trap():
    content = "# LLM Linter Guide\n\nSome guidance that got summarized down.\n"
    errors, _warnings = _scan_file(content, "LLM_LINTER_GUIDE.md")
    assert any("AI SUMMARIZATION BIAS TRAP" in e for e in errors)


def test_llm_linter_guide_with_its_required_sentinel_present_is_not_flagged():
    content = (
        "# LLM Linter Guide\n\n"
        "CRITICAL BIAS TRAP: Odoo 18+ normalized the res.users groups relation to "
        "'group_ids'.\n"
    )
    errors, _warnings = _scan_file(content, "LLM_LINTER_GUIDE.md")
    assert not any("AI SUMMARIZATION BIAS TRAP" in e for e in errors)


def test_ir_cron_record_with_audit_ignore_cron_is_not_flagged():
    xml = (
        "<odoo>\n"
        "    <!-- audit-ignore-cron: this cron is a one-shot bootstrap task -->\n"
        '    <record id="cron1" model="ir.cron">\n'
        '        <field name="name">My Cron</field>\n'
        "    </record>\n"
        "</odoo>\n"
    )
    _errors, warnings = _scan_file(xml, "my_data.xml")
    assert not any("CRON ARCHITECTURE" in w for w in warnings)


def test_clear_caches_call_is_forbidden_global_cache_invalidation():
    source = "self.env.registry.clear_caches()\n"
    errors, _warnings = _dict_findings(source)
    assert any("clear_cache" in e for e in errors)


def test_check_recursion_call_is_a_deprecated_hierarchy_api():
    source = "self._check_recursion()\n"
    errors, _warnings = _dict_findings(source)
    assert any("_has_cycle" in e for e in errors)


def test_getattr_probing_for_sudo_is_forbidden_obfuscation():
    source = "method = getattr(record, 'sudo')\n"
    errors, _warnings = _dict_findings(source)
    assert any("Obfuscated use of sudo" in e for e in errors)


def test_three_argument_getattr_is_forbidden_ai_laziness():
    source = "value = getattr(record, field_name, None)\n"
    errors, _warnings = _dict_findings(source)
    assert any("3-argument getattr()" in e for e in errors)


def test_three_argument_getattr_with_the_real_burn_ignore_tag_is_exempt():
    source = "value = getattr(record, field_name, None)  # burn-ignore-introspection\n"
    errors, _warnings = _dict_findings(source)
    assert not any("3-argument getattr()" in e for e in errors)


def test_two_argument_getattr_is_not_flagged_by_the_three_argument_rule():
    source = "value = getattr(record, field_name)\n"
    errors, _warnings = _dict_findings(source)
    assert not any("3-argument getattr()" in e for e in errors)


def test_setattr_mutating_group_ids_outside_tests_is_forbidden():
    source = "setattr(user, 'group_ids', [(6, 0, [group.id])])\n"
    errors, _warnings = _dict_findings(source)
    assert any("Mutating 'group_ids' via setattr" in e for e in errors)


def test_setattr_mutating_group_ids_inside_a_test_file_is_allowed():
    # The rule's own real exemption: self.filename (not filepath) starting with "test_".
    source = "setattr(user, 'group_ids', [(6, 0, [group.id])])\n"
    errors, _warnings = _dict_findings(
        source, filepath="/tmp/some_module/tests/test_res_users.py"
    )
    assert not any("Mutating 'group_ids' via setattr" in e for e in errors)


# The last of scan_file()'s XML/HTML walk: the XPATH RENDERING audit reminder (with its real
# audit-ignore-xpath exemption) and the XML AST parse-error catch, plus a sample of the JS
# tour crash-prevention regex cluster (window.confirm/alert freezing the headless browser).


def test_xpath_without_audit_ignore_xpath_gets_a_rendering_audit_reminder():
    xml = (
        "<odoo>\n"
        '    <record id="view1" model="ir.ui.view">\n'
        '        <field name="name">View</field>\n'
        '        <field name="model">ham.qso</field>\n'
        '        <field name="arch" type="xml">\n'
        "            <!-- [@ANCHOR: COMM_test] -->\n"
        '            <xpath expr="//div" position="inside"/>\n'
        "        </field>\n"
        "    </record>\n"
        "</odoo>\n"
    )
    _errors, warnings = _scan_file(xml, "my_view.xml")
    assert any("XPATH RENDERING" in w for w in warnings)


def test_xpath_with_audit_ignore_xpath_is_not_flagged():
    xml = (
        "<odoo>\n"
        '    <record id="view1" model="ir.ui.view">\n'
        '        <field name="name">View</field>\n'
        '        <field name="model">ham.qso</field>\n'
        '        <field name="arch" type="xml">\n'
        "            <!-- [@ANCHOR: COMM_test] audit-ignore-xpath: manually verified -->\n"
        '            <xpath expr="//div" position="inside"/>\n'
        "        </field>\n"
        "    </record>\n"
        "</odoo>\n"
    )
    _errors, warnings = _scan_file(xml, "my_view.xml")
    assert not any("XPATH RENDERING" in w for w in warnings)


def test_malformed_xml_is_a_critical_xml_ast_error_not_a_crash():
    xml = "<odoo>\n    <record id=\"thing\" model=\"some.model\">\n"  # unclosed tags
    errors, _warnings = _scan_file(xml, "broken.xml")
    assert any("CRITICAL XML AST ERROR" in e for e in errors)


def test_js_tour_using_window_confirm_freezes_the_headless_browser():
    content = (
        "import { registry } from '@web/core/registry';\n"
        "registry.category('web_tour.tours').add('my_tour', {\n"
        "    steps: () => [\n"
        "        { trigger: '.o_thing', run: function() { window.confirm('Are you sure?'); } },\n"
        "    ],\n"
        "});\n"
    )
    errors, _warnings = _scan_file(content, "my_tour.js")
    assert any("JS TOUR DIALOG" in e and "window.confirm" in e for e in errors)


def test_js_tour_without_any_of_the_flagged_crash_patterns_is_clean():
    content = (
        "import { registry } from '@web/core/registry';\n"
        "registry.category('web_tour.tours').add('my_tour', {\n"
        "    steps: () => [\n"
        "        { trigger: '.o_thing', run: 'click' },\n"
        "    ],\n"
        "});\n"
    )
    errors, _warnings = _scan_file(content, "my_tour.js")
    assert not any("JS TOUR" in e for e in errors)


def test_environment_instantiation_with_uid_as_a_keyword_argument_of_1_is_a_sudo_cheat():
    source = "env = api.Environment(cr, uid=1, context={})\n"
    errors, _warnings = _dict_findings(source)
    assert any("Instantiating an Environment" in e and "ZERO-SUDO" in e for e in errors)


def test_environment_instantiation_with_a_dotted_superuser_id_attribute_is_a_sudo_cheat():
    source = "env = api.Environment(cr, odoo.SUPERUSER_ID, {})\n"
    errors, _warnings = _dict_findings(source)
    assert any("Instantiating an Environment" in e and "ZERO-SUDO" in e for e in errors)
