# MASTER 02: Data Privacy, Location & Retention

## Status
Accepted (Consolidates ADRs 0009, 0010, 0017, 0020, 0033)

## Context & Philosophy
The platform must balance strict international privacy laws (GDPR/CCPA) with the inherently public, broadcast nature of Amateur Radio.

## Decisions & Mandates

### 1. Immutable Public RF Records & Infinite Retention
Amateur Radio contacts (QSOs) were transmitted over public spectrum -- broadcast on the air, for
anyone with a receiver to hear, at the moment they happened. That is what makes them a public
record rather than personal data in the GDPR sense: the platform is not the origin of the
exposure, it is a durable record of something that was already public the instant it went out
over RF. Erasing a QSO from the database after the fact would not undo that it was heard; it would
only make hams.com's own record of amateur radio history less accurate than the airwaves already
made it.
* `ham.qso` records are strictly exempt from cascading data destruction. A GDPR erasure request
  MUST NOT unlink a user's QSOs -- it reassigns ownership instead, leaving the contact record
  itself, its callsign, band, mode, and timestamp fully intact, never the record.
* **Ownership is anonymized by reassignment to a shared, dedicated service-account record
  (`zero_sudo.orphaned_record_owner`, "Anonymized User (GDPR Erasure)"), not by clearing the
  ownership field to null.** Reassignment works even when the ownership field is `required=True`
  (nulling it would fail validation, forcing every such field to be weakened to optional just to
  support erasure), and it means every view/report displaying these public records always has a
  real, presentable name to show -- no scattered null-ownership display handling needed anywhere.
  The centralized, tested helper for this is `zero_sudo.security.utils._anonymize_via_service_account(
  model_name, domain, owner_field, service_xml_id)`; it verifies the acting service account's own
  search visibility matches ground truth (a `sudo()` comparison, used only to compare counts, never
  for the actual write) before reassigning, and raises loudly instead of silently anonymizing a
  partial subset if they don't match -- the write-based sibling of `_erase_via_service_account`
  (see Decision 2 below), same reasoning: a silent partial anonymization is exactly as bad a
  failure as a silent partial deletion. Verified against the actual implementation, not just this
  policy text: `ham_logbook/models/res_users.py`'s `_execute_gdpr_erasure()` calls this utility for
  `ham.qso.owner_user_id`, and `ham_qso.py`'s `owner_user_id` field is declared `ondelete="set null"`
  as a defensive fallback (only relevant if a user record were ever actually hard-deleted, which
  this codebase's own GDPR flow never does -- accounts are deactivated and anonymized, not deleted).
* Relational links MUST use `ondelete='set null'` or `ondelete='restrict'`.
* Infinite growth of the `ham.qso` table is a platform feature, maintaining historical contest
  scores and mathematical integrity for the community.
* **The same "already public over RF" reasoning extends to contest scores and net-control
  history, by explicit decision (2026-08-25), not just QSOs.** `ham.contest.score.user_id` and
  `event.event.net_control_id` are, like QSOs, records of things that happened over the air, not
  private data about the person -- both are now reassigned to the same
  `zero_sudo.orphaned_record_owner` account via `_anonymize_via_service_account()`, from
  `ham_events/models/res_users.py`'s `_execute_gdpr_erasure()`, and both fields are exported via
  `_get_gdpr_export_data()` the same way QSOs are (a user can still get a copy of their own contest
  results and net-control history, even though the underlying platform record isn't deleted).
  `ham.event.issue` (correction reports a user filed) is a different shape -- personal input about
  something else, not itself a record of on-air activity -- and stays hard-deleted via
  `_erase_via_service_account()`, unchanged by this decision. `ham_callbook`'s `callbook_ids` and
  `ham_shack`'s `award_progress_ids` remain genuinely open questions, not yet decided either way.
* **Repeater directory listings (`ham.repeater`) are also never deleted on the trustee's own
  account erasure, by explicit decision (2026-08-25) -- and for a second, independent reason
  beyond "already public": community-infrastructure griefing resistance.** A repeater listing is
  public infrastructure information other operators actively rely on to find and use a real,
  physical machine -- deleting it on account erasure would let a disenchanted volunteer weaponize
  the erasure flow to unilaterally take down a community resource they no longer directly control,
  which is a real, distinct risk from the pure "it was already public" argument above (an
  ordinary, non-adversarial user erasing a purely personal record has no equivalent "hold the
  community hostage" angle). Reassigned to `zero_sudo.orphaned_record_owner` via
  `_anonymize_via_service_account()` from `ham_repeater_dir/models/res_users.py`'s
  `_execute_gdpr_erasure()`, same mechanism as the on-air records above. This griefing-resistance
  reasoning is worth checking against any other community-maintained-resource model found later,
  independently of whether that model's data also happens to be "public record" in the RF sense.

### 2. GDPR Erasure Separation of Privilege
When executing a GDPR Right to Erasure request, the system must cascade and hard-delete all standard user data (Websites, Blogs, test progress). To comply with the Zero-Sudo architecture, this operation MUST NOT use `.sudo().unlink()`. Instead, the erasure hook MUST impersonate the `gdpr_service_internal` micro-service account, which possesses the exact granular ACLs required to cascade unlinks across the restricted tables safely.

### 3. Location Data Precision & Geographic Fuzzing
Location data (Maidenhead Grid Squares) MUST be stored at maximum precision but presented conditionally:
* **Public RF Records:** Location data derived from DX spots or QSO logs is public and MUST be shown at full resolution.
* **Third-Party Directory Views:** When viewing a user's `ham.callbook` profile via public API or map, the ORM MUST mathematically truncate their grid to 4 characters and snap the map pin to the center of a regional bounding box, unless the user explicitly opts into `exact` privacy.
* **Private Dashboards:** Dashboards presented strictly to the authenticated user (e.g., Propagation Maps) MUST use precise, un-fuzzed data.
