#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
SCSS min()/max()/clamp() Checker
-------------------------------------------------------------------------------------------
Odoo compiles .scss with libsass (python `sass`, falling back to the `sassc` binary: both are
libsass; odoo/addons/base/models/assetsbundle.py, ScssStylesheetAsset). libsass implements
`min()` and `max()` as Sass BUILT-IN functions and evaluates them, which is wrong for CSS
math: `min-width: min(12rem, 100%);` made the compiler raise "Incompatible units: '%' and
'rem'", and Odoo then replaces the WHOLE web.assets_frontend bundle with its error stub (a red
"A css error occured" banner, every page unstyled). That took hams.com's stylesheet down after
a deploy on 2026-10-11/12; perens.com had the same class of failure earlier that day
(Undefined variable $o-base-color-palette). Neither the Odoo test suite nor the deploy
noticed. This is the static half of the guard; ham_base's test_asset_bundles_compile.py (the
real bundles, real compiler) and devbox_tools/production_readiness_check.py (the live sites)
are the other two.

What the compiler does, probed 2026-10-12 against the installed libsass 3.6.5 with the exact
options Odoo uses (output_style=expanded, precision=8) -- not assumed:

  min()/max()  EVALUATED. Fails with "Incompatible units" when the arguments' units cannot
               convert to each other. Compatible: unitless with anything (`min(1rem, 2)`,
               `min(0, 100%)`), same unit, the absolute lengths px/in/cm/mm/pt/pc (not Q), s/ms,
               deg/grad/rad/turn, Hz/kHz, dpi/dpcm/dppx. EVERYTHING ELSE conflicts, including
               %/vw/vh/vmin/vmax/em/rem/ch/ex/fr against each other and against px.
               Also fails ("is not a number for `min'") when an argument is var(), env(),
               calc(), attr(), clamp(), an interpolation `#{...}` or any other non-number.
               Fails the same way inside var() fallbacks, url(), @media, @supports, @include
               arguments, `$variable: ...`, maps, if() and any function argument.
  clamp()      NOT a Sass built-in here: passed through as plain CSS, so
               `clamp(1.5rem, 2.6vw, 2.5rem)` is fine and so is clamp(1px, 2rem, 3%).
               But the arguments are still ordinary SCSS expressions: `100% - 2rem` or
               `5vw + 1px` inside clamp() fails, and a min()/max() nested in it is evaluated.
               (`2vw * 2`, `100%/3` and `2vw -1rem` do not fail.)
  Passes through untouched (never evaluated, so never flagged): inside a quoted string
               (`unquote("min(12rem, 100%)")`, the form to use), inside calc() /
               -webkit-calc() / -moz-calc(), inside a raw custom-property value
               (`--w: min(12rem, 100%)`), inside // and /* */ comments, and the upper-case
               spellings MIN()/Max() (case-sensitive function names).
  NOT a way out: `#{min(12rem, 100%)}` -- interpolation evaluates its contents, it fails the
               same. An interpolation INSIDE a string (`unquote("#{min(...)}")`) also fails.

So this checker reports, for every .scss file, a min()/max() call whose arguments mix
incompatible unit classes or contain a non-number, and +/- arithmetic of incompatible units
inside a clamp() argument, outside the pass-through regions above. Arguments it cannot
classify (a `$variable`, a user function) are skipped rather than guessed: the bundle-compile
test covers those against the real compiler. There is no escape-hatch comment; write
`unquote("min(12rem, 100%)")`.

Usage: check_scss_math_functions.py <repo_root> [<more_repo_roots>...]
"""

import os
import re
import sys

SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".claude", "vendor", "target", "radae"}

# libsass unit groups that convert among themselves. A unit not listed forms a class of its own.
_UNIT_GROUPS = (
    {"px", "in", "cm", "mm", "pt", "pc"},
    {"s", "ms"},
    {"deg", "grad", "rad", "turn"},
    {"Hz", "kHz"},
    {"dpi", "dpcm", "dppx"},
)
_RAW_FUNCTIONS = ("calc", "-webkit-calc", "-moz-calc")
# Calls that are not numbers to the Sass min()/max() ("is not a number for `min'").
_NON_NUMBER_CALLS = re.compile(r"(?<![\w$.-])(var|env|calc|-webkit-calc|-moz-calc|attr|clamp|constant)\s*\(")
_CALL = re.compile(r"(?<![\w$.#-])(min|max)\(")
_CLAMP = re.compile(r"(?<![\w$.#-])clamp\(")
_NUMBER = re.compile(r"(?<![\w$.#])(\d*\.?\d+)(%|[A-Za-z]+)?")
_CUSTOM_PROPERTY = re.compile(r"(?<![\w$-])--[\w-]+\s*:")


def unit_class(unit):
    """Conversion class of a unit string: '' for a bare number, else a hashable group id."""
    if not unit:
        return ""
    for index, group in enumerate(_UNIT_GROUPS):
        if unit in group:
            return f"group{index}"
    return unit


def _interpolation_end(text, start):
    """`start` is the index just after `#{`; return the index of the matching `}` (or len)."""
    depth = 1
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            i = _string_end(text, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text)


def _string_end(text, start):
    """Index just after the string literal opening at text[start] (interpolations skipped)."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "#" and text[i + 1 : i + 2] == "{":
            i = _interpolation_end(text, i + 2) + 1
            continue
        if ch == quote:
            return i + 1
        i += 1
    return len(text)


def _blank(chars, start, end):
    for k in range(start, min(end, len(chars))):
        if chars[k] != "\n":
            chars[k] = " "


def _mask_raw(text, chars, start, end):
    """Blank text[start:end] except the bodies of `#{...}` interpolations, which Sass evaluates
    even inside otherwise raw text, and the strings' interpolations inside them."""
    i = start
    while i < end:
        if text[i] == "#" and text[i + 1 : i + 2] == "{":
            body_end = _interpolation_end(text, i + 2)
            _blank(chars, i, i + 2)
            _mask(text, chars, i + 2, body_end)
            if body_end < len(chars):
                chars[body_end] = " "
            i = body_end + 1
            continue
        if chars[i] != "\n":
            chars[i] = " "
        i += 1


def _paren_end(text, open_index):
    """Index of the `)` matching the `(` at open_index (strings and interpolations skipped)."""
    depth = 0
    i = open_index
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            i = _string_end(text, i)
            continue
        if ch == "#" and text[i + 1 : i + 2] == "{":
            i = _interpolation_end(text, i + 2) + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text) - 1


def _mask(text, chars, start, end):
    """Fill `chars` (a list copy of text) with the evaluated-code view of text[start:end]:
    comments, string text, raw calc() arguments and custom-property values become spaces;
    interpolation bodies stay. Offsets and newlines are preserved for line numbers."""
    i = start
    while i < end:
        ch = text[i]
        two = text[i : i + 2]
        if two == "//":
            j = text.find("\n", i)
            j = end if j < 0 or j > end else j
            _blank(chars, i, j)
            i = j
            continue
        if two == "/*":
            j = text.find("*/", i + 2)
            j = end if j < 0 else min(j + 2, end)
            _blank(chars, i, j)
            i = j
            continue
        if ch in "\"'":
            j = _string_end(text, i)
            # String text is never evaluated; its #{...} interpolations are.
            k = i + 1
            while k < j:
                if text[k] == "\\":
                    chars[k] = " "
                    k += 2
                    continue
                if text[k] == "#" and text[k + 1 : k + 2] == "{":
                    body_end = _interpolation_end(text, k + 2)
                    _blank(chars, k, k + 2)
                    _mask(text, chars, k + 2, body_end)
                    if body_end < len(chars):
                        chars[body_end] = " "
                    k = body_end + 1
                    continue
                if chars[k] != "\n":
                    chars[k] = " "
                k += 1
            i = j
            continue
        if ch == "-" or ch == "c":
            matched = next(
                (name for name in _RAW_FUNCTIONS if text.startswith(name + "(", i) and not _word_before(text, i)),
                None,
            )
            if matched:
                open_index = i + len(matched)
                close = _paren_end(text, open_index)
                _mask_raw(text, chars, open_index + 1, close)
                i = close + 1
                continue
        if ch == "-" and two == "--":
            m = _CUSTOM_PROPERTY.match(text, i)
            if m and not _word_before(text, i):
                j = _declaration_end(text, m.end(), end)
                _mask_raw(text, chars, m.end(), j)
                i = j
                continue
        i += 1


def _word_before(text, index):
    return index > 0 and (text[index - 1].isalnum() or text[index - 1] in "_$.-#")


def _declaration_end(text, start, end):
    """End of a custom-property value: the next `;` or unmatched `}` outside parens/strings."""
    depth = 0
    i = start
    while i < end:
        ch = text[i]
        if ch in "\"'":
            i = _string_end(text, i)
            continue
        if ch == "#" and text[i + 1 : i + 2] == "{":
            i = _interpolation_end(text, i + 2) + 1
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth <= 0 and ch in ";}":
            return i
        i += 1
    return end


def evaluated_view(text):
    """`text` with everything libsass never evaluates blanked out (see the module docstring)."""
    chars = list(text)
    _mask(text, chars, 0, len(text))
    return "".join(chars)


def _split_args(view, open_index, close_index):
    args = []
    depth = 0
    current = []
    for ch in view[open_index + 1 : close_index]:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(current))
            current = []
        else:
            current.append(ch)
    args.append("".join(current))
    return [a.strip() for a in args]


def _blank_calls(arg):
    """Replace every `name(...)` call in `arg` with a placeholder, returning (text, names)."""
    names = []
    out = []
    i = 0
    while i < len(arg):
        m = re.compile(r"[A-Za-z_-][\w-]*\(").match(arg, i)
        if m and not _word_before(arg, i):
            close = _paren_end(arg, m.end() - 1)
            names.append(m.group(0)[:-1])
            out.append("\x00")
            i = close + 1
            continue
        out.append(arg[i])
        i += 1
    return "".join(out), names


def _split_terms(text):
    """Split on binary + and -. Like Sass, `a - b` and `a-b` are subtraction but `a -b` is a
    space-separated list (not arithmetic), and the operator needs an operand before it."""
    terms = []
    current = []
    for index, ch in enumerate(text):
        if ch in "+-" and "".join(current).strip():
            before_space = text[index - 1].isspace() if index else False
            after_space = text[index + 1 : index + 2].isspace()
            prev = "".join(current).rstrip()[-1:]
            if before_space == after_space and (prev.isalnum() or prev in "%)\x00."):
                terms.append("".join(current))
                current = []
                continue
        current.append(ch)
    terms.append("".join(current))
    return [t for t in terms if t.strip()]


def classify_argument(arg):
    """Return (problem, unit_class). problem is a message when the argument cannot be a number
    to libsass or mixes incompatible units in +/- arithmetic; unit_class is the argument's
    conversion class ('' unitless, None unknown)."""
    if "#{" in arg:
        return ("an interpolation `#{...}` is a string, not a number", None)
    flat, calls = _blank_calls(arg)
    for name in calls:
        if _NON_NUMBER_CALLS.match(name + "("):
            return (f"`{name}()` is not a number to the compiler", None)
    if "$" in flat:
        return (None, None)
    term_classes = []
    for term in _split_terms(flat):
        units = {unit_class(m.group(2)) for m in _NUMBER.finditer(term) if m.group(2)}
        if term.strip() == "\x00" and calls == ["percentage"]:
            term_classes.append("%")
        elif "\x00" in term or "/" in term or len(units) > 1:
            term_classes.append(None)
        elif units:
            term_classes.append(next(iter(units)))
        else:
            term_classes.append("")
    known = {c for c in term_classes if c}
    if len(known) > 1:
        return (f"adds/subtracts incompatible units ({_describe(flat)})", None)
    if None in term_classes:
        return (None, None)
    return (None, next(iter(known)) if known else "")


def _describe(flat):
    return ", ".join(sorted({m.group(2) for m in _NUMBER.finditer(flat) if m.group(2)}))


def _line_of(text, index):
    return text.count("\n", 0, index) + 1


def check_scss_text(text, name="<scss>"):
    """Return a list of 'name:line: message' violations for one SCSS source."""
    view = evaluated_view(text)
    violations = []
    for match in _CALL.finditer(view):
        open_index = match.end() - 1
        close_index = _paren_end(view, open_index)
        function = match.group(1)
        args = _split_args(view, open_index, close_index)
        problems = []
        classes = {}
        for arg in args:
            problem, cls = classify_argument(arg)
            if problem:
                problems.append(f"argument `{arg}` {problem}")
            elif cls:
                classes.setdefault(cls, arg)
        if len(classes) > 1:
            shown = "; ".join(f"`{a}`" for a in classes.values())
            problems.append(f"arguments mix units that cannot be compared ({shown})")
        if problems:
            call = " ".join(view[match.start() : close_index + 1].split())
            violations.append(
                f"{name}:{_line_of(text, match.start())}: `{call}` -- {function}() is evaluated by Odoo's SCSS "
                f"compiler (libsass) and fails with Incompatible units / is not a number: "
                + "; ".join(problems)
                + f'. Write unquote("{function}(...)") so the CSS function passes through unevaluated '
                "(`#{...}` does not help)."
            )
    for match in _CLAMP.finditer(view):
        open_index = match.end() - 1
        close_index = _paren_end(view, open_index)
        for arg in _split_args(view, open_index, close_index):
            problem, _cls = classify_argument(arg)
            if problem and "adds/subtracts" in problem:
                violations.append(
                    f"{name}:{_line_of(text, match.start())}: clamp() argument `{arg}` {problem}: the compiler "
                    f"evaluates it as Sass arithmetic and fails (Incompatible units). Wrap the argument in "
                    f'calc() or write unquote("clamp(...)").'
                )
    return violations


def check_scss_math_functions(repo_root):
    violations = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in files:
            if not filename.endswith(".scss"):
                continue
            path = os.path.join(root, filename)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except (UnicodeDecodeError, OSError) as error:
                print(f"Warning: could not read {path}: {error}")
                continue
            violations.extend(check_scss_text(text, os.path.relpath(path, repo_root)))
    return violations


def resolve_repo_roots(given_paths):
    """The given roots, de-duplicated; a hams_shared path stands for its parent repository."""
    roots = []
    for given in given_paths:
        given = os.path.abspath(given)
        if os.path.basename(given) == "hams_shared":
            given = os.path.dirname(given)
        if given not in roots:
            roots.append(given)
    return roots


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_scss_math_functions.py <repo_root> [<repo_root>...]")
        sys.exit(2)
    found = []
    for scan_root in resolve_repo_roots(sys.argv[1:]):
        found.extend(check_scss_math_functions(scan_root))
    if found:
        print("❌ SCSS min()/max()/clamp() violations (Odoo's libsass would fail the whole asset bundle):")
        for line in found:
            print(f"  - {line}")
        sys.exit(1)
    print("✅ No SCSS min()/max()/clamp() violations found.")
    sys.exit(0)
