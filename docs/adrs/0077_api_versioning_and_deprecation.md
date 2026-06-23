# ADR 0077: API Versioning and Deprecation Strategy

## Status
Accepted

## Context
As the platform evolves toward Phase 2 (AI Simulator), existing REST APIs and local hardware relays require a stable interface to prevent regressions in connected third-party tools. [@ANCHOR: UX_API_VERSIONING_POLICY%]
Currently, endpoints are loosely versioned within the URI (e.g., `/api/v1/`). However, there is no formal policy governing how new versions are introduced or how legacy endpoints are sunset.

## Decision
We mandate a strict versioning and deprecation lifecycle for all HTTP/JSONRPC controllers.

### 1. Semantic Versioning in URIs
All public or integration APIs MUST utilize a major version prefix in the URI: `/api/v{major}/`.
* **V1 (Stable):** Core logbook, callbook, and hardware relay functions.
* **V2 (Experimental/Phase 2):** AI Simulator, RAG memory extraction, and STT/TTS streams.

### 2. The "Sunset" Header Protocol
When an endpoint is marked for deprecation:
1. The controller MUST inject a `Sunset: {date}` HTTP header in all responses.
2. The documentation MUST be updated with a "DEPRECATED" warning linked to the replacement anchor.

### 3. Deprecation Windows
* **Local Hardware Relays:** Due to the difficulty of updating user-installed desktop software, V1 hardware relay endpoints have a **12-month** sunset window.
* **Third-Party Integrations:** REST APIs for ADIF uploads/downloads have a **6-month** sunset window after the release of a stable V+1 replacement.

### 4. Breakage Prevention
A version MUST NOT be removed from the codebase until the CI/CD pipeline verifies that zero active production log entries (via `ir.logging`) have hit that version's route in the last 30 days.

## Consequences
* **Third-Party Stability:** External desktop loggers and hardware relays will remain functional during major platform upgrades.
* **Controlled Evolution:** Developers can iterate on Phase 2 AI features in V2 without risking V1 logbook integrity.
* **Operational Visibility:** Sunset headers provide proactive notification to developers of external tools.
