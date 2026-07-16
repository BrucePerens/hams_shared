---
name: code-review
description: How to execute a comprehensive codebase review using the divide-and-conquer architecture.
tools:
  - use_global_mcp: true
  - mcp:
      inherit_from: "parent"
      allow_all_global_servers: true
---

# Code Review

When asked to do a code-review, you MUST activate and follow the `divide-and-conquer` skill framework, utilizing the Nudge, Ignatz, Shamus, and Monitor architecture.

Provide the `divide-and-conquer` orchestrator with the following specific configuration parameters for this specialized topic:

## 1. Topic
**Odoo 19 Architectural Code Review & Linter Compliance.** The goal is to aggressively discover and fix architectural antipatterns, strict AST linter violations, and UX deficiencies across all Odoo modules.

## 2. Discovery Command
To discover the review targets during Phase 0, Ignatz must run:
`find hams_open hams_com -name "__manifest__.py" -not -path "*/.git/*"`
This will identify all Odoo modules. Additionally, scan `daemon/`, `daemons/`, `hams_shared/tools/`, `hams_shared/docs/`, and `hams_shared/agents/`.

## 3. Specialized Review Roles
Ignatz must assign reviews to the following three specialized roles (inject these into the reviewer prompts):

**Role 1: Compliance_and_Quality_Reviewer**
- **Required ADRs:** `MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md`, `MASTER_12_QA_TESTING_MANDATES.md`, `0073_fail_fast_dependency_resolution.md`, `0075_llm_dependency_contract_visibility.md`, `0076_ui_tour_mandate_and_bypass_governance.md`
- **Focus:** Linter improvement, Odoo 19 compatibility, AI foibles. **Licensing**: Verify `hams_com` code indicates it is proprietary/trade-secret (except "radae"), and `hams_shared`/`hams_open` is AGPL-3, checking for `SPDX-License-Identifier` headers.

**Role 2: Architecture_and_Security_Reviewer**
- **Required ADRs:** `MASTER_01_SECURITY_ZERO_SUDO.md`, `MASTER_03_EDGE_ROUTING_THREAT_MITIGATION.md`, `MASTER_06_DNS_CQRS.md`, `MASTER_07_ZERO_DB_ARCHITECTURE.md`, `MASTER_08_CORE_ARCHITECTURE_PERFORMANCE.md`, `MASTER_09_API_INTEGRATIONS.md`, `MASTER_10_IDENTITY_ACCESS_CONTROL.md`, `MASTER_16_FINANCIAL_DATA_PROTECTION.md`, `0083_multi_tenant_context_management.md`
- **Focus:** Security vulnerabilities, performance bottlenecks, latency reduction, and usage of SQL procedures/functions to reduce database turn-arounds.

**Role 3: Product_and_UX_Reviewer**
- **Required ADRs:** `MASTER_02_DATA_PRIVACY_RETENTION.md`, `MASTER_04_MODULARITY_SHARED_SERVICES.md`, `MASTER_05_SWL_LIFECYCLE.md`, `MASTER_13_FRONTEND_UX.md`, `MASTER_14_LLM_CONTEXT_MANAGEMENT.md`, `MASTER_15_DOMAIN_IDENTITY.md`, `0074_User_Facing_Semantic_Anchors_and_Context-Sensitive_Help.md`, `0081_ui_testability_and_tour_friendly_design.md`
- **Focus:** Architecture completeness, AI directive quality, UX consistency/attractiveness, documentation coverage, and testing quality.

**Required Pre-Reading:**
All roles must read the skill files for `linter-compliance`, `project-experience`, `odoo-development`, and `code-review/references/ai_antipatterns_guide.md`, along with the `hams_shared/docs/adrs/README.md` index.

## 4. Test-Driven Validation Workflow
During Phase 3 (Fix Application) and Phase 4 (Final Validation), Ignatz must follow this strict TDD protocol:
1. **Linter-First Validation:** Run `python3 tools/run_linters.py --files <list_of_modified_files>` on the specific changed files first. Do NOT run the test suite until the linter passes perfectly.
2. Run the full test suites: `python3 tools/test.py`
3. **NO DUMMY TESTS:** When satisfying `[@ANCHOR]` requirements, real functional tests are mandatory.

At the very end of the review process, run a full, global sweep in both repositories, fix any issues, and run the linter or test again. When done, should be no linter complaints, and 100% of tests should pass:
1. `cd hams_open && python3 tools/run_linters.py`
2. `cd hams_com && python3 tools/run_linters.py`
3. `cd hams_open && python3 tools/test.py`
4. `cd hams_com && python3 tools/test.py`
