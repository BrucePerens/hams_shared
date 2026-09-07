// This software is distributed under the terms of the Affero General Public License (AGPL-3).
// SPDX-License-Identifier: AGPL-3.0-or-later
//! Real `syn`-based AST scan of Rust source files for
//! `check_rust_function_test_anchors.py` (ADR 0090 decision 2's Rust
//! sub-track) -- the same "a real function-boundary computation needs a
//! real parser, not a guess" reasoning `js_function_scan.cjs`'s own doc
//! comment already established for JS, applied to Rust via `syn` instead
//! of a regex/text-scan approximation.
//!
//! Reads a JSON array of absolute file paths from stdin, writes a JSON
//! array of `{file, functions: [{name, start, end, is_trivial}]}` to
//! stdout -- one process invocation for the whole batch, matching
//! `js_function_scan.cjs`'s own batching (confirmed there to handle
//! hundreds of files in well under a second; the same reasoning applies
//! here, and per-process `cargo` startup cost makes batching even more
//! worth it for Rust than for Node). `is_trivial` is Stage 1's own
//! size/shape anchor-exclusion rule (see `FunctionEntry`'s own doc
//! comment) -- `check_rust_function_test_anchors.py` uses it to skip
//! bare, branchless, single-expression functions from the anchor
//! requirement entirely, not just report them as gaps a human then has
//! to triage by hand.
//!
//! Scope, matching `check_function_test_anchors.py`'s Python scope and
//! `check_js_function_test_anchors.py`'s JS scope as closely as Rust's own
//! real conventions allow:
//! - Free functions (`fn` items, at module level or inside a non-test
//!   `mod`), impl-block methods (qualified as `TypeName::method`), and
//!   trait *default* method bodies (qualified as `TraitName::method`) --
//!   a trait method with no body is a signature only, nothing to test.
//! - `#[test]`/`#[tokio::test]`/etc.-attributed functions (any attribute
//!   whose path's last segment is literally `test`) are excluded -- these
//!   ARE the tests, not code under test, the same reasoning `test_*.py`
//!   files get on the Python side.
//! - A `mod` carrying `#[cfg(test)]` (this codebase's own real, universal
//!   convention for inline unit tests, confirmed directly against this
//!   session's own AMBE/Codec2 work: every test module in every file
//!   touched this session uses exactly this shape) is skipped entirely,
//!   not recursed into -- the Rust analogue of excluding `test_*.py`
//!   files/`*.test.js` files, applied at the module level since Rust's
//!   own convention keeps tests inline rather than in separate files.
//! - A closure or `fn` nested inside another function's own body is
//!   deliberately NOT walked -- not an independently testable unit, the
//!   same reasoning both other scanners already give for excluding nested
//!   functions/closures.

use std::io::Read;

use serde::Serialize;
use syn::spanned::Spanned;

#[derive(Serialize)]
struct FunctionEntry {
    name: String,
    start: usize,
    end: usize,
    /// Bruce's own direct answer on Stage 1's real anchor-scope question
    /// (`ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md`, 2026-09-07): "Exclude
    /// small helpers by a size/shape rule" -- skip bare, branchless,
    /// single-expression functions from the anchor requirement; anything
    /// with real logic still needs one. `is_trivial` implements that
    /// rule precisely: true when the body contains no control-flow
    /// expression anywhere (`if`/`match`/`for`/`while`/`loop`, checked
    /// recursively, not just at the top level) AND has at most two real
    /// statements -- an `assert!`/`debug_assert!`/`assert_eq!`/
    /// `assert_ne!` macro-invocation statement doesn't count toward that
    /// limit, since it's a contract check compiled out in release builds,
    /// not real logic (this is exactly why `rshift_round_i128` -- one of
    /// Bruce's own cited examples, a `let` binding, a `debug_assert!`,
    /// and a tail expression -- reads as trivial under this rule despite
    /// having three source-level statements).
    is_trivial: bool,
}

/// Recursively checks whether `expr` contains a control-flow construct
/// anywhere in its own sub-expressions -- `if`/`match`/`for`/`while`/
/// `loop`, at any nesting depth, not just as the expression's own
/// top-level shape (`a + if x { 1 } else { 2 }` must count as having
/// control flow even though the outer expression is a plain `Binary`).
fn expr_has_control_flow(expr: &syn::Expr) -> bool {
    use syn::visit::Visit;
    struct Finder(bool);
    impl<'ast> Visit<'ast> for Finder {
        fn visit_expr(&mut self, e: &'ast syn::Expr) {
            match e {
                syn::Expr::If(_)
                | syn::Expr::Match(_)
                | syn::Expr::ForLoop(_)
                | syn::Expr::While(_)
                | syn::Expr::Loop(_) => {
                    self.0 = true;
                }
                _ => {}
            }
            syn::visit::visit_expr(self, e);
        }
    }
    let mut finder = Finder(false);
    finder.visit_expr(expr);
    finder.0
}

/// True when `stmt` is a bare `assert!`/`debug_assert!`/`assert_eq!`/
/// `assert_ne!` macro-invocation statement -- excluded from the trivial
/// rule's own statement count (see `FunctionEntry::is_trivial`'s own doc
/// comment for why).
fn is_assert_stmt(stmt: &syn::Stmt) -> bool {
    let syn::Stmt::Macro(m) = stmt else {
        return false;
    };
    m.mac.path.segments.last().is_some_and(|s| {
        matches!(
            s.ident.to_string().as_str(),
            "assert" | "debug_assert" | "assert_eq" | "assert_ne"
        )
    })
}

fn block_is_trivial(block: &syn::Block) -> bool {
    let real_stmts: Vec<&syn::Stmt> = block.stmts.iter().filter(|s| !is_assert_stmt(s)).collect();
    if real_stmts.len() > 2 {
        return false;
    }
    for stmt in &real_stmts {
        let has_cf = match stmt {
            syn::Stmt::Expr(e, _) => expr_has_control_flow(e),
            syn::Stmt::Local(local) => local
                .init
                .as_ref()
                .is_some_and(|init| expr_has_control_flow(&init.expr)),
            syn::Stmt::Macro(_) | syn::Stmt::Item(_) => false,
        };
        if has_cf {
            return false;
        }
    }
    true
}

#[derive(Serialize)]
struct FileResult {
    file: String,
    functions: Vec<FunctionEntry>,
}

/// Any attribute whose path's last segment is literally `test` -- matches
/// `#[test]`, `#[tokio::test]`, `#[async_std::test]`, and similar, the
/// same permissive-superset convention this codebase's other defensive
/// checks already use rather than hardcoding one exact macro path.
fn has_test_attr(attrs: &[syn::Attribute]) -> bool {
    attrs
        .iter()
        .any(|a| a.path().segments.last().is_some_and(|s| s.ident == "test"))
}

/// `#[cfg(test)]`, or any `#[cfg(...)]` whose argument tokens mention
/// `test` at all (`#[cfg(any(test, feature = "..."))]` and similar real
/// variants) -- a permissive superset, matching `has_test_attr` above.
fn has_cfg_test(attrs: &[syn::Attribute]) -> bool {
    attrs.iter().any(|a| {
        if !a.path().is_ident("cfg") {
            return false;
        }
        let Ok(tokens) = a.parse_args::<proc_macro2::TokenStream>() else {
            return false;
        };
        tokens
            .into_iter()
            .any(|tok| matches!(tok, proc_macro2::TokenTree::Ident(id) if id == "test"))
    })
}

/// Same backward-over-comment-lines lookback `_extend_span_backward_over_
/// comments` (JS) and `_function_span` (Python) both already implement --
/// a plain `// [@ANCHOR: ...]` line comment is trivia to `syn`'s own
/// parser, never part of the token stream, so it can't be found via any
/// attribute span; it has to be found by walking the raw source lines
/// backward from the item's own first real token, stopping at the first
/// non-comment (deliberately not crossing a blank line, for the same
/// reason both other scanners don't: crossing one risks absorbing the
/// PREVIOUS item's own trailing anchor comment as if it were this one's).
fn extend_backward_over_comments(start: usize, lines: &[&str]) -> usize {
    let mut start = start;
    while start > 1 {
        let prev = lines[start - 2].trim();
        if prev.starts_with("//") {
            start -= 1;
        } else {
            break;
        }
    }
    start
}

fn attrs_or_span_start_line(attrs: &[syn::Attribute], fallback: proc_macro2::Span) -> usize {
    attrs
        .first()
        .map(|a| a.span().start().line)
        .unwrap_or_else(|| fallback.start().line)
}

fn type_name(ty: &syn::Type) -> String {
    if let syn::Type::Path(p) = ty {
        if let Some(seg) = p.path.segments.last() {
            return seg.ident.to_string();
        }
    }
    quote::quote!(#ty).to_string().replace(' ', "")
}

fn qualify(prefix: &str, name: &str) -> String {
    if prefix.is_empty() {
        name.to_string()
    } else {
        format!("{prefix}::{name}")
    }
}

fn walk_items(items: &[syn::Item], prefix: &str, lines: &[&str], out: &mut Vec<FunctionEntry>) {
    for item in items {
        match item {
            syn::Item::Fn(f) => {
                if has_test_attr(&f.attrs) || has_cfg_test(&f.attrs) {
                    continue;
                }
                let start = attrs_or_span_start_line(&f.attrs, f.sig.fn_token.span());
                let start = extend_backward_over_comments(start, lines);
                let end = f.block.brace_token.span.close().start().line;
                out.push(FunctionEntry {
                    name: qualify(prefix, &f.sig.ident.to_string()),
                    start,
                    end,
                    is_trivial: block_is_trivial(&f.block),
                });
            }
            syn::Item::Mod(m) => {
                if has_cfg_test(&m.attrs) {
                    continue;
                }
                if let Some((_, content)) = &m.content {
                    let new_prefix = qualify(prefix, &m.ident.to_string());
                    walk_items(content, &new_prefix, lines, out);
                }
            }
            syn::Item::Impl(imp) => {
                let new_prefix = qualify(prefix, &type_name(&imp.self_ty));
                for ii in &imp.items {
                    if let syn::ImplItem::Fn(m) = ii {
                        if has_test_attr(&m.attrs) || has_cfg_test(&m.attrs) {
                            continue;
                        }
                        let start = attrs_or_span_start_line(&m.attrs, m.sig.fn_token.span());
                        let start = extend_backward_over_comments(start, lines);
                        let end = m.block.brace_token.span.close().start().line;
                        out.push(FunctionEntry {
                            name: qualify(&new_prefix, &m.sig.ident.to_string()),
                            start,
                            end,
                            is_trivial: block_is_trivial(&m.block),
                        });
                    }
                }
            }
            syn::Item::Trait(tr) => {
                let new_prefix = qualify(prefix, &tr.ident.to_string());
                for ti in &tr.items {
                    if let syn::TraitItem::Fn(m) = ti {
                        let Some(block) = &m.default else { continue };
                        if has_test_attr(&m.attrs) || has_cfg_test(&m.attrs) {
                            continue;
                        }
                        let start = attrs_or_span_start_line(&m.attrs, m.sig.fn_token.span());
                        let start = extend_backward_over_comments(start, lines);
                        let end = block.brace_token.span.close().start().line;
                        out.push(FunctionEntry {
                            name: qualify(&new_prefix, &m.sig.ident.to_string()),
                            start,
                            end,
                            is_trivial: block_is_trivial(block),
                        });
                    }
                }
            }
            _ => {}
        }
    }
}

fn scan_file(path: &str) -> Option<FileResult> {
    let content = std::fs::read_to_string(path).ok()?;
    let file = syn::parse_file(&content).ok()?;
    let lines: Vec<&str> = content.lines().collect();
    let mut functions = Vec::new();
    walk_items(&file.items, "", &lines, &mut functions);
    Some(FileResult {
        file: path.to_string(),
        functions,
    })
}

fn main() {
    let mut input = String::new();
    if std::io::stdin().read_to_string(&mut input).is_err() {
        return;
    }
    let paths: Vec<String> = match serde_json::from_str(&input) {
        Ok(p) => p,
        Err(_) => return,
    };
    let results: Vec<FileResult> = paths.iter().filter_map(|p| scan_file(p)).collect();
    if let Ok(out) = serde_json::to_string(&results) {
        println!("{out}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scan_source(src: &str) -> Vec<FunctionEntry> {
        let file = syn::parse_file(src).expect("test fixture must parse");
        let lines: Vec<&str> = src.lines().collect();
        let mut out = Vec::new();
        walk_items(&file.items, "", &lines, &mut out);
        out
    }

    #[test]
    fn finds_a_plain_module_level_function() {
        let entries = scan_source("fn foo() {\n    1;\n}\n");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, "foo");
        assert_eq!(entries[0].start, 1);
        assert_eq!(entries[0].end, 3);
    }

    #[test]
    fn qualifies_impl_methods_with_the_type_name() {
        let entries =
            scan_source("struct S;\nimpl S {\n    fn bar(&self) {\n        1;\n    }\n}\n");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, "S::bar");
    }

    #[test]
    fn qualifies_trait_default_methods_but_skips_bodyless_ones() {
        let entries = scan_source(
            "trait T {\n    fn required(&self);\n    fn provided(&self) {\n        1;\n    }\n}\n",
        );
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, "T::provided");
    }

    #[test]
    fn skips_functions_inside_a_cfg_test_module() {
        let entries = scan_source(
            "fn real() {\n    1;\n}\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn it_works() {\n        assert!(true);\n    }\n}\n",
        );
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, "real");
    }

    #[test]
    fn skips_test_attributed_functions_even_outside_a_cfg_test_module() {
        let entries = scan_source("#[test]\nfn it_works() {\n    assert!(true);\n}\n");
        assert!(entries.is_empty());
    }

    /// Regression test for a real bug this session's own AI-agent code
    /// review caught: a bare `#[cfg(test)] fn helper() {...}` (a test-only
    /// helper NOT wrapped in a `#[cfg(test)] mod` -- a real, existing
    /// pattern in this codebase, e.g. `fixed_fft.rs`'s own `f32_to_q23`/
    /// `build_twiddles_q23`) was only excluded when the *module* carried
    /// `#[cfg(test)]`, not when the function attribute itself did --
    /// falsely flagging real test-only helpers as needing a production
    /// anchor.
    #[test]
    fn skips_a_bare_cfg_test_function_not_wrapped_in_a_cfg_test_module() {
        let entries = scan_source("#[cfg(test)]\nfn helper() {\n    1;\n}\n");
        assert!(entries.is_empty());
    }

    #[test]
    fn does_not_descend_into_a_nested_inner_function() {
        let entries =
            scan_source("fn outer() {\n    fn inner() {\n        1;\n    }\n    inner();\n}\n");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, "outer");
    }

    #[test]
    fn recurses_into_inline_non_test_modules_with_a_qualified_name() {
        let entries = scan_source("mod util {\n    fn helper() {\n        1;\n    }\n}\n");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, "util::helper");
    }

    #[test]
    fn extends_the_start_line_backward_over_a_leading_anchor_comment() {
        let entries = scan_source("// [@ANCHOR: foo]\nfn foo() {\n    1;\n}\n");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].start, 1);
    }

    #[test]
    fn does_not_cross_a_blank_line_when_extending_backward() {
        let entries = scan_source("// belongs to nothing\n\nfn foo() {\n    1;\n}\n");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].start, 3);
    }

    /// Real examples Bruce's own answer named as trivial (`ANCHOR_COVERAGE_AND_REMEDIATION_
    /// PLAN.md`, 2026-09-07): `Complex::add`, `rshift_round_i128`, `f0_to_wo`.
    #[test]
    fn a_bare_tail_expression_constructor_call_is_trivial() {
        let entries = scan_source(
            "struct S;\nimpl S {\n    fn add(self, other: Self) -> Self {\n        Self::new(self.re + other.re, self.im + other.im)\n    }\n}\n",
        );
        assert_eq!(entries.len(), 1);
        assert!(entries[0].is_trivial);
    }

    #[test]
    fn a_single_arithmetic_tail_expression_is_trivial() {
        let entries = scan_source(
            "fn f0_to_wo(f0: f32) -> f32 {\n    std::f32::consts::TAU * f0 / SAMPLE_RATE as f32\n}\n",
        );
        assert_eq!(entries.len(), 1);
        assert!(entries[0].is_trivial);
    }

    #[test]
    fn a_let_binding_plus_debug_assert_plus_tail_expression_is_trivial() {
        // rshift_round_i128's own real shape: a let binding, a debug_assert! (excluded from
        // the statement count), and a tail expression -- 3 source-level statements, but only
        // 2 real ones under this rule.
        let entries = scan_source(
            "fn rshift_round_i128(x: i128, n: u32) -> i64 {\n    let shifted = (x + (1i128 << (n - 1))) >> n;\n    debug_assert!(shifted >= i64::MIN as i128, \"doesn't fit\");\n    shifted as i64\n}\n",
        );
        assert_eq!(entries.len(), 1);
        assert!(entries[0].is_trivial);
    }

    #[test]
    fn a_function_containing_an_if_expression_is_not_trivial() {
        let entries = scan_source(
            "fn clamp(x: f32) -> f32 {\n    if x > 1.0 {\n        1.0\n    } else {\n        x\n    }\n}\n",
        );
        assert_eq!(entries.len(), 1);
        assert!(!entries[0].is_trivial);
    }

    #[test]
    fn an_if_nested_inside_an_arithmetic_expression_is_still_not_trivial() {
        // Real property the recursive check exists for: a + if x { 1 } else { 2 } is a
        // top-level Binary expression, not an If expression, but still contains control flow.
        let entries =
            scan_source("fn f(x: bool, a: i32) -> i32 {\n    a + if x { 1 } else { 2 }\n}\n");
        assert_eq!(entries.len(), 1);
        assert!(!entries[0].is_trivial);
    }

    #[test]
    fn a_function_containing_a_for_loop_is_not_trivial() {
        let entries = scan_source(
            "fn sum(xs: &[i32]) -> i32 {\n    let mut total = 0;\n    for x in xs {\n        total += x;\n    }\n    total\n}\n",
        );
        assert_eq!(entries.len(), 1);
        assert!(!entries[0].is_trivial);
    }

    #[test]
    fn a_function_with_more_than_two_real_statements_is_not_trivial() {
        let entries = scan_source(
            "fn f() -> i32 {\n    let a = 1;\n    let b = 2;\n    let c = 3;\n    a + b + c\n}\n",
        );
        assert_eq!(entries.len(), 1);
        assert!(!entries[0].is_trivial);
    }

    #[test]
    fn a_control_flow_expression_inside_a_let_initializer_is_not_trivial() {
        let entries =
            scan_source("fn f(x: bool) -> i32 {\n    let y = if x { 1 } else { 2 };\n    y\n}\n");
        assert_eq!(entries.len(), 1);
        assert!(!entries[0].is_trivial);
    }

    #[test]
    fn a_match_expression_is_not_trivial() {
        let entries = scan_source(
            "fn f(x: Option<i32>) -> i32 {\n    match x {\n        Some(v) => v,\n        None => 0,\n    }\n}\n",
        );
        assert_eq!(entries.len(), 1);
        assert!(!entries[0].is_trivial);
    }
}
