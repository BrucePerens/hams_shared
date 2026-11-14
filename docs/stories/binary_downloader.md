# Story: Downloading, Sharing, and Retiring Third-Party Binaries

As a **System Administrator**, I want the platform to **fetch, verify, and share third-party binaries
across tenants** (the separate websites hosted by one Odoo) on its own, so that I don't have to manually install command-line dependencies (like
`kopia`, a backup program) on every machine, and so that a stale or duplicate binary never lingers on disk after the last
record referencing it is gone.

## Scenario: Registering and Installing a Binary

An admin creates a `binary.manifest` record naming the binary, its download URL, checksum, and archive
type.

- Only `https://` URLs are accepted `binary_downloader:binary_manifest_check_url_scheme`.

- The binary's own name can never contain a path-traversal character `binary_downloader:binary_manifest_check_name_no_slashes`.

- A `tar.gz`/`zip` archive must name the specific member to extract `binary_downloader:binary_manifest_check_extract_member`.

`action_install()` triggers `ensure_executable()`, which resolves the manifest (current company first,
then the global fallback) and calls the shared download engine -- the same engine handles raw
binaries, tarballs, and zips, verifying the SHA-256 checksum (a fingerprint of the file's bytes) and refusing symlinked archive members
`binary_downloader:binary_utils_download_and_extract`.

`_compute_is_installed()` reports whether the binary is already on `PATH` (the operating system's list of program directories) or already present in the
shared `hams_bin` pool, using a stable, checksum-derived filename `binary_downloader:binary_utils_get_target_filename`.

## Scenario: Publishing a New Version and Notifying Tenants

A `binary.version` record captures one specific upstream release, with the identical URL/name/
extract-member validation as the manifest itself.

- Same URL-scheme check `binary_downloader:binary_version_check_url_scheme`.

- Same no-slashes-in-the-identifier check, on the version number instead of the name `binary_downloader:binary_version_check_version_no_slashes`.

- Same extract-member-required-for-archives check `binary_downloader:binary_version_check_extract_member`.

`_get_central_path()` derives a deterministic, checksum-based path in the shared central pool
   `binary_downloader:binary_version_get_central_path`, and `_compute_is_downloaded()` reports
   whether that exact file is already present and executable
   `binary_downloader:binary_version_compute_is_downloaded`.
3. Once a new version is downloaded to the pool, `action_notify_tenants()` walks every tenant currently
   pinned to an older version of the same manifest, batching `pager.incident` alerts per company so each
   tenant's own on-call rotation is notified through their own PagerDuty (an on-call alerting service) routing
   `binary_downloader:binary_version_action_notify_tenants`.

## Scenario: A Tenant Adopts, Upgrades, and Retires a Binary

1. A `binary.tenant.link` ties one website/tenant to one active version of one manifest. Its symlink
   path is computed from the tenant's own namespaced directory and the manifest's name, refusing to
   compute a path at all if that name were ever somehow unsafe -- a defense-in-depth check independent
   of the manifest's own name constraint
   `binary_downloader:binary_tenant_link_compute_symlink_path`.
2. Creating the link immediately applies the real OS-level symlink pointing at the tenant's chosen
   version `binary_downloader:binary_tenant_link_create`; changing which version is active
   re-applies it, but only when `active_version_id` itself actually changed, not on every write
   `binary_downloader:binary_tenant_link_write`.
3. `action_upgrade_to_latest()` repoints a tenant at the newest release of its manifest, or reports
   "already up to date" if there's nothing newer
   `binary_downloader:binary_tenant_link_action_upgrade_to_latest`.
4. Deleting the link removes the real on-disk symlink, not just the database row
   `binary_downloader:binary_tenant_link_unlink`.

## Scenario: Retiring a Binary Without Breaking a Sibling That Shares Its Bytes

Because two different manifests (or manifest/version pairs) can legitimately share the exact same
underlying binary content (an identical checksum), deleting one must never delete the shared on-disk
file out from under the other.

- `binary.manifest.unlink()` counts how many other manifest/version rows still reference its checksum before ever asking the shared cleanup helper to remove the file `binary_downloader:binary_manifest_unlink`.

- `binary.version.unlink()` runs the identical count of other rows sharing the checksum (deduplication) `binary_downloader:binary_version_unlink`.

- The shared cleanup helper itself only ever removes the on-disk file once both callers agree nothing else references it `binary_downloader:binary_utils_unlink_binary_file`.

## Anchor reference

One entry per anchor of the story, written from the code in `binary_downloader/models/`. Test files are under `binary_downloader/tests/`.

### `binary_downloader:binary_manifest_check_url_scheme` [@ANCHOR: binary_downloader:binary_manifest_check_url_scheme]
Purpose: refuse a non-HTTPS download address when a `binary.manifest` is saved. The method `_check_url_scheme()` is an `@api.constrains("url")` constraint in `models/binary_manifest.py`, run by Odoo on create and on every write of `url`; it takes no arguments and returns nothing. For each record whose `url` is set, the text with surrounding spaces removed must start with `https://`, otherwise it raises `ValidationError("Only https:// URLs are allowed for security reasons.")` and the save is rolled back.

Special concerns: this is the first of three checks; `_download_and_extract` checks the scheme again at download time and, separately, resolves the host and refuses non-public addresses (SSRF), so a saved https URL is not by itself trusted. A blank URL is stopped by the `url` not-empty SQL constraint. Test: `test_11_url_validation` in `tests/test_binary_manifest.py`.

### `binary_downloader:binary_manifest_check_name_no_slashes` [@ANCHOR: binary_downloader:binary_manifest_check_name_no_slashes]
Purpose: keep a binary name usable as a file name. `_check_name_no_slashes()` is an `@api.constrains("name")` constraint on `binary.manifest`; no arguments, no return value. It raises `ValidationError` when the name contains `/` or a backslash, or is exactly `.` or `..`. The name becomes part of the file name in `hams_bin` (`<name>_<hash>`) and of the tenant symlink path, so a name with a separator could otherwise point outside the directories the module owns.

Special concerns: `ensure_executable`, `_download_and_extract`, `_unlink_binary_file` and `_compute_symlink_path` repeat the check independently (defence in depth), so a record created by raw SQL still cannot escape. Uniqueness is separate: the SQL constraint `UNIQUE(name, company_id)` makes the name unique per company. Test: `test_09_constraints` in `tests/test_binary_manifest.py`.

### `binary_downloader:binary_manifest_check_global_name_unique` [@ANCHOR: binary_downloader:binary_manifest_check_global_name_unique]
Purpose: keep a binary name unique among manifests that have no company. `_check_global_name_unique()` is an `@api.constrains("name", "company_id")` constraint on `binary.manifest`; no arguments, no return value. `UNIQUE(name, company_id)` never fires for company-less rows (NULLs differ in SQL), so for the checked records that have no company it searches the company-less manifests with the same names and raises `ValidationError("The binary name must be unique per company!")` when any name occurs more than once.

Special concerns: rows with a company are left to the SQL constraint. Test: `test_binary_manifest.py` `test_09_1_global_manifest_name_is_unique`.

### `binary_downloader:binary_manifest_check_extract_member` [@ANCHOR: binary_downloader:binary_manifest_check_extract_member]
Purpose: make sure an archive manifest says which file to take out of the archive. `_check_extract_member()` is an `@api.constrains("archive_type", "extract_member")` constraint on `binary.manifest`; no arguments, no return value. When `archive_type` is `tar.gz` or `zip` and `extract_member` is empty it raises `ValidationError("Extract Member is required for <type> archives.")`; for `binary` (a raw file) the field is not needed.

Special concerns: only one member is ever extracted, the first whose name equals `extract_member` or ends with `/<extract_member>`, so the value is a file name (or trailing path), not a pattern. A wrong value is not caught here; the download fails later with "Member ... not found in archive". The constraint fires when the manifest is saved, never at `ensure_executable` time. Test: `test_11_1_extract_member_required_for_archives` in `tests/test_binary_manifest.py`.

### `binary_downloader:binary_utils_download_and_extract` [@ANCHOR: binary_downloader:binary_utils_download_and_extract]
Purpose: the one download engine used for raw binaries, `.tar.gz` and `.zip` archives. `binary_downloader.mixin._download_and_extract(cmd_name, url, checksum, archive_type, extract_member=None)` is an `@api.model` method in `models/binary_utils.py`, called by `binary.manifest.ensure_executable` and `binary.version.action_download_to_pool`.

| Argument | Type | Meaning |
|---|---|---|
| `cmd_name` | str | Binary name; becomes the file name prefix. A `..` component is refused. |
| `url` | str | Source address; must start with `https://`. |
| `checksum` | str | Expected SHA-256 (hex) of the whole downloaded file or archive. |
| `archive_type` | str | `binary`, `tar.gz` or `zip`. |
| `extract_member` | str or None | Archive member to take; defaults to `cmd_name`. |

Returns the absolute path `<data_dir>/hams_bin/<name>_<16 hex digits of sha256("<name>_<checksum>")>` of the executable (mode `0o750`). Errors (all `UserError`): not Linux on x86_64, aarch64 or armv7l; non-https URL; unsafe host or redirect (see `binary_utils_assert_host_is_ssrf_safe`); more than 1 GiB downloaded; checksum mismatch; archive member missing or a link; network or archive failure. Locking: takes the transaction-scoped PostgreSQL advisory lock `pg_advisory_xact_lock` keyed on name and checksum, so concurrent workers install once; the lock lasts until the transaction ends. Blocking network I/O with a 15 second timeout per request (a failed HEAD is only logged). An existing raw file is re-hashed and replaced if wrong; an existing extracted file is trusted. Writes go through `_atomic_write_target`. Tests: `test_04_successful_download_and_checksum`, the tar and zip tests `test_10` to `test_17`, `test_23_download_exceeding_size_cap_is_rejected` in `tests/test_binary_manifest.py`, and `tests/test_binary_manifest_integration.py`.

### `binary_downloader:binary_utils_get_target_filename` [@ANCHOR: binary_downloader:binary_utils_get_target_filename]
Purpose: the one place that decides the file name of an installed binary. `_get_target_filename(cmd_name, checksum)` is an `@api.model` method of `binary_downloader.mixin` taking the binary name and its SHA-256 checksum (both str) and returning `<cmd_name>_<id>`, where `<id>` is the first 16 hex digits of the SHA-256 of the text `<cmd_name>_<checksum>`.

Special concerns: pure computation, no file or database access, safe to call anywhere. The name is stable for a given name and checksum, and a new checksum gives a new file, so an upgrade never overwrites a running binary and two records with the same name and checksum share one file. Callers: `_compute_is_installed`, `_get_central_path`, `_unlink_binary_file`; `_download_and_extract` computes the same value inline. Test: `test_06_is_installed_compute` in `tests/test_binary_manifest.py`.

### `binary_downloader:binary_version_check_url_scheme` [@ANCHOR: binary_downloader:binary_version_check_url_scheme]
Purpose: the same HTTPS-only rule as the manifest, for a single upstream release. `binary.version._check_url_scheme()` is an `@api.constrains("url")` constraint (no arguments, no return value) that raises `ValidationError("Only https:// URLs are allowed for security reasons.")` unless the trimmed `url` starts with `https://`. It runs on create and on every change of `url`.

Special concerns: the download itself (`action_download_to_pool`, via `_download_and_extract`) checks the scheme again and applies the public-address (SSRF) rules, so this constraint is only the first line of defence. The `url` not-empty SQL check backs it up. Test: `test_version_constraints` in `tests/test_binary_version.py`.

### `binary_downloader:binary_version_check_version_no_slashes` [@ANCHOR: binary_downloader:binary_version_check_version_no_slashes]
Purpose: keep a version number harmless. `binary.version._check_version_no_slashes()` is an `@api.constrains("version_number")` constraint with no arguments and no return value. It raises `ValidationError` when `version_number` contains `/` or a backslash, or is exactly `.` or `..`, mirroring the manifest's name check.

Special concerns: the version number is not used as a file name today (it is only hashed into the advisory-lock key used while downloading), so this is a precaution so that a later use as a path component does not inherit a weaker rule than the name check. Uniqueness is `UNIQUE(manifest_id, version_number)`. Test: `test_version_constraints` in `tests/test_binary_version.py`.

### `binary_downloader:binary_version_check_extract_member` [@ANCHOR: binary_downloader:binary_version_check_extract_member]
Purpose: the archive rule for a release. `binary.version._check_extract_member()` is an `@api.constrains("archive_type", "extract_member")` constraint (no arguments, no return value): for `tar.gz` or `zip` it requires `extract_member` and raises `ValidationError("Extract Member is required for <type> archives.")` when it is empty. A `binary` (raw) release needs none.

Special concerns: it runs when the version is saved, so a bad record is rejected before any download starts. It does not check that the named member exists in the archive; that is found at download time. Test: `test_version_constraints` in `tests/test_binary_version.py`.

### `binary_downloader:binary_version_get_central_path` [@ANCHOR: binary_downloader:binary_version_get_central_path]
Purpose: where a release lives in the shared pool. `binary.version._get_central_path()` takes no arguments, requires exactly one record (`ensure_one`) and returns the absolute path `<data_dir>/hams_bin/<name>_<id>` built from `_get_target_filename(manifest name, checksum)`; `data_dir` comes from the Odoo configuration, default `/var/lib/odoo`.

Special concerns: the pool is not a separate store; a release and a manifest with the same name and checksum map to the same file. The method only builds the path and does not check that the file exists. Used by `_compute_is_downloaded`, `apply_symlink` (the link target) and the download action. Test: `test_get_central_path` in `tests/test_binary_version.py`.

### `binary_downloader:binary_version_compute_is_downloaded` [@ANCHOR: binary_downloader:binary_version_compute_is_downloaded]
Purpose: the `is_downloaded` flag of a release. `binary.version._compute_is_downloaded()` is `@api.depends("version_number", "checksum")`, non-stored, no arguments, no return value. For each record it sets `is_downloaded` to `True` when the record is saved, has a checksum, and the file at `_get_central_path()` exists and is executable (`os.access(path, os.X_OK)`); otherwise `False`.

Special concerns: it reads the file system on every evaluation and never writes. It does not re-check the checksum of the file on disk, so a corrupt file still counts. `binary.tenant.link.apply_symlink` uses it to decide whether to download first. Test: `test_compute_is_downloaded_reflects_the_real_central_pool_file` in `tests/test_binary_version.py`.

### `binary_downloader:binary_version_action_notify_tenants` [@ANCHOR: binary_downloader:binary_version_action_notify_tenants]
Purpose: tell tenants that a newer release exists. `binary.version.action_notify_tenants()` is a `@public_rpc` button method; it requires one record (`ensure_one`) and no other arguments. Access: raises `UserError` unless the caller is in the Binary Downloader Manager group or is an administrator (portal users can read versions, so the check is essential: without it any reader could raise real on-call pages).

Behaviour: it pages through `binary.tenant.link` rows of the same manifest whose `active_version_id` is not this release (100 per page, by id), groups them by the website's company, and creates one `pager.incident` per link (source `binary_update`, severity `medium`, description "New binary version X is available for Y", with the website) as the service account, one company at a time; it commits after each page. Returns a client notification action ("Tenants notified via PagerDuty."). Side effects: real incidents in the `pager_duty` module, so repeating the action re-pages. Tests: `test_pager_integration_batching` in `tests/test_binary_manifest_integration.py` and `test_action_download_to_pool_and_notify_tenants_reject_an_unprivileged_caller` in `tests/test_binary_version.py`.

### `binary_downloader:binary_tenant_link_compute_symlink_path` [@ANCHOR: binary_downloader:binary_tenant_link_compute_symlink_path]
Purpose: the path a tenant uses to run its binary. `binary.tenant.link._compute_symlink_path()` is `@api.depends("website_id", "manifest_id")`, non-stored, no arguments and no return value. It sets `symlink_path` to `<data_dir>/tenant_bins/site_<website id>/<manifest name>`, giving each website its own directory. It sets `False` when the website or manifest is missing, or when the manifest name contains a slash or a `..` component, a check independent of the manifest's own name constraint.

Special concerns: pure computation, no file access; the symlink itself is made by `apply_symlink`. `data_dir` defaults to `/var/lib/odoo`. Test: `test_compute_symlink_path_rejects_a_manifest_name_with_a_slash` in `tests/test_binary_tenant_link.py`.

### `binary_downloader:binary_tenant_link_create` [@ANCHOR: binary_downloader:binary_tenant_link_create]
Purpose: make the link take effect immediately. `binary.tenant.link.create(vals_list)` is an `@api.model_create_multi` override; `vals_list` is a list of value dictionaries as for any Odoo create. It calls the standard create and then `apply_symlink()` on every new record, downloading the chosen version to the pool first if it is missing. Returns the new records.

Special concerns: it changes the file system (creates `tenant_bins/site_<id>/` with mode `0o750` and the symlink), and a download failure raises `UserError` and rolls the record back. The pair (website, manifest) is unique. Test: `tests/test_binary_tenant_link.py` and `test_pure_python_symlink_engine` in `tests/test_binary_manifest_integration.py`.

### `binary_downloader:binary_tenant_link_write` [@ANCHOR: binary_downloader:binary_tenant_link_write]
Purpose: re-point the symlink when the tenant changes version. `binary.tenant.link.write(vals)` (`vals` is the usual dictionary of field values) calls the standard write and then `apply_symlink()` on each record, but only when `active_version_id` is in `vals`; other edits (such as the name) do not touch the file system. Returns the result of the standard write (`True`).

Special concerns: `apply_symlink` replaces a wrong link and leaves a correct one alone; it downloads the version first if needed, which can take time and can raise `UserError`. Changing the manifest does not itself re-link. Test: `test_write_re_applies_the_symlink_only_when_active_version_changes` in `tests/test_binary_tenant_link.py`.

### `binary_downloader:binary_tenant_link_unlink` [@ANCHOR: binary_downloader:binary_tenant_link_unlink]
Purpose: delete the real symlink together with the database row. `binary.tenant.link.unlink()` removes, for each record, the file at `symlink_path` when it exists (`os.path.lexists`, so a dangling link also goes), logging a warning instead of failing if the removal raises `OSError`, then calls the standard unlink. No arguments; returns `True`.

Special concerns: only the per-tenant link is removed; the shared binary in `hams_bin` is untouched, because other tenants may use it. Deleting a website or a manifest cascades to the link rows through `ondelete="cascade"` and goes through this method. Test: `test_unlink_removes_the_real_os_level_symlink` in `tests/test_binary_tenant_link.py`.

### `binary_downloader:binary_tenant_link_action_upgrade_to_latest` [@ANCHOR: binary_downloader:binary_tenant_link_action_upgrade_to_latest]
Purpose: the "1-Click Upgrade to Latest" button. `binary.tenant.link.action_upgrade_to_latest()` is `@public_rpc` and needs one record. It finds the `binary.version` of the same manifest with the newest `release_date` (ties go to the highest id).

Returns: `False` when the manifest has no versions; a client notification "Up to Date" (type info) when the tenant already uses the newest; otherwise it sets `active_version_id` (which triggers `write`, hence the download and re-symlink) and returns an "Upgrade Successful" notification with the version number and a page reload. Errors: `UserError` from the download. It does not check the caller's group beyond the model's access rules (write access to the link is needed). Tests: `test_action_upgrade_to_latest_repoints_to_the_newest_version` and `test_action_upgrade_to_latest_is_a_no_op_when_already_current` in `tests/test_binary_tenant_link.py`.

### `binary_downloader:binary_manifest_unlink` [@ANCHOR: binary_downloader:binary_manifest_unlink]
Purpose: delete a manifest without breaking anything that still shares its file. `binary.manifest.unlink()` takes no arguments and returns `True`. It runs as the service account, builds the `(name, checksum)` key of each record being deleted, asks `_count_binary_file_references` how many manifest and version rows map to the same file, subtracts the rows being deleted in this same call, and calls `_unlink_binary_file` only for files nothing else references, then does the standard unlink.

Special concerns: the file name depends on both name and checksum, so two records with the same checksum but different names do not protect each other's files. Deleting several records that share one file in a single call still removes it. A removal failure is logged, not raised. Tests: `test_19_unlink_privilege_escalation`, `test_20_unlink_deletes_the_real_file_when_the_checksum_is_not_shared` and `test_21_unlink_batch_deletes_the_file_when_every_sharing_record_is_removed_together` in `tests/test_binary_manifest.py`.

### `binary_downloader:binary_version_unlink` [@ANCHOR: binary_downloader:binary_version_unlink]
Purpose: the same safe deletion for a release. `binary.version.unlink()` (no arguments, returns `True`) builds `(manifest name, checksum)` keys, counts references with `_count_binary_file_references` as the service account, subtracts the rows deleted in this call and removes the file with `_unlink_binary_file` only when nothing else uses it, then does the standard unlink.

Special concerns: a version's file is named from its manifest's name, so a manifest and a version of the same name and checksum share one file and keep each other alive. Tenant links that still point at the version are deleted or blocked by the foreign key (`active_version_id` is required), not by this method. Test: `test_unlink_does_not_remove_a_still_referenced_binary_file` in `tests/test_binary_version.py`.

### `binary_downloader:binary_utils_unlink_binary_file` [@ANCHOR: binary_downloader:binary_utils_unlink_binary_file]
Purpose: delete an installed binary file. `_unlink_binary_file(cmd_name, checksum)` is an `@api.model` method of `binary_downloader.mixin`; both arguments are str. It computes the file name with `_get_target_filename`, resolves the real path under `<data_dir>/hams_bin` and refuses (`UserError`, "Path traversal attempt detected") a name with a `..` component or a resolved path that is outside `hams_bin`. If the file exists it is unlinked; an `OSError` is logged as a warning and not raised. Returns nothing.

Special concerns: it does not count references; the callers (`binary.manifest.unlink`, `binary.version.unlink`) must decide that first with `_count_binary_file_references`. It does not remove tenant symlinks, which would dangle. Test: `test_unlink_ignores_a_differently_named_manifest_sharing_the_checksum` and `test_unlink_does_not_remove_a_still_referenced_binary_file` in `tests/test_binary_version.py`.
