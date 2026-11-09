#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_external_programs_documented.py (ADR 0114 decision 5, ADR 0113 anchors).

Real files in temporary trees; the last class also checks the shipped ADR against the shipped dependency_watch.json.

Tests [@ANCHOR: ext_programs:code_mask]
Tests [@ANCHOR: ext_programs:test_spans]
Tests [@ANCHOR: ext_programs:rust_launches]
Tests [@ANCHOR: ext_programs:script_dependencies]
Tests [@ANCHOR: ext_programs:adr_tables]
Tests [@ANCHOR: ext_programs:documented_names]
Tests [@ANCHOR: ext_programs:watch_findings]
Tests [@ANCHOR: ext_programs:find_findings]
Tests [@ANCHOR: ext_programs:main]
"""

import contextlib
import glob
import io
import json
import os
import tempfile
import unittest
import unittest.mock

import check_external_programs_documented as cepd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

ADR = """# ADR
| Program | Role | Obtained | Watched |
|---|---|---|---|
| Pat (`la5nta/pat`) | Winlink | download | yes (release) |
| Direwolf (`wb2osz/direwolf`) | TNC | mirror | **no** |
| `rigctl` / `rigctld` | rig | with Hamlib | yes |

| Platform | Programs | Used for |
|---|---|---|
| Linux | `amixer`, `pactl`; `tpm2_create.exe` | audio |

`find_pat_binary` `find_direwolf_binary` `find_mercury_binary` `find_ardopcf_binary`
"""

WATCH = {"pat": {"repo": "la5nta/pat", "mode": "release", "pinned": "v1"}}


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class CodeMaskTests(unittest.TestCase):
    def test_comments_and_literals_are_blanked_but_length_and_lines_kept(self):
        src = 'let a = "{"; // {\n/* { /* } */ } */ let c = \'}\'; let r = r#"}"#; fn f<\'a>() {}\n'
        masked = cepd.code_mask(src)
        self.assertEqual(len(masked), len(src))
        self.assertEqual(masked.count("\n"), src.count("\n"))
        self.assertEqual(masked.count("{"), 1)
        self.assertEqual(masked.count("}"), 1)
        self.assertIn("fn f<'a>() {}", masked)

    def test_escaped_quote_does_not_end_a_string(self):
        masked = cepd.code_mask('let s = "a\\"{"; {')
        self.assertEqual(masked.count("{"), 1)


    def test_raw_byte_and_raw_c_strings_are_blanked_without_escapes(self):
        for prefix in ("br", "cr", "r"):
            src = 'let a = %s"x\\"; Command::new("later");' % prefix
            masked = cepd.code_mask(src)
            self.assertEqual(len(masked), len(src))
            self.assertIn('Command::new("', masked, prefix)
            self.assertEqual(cepd.rust_launches(src), ({"later"}, 0), prefix)
        self.assertEqual(cepd.rust_launches('let a = br#"q"#; let b = 1; Command::new("p");'), ({"p"}, 0))
        self.assertEqual(cepd.code_mask('let a = br#"{"#;').count("{"), 0)


class TestSpansTests(unittest.TestCase):
    def test_cfg_test_module_with_nested_braces_is_one_span(self):
        src = "fn a() {}\n#[cfg(test)]\nmod tests { fn t() { if x { y } } }\nfn b() {}\n"
        spans = cepd.test_spans(cepd.code_mask(src))
        self.assertEqual(len(spans), 1)
        a, b = spans[0]
        self.assertTrue(src[a:b].startswith("#[cfg(test)]"))
        self.assertTrue(src[a:b].endswith("}"))
        self.assertNotIn("fn b", src[a:b])

    def test_out_of_line_module_and_stacked_attribute(self):
        src = "#[cfg(test)]\n#[path = \"x.rs\"]\nmod x;\nfn keep() {}\n"
        (a, b), = cepd.test_spans(cepd.code_mask(src))
        self.assertEqual(src[a:b].strip().splitlines()[-1], "mod x;")

    def test_cfg_all_test_counts(self):
        src = "#[cfg(all(test, unix))]\nfn only_in_tests() { x }\n"
        self.assertEqual(len(cepd.test_spans(cepd.code_mask(src))), 1)


class RustLaunchTests(unittest.TestCase):
    def test_literal_dynamic_comment_and_test_launches(self):
        src = (
            'fn a() { Command::new("pactl"); std::process::Command::new("amixer"); Command::new(path); }\n'
            '// Command::new("commented")\n'
            '#[cfg(test)]\nmod tests { fn t() { Command::new("python3"); Command::new(x); } }\n'
        )
        names, dynamic = cepd.rust_launches(src)
        self.assertEqual(names, {"pactl", "amixer"})
        self.assertEqual(dynamic, 1)


class ScriptTests(unittest.TestCase):
    def test_command_v_is_found_and_comments_are_not(self):
        text = "# command -v nothing\nif command -v systemctl >/dev/null; then :; fi\ncommand -v cargo-deb || true\n"
        self.assertEqual(cepd.script_dependencies(text), {"systemctl", "cargo-deb"})

    def test_powershell_get_command(self):
        text = "Get-Command schtasks.exe -ErrorAction SilentlyContinue\n# Get-Command no\n"
        self.assertEqual(cepd.script_dependencies(text, powershell=True), {"schtasks.exe"})


class AdrTests(unittest.TestCase):
    def test_tables_skip_separator_rows(self):
        tables = cepd.adr_tables(ADR)
        self.assertEqual(len(tables), 2)
        self.assertEqual(tables[0][0][0], "Program")
        self.assertEqual(len(tables[0][1]), 3)

    def test_documented_names_quoted_words_and_first_cell_words(self):
        names = cepd.documented_names(ADR)
        for expected in ("pat", "direwolf", "rigctl", "rigctld", "amixer", "pactl", "tpm2_create", "la5nta"):
            self.assertIn(expected, names)
        self.assertNotIn("winlink", names)


class WatchTests(unittest.TestCase):
    def test_unwatched_and_missing_repos_are_found(self):
        findings = cepd.watch_findings(ADR, WATCH)
        self.assertEqual(len(findings), 2)
        self.assertTrue(any("wb2osz/direwolf" in f and "no entry" in f for f in findings))
        self.assertTrue(any("wb2osz/direwolf" in f and "not watched" in f for f in findings))

    def test_watch_entry_the_adr_never_mentions_is_found(self):
        watch = dict(WATCH, extra={"repo": "someone/else", "mode": "release", "pinned": "1"})
        self.assertTrue(any("extra" in f and "not in ADR" in f for f in cepd.watch_findings(ADR, watch)))

    def test_shipped_adr_and_watch_agree(self):
        adr = glob.glob(os.path.join(HERE, "..", "docs", "adrs", "0114_*.md"))[0]
        with open(adr, encoding="utf-8") as f:
            adr_text = f.read()
        with open(os.path.join(HERE, "dependency_watch.json"), encoding="utf-8") as f:
            watch = json.load(f)
        self.assertEqual(cepd.watch_findings(adr_text, watch), [])
        for name in ("direwolf", "ka9q-radio"):
            self.assertIn(name, watch)


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.adr = os.path.join(self.root, "adr.md")
        self.watch = os.path.join(self.root, "watch.json")
        adr = ADR.replace("**no**", "yes")
        _write(self.adr, adr)
        _write(self.watch, json.dumps({**WATCH, "direwolf": {"repo": "wb2osz/direwolf", "mode": "branch", "pinned": "x"}}))
        self.relay = os.path.join(self.root, "daemons", "hams_local_relay")
        for func, rel in cepd.DISCOVERY_FUNCTIONS.items():
            _write(os.path.join(self.relay, rel), f"pub fn {func}() {{}}\n")

    def test_clean_tree_has_no_findings(self):
        _write(os.path.join(self.relay, "src", "audio.rs"), 'fn a() { Command::new("pactl"); }\n')
        self.assertEqual(cepd.find_findings(self.root, self.adr, self.watch), [])

    def test_undocumented_launch_script_and_test_file_handling(self):
        _write(os.path.join(self.relay, "src", "net.rs"), 'fn a() { Command::new("traceroute"); }\n')
        _write(os.path.join(self.relay, "src", "net_tests.rs"), 'fn t() { Command::new("python3"); }\n')
        _write(os.path.join(self.relay, "install_x.sh"), "command -v mokutil\ncommand -v amixer\n")
        _write(os.path.join(self.relay, "packaging", "install_windows.ps1"), "Get-Command certutil.exe\n")
        findings = cepd.find_findings(self.root, self.adr, self.watch)
        self.assertEqual(len(findings), 3, findings)
        self.assertTrue(any("traceroute" in f for f in findings))
        self.assertTrue(any("mokutil" in f for f in findings))
        self.assertTrue(any("certutil" in f for f in findings))
        self.assertFalse(any("python3" in f for f in findings))

    def test_missing_discovery_function_and_unnamed_one(self):
        os.remove(os.path.join(self.relay, "src", "winlink.rs"))
        _write(self.adr, ADR.replace("**no**", "yes").replace("`find_mercury_binary`", ""))
        findings = cepd.find_findings(self.root, self.adr, self.watch)
        self.assertTrue(any("find_pat_binary" in f and "no longer defines" in f for f in findings))
        self.assertTrue(any("find_mercury_binary" in f and "does not name" in f for f in findings))

    def test_repository_without_relay_only_checks_the_watch(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(cepd.find_findings(empty, self.adr, self.watch), [])

    def test_main_reports_findings_with_exit_1(self):
        out = io.StringIO()
        with unittest.mock.patch.object(cepd, "find_findings", return_value=["x is not documented"]):
            with contextlib.redirect_stdout(out):
                self.assertEqual(cepd.main([self.root]), 1)
        self.assertIn("x is not documented", out.getvalue())
        self.assertIn("FIX:", out.getvalue())

    def test_main_exit_codes_and_usage(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cepd.main([]), 2)
            with tempfile.TemporaryDirectory() as empty:
                self.assertEqual(cepd.main([empty]), 0)
        self.assertIn("SUCCESS", out.getvalue())
        _write(os.path.join(self.relay, "src", "net.rs"), 'fn a() { Command::new("zzz_unknown"); }\n')
        self.assertEqual(cepd.find_findings(self.root, self.adr, self.watch)[0].count("zzz_unknown"), 1)


if __name__ == "__main__":
    unittest.main()
