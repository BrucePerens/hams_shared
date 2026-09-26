---
name: gemini-checkin
description: How the Gemini session on bruce-yoga checks code, skills and lessons into the three repositories using its deploy keys, alongside the Claude sessions that share them.
---

# Gemini check-in routine (bruce-yoga)

This complements `antigravity-workflow` (commit and push stable work) and `project-experience` (the log of hard-learned traps). It adds the
parts that only apply when several agents on different machines write to the same three repositories.

## The three repositories and their keys

| Repository | Visibility | key file on bruce-yoga (`%USERPROFILE%\.ssh\hams_deploy\`) |
| --- | --- | --- |
| `hams_com` | private | `hams_com` |
| `hams_open` | public | `hams_open` |
| `hams_shared` (a submodule of `hams_open`, at `hams_open/hams_shared`) | public | `hams_shared` |

GitHub allows a deploy key on only one repository, so each repository has its own key. Every remote is the ordinary
`git@github.com:BrucePerens/<repository>.git`; each checkout is told which key to use with its own setting,
`git config core.sshCommand "ssh -i <key file> -o IdentitiesOnly=yes"`. There are no ssh host aliases and `~/.ssh/config` is not used.
`devbox_tools/bruce_yoga_git_setup.ps1` (Windows) or `.sh` in `hams_com` installs the keys and sets that up. Commits from this machine are authored
`Gemini (bruce-yoga)`, so a reader can tell them from the Claude sessions' commits.

Never write a private key, token or password into any repository. `hams_open` and `hams_shared` are public; do not put proprietary,
patent or business material in them (it belongs in `hams_com`).

## Before you start work

1. In each repository you will touch: `git fetch origin`, then `git status -sb`. If you are behind, `git pull --rebase --autostash` first.
   Several Claude sessions commit all day; the ingestion files under `hams_com/ingest/` are edited from both sides.
2. Read the newest entries of `hams_shared/agents/skills/project-experience/SKILL.md` and the files in `hams_shared/agents/skills/claude-memory/`
   (the Claude sessions' standing lessons, committed as policy). They record traps that will cost you an hour if you rediscover them.

## Checking in code (Mandatory PR Workflow)

**Never push directly to `main` (or any other protected branch) on `hams_com`, `hams_open`, or `hams_shared`.** ALL changes MUST go through a pull request (see `AGENTS.md` and ADR 0102).

* Create a dedicated feature branch for your work: `git checkout -b <descriptive-branch-name>`.
* Commit small, stable chunks, and only your own files by explicit path: `git add path1 path2`, then `git commit -m "..." -- path1 path2`. Never `git add -A` or `git commit -a`. Check `git diff -- path` first.
* Push your branch to origin: `git push -u origin <descriptive-branch-name>`.
* Open a pull request: `gh pr create --title "..." --body "..."`.
* Verify CI checks (such as `burn-list`) pass using `gh pr checks <PR-number>`.
* Once CI passes and requirements are met, merge via `gh pr merge <PR-number> --merge`.
* After merging, switch back to `main`, pull the merge commit (`git checkout main && git pull --rebase`), and delete the feature branch.
* A change to `hams_shared` is two PRs: the change inside `hams_open/hams_shared` (PR and merge it from inside `hams_open/hams_shared`), then a second PR in `hams_open` that updates the `hams_shared` submodule pointer (PR and merge it in `hams_open`). Do them in that order.
* Do not delete other people's branches, stashes or files.

## Checking in skills and lessons

* A new reusable procedure becomes a skill: `hams_shared/agents/skills/<name>/SKILL.md` with the `name` and `description` header used by the others.
  Skills that mention proprietary material go in `hams_com/agents/skills/` instead.
* A hard-learned trap (a linter surprise, a tool that fails silently, a wrong assumption that cost time) is appended to
  `hams_shared/agents/skills/project-experience/SKILL.md` as "The Trap" and "The Solution", in the same commit as the fix that taught it.
* A correction from Bruce that should apply to every future session ("always ...", "never ...") is a standing lesson: write it as one short file in
  `hams_shared/agents/skills/claude-memory/` (name it for the rule, explain why in full sentences, cite the date), so Claude sessions and Gemini both
  read it. Do not copy one-off task status into it.
* Findings you cannot act on now go in `hams_com/night_shift_todo/<priority>/` as one file each (see its README), not only in chat.

## Sending finished courses to hams.com

The large course output (chapter and form JSON, images, resources, venues) is **gitignored on purpose** and never goes to GitHub or Git LFS.
It reaches the production server, `hams.com`, only through the development machine, which is the one machine with a route to it:

1. On bruce-yoga, finish a batch under `workspace/hams_com/ham_training/data/course_HAM_TECH/` or `ics_training/data/course_ICS/`
   (`chapters`, `images`, `resources`, `venues`, `forms`). Run the pipeline's own validators first (`ingest/validate_forms.py` and the checks the
   `start-ingestion` skills name). Do not hand over a batch that fails them.
2. Record the hand-off as one to-do file, checked in like any other code: `hams_com/night_shift_todo/medium/send-courses-<yyyy-mm-dd>-<8 hex characters>.md`
   (header fields as in that directory's README) naming the course, the directories and file counts, and the validator results. Push it.
3. A session on the development machine (or the night-watch session that processes that queue) runs
   `python3 devbox_tools/send_courses_to_hams_com.py` (a dry run), then again with `--apply`. It pulls the directories from bruce-yoga with `scp`,
   then sends them with `rsync` to the module tree Odoo loads on hams.com. Nothing is deleted at either end.
4. When Odoo is running on hams.com, the same session loads the records with `ingest/push_course_to_odoo.py` and marks the to-do done.

Gemini never needs a login on hams.com and must not ask for one. If a batch must be re-sent, say so in the to-do file; re-sending overwrites files of the same name.

## When something goes wrong

If `git push` fails with a permission error, the deploy key for that repository is read-only or missing: tell Bruce which repository and stop,
do not try another key. If a rebase conflicts in a file you did not write, stop and report it rather than resolving another agent's work.
