---
name: hams-dev-box-home-not-persistent
description: "On hams.com/hams_open, ~bruce (the dev box's home directory) will not migrate to the production server -- only content committed into one of the project's git repositories persists."
metadata: 
  node_type: memory
  pinned: false
  originSessionId: 58453db6-5ee8-4667-a749-b2d8dad1938a
  modified: 2026-08-25T20:42:54.703Z
---

The user told Claude directly: "~bruce won't migrate with the server. Anything we want to be persistent should be in one of our three repositories." This means every file, script, cron job, or piece of configuration Claude creates under `/home/bruce/` on the current dev box -- `~/.local/bin`, `~/.secrets`, crontab entries, `/etc/letsencrypt`, and similar -- is dev-box-local and ephemeral relative to the real hams.com deployment. None of it survives a migration to production infrastructure unless it is also captured, in some form, inside one of the project's git repositories (hams_com, hams_open, hams_shared).

The practical consequence: when Claude builds a tool, script, or piece of automation on this dev box for a real, ongoing operational need (not a one-off local task), it should also be committed into the appropriate repository -- e.g. `hams_shared/tools/` for a general-purpose script -- rather than left to live only under `~bruce`. Secrets themselves obviously can't be committed to a repo, but the *mechanism* that uses them (scripts, renewal automation, systemd units) should be repo-committed, with only the live secret value expected to live in a proper secret store on the real server rather than a developer's home directory. When Claude notices it has built something operationally important only under `~bruce`, that's a signal to either move/duplicate it into a repository or explicitly flag in project documentation that it still needs a real, persistent home before production deployment.
