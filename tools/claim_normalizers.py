#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Per-language normalizers behind the bug-hunt claim hash, algorithm "v2" (2026-11-09).

A claim's `code_hash` must change when the anchored code's behaviour may have changed and must NOT
change when only comments or formatting did. v2 therefore hashes the code's TOKEN STREAM, never
its text: comments, blank lines, indentation style, line wrapping, spacing and the formatters'
own cosmetic rewrites (quote style, trailing commas, redundant parentheses, `a=1` vs `a = 1`) do not
reach the hash, while every code token does (identifiers, operators, numbers, and the value of
every string literal, because a string can carry behaviour: a SQL fragment, a header name).

Languages and how each is tokenized (pure Python, no network, no external toolchain, so the hash
is identical on the dev box and on every remote lint host; `tokenize` differences between Python
versions are removed, see `_python_tokens`):

  python      `tokenize`; COMMENT/NL dropped, docstrings dropped (the first statement of a def or
              class body when it is a plain string), INDENT/DEDENT kept as relative depth, string
              literals compared by value (single, double and triple quoted forms of one string are one token), f-strings by
              their source with the quote style normalized, trailing commas dropped (except a
              one-element tuple), redundant parentheses around a whole right-hand side or
              condition dropped, `;` equal to a line break.
  rust        a lexer: nested block comments, doc comments (comments), raw / byte / C strings,
              char literal vs lifetime, raw identifiers, number suffixes. Trailing commas and the
              comma after a block-bodied match arm (rustfmt adds and removes them) are dropped.
  javascript  a lexer: regex literal vs division (decided from the previous token, with a stack
              that knows block braces from object braces and `if (...)` heads), template literals
              with nested `${}`, strings compared with the quote style normalized, trailing commas
              dropped, `(x) =>` equal to `x =>`, a terminating `;` or `,` of the anchored item
              dropped.
  xml         a canonical tree: comments and processing instructions dropped, attribute order
              sorted, whitespace in text collapsed, whitespace-only text dropped (QWeb is XML).
  shell       a POSIX-shell lexer: `#` is a comment only at the start of a word, quotes, `$(...)`,
              `${...}`, heredocs kept verbatim, `;` equal to a line break, line continuation and
              blank lines dropped.
  sql         a lexer: `--` and nested `/* */` comments, `'..'` with doubled quotes, `E'..'`,
              `$tag$..$tag$` (body normalized as SQL again), `".."`; unquoted words lower-cased.

Known limits (each makes the hash MORE sensitive, never less): Python one-line compound statements
(`if x: y`) are not unfolded; Rust `use` reordering and import grouping are not normalized;
JavaScript object-property quoting is kept as written; shell and SQL
dollar-quote/heredoc bodies keep their inner whitespace for shell and are re-lexed for SQL.

Anchored items: for every language but Python and XML the anchored item is "the first code token
after the base anchor comment up to the end of that item" (see `_rust_item_end`, `_js_item_end`,
`_shell_item_end`, `_sql_item_end`); for XML it is the element that follows the anchor comment.
Python anchors keep using the AST function span, see check_claims_freshness.py.

Public API: `normalize_text`, `normalized_hash`, `anchored_texts`, `anchored_hashes`,
`normalize_named`, `language_for_path`, `PythonTokens`.
"""

import ast
import hashlib
import io
import keyword
import os
import re
import tokenize
import warnings
import xml.parsers.expat

import verify_anchors as va  # noqa: E402
from check_function_test_anchors import _is_base_anchor_declaration  # noqa: E402

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".rs": "rust",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".xml": "xml",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
}
LANGUAGES = ("python", "rust", "javascript", "xml", "shell", "sql")


class NormalizeError(ValueError):
    """The text cannot be tokenized (unterminated string or comment, malformed XML)."""


def language_for_path(path):
    return LANGUAGE_BY_SUFFIX.get(os.path.splitext(path)[1].lower())


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Tok:
    """One lexical token. `kind` is comment, ident, num, str, char, life, regex, punct, tpl*,
    word, nl, heredoc. `nl` is true when a line break separates it from the previous token."""

    __slots__ = ("kind", "text", "start", "end", "nl")

    def __init__(self, kind, text, start, end, nl=False):
        self.kind = kind
        self.text = text
        self.start = start
        self.end = end
        self.nl = nl

    def __repr__(self):
        return f"Tok({self.kind},{self.text!r})"

    def is_p(self, *texts):
        return self.kind == "punct" and self.text in texts


_IDENT = re.compile(r"[^\W\d]\w*")


# ---------------------------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------------------------

_RUST_RAW = re.compile(r'(?:br|cr|r)(#*)"')
_RUST_NUM = re.compile(
    r"0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+"
    r"|\d[\d_]*(?:\.\d[\d_]*|\.(?![.\w]))?(?:[eE][+-]?[\d_]+)?"
)
# `<` and `>` are always single tokens, so `Vec<Vec<u8>>` and `Vec<Vec<u8> >` are one stream.
_RUST_PUNCT2 = (
    "..=", "...", "::", "->", "=>", "==", "!=", "&&", "||", "+=", "-=", "*=", "/=", "%=",
    "^=", "&=", "|=", "..",
)


def lex_rust(text):
    toks = []
    n = len(text)
    i = 0
    nl = False

    def add(kind, j):
        nonlocal i, nl
        toks.append(Tok(kind, text[i:j], i, j, nl))
        nl = False
        i = j

    while i < n:
        c = text[i]
        if c.isspace():
            if c == "\n":
                nl = True
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            add("comment", n if j < 0 else j)
            continue
        if text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise NormalizeError("unterminated block comment")
            if "\n" in text[i:j]:
                nl = True
            add("comment", j)
            continue
        m = _RUST_RAW.match(text, i)
        if m:
            close = '"' + m.group(1)
            j = text.find(close, m.end())
            if j < 0:
                raise NormalizeError("unterminated raw string")
            add("str", j + len(close))
            continue
        if text.startswith(('b"', 'c"'), i) or c == '"':
            j = i + (1 if c == '"' else 2)
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise NormalizeError("unterminated string")
            add("str", j + 1)
            continue
        if text.startswith("b'", i) or c == "'":
            q = i + (2 if c == "b" else 1)
            if text[q : q + 1] == "\\":
                j = text.find("'", q + 2)
                if j < 0:
                    raise NormalizeError("unterminated char literal")
                add("char", j + 1)
            elif text[q + 1 : q + 2] == "'" and q + 1 < n:
                add("char", q + 2)
            else:
                m2 = _IDENT.match(text, q)
                if c == "b" or not m2:
                    raise NormalizeError("bad char literal")
                add("life", m2.end())
            continue
        if c == "r" and text.startswith("r#", i) and _IDENT.match(text, i + 2):
            add("ident", _IDENT.match(text, i + 2).end())
            continue
        m = _IDENT.match(text, i)
        if m:
            add("ident", m.end())
            continue
        if c.isdigit():
            m = _RUST_NUM.match(text, i)
            j = m.end()
            m2 = _IDENT.match(text, j)
            if m2:
                j = m2.end()
            add("num", j)
            continue
        for op in _RUST_PUNCT2:
            if text.startswith(op, i):
                add("punct", i + len(op))
                break
        else:
            add("punct", i + 1)
    return toks


_RUST_NOT_CALL = {
    "if", "while", "match", "return", "in", "let", "for", "else", "as", "mut", "ref", "box", "move",
    "break", "continue", "loop", "fn", "where", "impl", "dyn", "yield", "await", "unsafe", "const",
    "static", "pub", "use", "mod", "type", "struct", "enum", "trait", "async", "crate", "super",
}


def _rust_item_end(sig, i):
    """Index (exclusive) of the end of the item starting at `sig[i]`: after the `;` or the
    closing brace of the first brace group, or before an unmatched closer."""
    depth = 0
    head = None
    semi_item = False
    j = i
    while j < len(sig):
        t = sig[j]
        if t.kind == "punct":
            if t.text in "([{":
                depth += 1
            elif t.text in ")]}":
                depth -= 1
                if depth < 0:
                    return j
                if depth == 0 and t.text == "}" and not semi_item:
                    return j + 1
            elif t.text == ";" and depth == 0:
                return j + 1
        elif t.kind == "ident" and depth == 0 and head is None and t.text != "pub":
            head = t.text
            nxt = sig[j + 1].text if j + 1 < len(sig) else ""
            semi_item = head in ("static", "let", "type", "use") or (
                head == "const" and nxt not in ("fn", "unsafe", "async", "extern")
            )
        j += 1
    return len(sig)


# ---------------------------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------------------------

_JS_IDENT = re.compile(r"(?:[^\W\d]|\$)(?:\w|\$)*")
_JS_NUM = re.compile(
    r"0[xX][\da-fA-F_]+n?|0[oO][0-7_]+n?|0[bB][01_]+n?"
    r"|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?n?"
)
_JS_PUNCT = (
    ">>>=", "...", "===", "!==", "**=", "<<=", ">>=", ">>>", "&&=", "||=", "??=", "=>", "==",
    "!=", "<=", ">=", "&&", "||", "??", "?.", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=",
    "|=", "^=", "<<", ">>", "**",
)
_JS_REGEX_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do",
    "else", "yield", "await",
}
_JS_EXPR_BRACE_AFTER = {
    "(", ",", "=", ":", "[", "?", "||", "&&", "??", "!", "+", "-", "*", "%", "<", ">", "==",
    "===", "!=", "!==", "+=", "-=", "...", "|", "&", "^", "~", "||=", "&&=", "??=", "?.", "<=", ">=",
}
_JS_EXPR_BRACE_KEYWORDS = {
    "return", "typeof", "void", "delete", "throw", "new", "yield", "await", "in", "of",
    "instanceof", "case", "default",
}
_JS_CONTROL_HEADS = {"if", "while", "for", "with", "switch", "catch"}


def lex_js(text):
    toks = []
    sig = []  # significant (non-comment) tokens
    n = len(text)
    i = 0
    nl = False
    brace_stack = []  # "block", "expr" or "tpl"
    paren_stack = []  # True when the paren is an if/while/for head
    state = {"close_ctrl": False, "close_block": False}

    def add(kind, j):
        nonlocal i, nl
        tok = Tok(kind, text[i:j], i, j, nl)
        toks.append(tok)
        if kind != "comment":
            sig.append(tok)
        nl = False
        i = j
        return tok

    def regex_allowed():
        if not sig:
            return True
        p = sig[-1]
        if p.kind in ("num", "str", "tpl", "tpl_tail", "regex"):
            return False
        if p.kind == "ident":
            if p.text in _JS_REGEX_KEYWORDS:
                return not (len(sig) > 1 and sig[-2].is_p(".", "?."))
            return False
        if p.text == ")":
            return state["close_ctrl"]
        if p.text == "}":
            return state["close_block"]
        if p.text == "]" or p.text in ("++", "--"):
            return False
        return True

    def scan_template(j):
        """From just after a backtick or a closing `}`; returns (end, ended_with_backtick)."""
        while j < n:
            ch = text[j]
            if ch == "\\":
                j += 2
            elif ch == "`":
                return j + 1, True
            elif text.startswith("${", j):
                return j + 2, False
            else:
                j += 1
        raise NormalizeError("unterminated template literal")

    if text.startswith("#!"):
        j = text.find("\n")
        add("comment", n if j < 0 else j)
    while i < n:
        c = text[i]
        if c.isspace() or c in "﻿  ":
            if c in "\n  ":
                nl = True
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            add("comment", n if j < 0 else j)
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise NormalizeError("unterminated block comment")
            if "\n" in text[i:j]:
                nl = True
            add("comment", j + 2)
            continue
        if c in "'\"":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise NormalizeError("unterminated string")
            add("str", j + 1)
            continue
        if c == "`":
            j, closed = scan_template(i + 1)
            if closed:
                add("tpl", j)
            else:
                brace_stack.append("tpl")
                add("tpl_head", j)
            continue
        if c == "}" and brace_stack and brace_stack[-1] == "tpl":
            brace_stack.pop()
            j, closed = scan_template(i + 1)
            if closed:
                add("tpl_tail", j)
            else:
                brace_stack.append("tpl")
                add("tpl_mid", j)
            continue
        if c.isdigit() or (c == "." and text[i + 1 : i + 2].isdigit()):
            add("num", _JS_NUM.match(text, i).end())
            continue
        m = _JS_IDENT.match(text, i)
        if m is None and c == "#":
            m = _JS_IDENT.match(text, i + 1)
            if m:
                add("ident", m.end())
                continue
        if m:
            add("ident", m.end())
            continue
        if c == "/" and regex_allowed():
            j = i + 1
            in_class = False
            ok = False
            while j < n and text[j] != "\n":
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    ok = True
                    break
                j += 1
            if ok:
                j += 1
                while j < n and (text[j].isalnum() or text[j] == "_"):
                    j += 1
                add("regex", j)
                continue
        end = i + 1
        for op in _JS_PUNCT:
            if text.startswith(op, i):
                if op == "?." and text[i + 2 : i + 3].isdigit():
                    continue
                end = i + len(op)
                break
        prev = sig[-1] if sig else None
        tok = add("punct", end)
        t = tok.text
        if t == "(":
            ctrl = prev is not None and prev.kind == "ident" and (
                prev.text in _JS_CONTROL_HEADS
                or (prev.text == "await" and len(sig) > 2 and sig[-3].text == "for")
            )
            paren_stack.append(ctrl)
        elif t == ")":
            state["close_ctrl"] = paren_stack.pop() if paren_stack else False
        elif t == "{":
            is_expr = prev is not None and (
                (prev.kind == "punct" and prev.text in _JS_EXPR_BRACE_AFTER)
                or (prev.kind == "ident" and prev.text in _JS_EXPR_BRACE_KEYWORDS)
                or prev.kind in ("tpl_head", "tpl_mid")
            )
            brace_stack.append("expr" if is_expr else "block")
        elif t == "}":
            kind = brace_stack.pop() if brace_stack else "block"
            state["close_block"] = kind == "block"
    return toks


_JS_CONT_START = {
    "(", "[", "+", "-", "*", "/", "%", ".", "?.", ",", "?", ":", "=", "=>", "==", "===", "!=", "!==",
    "<", ">", "<=", ">=", "&&", "||", "??", "&", "|", "^", "<<", ">>", ">>>", "**", "+=", "-=",
    "*=", "/=", "%=", "&&=", "||=", "??=", "&=", "|=", "^=", "**=", "<<=", ">>=", ">>>=", "...",
    ")", "]", "}",
}
_JS_CONT_WORDS = {"in", "instanceof", "of", "else", "catch", "finally", "extends"}
_JS_EXPR_HEAD_PREV = {"=", ":", ",", "(", "[", "?", "||", "&&", "??", "!", "return"}


def _js_item_end(sig, i):
    """Exclusive end of the statement, declaration, method or property starting at `sig[i]`; the
    terminating `;` or `,` is not part of it."""
    depth = 0
    first = sig[i].text
    comma_continues = first in ("const", "let", "var")
    j = i
    while j < len(sig):
        t = sig[j]
        if j > i:
            p = sig[j - 1]
            if depth == 0 and t.nl:
                p_ends = (
                    p.kind in ("num", "str", "tpl", "tpl_tail", "regex")
                    or (p.kind == "ident" and p.text not in _JS_CONT_WORDS)
                    or p.is_p(")", "]", "}", "++", "--")
                )
                t_cont = (
                    t.text in _JS_CONT_START and t.kind == "punct"
                    or t.kind in ("tpl", "tpl_head")
                    or (t.kind == "ident" and t.text in _JS_CONT_WORDS)
                    or (t.is_p("{") and p.is_p(")"))
                )
                if p_ends and not t_cont:
                    return j
        if t.kind in ("punct", "tpl_head", "tpl_mid", "tpl_tail"):
            if t.kind == "tpl_head" or t.is_p("(", "[", "{"):
                depth += 1
            elif t.kind == "tpl_tail" or t.is_p(")", "]", "}"):
                depth -= 1
                if depth < 0:
                    return j
                if depth == 0 and t.text == "}":
                    opener = _matching_open(sig, j)
                    prev = sig[opener - 1] if opener > i else None
                    is_expr = prev is not None and (
                        (prev.kind == "punct" and prev.text in _JS_EXPR_HEAD_PREV)
                        or (prev.kind == "ident" and prev.text in ("return", "typeof"))
                    )
                    nxt = sig[j + 1] if j + 1 < len(sig) else None
                    continues = nxt is not None and nxt.kind == "ident" and nxt.text in ("else", "catch", "finally")
                    if not is_expr and not continues:
                        return j + 1
            elif t.kind == "punct" and depth == 0 and t.text in (";", ",") and not (
                t.text == "," and comma_continues
            ):
                return j
        j += 1
    return len(sig)


def _matching_open(sig, close_index):
    depth = 0
    k = close_index
    while k >= 0:
        t = sig[k]
        if t.kind == "tpl_tail" or t.is_p(")", "]", "}"):
            depth += 1
        elif t.kind == "tpl_head" or t.is_p("(", "[", "{"):
            depth -= 1
            if depth == 0:
                return k
        k -= 1
    return 0


def _js_canonical_string(text):
    """Quote style and needless escapes of a JS string do not matter: `'a'` == `"a"`."""
    body = text[1:-1]
    out = []
    k = 0
    while k < len(body):
        ch = body[k]
        if ch == "\\" and k + 1 < len(body):
            nxt = body[k + 1]
            out.append(nxt if nxt in "'\"" else ch + nxt)
            k += 2
            continue
        out.append(ch)
        k += 1
    value = "".join(out)
    # re-escape the double quotes that are not already escaped
    result = []
    k = 0
    while k < len(value):
        ch = value[k]
        if ch == "\\" and k + 1 < len(value):
            result.append(value[k : k + 2])
            k += 2
            continue
        result.append('\\"' if ch == '"' else ch)
        k += 1
    return '"' + "".join(result) + '"'


# ---------------------------------------------------------------------------------------------
# Shared rendering for the brace languages
# ---------------------------------------------------------------------------------------------

def _rust_drop_diverging_semicolons(toks):
    """`{ return x; }` and `{ return x }` are the same block (it never produces a value), and
    rustfmt writes either: the `;` that ends a final `return`, `break` or `continue` statement is
    dropped."""
    out = list(toks)
    drop = set()
    for i, t in enumerate(toks):
        if not (t.is_p(";") and i + 1 < len(toks) and toks[i + 1].is_p("}")):
            continue
        depth = 0
        k = i - 1
        while k >= 0:
            u = toks[k]
            if u.kind == "punct" and u.text in ")]}":
                depth += 1
            elif u.kind == "punct" and u.text in "([{":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and u.is_p(";"):
                break
            k -= 1
        if k + 1 < i and toks[k + 1].kind == "ident" and toks[k + 1].text in ("return", "break", "continue"):
            drop.add(i)
    return [t for i, t in enumerate(out) if i not in drop] if drop else toks


def _rust_unwrap_single_expression_blocks(toks):
    """rustfmt adds braces around a match-arm or closure body it has to wrap, and removes them from
    one that fits: `x => { f(a) }` is `x => f(a),` and `|t| { g(t) }` is `|t| g(t)`. A block with no
    statement (no `;` at its own level) is the expression itself, so the braces are dropped, and an
    arm always ends with its comma."""
    opens = []
    pair = {}
    for i, t in enumerate(toks):
        if t.kind == "punct" and t.text in "([{":
            opens.append(i)
        elif t.kind == "punct" and t.text in ")]}" and opens:
            pair[opens.pop()] = i
    remove = set()
    add_comma = set()
    for i, j in pair.items():
        t = toks[i]
        if not t.is_p("{") or i == 0 or j == i + 1 or not toks[i - 1].is_p("=>", "|", "||"):
            continue
        depth = 0
        statement = False
        for k in range(i + 1, j):
            u = toks[k]
            if u.kind == "punct" and u.text in "([{":
                depth += 1
            elif u.kind == "punct" and u.text in ")]}":
                depth -= 1
            elif depth == 0 and u.is_p(";"):
                statement = True
                break
        if statement:
            continue
        remove.add(i)
        remove.add(j)
        if toks[i - 1].is_p("=>"):
            add_comma.add(j)
    if not remove:
        return toks
    out = []
    for i, t in enumerate(toks):
        if i in remove:
            if i in add_comma and not (i + 1 < len(toks) and toks[i + 1].is_p(",")):
                out.append(Tok("punct", ",", t.start, t.end))
            continue
        out.append(t)
    return out


def _drop_trailing_commas(toks, language):
    """Removes the commas a formatter adds or removes: a comma before a closer, and (Rust) the comma
    after a block-bodied match arm. `(a,)` stays in Rust: it is a one-element tuple."""
    out = []
    stack = []
    skip_comma = False
    for tk in toks:
        if skip_comma and tk.is_p(","):
            skip_comma = False
            continue
        skip_comma = False
        if tk.kind == "punct" and tk.text in "([{":
            stack.append([tk.text, out[-1] if out else None, 0])
            out.append(tk)
        elif tk.kind == "punct" and tk.text in ")]}":
            frame = stack.pop() if stack else None
            if out and out[-1].is_p(","):
                keep = False
                if language == "rust" and tk.text == ")" and frame and frame[2] == 1:
                    p = frame[1]
                    call_like = p is not None and (
                        (p.kind == "ident" and p.text not in _RUST_NOT_CALL) or p.is_p(")", "]", ">")
                    )
                    keep = not call_like
                if not keep:
                    out.pop()
            if language == "rust" and tk.text == "}" and frame and frame[1] is not None and frame[1].is_p("=>"):
                skip_comma = True
            out.append(tk)
        else:
            if tk.is_p(","):
                if stack:
                    stack[-1][2] += 1
            elif language == "rust" and tk.is_p(">") and out and out[-1].is_p(","):
                out.pop()
            out.append(tk)
    return out


def _js_drop_semicolons(toks):
    """A `;` that automatic semicolon insertion would supply anyway (before `}`, at the end of the
    text, or at a line end whose next token cannot continue the statement) is formatting. A `;`
    inside parentheses (`for (;;)`) is never touched."""
    out = []
    depth = 0
    for k, t in enumerate(toks):
        if t.is_p("(", "[") or t.kind == "tpl_head":
            depth += 1
        elif t.is_p(")", "]") or t.kind == "tpl_tail":
            depth -= 1
        if t.is_p(";") and depth == 0:
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            if nxt is None or nxt.is_p("}", ";"):
                continue
            continues = (
                (nxt.kind == "punct" and nxt.text in _JS_CONT_START)
                or nxt.kind in ("tpl", "tpl_head")
                or (nxt.kind == "ident" and nxt.text in _JS_CONT_WORDS)
            )
            if nxt.nl and not continues:
                continue
        out.append(t)
    return out


def _js_drop_arrow_parens(toks):
    out = []
    k = 0
    while k < len(toks):
        if (
            toks[k].is_p("(")
            and k + 3 < len(toks)
            and toks[k + 1].kind == "ident"
            and toks[k + 2].is_p(")")
            and toks[k + 3].is_p("=>")
        ):
            out.append(toks[k + 1])
            k += 3
            continue
        out.append(toks[k])
        k += 1
    return out


def _tok_text(tok, language):
    if language == "javascript" and tok.kind == "str":
        return _js_canonical_string(tok.text)
    if language == "sql" and tok.kind == "ident":
        return tok.text.lower()
    return tok.text


def render_tokens(toks, language):
    """Canonical text of a token list: one space between tokens, a line break after `{`, `;` and
    before `}`, indentation by brace depth. Depends on the tokens only."""
    lines = []
    cur = []
    depth = 0

    def flush():
        if cur:
            lines.append("    " * depth + " ".join(cur))
            cur.clear()

    for tk in toks:
        text = _tok_text(tk, language)
        if tk.is_p("}"):
            flush()
            depth = max(depth - 1, 0)
            cur.append(text)
        elif tk.is_p("{"):
            cur.append(text)
            flush()
            depth += 1
        elif tk.is_p(";"):
            cur.append(text)
            flush()
        else:
            cur.append(text)
    flush()
    return "\n".join(lines)


def _finish_brace_tokens(toks, language):
    if language == "rust":
        toks = _rust_unwrap_single_expression_blocks(_rust_drop_diverging_semicolons(toks))
    toks = _drop_trailing_commas(toks, language)
    if language == "javascript":
        toks = _js_drop_arrow_parens(_js_drop_semicolons(toks))
    return toks


# ---------------------------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------------------------

_SQL_DOLLAR = re.compile(r"\$([A-Za-z_]\w*)?\$")
_SQL_NUM = re.compile(r"\d[\d_]*(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?")
_SQL_PUNCT2 = ("<=", ">=", "<>", "!=", "||", "::", "->>", "->", "=>", ":=", "<<", ">>", "#>>", "#>")


def lex_sql(text, offset=0):
    toks = []
    n = len(text)
    i = 0
    nl = False

    def add(kind, j):
        nonlocal i, nl
        toks.append(Tok(kind, text[i:j], i + offset, j + offset, nl))
        nl = False
        i = j

    while i < n:
        c = text[i]
        if c.isspace():
            if c == "\n":
                nl = True
            i += 1
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            add("comment", n if j < 0 else j)
            continue
        if text.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise NormalizeError("unterminated block comment")
            if "\n" in text[i:j]:
                nl = True
            add("comment", j)
            continue
        if c == "'" or (c in "eE" and text[i + 1 : i + 2] == "'"):
            escapes = c != "'"
            j = i + (2 if escapes else 1)
            while j < n:
                if escapes and text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "'":
                    if text[j + 1 : j + 2] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise NormalizeError("unterminated string")
            add("str", j + 1)
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == '"':
                    if text[j + 1 : j + 2] == '"':
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise NormalizeError("unterminated quoted identifier")
            add("qident", j + 1)
            continue
        m = _SQL_DOLLAR.match(text, i)
        if m:
            close = m.group(0)
            j = text.find(close, m.end())
            if j < 0:
                raise NormalizeError("unterminated dollar quote")
            body = text[m.end() : j]
            add("dollar", j + len(close))
            toks[-1].text = (close, body, i + offset + len(close))
            continue
        if c.isdigit() or (c == "." and text[i + 1 : i + 2].isdigit()):
            add("num", _SQL_NUM.match(text, i).end())
            continue
        m = _IDENT.match(text, i)
        if m:
            add("ident", m.end())
            continue
        for op in _SQL_PUNCT2:
            if text.startswith(op, i):
                add("punct", i + len(op))
                break
        else:
            add("punct", i + 1)
    return toks


def _sql_flatten(toks):
    """Dollar-quoted bodies are re-lexed as SQL so a comment or spacing inside a function body does
    not matter; a body that does not lex stays a verbatim string."""
    out = []
    for t in toks:
        if t.kind != "dollar":
            out.append(t)
            continue
        close, body, body_start = t.text
        try:
            inner = [x for x in lex_sql(body, body_start) if x.kind != "comment"]
        except NormalizeError:
            out.append(Tok("str", close + body + close, t.start, t.end, t.nl))
            continue
        out.append(Tok("punct", close, t.start, t.start, t.nl))
        out.extend(_sql_flatten(inner))
        out.append(Tok("punct", close, t.end, t.end))
    return out


def _sql_item_end(sig, i):
    depth = 0
    for j in range(i, len(sig)):
        t = sig[j]
        if t.kind == "punct":
            if t.text == "(":
                depth += 1
            elif t.text == ")":
                depth -= 1
            elif t.text == ";" and depth <= 0:
                return j
    return len(sig)


# ---------------------------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------------------------

_SH_OPS = ("<<<", "<<-", "<<", ">>", "&&", "||", ";;", ">&", "<&", ">|", "<>")
_SH_STOP = set(" \t\r\n;&|()<>")


def _sh_scan_paren(text, j):
    """`j` is the index of `(`; returns the index after its matching `)`."""
    n = len(text)
    depth = 0
    while j < n:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "'":
            k = text.find("'", j + 1)
            j = n if k < 0 else k + 1
            continue
        if ch == '"':
            j = _sh_scan_dq(text, j + 1)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise NormalizeError("unterminated $( )")


def _sh_scan_dq(text, j):
    n = len(text)
    while j < n:
        ch = text[j]
        if ch == "\\":
            j += 2
        elif ch == '"':
            return j + 1
        elif text.startswith("$(", j):
            j = _sh_scan_paren(text, j + 1)
        elif ch == "`":
            k = text.find("`", j + 1)
            if k < 0:
                raise NormalizeError("unterminated backtick")
            j = k + 1
        else:
            j += 1
    raise NormalizeError("unterminated double quote")


def _sh_scan_word(text, i):
    n = len(text)
    j = i
    while j < n:
        ch = text[j]
        if ch in _SH_STOP:
            break
        if ch == "\\":
            j += 2
        elif ch == "'":
            k = text.find("'", j + 1)
            if k < 0:
                raise NormalizeError("unterminated single quote")
            j = k + 1
        elif ch == '"':
            j = _sh_scan_dq(text, j + 1)
        elif text.startswith("$(", j):
            j = _sh_scan_paren(text, j + 1)
        elif text.startswith("${", j):
            depth = 0
            while j < n:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
        elif ch == "`":
            k = text.find("`", j + 1)
            if k < 0:
                raise NormalizeError("unterminated backtick")
            j = k + 1
        else:
            j += 1
    return j


def lex_shell(text):
    toks = []
    n = len(text)
    i = 0
    heredocs = []
    while i < n:
        c = text[i]
        if c == "\n":
            toks.append(Tok("nl", "\n", i, i + 1))
            i += 1
            for delim, strip in heredocs:
                start = i
                while True:
                    end = text.find("\n", i)
                    line = text[i : n if end < 0 else end]
                    if (line.lstrip("\t") if strip else line) == delim:
                        i = n if end < 0 else end
                        break
                    if end < 0:
                        raise NormalizeError("unterminated heredoc")
                    i = end + 1
                toks.append(Tok("heredoc", text[start:i], start, i))
            heredocs = []
            continue
        if c in " \t\r":
            i += 1
            continue
        if c == "\\" and text[i + 1 : i + 2] == "\n":
            i += 2
            continue
        if c == "#":
            j = text.find("\n", i)
            j = n if j < 0 else j
            toks.append(Tok("comment", text[i:j], i, j))
            i = j
            continue
        if c in ";&|()<>":
            op = next((o for o in _SH_OPS if text.startswith(o, i)), c)
            toks.append(Tok("op", op, i, i + len(op)))
            i += len(op)
            if op in ("<<", "<<-"):
                k = i
                while k < n and text[k] in " \t":
                    k += 1
                e = _sh_scan_word(text, k)
                delim = re.sub(r"""['"\\]""", "", text[k:e])
                heredocs.append((delim, op == "<<-"))
            continue
        j = _sh_scan_word(text, i)
        if j == i:
            j = i + 1
        toks.append(Tok("word", text[i:j], i, j))
        i = j
    return toks


_SH_OPEN = {"if", "case", "for", "while", "until", "select", "{"}
_SH_CLOSE = {"fi", "esac", "done", "}"}
_SH_CMD_AFTER = {"then", "do", "else", "elif", "!", "{", "time"}


def _shell_item_end(sig, i):
    depth = 0
    paren = 0
    cmd = True
    j = i
    while j < len(sig):
        t = sig[j]
        if t.kind == "nl" or (t.kind == "op" and t.text in (";", "&")):
            if depth == 0 and paren == 0 and j > i:
                # `name()` followed by a newline and `{`: the body is still to come
                body = [x.text for x in sig[i:j]]
                if not (len(body) == 3 and body[1:] == ["(", ")"] or (len(body) == 4 and body[0] == "function")):
                    return j
                k = j
                while k < len(sig) and sig[k].kind == "nl":
                    k += 1
                if k >= len(sig) or sig[k].text not in ("{", "("):
                    return j
                j = k
                continue
            cmd = True
            j += 1
            continue
        if t.kind == "op":
            if t.text == "(":
                paren += 1
                cmd = True
            elif t.text == ")":
                paren -= 1 if paren > 0 else 0
            elif t.text in ("&&", "||", "|", ";;"):
                cmd = True
            j += 1
            continue
        if t.kind == "word":
            if cmd and t.text in _SH_OPEN:
                depth += 1
            elif cmd and t.text in _SH_CLOSE:
                depth -= 1
                if depth < 0:
                    return j
            cmd = t.text in _SH_CMD_AFTER
        j += 1
    return len(sig)


_SH_SEP_DROP_AFTER = {"&&", "||", "|", "&", ";;"}


def render_shell(toks):
    lines = []
    cur = []
    last = None  # last emitted token text, "\n" for a separator

    def sep():
        nonlocal last
        if cur:
            lines.append(" ".join(cur))
            cur.clear()
        if last is not None:
            last = "\n"

    for t in toks:
        if t.kind == "nl" or (t.kind == "op" and t.text == ";"):
            if last is None or last in _SH_SEP_DROP_AFTER or last == "\n":
                continue
            sep()
            continue
        if t.kind == "heredoc":
            if cur:
                lines.append(" ".join(cur))
                cur.clear()
            lines.append(t.text)
            last = "\n"
            continue
        if t.text == "{" and last == "\n" and not cur and lines and lines[-1].endswith("( )"):
            cur.append(lines.pop())  # `name()` newline `{`: the same function header
        cur.append(t.text)
        last = t.text
        if t.text in _SH_SEP_DROP_AFTER and t.kind == "op" and t.text in ("&", ";;"):
            sep()
            last = t.text
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------------------------

_XML_UNKNOWN_ENTITY = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\w+);)([A-Za-z_][\w.-]*);")


class _XNode:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs
        self.children = []


def _xml_escape(s, attr=False):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace('"', "&quot;") if attr else s


def render_xml_node(node):
    lines = []

    def walk(nd, depth):
        pad = "  " * depth
        attrs = "".join(f' {k}="{_xml_escape(v, True)}"' for k, v in sorted(nd.attrs.items()))
        kids = []
        for ch in nd.children:
            if isinstance(ch, str):
                if kids and isinstance(kids[-1], str):
                    kids[-1] += ch
                else:
                    kids.append(ch)
            else:
                kids.append(ch)
        kids = [
            (re.sub(r"\s+", " ", k).strip() if isinstance(k, str) else k) for k in kids
        ]
        kids = [k for k in kids if not (isinstance(k, str) and not k)]
        if not kids:
            lines.append(f"{pad}<{nd.tag}{attrs}/>")
            return
        lines.append(f"{pad}<{nd.tag}{attrs}>")
        for k in kids:
            if isinstance(k, str):
                lines.append(f"{pad}  {_xml_escape(k)}")
            else:
                walk(k, depth + 1)
        lines.append(f"{pad}</{nd.tag}>")

    walk(node, 0)
    return "\n".join(lines)


def parse_xml(text):
    """Returns `(roots, anchored)`: the document's element nodes and `{anchor_name: node}` for every
    base-declared anchor comment, bound to the next element that starts after it."""
    text = _XML_UNKNOWN_ENTITY.sub(r"&amp;\1;", text)
    parser = xml.parsers.expat.ParserCreate()
    stack = []
    roots = []
    pending = []
    anchored = {}

    def start(tag, attrs):
        node = _XNode(tag, dict(attrs))
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)
        for name in pending:
            anchored.setdefault(name, node)
        del pending[:]

    def end(_tag):
        stack.pop()

    def chars(data):
        if stack:
            stack[-1].children.append(data)

    def comment(data):
        if "[@ANCHOR" not in data:
            return
        for m in va.ANCHOR_PATTERN.finditer(data):
            if _is_base_anchor_declaration("<!--" + data + "-->", _shift_match(data, m), ""):
                pending.append(va._clean(m.group(1)))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.CommentHandler = comment
    try:
        parser.Parse(text, True)
    except xml.parsers.expat.ExpatError as error:
        raise NormalizeError(f"malformed XML: {error}")
    return roots, anchored


def _shift_match(data, m):
    """The same match re-found in `<!--data-->` (a match against the comment's own line)."""
    line = "<!--" + data + "-->"
    for m2 in va.ANCHOR_PATTERN.finditer(line):
        if m2.start() == m.start() + 4:
            return m2
    return m


# ---------------------------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------------------------

_PY_STRING_PREFIX = re.compile(r"^([A-Za-z]*)('''|\"\"\"|'|\")", re.S)
_PY_FSTRING_STARTS = tuple(
    getattr(tokenize, name) for name in ("FSTRING_START", "TSTRING_START") if hasattr(tokenize, name)
)
_PY_FSTRING_ENDS = tuple(
    getattr(tokenize, name) for name in ("FSTRING_END", "TSTRING_END") if hasattr(tokenize, name)
)
_PY_AUGASSIGN = {"+=", "-=", "*=", "/=", "//=", "%=", "@=", "&=", "|=", "^=", ">>=", "<<=", "**=", "="}
_PY_PAREN_PREV = _PY_AUGASSIGN | {"return", "if", "elif", "while", "assert", "in", "else"}


def _py_canonical_string(src):
    m = _PY_STRING_PREFIX.match(src)
    if not m:
        return src
    prefix = m.group(1).lower()
    if "f" in prefix or "t" in prefix:
        quote = m.group(2)
        body = src[len(m.group(0)) : len(src) - len(quote)]
        if quote in ("'", "'''") and '"' not in body and "\\" not in body:
            quote = '"' if quote == "'" else '"""'
        return prefix + quote + body + quote
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            value = ast.literal_eval(src)
    except (ValueError, SyntaxError):
        return src
    import json

    if isinstance(value, bytes):
        return "b" + json.dumps(value.decode("latin-1"))
    return json.dumps(value)


def _py_is_plain_string(tok):
    m = _PY_STRING_PREFIX.match(tok.string)
    return bool(m) and not set(m.group(1).lower()) & set("bft")


class PythonTokens:
    """Tokens of one Python source text, with f-strings merged into single STRING tokens (so the
    token stream is the same on Python 3.8 and 3.14) and docstrings marked."""

    def __init__(self, text):
        self.text = text
        self._starts = [0]
        for line in text.split("\n"):
            self._starts.append(self._starts[-1] + len(line) + 1)
        try:
            raw = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, IndentationError, SyntaxError) as error:
            raise NormalizeError(f"cannot tokenize Python: {error}")
        self.toks = self._merge_fstrings(raw)
        self.docs = self._docstring_indexes()

    def _offset(self, pos):
        return self._starts[pos[0] - 1] + pos[1]

    def _merge_fstrings(self, raw):
        out = []
        k = 0
        while k < len(raw):
            t = raw[k]
            if t.type in _PY_FSTRING_STARTS:
                depth = 1
                m = k + 1
                while m < len(raw) and depth:
                    if raw[m].type in _PY_FSTRING_STARTS:
                        depth += 1
                    elif raw[m].type in _PY_FSTRING_ENDS:
                        depth -= 1
                    m += 1
                last = raw[m - 1]
                src = self.text[self._offset(t.start) : self._offset(last.end)]
                out.append(tokenize.TokenInfo(tokenize.STRING, src, t.start, last.end, t.line))
                k = m
                continue
            out.append(t)
            k += 1
        return out

    def _docstring_indexes(self):
        toks = self.toks
        skip = (tokenize.NL, tokenize.COMMENT)
        docs = set()
        first = 0
        while first < len(toks) and toks[first].type in skip + (tokenize.ENCODING,):
            first += 1
        j = first
        while j < len(toks) and toks[j].type == tokenize.STRING and _py_is_plain_string(toks[j]):
            j += 1
        if j > first and j < len(toks) and toks[j].type == tokenize.NEWLINE:
            docs.update(range(first, j + 1))  # the module docstring
        line_first = None
        line_last = None
        is_def = False
        k = 0
        while k < len(toks):
            t = toks[k]
            if t.type in skip or t.type in (tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING):
                k += 1
                continue
            if t.type == tokenize.NEWLINE:
                if is_def and line_last is not None and toks[line_last].string == ":":
                    j = k + 1
                    while j < len(toks) and toks[j].type in skip:
                        j += 1
                    if j < len(toks) and toks[j].type == tokenize.INDENT:
                        j += 1
                        while j < len(toks) and toks[j].type in skip:
                            j += 1
                        run = []
                        while j < len(toks) and toks[j].type == tokenize.STRING and _py_is_plain_string(toks[j]):
                            run.append(j)
                            j += 1
                            while j < len(toks) and toks[j].type in skip:
                                j += 1
                        if run and j < len(toks) and toks[j].type == tokenize.NEWLINE:
                            docs.update(range(run[0], j + 1))
                line_first = line_last = None
                is_def = False
                k += 1
                continue
            if line_first is None:
                line_first = k
                is_def = t.string in ("def", "class") or (
                    t.string == "async" and k + 1 < len(toks) and toks[k + 1].string == "def"
                )
            line_last = k
            k += 1
        return docs

    def normalize(self, start_row=None, end_row=None):
        """Canonical text of the tokens whose first line is in `[start_row, end_row]` (1-based,
        inclusive; the whole text by default)."""
        lo = start_row if start_row is not None else 0
        hi = end_row if end_row is not None else 1 << 60
        depth = 0
        lines = []
        cur = []  # (type, text)
        bracket = 0

        def flush():
            if cur:
                items = _py_simplify(cur)
                if items:
                    lines.append("    " * max(depth, 0) + " ".join(s for _t, s in items))
            cur.clear()

        for k, t in enumerate(self.toks):
            if not lo <= t.start[0] <= hi or k in self.docs:
                continue
            ty = t.type
            if ty in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING, tokenize.ENDMARKER):
                continue
            if ty in (tokenize.INDENT, tokenize.DEDENT):
                # the indentation changes that arrive before the slice's first code token belong
                # to the code above it: the baseline depth is the first token's own
                if not lines and not cur:
                    continue
                flush()
                depth += 1 if ty == tokenize.INDENT else -1
            elif ty == tokenize.NEWLINE:
                flush()
                bracket = 0
            elif ty == tokenize.STRING:
                cur.append((ty, _py_canonical_string(t.string)))
            elif ty == tokenize.NUMBER:
                cur.append((ty, t.string.lower()))
            elif ty == tokenize.OP:
                if t.string in "([{":
                    bracket += 1
                elif t.string in ")]}":
                    bracket -= 1
                if t.string == ";" and bracket == 0:
                    flush()
                else:
                    cur.append((ty, t.string))
            else:
                cur.append((ty, t.string))
        flush()
        return "\n".join(lines)


def _py_simplify(items):
    items = _py_drop_trailing_commas(items)
    changed = True
    while changed:
        items, changed = _py_drop_one_paren_wrap(items)
    return items


def _py_drop_trailing_commas(items):
    out = []
    stack = []
    for ty, s in items:
        if ty == tokenize.OP and s in "([{":
            stack.append([s, out[-1] if out else None, 0])
            out.append((ty, s))
        elif ty == tokenize.OP and s in ")]}":
            frame = stack.pop() if stack else None
            if out and out[-1] == (tokenize.OP, ",") and frame is not None:
                opener, prev, commas = frame
                p_ty, p_s = prev if prev else (None, "")
                call_like = prev is not None and (
                    (p_ty == tokenize.NAME and not keyword.iskeyword(p_s))
                    or (p_ty == tokenize.OP and p_s in (")", "]", "}") )
                    or p_ty == tokenize.STRING
                )
                keep = commas == 1 and ((opener == "(" and not call_like) or (opener == "[" and call_like))
                if not keep:
                    out.pop()
            out.append((ty, s))
        else:
            if ty == tokenize.OP and s == "," and stack:
                stack[-1][2] += 1
            out.append((ty, s))
    return out


def _py_drop_one_paren_wrap(items):
    """Removes one pair of parentheses that only wraps a whole right-hand side, return value,
    condition, loop target or loop iterable (what black adds around a long line). A tuple keeps its
    parentheses wherever dropping them would change the meaning (`assert (a, b)`, `x in (a, b)`)."""
    first = items[0][1] if items else ""
    closers = (",", ")", "]", "}")
    for k, (ty, s) in enumerate(items):
        if not (ty == tokenize.OP and s == "("):
            continue
        p = items[k - 1][1] if k else None
        depth = 0
        close = None
        for m in range(k, len(items)):
            t2, s2 = items[m]
            if t2 == tokenize.OP and s2 in "([{":
                depth += 1
            elif t2 == tokenize.OP and s2 in ")]}":
                depth -= 1
                if depth == 0:
                    close = m
                    break
        if close is None or close == k + 1:
            continue
        rest = items[close + 1 :]
        nxt = rest[0][1] if rest else None
        tuple_ok = False
        if p is None:
            if nxt != "=":
                continue
            tuple_ok = True  # `(a, b) = x`
        elif p in _PY_PAREN_PREV:
            if rest and not (len(rest) == 1 and nxt == ":") and nxt not in closers:
                continue
            tuple_ok = (p in _PY_AUGASSIGN or p in ("return", "yield")) and nxt not in closers or (
                p == "in" and first in ("for", "async") and len(rest) == 1
            )
        elif p == "yield" and not rest:
            tuple_ok = True
        elif p == "for" and first in ("for", "async") and k <= 2 and nxt == "in":
            tuple_ok = True  # `for (a, b) in x`
        elif p in (",", "(", "[", "{", ":") and (nxt in closers or (p != "," and nxt in ("for", "async"))):
            pass  # a whole argument, element or value (or a comprehension's element)
        elif p == "except" and nxt in ("as", ":"):
            pass
        elif p == "assert" and nxt == ",":
            pass
        elif p == "not" and rest and len(rest) == 1 and nxt == ":":
            pass
        else:
            continue
        inner = items[k + 1 : close]
        if inner[0][1] in ("*", "**"):
            continue
        d = 0
        bad = False
        for t2, s2 in inner:
            if t2 == tokenize.OP and s2 in "([{":
                d += 1
            elif t2 == tokenize.OP and s2 in ")]}":
                d -= 1
            elif d == 0 and (
                (t2 == tokenize.OP and s2 == ":=")
                or (t2 == tokenize.OP and s2 == "," and not tuple_ok)
                or (t2 == tokenize.NAME and s2 in ("yield", "for", "as"))
            ):
                bad = True
                break
        if bad:
            continue
        return items[:k] + inner + items[close + 1 :], True
    return items, False


# ---------------------------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------------------------

def _sig_and_all(text, language):
    if language == "rust":
        toks = lex_rust(text)
    elif language == "javascript":
        toks = lex_js(text)
    elif language == "sql":
        toks = lex_sql(text)
    elif language == "shell":
        toks = lex_shell(text)
    else:
        raise NormalizeError(f"no token lexer for {language!r}")
    return toks, [t for t in toks if t.kind != "comment"]


def _render(sig_slice, language):
    if language == "shell":
        return render_shell(sig_slice)
    if language == "sql":
        sig_slice = _sql_flatten(sig_slice)
    return render_tokens(_finish_brace_tokens(sig_slice, language), language)


def normalize_text(text, language):
    """Canonical text of a whole source text."""
    if language == "python":
        return PythonTokens(text).normalize()
    if language == "xml":
        roots, _anchored = parse_xml(text)
        return "\n".join(render_xml_node(r) for r in roots)
    _all, sig = _sig_and_all(text, language)
    return _render(sig, language)


def normalized_hash(text, language):
    return sha256_text(normalize_text(text, language))


def _line_of(text, pos):
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return start, text[start : len(text) if end < 0 else end]


def _anchor_comment_names(text, tok):
    """Base-declared anchor names in one comment token."""
    names = []
    if "[@ANCHOR" not in tok.text:
        return names
    for m in va.ANCHOR_PATTERN.finditer(tok.text):
        pos = tok.start + m.start()
        line_start, line = _line_of(text, pos)
        prev_line = ""
        if line_start > 0:
            prev_line = _line_of(text, line_start - 1)[1]
        for m2 in va.ANCHOR_PATTERN.finditer(line):
            if m2.start() == pos - line_start:
                if _is_base_anchor_declaration(line, m2, prev_line):
                    names.append(m2.group(1))
                break
    return names


def anchored_texts(text, language, wanted=None):
    """`{anchor_name: canonical text of the anchored item}` for every base-declared anchor in a
    Rust, JavaScript, XML, shell or SQL source. `wanted` (a set of names) limits the work."""
    if language == "xml":
        _roots, anchored = parse_xml(text)
        return {
            va._clean(name): render_xml_node(node)
            for name, node in anchored.items()
            if wanted is None or va._clean(name) in wanted
        }
    if "[@ANCHOR" not in text:
        return {}
    all_toks, sig = _sig_and_all(text, language)
    ends = {"rust": _rust_item_end, "javascript": _js_item_end, "sql": _sql_item_end, "shell": _shell_item_end}[language]
    out = {}
    starts = [t.start for t in sig]
    import bisect

    for tok in all_toks:
        if tok.kind != "comment":
            continue
        for name in _anchor_comment_names(text, tok):
            clean = va._clean(name)
            if clean in out or (wanted is not None and clean not in wanted):
                continue
            i = bisect.bisect_left(starts, tok.end)
            if language == "shell":
                while i < len(sig) and sig[i].kind == "nl":
                    i += 1
            if i >= len(sig):
                continue
            j = ends(sig, i)
            out[clean] = _render(sig[i:j], language)
    return out


def anchored_hashes(text, language, wanted=None):
    return {name: sha256_text(t) for name, t in anchored_texts(text, language, wanted).items()}


def normalize_named(text, language, name):
    """Canonical text of the function or anchored item called `name` (an anchor name, a Python
    `Class.method` / function name, a Rust `fn name`, a JavaScript function, method or variable),
    or None when it is not found. Used by the blind-pass view."""
    if language == "python":
        tree = ast.parse(text)
        lines = text.splitlines()
        want = name.split(".")[-1]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == want:
                start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
                end = getattr(node, "end_lineno", node.lineno)
                return PythonTokens(text).normalize(start, min(end, len(lines)))
        return None
    anchored = anchored_texts(text, language)
    if name in anchored:
        return anchored[name]
    if language == "xml":
        return None
    all_toks, sig = _sig_and_all(text, language)
    ends = {"rust": _rust_item_end, "javascript": _js_item_end, "sql": _sql_item_end, "shell": _shell_item_end}[language]
    base = name.split("::")[-1].split(".")[-1]
    for i, t in enumerate(sig):
        if t.kind not in ("ident", "word") or t.text != base:
            continue
        prev = sig[i - 1].text if i else ""
        nxt = sig[i + 1].text if i + 1 < len(sig) else ""
        if language == "rust" and prev != "fn":
            continue
        if language == "javascript" and not (
            prev in ("function", "const", "let", "var", "get", "set", "async", "static", "*")
            or nxt in ("(", ":", "=")
        ):
            continue
        if language == "shell" and not (nxt == "(" or prev == "function"):
            continue
        if language == "sql" and prev.lower() not in ("function", "procedure", "view", "table"):
            continue
        # walk back to the start of the item: modifiers and attributes on the same statement
        k = i
        while k > 0 and not sig[k].nl and sig[k - 1].kind in ("ident", "word", "str"):
            k -= 1
        if language == "rust":
            while k > 0 and sig[k - 1].is_p("]"):
                depth = 0
                m = k - 1
                while m >= 0:
                    if sig[m].is_p("]"):
                        depth += 1
                    elif sig[m].is_p("["):
                        depth -= 1
                        if depth == 0:
                            break
                    m -= 1
                if m > 0 and sig[m - 1].is_p("#"):
                    k = m - 1
                else:
                    break
        j = ends(sig, k)
        return _render(sig[k:j], language)
    return None
