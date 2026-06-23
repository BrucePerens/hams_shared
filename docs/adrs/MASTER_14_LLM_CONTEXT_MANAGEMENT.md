# MASTER 14: LLM Context & Cognitive Load Management

## Status
Accepted (Consolidates ADR 0016, 0072, Patch Protocol, API Contracts)

## Context & Philosophy
The platform is governed by a massive edifice of operational rules, linters, and architectural constraints. If an LLM is fed the entire repository alongside these meta-rules, its attention dilutes, leading to instruction drift, hallucination, and security regressions. We must aggressively prune input and output context to preserve reasoning capacity.

## Decisions & Mandates

### 1. LLM Instruction & Prompt Engineering Standards
LLMs natively possess generic base instructions (e.g., "always bundle all code," "use a friendly tone"). To overcome this internal conflict:
* **The "SYSTEM OVERRIDE:" Directive:** Meta-instructions must utilize the explicit `SYSTEM OVERRIDE:` prefix to force the LLM to abandon its base programming in favor of our architectural constraints.
* **Positive Constraints:** Instructions MUST utilize deterministic positive framing (stating exactly what the LLM *must* do) rather than negative framing, which increases hallucination probabilities.
* **Persona Framing:** System instructions must explicitly declare the LLM's role as a "rigid technical executor" to naturally suppress conversational filler.

### ### 2. API Contracts Over Implementations
### When instructing the LLM to interact with core frameworks, do not feed it the raw Python implementation files.
### * Use the Markdown API contracts located in `docs/modules/`.
### * **Explicit Import Paths:** API contracts and `README.md` files MUST explicitly provide the exact, literal Python import path. LLMs are strictly forbidden from guessing internal filenames.
### * **Dependency Visibility (ADR 0075):** API contracts MUST explicitly list all external Python dependencies (e.g., `redis`, `ephem`) to ensure AI agents correctly mock and utilize them across the repository.

### ### 3. Targeted Directory Ingestion
### Context bundling tools MUST NOT be run against the repository root for execution tasks. Developers MUST pass specific subdirectory targets to strip irrelevant domain logic from the prompt.

