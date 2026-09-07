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
//! array of `{file, functions: [{name, start, end}]}` to stdout -- one
//! process invocation for the whole batch, matching `js_function_scan.cjs`'s
//! own batching (confirmed there to handle hundreds of files in well under
//! a second; the same reasoning applies here, and per-process `cargo`
//! startup cost makes batching even more worth it for Rust than for Node).
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
                if has_test_attr(&f.attrs) {
                    continue;
                }
                let start = attrs_or_span_start_line(&f.attrs, f.sig.fn_token.span());
                let start = extend_backward_over_comments(start, lines);
                let end = f.block.brace_token.span.close().start().line;
                out.push(FunctionEntry {
                    name: qualify(prefix, &f.sig.ident.to_string()),
                    start,
                    end,
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
                        if has_test_attr(&m.attrs) {
                            continue;
                        }
                        let start = attrs_or_span_start_line(&m.attrs, m.sig.fn_token.span());
                        let start = extend_backward_over_comments(start, lines);
                        let end = m.block.brace_token.span.close().start().line;
                        out.push(FunctionEntry {
                            name: qualify(&new_prefix, &m.sig.ident.to_string()),
                            start,
                            end,
                        });
                    }
                }
            }
            syn::Item::Trait(tr) => {
                let new_prefix = qualify(prefix, &tr.ident.to_string());
                for ti in &tr.items {
                    if let syn::TraitItem::Fn(m) = ti {
                        let Some(block) = &m.default else { continue };
                        if has_test_attr(&m.attrs) {
                            continue;
                        }
                        let start = attrs_or_span_start_line(&m.attrs, m.sig.fn_token.span());
                        let start = extend_backward_over_comments(start, lines);
                        let end = block.brace_token.span.close().start().line;
                        out.push(FunctionEntry {
                            name: qualify(&new_prefix, &m.sig.ident.to_string()),
                            start,
                            end,
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
    Some(FileResult { file: path.to_string(), functions })
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
        let entries = scan_source("struct S;\nimpl S {\n    fn bar(&self) {\n        1;\n    }\n}\n");
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

    #[test]
    fn does_not_descend_into_a_nested_inner_function() {
        let entries = scan_source(
            "fn outer() {\n    fn inner() {\n        1;\n    }\n    inner();\n}\n",
        );
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
}
