// SPDX-License-Identifier: AGPL-3.0-or-later
//! The rule file (`rules/checks.toml`): its schema, the loader that compiles it, and the per-file
//! evaluation of one check. A check is declarative (path filters plus line rules) unless it names an
//! `algorithm` implemented in `algos.rs`; a declarative rule may name a `post_filter` algorithm for the
//! one exception a pattern cannot express. See `rules/checks.toml` for the documented format.

use regex::Regex;
use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::algos;
use crate::text::{self, py_ext};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuleFile {
    pub version: u32,
    #[serde(rename = "check", default)]
    pub checks: Vec<CheckSpec>,
}

#[derive(Debug, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct CheckSpec {
    /// The lint step name; pass stamps and `run_linters.py` know the check by it.
    pub name: String,
    /// The Python checker this rule set replaces (the differential harness runs it as the reference).
    pub legacy: String,
    /// How command-line roots are taken: `single` (only the first argument, as `sys.argv[1]`),
    /// `multi` (every argument, duplicates dropped), `multi_shared_parent` (every argument; a directory
    /// named hams_shared stands for its parent).
    pub roots: String,
    /// `list` (a header line, then `  - rel:line message`) or `inline` (`❌ rel:line: message`, the
    /// root's name prefixed when several roots are given).
    pub report: String,
    #[serde(default)]
    pub header: String,
    pub usage: String,
    /// What `require_scanned` says the checker reads ("Python files").
    pub what: String,
    /// `walk` (tree_walk: files of a directory before its subdirectories) or `path` (sorted paths).
    #[serde(default = "default_order")]
    pub order: String,
    /// `before` scan (the count is checked first) or `after`.
    #[serde(default = "default_scanned_check")]
    pub scanned_check: String,
    pub files: FileSpec,
    /// `lines_strict` (Python line iteration, utf-8, partial on a decode error),
    /// `text_replace` (whole text, invalid bytes replaced, `splitlines()` numbering).
    pub read: String,
    /// On a read or decode error: `warn` prints `Warning: could not read PATH: ERROR`, `silent` does not.
    #[serde(default = "default_on_error")]
    pub on_error: String,
    /// Results are cached by file content (worth it for an expensive algorithm; for a pattern scan
    /// the SHA-256 costs as much as the scan, measured 2026-11-09).
    #[serde(default)]
    pub cacheable: bool,
    /// Named Rust algorithm instead of the line rules.
    #[serde(default)]
    pub algorithm: String,
    /// The whole file is clean when this matches anywhere in its text.
    #[serde(default)]
    pub skip_file_if: String,
    #[serde(default, rename = "rule")]
    pub rules: Vec<LineRuleSpec>,
    /// Injected cases: run through both the Python checker and this engine by the parity harness, and
    /// through the engine alone by `hams_lint selftest`.
    #[serde(default, rename = "example")]
    pub examples: Vec<Example>,
    /// Lines the parity harness appends to real files to inject a violation (read by
    /// tools/lint_engine_parity.py from the rule file, not here).
    #[serde(default)]
    #[allow(dead_code)]
    pub inject: Vec<String>,
}

fn default_order() -> String {
    "path".into()
}
fn default_scanned_check() -> String {
    "after".into()
}
fn default_on_error() -> String {
    "warn".into()
}

#[derive(Debug, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct FileSpec {
    /// File-name endings to keep; empty string keeps every file.
    pub suffixes: Vec<String>,
    #[serde(default)]
    pub skip_dirs: Vec<String>,
    #[serde(default)]
    pub skip_hidden_dirs: bool,
    /// Path prefixes (relative to the root, `/`-separated, no trailing slash) never scanned.
    #[serde(default)]
    pub skip_rel_dirs: Vec<String>,
    #[serde(default)]
    pub untracked: bool,
    /// Extensions (`os.path.splitext`) to keep after the suffix filter; empty keeps all.
    #[serde(default)]
    pub exts: Vec<String>,
    /// File names kept regardless of `exts`.
    #[serde(default)]
    pub names: Vec<String>,
    /// Keep a file whose name has no dot even if `exts` does not list its (empty) extension.
    #[serde(default)]
    pub keep_dotless: bool,
    /// Regexes on the relative path; a file is kept when any matches (empty keeps all).
    #[serde(default)]
    pub accept_any: Vec<String>,
    /// File names never scanned (exact) and name patterns (`prefix*suffix`).
    #[serde(default)]
    pub skip_names: Vec<String>,
    /// A broken symlink is skipped before counting (instead of failing to read).
    #[serde(default)]
    pub skip_broken_links: bool,
}

#[derive(Debug, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct LineRuleSpec {
    pub id: String,
    /// Regex (Rust `regex` syntax) searched in each line; or `literal`, a plain substring.
    #[serde(default)]
    pub regex: String,
    #[serde(default)]
    pub literal: String,
    /// The Python pattern this replaces, when the Rust form differs (documentation; the harness proves
    /// both find the same lines). Read by tools/lint_engine_parity.py from the rule file, not here.
    #[serde(default)]
    #[allow(dead_code)]
    pub legacy_regex: String,
    /// The line is exempt when this matches it.
    #[serde(default)]
    pub unless_line: String,
    /// `{1}`..`{9}` are the regex's capture groups.
    pub message: String,
    #[serde(default)]
    pub post_filter: String,
}

#[derive(Debug, Deserialize, Clone)]
#[serde(deny_unknown_fields)]
pub struct Example {
    pub path: String,
    pub content: String,
    /// Line numbers the check must report.
    pub lines: Vec<usize>,
}

/// One finding: 1-based line and message.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Finding {
    pub line: usize,
    pub message: String,
}

/// What one check concluded about one file.
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct FileResult {
    pub findings: Vec<Finding>,
    /// A read/decode problem, already formatted (`Warning: could not read ...`), when `on_error = warn`.
    pub warning: Option<String>,
}

pub struct LineRule {
    pub spec: LineRuleSpec,
    regex: Option<Regex>,
    unless: Option<Regex>,
}

pub struct Check {
    pub spec: CheckSpec,
    pub fingerprint: String,
    accept_any: Vec<Regex>,
    skip_file_if: Option<Regex>,
    pub rules: Vec<LineRule>,
}

fn compile(pattern: &str, what: &str, check: &str) -> Result<Option<Regex>, String> {
    if pattern.is_empty() {
        return Ok(None);
    }
    Regex::new(pattern)
        .map(Some)
        .map_err(|e| format!("check {check}: {what} {pattern:?}: {e}"))
}

pub const ENGINE_VERSION: &str = env!("CARGO_PKG_VERSION");

pub fn load(text: &str) -> Result<Vec<Check>, String> {
    let file: RuleFile = toml::from_str(text).map_err(|e| format!("rule file: {e}"))?;
    if file.version != 1 {
        return Err(format!(
            "rule file: version {} is not supported (1 is)",
            file.version
        ));
    }
    let mut out = Vec::new();
    let mut names = std::collections::HashSet::new();
    for spec in file.checks {
        if !names.insert(spec.name.clone()) {
            return Err(format!("rule file: check {} is defined twice", spec.name));
        }
        for (field, value, allowed) in [
            (
                "roots",
                &spec.roots,
                &["single", "multi", "multi_shared_parent"][..],
            ),
            ("report", &spec.report, &["list", "inline"][..]),
            ("order", &spec.order, &["walk", "path"][..]),
            (
                "scanned_check",
                &spec.scanned_check,
                &["before", "after"][..],
            ),
            ("read", &spec.read, &["lines_strict", "text_replace"][..]),
            ("on_error", &spec.on_error, &["warn", "silent"][..]),
        ] {
            if !allowed.contains(&value.as_str()) {
                return Err(format!(
                    "check {}: {field} = {value:?}, expected one of {allowed:?}",
                    spec.name
                ));
            }
        }
        if !spec.algorithm.is_empty() && !algos::known(&spec.algorithm) {
            return Err(format!(
                "check {}: unknown algorithm {:?}",
                spec.name, spec.algorithm
            ));
        }
        if spec.algorithm.is_empty() && spec.rules.is_empty() {
            return Err(format!(
                "check {}: neither an algorithm nor a rule",
                spec.name
            ));
        }
        let mut rules = Vec::new();
        for r in &spec.rules {
            if r.regex.is_empty() == r.literal.is_empty() {
                return Err(format!(
                    "check {} rule {}: exactly one of regex and literal",
                    spec.name, r.id
                ));
            }
            if !r.post_filter.is_empty() && !algos::known_post_filter(&r.post_filter) {
                return Err(format!(
                    "check {} rule {}: unknown post_filter {:?}",
                    spec.name, r.id, r.post_filter
                ));
            }
            rules.push(LineRule {
                regex: compile(&r.regex, "regex", &spec.name)?,
                unless: compile(&r.unless_line, "unless_line", &spec.name)?,
                spec: r.clone(),
            });
        }
        let accept_any = spec
            .files
            .accept_any
            .iter()
            .map(|p| compile(p, "accept_any", &spec.name).map(|r| r.expect("non-empty")))
            .collect::<Result<Vec<_>, _>>()?;
        let skip_file_if = compile(&spec.skip_file_if, "skip_file_if", &spec.name)?;
        // The fingerprint keys the result cache: any change to the check's definition or to the engine
        // invalidates every cached result of that check.
        let mut h = Sha256::new();
        h.update(ENGINE_VERSION.as_bytes());
        h.update(format!("{spec:?}").as_bytes());
        let fingerprint = hex(&h.finalize());
        out.push(Check {
            spec,
            fingerprint,
            accept_any,
            skip_file_if,
            rules,
        });
    }
    Ok(out)
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn name_matches(pattern: &str, name: &str) -> bool {
    match pattern.split_once('*') {
        None => pattern == name,
        Some((pre, post)) => {
            name.len() >= pre.len() + post.len() && name.starts_with(pre) && name.ends_with(post)
        }
    }
}

impl Check {
    pub fn untracked(&self) -> bool {
        self.spec.files.untracked
    }

    /// Whether the file at `rel` (relative to the root) is one this check reads.
    pub fn selects(&self, rel: &str) -> bool {
        let f = &self.spec.files;
        if !f.suffixes.iter().any(|s| rel.ends_with(s.as_str())) {
            return false;
        }
        let (dirs, name) = match rel.rsplit_once('/') {
            Some((d, n)) => (Some(d), n),
            None => (None, rel),
        };
        if let Some(dirs) = dirs {
            for part in dirs.split('/') {
                if f.skip_dirs.iter().any(|s| s == part)
                    || (f.skip_hidden_dirs && part.starts_with('.'))
                {
                    return false;
                }
            }
        }
        if f.skip_rel_dirs
            .iter()
            .any(|d| rel.starts_with(d.as_str()) && rel[d.len()..].starts_with('/'))
        {
            return false;
        }
        if !self.accept_any.is_empty() && !self.accept_any.iter().any(|r| r.is_match(rel)) {
            return false;
        }
        if !f.exts.is_empty() {
            let ext = py_ext(name);
            let dotless_ok = f.keep_dotless && !name.contains('.');
            if !f.exts.iter().any(|e| e == ext) && !f.names.iter().any(|n| n == name) && !dotless_ok
            {
                return false;
            }
        }
        !f.skip_names.iter().any(|p| name_matches(p, name))
    }

    /// Evaluates this check on one file's bytes.
    pub fn eval(&self, rel: &str, abs_display: &str, bytes: &[u8]) -> FileResult {
        if !self.spec.algorithm.is_empty() {
            return algos::run(self, rel, abs_display, bytes);
        }
        match self.spec.read.as_str() {
            "lines_strict" => {
                let (lines, err) = text::strict_lines(bytes);
                let mut result = FileResult::default();
                for (i, line) in lines.iter().enumerate() {
                    self.apply_rules(line, i + 1, None, &mut result.findings);
                }
                if let Some(err) = err {
                    result.warning = self.warning(abs_display, &err.message);
                }
                result
            }
            _ => {
                let content = text::lossy_text(bytes);
                let mut result = FileResult::default();
                if self
                    .skip_file_if
                    .as_ref()
                    .is_some_and(|r| r.is_match(&content))
                {
                    return result;
                }
                for (i, line) in text::splitlines(&content).iter().enumerate() {
                    self.apply_rules(line, i + 1, Some(&content), &mut result.findings);
                }
                result
            }
        }
    }

    pub fn warning(&self, abs_display: &str, error: &str) -> Option<String> {
        (self.spec.on_error == "warn")
            .then(|| format!("Warning: could not read {abs_display}: {error}"))
    }

    fn apply_rules(
        &self,
        line: &str,
        number: usize,
        content: Option<&str>,
        out: &mut Vec<Finding>,
    ) {
        for rule in &self.rules {
            if rule.unless.as_ref().is_some_and(|r| r.is_match(line)) {
                continue;
            }
            let message = match &rule.regex {
                None => {
                    if !line.contains(rule.spec.literal.as_str()) {
                        continue;
                    }
                    rule.spec.message.clone()
                }
                Some(re) => {
                    let Some(caps) = re.captures(line) else {
                        continue;
                    };
                    if !rule.spec.post_filter.is_empty()
                        && !algos::post_filter(
                            &rule.spec.post_filter,
                            &caps,
                            content.unwrap_or(line),
                        )
                    {
                        continue;
                    }
                    let mut m = rule.spec.message.clone();
                    for g in 0..caps.len().min(10) {
                        let key = format!("{{{g}}}");
                        if m.contains(&key) {
                            m = m.replace(&key, caps.get(g).map_or("", |c| c.as_str()));
                        }
                    }
                    m
                }
            };
            out.push(Finding {
                line: number,
                message,
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn name_patterns_take_one_star() {
        assert!(name_matches("course_*.json", "course_a.json"));
        assert!(!name_matches("course_*.json", "course_.jso"));
        assert!(name_matches(
            "night_shift_history.md",
            "night_shift_history.md"
        ));
    }

    #[test]
    fn the_shipped_rule_file_loads() {
        let checks = load(crate::DEFAULT_RULES).expect("rules load");
        assert!(checks.len() >= 4);
    }

    #[test]
    fn a_bad_rule_file_is_refused_with_the_check_named() {
        let bad = "version = 1\n[[check]]\nname='x'\nlegacy='x.py'\nroots='single'\nreport='list'\nusage='u'\nwhat='w'\nread='lines_strict'\nfiles={suffixes=['.py']}\n[[check.rule]]\nid='r'\nregex='('\nmessage='m'\n";
        let err = load(bad).err().expect("refused");
        assert!(err.contains("check x"), "{err}");
    }
}
