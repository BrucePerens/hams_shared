// SPDX-License-Identifier: AGPL-3.0-or-later
//! Result cache keyed by file CONTENT: (check fingerprint, path relative to the root, SHA-256 of the
//! bytes) -> that check's result for that file. A file whose bytes did not change is not evaluated
//! again; a changed rule definition or engine version changes the fingerprint and so misses. The path
//! is part of the key because a check's file filters and messages depend on it. One JSON file per root
//! under `~/.cache/hams-lint/engine/`; it holds only the entries the last run used, so it never grows
//! past one tree's worth.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use sha2::{Digest, Sha256};

use crate::rules::{hex, FileResult};

pub struct Cache {
    path: PathBuf,
    old: HashMap<String, FileResult>,
    used: Mutex<HashMap<String, FileResult>>,
    pub hits: std::sync::atomic::AtomicUsize,
}

pub fn default_dir() -> PathBuf {
    let base = std::env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".cache")
        });
    base.join("hams-lint").join("engine")
}

pub fn content_hash(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}

impl Cache {
    pub fn open(dir: &Path, root: &Path) -> Cache {
        let real = std::fs::canonicalize(root).unwrap_or_else(|_| root.to_path_buf());
        let name = format!(
            "{}.json",
            &content_hash(real.to_string_lossy().as_bytes())[..24]
        );
        let path = dir.join(name);
        let old = std::fs::read(&path)
            .ok()
            .and_then(|b| serde_json::from_slice::<HashMap<String, FileResult>>(&b).ok())
            .unwrap_or_default();
        Cache {
            path,
            old,
            used: Mutex::new(HashMap::new()),
            hits: Default::default(),
        }
    }

    fn key(fingerprint: &str, rel: &str, sha: &str) -> String {
        format!("{fingerprint}\t{rel}\t{sha}")
    }

    pub fn get(&self, fingerprint: &str, rel: &str, sha: &str) -> Option<FileResult> {
        let key = Self::key(fingerprint, rel, sha);
        let hit = self.old.get(&key)?.clone();
        self.hits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        self.used
            .lock()
            .expect("cache lock")
            .insert(key, hit.clone());
        Some(hit)
    }

    pub fn put(&self, fingerprint: &str, rel: &str, sha: &str, result: &FileResult) {
        self.used
            .lock()
            .expect("cache lock")
            .insert(Self::key(fingerprint, rel, sha), result.clone());
    }

    /// Writes the entries this run used (atomically: a temporary file renamed over the old one).
    pub fn save(&self) {
        let used = self.used.lock().expect("cache lock");
        let Some(dir) = self.path.parent() else {
            return;
        };
        if std::fs::create_dir_all(dir).is_err() {
            return;
        }
        let tmp = self
            .path
            .with_extension(format!("tmp{}", std::process::id()));
        if let Ok(data) = serde_json::to_vec(&*used) {
            if std::fs::write(&tmp, data).is_ok() {
                let _ = std::fs::rename(&tmp, &self.path);
            }
        }
    }
}
