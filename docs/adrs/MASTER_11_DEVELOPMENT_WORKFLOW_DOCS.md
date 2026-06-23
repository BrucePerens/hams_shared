# MASTER 11: Agile Development & Documentation Workflow

## Status
Accepted (Consolidates ADRs 0004, 0007, 0016, 0043, 0055, 0056, 0073, 0077)

## Context & Philosophy
Maintaining architectural cohesion across a large platform relies on strict documentation traceability and minimizing developer (and AI) cognitive load. Documentation must remain perfectly synchronized with source code.

## Decisions & Mandates

### 1. Semantic Anchor Traceability & Multi-Repo Governance
* Source code and Agile documentation (Stories, Runbooks) MUST be mathematically linked using Semantic Anchors (`[@ANCHOR: example_feature_name%]`).
* **Multi-Repository Prefixing:** To prevent collisions across the platform's multi-repository architecture, all new anchors MUST utilize the following prefixing convention:
    * **`COMM_`**: Reserved for `hams_community`.
    * **`PRI_`**: Reserved for `hams_private_primary`.
    * **`UX_`**: Reserved for user-facing interaction anchors (See ADR 0074).
* **Inline Documentation Placement:** Within documentation files, anchors MUST be placed directly inline/adjacent to the specific text describing the functionality. A dedicated `ANCHOR_MANIFEST.md` file is strictly forbidden.
* The CI/CD pipeline scans for orphaned or missing anchors and fails the build if the mapping breaks.

### 2. LLM Context Management (See MASTER 14)
* To prevent instruction drift and cognitive overload, LLM interactions MUST strictly adhere to the Context Management mandates outlined in MASTER 14.
* This includes targeting API contracts over raw implementations and enforcing the Patch Protocol to minimize output token generation.

### 3. Clear, Conversational Writing Style
* "Oblique" AI tones, passive voice, and dense corporate jargon are strictly forbidden. All documentation MUST be written conversationally, directly, and plainly.

### 4. Documentation Boundaries
* `docs/runbooks/` holds strategic Standard Operating Procedures. It MUST NOT contain step-by-step CLI commands.
* `docs/deploy/` holds tactical deployment steps and CLI commands.
* **API Contracts & Versioning (ADR 0077):** Any technical documentation intended for LLMs MUST explicitly state the exact Python import paths. Public APIs MUST utilize URI versioning (e.g., `/api/v1/`) and implement the Sunset Header protocol for deprecation.

### 5. Solo-Maintainer Automation & SRE
* The platform MUST prioritize self-healing infrastructure, zero-touch CI/CD, and highly centralized unified moderation queues.
* **Fail-Fast Dependency Resolution:** All external Python libraries MUST be declared in the `external_dependencies` dictionary of a module's `__manifest__.py`.
* **JIT Self-Healing Dependencies:** Daemons and modules MUST implement Just-In-Time (JIT) binary resolution. If an expected OS-level package is missing, the Python code must dynamically download the static standalone executable from official sources.

### 6. No Cybercrud Policy (Log Hygiene)
* Repetitive, non-actionable warnings occurring inside high-frequency loops or controllers MUST use a "run-once" global boolean flag per WSGI worker to ensure they print exactly once to the logs and then fall silent.

### 7. Human Time vs. Machine Time (The Time Protection Mandate)
As a solo-maintainer, all architectural decisions MUST aggressively protect the administrator's time by shifting the diagnostic and operational burden to the system through exhaustive automated testing, shift-left data validation, and idempotent background jobs.
