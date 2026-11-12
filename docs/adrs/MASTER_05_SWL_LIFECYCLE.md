# MASTER 05: SWL (Short-Wave Listener) Lifecycle & Automated Progression

## Status
Accepted (Consolidates ADRs 0018, 0035)

## Context & Philosophy
The platform requires a structured environment for prospective operators (Short Wave Listeners) to study. However, allowing unverified users full access risks polluting the logbook and wasting infrastructure resources.

## Decisions & Mandates

### 1. The SWL Sandbox
* **Registration:** SWLs bypass the Ham-CAPTCHA during signup but must provide legal Name, Zip Code, and an optional Regulatory ID.
* **Identity Trapping:** To prevent impersonation, their `login`, `name`, and `callsign` are forcefully prefixed with `SWL_`.
* **Feature Access:** They are hard-blocked from `ham_logbook` APIs and `ham_dns` zone creation, but permitted read-access to the DX Cluster, Propagation Maps, and write-access to the Elmering Forums (displaying a specific SWL Trust Badge).

### 2. The Correlation Engine & Study Heuristic
A background daemon (`fcc_uls_sync`) constantly scans new license grants against the SWL database.
* **High Confidence (Auto-Upgrade):** If the FRN (FCC Registration Number, the regulator's permanent identifier for a license holder) matches, the system automatically strips the `SWL_` prefix, assigns the official callsign, transitions the user to `ham`, and provisions their DNS zone.
* **Low Confidence (Moderation Queue):** If the system only matches on Name and Zip Code, it creates a ticket for the Moderation Queue to prevent familial collisions (e.g., father and son at the same address).
* **Study Heuristic (Exception):** If the Name/Zip matches AND the user has actively studied for that specific license class in the `ham.testing.progress` module within the last 90 days, confidence is mathematically raised back to Auto-Upgrade levels, bypassing human moderation safely.

## Status check 2026-11-12

Checked against `ham_onboarding`. The sandbox rules of decision 1 hold (the signup controller builds `SWL_<handle>`). The correlation engine of decision 2 works differently from the text:
- It is not a background scan by `fcc_uls_sync`. The daemon only keeps the callbook current. The upgrade is requested by the member: `res.users.action_request_callsign_upgrade(new_callsign)`
  (`ham_onboarding/models/res_users.py`) looks the requested call up in `ham.callbook` and scores the match: 100 for a matching FRN, 86 or 70 for a similar or same last name with the same Zip code
  (name similarity above 0.85 gives 86; a same last name gives 70), and 90 when the Name/Zip match is backed by recent study (a `ham.testing.progress` row for that licence class within 90 days).
- A score of 80 or more upgrades; anything lower goes to manual review (`_queue_callsign_upgrade_for_manual_review`). So the high-confidence cases are FRN (100), a strong name match (86) and the
  study-backed match (90), not FRN alone.
- An FRN match built from a self-reported value (the member typed their own FRN) is not applied at once: a one-time code is first emailed to the official regulatory email on record for the target
  callsign, and the upgrade applies when it is entered (if no email is on record, the request goes to manual review).
- An administrator can also upgrade by hand (`action_upgrade_swl_to_ham`).
