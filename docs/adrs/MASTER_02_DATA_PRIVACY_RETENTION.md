# MASTER 02: Data Privacy, Location & Retention

## Status
Accepted (Consolidates ADRs 0009, 0010, 0017, 0020, 0033)

## Context & Philosophy
The platform must balance strict international privacy laws (GDPR/CCPA) with the inherently public, broadcast nature of Amateur Radio.

## Decisions & Mandates

### 1. Immutable Public RF Records & Infinite Retention
Amateur Radio contacts (QSOs) occur over public spectrum. They are legally classified as public matters of record.
* `ham.qso` records are strictly exempt from cascading data destruction.
* Relational links MUST use `ondelete='set null'` or `ondelete='restrict'`.
* Infinite growth of the `ham.qso` table is a platform feature, maintaining historical contest scores and mathematical integrity for the community.

### 2. GDPR Erasure Separation of Privilege
When executing a GDPR Right to Erasure request, the system must cascade and hard-delete all standard user data (Websites, Blogs, test progress). To comply with the Zero-Sudo architecture, this operation MUST NOT use `.sudo().unlink()`. Instead, the erasure hook MUST impersonate the `gdpr_service_internal` micro-service account, which possesses the exact granular ACLs required to cascade unlinks across the restricted tables safely.

### 3. Location Data Precision & Geographic Fuzzing
Location data (Maidenhead Grid Squares) MUST be stored at maximum precision but presented conditionally:
* **Public RF Records:** Location data derived from DX spots or QSO logs is public and MUST be shown at full resolution.
* **Third-Party Directory Views:** When viewing a user's `ham.callbook` profile via public API or map, the ORM MUST mathematically truncate their grid to 4 characters and snap the map pin to the center of a regional bounding box, unless the user explicitly opts into `exact` privacy.
* **Private Dashboards:** Dashboards presented strictly to the authenticated user (e.g., Propagation Maps) MUST use precise, un-fuzzed data.
