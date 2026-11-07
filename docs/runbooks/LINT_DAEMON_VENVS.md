# Lint: the cached venv of an isolated daemon (anchor reference, ADR 0113)

Written 2026-11-07. Code: `tools/lint_daemon_venv.py` and three helpers in `tools/run_linters.py`. Tests:
`tools/test_lint_daemon_venv.py`.

Contents: [Why](#why) - [lint_daemon_venv](#lint_daemon_venv) - [run_linters](#run_linters) - [Operation](#operation)

## Why

`daemons/hams_simulated_bots` keeps its own virtualenv by design (`requirements.txt`: aiortc, av, faster-whisper, piper-tts,
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
`.hams-lint-venv-ready` was written, so a killed build is never mistaken for a complete venv. Idempotent and retry-safe. Needs
network access to the package index on the first build of a given requirements file (real network access, as production).
No secrets involved. The venv is created without system site packages (the `venv` default).

Example: `python = ensure_venv("hams_simulated_bots", "daemons/hams_simulated_bots/requirements.txt")`.
See also: `venv_dir`, `main`; tests `test_ensure_venv_builds_once_then_reuses_and_the_venv_is_isolated`,
`test_a_failed_build_raises_with_the_output_and_leaves_no_ready_venv`.

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

### `run_linters:ISOLATED_VENV_DAEMONS`
Data structure: the tuple `("hams_simulated_bots",)` and the string prefix `ISOLATED_VENV_PREFIX = "isolated-venv:"`, both module-level
in `tools/run_linters.py`.

| Name | Type | Meaning |
|---|---|---|
| `ISOLATED_VENV_DAEMONS` | tuple of str | daemon directory names (under `daemons/`) whose `requirements.txt` is an isolated venv by design. Add a daemon only when its tests need packages that must not be installed system-wide |
| `ISOLATED_VENV_PREFIX` | str | the prefix of the pseudo-interpreter `isolated_venv_marker_for` returns; it is only a grouping key and is never executed |

Invariants: every listed daemon has a `requirements.txt`. Constants, never mutated at run time. Read by `isolated_venv_marker_for`
and `pytest_argv_prefix`. Test: `test_only_listed_daemons_own_directories_get_the_isolated_venv`.

### `run_linters:isolated_venv_marker_for`
Decides whether a daemon test directory runs under the cached venv. Called by the step builder's interpreter lookup, only after
the walk up from the test directory found no `.venv` of the daemon's own.

`isolated_venv_marker_for(test_dir, daemons_root) -> str or None`

| Argument | Type | Meaning |
|---|---|---|
| `test_dir` | str | the directory holding test files, under `daemons_root` |
| `daemons_root` | str | the repository's `daemons/` directory |

Returns `ISOLATED_VENV_PREFIX + <daemon>` when `test_dir` is a listed daemon's own directory or one level below it (`tests`, `tools`)
and `<daemon>/requirements.txt` exists; None otherwise. Deeper directories (`hams_simulated_bots/fine_tuning/tests`, which has its own
torch-sized venv and runs under the system python in a clean tree) never match. Pure; no errors. Tests:
`test_only_listed_daemons_own_directories_get_the_isolated_venv`, `test_a_daemon_without_requirements_txt_is_not_matched`.

### `run_linters:pytest_argv_prefix`
Builds the start of a pytest step's argv for a group's interpreter value.

`pytest_argv_prefix(interpreter, python_exec, dir_path) -> list of str`

| Argument | Type | Meaning |
|---|---|---|
| `interpreter` | str | a python path, or an `isolated-venv:<daemon>` marker |
| `python_exec` | str | the system python running the lint |
| `dir_path` | str | the repository root (the step's paths are placed under it, so `--remote` can relocate them) |

Returns `[interpreter, "-m", "pytest", "-q"]` for a path; for a marker
`[python_exec, <dir_path>/tools/lint_daemon_venv.py, --name, <daemon>, --requirements, <dir_path>/daemons/<daemon>/requirements.txt, --, -m, pytest, -q]`.
The caller appends the test files. The argv names no machine-specific absolute path other than under `dir_path`, so the step stays
eligible for remote hosts, where it builds its own cached venv. pytest-xdist is not added to such a step (the step is serial anyway:
`lint_exec.XDIST_SERIAL`). Test: `test_pytest_argv_prefix_for_a_marker_and_for_a_plain_interpreter`.

## Operation

* Location: `~/.cache/hams-lint-venvs/<daemon>-py<ver>-<hash>` (and `.lock`), on each machine. Outside every worktree, shared by all.
* Size: a few hundred MB per requirements version (onnxruntime, av, ctranslate2). Old versions are not pruned automatically; delete
  the directory (`rm -rf ~/.cache/hams-lint-venvs`) to reclaim space, the next run rebuilds. Free-space floors of `lint_cache.py` do
  not cover it.
* A remote host needs `python3 -m venv` (Debian: `python3-venv`) and network access to PyPI for the first run.
* Piper voice model: `main.py` needs `models/en_US-lessac-low.onnx`; the tests that synthesise speech skip with a stated reason
  when it is absent (daemon `tests/conftest.py`), so a clean tree passes and the dev box's main checkout runs them.
