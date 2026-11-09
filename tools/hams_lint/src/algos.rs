// SPDX-License-Identifier: AGPL-3.0-or-later
//! Checks (and rule exceptions) that a line pattern cannot express, implemented in Rust. Each names
//! the Python function it reproduces; the parity harness holds them to the same findings.

use std::sync::OnceLock;

use regex::{Captures, Regex};

use crate::rules::{Check, FileResult, Finding};
use crate::text::{self, py_ext, py_lstrip, py_strip};

const ALGORITHMS: &[&str] = &["shebang"];
const POST_FILTERS: &[&str] = &["usb_trait_impl"];

pub fn known(name: &str) -> bool {
    ALGORITHMS.contains(&name)
}

pub fn known_post_filter(name: &str) -> bool {
    POST_FILTERS.contains(&name)
}

pub fn run(check: &Check, rel: &str, abs_display: &str, bytes: &[u8]) -> FileResult {
    match check.spec.algorithm.as_str() {
        "shebang" => shebang(check, rel, abs_display, bytes),
        other => unreachable!("algorithm {other} passed the loader"),
    }
}

/// True to KEEP the finding.
pub fn post_filter(name: &str, caps: &Captures, content: &str) -> bool {
    match name {
        "usb_trait_impl" => !implements_usb_trait(caps.get(1).map_or("", |m| m.as_str()), content),
        other => unreachable!("post_filter {other} passed the loader"),
    }
}

fn heredoc_open() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r#"<<-?\s*(?:'([^']+)'|"([^"]+)"|\\?([A-Za-z_][A-Za-z0-9_]*))"#).expect("valid")
    })
}

/// `check_shebang.check_shebang`: a `#!` that does not start line 1 of a .py/.sh/extensionless file,
/// outside shell here-documents (their bodies are data: a script may write another script).
fn shebang(check: &Check, rel: &str, abs_display: &str, bytes: &[u8]) -> FileResult {
    let name = rel.rsplit('/').next().unwrap_or(rel);
    let is_py = py_ext(name) == ".py";
    let (lines, err) = text::strict_lines(bytes);
    let mut result = FileResult::default();
    let mut heredoc_end: Option<String> = None;
    for (i, line) in lines.iter().enumerate() {
        let number = i + 1;
        if let Some(end) = &heredoc_end {
            if py_strip(line) == end {
                heredoc_end = None;
            }
            continue;
        }
        if !is_py && !py_lstrip(line).starts_with('#') {
            if let Some(c) = heredoc_open().captures(line) {
                heredoc_end = (1..=3)
                    .find_map(|g| c.get(g))
                    .map(|m| m.as_str().to_owned());
                continue;
            }
        }
        if line.starts_with("#!") && number != 1 {
            result.findings.push(Finding {
                line: number,
                message: "Contains shebang `#!` on a line other than line 1".into(),
            });
        }
    }
    // check_shebang drops a UnicodeDecodeError silently (the findings before it stay).
    if err.is_some() && check.spec.on_error == "warn" {
        result.warning = check.warning(abs_display, &err.map(|e| e.message).unwrap_or_default());
    }
    result
}

fn usb_trait_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"((?:[ \t]*///[^\n]*\n|[ \t]*#\[[^\n]*\]\n)+)[ \t]*(?:pub(?:\([^)]*\))?\s+)?trait\s+(\w+)")
            .expect("valid")
    })
}

fn usb_word() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\bUSB\b").expect("valid"))
}

/// `check_fake_network_listener`: a fake that implements a trait documented as a USB device trait
/// stands in for hardware, not for the network.
fn implements_usb_trait(fake: &str, content: &str) -> bool {
    usb_trait_re()
        .captures_iter(content)
        .filter(|c| usb_word().is_match(c.get(1).map_or("", |m| m.as_str())))
        .filter_map(|c| c.get(2).map(|m| m.as_str().to_owned()))
        .any(|t| {
            let pattern = format!(
                r"\bimpl\s+{}\s+for\s+{}\b",
                regex::escape(&t),
                regex::escape(fake)
            );
            Regex::new(&pattern).is_ok_and(|r| r.is_match(content))
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn usb_documented_trait_exempts_its_fake() {
        let src = "/// A USB serial device.\npub trait Port {}\nstruct FakeSocket;\nimpl Port for FakeSocket {}\n";
        assert!(implements_usb_trait("FakeSocket", src));
        assert!(!implements_usb_trait("FakeStream", src));
        let not_usb = "/// A network port.\npub trait Port {}\nimpl Port for FakeSocket {}\n";
        assert!(!implements_usb_trait("FakeSocket", not_usb));
    }
}
