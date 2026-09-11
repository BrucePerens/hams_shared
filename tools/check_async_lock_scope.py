#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Async lock-guard-held-across-.await gate (Rust daemons only).

Born 2026-09-11 from a real bug found by adversarial review in `hams_relay_bridge`'s
`telemetry_loop`: a `tokio::sync::RwLock` read guard was bound at the top of the loop body and
never explicitly dropped, so it remained held across a slow `client.post(...).send().await` --
Rust's lexical (not NLL-shortened) drop-scope rules mean a value without an explicit early drop
lives until its enclosing block's closing brace, regardless of the borrow checker's own "last
use" analysis. Since `tokio::sync::RwLock` is write-preferring, one held-too-long read blocks
every subsequent writer AND every subsequent reader queued behind that writer -- a daemon-wide
stall, not just a slow path. The exact same shape (a write lock held across slow STUN I/O in
`webrtc_offer`) was found and fixed earlier the same night in `hams_local_relay/multiplexer.rs`.

`cargo clippy`'s own `await_holding_lock` lint (already wired in via check_cargo_clippy.py) does
NOT catch this: it only fires for non-async-aware guards (`std::sync::Mutex`/`RwLock`), where
holding one across an await point can deadlock a single-threaded executor outright. Tokio's own
`RwLock`/`Mutex` guards are `Send` and *designed* to be held across awaits sometimes (that's the
whole reason `tokio::sync` locks exist instead of `std::sync` ones in async code) -- so clippy
deliberately does not flag them, and nothing else in this toolchain does either. This gate fills
that specific gap for the one sub-case that's almost always a bug: a guard bound with no
following use of its own data, sitting alive across a *slow, external* `.await` (a network call)
rather than a fast, purely-internal one.

This is a heuristic textual scan, not a real dataflow analysis (no MIR, no borrow-checker state)
-- the same tradeoff check_burn_list.py's own GENERAL_ERROR_RULES already make throughout this
codebase. It tracks brace depth per file to approximate block scope, watches for a
`let ... = <expr>.(read|write|lock)().await;` binding, and flags any `.await` that occurs later
in the same enclosing block (i.e. before brace depth drops below the binding's own depth) with no
intervening `drop(<name>)` for that exact variable. False positives are possible (a `.await`
inside a string/comment the scanner doesn't special-case, a re-used variable name shadowing an
already-dropped guard) -- suppress a specific line with a same-line or immediately-preceding
`// lock-scope-ignore: <reason>` comment rather than disabling the whole file.
"""

import os
import re
import sys

IGNORE_DIR_NAMES = {
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "target",
    ".git",
    "worktrees",
}

_BINDING_RE = re.compile(
    r"^\s*let\s+(?:mut\s+)?(\w+)\s*=.*?\.(?:read|write|lock)\(\)\s*\.await\s*;\s*(?://.*)?$"
)
_DROP_RE_TEMPLATE = r"\bdrop\(\s*{name}\s*\)"
_IGNORE_RE = re.compile(r"lock-scope-ignore\s*:\s*\S")
# A `tokio::sync::RwLock`/`Mutex` guard from `.read()`/`.write()`/`.lock()` (not the distinct
# `.read_owned()`/`.write_owned()`/`.lock_owned()` API) borrows from the lock and is therefore
# not `'static` -- it cannot legally be moved into a `tokio::spawn(async move { ... })` closure,
# which requires everything it captures to be `'static`. So any `.await` lexically inside such a
# spawned block can never actually be holding a guard bound in the enclosing (non-spawned) scope,
# even though the spawn's own block is textually nested inside it. Real false positive this
# exempted: hams_local_relay/multiplexer.rs reads `st.digital_tx`, clones the owned `Sender`
# out of it, and only the clone is moved into `tokio::spawn(async move { ... })` -- the spawned
# task's own `.await`s run later, in a separate task, after `st` (the borrow) has long since
# gone out of scope; the checker's own brace-depth tracking can't tell that apart from a real
# same-scope await without this explicit boundary.
_SPAWN_BOUNDARY_RE = re.compile(r"(?:tokio::spawn|task::spawn)\s*\(\s*async\s+move\s*\{")


def find_rust_files(repo_root):
    found = []
    for root, dirs, filenames in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES and not d.startswith(".")]
        for fname in filenames:
            if fname.endswith(".rs"):
                found.append(os.path.join(root, fname))
    return sorted(found)


def check_file(path):
    """Returns a list of (line_num, message) findings for one .rs file."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    findings = []
    depth = 0
    # Each entry: {"name": str, "depth": int, "bind_line": int}
    tracked = []
    # Depths at which a `tokio::spawn(async move { ... })` block is currently open -- see
    # _SPAWN_BOUNDARY_RE's own comment for why anything at or below one of these depths is
    # unreachable from inside the spawned block.
    spawn_barriers = []

    for i, raw_line in enumerate(lines):
        line_num = i + 1
        line = raw_line

        # A suppression comment on this exact line (or the line immediately
        # above, for a binding annotated on its own preceding line) exempts
        # this line from both starting new tracking and from raising a
        # violation against already-tracked guards.
        suppressed = bool(_IGNORE_RE.search(line)) or (
            i > 0 and bool(_IGNORE_RE.search(lines[i - 1]))
        )

        open_count = line.count("{")
        close_count = line.count("}")

        # A line whose first non-whitespace character is `}` closes its
        # enclosing block BEFORE anything else on that same line runs --
        # essential for a `} else {`/`} catch {`-shaped line (a real,
        # common rustfmt output in this codebase): the `if`-block's own
        # guards must be pruned by the leading `}` before the sibling
        # `else`-block's fresh scope opens, even though the line's NET
        # brace-depth change is zero. Any other line (an inline
        # self-contained block like `if x.is_empty() { continue; }`, or a
        # plain block-opener/-closer) opens before it closes in real
        # source order, so pruning must NOT run early there -- real false
        # positive this distinction fixed: treating every net-zero-brace
        # line the same as a `} else {` line pruned `st` the moment a
        # single-line `if cond { continue; }` was reached, even though
        # that inline block never actually enclosed `st`'s own binding.
        starts_with_close = line.lstrip().startswith("}")
        if starts_with_close:
            depth_for_this_line = depth - close_count
            tracked = [t for t in tracked if t["depth"] <= depth_for_this_line]
        else:
            depth_for_this_line = depth

        if not suppressed:
            explicit_drop_names = set(re.findall(r"\bdrop\(\s*(\w+)\s*\)", line))
            tracked = [t for t in tracked if t["name"] not in explicit_drop_names]

            m = _BINDING_RE.match(line)
            # An underscore-prefixed name (`_guard`, `_session_write_guard`) is Rust's own
            # idiom for "this binding exists only to be held, not to access data through it" --
            # confirmed as the real, intentional pattern behind every such name this checker
            # found in hams_local_relay (a test's own deliberate whole-scope lock-contention
            # simulation in multiplexer.rs, and a cross-test serialization mutex in
            # winlink.rs), not an accidental forgot-to-drop bug. Real values worth flagging are
            # always named for what they still need to read/write through (`st`, `session`).
            if m and not m.group(1).startswith("_"):
                tracked.append({"name": m.group(1), "depth": depth_for_this_line + open_count, "bind_line": line_num})
            elif ".await" in line:
                # A guard bound OUTSIDE a currently-open spawn boundary can never actually be
                # held at this .await -- see _SPAWN_BOUNDARY_RE's own comment.
                reachable = [
                    t for t in tracked
                    if not any(barrier > t["depth"] for barrier in spawn_barriers)
                ]
                for t in reachable:
                    findings.append((
                        line_num,
                        f"Lock guard `{t['name']}` (bound line {t['bind_line']}) is still in "
                        f"scope at this `.await` with no intervening `drop({t['name']})` -- "
                        f"tokio::sync locks are write-preferring, so a guard held across a slow "
                        f"external .await can stall every other reader/writer in the daemon. "
                        f"Scope the guard's use to a block that ends before this call, or add "
                        f"an explicit `drop({t['name']})` first. If this is a deliberate, "
                        f"reviewed exception, add `// lock-scope-ignore: <reason>` on this line.",
                    ))

        if starts_with_close:
            depth = depth_for_this_line + open_count
        else:
            depth = depth + open_count - close_count

        if not suppressed and _SPAWN_BOUNDARY_RE.search(line):
            spawn_barriers.append(depth)
        spawn_barriers = [b for b in spawn_barriers if b <= depth]

    return findings


def main():
    if len(sys.argv) < 2:
        print("Usage: check_async_lock_scope.py <repo_root>")
        sys.exit(1)

    repo_root = os.path.abspath(sys.argv[1])
    if os.path.basename(repo_root) == "hams_shared":
        repo_root = os.path.dirname(repo_root)

    any_found = False
    for path in find_rust_files(repo_root):
        for line_num, msg in check_file(path):
            any_found = True
            rel = os.path.relpath(path, repo_root)
            print(f"❌ {rel}:{line_num}: {msg}")

    if any_found:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
