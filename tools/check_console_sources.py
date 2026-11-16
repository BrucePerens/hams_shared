#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Runs the shack console's build-script source validation (daemons/shack_console/pipeline.rs `build_with_tests`, the code build.rs runs, tests included)
without building the shack_console crate: a throwaway cargo project holds one main.rs that includes pipeline.rs by path and needs only its three build
dependencies. Exit 0 when the console sources are clean, 1 with one problem per line otherwise. Usage: check_console_sources.py [repo_root]
Full entry: tools/README.md.
"""

import os
import re
import shutil
import subprocess
import sys

import check_target

MAIN_RS = """#[allow(dead_code)]
#[path = "{pipeline}"]
mod pipeline;
fn main() {{
    let dir = std::path::PathBuf::from(std::env::args().nth(1).expect("console dir"));
    if let Err(problems) = pipeline::build_with_tests(&dir, "0.0.0", true) {{
        for problem in &problems {{
            println!("{{problem}}");
        }}
        std::process::exit(1);
    }}
}}
"""


# [@ANCHOR: check_console_sources:main]
def main(argv=None):
    """Validate the console sources of `repo_root` (default: the current directory). Returns the exit code: 0 clean, 1 problems, 2 not checked (no such directory or no shack_console crate: run_linters only adds the step when the crate exists)."""
    argv = sys.argv[1:] if argv is None else argv
    repo_root = check_target.require_directory(os.path.realpath(argv[0] if argv else "."), "check_console_sources")
    crate = os.path.join(repo_root, "daemons", "shack_console")
    manifest = os.path.join(crate, "Cargo.toml")
    check_target.require_file(manifest, "check_console_sources")
    with open(manifest, encoding="utf-8") as handle:
        match = re.search(r"^\[build-dependencies\]\n(.*?)(?=^\[|\Z)", handle.read(), re.M | re.S)
    deps = "\n".join(line for line in (match.group(1) if match else "").splitlines() if line.strip() and not line.startswith("#"))
    base = os.environ.get("CARGO_TARGET_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "hams-lint", "target")
    project = os.path.join(base, "console_sources_project")
    os.makedirs(os.path.join(project, "src"), exist_ok=True)
    with open(os.path.join(project, "Cargo.toml"), "w", encoding="utf-8") as out:
        out.write(f'[package]\nname = "console_sources"\nversion = "0.0.0"\nedition = "2021"\n[workspace]\n[dependencies]\n{deps}\n')
    with open(os.path.join(project, "src", "main.rs"), "w", encoding="utf-8") as out:
        out.write(MAIN_RS.format(pipeline=os.path.join(crate, "pipeline.rs")))
    shutil.copyfile(os.path.join(crate, "Cargo.lock"), os.path.join(project, "Cargo.lock"))
    env = dict(os.environ, CARGO_TARGET_DIR=os.path.join(base, "console_sources"))
    result = subprocess.run(
        ["cargo", "run", "-q", "--manifest-path", os.path.join(project, "Cargo.toml"), "--", os.path.join(crate, "console")],
        env=env, check=False,
    )
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
