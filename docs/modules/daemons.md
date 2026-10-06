# ⚙️ Background Daemons (`daemons/`)

*Copyright © Bruce Perens K6BP. AGPL-3.0-or-later.*

**Context:** API Contracts for standalone background processes.

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

## 1. Network Integrations
* **Local Hardware Relay:** A lightweight Flask server running on the user's machine that intercepts `fetch()` requests to command connected transceivers via Hamlib [@ANCHOR: local_relay_qsy_endpoint].

## 2. RabbitMQ Consumers
* **ADIF Processor:** Consumes base64 ADIF payloads from the message broker to process massive log files asynchronously without blocking web workers [@ANCHOR: consume_adif_task].

* **PowerDNS Sync:** Consumes Odoo DNS zone mutations and synchronizes them to the authoritative PowerDNS SQLite backend [@ANCHOR: pdns_rabbitmq_consumer].

## 3. Scheduled Polling
* **QSL Synchronization:** Gently polls ARRL LoTW [@ANCHOR: daemon_sync_lotw_batch] and eQSL [@ANCHOR: daemon_sync_eqsl_batch] daily to automatically confirm user contacts.

* **Space Weather:** Fetches SFI and K-Index metrics from the NOAA SWPC JSON APIs hourly to drive propagation calculations [@ANCHOR: fetch_solar_metrics].

## 4. Platform Synchronization
* Executes the regulatory sync cycle [@ANCHOR: regulatory_sync_cycle] and pushes FCC batches [@ANCHOR: odoo_sync_fcc_batch] via [@ANCHOR: daemon_sync_fcc_batch].

* Syncs satellite TLEs [@ANCHOR: daemon_sync_tles].

* Listens for Postgres NOTIFY events [@ANCHOR: firehose_notify_handler] and broadcasts them to connected WebSocket clients.
