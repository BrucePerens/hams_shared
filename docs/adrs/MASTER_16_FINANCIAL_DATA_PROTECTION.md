# MASTER 16: Financial Data Protection & Defense-in-Depth

## Status
Accepted

## Context & Philosophy
The platform processes financial transactions to support club membership dues, online swap meets (classifieds), and other portal activities. However, financial identifiers (such as bank accounts, credit card numbers, and payment tokens) are highly sensitive and present a critical theft risk.

Odoo's native ORM and relational data structures (e.g., traversing `res.partner.bank_ids` or `payment.token`) are notoriously prone to silent data leakage if a downstream module or UI view carelessly exposes a relationship. Relying solely on Micro-Privilege (Service Accounts) is a single point of failure and is insufficient. We must employ a strict Defense-in-Depth strategy.

## Decisions & Mandates

### 1. Multiple Levels of Protection (Defense-in-Depth)
Financial identifiers MUST ALWAYS be protected by multiple, redundant layers of security. The architecture must assume that any single layer (like a specific Access Control List or UI restriction) will eventually fail or be bypassed.

### 2. Mandatory Data Masking via SQL Views
To permanently neutralize Odoo's relational leakage, raw financial tables MUST NOT be directly queried or exposed throughout the general software.
* **Mandatory Masking:** PostgreSQL Views (`_auto = False`) that mask privileged financial data MUST be mandatory everywhere in the software.
* **Opaque Abstraction:** These views must only expose safe, aggregated, or masked representations (e.g., returning a boolean `has_payment_method` or a masked string `****-1234`).
* **Transaction Exclusivity:** The unmasked, raw financial data is strictly forbidden from entering the application's WSGI memory EXCEPT during the exact execution context where the financial data is actually required (i.e., actively processing a payment gateway transaction).

### 3. Financial Micro-Privilege Confinement
While not the only layer, micro-privilege remains a critical boundary.
* Service accounts used for non-financial operations (e.g., provisioning, onboarding, or DNS management) MUST be explicitly denied read/write access to financial models in their `ir.model.access.csv` declarations.
* Financial transactions must be executed by highly specialized, isolated proxy accounts dedicated exclusively to payment routing or ledger entries.
