# MASTER 04: Modularity & Shared Services

## Status
Accepted (Consolidates ADRs 0014, 0021, 0045, 0046)

## Context & Philosophy
Cross-module dependencies create monolithic entanglement, making the platform brittle and difficult to test. Shared logic and services must be centralized and abstracted to ensure isolated testability and DRY (Don't Repeat Yourself) compliance.

## Decisions & Mandates

### 1. Shared Service Account Centralization
When a Service Account, security group, or foundational utility is used by two or more sibling modules, it MUST be migrated to the `core_base` module. Higher-level modules reference this shared service securely without creating lateral dependencies.

### 2. Code Deduplication Mandate
Any execution logic (e.g., API authentication, math utilities) duplicated across modules MUST be proactively abstracted into `core_base` (e.g., `core.math.utils`).

### 3. Centralized Reverse Traceability
Any utility or Service Account hosted in `core_base` MUST include a `CONSUMERS:` block in its docstring.
* This block explicitly lists every active usage across the platform using Semantic Anchors (`[@ANCHOR: example_name]`). The text `example_name` is only a placeholder for the anchor id of a real consumer, written in the same `module:id` form the verifier reads (see MASTER 11). A `CONSUMERS:` block therefore has one anchor per calling function or test, so a search for a core utility's anchor finds every user of it. The block lives in the docstring, next to the code it describes, and must be updated in the same pull request that adds or removes a consumer.
* Developers modifying core utilities MUST consult this block to understand downstream impacts and prevent regression.

## Status check 2026-11-12

Checked against the repositories. There is no module named `core_base` and no `core.math.utils`, and no docstring carries a `CONSUMERS:` block. The shared-foundation role this ADR gives
`core_base` is held in practice by `zero_sudo` (hams_open: service accounts, the Zero-Sudo utilities, security groups) and, for hams_com, `ham_base`; sibling modules depend on those rather than on each other.
Read "`core_base`" above as "the shared foundation module" until the name is settled. The `CONSUMERS:` traceability rule is not applied and no linter checks it; the anchors in each module's
README (ADR 0090, ADR 0113) are what trace a utility to its callers today.
