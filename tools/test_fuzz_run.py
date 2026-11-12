#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
"""Tests for fuzz_run.py and hypothesis_scale.py: crash detection, the to-do stub, dedup across runs, the live-checkout refusal, a fake `cargo fuzz`
end to end, and a real Hypothesis counterexample.

Tests [@ANCHOR: fuzz_run:new_crash_files]
Tests [@ANCHOR: fuzz_run:extract_stack]
Tests [@ANCHOR: fuzz_run:stub_text]
Tests [@ANCHOR: fuzz_run:is_live_checkout]
Tests [@ANCHOR: fuzz_run:repo_of]
Tests [@ANCHOR: fuzz_run:state_paths]
Tests [@ANCHOR: fuzz_run:falsifying_signature]
Tests [@ANCHOR: fuzz_run:cargo_fuzz_available]
Tests [@ANCHOR: fuzz_run:run_cargo_fuzz]
Tests [@ANCHOR: fuzz_run:run_hypothesis]
Tests [@ANCHOR: fuzz_run:run_libfuzzer]
Tests [@ANCHOR: fuzz_run:run_libfuzzer_worker]
Tests [@ANCHOR: fuzz_run:summary_line]
Tests [@ANCHOR: fuzz_run:main]
Tests [@ANCHOR: hypothesis_scale:scale_item]
"""
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fuzz_run  # noqa: E402
import hypothesis_scale  # noqa: E402
import overnight_common as common  # noqa: E402

heavy = unittest.skipIf(os.environ.get("HAMS_SKIP_HEAVY"), "heavy: starts real jobs and ram_dirs.py")
SHEBANG = "#" + "!/bin/bash"  # the fake scripts below carry one; a literal one in this file would not be on line 1

FAKE_CARGO = r'''@SH@
# fake `cargo fuzz`: --version works; `run T corpus -- -artifact_prefix=P/` writes a crash when FAKE_CRASH is set; tmin prints a minimized path; `run T file` panics
shift  # "fuzz"
case "$1" in
  --version) echo "cargo-fuzz 0.0-fake"; exit 0 ;;
  tmin)
    crash="$3"; dir=$(dirname "$crash"); printf 'M' > "$dir/minimized-from-fake"
    printf 'Minimized artifact:\n\n\t%s/minimized-from-fake\n' "$dir"; exit 0 ;;
  run)
    last="${@: -1}"
    if [ -f "$3" ] && [ "${3##*.}" = "min" ]; then
      echo "thread '<unnamed>' panicked at src/frame.rs:12:5:"; echo "index out of bounds"; echo "   0: frame::parse"; exit 101
    fi
    for a in "$@"; do case "$a" in -artifact_prefix=*) prefix="${a#-artifact_prefix=}";; esac; done
    if [ -n "$FAKE_CRASH" ]; then printf '%s' "$FAKE_CRASH" > "${prefix}crash-fake"; exit 77; fi
    exit 0 ;;
esac
'''

FAKE_LIBFUZZER_CARGO = r'''@SH@
# fake `cargo build ... --bin B`: writes a fake libFuzzer binary into $CARGO_TARGET_DIR
bin="${@: -1}"
d="$CARGO_TARGET_DIR/x86_64-unknown-linux-gnu/release"; mkdir -p "$d"
cat > "$d/$bin" <<'BIN'
@SH@
for a in "$@"; do
  case "$a" in
    -minimize_crash=1) min=1;;
    -exact_artifact_path=*) out="${a#-exact_artifact_path=}";;
    -artifact_prefix=*) prefix="${a#-artifact_prefix=}";;
  esac
done
if [ -n "$min" ]; then printf 'M' > "$out"; exit 77; fi
if [ -f "$1" ] && [ "${1##*.}" = "min" ]; then echo "thread 'main' panicked at src/tlv.rs:9:1:"; echo "slice index"; exit 101; fi
if [ -n "$FAKE_CRASH" ]; then printf '%s' "$FAKE_CRASH" > "${prefix}crash-fake"; exit 77; fi
exit 0
BIN
chmod +x "$d/$bin"
'''


class PureTests(unittest.TestCase):
    def test_new_crash_files_skips_seen_and_other_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, data in (("crash-a", b"1"), ("oom-b", b"2"), ("minimized-from-x", b"3"), ("notes.txt", b"4")):
                with open(os.path.join(tmp, name), "wb") as handle:
                    handle.write(data)
            found = fuzz_run.new_crash_files(tmp, set())
            self.assertEqual([os.path.basename(p) for p, _ in found], ["crash-a", "oom-b"])
            self.assertEqual(fuzz_run.new_crash_files(tmp, {found[0][1]}), found[1:])
            self.assertEqual(fuzz_run.new_crash_files(os.path.join(tmp, "none"), set()), [])

    def test_extract_stack_starts_at_the_panic_else_takes_the_tail(self):
        text = "noise\nmore\nthread 'main' panicked at src/a.rs:1:1:\nboom\n  0: a\n"
        self.assertTrue(fuzz_run.extract_stack(text).startswith("thread 'main' panicked"))
        self.assertEqual(fuzz_run.extract_stack("a\nb\nc", limit=2), "b\nc")
        self.assertEqual(len(fuzz_run.extract_stack("panicked at x\n" + "l\n" * 200, limit=10).splitlines()), 10)

    def test_stub_has_front_matter_repro_input_and_stack(self):
        text = fuzz_run.stub_text({"name": "relay frame", "kind": "cargo-fuzz", "dir": "/w/hams_com/daemons/x"}, "ab" * 32, "cargo fuzz run t in.min", "/m/in.min",
                                  "panicked at x", when="2026-11-09")
        self.assertTrue(text.startswith("---\nstatus: open\n"))
        for needle in ("repo: hams_com", "created_at: 2026-11-09", "cargo fuzz run t in.min", "/m/in.min", "panicked at x", "abababababab"):
            self.assertIn(needle, text)

    def test_repo_of_reads_the_directory_names(self):
        self.assertEqual(fuzz_run.repo_of("/x/job/hams_shared_wt/tools"), "hams_shared")
        self.assertEqual(fuzz_run.repo_of("/x/hams_open/daemons/a"), "hams_open")
        self.assertEqual(fuzz_run.repo_of("/x/other"), "")

    def test_live_main_checkout_is_detected_a_worktree_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "hams_com"))
            os.makedirs(os.path.join(tmp, "job", "hams_com"))
            self.assertTrue(fuzz_run.is_live_checkout(os.path.join(tmp, "hams_com"), tmp))
            self.assertFalse(fuzz_run.is_live_checkout(os.path.join(tmp, "job", "hams_com"), tmp))

    def test_a_directory_inside_a_live_main_checkout_is_detected_and_one_inside_a_worktree_is_not(self):
        # `dir` of a cargo-fuzz target is the crate, a subdirectory of the checkout, not its root
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "hams_com", "daemons", "relay"))
            os.makedirs(os.path.join(tmp, "hams_open", "hams_shared", "tools"))
            os.makedirs(os.path.join(tmp, "job", "hams_com", "daemons", "relay"))
            os.makedirs(os.path.join(tmp, "hams_open", "tmp", "wt", "daemons"))
            open(os.path.join(tmp, "hams_open", "tmp", "wt", ".git"), "w").close()  # a worktree nested in the main checkout
            self.assertTrue(fuzz_run.is_live_checkout(os.path.join(tmp, "hams_com", "daemons", "relay"), tmp))
            self.assertTrue(fuzz_run.is_live_checkout(os.path.join(tmp, "hams_open", "hams_shared", "tools"), tmp))
            self.assertFalse(fuzz_run.is_live_checkout(os.path.join(tmp, "job", "hams_com", "daemons", "relay"), tmp))
            self.assertFalse(fuzz_run.is_live_checkout(os.path.join(tmp, "hams_open", "tmp", "wt", "daemons"), tmp))

    def test_state_paths_and_signature(self):
        self.assertTrue(fuzz_run.state_paths("/o", "a b")["seen"].endswith("state/a-b/seen.json"))
        sha, block = fuzz_run.falsifying_signature("x\nFalsifying example: f(a=1,\n  b=2)\n\nnext")
        self.assertIn("b=2", block)
        self.assertEqual(sha, fuzz_run.falsifying_signature("Falsifying example: f(a=1,\n    b=2)\n")[0])
        self.assertIsNone(fuzz_run.falsifying_signature("all fine"))

    def test_summary_line(self):
        docs = [{"status": "clean", "new_crashes": []}, {"status": "crashes", "new_crashes": [{}, {}]}]
        self.assertEqual(fuzz_run.summary_line(docs), "fuzz: 2 target(s): 1 clean, 1 crashes; new crashes 2")

    def test_hypothesis_scale_replaces_explicit_settings_and_ignores_plain_functions(self):
        from hypothesis import given, settings, strategies as st

        @settings(max_examples=5)
        @given(st.integers())
        def check(n):
            pass
        self.assertTrue(hypothesis_scale.scale_item(check, 777))
        self.assertEqual(check._hypothesis_internal_use_settings.max_examples, 777)
        self.assertIsNone(check._hypothesis_internal_use_settings.deadline)
        self.assertFalse(hypothesis_scale.scale_item(lambda: 1, 777))


class FakeLoad:
    def critical(self):
        return None


@heavy
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HAMS_REPORTS_DIR": os.path.join(self.tmp.name, "rep"), "HAMS_JOBS_DIR": os.path.join(self.tmp.name, "jobs")})
        env.start()
        self.addCleanup(env.stop)
        self.cargo = os.path.join(self.tmp.name, "cargo")
        with open(self.cargo, "w") as handle:
            handle.write(FAKE_CARGO.replace("@SH@", SHEBANG))
        os.chmod(self.cargo, os.stat(self.cargo).st_mode | stat.S_IXUSR)
        self.crate = os.path.join(self.tmp.name, "job", "hams_com", "daemons", "x")
        os.makedirs(os.path.join(self.crate, "fuzz"))
        self.targets = os.path.join(self.tmp.name, "t.toml")
        with open(self.targets, "w") as handle:
            handle.write('[[target]]\nname="frame"\nkind="cargo-fuzz"\ndir="%s"\nfuzz_target="parse"\nbudget_minutes=1\n' % self.crate)
        self.rep = os.path.join(self.tmp.name, "rep")

    def run_main(self, crash=None):
        environ = {"HAMS_FUZZ_CARGO": self.cargo}
        if crash is not None:
            environ["FAKE_CRASH"] = crash
        with mock.patch.dict(os.environ, environ):
            return fuzz_run.main([self.targets], load=FakeLoad)

    def test_new_crash_is_minimized_stubbed_and_not_stubbed_twice(self):
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(common.read_json(os.path.join(self.rep, "fuzz", "frame.json"))["status"], "clean")
        self.assertEqual(self.run_main(crash="boom"), 1)
        document = common.read_json(os.path.join(self.rep, "fuzz", "frame.json"))
        self.assertEqual(document["status"], "crashes")
        record = document["new_crashes"][0]
        self.assertEqual(open(record["minimized"]).read(), "M")
        stub = open(record["stub"]).read()
        for needle in ("status: open", "repo: hams_com", "panicked at src/frame.rs:12:5", "cargo fuzz run parse", record["minimized"]):
            self.assertIn(needle, stub)
        self.assertEqual(self.run_main(crash="boom"), 0)  # the same crash again: seen, no second stub
        self.assertEqual(len(os.listdir(os.path.join(self.rep, "findings"))), 1)

    def test_libfuzzer_kind_builds_runs_minimizes_and_stubs_a_crash_once(self):
        crate = os.path.join(self.tmp.name, "job", "hams_open", "daemons", "radiod", "fuzz")
        os.makedirs(crate)
        open(os.path.join(crate, "Cargo.toml"), "w").close()
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "cargo"), "w") as handle:
            handle.write(FAKE_LIBFUZZER_CARGO.replace("@SH@", SHEBANG))
        os.chmod(os.path.join(bindir, "cargo"), 0o755)
        with open(self.targets, "w") as handle:
            handle.write('[[target]]\nname="tlv"\nkind="libfuzzer"\ndir="%s"\nbin="tlv_parse"\nbudget_minutes=1\n' % crate)
        path = bindir + os.pathsep + os.environ["PATH"]
        with mock.patch.dict(os.environ, {"PATH": path}):
            self.assertEqual(fuzz_run.main([self.targets], load=FakeLoad), 0)
            self.assertEqual(common.read_json(os.path.join(self.rep, "fuzz", "tlv.json"))["status"], "clean")
            with mock.patch.dict(os.environ, {"FAKE_CRASH": "boom"}):
                self.assertEqual(fuzz_run.main([self.targets], load=FakeLoad), 1)
                record = common.read_json(os.path.join(self.rep, "fuzz", "tlv.json"))["new_crashes"][0]
                self.assertEqual(open(record["minimized"]).read(), "M")
                stub = open(record["stub"]).read()
                for needle in ("panicked at src/tlv.rs:9:1", "RUSTC_BOOTSTRAP=1", "--bin tlv_parse", "repo: hams_open", record["minimized"]):
                    self.assertIn(needle, stub)
                self.assertEqual(fuzz_run.main([self.targets], load=FakeLoad), 0)  # same crash again: no second stub
        self.assertEqual(len(os.listdir(os.path.join(self.rep, "findings"))), 1)
        self.assertEqual(os.listdir(crate), ["Cargo.toml"])  # the checkout was not written to

    def test_libfuzzer_without_cargo_toml_is_an_error(self):
        with open(self.targets, "w") as handle:
            handle.write('[[target]]\nname="x"\nkind="libfuzzer"\ndir="%s"\nbin="b"\n' % self.tmp.name)
        self.assertEqual(fuzz_run.main([self.targets], load=FakeLoad), 1)

    def test_live_main_checkout_is_refused(self):
        with mock.patch.object(fuzz_run, "is_live_checkout", return_value=True), mock.patch.dict(os.environ, {"HAMS_FUZZ_CARGO": self.cargo}):
            self.assertEqual(fuzz_run.main([self.targets], load=FakeLoad), 1)
        self.assertIn("live main checkout", common.read_json(os.path.join(self.rep, "fuzz", "frame.json"))["detail"])

    def test_missing_cargo_fuzz_is_unavailable_not_a_failure(self):
        with mock.patch.dict(os.environ, {"HAMS_FUZZ_CARGO": "/nonexistent/cargo"}):
            self.assertEqual(fuzz_run.main([self.targets], load=FakeLoad), 0)
        self.assertEqual(common.read_json(os.path.join(self.rep, "fuzz", "frame.json"))["status"], "unavailable")

    def test_hypothesis_counterexample_becomes_a_stub_once(self):
        root = os.path.join(self.tmp.name, "tree")
        os.makedirs(root)
        with open(os.path.join(root, "test_prop.py"), "w") as handle:
            handle.write("from hypothesis import given, settings, strategies as st\n\n@settings(max_examples=3)\n@given(st.integers(min_value=0, max_value=1000))\n"
                         "def test_small(n):\n    assert n < 500\n")
        with open(self.targets, "w") as handle:
            handle.write('[[target]]\nname="prop"\nkind="hypothesis"\nroot="%s"\ntest="test_prop.py"\nexamples=2000\nbudget_minutes=2\n' % root)
        self.assertEqual(fuzz_run.main([self.targets], load=FakeLoad), 1)
        document = common.read_json(os.path.join(self.rep, "fuzz", "prop.json"))
        self.assertEqual(document["status"], "crashes")
        self.assertIn("n=500", open(document["new_crashes"][0]["minimized"]).read())
        self.assertEqual(fuzz_run.main([self.targets], load=FakeLoad), 0)
        self.assertEqual(common.read_json(os.path.join(self.rep, "fuzz", "prop.json"))["status"], "known")
        self.assertEqual(len(os.listdir(os.path.join(self.rep, "findings"))), 1)
        self.assertEqual(os.listdir(root), ["test_prop.py"])  # nothing written into the checkout


class CargoFuzzAvailabilityTests(unittest.TestCase):
    def test_a_missing_binary_is_not_available(self):
        with mock.patch.dict(os.environ, {"HAMS_FUZZ_CARGO": "/nonexistent/cargo"}):
            self.assertFalse(fuzz_run.cargo_fuzz_available({}))


if __name__ == "__main__":
    unittest.main()
