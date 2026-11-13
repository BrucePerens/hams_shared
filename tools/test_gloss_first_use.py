# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests [@ANCHOR: gloss_first_use:find_missing] [@ANCHOR: gloss_first_use:apply_glosses]"""

import os
import tempfile
import unittest

import gloss_first_use

TERMS = {"CW": "continuous wave", "TLS": "Transport Layer Security", "SDR": "software-defined radio"}


def glossed(text):
    return gloss_first_use.apply_glosses(text, TERMS)


class FindMissingTests(unittest.TestCase):
    def test_first_prose_use_is_glossed_and_later_uses_are_left(self):
        # Tests [@ANCHOR: gloss_first_use:apply_glosses]
        self.assertEqual(glossed("Send CW now. CW is slow.\n"),
                         "Send CW (continuous wave) now. CW is slow.\n")

    def test_skips_headings_code_links_paths_tables_and_anchor_lines(self):
        # Tests [@ANCHOR: gloss_first_use:apply_glosses]
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_gloss_first_use_fixture.md"),
                  encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual(glossed(text), text.replace("Real CW use", "Real CW (continuous wave) use"))

    def test_front_matter_is_skipped(self):
        # Tests [@ANCHOR: gloss_first_use:find_missing]
        text = "---\ntitle: CW\n---\nCW here.\n"
        self.assertEqual([(t, o) for t, o, _ in gloss_first_use.find_missing(text, TERMS)], [("CW", 18)])

    def test_existing_glosses_pass(self):
        # Tests [@ANCHOR: gloss_first_use:find_missing]
        for text in (
                "CW (Morse) is old.\n", "Continuous wave (CW) is old.\n", "Transport\nLayer Security: TLS.\n",
                "A software-defined radio, an SDR.\n", "Keys (see TLS: Transport Layer Security).\n",
                "CW is Morse.\n", "TLS, i.e. security.\n"):
            self.assertEqual(gloss_first_use.find_missing(text, TERMS), [], text)

    def test_hyphenated_dotted_and_embedded_uses_are_not_uses(self):
        # Tests [@ANCHOR: gloss_first_use:find_missing]
        for text in ("RTL-SDR and CW-only and SDRX and a.CW and CW.md.\n", "No terms.\n"):
            self.assertEqual(gloss_first_use.find_missing(text, TERMS), [], text)

    def test_plural_and_sentence_end_count_as_use(self):
        # Tests [@ANCHOR: gloss_first_use:apply_glosses]
        self.assertEqual(glossed("Many SDRs.\nAn SDR.\n"), "Many SDRs (software-defined radio).\nAn SDR.\n")
        self.assertEqual(glossed("It uses TLS.\n"), "It uses TLS (Transport Layer Security).\n")

    def test_use_inside_parentheses_does_not_nest(self):
        # Tests [@ANCHOR: gloss_first_use:apply_glosses]
        self.assertEqual(glossed("Keys (see TLS) here.\n"), "Keys (see TLS: Transport Layer Security) here.\n")
        self.assertEqual(glossed("Keys (a CW key) here.\n"), "Keys (a CW (continuous wave) key) here.\n")

    def test_list_continuation_is_prose_not_code(self):
        # Tests [@ANCHOR: gloss_first_use:apply_glosses]
        self.assertEqual(glossed("- item\n\n    continues with CW\n"),
                         "- item\n\n    continues with CW (continuous wave)\n")

    def test_apply_is_idempotent_and_changes_only_the_gloss(self):
        # Tests [@ANCHOR: gloss_first_use:apply_glosses]
        text = "A CW and TLS. [@ANCHOR: x:y]\n\nSDR and CW.\n"
        once = glossed(text)
        self.assertEqual(glossed(once), once)
        self.assertEqual(once.replace(" (continuous wave)", "").replace(" (Transport Layer Security)", "")
                         .replace(" (software-defined radio)", ""), text)


class CommandLineTests(unittest.TestCase):
    def test_check_lists_then_apply_fixes(self):
        # Tests [@ANCHOR: gloss_first_use:find_missing]
        with tempfile.TemporaryDirectory() as directory:
            terms = os.path.join(directory, "terms.txt")
            document = os.path.join(directory, "a.md")
            with open(terms, "w", encoding="utf-8") as handle:
                handle.write("# comment\nCW | continuous wave\n")
            with open(document, "w", encoding="utf-8") as handle:
                handle.write("x\nSend CW.\n")
            self.assertEqual(gloss_first_use.main(["--terms", terms, directory]), 1)
            self.assertEqual(gloss_first_use.main(["--terms", terms, "--apply", directory]), 0)
            with open(document, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "x\nSend CW (continuous wave).\n")
            self.assertEqual(gloss_first_use.main(["--terms", terms, directory]), 0)


if __name__ == "__main__":
    unittest.main()
