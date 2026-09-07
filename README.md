# hams_shared

Shared developer tooling, CI/CD linters, architecture decision records, and Claude/AI agent skills
used identically by both [`hams_open`](https://github.com/BrucePerens/hams_open) (the open-source
Odoo 19 Community modules and LGPL-3.0-or-later amateur-radio digital-mode daemons) and `hams_com`
(the proprietary hams.com web site and infrastructure). This is a real, independent git repository
-- a submodule of each parent repo (`.gitmodules`), not just a subdirectory -- with its own commit
history and versioning, so a change here takes effect in both parent repos as soon as each bumps
its own submodule pointer.

**License:** AGPL-3.0-or-later by default (matching the license the majority of `hams_open`'s own
code uses), with one deliberate exception: `tools/odoo_mypy_plugin.py`, its test, and
`tools/odoo_type_stubs/odoo/*.pyi` are GPL-3.0-or-later -- see `hams_open/LICENSING.md` for why.
This is dev-time tooling, never deployed as a network service, so AGPL's network-copyleft clause is
largely moot here either way; the license choice still matters for anyone vendoring this code
elsewhere.

## How this repo is wired into each parent

`hams_open/AGENTS.md`, `hams_open/tools`, and `hams_open/docs` (and the equivalent paths under
`hams_com`, except `hams_com/docs`, which is proprietary content of its own) are symlinks straight
into this repo -- `AGENTS.md -> hams_shared/AGENTS.md`, `tools -> hams_shared/tools`,
`docs -> hams_shared/docs`. A script or doc path that looks like it lives directly under a parent
repo's own tree (e.g. `hams_open/tools/check_burn_list.py`) is very often actually this repo's
content, reached through that symlink. `check_burn_list.py`'s own `_is_odoo_module()` and several
other tools resolve symlinks explicitly (`os.path.realpath`) for exactly this reason -- a naive
`os.path.dirname()` walk over `__file__` gives the wrong answer once a symlink is in the path.

`run_linters.py` (the CI entry point both parent repos' pipelines call) accepts being invoked from
any of three roots -- `hams_open`, `hams_com`, or `hams_shared` itself -- and resolves the real repo
root and, where relevant, the sibling repo, from whichever one it's given.

## What's here

* **`AGENTS.md`**: the primary AI-agent instruction file for both parent repos -- persona and
  boundaries, universal technical standards, the pre-flight/anchor protocol, and the final
  verification checklist every change goes through.
* **`tools/`** (114 Python scripts plus `rust_function_scan/`, a small standalone Rust crate): the
  CI/CD linter and verification suite `run_linters.py` orchestrates. Highlights: `check_burn_list.py`
  (the AST-based security/architecture "Burn List" linter, ADR-0083/ADR-0022), `verify_anchors.py`
  and `check_function_test_anchors.py`/`check_js_function_test_anchors.py`/
  `check_rust_function_test_anchors.py` (the bidirectional Semantic Anchor System's enforcement,
  per-language), `run_rust_coverage.py` (real per-line Rust coverage via `cargo llvm-cov`, joined
  against anchor claims), `odoo_mypy_plugin.py` (Odoo-model-aware static typing, with a generated
  Odoo-core type-stub tree at `tools/odoo_type_stubs/`), `pre_flight_check.py`, `mcp_watchdog.py`
  (the shared MCP queue/executor coordination layer described in the `avoiding-api-costs` skill
  below), and dozens more single-purpose checkers, each with its own test suite.
* **`docs/adrs/`** (35 ADRs as of this writing): the formal Architecture Decision Record set --
  cross-cutting standing policy for both parent repos lives here, not in either repo's own
  `docs/proposals/`.
* **`agents/skills/`** (17 skill packages): reusable Claude Code skills shared by both repos'
  sessions, activated on demand rather than loaded unconditionally into every session. Three of
  these are where this repo's own former `docs/LLM_*.md` instruction files ended up when they were
  split out of always-loaded context: `linter-compliance` (ex-`LLM_LINTER_GUIDE.md`, the Burn
  List/anti-evasion reference), `odoo-development` (ex-`LLM_ODOO_REQUIREMENTS.md`, Odoo 19+
  architectural mandates), and `project-experience` (ex-`LLM_EXPERIENCE.md`, the AI's own
  cross-session lessons-learned log). The rest cover the night-shift workflow, Odoo
  testing/UI-tour conventions, headless-browser testing, multi-agent code review, and more.
  (`mcp_watchdog.py` above is the coordination layer behind the API-cost-avoidance pattern
  described in `hams_com`'s own `.claude/skills/avoiding-api-costs/` skill, which is not itself
  shared here.)
* **`scripts/`**: `run_headless_chrome.py`, a shared real-Chrome-plus-CDP harness used for
  JS-coverage and live DOM instrumentation work by both repos.
* **`eslint.config.js` / `package.json` / `package-lock.json`**: the shared ESLint configuration
  `run_linters.py` runs as a required (fail, not warn) CI gate against both repos' JS.
