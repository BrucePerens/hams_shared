# Approved backup encryption changes

Bruce, 2026-11-09: no agent may change, regenerate, move or overwrite backup key material or backup encryption settings. The lint
`tools/check_backup_encryption_changes.py` fails a change to provisioning or backup code that adds or changes a pgBackRest cipher
setting, creates a stanza, runs `kopia repository create|connect|change-password|set-parameters`, generates a backup key, or writes
over a file that holds one, unless THIS change adds an entry here. An entry older than the change licenses nothing.

Format, one line per approval (the date is Bruce's, real, and not in the future):

    - <file path or glob> | <rules: cipher, stanza, kopia-repo, keygen, secret-write, or *> | approved by Bruce on YYYY-MM-DD | <why>

Add an entry only with Bruce's own words in the pull request; the reviewer checks them. Agents never add one on their own.
The Claude Code hook `tools/claude_hooks/guard_backup_secrets.py` separately stops agents from touching the live keys on any host.

## Approvals

(none)
