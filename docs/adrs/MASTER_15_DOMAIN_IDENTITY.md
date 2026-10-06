# MASTER 15: Domain Identity & Verification

## Status
Accepted (Consolidates ADRs 0019, 0064)

## Context & Philosophy
Domain-specific rules for verifying Amateur Radio operator identities securely without exposing internal ERP models.

## Decisions & Mandates

### 1. Identity Verification Fallback Matrix
* Operator verification MUST support a diverse, international matrix to guarantee accessibility:
    1. Cryptographic LoTW (the ARRL's Logbook of The World; Golden Path)
    2. Knowledge-based (Ham-CAPTCHA, a test of amateur-radio knowledge, or a challenge on the applicant's QRZ.com profile)
    3. Skill-based (Dynamic Morse Code Challenge)
    4. Regulatory (Official FCC Email OTP, a one-time password sent to the email address on the regulator's public record)
    5. Manual ID Upload

### 2. Shadow Profile Indexing (Search Indexes)
The platform relies on background daemons to perform cross-user lookups. Querying `res.users` directly requires `base.group_user`, which violates the Zero-Sudo mandate by exposing the ERP to microservices. We mandate the **Shadow Profile Pattern** (a "shadow profile" is a view that stands beside `res.users` and is queried in its place, so daemons never read the real user table):
1. **Isolated PostgreSQL Views:** Introduce dedicated, domain-specific views (`ham.operator.index`) that exist entirely outside of the `res.users` Python security hierarchy using `_auto = False`.
2. **Strict Group Gating:** The views use SQL `JOIN` constraints to exclusively expose users belonging to the required domain groups. Standard Odoo ERP users are natively excluded.
3. **Daemon Rerouting:** All external daemons, webhooks, and APIs MUST execute their searches against these shadow indexes.
4. **The User Manager Proxy:** When a daemon finds a match in the index and must alter the actual user (e.g., flipping `is_identity_verified` to True), it escalates exclusively to the `user_manager_service_internal` account. This acts as a secure, one-way drawbridge into the core ERP.
