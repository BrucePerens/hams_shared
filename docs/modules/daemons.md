# ⚙️ Background Daemons (`daemons/`)

*Copyright © Bruce Perens K6BP. AGPL-3.0-or-later.*

**Context:** API Contracts for standalone background processes.

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

## 1. Network Integrations
* **Local Hardware Relay:** A lightweight Flask server running on the user's machine that intercepts `fetch()` requests to command connected transceivers via Hamlib. The `/qsy` endpoint refuses a request that lacks the header `X-Hams-Action: execute` [@ANCHOR: local_relay_qsy_endpoint].

## 2. RabbitMQ Consumers
* **ADIF Processor:** Consumes base64 ADIF payloads from the message broker to process massive log files asynchronously without blocking web workers [@ANCHOR: consume_adif_task].

* **PowerDNS Sync:** Consumes Odoo DNS zone mutations and synchronizes them to the authoritative PowerDNS SQLite backend [@ANCHOR: pdns_rabbitmq_consumer].

## 3. Scheduled Polling
* **QSL Synchronization (LoTW and eQSL):** ARRL Logbook of the World (LoTW) and eQSL are no longer polled by a central daemon; the two anchors [@ANCHOR: daemon_sync_lotw_batch] and [@ANCHOR: daemon_sync_eqsl_batch] name that retired design, and no code carries them now. Each member's account password stays on the member's own machine: the local hardware relay downloads that member's confirmations there and reports them to Odoo, where a scheduled action applies them to the logbook (anchors `ham_relay_bridge:relay_lotw_apply_confirmations_cron` and `ham_relay_bridge:relay_eqsl_apply_confirmations_cron`).

* **Space Weather:** Fetches the solar flux index (SFI, the 10.7 cm radio flux) and the K-index (a three-hourly measure of geomagnetic disturbance) from the NOAA Space Weather Prediction Center (SWPC) JSON feeds and writes them to Odoo's `ham.space.weather` records, so logged contacts can be matched to the conditions at the time. It polls every 30 minutes by default (the `POLL_INTERVAL` environment variable overrides it; values under one minute are ignored), not hourly: NOAA publishes new K-index values only every three hours [@ANCHOR: fetch_solar_metrics].

## 4. Platform Synchronization
* Executes the regulatory sync cycle [@ANCHOR: regulatory_sync_cycle] and pushes FCC batches [@ANCHOR: odoo_sync_fcc_batch] via [@ANCHOR: daemon_sync_fcc_batch].

* Syncs satellite TLEs [@ANCHOR: daemon_sync_tles].

* Listens for Postgres NOTIFY events [@ANCHOR: firehose_notify_handler] and broadcasts the logged contacts (QSOs) they announce to connected WebSocket clients (the DX firehose daemon).
