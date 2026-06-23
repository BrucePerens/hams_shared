# Service Account Catalog

This document details the Service User IDs (`is_service_account=True`) defined across the `hams_community` repository modules, their intended use cases, and their privilege levels.

Under the Zero-Sudo architecture, no Python code is allowed to use `sudo()`. Instead, operations that require elevated privileges must execute within the context of a dedicated service account using `with_user()`.

## Core System Accounts (`zero_sudo`)

| XML ID | Description | Privileges |
|--------|-------------|------------|
| `zero_sudo.mail_service_internal` | Central Mail Service Account | Can dispatch emails, read mail templates, and bypass mail tracking limits. Used for sending notification emails. |
| `zero_sudo.odoo_facility_service_internal` | Odoo Facility Service Account | Highest level. Can interact with `ir.module.module` to install/upgrade Odoo applications. |
| `zero_sudo.config_service_internal` | Central Configuration Service | Can read and mutate `ir.config_parameter` global system parameters. |
| `zero_sudo.cache_invalidation_service_internal` | Central Cache Invalidation | Capable of triggering global ORM registry flushes and invalidating web controller caches. |

## Feature Modules

### User Websites (`user_websites`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `user_websites.user_websites_service_account` | General Service Account | Low-privilege account for routine read/write operations within the user_websites scope. |
| `user_websites.user_websites_service_privileged` | Privileged Account | Elevated permissions strictly bounded to module administration tasks (e.g., creating portals, overriding moderation). |

### Cloudflare (`cloudflare`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `cloudflare.user_cloudflare_service` | General Service Account | General reads and metric updates. |
| `cloudflare.user_cloudflare_purge` | Cache Purge Account | Can execute targeted cache invalidations on Cloudflare Edge. |
| `cloudflare.user_cloudflare_waf` | WAF Manager Account | Can create, update, and lift IP bans and WAF rules. |
| `cloudflare.user_cloudflare_tunnel` | Tunnel Manager | Can provision and delete secure edge tunnels. |

### Pager Duty (`pager_duty`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `pager_duty.user_pager_service_internal` | Pager Service Internal | Can read/write heartbeat monitors, resolve alerts, and access underlying check data. |
| `pager_duty.user_pager_incident_creator` | Incident Creator | Limited privilege. Only allowed to create new incident tickets (cannot resolve them or modify existing checks). |

### Backup Management (`backup_management`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `backup_management.user_backup_service_internal` | Backup Service Account | Low-privilege read access for backup reporting. |
| `backup_management.user_backup_service_privileged` | Privileged Backup Account | Can initiate PostgreSQL database dumps and sync them to remote S3 storage. |

### Binary Downloader (`binary_downloader`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `binary_downloader.user_binary_downloader_service` | Downloader Service | Handles scheduled fetches of remote binaries, verifies checksums. |
| `binary_downloader.user_binary_downloader_privileged` | Privileged Downloader | Can stage verified binaries for installation and modify system paths. |

### Caching (`caching`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `caching.user_caching_service` | Caching Service | Routine memory reads and lightweight cache clears. |
| `caching.user_caching_service_privileged` | Privileged Cache Admin | Deep cache invalidation and system memory controller interactions. |

### Compliance (`compliance`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `compliance.user_compliance_service` | Compliance Service | Generates compliance audit trails and reads privacy flags. |
| `compliance.user_compliance_service_privileged` | Privileged Compliance Admin | Executes automated GDPR/CCPA anonymization scrubs and irreversible data deletion. |

### Daemon Key Manager (`daemon_key_manager`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `daemon_key_manager.user_daemon_key_manager_service` | Key Manager Service | Reads current tokens for daemons. |
| `daemon_key_manager.user_daemon_key_manager_privileged` | Privileged Key Admin | Rotates and issues new authentication tokens for background daemon processes. |

### Database Management (`database_management`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `database_management.user_database_management_service` | Database Management Service | Scans PostgreSQL tables for bloat and reports performance metrics. |
| `database_management.user_database_management_privileged` | Privileged DB Admin | Triggers vacuum operations and index rebuilds. |

### Distributed Redis Cache (`distributed_redis_cache`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `distributed_redis_cache.cache_manager_service_internal` | Cache Manager | Interfaces with the Redis cluster to distribute object states globally. |

### Hams Helpdesk (`hams_helpdesk`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `hams_helpdesk.user_helpdesk_service` | Helpdesk Service Account | Routes incoming tickets and handles SLA alerts. |
| `hams_helpdesk.user_helpdesk_service_privileged` | Privileged Helpdesk Admin | Escalates unresolved threads and manages core SLA configurations. |

### Knowledge (`knowledge`)
| XML ID | Description | Privileges |
|--------|-------------|------------|
| `knowledge.user_knowledge_service_account` | Knowledge System | Reads and organizes articles. |
| `knowledge.user_knowledge_service_privileged` | Privileged Knowledge Admin | Publishes automated support articles and modifies the root hierarchy structure. |
