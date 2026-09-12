#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_async_lock_scope.py, exercised against real temp-directory .rs fixtures
(no cargo/rustc involvement -- this is a textual heuristic scan, not a real build).
"""

import os
import shutil
import sys
import tempfile
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_async_lock_scope as chk  # noqa: E402


def _write_rs(tmp, content):
    path = os.path.join(tmp, "src", "lib.rs")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class CheckFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_read_guard_held_across_a_later_await_is_flagged(self):
        # The real telemetry_loop shape this checker was born from: a guard bound at the top
        # of a block, with no drop, sitting alive across a later, unrelated .await.
        path = _write_rs(
            self.tmp,
            """
            async fn f() {
                loop {
                    let st = state.read().await;
                    let x = st.value;
                    client.post().send().await;
                }
            }
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)
        self.assertIn("st", findings[0][1])

    def test_a_guard_dropped_before_the_later_await_is_not_flagged(self):
        path = _write_rs(
            self.tmp,
            """
            async fn f() {
                loop {
                    let st = state.read().await;
                    let x = st.value;
                    drop(st);
                    client.post().send().await;
                }
            }
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_a_guard_whose_own_block_closes_before_the_later_await_is_not_flagged(self):
        path = _write_rs(
            self.tmp,
            """
            async fn f() {
                if cond {
                    let st = state.read().await;
                    use_it(st.value);
                }
                other_thing().await;
            }
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_an_if_else_sibling_branch_does_not_inherit_the_ifs_own_guard(self):
        # Real false positive found and fixed 2026-09-11: a `} else {`-shaped line's net
        # brace-depth change is zero, which a naive scanner can mistake for "still the same
        # block" -- the guard bound inside the `if` must not be considered live in `else`.
        path = _write_rs(
            self.tmp,
            """
            async fn f() {
                if cond {
                    let st = state.write().await;
                    st.insert(x);
                } else {
                    fallback().await;
                }
            }
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_an_inline_self_contained_block_does_not_falsely_prune_an_outer_guard(self):
        # Real false positive found and fixed 2026-09-11: a single-line `if cond { stmt }`
        # opens and closes on the same line (net zero), which must NOT be treated the same as
        # a `} else {` line -- it never actually enclosed the outer guard's own binding.
        path = _write_rs(
            self.tmp,
            """
            async fn f() {
                let st = state.read().await;
                if items.is_empty() { continue; }
                client.post().send().await;
            }
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)

    def test_a_guard_captured_only_by_clone_into_a_spawned_task_is_not_flagged(self):
        # Real false positive found and fixed 2026-09-11: only an owned clone crosses into
        # `tokio::spawn(async move { ... })` (a borrowed guard can't -- it isn't 'static), so
        # the spawned task's own .await never actually holds the outer guard.
        path = _write_rs(
            self.tmp,
            """
            async fn f() {
                let st = state.read().await;
                let sender = st.sender.clone();
                tokio::spawn(async move {
                    sender.send(x).await;
                });
            }
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_an_underscore_prefixed_name_is_never_flagged(self):
        # Rust's own idiom for "deliberately held for the whole scope" -- confirmed as the
        # real, intentional pattern behind every such name found in this codebase's own test
        # suites (a lock-contention simulation, a cross-test serialization mutex).
        path = _write_rs(
            self.tmp,
            """
            async fn f() {
                let _guard = LOCK.lock().await;
                do_the_serialized_thing().await;
            }
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_a_suppression_comment_on_the_binding_line_exempts_it(self):
        path = _write_rs(
            self.tmp,
            """
            async fn f() {
                let st = state.read().await; // lock-scope-ignore: reviewed, deliberate
                slow_call().await;
            }
            """,
        )
        self.assertEqual(chk.check_file(path), [])

    def test_multiple_awaits_after_one_binding_each_produce_their_own_finding(self):
        path = _write_rs(
            self.tmp,
            """
            async fn f() {
                let st = state.write().await;
                one().await;
                two().await;
            }
            """,
        )
        self.assertEqual(len(chk.check_file(path)), 2)

    @given(num_awaits=st.integers(min_value=0, max_value=12))
    @settings(max_examples=50)
    def test_a_held_guard_produces_exactly_one_finding_per_trailing_await(self, num_awaits):
        # Generalizes test_multiple_awaits_after_one_binding_each_produce_their_own_finding
        # (fixed at 2) across an arbitrary count, including the boundary case of zero trailing
        # awaits (no finding at all -- a bound-but-never-awaited-past guard is not a bug this
        # checker's own contract covers).
        body = "\n".join(f"                call_{i}().await;" for i in range(num_awaits))
        path = _write_rs(
            self.tmp,
            f"""
            async fn f() {{
                let st = state.write().await;
{body}
            }}
            """,
        )
        findings = chk.check_file(path)
        self.assertEqual(
            len(findings),
            num_awaits,
            f"expected exactly {num_awaits} findings (one per trailing await) for a guard with "
            f"no drop and no scope exit, got {len(findings)}: {findings}",
        )

    @given(
        num_guards=st.integers(min_value=0, max_value=5),
        num_awaits=st.integers(min_value=0, max_value=5),
    )
    @settings(max_examples=50)
    def test_any_number_of_underscore_prefixed_guards_is_never_flagged(self, num_guards, num_awaits):
        # Generalizes test_an_underscore_prefixed_name_is_never_flagged across an arbitrary
        # number of deliberately-held guards and an arbitrary number of later awaits -- Rust's
        # own "held for the whole scope on purpose" idiom must never be flagged, no matter how
        # many such guards are stacked or how many awaits follow them.
        guard_lines = "\n".join(
            f"                let _guard_{i} = LOCK_{i}.lock().await;" for i in range(num_guards)
        )
        await_lines = "\n".join(f"                call_{i}().await;" for i in range(num_awaits))
        path = _write_rs(
            self.tmp,
            f"""
            async fn f() {{
{guard_lines}
{await_lines}
            }}
            """,
        )
        self.assertEqual(
            chk.check_file(path),
            [],
            f"expected zero findings for {num_guards} underscore-prefixed guards and "
            f"{num_awaits} trailing awaits",
        )

    @given(num_guards=st.integers(min_value=0, max_value=5))
    @settings(max_examples=50)
    def test_any_number_of_immediately_dropped_guards_is_never_flagged(self, num_guards):
        # Generalizes test_a_guard_dropped_before_the_later_await_is_not_flagged across an
        # arbitrary number of independently-named, independently-dropped guards, all sharing
        # one later await -- each guard's own explicit drop() must suppress it regardless of
        # how many siblings are also being tracked at the same time.
        bind_and_drop_lines = "\n".join(
            f"                let st_{i} = state_{i}.read().await;\n"
            f"                drop(st_{i});"
            for i in range(num_guards)
        )
        path = _write_rs(
            self.tmp,
            f"""
            async fn f() {{
{bind_and_drop_lines}
                slow_call().await;
            }}
            """,
        )
        self.assertEqual(
            chk.check_file(path),
            [],
            f"expected zero findings for {num_guards} guards each dropped before the trailing await",
        )


class FindRustFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_rs_files_and_skips_target_dir(self):
        _write_rs(self.tmp, "// real\n")
        built = os.path.join(self.tmp, "target", "debug", "build")
        os.makedirs(built, exist_ok=True)
        with open(os.path.join(built, "generated.rs"), "w") as f:
            f.write("// generated\n")
        found = chk.find_rust_files(self.tmp)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith(os.path.join("src", "lib.rs")))


if __name__ == "__main__":
    unittest.main()
