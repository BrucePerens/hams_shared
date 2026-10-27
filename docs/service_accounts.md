# Service Account Catalog

This document details the Service User IDs (`is_service_account=True`) defined across the `hams_open` repository modules, their intended use cases, and their privilege levels.

Under the Zero-Sudo architecture, no Python code is allowed to use `sudo()`. Instead, operations that require elevated privileges must execute within the context of a dedicated service account using `with_user()`.

A service account is a `res.users` record flagged `is_service_account` (the flag comes from the `zero_sudo` module). The rule and its reasoning are in [MASTER 01](adrs/MASTER_01_SECURITY_ZERO_SUDO.md) and [`modules/zero_sudo.md`](modules/zero_sudo.md): service accounts are blocked from interactive web login, and `_get_service_uid` refuses any service account that holds `base.group_system` or `base.group_erp_manager`, so none of the accounts below is a superuser.

How to read the tables: "Model access" is read from each module's `security/ir.model.access.csv` for the account's groups, written as the model's XML ID without the `model_` prefix (for example `backup_job` is `model_backup_job`), then a colon and the letters R (read), W (write), C (create) and D (delete). Groups that `base.group_user` or `website.group_multi_website` add to an account are not listed. Record rules (`ir.rule`) narrow many of these grants to some rows (for example by website or company); read the module's `security` folder for the exact domains. Accounts marked as having no model access act through other means, such as a daemon key.

## Core system accounts (`zero_sudo`)

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `zero_sudo.config_service_internal` | Reads and writes `ir.config_parameter` (system parameters). | `base.ir_config_parameter:RWCD` |
| `zero_sudo.gdpr_service_internal` | Runs the erasure chain for GDPR (the European Union's data protection regulation) requests: rewrites the user record, deletes the member's pages, posts and attachments. | `base.res_users:RW`, `website.website_page:RD`, `website_blog.blog_post:RD`, `website_blog.blog_blog:RD`, `base.ir_attachment:RD` |
| `zero_sudo.mail_service_internal` | Sends notification mail and posts chatter messages on behalf of other code; bookkeeping rows of the models listed. | `backup_config:RW`, `backup_job:RW`, `content_violation_report:RW`, `pager_incident:RW`, `content_violation_appeal:RW`, `website_blog.blog_post:R`, `mail.mail_message:RWC`, `mail.mail_mail:RWC`, `mail.mail_followers:RWC`, `mail.mail_template:RWC`, `base.res_partner:RW`, `base.res_users:R`, `base.res_company:R`, `mail.mail_alias_domain:R` |
| `zero_sudo.odoo_facility_service_internal` | Deep framework reads (module list, static-file scan) and the zero_sudo key-value store; it has no write access to modules and cannot install or upgrade anything. | `zero_sudo_kv:RWCD`, `zero_sudo_security_log:RC` |
| `zero_sudo.orphaned_record_owner` | Not an acting account. It owns rows that were reassigned to it before erasure stopped anonymizing ownership; it holds no groups and is never used with `with_user()`. | none |
| `zero_sudo.user_lockout_service_internal` | Deactivates a user record (`active` False) when a member uses the unsubscribe-and-lock-out link. | `base.res_users:RW` |

## `backup_management`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `backup_management.user_backup_service_internal` | Backup configuration, job and snapshot bookkeeping, and the backup notification mail (it also holds the central mail service group). | `backup_config:RW`, `backup_job:RWC`, `base.res_partner:RW`, `base.res_users:R`, `base.res_company:R`, `mail.mail_alias_domain:R`, `binary_downloader.binary_manifest:R`; `backup_config:RW`, `backup_job:RW`, `content_violation_report:RW`, `pager_incident:RW`, `content_violation_appeal:RW`, `website_blog.blog_post:R`, `mail.mail_message:RWC`, `mail.mail_mail:RWC`, `mail.mail_followers:RWC`, `mail.mail_template:RWC` |

## `binary_downloader`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `binary_downloader.user_binary_downloader_service` | Manages the binary manifests, versions and tenant links. | `binary_manifest:RWCD`, `binary_version:RWCD`, `binary_tenant_link:RWCD`, `base.res_company:R` |

## `caching`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `caching.user_caching_service` | Reads `caching.*` system parameters only (a record rule limits it) and the module list. | `base.ir_config_parameter:R`, `base.ir_module_module:R` |

## `cloudflare`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `cloudflare.user_cloudflare_purge` | Cache-purge queue and the per-tenant key. | `cloudflare_purge_queue:RWCD`, `website.website:R`, `base.res_company:R`, `base.ir_config_parameter:R`, `cloudflare_tenant_key:RC` |
| `cloudflare.user_cloudflare_service` | Deprecated and shipped inactive, with no groups. | none |
| `cloudflare.user_cloudflare_trusted_ip` | Maintains the trusted Cloudflare address ranges in system parameters. | `cloudflare_trusted_ip_utils:RWCD`, `base.ir_config_parameter:RWC` |
| `cloudflare.user_cloudflare_tunnel` | Tunnel records and routes. It also holds the Daemon Key Manager group, so it can read and write API keys of service accounts (see that account). | `cloudflare_tunnel:RWCD`, `cloudflare_tunnel_route:RWCD`, `website.website:R`, `base.res_company:R`, `cloudflare_tenant_key:RC`, `cloudflare_hostname_pending_delete:RWCD`; `daemon_key_registry:RWCD`, `base.res_users_apikeys:RWCD`, `base.res_users:R`, `base.ir_cron:RW` |
| `cloudflare.user_cloudflare_waf` | Web application firewall rules, IP bans, configuration backups and the purge queue. | `cloudflare_waf_rule:RWCD`, `cloudflare_cache_rule:R`, `cloudflare_ip_ban:RWCD`, `cloudflare_config_backup:RWCD`, `cloudflare_config_manager:R`, `website.website:R`, `base.res_company:R`, `base.res_users:R`, `cloudflare_purge_queue:RWCD`, `cloudflare_tenant_key:RC` |

## `compliance`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `compliance.user_compliance_service` | Installs and refreshes the legal pages (website, pages, views) and the compliance documents. | `website.website:RW`, `website.website_page:RWC`, `base.ir_ui_view:RWC`, `base.res_groups:R`, `base.res_company:R`, `compliance_document:RWCD` |

## `daemon_key_manager`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `daemon_key_manager.user_daemon_key_manager_service` | Issues, rotates and reads the API keys of service accounts and keeps the key registry. | `daemon_key_registry:RWCD`, `base.res_users_apikeys:RWCD`, `base.res_users:R`, `base.ir_cron:RW` |

## `database_management`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `database_management.user_database_management_service` | Reads PostgreSQL statistics and runs the optimize, high-availability and explain wizards. | `database_table_stat:R`, `database_query_stat:R`, `database_activity:R`, `database_index_stat:R`, `database_pg_setting:R`, `pg_optimize_wizard:RWCD`, `pg_ha_wizard:RWCD`, `pg_explain_wizard:RWCD`, `database_index_advisor:R`, `database_replication_stat:R` |

## `distributed_redis_cache`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `distributed_redis_cache.cache_manager_service_internal` | Identity of the cache manager daemon; no Odoo model access of its own. | none |

## `edge_routing`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `edge_routing.edge_routing_service_account` | Read-only access to the domain, user-site group, company, partner and bank records the edge routing daemon needs. | `base.res_users:R`, `base.res_company:R`, `base.res_partner:R`, `base.res_partner_bank:R`, `mail.discuss_channel:R`, `edge_routing_domain:R`, `user_websites_group:R` |

## `form_shield`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `form_shield.user_form_shield_service` | Writes and purges the daily refusal counters from public request handlers and the purge cron. No other model. | `form_shield_rejection:RWCD` |

## `hams_ai_safety_reporting`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `hams_ai_safety_reporting.user_ai_safety_reporting_service` | Creates helpdesk tickets (child-safety self-reports) and cannot read them back. | `hams_helpdesk.hams_helpdesk_ticket:C` |

## `hams_helpdesk`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `hams_helpdesk.user_ai_triage_service` | Read-only access to tickets of types `general` and `hams_local_relay` that are not marked suspicious. | `hams_helpdesk_ticket:R` |
| `hams_helpdesk.user_exception_reporter_service` | Writes the automatic exception tickets and their fingerprint and member rows. | `hams_helpdesk_ticket:RWC`, `hams_helpdesk_exception_fingerprint:RWC`, `hams_helpdesk_exception_member:RWC` |
| `hams_helpdesk.user_github_webhook_service` | Creates and reads tickets for GitHub webhook events. | `hams_helpdesk_ticket:RC` |
| `hams_helpdesk.user_helpdesk_service` | Routes tickets, applies the on-duty assignment and handles escalations, with the helpdesk manager's access. | `hams_helpdesk_ticket:RWCD`, `base.res_users:R`, `calendar.calendar_event:R`, `hams_helpdesk_ticket_suspicion:RWCD`, `hams_helpdesk_exception_fingerprint:RD` |
| `hams_helpdesk.user_mail_ingest_service` | Identity the inbound-mail daemon calls the helpdesk with; read access to tickets and partners. | `hams_helpdesk_ticket:R`, `base.res_partner:R` |

## `hams_s3`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `hams_s3.s3_manager_service_internal` | Intended for the S3 (Amazon's object storage protocol) storage backend settings. Its group carries no model access, and the file that defines it says it is a draft that was never tested end to end. | none |

## `knowledge`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `knowledge.user_knowledge_service_account` | Reads, writes and deletes knowledge articles, posts their messages, and creates and deletes `ir.model.data` rows. | `knowledge_article:RWCD`, `mail.mail_message:RWC`, `mail.mail_followers:RWCD`, `base.res_partner:RW`, `base.res_users:R`, `base.res_company:R`, `mail.mail_alias_domain:R`, `mail.discuss_channel:RW`, `base.ir_model_data:RWCD` |

## `pager_duty`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `pager_duty.user_pager_incident_creator` | Only creates incidents (and can read and write the ones it may see). | `pager_incident:RWC` |
| `pager_duty.user_pager_mcp_triage_service` | Read-only view of incidents for the triage tool server. | `pager_incident:R` |
| `pager_duty.user_pager_service_internal` | The monitoring daemon: incidents, checks, on-call calendar events and log-search jobs. | `pager_incident:RWC`, `pager_check:RWC`, `base.res_users:R`, `base.res_partner:R`, `base.res_company:R`, `mail.mail_activity_type:R`, `mail.mail_message:RC`, `calendar.calendar_event:RWC`, `calendar.calendar_attendee:RWC`, `mail.mail_template:R`, `pager_log_search_job:RWCD`, `pager_log_file:R`, `pager_log_pattern:R` |

## `parking`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `parking.user_parking_service` | Public handler for parked domains: reads parked domains, records inquiries. | `parking_domain:R`, `parking_inquiry:RC` |

## `ses_webhook`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `ses_webhook.user_ses_webhook_service_internal` | Records Amazon SES (Simple Email Service) delivery notifications and bounces; its record rules cover every tenant company. | `ses_webhook_domain:R`, `ses_webhook_log:RC`, `ses_webhook_pending_submission:RC`, `mail.mail_mail:RWC`, `mail.mail_blacklist:RWC` |

## `tenant_sites`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `tenant_sites.user_tenant_sites_service` | Routes requests to tenant sites: reads the site, host and website records and can create views. | `tenant_site:R`, `tenant_site_host:R`, `tenant_site_own_host:R`, `website.website:R`, `base.res_users:R`, `base.res_company:R`, `base.res_partner:R`, `edge_routing.edge_routing_domain:R`, `website.website_page:R`, `base.ir_module_module:R`, `base.ir_model_data:R`, `base.ir_ui_view:RWC`, `base.ir_asset:R`, `base.res_groups:R`, `tenant_site_kept_module:R` |

## `user_websites`

| XML ID | What it is for | Model access |
|--------|----------------|--------------|
| `user_websites.user_gdpr_export_service` | Issues and reads the download tokens of GDPR data exports. | `ham_gdpr_export_token:RWC` |
| `user_websites.user_websites_service_account` | "System Provisioner": creates and edits member sites, pages, blogs, groups and the moderation records on behalf of members, after the calling code has checked ownership. | `website.website_page:RWCD`, `website_blog.blog_post:RWCD`, `user_websites_group:RWCD`, `website_blog.blog_blog:RWCD`, `base.res_users:RW`, `base.res_groups:RWCD`, `website.website_rewrite:RWCD`, `base.ir_ui_view:RWCD`, `base.res_partner:RW`, `content_violation_appeal:RWCD`, `mail.mail_message:RWC`, `mail.mail_template:R`, `mail.mail_followers:RWCD`, `website.website:R`, `mail.mail_message_subtype:R`, `mail.mail_alias_domain:R`, `mail.discuss_channel_member:RWCD`, `base.res_company:R`, `mail.discuss_channel:RW`, `base.ir_actions_report:R`, `base.res_partner_bank:R`, `user_websites_weekly_digest_view:R`, `user_websites_public_directory_view:R`, `user_websites_content_routing_view:R`, `website.website_menu:RW`, `edge_routing.edge_routing_domain:R`, `base.ir_attachment:RWC`, `website_blog.blog_post:RWCD`; `content_violation_appeal:RWCD` |

## Notes

* `cache_manager.py`'s standalone daemon process connects directly to PostgreSQL, not through the Odoo ORM (object-relational mapper) layer that the tables above describe, so it also has a dedicated raw PostgreSQL role, not an Odoo `res.users` record: `cache_manager_ro`, created by `distributed_redis_cache/scripts/provision_cache_manager_db_role.py` and granted `CONNECT` on the daemon's target database and nothing else.
* The SES Webhook account's `company_ids` grows automatically as tenants configure webhook domains (`_sync_service_account_companies()` in `ses_webhook_domain.py`); it is never a hardcoded list.
* `user_websites.user_websites_service_account` is deliberately not a member of the user-websites administrator group: that group is a human moderation group which implies `base.group_user` and carries rules covering every row.
