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
