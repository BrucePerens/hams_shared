// SPDX-License-Identifier: AGPL-3.0-or-later
//! hams_lint: one parallel lint engine for the hams repositories (rule format and the parity rule:
//! the header of `rules/checks.toml`; the parity harness: `tools/lint_engine_parity.py`).
//!
//! Each root's file list is taken once (`git ls-files`, see files.rs), every file a selected check
//! needs is read ONCE and handed to all of those checks, files are processed in parallel (rayon), and
//! each (check, file) result is cached by the file's content hash (cache.rs). Checks are declared in
//! `rules/checks.toml` (embedded at build time; `--rules FILE` overrides) as path filters plus line
//! patterns, or name a Rust algorithm (algos.rs) when a pattern cannot express them.
//!
//!     hams_lint check NAME [ARG...]      drop-in for the Python checker NAME: same arguments, output, exit status
//!     hams_lint scan [--check NAME]... [--json] [--no-cache] [--cache-dir DIR] [--jobs N] ROOT...
//!     hams_lint files NAME ROOT          the files NAME reads under ROOT, in its report order
//!     hams_lint list                     the checks and the Python checkers they replace
//!     hams_lint selftest                 every check's `[[check.example]]` cases
//!
//! Exit status: 0 clean, 1 findings, 2 could not check (bad target, empty scan, bad rule file).

mod algos;
mod cache;
mod files;
mod rules;
mod text;

use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Instant;

use rayon::prelude::*;

use crate::cache::Cache;
use crate::files::{py_abspath, walk_order, RootFiles};
use crate::rules::{Check, FileResult};

pub const DEFAULT_RULES: &str = include_str!("../rules/checks.toml");
const EXIT_NOT_CHECKED: i32 = 2;

/// One file's results: its path, (check index, result) pairs, and the bytes read.
type Evaluated<'a> = (&'a str, Vec<(usize, FileResult)>, usize);

/// One check's results under one root, in the check's report order.
struct RootResult {
    root: PathBuf,
    scanned: usize,
    files: Vec<(String, FileResult)>,
}

struct Stats {
    walked_roots: usize,
    files_read: usize,
    bytes_read: usize,
    list_seconds: f64,
}

fn refuse(message: &str) -> ! {
    eprintln!("{message}");
    std::process::exit(EXIT_NOT_CHECKED);
}

/// Python `repr()` of a plain path string.
fn py_repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        format!("\"{s}\"")
    } else {
        format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// `check_target.require_directory`.
fn require_directory(given: &str, checker: &str) -> PathBuf {
    let absolute = py_abspath(given);
    let shown = absolute.display();
    if !absolute.exists() {
        refuse(&format!(
            "[!] {checker}: target {} does not exist (resolved to {shown}); nothing was scanned, so this is not a pass. \
             Next: pass the directory that holds the sources (a mistyped path, a renamed module or an unpopulated checkout).",
            py_repr(given)
        ));
    }
    if !absolute.is_dir() {
        refuse(&format!(
            "[!] {checker}: target {} is not a directory (resolved to {shown}); nothing was scanned, so this is not a pass. \
             Next: pass the directory that holds the sources, not a file.",
            py_repr(given)
        ));
    }
    absolute
}

/// `check_target.require_scanned`.
fn require_scanned(count: usize, root: &str, checker: &str, what: &str) {
    if count > 0 || std::env::var("HAMS_CHECK_ALLOW_EMPTY").as_deref() == Ok("1") {
        return;
    }
    refuse(&format!(
        "[!] {checker}: scanned 0 {what} under {root}; a scan of nothing is not a pass. \
         Cause: the directory holds none of the files this checker reads (the wrong directory, a module that was moved or is not \
         checked out, or an empty tree). \
         Next: pass the directory that holds them. A step that legitimately can see none sets HAMS_CHECK_ALLOW_EMPTY=1."
    ));
}

/// Python's text for an OSError from `open(path)`.
fn py_oserror(e: &std::io::Error, path: &str) -> String {
    match e.raw_os_error() {
        Some(code) => {
            let text = std::io::Error::from_raw_os_error(code).to_string();
            let text = text.split(" (os error").next().unwrap_or(&text).to_owned();
            format!("[Errno {code}] {text}: {}", py_repr(path))
        }
        None => e.to_string(),
    }
}

fn is_broken_link(path: &Path) -> bool {
    path.symlink_metadata()
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
        && !path.exists()
}

/// The files `check` reads under `listing`, in its report order.
fn selected(check: &Check, listing: &RootFiles) -> Vec<String> {
    let candidates: Vec<&String> = listing.candidates(check.untracked()).collect();
    let mut rels: Vec<String> = candidates
        .into_par_iter()
        .filter(|rel| check.selects(rel))
        .filter(|rel| {
            !(check.spec.files.skip_broken_links
                && is_broken_link(&listing.root.join(rel.as_str())))
        })
        .cloned()
        .collect();
    if check.spec.order == "walk" {
        rels.sort_by(|a, b| walk_order(a, b));
    } else {
        rels.sort();
    }
    rels.dedup();
    rels
}

/// Runs `checks` over one root: each needed file read once, in parallel, results cached by content.
fn scan_root(
    root: &Path,
    checks: &[&Check],
    cache: Option<&Cache>,
    stats: &mut Stats,
) -> Vec<RootResult> {
    let started = Instant::now();
    let listing = RootFiles::list(root, checks.iter().any(|c| c.untracked()));
    let per_check: Vec<Vec<String>> = checks.iter().map(|c| selected(c, &listing)).collect();
    stats.list_seconds += started.elapsed().as_secs_f64();
    stats.walked_roots += usize::from(!listing.git);

    let mut wanted: HashMap<&str, Vec<usize>> = HashMap::new();
    for (ci, rels) in per_check.iter().enumerate() {
        for rel in rels {
            wanted.entry(rel.as_str()).or_default().push(ci);
        }
    }
    let work: Vec<(&str, Vec<usize>)> = wanted.into_iter().collect();
    let root_display = root.display().to_string();
    let evaluated: Vec<Evaluated> = work
        .par_iter()
        .map(|(rel, cis)| {
            let abs = format!("{root_display}/{rel}");
            let bytes = match std::fs::read(&abs) {
                Ok(b) => b,
                Err(e) => {
                    let msg = py_oserror(&e, &abs);
                    let out = cis
                        .iter()
                        .map(|&ci| {
                            (
                                ci,
                                FileResult {
                                    findings: Vec::new(),
                                    warning: checks[ci].warning(&abs, &msg),
                                },
                            )
                        })
                        .collect();
                    return (*rel, out, 0);
                }
            };
            // Hashing costs about as much as a pattern scan: only checks marked `cacheable`
            // (algorithms worth skipping) pay for it.
            let cache = cache.filter(|_| cis.iter().any(|&ci| checks[ci].spec.cacheable));
            let sha = cache.map(|_| cache::content_hash(&bytes));
            let out = cis
                .iter()
                .map(|&ci| {
                    let check = checks[ci];
                    if let (Some(cache), Some(sha), true) =
                        (cache, sha.as_deref(), check.spec.cacheable)
                    {
                        if let Some(hit) = cache.get(&check.fingerprint, rel, sha) {
                            return (ci, hit);
                        }
                        let result = check.eval(rel, &abs, &bytes);
                        cache.put(&check.fingerprint, rel, sha, &result);
                        return (ci, result);
                    }
                    (ci, check.eval(rel, &abs, &bytes))
                })
                .collect();
            (*rel, out, bytes.len())
        })
        .collect();

    stats.files_read += evaluated.len();
    let mut by_check: Vec<HashMap<&str, FileResult>> = vec![HashMap::new(); checks.len()];
    for (rel, results, size) in evaluated {
        stats.bytes_read += size;
        for (ci, r) in results {
            by_check[ci].insert(rel, r);
        }
    }
    per_check
        .iter()
        .enumerate()
        .map(|(ci, rels)| RootResult {
            root: root.to_path_buf(),
            scanned: rels.len(),
            files: rels
                .iter()
                .map(|rel| {
                    (
                        rel.clone(),
                        by_check[ci].remove(rel.as_str()).unwrap_or_default(),
                    )
                })
                .collect(),
        })
        .collect()
}

/// Roots as the legacy checker takes them from its arguments.
fn legacy_roots(check: &Check, args: &[String]) -> Vec<PathBuf> {
    let name = &check.spec.name;
    match check.spec.roots.as_str() {
        "single" => vec![require_directory(&args[0], name)],
        "multi" => {
            let mut seen = Vec::new();
            for a in args {
                if !seen.contains(a) {
                    seen.push(a.clone());
                }
            }
            seen.iter().map(|a| require_directory(a, name)).collect()
        }
        _ => {
            let mut roots: Vec<PathBuf> = Vec::new();
            for a in args {
                let mut r = require_directory(a, name);
                if r.file_name().is_some_and(|n| n == "hams_shared") {
                    r = r.parent().map(Path::to_path_buf).unwrap_or(r);
                }
                if !roots.contains(&r) {
                    roots.push(r);
                }
            }
            roots
        }
    }
}

fn join_roots(roots: &[PathBuf]) -> String {
    roots
        .iter()
        .map(|r| r.display().to_string())
        .collect::<Vec<_>>()
        .join(", ")
}

/// Prints one check's results the way its Python checker does; returns whether it found anything.
fn report(check: &Check, results: &[RootResult], out: &mut impl Write) -> bool {
    let multi = results.len() > 1;
    let mut found = false;
    if check.spec.report == "inline" {
        for rr in results {
            let base = rr
                .root
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            for (rel, fr) in &rr.files {
                if let Some(w) = &fr.warning {
                    let _ = writeln!(out, "{w}");
                }
                for f in &fr.findings {
                    found = true;
                    let shown = if multi {
                        format!("{base}/{rel}")
                    } else {
                        rel.clone()
                    };
                    let _ = writeln!(out, "❌ {shown}:{}: {}", f.line, f.message);
                }
            }
        }
        return found;
    }
    let mut lines = Vec::new();
    for rr in results {
        for (rel, fr) in &rr.files {
            if let Some(w) = &fr.warning {
                let _ = writeln!(out, "{w}");
            }
            for f in &fr.findings {
                lines.push(format!("  - {rel}:{} {}", f.line, f.message));
            }
        }
    }
    if !lines.is_empty() {
        let _ = writeln!(out, "{}", check.spec.header);
        for l in lines {
            let _ = writeln!(out, "{l}");
        }
    }
    !lines_empty_guard(results)
}

fn lines_empty_guard(results: &[RootResult]) -> bool {
    results
        .iter()
        .all(|rr| rr.files.iter().all(|(_, fr)| fr.findings.is_empty()))
}

fn find_check<'a>(checks: &'a [Check], name: &str) -> &'a Check {
    let name = name.trim_end_matches(".py");
    checks
        .iter()
        .find(|c| c.spec.name == name)
        .unwrap_or_else(|| {
            refuse(&format!(
                "[!] hams_lint: no check named {name:?} (hams_lint list shows them)"
            ))
        })
}

fn cmd_check(checks: &[Check], args: &[String]) -> i32 {
    let Some((name, rest)) = args.split_first() else {
        refuse("usage: hams_lint check NAME [ARG...]");
    };
    let check = find_check(checks, name);
    if rest.is_empty() {
        println!("{}", check.spec.usage);
        return 1;
    }
    let roots = legacy_roots(check, rest);
    let mut stats = Stats {
        walked_roots: 0,
        files_read: 0,
        bytes_read: 0,
        list_seconds: 0.0,
    };
    let display = join_roots(&roots);
    let mut results = Vec::new();
    for root in &roots {
        results.extend(scan_root(root, &[check], None, &mut stats));
    }
    let scanned: usize = results.iter().map(|r| r.scanned).sum();
    if check.spec.scanned_check == "before" {
        require_scanned(scanned, &display, &check.spec.name, &check.spec.what);
    }
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    let found = report(check, &results, &mut out);
    let _ = out.flush();
    if check.spec.scanned_check == "after" {
        require_scanned(scanned, &display, &check.spec.name, &check.spec.what);
    }
    i32::from(found)
}

fn json_escape_results(check: &Check, results: &[RootResult]) -> serde_json::Value {
    let mut findings = Vec::new();
    let mut warnings = Vec::new();
    for rr in results {
        let root = rr.root.display().to_string();
        for (rel, fr) in &rr.files {
            if let Some(w) = &fr.warning {
                warnings.push(serde_json::json!([root, rel, w]));
            }
            for f in &fr.findings {
                findings.push(serde_json::json!([root, rel, f.line, f.message]));
            }
        }
    }
    serde_json::json!({
        "legacy": check.spec.legacy,
        "scanned": results.iter().map(|r| r.scanned).sum::<usize>(),
        "findings": findings,
        "warnings": warnings,
    })
}

fn cmd_scan(checks: &[Check], args: &[String]) -> i32 {
    let mut names = Vec::new();
    let mut roots = Vec::new();
    let mut json = false;
    let mut use_cache = true;
    let mut cache_dir = cache::default_dir();
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--check" => names.push(it.next().cloned().unwrap_or_default()),
            "--json" => json = true,
            "--no-cache" => use_cache = false,
            "--cache-dir" => cache_dir = PathBuf::from(it.next().cloned().unwrap_or_default()),
            _ => roots.push(a.clone()),
        }
    }
    if roots.is_empty() {
        refuse("usage: hams_lint scan [--check NAME]... [--json] [--no-cache] [--cache-dir DIR] ROOT...");
    }
    let selected: Vec<&Check> = if names.is_empty() {
        checks.iter().collect()
    } else {
        names.iter().map(|n| find_check(checks, n)).collect()
    };
    let started = Instant::now();
    let mut stats = Stats {
        walked_roots: 0,
        files_read: 0,
        bytes_read: 0,
        list_seconds: 0.0,
    };
    let mut per_check: Vec<Vec<RootResult>> = selected.iter().map(|_| Vec::new()).collect();
    let mut hits = 0;
    for given in &roots {
        let root = require_directory(given, "hams_lint");
        let cache = use_cache.then(|| Cache::open(&cache_dir, &root));
        for (ci, rr) in scan_root(&root, &selected, cache.as_ref(), &mut stats)
            .into_iter()
            .enumerate()
        {
            per_check[ci].push(rr);
        }
        if let Some(c) = cache {
            hits += c.hits.load(std::sync::atomic::Ordering::Relaxed);
            c.save();
        }
    }
    let found = per_check.iter().any(|rs| !lines_empty_guard(rs));
    if json {
        let mut map = serde_json::Map::new();
        for (check, rs) in selected.iter().zip(&per_check) {
            map.insert(check.spec.name.clone(), json_escape_results(check, rs));
        }
        let doc = serde_json::json!({
            "engine": rules::ENGINE_VERSION,
            "checks": map,
            "files_read": stats.files_read,
            "bytes_read": stats.bytes_read,
            "cache_hits": hits,
            "walked_roots": stats.walked_roots,
            "seconds": {"list": stats.list_seconds, "total": started.elapsed().as_secs_f64()},
        });
        println!("{doc}");
    } else {
        let stdout = std::io::stdout();
        let mut out = stdout.lock();
        for (check, rs) in selected.iter().zip(&per_check) {
            let _ = writeln!(out, "== {}", check.spec.name);
            report(check, rs, &mut out);
        }
        let _ = writeln!(
            out,
            "hams_lint: {} checks, {} files ({} bytes) read once, {} cache hits, {:.2}s",
            selected.len(),
            stats.files_read,
            stats.bytes_read,
            hits,
            started.elapsed().as_secs_f64()
        );
    }
    i32::from(found)
}

fn cmd_files(checks: &[Check], args: &[String]) -> i32 {
    let [name, root] = args else {
        refuse("usage: hams_lint files NAME ROOT")
    };
    let check = find_check(checks, name);
    let root = require_directory(root, &check.spec.name);
    let listing = RootFiles::list(&root, check.untracked());
    for rel in selected(check, &listing) {
        println!("{rel}");
    }
    0
}

fn cmd_selftest(checks: &[Check]) -> i32 {
    let base = std::env::temp_dir().join(format!("hams_lint_selftest_{}", std::process::id()));
    let mut failed = 0;
    let mut ran = 0;
    for check in checks {
        for (n, ex) in check.spec.examples.iter().enumerate() {
            let dir = base.join(format!("{}_{n}", check.spec.name));
            let path = dir.join(&ex.path);
            if let Some(parent) = path.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            if std::fs::write(&path, &ex.content).is_err() {
                eprintln!("[!] selftest: cannot write {}", path.display());
                failed += 1;
                continue;
            }
            let mut stats = Stats {
                walked_roots: 0,
                files_read: 0,
                bytes_read: 0,
                list_seconds: 0.0,
            };
            let results = scan_root(&dir, &[check], None, &mut stats);
            let got: Vec<usize> = results
                .iter()
                .flat_map(|r| {
                    r.files
                        .iter()
                        .flat_map(|(_, fr)| fr.findings.iter().map(|f| f.line))
                })
                .collect();
            ran += 1;
            if got != ex.lines {
                failed += 1;
                println!(
                    "FAIL {} example {n} ({}): expected lines {:?}, got {:?}",
                    check.spec.name, ex.path, ex.lines, got
                );
            }
        }
    }
    let _ = std::fs::remove_dir_all(&base);
    println!("selftest: {ran} examples, {failed} failed");
    if ran == 0 {
        return EXIT_NOT_CHECKED;
    }
    i32::from(failed > 0)
}

fn main() {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut rules_text = DEFAULT_RULES.to_owned();
    if let Some(i) = args.iter().position(|a| a == "--rules") {
        let path = args.get(i + 1).cloned().unwrap_or_default();
        rules_text = std::fs::read_to_string(&path).unwrap_or_else(|e| {
            refuse(&format!("[!] hams_lint: cannot read rule file {path}: {e}"))
        });
        args.drain(i..=(i + 1).min(args.len() - 1));
    }
    if let Some(i) = args.iter().position(|a| a == "--jobs") {
        if let Some(n) = args.get(i + 1).and_then(|n| n.parse::<usize>().ok()) {
            let _ = rayon::ThreadPoolBuilder::new()
                .num_threads(n)
                .build_global();
        }
        args.drain(i..=(i + 1).min(args.len() - 1));
    }
    let checks =
        rules::load(&rules_text).unwrap_or_else(|e| refuse(&format!("[!] hams_lint: {e}")));
    let Some((cmd, rest)) = args.split_first() else {
        refuse("usage: hams_lint {check|scan|files|list|selftest} ... (see the module doc in src/main.rs)");
    };
    let code = match cmd.as_str() {
        "check" => cmd_check(&checks, rest),
        "scan" => cmd_scan(&checks, rest),
        "files" => cmd_files(&checks, rest),
        "selftest" => cmd_selftest(&checks),
        "list" => {
            for c in &checks {
                let kind = if c.spec.algorithm.is_empty() {
                    format!("{} rules", c.rules.len())
                } else {
                    c.spec.algorithm.clone()
                };
                println!("{}\t{}\t{}", c.spec.name, c.spec.legacy, kind);
            }
            0
        }
        other => refuse(&format!("[!] hams_lint: unknown command {other:?}")),
    };
    std::process::exit(code);
}
