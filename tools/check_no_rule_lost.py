#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
check_no_rule_lost: proves that text moved out of an instruction file (CLAUDE.md, AGENTS.md, a skill) still exists, word for word.

    check_no_rule_lost.py --before OLD [OLD...] --after NEW [NEW...]   exit 0 when every unit of OLD is in some NEW, else 1
    check_no_rule_lost.py --before origin/main:CLAUDE.md --after CLAUDE.md docs/agent_ondemand/*.md
    check_no_rule_lost.py --measure CLAUDE.md AGENTS.md                characters and a rough token estimate (characters / 4)

Why (2026-11-09, token optimization): every agent re-reads the instruction files on every turn from the prompt cache, so a section
needed only for one kind of task (production host notes, legacy invocations, lint machine details) is moved verbatim into an on-demand
document and replaced by a short pointer. The meta-editing rule of AGENTS.md forbids deleting or condensing a rule, so the move has to
be provable: this tool splits the OLD text into units (each heading, paragraph, list item and fenced code block, whitespace collapsed)
and requires every unit to appear verbatim, with collapsed whitespace, inside one of the NEW files. A unit that was reworded, shortened
or dropped is printed with the sentences of it that are missing. The pointer lines added in place of moved text are new text and are
not checked. `check_summation_bias.py` uses `moved_verbatim()` to accept a shrink of a file whose old text all landed in the same change.

An argument of the form REV:PATH (`origin/main:AGENTS.md`) is read with `git show` in the current directory.
"""
import argparse
import re
import subprocess
import sys

_HEADING = re.compile(r"^(#{1,6}\s|<\/?[A-Za-z_][\w-]*>\s*$)")
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?:;])\s+")
MIN_UNIT_CHARS = 3


# [@ANCHOR: check_no_rule_lost:normalize]
def normalize(text):
    """`text` with every run of whitespace (newlines and indentation included) collapsed to one space, stripped."""
    return " ".join(text.split())


# [@ANCHOR: check_no_rule_lost:units]
def units(text):
    """The units of a Markdown text, in order, each normalized: a heading or lone tag line, a paragraph, a list item with its
    continuation lines, a fenced code block (fence lines included). Units shorter than MIN_UNIT_CHARS are left out."""
    out, current = [], []
    in_fence = False

    def flush():
        if current:
            unit = normalize("\n".join(current))
            if len(unit) >= MIN_UNIT_CHARS:
                out.append(unit)
            current.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_fence:
                flush()
                in_fence = True
                current.append(line)
            else:
                current.append(line)
                in_fence = False
                flush()
            continue
        if in_fence:
            current.append(line)
        elif not stripped:
            flush()
        elif _HEADING.match(stripped):
            flush()
            current.append(line)
            flush()
        elif _LIST_ITEM.match(line):
            flush()
            current.append(line)
        else:
            current.append(line)
    flush()
    return out


# [@ANCHOR: check_no_rule_lost:missing_units]
def missing_units(before_texts, after_texts):
    """Units of the `before_texts` that do not appear verbatim (normalized) in any of the `after_texts`, in order, each once."""
    haystacks = [normalize(text) for text in after_texts]
    missing, seen = [], set()
    for text in before_texts:
        for unit in units(text):
            if unit in seen:
                continue
            seen.add(unit)
            if not any(unit in hay for hay in haystacks):
                missing.append(unit)
    return missing


# [@ANCHOR: check_no_rule_lost:moved_verbatim]
def moved_verbatim(old_text, new_texts):
    """True when every unit of `old_text` is present verbatim in one of `new_texts` (the file's new content and the other files of
    the same change): nothing was deleted or condensed, only moved."""
    return not missing_units([old_text], new_texts)


def missing_sentences(unit, after_texts):
    """The sentences of `unit` that appear in none of `after_texts` (for the report)."""
    haystacks = [normalize(text) for text in after_texts]
    return [s for s in _SENTENCE_END.split(unit) if s and not any(s in hay for hay in haystacks)]


# [@ANCHOR: check_no_rule_lost:read_source]
def read_source(spec):
    """The text of a file path, or of REV:PATH through `git show` when the path part does not name a local file."""
    if ":" in spec and not spec.startswith("/"):
        result = subprocess.run(["git", "show", spec], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout
    with open(spec, encoding="utf-8") as handle:
        return handle.read()


def token_estimate(text):
    """A rough token count: characters / 4 (no tokenizer is installed on the dev box)."""
    return round(len(text) / 4)


# [@ANCHOR: check_no_rule_lost:main]
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n\n")[0])
    parser.add_argument("--before", nargs="+", default=[], help="the old text: files or REV:PATH")
    parser.add_argument("--after", nargs="+", default=[], help="every file the old text may now be in")
    parser.add_argument("--measure", nargs="+", default=[], help="print characters and ~tokens (chars/4) of these files")
    options = parser.parse_args(argv)
    if not options.measure and not (options.before and options.after):
        parser.error("give --before and --after, or --measure")
    for spec in options.measure:
        text = read_source(spec)
        print("%8d chars  ~%6d tokens (chars/4)  %s" % (len(text), token_estimate(text), spec))
    if not options.before:
        return 0
    before = [read_source(spec) for spec in options.before]
    after = [read_source(spec) for spec in options.after]
    total = sum(len(units(text)) for text in before)
    missing = missing_units(before, after)
    if not missing:
        print("check_no_rule_lost: OK, all %d units of %s are present verbatim in %s" % (total, ", ".join(options.before), ", ".join(options.after)))
        return 0
    print("check_no_rule_lost: FAIL, %d of %d units are not present verbatim in the new text:" % (len(missing), total))
    for unit in missing:
        print("  - unit: %s" % (unit[:200] + ("..." if len(unit) > 200 else "")))
        for sentence in missing_sentences(unit, after):
            print("      missing sentence: %s" % sentence[:200])
    return 1


if __name__ == "__main__":
    sys.exit(main())
