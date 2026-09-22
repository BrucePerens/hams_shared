# Architecture Decision Records (ADRs)

This directory contains the Architecture Decision Records (ADRs) that define the structural, security, and operational mandates for the platform.

## Master ADRs (Consolidated Paradigms)

* [MASTER 01: Security & Zero-Sudo Architecture](MASTER_01_SECURITY_ZERO_SUDO.md)
  Enforces the Service Account pattern, web isolation for daemons, strict limitations on `.sudo()`, View Abstraction, and OS-level Daemon Restrictions.
* [MASTER 02: Data Privacy, Location & Retention](MASTER_02_DATA_PRIVACY_RETENTION.md)
  Dictates GDPR erasure procedures, immutable public RF records, and geographic fuzzing.
* [MASTER 03: Edge Routing & Threat Mitigation](MASTER_03_EDGE_ROUTING_THREAT_MITIGATION.md)
  Defines Cloudflare edge orchestration, proactive caching, WAF bot verification, and dynamic Nginx tarpitting via silent honeypots.
* [MASTER 04: Modularity & Shared Services](MASTER_04_MODULARITY_SHARED_SERVICES.md)
  Mandates centralizing shared logic and Service Accounts into the `core_base` module to prevent monolithic cross-module entanglement.
* [MASTER 05: SWL Lifecycle & Automated Progression](MASTER_05_SWL_LIFECYCLE.md)
  Defines the SWL sandbox and automated correlation heuristics for licensing upgrades.
* [MASTER 06: DNS CQRS Architecture](MASTER_06_DNS_CQRS.md)
  Isolates DNS read infrastructure from Odoo state using RabbitMQ and PowerDNS SQLite.
* [MASTER 07: Zero-DB Architecture](MASTER_07_ZERO_DB_ARCHITECTURE.md)
  Prevents database bloat by caching real-time ephemeral data in Redis and broadcasting via WebSockets.
* [MASTER 08: Core Architecture & Performance](MASTER_08_CORE_ARCHITECTURE_PERFORMANCE.md)
  Details the hybrid monolith-daemon structure, distributed Redis caching, asynchronous bastions, and bounded chunking.
* [MASTER 09: API Integrations & Cryptography](MASTER_09_API_INTEGRATIONS.md)
  Defines HMAC Zero-Knowledge proofs, idempotency, ethical crawling, and strict headless API conventions.
* [MASTER 10: Core Identity & Access Control](MASTER_10_IDENTITY_ACCESS_CONTROL.md)
  Outlines the Proxy Ownership pattern, domain sandbox mandates, and secure admin password management.
* [MASTER 11: Agile Development & Documentation Workflow](MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md)
  Requires Semantic Anchor traceability, conversational documentation, fail-fast and Just-In-Time (JIT) dependencies, and strict log hygiene.
* [MASTER 12: QA & Automated Testing Mandates](MASTER_12_QA_TESTING_MANDATES.md)
  Enforces fast-fail CI/CD pipelines, deep AST test verification, strict syntactic parsing, and real-transaction testing methodologies.
* [MASTER 13: Frontend UX & Accessibility](MASTER_13_FRONTEND_UX.md)
  Governs ARIA live-regions and OLED burn-in protection for dashboards.
* [MASTER 14: LLM Context & Cognitive Load Management](MASTER_14_LLM_CONTEXT_MANAGEMENT.md)
  Establishes rules for AI agents, including prompt engineering, API contracts, and patching protocols.
* [MASTER 15: Domain Identity & Verification](MASTER_15_DOMAIN_IDENTITY.md)
  Defines identity verification fallbacks and the Shadow Profile indexing pattern.
* [MASTER 16: Financial Data Protection & Defense-in-Depth](MASTER_16_FINANCIAL_DATA_PROTECTION.md)
  Mandates multiple layers of security and mandatory SQL view masking to prevent financial identifier leakage through Odoo's ORM.

## Standard ADRs

* [ADR 0073: Fail-Fast Dependency Resolution](0073_fail_fast_dependency_resolution.md)
  Mandates that all external Python dependencies must be declared in module manifests to trigger immediate startup halts instead of silent runtime failures.
* [ADR 0074: User-Facing Semantic Anchors and Context-Sensitive Help](0074_User_Facing_Semantic_Anchors_and_Context-Sensitive_Help.md)
  Governs the injection of help documentation dynamically into views via system parameters.
* [ADR 0075: LLM Dependency Contract Visibility](0075_llm_dependency_contract_visibility.md)
  Mandates that external Python dependencies be explicitly listed in LLM API contracts to ensure AI agents can properly mock and integrate them across the repository.
* [ADR 0076: UI Tour Mandate and Bypass Governance](0076_ui_tour_mandate_and_bypass_governance.md)
  Defines the strict criteria for when JavaScript UI Tours are mandatory ("The Gold Standard") and when the `burn-ignore-tour` tag is architecturally justified.
* [ADR 0079: Hostname Resolution and Environment Fallbacks](0079_hostname_resolution_and_environment_fallbacks.md)
  Bans hardcoding 127.0.0.1 and enforces a two-argument environment variable fallback to "localhost" for service hostnames to ensure portability across Docker and bare-metal environments..
* [ADR 0086: Own-Model Extension Consolidation](0086_own_model_extension_consolidation.md)
  Requires code for an in-house model to live in that model's own base file rather than a separate cross-module `_inherit`, bans cross-module extension of `_auto = False` SQL-view models outright, and exempts genuinely optional/pluggable modules where merging would create a dependency cycle.
* [ADR 0087: Third-Party Dependency Version Tracking](0087_third_party_dependency_version_tracking.md)
  Splits code we don't control the version of into three shapes (framework-vendored silent drift, vendored-by-copy silent staleness, external OS binaries) and mandates a version-check-and-warn, calendar check, or startup capability probe respectively -- never a hard failure.
* [ADR 0088: HTML as the Interoperability Format for Complex Documents; PDF Emission Deprecated](0088_html_over_pdf_for_generated_documents.md)
  Standardizes on HTML (with real `@media print` CSS) as the canonical format for generated documents; deprecates server-side PDF generation (`wkhtmltopdf`) in favor of client-side browser print or on-demand headless-Chromium rendering. Governs emission only -- accepting PDFs sent to us is unaffected.
* [ADR 0089: Anchor Scheme Robustness Against Reformatting](0089_anchor_scheme_reformatting_robustness.md)
  Fixes `check_burn_list.py`'s single-line ignore-tag checks to scan the flagged AST node's full `lineno`..`end_lineno` span instead of one exact line, and adds an explicit multi-line `[@ANCHOR-BEGIN:]`/`[@ANCHOR-END:]` anchor form (alongside the still-supported single-line `[@ANCHOR:]`) so a test/documentation anchor can claim an arbitrary body of code, immune to reformatting.
* [ADR 0090: Universal Function Test-Anchor Ratchet](0090_universal_function_test_anchor_ratchet.md)
  Every function must carry a real test anchor (documentation is routed by audience -- README.md for infrastructure, docs/stories or data/documentation.html for user-visible features -- never exempted); a CI-enforced ratchet (`check_function_test_anchors.py`, run_linters.py step 38) blocks new unanchored code starting now, with the real pre-existing backlog (1193 functions, both repos) grandfathered into a committed baseline pending the full sweep.
* ADR 0091: Function-Level Claims Directory and Bug-Hunt Integration -- **relocated to
  `hams_com/docs/adrs/0091_function_level_claims_and_bug_hunt_integration.md`** (2026-09-09).
* [ADR 0094: Server-Gated Test-Only Hooks in Client-Shipped JavaScript](0094_js_test_only_hook_gating.md)
  Mandates that a `TEST_*`-prefixed postMessage/event hook in client-shipped JS be gated behind a real, per-deployment `ir.config_parameter` substituted server-side into a `TEST_HOOKS_ENABLED` constant -- never Odoo's own `test_enable` flag, which is banned for this purpose. Enforced by a new CI linter (`check_js_test_hook_gating.py`, run_linters.py step 46) via a real acorn AST scan, no baseline/ratchet needed at introduction.
  `hams_shared` is a public repo; the bug-hunt review method itself (the claim-then-verify
  technique, the accumulated bug-class checklist, the cost-tiered model-routing strategy) is a
  competitive/trade-secret asset and doesn't belong here. What DOES stay in this public repo and
  still applies to both `hams_com` and `hams_open`: the mechanical, content-free plumbing --
  `check_claims_freshness.py` (hash-based freshness checking, no method narrative) and
  `docs/odoo_orm_reference.md` (generic third-party Odoo-framework facts). A per-function claim
  itself still lives at `<module>/claims/<anchor_name>.md`, inside whichever repo owns that
  module -- that part of the design is unchanged, only the review method's own document moved.
* ADR 0092: HamCall Licensed Callsign Verify-Only Integration -- lives at
  `hams_com/docs/adrs/0092_hamcall_licensed_verify_only_integration.md` (written there directly,
  matching ADR 0091's relocation above, not moved after the fact). `hams_shared` is a public repo
  under Bruce Perens' own name; this ADR names a specific commercial vendor (HamCall/hamcall.net),
  documents an ongoing effort to reverse-engineer that vendor's proprietary file format
  (`hamcall.dat`), and describes integrating that vendor's licensed, confidential data -- all of
  which is real vendor/legal exposure regardless of whether the underlying DATA ever leaks, the
  same reasoning ADR 0091's relocation note already establishes for a different kind of sensitive
  content. Governs a `hams_com`-only module (`hamcall_verify`) and daemon
  (`daemons/hamcall_idx_sync`) plus a Postgres-facts-only cross-reference back to ADR-0083 (still
  public, unaffected) -- nothing in this repo's own mechanical tooling needs to know HamCall exists.
* [ADR 0093: Third-Party Data Sovereignty Against Vendor Cutoff](0093_third_party_data_sovereignty.md)
  A commercial (non-government) third party may withdraw API access at any time, for reasons outside
  our control -- splits these dependencies into data-source (ingest and persist locally, don't just
  proxy live) and capability/verification (diversify across independent paths, never single-source a
  trust decision) shapes, each needing a different defense. `ham_onboarding`'s existing multi-method
  identity verification (LoTW, QRZ, official-email OTP, Morse challenge, AI/admin license review) is
  a real, already-built instance of the capability-diversification half of this decision.
* [ADR 0095: Third-Party Credentials Stay on the User's Own System](0095_third_party_credential_locality.md)
  A third-party credential a user entrusts to this platform (LoTW/eQSL/AllStar/EchoLink/ClubLog
  passwords, and any future one) is held on the user's behalf, not this platform's to use freely --
  prefer local use (the user's own hardware) over central decryption where the real functional need
  allows it; where central decryption is genuinely necessary, scope it to one specific, confirmed
  real consumer via `_get_service_uid()`, never a blanket `is_service_account` check; decrypt only
  at the moment of legitimate use; never let a decrypted credential reach a log or side channel.
  Prompted by a real over-broad `ham_logbook` trust-boundary finding -- that field's own end-to-end
  audit against this ADR is tracked in `night_shift_todo.md`, not restated here.
* [ADR 0096: `res.config.settings` Authorization Is All-or-Nothing, By Design](0096_res_config_settings_authorization_model.md)
  `res.config.settings` write access is `base.group_system` only, full stop -- no module may grant
  a different group model-level access to this shared `TransientModel`, since `ir.model.access.csv`
  grants are per (model, group), never per field, so there is no such thing as a narrower delegation
  here. `set_values()`/`get_values()` overrides may only persist independent scalar
  `config_parameter` values, never a side effect on another model, because this method fires
  unconditionally on every installed module's Settings save. Delegating a narrower administrative
  role goes through Odoo's own Users & Groups mechanism instead. Prompted by a real `user_websites`
  privilege-escalation hole (a content-moderation role held read/write on every module's Settings
  fields, including other modules' credentials) found alongside the crash that originally motivated
  this ADR; both are recorded in `night_shift_todo.md`.
* [ADR 0097: No Ham Radio Function Depends on Our Server](0097_server_independent_relay_operation.md)
  Given `hams_local_relay` installed and the remote endpoint directly reachable over the Internet,
  controlling your own radio, a friend's/club's authorized radio, or any repeater you're authorized
  for must all work with hams.com's own server completely unreachable -- only logging queues and
  syncs later. Names a real, unresolved tension (locally-cached authorization for offline
  availability vs. timely revocation) rather than papering over it, and sets a standing review bar
  for all future features: does this need hams.com up at the moment of use, and why.
* [ADR 0098: Public-Key Friend/Club Authorization and Direct Relay Connectivity](0098_public_key_authorization_direct_connectivity.md)
  Resolves ADR 0097's own named tension concretely: friend/club timeshare authorization moves from a
  live per-connection Odoo lookup to a locally-verified public-key credential (extending the relay's
  already-real Noise/Ed25519 attestation pipeline, not a new crypto scheme), hams.com's own relay
  infrastructure becomes a fallback for when direct relay-to-relay connectivity genuinely isn't
  available rather than the default path, and IPv6 is a first-class connectivity preference given how
  much less it relies on NAT than IPv4.
* ADR 0099: Commit Discipline in a Working Tree Shared by Concurrent Agents -- **retired
  2026-09-22, superseded by ADR 0102.** Its private-`GIT_INDEX_FILE` discipline only ever protected
  the commit step; it never protected the shared working tree itself, and one session's
  `git checkout-index -a -f` destroyed another's uncommitted work despite it. File deleted; the
  decision record lives in git history only.
* [ADR 0100: A Shared `localhost.hams.com` Certificate, With Its Private Key On Every Relay](0100_shared_localhost_certificate_on_every_relay.md)
  Bruce's 2026-09-20 decision to build both certificates: the relay keeps its per-user `u<id>.local.hams.com` certificate and also serves one shared, centrally renewed `localhost.hams.com` certificate to a browser on the same machine, chosen by SNI. The shared key sits on every relay, which is acceptable because a copied TLS key cannot impersonate a relay (pinned key plus Noise handshake); the key lives in files, never the database, and is distributed over one authenticated, rate-limited, TLS-only route. The relay keeps serving its last good copy with hams.com down (ADR 0097).
* [ADR 0101: AllStar SIP Credentials Are Synced to the User's Own Browsers (Bounded Exception to ADR 0095)](0101_browser_synced_allstar_sip_credentials.md)
  Bruce's 2026-09-21 decision for the browser-direct AllStar path (no relay, no hams.com in the audio or signalling path): the user's own AllStar SIP password is kept encrypted at rest on the server and synced to their own browsers, because offline operation (ADR 0097) needs the browser to hold it. Mitigations: one narrow owner-only, `no-store`, rate-limited, TLS-only endpoint; service accounts refused; browser copy in IndexedDB under a non-extractable AES-GCM key; never in localStorage, URLs or logs; kept across logout unless the user chooses "forget on this device".
* [ADR 0102: Concurrent-Session Isolation and Merge Gating (Replaces ADR 0099)](0102_concurrent_session_isolation_and_merge_gating.md)
  Replaces ADR 0099 after its private-index discipline let one session's `git checkout-index -a -f` destroy another's uncommitted work: per-session `git worktree`s instead of a shared checkout, so a destructive local command only ever touches its own copy; real server-side branch protection on both repos (`enforce_admins: true`, unconditional required review) instead of a client-side procedure, closing the admin-bypass hole a security test had already found; a dedicated local script (not GitHub Actions -- standing policy) gives night-watch's own `night-shift/*` branches a fast merge path using a second, distinct credential, while `hams_helpdesk` ticket-triage's `ai-triage/*` PRs are excluded by construction and always reach a human; and an hourly sweep for worktrees orphaned by a crash rather than a graceful exit.
