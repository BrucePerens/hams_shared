// SPDX-License-Identifier: AGPL-3.0-or-later
//! Which files of a checkout belong to the tree: the Rust twin of `tools/tree_files.py`.
//!
//! In a git work tree the list comes from `git ls-files --cached --recurse-submodules` (plus, for checks
//! that ask for it, `--others --exclude-standard` in the tree and in each submodule), listed ONCE per root
//! and then filtered per check, instead of one `git ls-files` per checker process. Outside git the
//! directory is walked like `os.walk` (symlinked directories are not entered, a directory holding its own
//! `.git` is another checkout and is skipped). A tracked path that no longer exists, or that is a
//! directory (a symlink to one, a submodule), is dropped, as `git_tracked` does.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::process::Command;

use rayon::prelude::*;

// [@ANCHOR: hams_lint:files_root_files_struct]
/// The candidate files of one root, relative to it with `/` separators.
pub struct RootFiles {
    pub root: PathBuf,
    /// Listed by git (false: walked).
    pub git: bool,
    pub tracked: Vec<String>,
    /// Untracked, not ignored files (git only; empty outside git).
    pub untracked: Vec<String>,
}

fn ls_files(dir: &Path, args: &[&str]) -> Option<Vec<String>> {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .arg("ls-files")
        .arg("-z")
        .args(args)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    Some(
        text.split('\0')
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .collect(),
    )
}

fn submodule_paths(root: &Path) -> Vec<String> {
    let Some(entries) = ls_files(root, &["--stage"]) else {
        return Vec::new();
    };
    entries
        .iter()
        .filter_map(|e| {
            let (meta, path) = e.split_once('\t')?;
            meta.starts_with("160000 ").then(|| path.to_owned())
        })
        .collect()
}

/// `os.path.lexists(p) and not os.path.isdir(p)`.
fn is_listed_file(path: &Path) -> bool {
    path.symlink_metadata().is_ok() && !path.metadata().map(|m| m.is_dir()).unwrap_or(false)
}

/// Python `os.path.normpath` for a `/`-separated relative path made of `a/../b` style parts.
fn normalize_rel(rel: &str) -> String {
    let mut parts: Vec<&str> = Vec::new();
    for part in rel.split('/') {
        match part {
            "" | "." => {}
            ".." if parts.last().is_some_and(|p| *p != "..") => {
                parts.pop();
            }
            _ => parts.push(part),
        }
    }
    parts.join("/")
}

fn walk(root: &Path) -> Vec<String> {
    let mut out = Vec::new();
    let mut stack = vec![(root.to_path_buf(), String::new())];
    while let Some((dir, prefix)) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            let rel = if prefix.is_empty() {
                name.clone()
            } else {
                format!("{prefix}/{name}")
            };
            let path = entry.path();
            let Ok(kind) = entry.file_type() else {
                continue;
            };
            if kind.is_dir() {
                if !path.join(".git").exists() {
                    stack.push((path, rel));
                }
            } else if kind.is_symlink() {
                // os.walk lists a symlink to a directory as a directory it does not enter.
                if !path.metadata().map(|m| m.is_dir()).unwrap_or(false) {
                    out.push(rel);
                }
            } else {
                out.push(rel);
            }
        }
    }
    out
}

impl RootFiles {
    /// Lists `root` once. `want_untracked`: also list untracked files (any selected check needs them).
    pub fn list(root: &Path, want_untracked: bool) -> RootFiles {
        let Some(tracked) = ls_files(root, &["--cached", "--recurse-submodules"]) else {
            return RootFiles {
                root: root.to_path_buf(),
                git: false,
                tracked: walk(root),
                untracked: Vec::new(),
            };
        };
        let mut seen: HashSet<String> = HashSet::new();
        let unique: Vec<String> = tracked
            .into_iter()
            .filter(|rel| seen.insert(rel.clone()))
            .collect();
        // Two stat calls per tracked file: done in parallel (order is kept).
        let tracked: Vec<String> = unique
            .into_par_iter()
            .filter(|rel| is_listed_file(&root.join(rel)))
            .collect();
        let mut untracked = Vec::new();
        if want_untracked {
            let mut others: Vec<String> =
                ls_files(root, &["--others", "--exclude-standard"]).unwrap_or_default();
            for sub in submodule_paths(root) {
                let sub_root = root.join(&sub);
                if sub_root.is_dir() {
                    for rel in
                        ls_files(&sub_root, &["--others", "--exclude-standard"]).unwrap_or_default()
                    {
                        others.push(normalize_rel(&format!("{sub}/{rel}")));
                    }
                }
            }
            for rel in others {
                if seen.insert(rel.clone()) && is_listed_file(&root.join(&rel)) {
                    untracked.push(rel);
                }
            }
        }
        RootFiles {
            root: root.to_path_buf(),
            git: true,
            tracked,
            untracked,
        }
    }

    /// Tracked files, plus the untracked ones when `untracked` (outside git every walked file).
    pub fn candidates(&self, untracked: bool) -> impl Iterator<Item = &String> {
        let extra: &[String] = if untracked { &self.untracked } else { &[] };
        self.tracked.iter().chain(extra.iter())
    }
}

/// Order of `tree_walk`: in each directory its files (sorted) come before its subdirectories (sorted).
pub fn walk_order(a: &str, b: &str) -> std::cmp::Ordering {
    let pa: Vec<&str> = a.split('/').collect();
    let pb: Vec<&str> = b.split('/').collect();
    for k in 0..pa.len().min(pb.len()) {
        if pa[k] != pb[k] {
            let a_file = k == pa.len() - 1;
            let b_file = k == pb.len() - 1;
            return match (a_file, b_file) {
                (true, false) => std::cmp::Ordering::Less,
                (false, true) => std::cmp::Ordering::Greater,
                _ => pa[k].cmp(pb[k]),
            };
        }
    }
    pa.len().cmp(&pb.len())
}

/// Python `os.path.abspath`: absolute and normalized, symlinks NOT resolved.
pub fn py_abspath(given: &str) -> PathBuf {
    let joined = if given.starts_with('/') {
        given.to_owned()
    } else {
        let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/"));
        format!("{}/{}", cwd.display(), given)
    };
    PathBuf::from(format!("/{}", normalize_rel(&joined)))
}

#[cfg(test)]
mod tests {
    use super::*;
    // Tests [@ANCHOR: hams_lint:files_root_files_struct]

    #[test]
    fn walk_order_puts_files_before_subdirectories() {
        let mut v = vec!["b/x.py", "a.py", "a/z.py", "c.py", "a-b.py"];
        v.sort_by(|a, b| walk_order(a, b));
        assert_eq!(v, vec!["a-b.py", "a.py", "c.py", "a/z.py", "b/x.py"]);
    }

    #[test]
    fn abspath_normalizes_without_resolving() {
        assert_eq!(py_abspath("/a/./b/../c//d/"), PathBuf::from("/a/c/d"));
        assert_eq!(normalize_rel("x/../../y"), "../y");
    }
}
