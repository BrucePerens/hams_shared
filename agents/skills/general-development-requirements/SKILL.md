---
name: general-development-requirements
description: >-
  The general, cross-cutting development conventions that odoo-development and ham-requirements
  both extend -- end-user documentation via knowledge_docs, the general WCAG accessibility
  baseline, and where Semantic Anchors and other cross-cutting standards actually live. Load this
  when you need the "parent" a module-specific or domain-specific requirements skill refers to.
version: 1
---

# General Development Requirements

Both `odoo-development/SKILL.md` and `hams_com/agents/skills/ham_training/ham-requirements/SKILL.md`
say they "extend" a general requirements document -- historically `LLM_GENERAL_REQUIREMENTS.md`, a
file from an earlier, pre-repo phase of this project that was never carried into either git-tracked
repo. This skill is that real, current parent, rebuilt from what's actually still true today rather
than transcribed from the old file. Per Bruce's own instruction, 2026-09-15: "Make it a skill if it
still has relevant data. Remove the old LLM_* files once you are sure they are duplicated in current
skills." Most of the old file's content turned out to already be duplicated by real, current
conventions (see "What's covered elsewhere" below) -- this skill holds only what wasn't.

## End-user documentation: `knowledge_docs`

Any new module with user-facing features gets real end-user documentation, discoverable inside the
app itself, not just a developer-facing README. The current, verified mechanism (confirmed against
27 real modules doing this, 2026-09-15):

- Write the documentation as `data/documentation.html` inside the module.
- Declare it in `__manifest__.py`:
  ```python
  "knowledge_docs": [
      {
          "name": "Your Module's Manual",
          "path": "data/documentation.html",
          "icon": "📡",
          "category": "workspace",
      }
  ],
  ```
- That's it -- no `post_init_hook` or `hooks.py` needed for this specifically (an older draft of
  this convention described one; it's not what current modules actually do, confirmed by checking:
  only one module in the whole codebase has a `hooks.py` at all, and its `post_init_hook` does
  something unrelated -- admin bootstrap, daemon key registration, i18n loading -- not
  `knowledge_docs`). The `knowledge` module's own install/upgrade path picks up the manifest key
  directly.
- Real modules test that this documentation is actually reachable and public where it should be
  (see e.g. `ham_shack/tests/test_help_manual_public.py`, `ham_repeater_dir/tests/test_help_manual_public.py`)
  -- write the equivalent test for a new module's own manual rather than treating the manifest entry
  alone as "done."

## Accessibility: WCAG 2.1 AA baseline

The general baseline for any user-facing view: semantic HTML, `aria-label`s where a control's
purpose isn't obvious from its visible text, and full keyboard navigability. This is the default
every view should meet. Domain-specific skills can layer real, justified exceptions on top where the
baseline actively works against usability -- e.g. `ham-requirements/SKILL.md`'s own rule that a
rapidly-updating real-time grid (a DX cluster bandmap, a net control roster) must NOT use a bare
`aria-live="polite"`, since constant interruptions are actively hostile to screen readers; that skill
requires a "Pause Updates"/"Screen Reader Mode" toggle instead. An exemption like that supersedes
this baseline only for the specific case it names, not as a general license to skip accessibility.

## What's covered elsewhere (don't duplicate it here)

The old `LLM_GENERAL_REQUIREMENTS.md` also covered several things that turned out to already have
real, current, more-authoritative homes -- if you're looking for one of these, go there instead of
expecting this skill to define it:

- **Semantic Anchors** (`[@ANCHOR: name]`, the regression-prevention/documentation-linking
  convention): defined and evolved across real ADRs --
  `hams_shared/docs/adrs/0074_User_Facing_Semantic_Anchors_and_Context-Sensitive_Help.md`,
  `0089_anchor_scheme_reformatting_robustness.md`, `0090_universal_function_test_anchor_ratchet.md`
  -- with real enforcement tooling in `hams_shared/tools/` (`check_anchor_coverage.py`,
  `verify_anchors.py`, `check_function_test_anchors.py` and its Rust/JS equivalents).
- **Zero-sudo, service accounts, the Centralized Security Utility pattern, cron batching, ORM/model
  standards**: `odoo-development/SKILL.md` covers all of this in far more current detail than the
  old general doc ever did.
- **ADRs for major structural decisions**: `hams_shared/docs/adrs/` is the live, actively-used
  destination -- see the `hams-standing-policy-goes-in-adr-not-proposal` convention.
- **The burn-list linter** (`docs/LLM_LINTER_GUIDE.md`'s old role as "ultimate authority on syntax"):
  the real current authority is `hams_shared/tools/check_burn_list.py` and the AST-based linter it
  runs -- referenced directly by `odoo-development/SKILL.md`.

## What was dropped, and why

A few things in the old file were specific to an earlier Gemini/Antigravity-based workflow with its
own file-editing transport format and context-window constraints -- a "Patch Protocol"/"Transport
Terminator" boundary-string convention for that tool's own diff format, and an "Autonomous Chunking"
rule about splitting large outputs into batches and saying "continue." Neither applies to Claude
Code's own tool-based editing model, so they're not carried forward here. A specific Python
line-length number (70 characters) was also dropped -- no `.flake8`/`pyproject.toml` in this repo
currently enforces it, and reviving an unenforced, oddly-specific number as if it were a real
standard would be worse than saying nothing.

## Self-improvement

If you find another real, current convention that a domain-specific skill's own "extends" line
implicitly assumes but never actually resolves anywhere, add it here rather than letting the
reference keep dangling.
