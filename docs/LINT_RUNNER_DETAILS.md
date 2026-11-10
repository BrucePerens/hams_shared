# Lint runner details (moved verbatim from AGENTS.md)

Moved out of `AGENTS.md`, section "Linters: the pre-merge command (rule of 2026-10-29)", on 2026-11-09, word for word, so that
agents who are not working on the lint machinery do not carry it in their context on every turn (Bruce asked for token optimization;
`tools/check_no_rule_lost.py` proves the move lost nothing). Every rule here applies exactly as it did in AGENTS.md. Paths such as
`tools/...` and `docs/...` are relative to the hams_shared root.

## From "Linters: the pre-merge command (rule of 2026-10-29)"

* **Heavy runs queue, one per machine** (`tools/lint_lock.py`, 2026-11-02). Ten agents running
  `--changed` together pushed the dev box to load 55 and failed every timing test, so a run that has
  steps to execute first takes `~/.cache/hams-lint/run.lock` (a flock fronted by the FIFO ticket queue of
  `tools/lock_queue.py`; a dead holder or waiter is detected by pid and start time, nothing goes stale).
  A waiting run prints `waiting for the lint run in <worktree> (pid N), position K`. Then a **load gate**
  holds the start until the 1-minute load average is at most the number of cores (default wait 900 s, then
  exit 75 with the reason). Runs that need neither: every step a cached pass, or uncached steps whose
  recorded durations total at most `HAMS_LINT_CHEAP_SECONDS` (default 60; a step never measured counts as
  heavy), so `--only check_summation_bias` never waits. Controls: `--lock-timeout S` /
  `HAMS_LINT_LOCK_TIMEOUT` (exit 75), `--lock-status` (holder and queue), `--no-lock` /
  `HAMS_LINT_NO_LOCK=1` (CI; no lock, no gate), `--load-limit N` / `HAMS_LINT_LOAD_LIMIT`,
  `--load-timeout S` / `HAMS_LINT_LOAD_TIMEOUT`; the gate uses the same `HAMS_SKIP_CI_LOAD_GATE=1`,
  `HAMS_CI_LOAD_GATE_TIMEOUT` as `test.py`'s CI load gate (`HAMS_CI_LOAD_GATE_RATIO` is ignored by `test.py` with a warning unless `HAMS_CI_LOAD_GATE_LEGACY=1`; do not copy it from old command lines). Do not start a
  second heavy run by other means (no `nohup` loops around `pgrep`): queue, and spend the wait on something
  else. `docs/LINT_STEP_ANALYSIS.md` lists what each step costs and overlaps.
  **The dev box is the primary lint runner** (Bruce, 2026-11-08: 32 cores and 125 GB are not limited by the smaller machines). Every
  step runs here, including `check_cargo_fmt`, `check_cargo_clippy` and `check_cargo_deny` (rustup's pinned toolchain with rustfmt and
  clippy is in `~/.cargo/bin`, which `run_linters.py` puts first on PATH; `/etc/profile.d/hams_test_environment.sh` does the same for
  shells). `lint_slots` in `lint_machines.json` (4 on the dev box) lets that many heavy runs go at once, each in a lock file of its own
  (`~/.cache/hams-lint/run.lock`, `run.lock.slot-K`), at `nice` 10, with its own TMPDIR (slots above 1: `$TMPDIR/s<K>`) and its own warm Cargo
  target directory (`hams-lint-cargo/<repo>-<toolchain>-sK`); a run beyond the last slot queues in the one FIFO, and the load gate delays a start while the box is saturated: it is test.py's rule (CPU busy >= 92 % AND pressure `some avg10` >= 25 %, `HAMS_CI_LOAD_GATE_CPU_BUSY_PERCENT` / `HAMS_CI_LOAD_GATE_PSI_PERCENT`), not the load average (86 at 18 % stall on this box), unless a load limit is set explicitly. The slot count is `HAMS_LINT_SLOTS`, else the first line of `/opt/hams/etc/lint_slots` or `~/.config/hams-lint/slots`, else `lint_slots` of the inventory; each run's default `-j` is half the CPUs divided by the slots (`--lock-status` lists
  the slots and the queue). A run is shipped to another machine **only as overflow**: when every slot is taken (or a run waits), the *whole* run goes to the best idle runner (the one that can run the most of its steps; least loaded on a tie), under
  that machine's own lock and gate, with output as `[host] ...`; the steps that need this checkout (`check_summation_bias`, `check_pip_audit`,
  `check_claims_freshness`, `check_cargo_deny`, `check_noupdate_edits`, `pytest tools/[host-bound]`) stay and, when they total at most
  `HAMS_LINT_RESIDUE_SECONDS` (240), wait only for the load gate. A per-machine claim (`~/.cache/hams-lint/claims/HOST.lock`) keeps two runs here from
  choosing the same idle machine; if every machine is claimed, busy or unreachable the run waits for a slot here, so it always ends with
  a real result and a pass stamp (the old "claimed host, fall back without clippy, no stamp" path is gone: this box has the full
  toolchain). Nothing is sent away merely because a machine is idle (the spreading of 60 s steps and `--distribute always` were removed);
  `--distribute off` (or `HAMS_LINT_DISTRIBUTE=off`) never ships; `--remote HOST` names a machine; `--where` shows the plan.
* Steps over several test directories are merged where that is provably safe (one daemon, one interpreter:
  `pytest daemons/hams_local_relay`); the old step names are aliases that `--only` still accepts. The repository-pair
  checkers (`check_async_lock_scope`, `check_fake_network_listener`, `check_int_float_overflow`,
  `check_float_isfinite`, `check_window_fetch_reassignment`) are one step each over hams_com and hams_open, as is
  `check_boundary_hazards` (credential header through urllib without a redirect refuser, raw command line in a log call, public
  `@api.model` method acting as a service account with no caller check, truncated `gh --json files`, unchecked outside number into
  `gmtime`/`fromtimestamp`; fix, `# boundary-hazard-ignore: <rule>: <reason>`, or `tools/boundary_hazards_known.json` with an owner:
  `tools/README.md` "Boundary hazards").
  `tools/lint_step_inventory.py compare` proves a restructuring kept every test (`docs/LINT_STEP_ANALYSIS.md`).
* **Remote execution** (`--remote auto|HOST|local`, `--where`; `tools/lint_remote.py`,
  `tools/lint_machines.json`). `--remote auto` runs everything on the dev box and uses the other lint runners only as overflow (above);
  `--remote HOST` sends each heavy step that machine can run (>= 60 s; never a step that needs git history, the network, this
  checkout or this login session) there. Runners are probed over ssh (read-only: cores, load, which of python3, flake8, pytest, xdist, node,
  eslint, cargo with clippy and rustfmt, chrome, odoo exist). `--where` prints the routing plan and runs nothing. Machines (versions are recorded in the inventory):
  **bruce-yoga** (Debian x86-64 build server, 8 cores, 4 workers at nice 10; ssh with `-o
  HostName=bruce-yoga.lan`; stays awake on AC, on battery it may suspend and shows as unreachable),
  **mac1-vm** (the Debian arm64 Lima VM on mac1, 3 workers at nice 19; it also runs the production FCC ULS
  sync, never started by the linter), **bruce17** (Debian x86-64 laptop, 4 threads, 7.6 GiB; ssh as `ai@bruce17.lan` with the key path named in the inventory; cargo uses the git command line, `CARGO_NET_GIT_FETCH_WITH_CLI`, because libgit2 ignores the `github-hams_shared` Host alias). Not runners, with reasons in the inventory: mac1 itself (Bruce's Mac,
  python 3.9), `bruce-yoga-win` and `Bruce_Thinkpad` (Windows), and **hams1 and the router (`never`)**.
  * **Provisioning is a script, not hand work**: `python3 tools/lint_remote.py provision HOST` prints the
    plan, `--apply` installs what is missing (`tools/provision_build_host.sh`, run on the machine; apt
    packages with `sudo -n`, everything else in the user's home: rustup with the toolchain pinned by the
    repositories' `rust-toolchain.toml`; the Python test stack (pytest, xdist, flake8, hypothesis, mcp, numpy,
    cryptography, pyopenssl, playwright and their dependencies) from `tools/requirements-test.txt`, every wheel
    pinned by SHA-256 for both x86-64 and aarch64; Debian's own `python3-*` libraries, including
    `python3-typeshed` (the stubs mypy needs) and `python3-pyinotify` (no wheel exists); and, downloaded and
    refused unless their SHA-256 matches, kopia, the Odoo nightly `.deb` the dev box runs (service left
    disabled) and PostgreSQL 18 from apt.postgresql.org with its key fingerprint checked, plus Go 1.24),
    `--verify` proves it can `cargo test`, run `node --test`, `pytest -n` and take a headless
    Chrome screenshot, `--versions` prints the versions for the inventory, `--undo` removes exactly what
    `--apply` recorded. It is idempotent. A new machine needs only an `ai` account with a key and `sudo -n`.
  * A machine holds a persistent mirror `~/hams-lint-mirror/{hams_com,hams_open}` (the tracked tree of HEAD
    including `export-ignore` files, incremental from the last synced commit, plus uncommitted regular files;
    never `.git`, `target/`, symlinks or secrets; `node_modules` of hams_shared is installed there by
    `npm ci`) and runs the steps through its own run lock and load gate. Output streams back as `[host] ...`.
  * The pass stamp is written locally only when every step passed (here or there) for the commit that is
    still HEAD, with `hosts=` metadata. A machine that is unreachable, busy, lacks a tool, or whose results
    cannot be read does not fail the run: its steps run here. **A step that fails on a machine is run
    again here** before it counts (the dev box is the reference environment; a remote failure is often a
    package version the machine lacks), so a remote failure never stamps and never blocks a step that
    passes here; if you see `reported failing steps ... running them here to confirm` often, the machine
    needs provisioning (`provision HOST --verify`; both runners have had every Python package, Odoo,
    PostgreSQL, kopia and Go the steps need since 2026-11-03, so only the steps that must stay local fall back).
    The mirror's hams_shared is a git repository of its own and a gitlink of the mirror's hams_open, as on the
    dev box (before 2026-11-03 hams_open's mirror also listed hams_shared's files, which made
    `check_rust_function_test_anchors` fail on every runner). The siblings (hams_open or hams_com, and hams_shared)
    go as they are on this machine.
* **pytest-xdist** (test-only dependency, approved by Bruce 2026-11-02, `tools/requirements-test.txt`): a
  `pytest ...` step runs as `pytest -n N --dist loadfile` when its interpreter can import `xdist` (N = cores
  divided by the steps running side by side, 2 to 4; a file's tests stay on one worker, so module-level state
  and the report per module are unchanged; the pass cache is unaffected). `HAMS_LINT_XDIST=0` turns it off, `=N`
  forces N workers. Groups that are not xdist-safe are listed in `lint_exec.XDIST_SERIAL`: `pytest tools/[host-bound]`
  (copies the tree, drives the real checkout), `pytest daemons/hams_simulated_bots` (idle-CPU and timing tests),
  and `pytest ingest/` and `pytest scripts/` (not validated yet). `pytest devbox_tools/` runs under `-n` since 2026-11-08 (294 s serial at load 150, 51 s with four workers). All 46 other daemon steps and the four
  `tools` chunks passed under `-n` on mac1-vm with the same results as serial.
