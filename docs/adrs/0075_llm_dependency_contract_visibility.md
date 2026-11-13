# ADR 0075: LLM Dependency Contract Visibility

LLM (large language model) here means large language model, the kind of AI coding assistant that reads a project's code.

## Status
Accepted

## ## Context
## AI agents rely strictly on Markdown API contracts (`hams_shared/docs/modules/*.md` and `README.md`) to understand system boundaries..
While ADR 0073 enforces fail-fast dependency resolution at the manifest layer (`__manifest__.py`), LLMs lack visibility into these requirements if they aren't explicitly documented in the markdown contracts. This causes inter-repository use and AI code generation to break (e.g., failing to import or mock external libraries like `redis` or `ephem` in test suites).

## Decision
All `README.md` files and their corresponding `hams_shared/docs/modules/*.md` API contracts MUST include a standardized `External Dependencies` section.
This section must explicitly list the `external_dependencies` defined in the module's `__manifest__.py`.

## Consequences
* **AI Resilience:** AI agents will accurately mock required libraries during test generation without hallucinating standard libraries.
* **Cross-Repository Integration:** Developers and AI agents will immediately understand the environmental prerequisites of a module without needing to inspect its raw Python source code.

## Status check 2026-11-12

Checked against the repositories. Not yet true everywhere: of 28 modules that declare `external_dependencies` in `__manifest__.py`, 13 have a `README.md` without an "External Dependencies" section (hams_open: `edge_routing`,
`form_shield`, `hams_rabbitmq`, `pager_duty`, `ses_webhook`, `user_websites`; hams_com: `content_security_policy`, `ham_base`, `ham_classifieds`, `ham_member_certs`, `ham_rig_reports`, `ham_satellite`, `ham_training`), and 12 of the
23 files in `hams_shared/docs/modules/` lack it. No linter checks the section. The gap is recorded in the night-shift to-do for this audit.
