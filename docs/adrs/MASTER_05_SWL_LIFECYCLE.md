# MASTER 05: SWL Lifecycle & Automated Progression

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
* **High Confidence (Auto-Upgrade):** If the FRN matches, the system automatically strips the `SWL_` prefix, assigns the official callsign, transitions the user to `ham`, and provisions their DNS zone.
* **Low Confidence (Moderation Queue):** If the system only matches on Name and Zip Code, it creates a ticket for the Moderation Queue to prevent familial collisions (e.g., father and son at the same address).
* **Study Heuristic (Exception):** If the Name/Zip matches AND the user has actively studied for that specific license class in the `ham.testing.progress` module within the last 90 days, confidence is mathematically raised back to Auto-Upgrade levels, bypassing human moderation safely.
