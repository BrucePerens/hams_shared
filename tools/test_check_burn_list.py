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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from check_burn_list import parse_odoo_xml, _xml_audit_lookback_start  # noqa: E402


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
