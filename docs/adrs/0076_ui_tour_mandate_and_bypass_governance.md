# ADR 0076: UI Tour Mandate and Bypass Governance

## Status
Accepted

## Context
The platform's DevSecOps pipeline utilizes an AST Burn List to strictly enforce the presence of JavaScript UI Tours for all Odoo interface views (`ir.ui.view`). While comprehensive testing is critical, JavaScript DOM-based tours are inherently brittle. They rely on specific CSS selectors and DOM structures, making them susceptible to false positives caused by minor layout shifts, core framework upgrades, or network latency.

Mandating 100% tour coverage creates a "Maintenance Trap," where CI/CD pipelines constantly fail, and developer bandwidth is consumed by fixing fragile tests rather than building features.

## Decision

We will not eliminate the `burn-ignore-tour` linter bypass. Instead, we elevate the standard for its use. The platform bifurcates view testing into "Mandatory Tours" and "Justified Exceptions."

### 1. The "Gold Standard" (When Tours are Mandatory)
A JavaScript UI Tour (`web_tour`) **MUST** be written for views meeting any of the following criteria:
* **Critical User Journeys:** High-stakes workflows such as SWL callsign upgrades, marketplace purchases, or administrative verifications.
* **Complex State Machines:** Form views that utilize dynamic `invisible`, `readonly`, or `required` attributes based on the record's state (e.g., dynamic exam forms or multi-step wizards).
* **Custom Widgets:** Any view injecting custom JavaScript components (e.g., the Morse Code Keyer, DX Cluster WebSocket dashboards, or interactive maps).

### 2. The Justified Exceptions (When to use `<!-- burn-ignore-tour -->`)
The `<!-- burn-ignore-tour -->` tag is strictly reserved for the following scenarios, where the Return on Investment (ROI) of a DOM-based tour is zero:
* **Simple Dictionary / Lookup Tables:** Basic `tree`, `list`, or `form` views displaying static data (e.g., ITU Zones, Country Codes) with no complex interactions. Standard Python CRUD unit tests are sufficient here.
* **Invisible / Programmatic Views:** Views explicitly designed to be invoked silently by background processes or Python code, which are never organically navigated to by a user.
* **Read-Only Audit Logs:** Backend history views (e.g., ADIF queue logs, QRZ sync logs) where user data mutation is impossible.
* **Micro-Inheritances:** Inheriting a base Odoo view solely to inject a single `invisible="1"` field, an `xpath` removal, or a basic domain filter.

### 3. Concurrent Audit Tags (Do Not Cannibalize)
The `burn-ignore-tour` tag serves a distinct purpose from `audit-ignore-view` and `audit-ignore-xpath`. The audit tags signal that a view's syntactic rendering is covered by a backend Python test.
When bypassing both the Tour Mandate and the isolated View Rendering test, **both tags must be explicitly defined side-by-side**. You MUST NOT delete `audit-ignore-view` to add `burn-ignore-tour`.
%
