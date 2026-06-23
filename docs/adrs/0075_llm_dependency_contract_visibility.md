# ADR 0075: LLM Dependency Contract Visibility

## Status
Accepted

## ## Context
## AI agents rely strictly on Markdown API contracts (`docs/modules/*.md` and `README.md`) to understand system boundaries..
While ADR 0073 enforces fail-fast dependency resolution at the manifest layer (`__manifest__.py`), LLMs lack visibility into these requirements if they aren't explicitly documented in the markdown contracts. This causes inter-repository use and AI code generation to break (e.g., failing to import or mock external libraries like `redis` or `ephem` in test suites).

## Decision
All `README.md` files and their corresponding `docs/modules/*.md` API contracts MUST include a standardized `External Dependencies` section.
This section must explicitly list the `external_dependencies` defined in the module's `__manifest__.py`.

## Consequences
* **AI Resilience:** AI agents will accurately mock required libraries during test generation without hallucinating standard libraries.
* **Cross-Repository Integration:** Developers and AI agents will immediately understand the environmental prerequisites of a module without needing to inspect its raw Python source code.
