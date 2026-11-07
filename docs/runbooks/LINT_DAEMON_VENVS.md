# Lint: the cached venv of an isolated daemon (anchor reference, ADR 0113)

Written 2026-11-07. Code: `tools/lint_daemon_venv.py` and three helpers in `tools/run_linters.py`. Tests:
`tools/test_lint_daemon_venv.py`.

Contents: [Why](#why) - [lint_daemon_venv](#lint_daemon_venv) - [run_linters](#run_linters) - [Operation](#operation)

## Why

`daemons/hams_simulated_bots` (and its `fine_tuning/` directory, with its own `requirements.txt`: transformers, peft, datasets, accelerate, CPU torch) keep their own virtualenvs by design (`requirements.txt`: aiortc, av, faster-whisper, piper-tts,
isolated from the system python so mypy never type-checks `import av`). The venv is untracked, so a clean worktree and every
remote lint host had none, and `pytest daemons/hams_simulated_bots` could not even collect (`import av`). The step now
builds, once per machine and per requirements content, a venv under the lint cache and runs pytest in it. A daemon that
already has a `.venv` in its tree (the dev box's main checkout, or a worktree where `fix_worktree_symlinks.py` linked it)
keeps using that one.

## lint_daemon_venv

### `lint_daemon_venv:venv_dir`
The directory a daemon's cached venv lives in. Pure: reads the requirements file, touches nothing else.

`venv_dir(name, requirements_path, environ=None, version_info=None) -> str`

| Argument | Type | Meaning |
|---|---|---|
| `name` | str | daemon name, a path-safe label (`hams_simulated_bots`) |
| `requirements_path` | str | the daemon's `requirements.txt`; read in full (bytes). `OSError` if it is unreadable |
| `environ` | mapping or None | `XDG_CACHE_HOME` selects the cache root, else `~/.cache`; None means `os.environ` |
| `version_info` | tuple or None | the python's (major, minor, ...); None means the running interpreter. Only major and minor are used |

Returns `<cache>/hams-lint-venvs/<name>-py<major><minor>-<first 16 hex of sha256(requirements bytes)>`. A changed
requirements file, another python minor version or another name is another directory; whitespace-only edits count as
changes. Concurrency: none needed. See also: `ensure_venv`, test `test_venv_dir_is_keyed_on_name_python_and_requirements_content`.

### `lint_daemon_venv:ensure_venv`
Returns the python of the daemon's cached venv, building the venv when it is not there. Called by `main` at the start of
the daemon's pytest step, on the dev box and on every remote lint host.

`ensure_venv(name, requirements_path, environ=None, run=subprocess.run) -> str`

| Argument | Type | Meaning |
|---|---|---|
| `name`, `requirements_path`, `environ` | | as `venv_dir` |
| `run` | callable | `subprocess.run` signature; a test replaces it. Called as `run(argv, capture_output=True, text=True)` |

Returns `<venv_dir>/bin/python3`. Raises `RuntimeError` (message = the failing command and its stdout and stderr) when
`python -m venv` or `pip install -r` fails; `OSError` when the cache directory cannot be made. Nothing complete is left behind
after a failure, so the next call builds again.

Special concerns: blocking; a cold build downloads wheels (several minutes for aiortc, av, faster-whisper and piper-tts, onnxruntime
included), a warm call is a stat. Takes an exclusive `flock` on `<venv_dir>.lock` for the whole build, so concurrent lint steps
on one machine serialise and the second one finds the venv ready; the lock file is beside the directory, never inside it. The
build is made in `<venv_dir>.partial` and renamed into place after `pip` succeeded and the marker file
`.hams-lint-venv-ready` was written, so a killed build is never mistaken for a complete venv. A reused venv has the ready marker's mtime touched (that mtime is the venv's last use); after a fresh build, `prune_venvs(name)` removes
the surplus old venvs of the same name. Idempotent and retry-safe. Needs
network access to the package index on the first build of a given requirements file (real network access, as production).
No secrets involved. The venv is created without system site packages (the `venv` default).

Example: `python = ensure_venv("hams_simulated_bots", "daemons/hams_simulated_bots/requirements.txt")`.
See also: `venv_dir`, `main`; tests `test_ensure_venv_builds_once_then_reuses_and_the_venv_is_isolated`,
`test_a_failed_build_raises_with_the_output_and_leaves_no_ready_venv`.

### `lint_daemon_venv:prune_venvs`
Bounds the cache: removes all but the newest few cached venvs of one name. Called by `ensure_venv` after every fresh build (not on a plain reuse).

`prune_venvs(name, keep=KEEP_VENVS, environ=None, now=None, min_age=PRUNE_MIN_AGE_SECONDS) -> list of str`

| Argument | Type | Meaning |
|---|---|---|
| `name` | str | the daemon name exactly (`hams_simulated_bots` does not match `hams_simulated_bots_fine_tuning`) |
| `keep` | int | how many of the most recently used complete venvs of `name` to keep; `KEEP_VENVS` is 2. Counts across python minor versions and requirements hashes |
| `environ` | mapping or None | as `venv_dir` (cache root) |
| `now` | float or None | epoch seconds; None means the current time (tests pass one) |
| `min_age` | float | seconds; a venv used more recently than this is never removed (`PRUNE_MIN_AGE_SECONDS`, one hour), and a `.partial` build directory younger than this is left alone |

Returns the list of directories removed (empty when the cache does not exist or nothing is surplus). Recency is the mtime of
`.hams-lint-venv-ready`, touched by `ensure_venv` on each use. Skips a venv whose `<dir>.lock` is held (a build is in progress) and never
raises for a directory it cannot remove; removes the `.lock` file of each venv it deleted, and any stale `.partial` of this name. Special
concerns: blocking file-system work only, no network; a venv running tests in another worktree is protected by `min_age` (a running
step touches nothing after exec, so a run longer than the hour on a venv outside the newest `keep` could be pruned by a build
elsewhere: with `keep` 2 that needs three requirements versions in use at once). Size bound: at most `keep` venvs per name plus those used in the last hour.
Tests: `test_prune_keeps_the_newest_n_of_one_name_and_never_touches_another_name`,
`test_prune_spares_a_recently_used_venv_a_locked_one_and_a_young_partial_but_removes_an_old_partial`,
`test_prune_with_no_cache_directory_removes_nothing`, `test_ensure_venv_marks_use_and_prunes_old_venvs_of_its_name_after_a_build`.

### `lint_daemon_venv:main`
The command line, and the program the pytest step actually runs: `python3 lint_daemon_venv.py --name N --requirements FILE -- ARGS...`.

`main(argv=None, environ=None, execv=os.execv) -> int`

| Argument | Type | Meaning |
|---|---|---|
| `argv` | list of str or None | `--name`, `--requirements`, then `--` and the arguments for the venv's python (`-m pytest -q files...`); None means `sys.argv[1:]` |
| `environ` | mapping or None | passed to `ensure_venv` |
| `execv` | callable | `os.execv` signature; a test replaces it |

Returns 2 after printing the error to stderr when the venv cannot be built; otherwise `execv` replaces the process with the venv's
python and never returns (the return value 0 is reached only when a test's `execv` returns). Because it execs, the step's working
directory, environment, output streaming and exit code are those of pytest itself. See also: `ensure_venv`; tests
`test_main_execs_the_venv_python_with_the_arguments_after_the_double_dash`, `test_main_returns_2_and_prints_when_the_build_fails`.

## run_linters

### `run_linters:ISOLATED_VENV_DIRS`
Data structure: the tuple `("hams_simulated_bots", "hams_simulated_bots/fine_tuning")` and the string prefix `ISOLATED_VENV_PREFIX = "isolated-venv:"`, both module-level
in `tools/run_linters.py`.

| Name | Type | Meaning |
|---|---|---|
| `ISOLATED_VENV_DIRS` | tuple of str | directories under `daemons/`, written with `/`, whose `requirements.txt` is an isolated venv by design. Add one only when its tests need packages that must not be installed system-wide |
| `ISOLATED_VENV_PREFIX` | str | the prefix of the pseudo-interpreter `isolated_venv_marker_for` returns; it is only a grouping key and is never executed |

Invariants: every listed directory has a `requirements.txt`. Constants, never mutated at run time. Read by `isolated_venv_marker_for`
and `pytest_argv_prefix`. Test: `test_the_nearest_listed_directory_with_a_requirements_file_wins`.

### `run_linters:isolated_venv_marker_for`
Decides whether a daemon test directory runs under a cached venv. Called by the step builder's interpreter lookup, only after
the walk up from the test directory found no `.venv` in the tree.

`isolated_venv_marker_for(test_dir, daemons_root) -> str or None`

| Argument | Type | Meaning |
|---|---|---|
| `test_dir` | str | the directory holding test files, under `daemons_root` |
| `daemons_root` | str | the repository's `daemons/` directory |

Returns `ISOLATED_VENV_PREFIX + <relative directory>` for the nearest directory at or above `test_dir`, below `daemons_root`, that is
in `ISOLATED_VENV_DIRS` and holds a `requirements.txt` (so `hams_simulated_bots/fine_tuning/tests` gets `fine_tuning`'s, `hams_simulated_bots/tests` the
bot's); None when there is none (including `test_dir == daemons_root`). Pure; no errors. Tests:
`test_the_nearest_listed_directory_with_a_requirements_file_wins`, `test_a_directory_without_requirements_txt_is_skipped_for_the_next_one_up`.

### `run_linters:pytest_argv_prefix`
Builds the start of a pytest step's argv for a group's interpreter value.

`pytest_argv_prefix(interpreter, python_exec, dir_path) -> list of str`

| Argument | Type | Meaning |
|---|---|---|
| `interpreter` | str | a python path, or an `isolated-venv:<directory>` marker |
| `python_exec` | str | the system python running the lint |
| `dir_path` | str | the repository root (the step's paths are placed under it, so `--remote` can relocate them) |

Returns `[interpreter, "-m", "pytest", "-q"]` for a path; for a marker
`[python_exec, <dir_path>/tools/lint_daemon_venv.py, --name, <directory with "/" as "_">, --requirements, <dir_path>/daemons/<directory>/requirements.txt, --, -m, pytest, -q]`.
The caller appends the test files. The argv names no machine-specific absolute path other than under `dir_path`, so the step stays
eligible for remote hosts, where it builds its own cached venv. pytest-xdist is not added to such a step (the step is serial anyway:
`lint_exec.XDIST_SERIAL`). Test: `test_pytest_argv_prefix_for_a_marker_and_for_a_plain_interpreter`.

## Operation

* Location: `~/.cache/hams-lint-venvs/<name>-py<ver>-<hash>` (and `.lock`), on each machine, `<name>` being `hams_simulated_bots` or
  `hams_simulated_bots_fine_tuning`. Outside every worktree, shared by all.
* Size: a few hundred MB per requirements version (bots: onnxruntime, av, ctranslate2; fine_tuning: CPU torch, transformers). Bounded:
  `prune_venvs` keeps the newest 2 per name (plus anything used in the last hour) after each fresh build. `rm -rf ~/.cache/hams-lint-venvs`
  clears it; the next run rebuilds.
* A remote host needs `python3 -m venv` (Debian: `python3-venv`) and network access to PyPI (and download.pytorch.org for the CPU torch wheel) for the first run.
* Piper voice model: `main.py` needs `models/en_US-lessac-low.onnx`; the tests that synthesise speech skip with a stated reason
  when it is absent (daemon `tests/conftest.py`), so a clean tree passes and the dev box's main checkout runs them.
* SmolLM2 model: the `fine_tuning` tests that load the real `HuggingFaceTB/SmolLM2-360M-Instruct` tokenizer or checkpoint (about 700 MB) skip with a
  stated reason when it is not in the Hugging Face cache (`fine_tuning/tests/conftest.py`); they run wherever it is cached.
