#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for claim_normalizers.py: the v2 claim hash depends only on the code's token stream.

For every language: a formatting-only change and a comment-only change keep the hash; a code
change (a string literal, an operator, a number, a renamed identifier) changes it; and the
adversarial inputs (comment-looking text in strings, nested comments, raw strings, regex
literals) are tokenized correctly.
"""

import os
import tempfile
import unittest

os.environ.setdefault("HAMS_CLAIM_CACHE_DIR", tempfile.mkdtemp())

import check_claims_freshness as ccf  # noqa: E402
import claim_normalizers as cn  # noqa: E402

SUFFIX = {"python": "py", "rust": "rs", "javascript": "js", "xml": "xml", "shell": "sh", "sql": "sql"}


def anchor_hash(language, text, name="t:x"):
    return ccf.anchor_hashes_from_text(text, f"mod.{SUFFIX[language]}")[name].v2


class LanguageCase:
    """Subclasses define BASE (with the anchor `t:x`), FORMATTED (same code, other layout),
    COMMENTED (same code, other comments) and CHANGES (each differs from BASE in code)."""

    language = None
    BASE = FORMATTED = COMMENTED = ""
    CHANGES = ()

    def h(self, text):
        return anchor_hash(self.language, text)

    def test_formatting_only_change_keeps_the_hash(self):
        self.assertEqual(self.h(self.BASE), self.h(self.FORMATTED))

    def test_comment_only_change_keeps_the_hash(self):
        self.assertEqual(self.h(self.BASE), self.h(self.COMMENTED))

    def test_every_code_change_changes_the_hash(self):
        for label, text in self.CHANGES:
            with self.subTest(label):
                self.assertNotEqual(text, self.BASE, "the test's own change did not apply")
                self.assertNotEqual(self.h(self.BASE), self.h(text))

    def test_the_hash_is_a_sha256_of_the_normalized_item(self):
        self.assertRegex(self.h(self.BASE), r"^[0-9a-f]{64}$")


class PythonTests(LanguageCase, unittest.TestCase):
    language = "python"
    BASE = '''
# [@ANCHOR: t:x]
def foo(a, b=1, *, c="x"):
    """Doc."""
    total = a + b  # add
    if total > 3:
        return {"k": [1, 2, 3], "s": 'it\\'s'}
    return foo_helper(a, b, c)
'''
    FORMATTED = '''
# [@ANCHOR: t:x]
def foo(
    a,
    b = 1,
    *,
    c = 'x',
):
    total=a+b
    if (total>3):
        return {
            'k': [
                1,
                2,
                3,
            ],
            "s": "it's",
        }


    return foo_helper(
        a,
        b,
        c,
    )
'''
    COMMENTED = '''
# [@ANCHOR: t:x]
# another comment above
def foo(a, b=1, *, c="x"):
    """A completely different docstring,
    over two lines."""
    # a comment line
    total = a + b
    if total > 3:  # trailing
        return {"k": [1, 2, 3], "s": 'it\\'s'}  # "#" inside a comment
    return foo_helper(a, b, c)
'''
    CHANGES = (
        ("string literal", BASE.replace('c="x"', 'c="y"')),
        ("operator", BASE.replace("a + b", "a - b")),
        ("number", BASE.replace("> 3", "> 4")),
        ("renamed identifier", BASE.replace("total", "tot")),
        ("added statement", BASE.replace("    total", "    print(a)\n    total")),
        ("indentation structure", BASE.replace("    return foo_helper", "        return foo_helper")),
        ("string with a hash", BASE.replace('"k"', '"k#"')),
    )

    def test_a_one_element_tuple_is_not_a_parenthesized_value(self):
        a = "# [@ANCHOR: t:x]\ndef f():\n    return (1,)\n"
        b = "# [@ANCHOR: t:x]\ndef f():\n    return (1)\n"
        self.assertNotEqual(self.h(a), self.h(b))

    def test_a_trailing_comma_in_a_call_does_not_matter_but_in_a_subscript_tuple_does(self):
        a = "# [@ANCHOR: t:x]\ndef f(x):\n    return g(x,)\n"
        b = "# [@ANCHOR: t:x]\ndef f(x):\n    return g(x)\n"
        self.assertEqual(self.h(a), self.h(b))
        c = "# [@ANCHOR: t:x]\ndef f(x):\n    return x[1,]\n"
        d = "# [@ANCHOR: t:x]\ndef f(x):\n    return x[1]\n"
        self.assertNotEqual(self.h(c), self.h(d))

    def test_black_wrapping_a_long_right_hand_side_in_parentheses_does_not_matter(self):
        wrapped = "# [@ANCHOR: t:x]\ndef f(a, b):\n    x = (\n        a + b\n    )\n    return x\n"
        plain = "# [@ANCHOR: t:x]\ndef f(a, b):\n    x = a + b\n    return x\n"
        self.assertEqual(self.h(wrapped), self.h(plain))

    def test_a_tuple_in_parentheses_is_not_unwrapped_into_something_else(self):
        tup = "# [@ANCHOR: t:x]\ndef f(a, b):\n    x = (a, b)\n    return x\n"
        other = "# [@ANCHOR: t:x]\ndef f(a, b):\n    x = (a + b)\n    return x\n"
        self.assertNotEqual(self.h(tup), self.h(other))

    def test_the_parentheses_black_adds_at_a_narrow_width_do_not_matter(self):
        plain = (
            "# [@ANCHOR: t:x]\ndef f(a, b, c, d):\n"
            "    for x, y in c:\n        pass\n"
            "    for x in a, b:\n        pass\n"
            "    t, u = g(a)\n"
            "    try:\n        v = h(k=a if b else c)\n    except E as e:\n        raise\n"
            "    assert a == b, 'msg'\n"
            "    w = [x if x else d for x in c]\n"
            "    return t, u\n"
        )
        wrapped = (
            "# [@ANCHOR: t:x]\ndef f(a, b, c, d):\n"
            "    for (x, y) in c:\n        pass\n"
            "    for x in (a, b):\n        pass\n"
            "    (t, u) = g(a)\n"
            "    try:\n        v = h(k=(a if b else c))\n    except (E) as e:\n        raise\n"
            "    assert (a == b), 'msg'\n"
            "    w = [(x if x else d) for x in c]\n"
            "    return (t, u)\n"
        )
        self.assertEqual(self.h(plain), self.h(wrapped))

    def test_parentheses_that_change_meaning_are_kept(self):
        for a, b in (
            ("x = (a + b) * c", "x = a + b * c"),
            ("assert (a, b)", "assert a, b"),
            ("x = a in (b, c)", "x = a in b"),
            ("x = (a if b else c).y", "x = a if b else c.y"),
            ("x = f((i for i in a))", "x = f(i for i in a, b)"),
        ):
            with self.subTest(a):
                try:
                    hb = self.h(f"# [@ANCHOR: t:x]\ndef f(a, b, c):\n    {b}\n")
                except Exception:
                    continue  # the second form is not valid Python
                self.assertNotEqual(self.h(f"# [@ANCHOR: t:x]\ndef f(a, b, c):\n    {a}\n"), hb)

    def test_string_values_not_spellings_are_compared(self):
        a = "# [@ANCHOR: t:x]\ndef f():\n    return 'a\"b', b'x', r'\\d'\n"
        b = "# [@ANCHOR: t:x]\ndef f():\n    return \"a\\\"b\", b\"x\", '\\\\d'\n"
        self.assertEqual(self.h(a), self.h(b))
        c = "# [@ANCHOR: t:x]\ndef f():\n    return 'a\"b', b'y', r'\\d'\n"
        self.assertNotEqual(self.h(a), self.h(c))

    def test_f_strings_are_compared_by_content(self):
        a = "# [@ANCHOR: t:x]\ndef f(n):\n    return f'x{n!r:>{n}} {n + 1}'\n"
        b = "# [@ANCHOR: t:x]\ndef f(n):\n    return f\"x{n!r:>{n}} {n + 1}\"\n"
        c = "# [@ANCHOR: t:x]\ndef f(n):\n    return f'x{n!r:>{n}} {n + 2}'\n"
        self.assertEqual(self.h(a), self.h(b))
        self.assertNotEqual(self.h(a), self.h(c))

    def test_a_docstring_looking_string_later_in_the_body_is_code(self):
        a = "# [@ANCHOR: t:x]\ndef f():\n    x = 1\n    'not a docstring'\n    return x\n"
        b = a.replace("not a docstring", "different")
        self.assertNotEqual(self.h(a), self.h(b))

    def test_hash_comment_text_inside_a_string_is_kept(self):
        a = "# [@ANCHOR: t:x]\ndef f():\n    return 'a # b'\n"
        self.assertNotEqual(self.h(a), self.h(a.replace("a # b", "a ")))

    def test_semicolon_equals_line_break_and_line_continuation_is_ignored(self):
        a = "# [@ANCHOR: t:x]\ndef f():\n    a = 1; b = 2\n    return a + \\\n        b\n"
        b = "# [@ANCHOR: t:x]\ndef f():\n    a = 1\n    b = 2\n    return a + b\n"
        self.assertEqual(self.h(a), self.h(b))

    def test_a_method_in_a_class_gets_a_hash(self):
        a = "class K:\n    # [@ANCHOR: t:x]\n    def m(self):\n        if self:\n            return 1\n"
        self.assertRegex(self.h(a), r"^[0-9a-f]{64}$")

    def test_module_and_class_docstrings_are_dropped_but_later_strings_are_code(self):
        text = cn.normalize_text('"""Module."""\nclass K:\n    """Class."""\n    x = "kept"\n', "python")
        self.assertNotIn("Module", text)
        self.assertNotIn("Class", text)
        self.assertIn('"kept"', text)

    def test_a_slice_starting_after_a_dedent_keeps_its_own_indentation_baseline(self):
        src = "def a():\n    return 1\n\n\ndef b():\n    if x:\n        return 2\n"
        text = cn.PythonTokens(src).normalize(5, 7)
        self.assertEqual(text, "def b ( ) :\n    if x :\n        return 2")

    def test_normalized_output_is_readable_and_keeps_depth(self):
        text = cn.normalize_text("def f(a):\n    if a:\n        return 'x'\n    return 2\n", "python")
        self.assertEqual(text, 'def f ( a ) :\n    if a :\n        return "x"\n    return 2')


class RustTests(LanguageCase, unittest.TestCase):
    language = "rust"
    BASE = '''
// [@ANCHOR: t:x]
#[inline]
pub fn foo<'a>(x: &'a str, c: char) -> Vec<Vec<u8>> {
    let raw = r#"a "// not a comment" b"#;
    let q = '\\'';
    let t: (u8,) = (1,);
    match x {
        "a" => { call(x, c) }
        _ => other(x),
    }
    helper(x, c)
}
'''
    FORMATTED = '''
// [@ANCHOR: t:x]
#[inline]
pub fn foo<'a>(
    x: &'a str,
    c: char,
) -> Vec< Vec<u8> >
{
    let raw=r#"a "// not a comment" b"#;
    let q='\\'';
    let t : (u8 ,) = ( 1 , );
    match x {
        "a" => {
            call(
                x,
                c,
            )
        },
        _ => other(x,),
    }
    helper(
        x,
        c,
    )
}
'''
    COMMENTED = '''
// [@ANCHOR: t:x]
/// Doc comment with a /* in it.
//! inner doc
#[inline]
/* block /* nested */ still a comment */
pub fn foo<'a>(x: &'a str, c: char) -> Vec<Vec<u8>> {
    // line comment with "quote
    let raw = r#"a "// not a comment" b"#; /* trailing */
    let q = '\\'';
    let t: (u8,) = (1,);
    match x {
        "a" => { call(x, c) } // arm
        _ => other(x),
    }
    /** doc */ helper(x, c)
}
'''
    CHANGES = (
        ("string literal", BASE.replace('"a" =>', '"b" =>')),
        ("raw string", BASE.replace("not a comment", "other")),
        ("type", BASE.replace("&'a str", "&'a mut str")),
        ("renamed identifier", BASE.replace("helper", "helper2")),
        ("char literal", BASE.replace("'\\''", "'x'")),
        ("one-element tuple comma", BASE.replace("(1,)", "(1)")),
        ("lifetime", BASE.replace("<'a>(x: &'a str", "<'b>(x: &'b str")),
        ("visibility", BASE.replace("pub fn", "fn")),
        ("operator", BASE.replace("helper(x, c)", "helper(x, c) + 1")),
    )

    def test_rustfmt_braces_around_a_single_expression_arm_or_closure_do_not_matter(self):
        a = "// [@ANCHOR: t:x]\nfn f(x: u8) -> u8 {\n    let g = |t| t + 1;\n    match x {\n        1 => g(x),\n        2 => return 3,\n        _ => other(x),\n    }\n}\n"
        b = (
            "// [@ANCHOR: t:x]\nfn f(x: u8) -> u8 {\n    let g = |t| { t + 1 };\n    match x {\n"
            "        1 => {\n            g(x)\n        }\n        2 => {\n            return 3;\n        }\n"
            "        _ => other(x),\n    }\n}\n"
        )
        self.assertEqual(self.h(a), self.h(b))
        c = b.replace("g(x)", "g(x); h()")
        self.assertNotEqual(self.h(a), self.h(c))

    def test_a_nested_block_comment_hides_code(self):
        a = "// [@ANCHOR: t:x]\nfn f() { /* a /* b */ x(); */ y() }\n"
        b = "// [@ANCHOR: t:x]\nfn f() { y() }\n"
        self.assertEqual(self.h(a), self.h(b))

    def test_comment_looking_text_in_strings_is_code(self):
        a = '// [@ANCHOR: t:x]\nfn f() { let s = "// x /* y"; g(s) }\n'
        self.assertNotEqual(self.h(a), self.h(a.replace("// x /* y", "")))
        b = '// [@ANCHOR: t:x]\nfn f() { let s = b"/* x"; g(s) }\n'
        self.assertNotEqual(self.h(b), self.h(b.replace("/* x", "/* z")))

    def test_a_raw_string_with_hashes_and_quotes_does_not_end_early(self):
        src = '// [@ANCHOR: t:x]\nfn f() { let s = r##"a"# still "## ; g(s) }\nfn after() {}\n'
        text = cn.anchored_texts(src, "rust")["t:x"]
        self.assertIn('r##"a"# still "##', text)
        self.assertNotIn("after", text)

    def test_a_lifetime_is_not_a_char_literal_and_a_char_is_not_a_lifetime(self):
        toks = [(t.kind, t.text) for t in cn.lex_rust("fn f<'a>(x: &'a u8) -> char { 'a' }") if t.kind != "comment"]
        self.assertIn(("life", "'a"), toks)
        self.assertIn(("char", "'a'"), toks)

    def test_byte_and_c_strings_and_raw_identifiers(self):
        toks = [t.kind for t in cn.lex_rust('x(b"a", br#"b"#, c"c", b\'d\', r#type)')]
        self.assertEqual(toks.count("str"), 3)
        self.assertEqual(toks.count("char"), 1)

    def test_the_item_ends_at_its_closing_brace(self):
        src = "// [@ANCHOR: t:x]\nfn a() { 1 }\nfn bb() { 2 }\n"
        self.assertNotIn("bb", cn.anchored_texts(src, "rust")["t:x"])

    def test_a_const_item_ends_at_its_semicolon(self):
        src = "// [@ANCHOR: t:x]\nconst A: S = S { a: 1 };\nfn b() {}\n"
        self.assertNotIn("fn", cn.anchored_texts(src, "rust")["t:x"])

    def test_a_test_reference_is_not_a_declaration(self):
        src = "// Tests [@ANCHOR: t:x]\nfn t() {}\n"
        self.assertEqual(cn.anchored_texts(src, "rust"), {})


class JavaScriptTests(LanguageCase, unittest.TestCase):
    language = "javascript"
    BASE = '''
// [@ANCHOR: t:x]
async function foo(a, b) {
  const re = /ab+c[/]d/g;
  const s = 'it\\'s // not a comment';
  const half = a / 2 / b;
  if (a) /x/.test(b);
  return `v${a + 1}w${{k: b}.k}`.replace(re, (m) => m + s) + half;
}
'''
    FORMATTED = '''
// [@ANCHOR: t:x]
async function foo(
  a,
  b,
) {
    const re = /ab+c[/]d/g;
    const s = "it's // not a comment";
    const half = a/2/b;
    if (a) /x/.test(b);
    return `v${a + 1}w${{ k: b }.k}`.replace(re, m => m + s) + half;
}
'''
    COMMENTED = '''
// [@ANCHOR: t:x]
/** Doc block with a } and a ` in it. */
async function foo(a, b) { // trailing
  const re = /ab+c[/]d/g; /* c */
  // const re = 'removed'
  const s = 'it\\'s // not a comment';
  const half = a / 2 / b;
  if (a) /x/.test(b);
  return `v${a + 1}w${{k: b}.k}`.replace(re, (m) => m + s) + half; // end
}
'''
    CHANGES = (
        ("string literal", BASE.replace("not a comment", "other")),
        ("operator", BASE.replace("a / 2", "a * 2")),
        ("number", BASE.replace("a / 2", "a / 3")),
        ("renamed identifier", BASE.replace("half", "part")),
        ("regex literal", BASE.replace("ab+c", "ab*c")),
        ("regex flag", BASE.replace("/g;", "/gi;")),
        ("template text", BASE.replace("v${a", "u${a")),
        ("async", BASE.replace("async ", "")),
    )

    def test_division_and_regex_are_told_apart(self):
        toks = [
            (t.kind, t.text)
            for t in cn.lex_js("a = b / c / d; x = /e/.test(y); f(g) / 2; if (h) /i/.test(j)")
            if t.kind != "comment"
        ]
        self.assertEqual([t for k, t in toks if k == "regex"], ["/e/", "/i/"])
        self.assertEqual(sum(1 for k, t in toks if t == "/"), 3)

    def test_regex_after_a_block_and_division_after_an_object_literal(self):
        kinds = [t.kind for t in cn.lex_js("function f() {}\n/re/.test(x)")]
        self.assertIn("regex", kinds)
        kinds = [t.kind for t in cn.lex_js("x = {a: 1} / 2")]
        self.assertNotIn("regex", kinds)

    def test_a_slash_in_a_character_class_and_in_a_string_does_not_end_things(self):
        src = "// [@ANCHOR: t:x]\nfunction f() { return /[/*]x/.test('/*') }\n"
        self.assertIn("/[/*]x/", cn.anchored_texts(src, "javascript")["t:x"])

    def test_comment_looking_text_in_template_and_string_is_code(self):
        a = "// [@ANCHOR: t:x]\nfunction f() { return `// ${x} /* y` }\n"
        self.assertNotEqual(self.h(a), self.h(a.replace("/* y", "")))

    def test_nested_templates_and_braces_in_templates(self):
        src = "// [@ANCHOR: t:x]\nconst f = (a) => `a${`b${a}c`}d${ {x: 1}.x }`;\nconst gg = 1;\n"
        self.assertNotIn("gg", cn.anchored_texts(src, "javascript")["t:x"])

    def test_item_forms_end_where_they_should(self):
        src = "class K {\n  // [@ANCHOR: t:x]\n  foo(a) { return a; }\n  bar() { return 2; }\n}\n"
        self.assertNotIn("bar", cn.anchored_texts(src, "javascript")["t:x"])
        src = "const o = {\n  // [@ANCHOR: t:x]\n  foo: (a) => a + 1,\n  bar: 2,\n};\n"
        self.assertNotIn("bar", cn.anchored_texts(src, "javascript")["t:x"])
        src = "// [@ANCHOR: t:x]\nconst f = (a) => a + 1\nconst gg = 2\n"
        self.assertNotIn("gg", cn.anchored_texts(src, "javascript")["t:x"])
        src = "// [@ANCHOR: t:x]\nconst o = {a: 1,\n b: 2}\nconst pp = 3;\n"
        self.assertNotIn("pp", cn.anchored_texts(src, "javascript")["t:x"])

    def test_an_if_else_statement_is_one_item(self):
        src = "// [@ANCHOR: t:x]\nfunction f() { if (a) { b() } else { c() } }\nfunction gg() {}\n"
        self.assertNotIn("gg", cn.anchored_texts(src, "javascript")["t:x"])


class XmlTests(LanguageCase, unittest.TestCase):
    language = "xml"
    BASE = '''<odoo>
  <!-- [@ANCHOR: t:x] -->
  <record id="a" model="ir.rule">
    <field name="domain_force">[('x', '=', 1)]</field>
    <field name="name">Rule</field>
  </record>
  <record id="b" model="ir.rule"/>
</odoo>'''
    FORMATTED = '''<odoo><!-- [@ANCHOR: t:x] --><record model="ir.rule"   id="a"><field
        name="domain_force">
   [('x', '=', 1)]
 </field><field name='name'>Rule</field></record>
<record id="b" model="ir.rule"/></odoo>'''
    COMMENTED = '''<odoo>
  <!-- [@ANCHOR: t:x] -->
  <record id="a" model="ir.rule">
    <!-- why this domain -->
    <field name="domain_force">[('x', '=', 1)]</field>
    <field name="name">Rule</field><?pi x?>
  </record>
  <record id="b" model="ir.rule"/>
</odoo>'''
    CHANGES = (
        ("attribute value", BASE.replace('id="a"', 'id="c"')),
        ("text", BASE.replace("'=', 1", "'=', 2")),
        ("tag", BASE.replace('<field name="name">Rule</field>', '<value name="name">Rule</value>')),
        ("attribute name", BASE.replace('model="ir.rule">', 'models="ir.rule">')),
        (
            "child order",
            BASE.replace(
                "<field name=\"domain_force\">[('x', '=', 1)]</field>\n    <field name=\"name\">Rule</field>",
                "<field name=\"name\">Rule</field>\n    <field name=\"domain_force\">[('x', '=', 1)]</field>",
            ),
        ),
    )

    def test_the_anchor_binds_to_the_next_element_only(self):
        self.assertNotIn('id="b"', cn.anchored_texts(self.BASE, "xml")["t:x"])

    def test_unknown_html_entities_do_not_break_parsing(self):
        src = "<t><!-- [@ANCHOR: t:x] --><div>a&nbsp;b</div></t>"
        self.assertIn("&amp;nbsp;", cn.anchored_texts(src, "xml")["t:x"])

    def test_comment_looking_text_in_an_attribute_is_kept(self):
        src = '<t><!-- [@ANCHOR: t:x] --><a href="&lt;!-- x --&gt;"/></t>'
        self.assertIn("&lt;!-- x --&gt;", cn.anchored_texts(src, "xml")["t:x"])

    def test_malformed_xml_is_an_error_not_a_hash(self):
        with self.assertRaises(cn.NormalizeError):
            cn.anchored_texts("<a><b></a>", "xml")


class ShellTests(LanguageCase, unittest.TestCase):
    language = "shell"
    BASE = '''
# [@ANCHOR: t:x]
foo() {
  local a="$1" b='x # y'
  if [ -n "$a" ] && [ "${#a}" -gt 3 ]; then
    echo "$(date +%s) # not a comment" >&2
  fi
  cat <<EOF
line # kept
EOF
  return 0
}
bar() { :; }
'''
    FORMATTED = '''
# [@ANCHOR: t:x]
foo()
{
  local a="$1" \\
        b='x # y'

  if [ -n "$a" ] &&
     [ "${#a}" -gt 3 ]
  then
    echo "$(date +%s) # not a comment" >&2
  fi;
  cat <<EOF
line # kept
EOF
  return 0;
}
bar() { :; }
'''
    COMMENTED = '''
# [@ANCHOR: t:x]
# a comment above
foo() { # trailing
  local a="$1" b='x # y'   # comment
  # echo removed
  if [ -n "$a" ] && [ "${#a}" -gt 3 ]; then
    echo "$(date +%s) # not a comment" >&2
  fi
  cat <<EOF
line # kept
EOF
  return 0
}
bar() { :; }
'''
    CHANGES = (
        ("string literal", BASE.replace("not a comment", "other")),
        ("operator", BASE.replace("-gt 3", "-lt 3")),
        ("number", BASE.replace("-gt 3", "-gt 4")),
        ("renamed variable", BASE.replace("local a=", "local c=")),
        ("heredoc body", BASE.replace("line # kept", "line # changed")),
        ("single-quoted text", BASE.replace("x # y", "x # z")),
    )

    def test_a_hash_inside_a_word_is_not_a_comment(self):
        src = "# [@ANCHOR: t:x]\nf() { echo ${#a} $# a#b; }\n"
        text = cn.anchored_texts(src, "shell")["t:x"]
        self.assertIn("${#a}", text)
        self.assertIn("a#b", text)

    def test_the_item_ends_at_the_closing_brace(self):
        self.assertNotIn("bar", cn.anchored_texts(self.BASE, "shell")["t:x"])


class SqlTests(LanguageCase, unittest.TestCase):
    language = "sql"
    BASE = '''
-- [@ANCHOR: t:x]
CREATE OR REPLACE FUNCTION get_uid(name text) RETURNS int AS $$
    SELECT id FROM res_users WHERE login = 'a--b' AND note = 'it''s /* x */';
$$ LANGUAGE sql STABLE;
SELECT 2;
'''
    FORMATTED = '''
-- [@ANCHOR: t:x]
create or replace function get_uid (
    name text
)
returns int
as $$ select id
        from res_users
        where login='a--b' and note='it''s /* x */' ; $$
language sql stable;
SELECT 2;
'''
    COMMENTED = '''
-- [@ANCHOR: t:x]
/* block /* nested */ comment */
CREATE OR REPLACE FUNCTION get_uid(name text) RETURNS int AS $$
    -- the body comment
    SELECT id FROM res_users WHERE login = 'a--b' AND note = 'it''s /* x */'; -- trailing
$$ LANGUAGE sql STABLE; -- end
SELECT 2;
'''
    CHANGES = (
        ("string literal", BASE.replace("'a--b'", "'a-b'")),
        ("escaped quote literal", BASE.replace("it''s", "its")),
        ("operator", BASE.replace("login =", "login <>")),
        ("renamed identifier", BASE.replace("res_users", "res_user")),
        ("quoted identifier", BASE.replace("FROM res_users", 'FROM "res_users"')),
        ("volatility", BASE.replace(" STABLE", " VOLATILE")),
    )

    def test_a_dollar_quoted_body_is_not_cut_at_its_inner_semicolon(self):
        self.assertIn("login", cn.anchored_texts(self.BASE, "sql")["t:x"])

    def test_the_item_ends_at_its_semicolon(self):
        self.assertNotIn("select 2", cn.anchored_texts(self.BASE, "sql")["t:x"])


class DispatchTests(unittest.TestCase):
    def test_language_by_suffix(self):
        self.assertEqual(cn.language_for_path("a/b.rs"), "rust")
        self.assertEqual(cn.language_for_path("a/b.mjs"), "javascript")
        self.assertEqual(cn.language_for_path("a/b.xml"), "xml")
        self.assertIsNone(cn.language_for_path("a/b.txt"))

    def test_unterminated_input_is_an_error_not_a_hash(self):
        for language, text in (
            ("rust", "fn f() { /* x"),
            ("javascript", "a = 'x"),
            ("sql", "select 'x"),
            ("shell", "echo 'x"),
        ):
            with self.subTest(language):
                with self.assertRaises(cn.NormalizeError):
                    cn.normalize_text(text, language)

    def test_normalize_named_finds_a_function_without_an_anchor(self):
        self.assertIn("return 1", cn.normalize_named("def foo():\n    # c\n    return 1\n", "python", "foo"))
        rust = "pub fn foo(a: u8) -> u8 { a + 1 }\nfn bar() {}\n"
        self.assertIn("a + 1", cn.normalize_named(rust, "rust", "foo"))
        self.assertNotIn("bar", cn.normalize_named(rust, "rust", "foo"))
        js = "const a = 1;\nfunction foo(x) { return x * 2 }\n"
        self.assertIn("x * 2", cn.normalize_named(js, "javascript", "foo"))
        self.assertIsNone(cn.normalize_named("fn a() {}", "rust", "missing"))


if __name__ == "__main__":
    unittest.main()

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: claim_normalizers:claim_normalizers_python_tokens_class]
# Tests [@ANCHOR: claim_normalizers:claim_normalizers__xnode_class]
# Tests [@ANCHOR: claim_normalizers:claim_normalizers_tok_class]
# Tests [@ANCHOR: claim_normalizers:claim_normalizers_normalize_error_class]
