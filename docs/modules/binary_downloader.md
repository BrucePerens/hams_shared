# Binary Downloader Module (`binary_downloader`)

*Copyright © Bruce Perens K6BP. Licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).*

The **Binary Downloader** is a secure, database-backed orchestration module designed to provide static executable dependencies (e.g., `kopia`, `etcd`, `cloudflared`) to other Odoo subsystems. It implements a robust lifecycle management system for external tools while maintaining strict security standards.

---

# Technical Documentation

<system_role>
**Context:** Technical documentation strictly for developers, LLMs, and System Integrators.
</system_role>

<security_design>
## 1. Security Design
* **DB-Backed Manifests:** Download targets and cryptographic SHA-256 checksums are stored in the `binary.manifest` model, preventing reliance on insecure flat-file manifests.
* **Least Privilege:** Executes downloads and installations under the dedicated `user_binary_downloader_service` service account. The module is fully compliant with the **Zero-Sudo** mandate (no `.sudo()`; privileged work runs as a dedicated, narrowly scoped service account -- defined in [zero_sudo.md](zero_sudo.md), accounts catalogued in [service_accounts.md](../service_accounts.md)).
* **Integrity Enforcement:** Verifies SHA-256 hashes before moving binaries to the execution path (`hams_bin`). For an archive, the hash checked is that of the whole downloaded archive. A download larger than 1 GiB is aborted before its checksum is checked.
* **Concurrency Protection:** Implements PostgreSQL **advisory locks** (via `pg_advisory_xact_lock`) during the installation process to prevent race conditions and file corruption when multiple Odoo workers trigger installations simultaneously.
* **Archive Security (Tar/Zip Slip):** "Tar Slip"/"Zip Slip" is an attack in which an archive member's path (for example `../../x`) makes extraction write outside the intended directory. Only one member is ever extracted (`.tar.gz` and `.zip`): the first whose name equals `extract_member` or ends in `/<extract_member>`. Its bytes are written to the computed target filename in `hams_bin`, so the member's own path never decides where the file lands. If the selected member is a symbolic or hard link, installation is refused.
* **Timeouts:** Both network requests have a 15 second timeout (HEAD and GET alike) to prevent resource exhaustion and hanging threads. A failed HEAD request is only logged; the GET request decides success.
* **Download Restrictions:** Only `https://` URLs are accepted (when a manifest or version is saved and again at download time). Before connecting, the host name must resolve only to public addresses, and the same check is applied to wherever a redirect lands; loopback, link-local, private-use, multicast and reserved addresses are rejected. This blocks Server-Side Request Forgery (SSRF), where a manifest URL would make the Odoo server fetch from internal services.
* **Permissions:** Target directory (`hams_bin`) and binaries are set to `0o750` to restrict execution and access.
* **Multi-Company Isolation:** Binaries are stored in one shared directory (`hams_bin`), with version-aware filenames `<name>_<id>` (`<id>` is the first 16 hex digits of the SHA-256 of `<name>_<checksum>`, so a new checksum gives a new file). `binary.manifest` has a `company_id`; `ensure_executable` uses the current company's manifest first and falls back only to a global one (no company), never to another company's. Per-website versions are handled by `binary.tenant.link` and `binary.version` (see the README).
</security_design>

<api>
## 2. API Reference

### `binary.manifest` model
The primary interface for dependency resolution.

#### `ensure_executable(cmd_name)`
`binary_ensure_executable`

Resolves and ensures a binary is available and executable. Returns the absolute path to the binary. It looks up the manifest for `cmd_name` (current company first, then global) as the module's service account, then returns that manifest's file in Odoo's private binary directory (`hams_bin`) if it is already there and valid; otherwise it attempts an automatic download and installation. It deliberately does not consult the system `PATH`, so a same-named program elsewhere on the server cannot shadow the verified copy.

**Parameters:**
- `cmd_name` (str): The name of the command to ensure (e.g., `"kopia"`).

**Returns:**
- Absolute path (str) to the verified executable.

**Raises:**
- `ValidationError`: If the command name is invalid (empty, contains slashes or backslashes, or is `.` or `..`). Manifest constraints (e.g., missing `extract_member` for tarballs) raise `ValidationError` when the manifest is saved, not here.
- `UserError`: If the manifest is missing, the platform is unsupported (anything but Linux on x86_64, aarch64 or armv7l), checksum/integrity checks fail, the download is refused by the restrictions above, or the download or extraction fails.

#### `_compute_is_installed()`
`binary_compute_installed`

Tracks whether a binary is available in the system `PATH` or `hams_bin` and has appropriate execution permissions (a program on the `PATH` counts as installed here even though `ensure_executable` never uses it).

#### `action_install()`
`binary_action_install`

Triggers manual installation via the UI; it raises `UserError` unless the user is in the Binary Downloader Manager group or is an administrator.

* **Logic:**
    1. Checks if the binary is already available and valid.
    2. If not, downloads, verifies checksum, and extracts/installs if necessary to `<data_dir>/hams_bin/` (`data_dir` from the Odoo configuration, default `/var/lib/odoo`) by calling `ensure_executable`.
</api>

<usage>
## 3. Usage Example
```python
# To be called by other modules needing a binary dependency
bin_path = self.env["binary.manifest"].ensure_executable("kopia")
# Verified by test_binary_manifest_standard

# Verified by test_binary_manifest_integration
subprocess.run([bin_path, "--version"], check=True)
```
</usage>

<stories_and_journeys>
## 4. Architectural Stories & Journeys

For detailed narratives and end-to-end workflows, refer to the following:

### Stories
* [Binary Resolution](../../../binary_downloader/docs/stories/binary_resolution.md)
* [UI Installation](../../../binary_downloader/docs/stories/ui_installation.md)
* [Installation Status Check](../../../binary_downloader/docs/stories/is_installed_check.md)

### Journeys
* [Automated Provisioning Flow](../../../binary_downloader/docs/journeys/auto_provisioning_flow.md)
</stories_and_journeys>

<semantic_anchors>
## 5. Semantic Anchors
- `binary_ensure_executable` - Core binary resolution method.

- `binary_compute_installed` - Installation status computation.

- `binary_action_install` - UI installation trigger.

- `UX_BINARY_INSTALL` - UI elements for installation.

- `test_binary_manifest_standard` - Standard unit tests.

- `test_binary_manifest_integration` - Unmocked physical integration tests.

- `test_binary_install_tour` - UI tour for binary installation.

- `test_binary_manifest_views` - View rendering tests.
</semantic_anchors>

## Anchor reference

One entry per anchor, written from the code in `binary_downloader/models/binary_manifest.py`, its views and tests. `hams_open/binary_downloader/README.md` holds the longer reference for the whole module.

### `binary_ensure_executable` [@ANCHOR: binary_ensure_executable]
Purpose: the public entry point other modules use to get a verified program. `binary.manifest.ensure_executable(cmd_name)` is `@api.private @api.model`: callable from server code, not over RPC.

| Argument | Type | Meaning |
|---|---|---|
| `cmd_name` | str | Binary name, for example `"kopia"`. Empty, containing `/` or a backslash, `.` or `..` raises `ValidationError`. |

Returns the absolute path (str) of the verified executable in `hams_bin`. It finds the manifest as the `user_binary_downloader_service` account: the current company's manifest first, then the global one (no company), never another company's. It does not consult the system `PATH`, so a same-named program elsewhere cannot shadow the verified copy (the `zero_sudo` wrapper `_ensure_executable` tries `PATH` first when a caller wants that). Errors: `UserError` when no manifest exists, or from `_download_and_extract` (unsupported platform, SSRF refusal, size cap, checksum mismatch, extraction failure). Side effects: may download and write a file, taking a PostgreSQL advisory lock for the length of the transaction. Blocking; the first call for a new binary can take as long as the download. Safe to retry. Tests: `test_01_already_installed` to `test_04_successful_download_and_checksum` and `test_18_path_shadow_prevention` in `tests/test_binary_manifest.py`.

### `binary_compute_installed` [@ANCHOR: binary_compute_installed]
Purpose: the `is_installed` column of the manifest list. `binary.manifest._compute_is_installed()` is `@api.depends("name", "checksum", "archive_type", "extract_member")`, non-stored, no arguments and no return value. For each record it sets `is_installed` to `False` without a name; `True` if `shutil.which(name)` finds a program of that name on the system `PATH`; otherwise `True` when the file `<data_dir>/hams_bin/<name>_<id>` exists and is executable.

Special concerns: the `PATH` rule means a program installed by the operating system counts as installed, although `ensure_executable` never uses it (that is why the Install button is hidden for it). It reads the file system on each evaluation and never writes or downloads. Test: `test_06_is_installed_compute` in `tests/test_binary_manifest.py`.

### `binary_action_install` [@ANCHOR: binary_action_install]
Purpose: the "Install Now" button on a manifest. `binary.manifest.action_install()` is a `@public_rpc` method and needs exactly one record (`ensure_one`). Access: raises `UserError` ("You do not have sufficient permissions to install binaries.") unless the caller is in the Binary Downloader Manager group or is an administrator (`env.is_admin()`).

Behaviour: calls `ensure_executable(self.name)`, which resolves the manifest (current company, then global), downloads and verifies the file into `<data_dir>/hams_bin/` if it is not already there. Returns a client action: a "Success" notification ("Installation of X completed successfully.") followed by a page reload. Errors from `ensure_executable` (`UserError`) are passed on. Blocking for the length of the download. Tests: `test_07_action_install` and `test_12_action_install_permissions` in `tests/test_binary_manifest.py`, UI tour `test_binary_install_tour`.

### `UX_BINARY_INSTALL` [@ANCHOR: UX_BINARY_INSTALL]
Purpose: marks the user-interface element that starts an install. In `views/binary_manifest_views.xml` the form `view_binary_downloader_manifest_form` has a header with the button "Install Now" (`name="action_install"`, `type="object"`, class `oe_highlight`, accessible label "Install Binary Now"). It is hidden when the record is already installed or not yet saved (`invisible="is_installed or not id"`) and only offered to the groups `binary_downloader.group_binary_downloader_manager` and `base.group_system`.

Special concerns: hiding the button is not the security control; the check lives in `action_install` itself. The element has no code of its own; the behaviour is that of `action_install`. Tests: `test_binary_install_tour` (clicks the button), `test_binary_manifest_views`.

### `test_binary_manifest_standard` [@ANCHOR: test_binary_manifest_standard]
Test anchor for the standard (mocked) unit tests of the manifest in `tests/test_binary_manifest.py`, starting at `test_01_already_installed`. The tests mock the network (`urlopen`), `shutil.which` and the platform checks and replace `_assert_host_is_ssrf_safe`, so no live DNS or download is needed. They cover an already installed binary (`test_01`), a missing manifest (`test_02`), an unsupported platform (`test_03`), a successful download with checksum (`test_04`), `action_install` and its permissions (`test_07`, `test_12`), path traversal (`test_08`), tar and zip slip and link refusal (`test_10` to `test_17`), PATH shadowing (`test_18`), safe unlink (`test_19` to `test_21`), interrupted extraction (`test_22`) and the size cap (`test_23`). It is the evidence for `ensure_executable`, `_compute_is_installed` and `action_install`. Run with `test.py -u binary_downloader`.

### `test_binary_manifest_integration` [@ANCHOR: test_binary_manifest_integration]
Test anchor for the unmocked integration tests in `tests/test_binary_manifest_integration.py`, class `TestBinaryManifestIntegration` (tags `post_install`, `-at_install`, `integration`). `test_kopia_install_integration` installs a binary through the real `ensure_executable` path into a real `hams_bin`; `test_pure_python_symlink_engine` checks the tenant symlink creation and re-pointing; `test_pager_integration_batching` checks that `action_notify_tenants` creates incidents in batches. Only the DNS-based host check is patched out so the suite does not depend on live DNS. Run with `test.py -u binary_downloader`; it writes under the Odoo data directory.

### `test_binary_install_tour` [@ANCHOR: test_binary_install_tour]
Test anchor for the browser tour `TestBinaryDownloaderTour` in `tests/test_ui_tours_api.py` (class derived from `HamsHttpCase`, tags `post_install`, `-at_install`). The tour opens the Binary Manifests menu, creates a manifest named `tourbin` with a placeholder host and clicks "Install Now", which exercises `UX_BINARY_INSTALL` and `action_install`. The setup sets the admin language to US English and replaces `_assert_host_is_ssrf_safe`; teardown deletes the `tourbin_*` file the install writes, so later runs start clean. It needs a headless browser, so it is slower than the unit tests.

### `test_binary_manifest_views` [@ANCHOR: test_binary_manifest_views]
Test anchor for the view-rendering test of the manifest: `test_05_views_render` in `tests/test_binary_manifest.py` loads the list and form views with `get_view` and asserts that the `name` and `url` fields are present. The anchor is also cited in `views/binary_manifest_views.xml` as the test that covers both views (`view_binary_downloader_manifest_list` and `view_binary_downloader_manifest_form`). It checks that the XML compiles and carries the fields, not the permissions of the Install button.
