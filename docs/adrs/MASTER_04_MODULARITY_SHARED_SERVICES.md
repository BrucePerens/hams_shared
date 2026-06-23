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
* This block explicitly lists every active usage across the platform using Semantic Anchors (`[@ANCHOR: example_name]`).
* Developers modifying core utilities MUST consult this block to understand downstream impacts and prevent regression.
