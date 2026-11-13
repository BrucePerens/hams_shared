# ⚙️ Background Daemons (`daemons/`)

*Copyright © Bruce Perens K6BP. AGPL-3.0-or-later.*

**Context:** API Contracts for standalone background processes.

*Anchor tags: each bracketed anchor tag below is a Semantic Anchor, a marker that ties one function to its test and to its documentation; the `COMM_` prefix marks anchors owned by `hams_open`. See [MASTER 11](../adrs/MASTER_11_DEVELOPMENT_WORKFLOW_DOCS.md) and [ADR-0090](../adrs/0090_universal_function_test_anchor_ratchet.md).*

## 1. Network Integrations
* **Local Hardware Relay:** A lightweight Flask server running on the user's machine that intercepts `fetch()` requests to command connected transceivers via Hamlib. The `/qsy` endpoint refuses a request that lacks the header `X-Hams-Action: execute` `local_relay_qsy_endpoint`.

## 2. RabbitMQ Consumers
* **ADIF Processor:** Consumes base64 ADIF (Amateur Data Interchange Format, the standard radio-log file format) payloads from the message broker to process massive log files asynchronously without blocking web workers `consume_adif_task`.

* **PowerDNS Sync:** Consumes Odoo DNS zone mutations and synchronizes them to the authoritative PowerDNS SQLite backend `pdns_rabbitmq_consumer`.

## 3. Scheduled Polling
* **QSL Synchronization (eQSL):** (a QSL is a confirmation of a radio contact; eQSL is an electronic QSL service) eQSL is no longer polled by a central daemon; the anchor [@ANCHOR: daemon_sync_eqsl_batch] names that retired design, and no code carries it now. Each member's account password stays on the member's own machine: the local hardware relay downloads that member's confirmations there and reports them to Odoo, where a scheduled action applies them to the logbook (anchor `ham_relay_bridge:relay_eqsl_apply_confirmations_cron`).

* **Space Weather:** Fetches the solar flux index (SFI, the 10.7 cm radio flux) and the K-index (a three-hourly measure of geomagnetic disturbance) from the NOAA Space Weather Prediction Center (SWPC) JSON feeds and writes them to Odoo's `ham.space.weather` records, so logged contacts can be matched to the conditions at the time. It polls every 30 minutes by default (the `POLL_INTERVAL` environment variable overrides it; values under one minute are ignored), not hourly: NOAA publishes new K-index values only every three hours [@ANCHOR: ham_logbook:fetch_solar_metrics].

## 4. Platform Synchronization
* Executes the regulatory sync cycle `regulatory_sync_cycle` and pushes FCC (US Federal Communications Commission, the amateur licence regulator) batches `odoo_sync_fcc_batch` via `daemon_sync_fcc_batch`.

* Syncs satellite TLEs `daemon_sync_tles`.

* Listens for Postgres NOTIFY events `firehose_notify_handler` and broadcasts the logged contacts (QSOs) they announce to connected WebSocket clients (the DX firehose daemon).

## Anchor reference

### `local_relay_qsy_endpoint` [@ANCHOR: local_relay_qsy_endpoint]

**Purpose.** The Flask route `GET /qsy` of the local hardware relay (`qsy()` in `daemons/hams_local_relay/main.py`) that retunes the transceiver the user's own `rigctld` (Hamlib daemon) controls. The Web Shack page calls it with `fetch()`.

| input | where | meaning |
|---|---|---|
| `X-Hams-Action: execute` | header | required; otherwise HTTP 403 |
| `freq` | query | frequency in MHz, finite float; missing gives 400 "Missing frequency", unparseable or `inf`/`nan` gives 400 "Invalid frequency format" |
| `mode` | query | optional; `false`, `null`, `undefined` are ignored; other values have every non-alphanumeric character removed and are upper-cased |

**Returns.** JSON `{status, success, freq_response, mode_response}`; `mode_response` is "Not requested" with no mode. It sends `F <hertz>` and `M <MODE> 0` to rigctld through `send_hamlib_cmd` (2 second socket timeout); a rigctld error text comes back as the response string, not an HTTP error.

**Special concerns.** The header is the only guard, so the relay listens on loopback by default (`FLASK_HOST` overrides); it changes the radio's frequency and mode but does not key the transmitter. Test: `ham_shack/tests/test_webrtc_tour.py`.

### `consume_adif_task` [@ANCHOR: adif_processor:consume_adif_task]

**Purpose.** The RabbitMQ consumer that parses uploaded ADIF logs (the standard radio-log file format) outside the web workers: `callback(ch, method, properties, body)` in `daemons/adif_processor/main.py`.

**Interface.** It is registered with `basic_consume` on the durable queue `adif_tasks` with prefetch 1, so one big log is parsed at a time and memory stays flat. The body is JSON `{"queue_id": <int>}`, published by `ham_logbook` after it stores an upload; `queue_id` must be a true `int` (not a bool), otherwise the message is logged as malformed and dropped. A valid id goes to `process_job(client, job_id)`, which parses and writes the final state to the `ham.adif.queue` row.

**Special concerns.** Every message is acknowledged in a `finally`, even on error: failures are recorded as the row's `failed` state, so a poison message cannot redeliver forever. At start-up the daemon re-publishes every row still `pending`. Test: `daemons/adif_processor/test_adif_processor.py`.

### `pdns_rabbitmq_consumer` [@ANCHOR: pdns_rabbitmq_consumer]

**Purpose.** Applies Odoo DNS zone changes to the authoritative PowerDNS server: `process_dns_update(ch, method, properties, body)`, the closure from `get_callback(odoo_client)` in `daemons/pdns_sync/main.py`, triggered by `ham_dns:trigger_pdns_sync`.

**Interface.** `main()` consumes the durable queue `dns_zone_updates` with prefetch 5, after refusing missing or `guest` RabbitMQ credentials and an incomplete zone configuration. The JSON body has `action` (`upsert`, the default, or `delete`), `zone_name`, `zone_id` and `records` (each `name`, `type`, `content`, `ttl`).

**Behaviour and errors.** A body that is not an object, a bad zone name or any malformed record is logged and acknowledged (dropped, never requeued). An upsert PATCHes the zone so PowerDNS holds exactly the supplied rrsets, then reports `done`, `partial` or `failed` to Odoo. A permanent 4xx is dropped; other failures back off and republish with an attempt counter up to `PDNS_SYNC_MAX_ATTEMPTS` (30). The nightly reconcile re-queues what Odoo has. Tests: `ham_dns/tests/test_dns_automation.py`.

### `regulatory_sync_cycle` [@ANCHOR: fcc_uls_sync:regulatory_sync_cycle]

**Purpose.** One pass of the FCC ULS (Universal Licensing System) amateur-licence download, parse and push: `run_sync()` in `daemons/fcc_uls_sync/main.py`.

**Behaviour.** It connects with `get_odoo_client`, rotates its own API key when due, then for each file in `FCC_DOWNLOADS` (the weekly `l_amat.zip` plus the daily files) decides whether to fetch: a daily file already superseded by the weekly one is skipped without a request, a staged drop is preferred over a download, otherwise `smart_download()` is used (optional `FCC_ULS_PROXY_URL` egress). Fetched files go through `_ingest()`.

**Returns.** A success flag; the failed files, files blocked by HTTP 403 (a block, not an outage) and superseded files are recorded and reported, and the caller turns the flag into the process exit status with `sync_exit_status()`.

**Special concerns.** Runs unattended on a schedule and uses the network and Odoo's JSON-2 API. Tests: `daemons/fcc_uls_sync/test_fcc_sync.py`, `ham_callbook/tests/test_callbook.py`.

### `odoo_sync_fcc_batch` [@ANCHOR: ham_callbook:odoo_sync_fcc_batch]

**Purpose.** The Odoo side of the FCC batch push: `ham.callbook.sync_fcc_batch(batch_data)` in `ham_callbook/models/ham_callbook.py`, a `@public_rpc @api.model` method the sync daemons call over JSON-2.

**Interface.** `batch_data` is a list of dicts (`frn`, `callsign`, names, address, `license_class`, dates, `country_code`, `state_code`, entity fields). It begins with `_require_sync_caller()`: only the callbook sync service user, an administrator or the superuser environment, else `AccessError`. Returns `True`, also for a batch with no FRNs.

**Behaviour.** De-duplicates the batch by FRN and ignores rows lacking an FRN or a usable callsign. Existing records (matched by `frn`) get only the fields that changed, grouped into few writes; an empty incoming email never blanks a stored one; a callsign change is copied to the linked user. Unknown FRNs are created, country and state codes are mapped to ids, and the SWL correlation phase runs unless the context sets `skip_swl_correlation`.

**Special concerns.** Idempotent: re-sending a batch changes nothing. Tests: `ham_callbook/tests/test_callbook.py`, `test_fcc_idempotency.py`.

### `daemon_sync_fcc_batch` [@ANCHOR: daemon_sync_fcc_batch]

**Purpose.** The daemon-side step that streams parsed FCC records to Odoo: inside `_ingest()` in `daemons/fcc_uls_sync/main.py`.

**Behaviour.** It takes records from `process_fcc_zip()` and calls `push_batches_to_odoo(client, logger, records, "ham.callbook", "sync_fcc_batch")`, 500 records per call, which reaches `odoo_sync_fcc_batch`. A failed batch is logged and the rest continue; `_ingest()` returns `False` when any batch failed.

**State markers.** The sync-state markers (content hash, ETag, Last-Modified) are written with `update_sync_state()` only after complete success, so after a failure the next run retries the whole file. A file whose hash equals the stored one is not re-pushed.

**Special concerns.** Network and Odoo JSON-2 writes; safe to repeat because the Odoo end is idempotent. Test: `daemons/fcc_uls_sync/test_fcc_sync.py`.

### `daemon_sync_tles` [@ANCHOR: daemon_sync_tles]

**Purpose.** Keeps the satellite two-line-element table current for pass prediction: `run_sync(client)` in `daemons/amsat_tle_sync/main.py`.

**Behaviour.** It downloads AMSAT's `nasabare.txt` with `smart_download()`, skips the file when its SHA-256 equals `ham.satellite.tle_hash`, parses three-line TLE blocks (name, line 1 starting `1 `, line 2 starting `2 `) and calls `ham.satellite.tle.sync_tles(tles=...)` in Odoo (`ham_satellite:odoo_sync_tles`), which replaces the table atomically and invalidates the caches.

**Returns and errors.** `True` on success; `False` on an error or a rejected batch, which the `__main__` block turns into an exit status with `sync_exit_status()`. State markers are stored only when Odoo accepted the batch. A payload with no valid TLE is recorded with `record_outcome()`; three in a row raise one ticket.

**See also.** Test `ham_satellite/tests/test_satellite_cache.py`.

### `firehose_notify_handler` [@ANCHOR: ham_dx_cluster:firehose_notify_handler]

**Purpose.** Feeds the DX firehose: when a logged contact is published, this callback starts the broadcast to connected WebSocket clients. It is `postgres_notify_handler(connection, pid, channel, payload)` in `daemons/dx_firehose/main.py`, triggered by `ham_logbook:qso_firehose_notify`.

**Interface.** A synchronous asyncpg callback fired for each NOTIFY on the channel `ham_qso_firehose`; `payload` is a comma-separated list of QSO ids. It only logs and schedules `fetch_and_broadcast_qsos(payload)` as an asyncio task.

**Behaviour.** That coroutine reads the rows with the daemon's own PostgreSQL login (never through Odoo; simulated practice contacts are excluded) and sends each as JSON to every connected client, dropping `comments` unless enabled. `keep_listener_alive()` probes the LISTEN connection every 60 seconds and replaces it if needed.

**Test.** `daemons/dx_firehose/test_dx_firehose.py` (`test_01_postgres_notify_handler`).
