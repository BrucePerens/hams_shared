# ADR 0099: Commit Discipline in a Working Tree Shared by Concurrent Agents

## Status
Accepted

## Context

The development box runs many Claude sessions at once, and they share one checkout of each
repository. They therefore share more than the files: they share the single git index
(`.git/index`) and the single `main` branch reference. Every session commits as
`BrucePerens <bruce@perens.com>`, so a commit's authorship identifies nothing about which session
made it.

This produces a failure that no session causes on its own and that none of the usual habits prevent.
A session stages its own change, another session stages something unrelated a second later, and the
first session's `git commit` takes the whole index. The result is a commit whose message describes
one change and whose content is two, pushed under a description that is now false.

It has happened repeatedly, and the same discipline would have prevented each instance:

* **2026-09-14.** A bare `git commit` intended for a to-do file swept in another session's staged
  Rust changes -- 199 of the commit's 201 lines -- and pushed them under a to-do-only message.
* **2026-09-14.** One session's `git reset` orphaned another session's commit (`6acbd479`), and an
  unscoped `git commit --amend` produced a mislabeled commit (`162bb494`).
* **2026-09-15.** A session used a private index correctly for one file, then used
  `git commit -- docs/BRUCE_ACTION_ITEMS.md` for another and pushed a peer's uncommitted edit to
  that same file under its own message. A pathspec narrows the blast radius; it does not close the
  hole, because `git commit -- <path>` commits that path's **working-tree** content, including
  another session's uncommitted edits to it.
* **2026-09-15.** A session used the short private-index form and skipped the resynchronisation
  step, leaving the real `.git/index` describing the pre-commit tree. `git status` then showed one
  file as `MM` and a deliberately deleted file as `AD` -- added in the index, deleted in the working
  tree. The next `git add -A` by **any** session on the box would have staged that stale view and
  reverted the commit, resurrecting the deleted file.
* **2026-09-16.** A session used `git add <paths>` followed by a bare `git commit`, which carried
  another session's staged removal of `daemons/hams_local_relay/src/offline_records.rs`, unmentioned
  in its message. The pushed tree declared `mod offline_records;` for a file that no longer existed,
  so the relay could not compile. Two further heads were pushed on top before anyone noticed, and
  both queued a full continuous-integration matrix on the development box that could only fail at
  compilation.

The last instance is the clearest statement of the cost: this is not merely a tidiness problem about
commit messages. It pushes broken trees to `main`, it spends scarce build capacity on runs that
cannot pass, and it makes a later reader attribute a break to the wrong change.

## Decision

**1. Never run a bare `git commit`, `git commit -a`, or `git add -A` in a shared working tree.**
Both take whatever the shared index holds, which is not knowable at the moment you run them. This
applies to every session and every kind of change, including ones that feel too small to matter --
a to-do note is exactly the commit that swept in 199 lines of someone else's work.

**2. `git commit -- <path>` is not sufficient and must not be treated as the safe form.** It commits
that path's working-tree content, so a peer's uncommitted edit to a file you also touched still
leaves under your message. Use it only after confirming with `git diff HEAD -- <path>` that every
hunk in it is yours.

**3. The standard mechanism is a private index**, so that what you commit is exactly what you chose
and the shared index is never consulted:

```bash
GIT_INDEX_FILE=idx git read-tree HEAD
GIT_INDEX_FILE=idx git add <your paths>
GIT_INDEX_FILE=idx git commit -m "..."
```

To append only your own text to a file other sessions also append to, build the blob from
`HEAD`'s content rather than from the working tree, so that a peer's in-progress edit to the same
file cannot ride along:

```bash
git show HEAD:<file> > tmp
cat my_entry >> tmp
GIT_INDEX_FILE=idx git read-tree HEAD
GIT_INDEX_FILE=idx git update-index --add \
  --cacheinfo 100644,"$(git hash-object -w tmp)",<file>
c=$(git commit-tree "$(GIT_INDEX_FILE=idx git write-tree)" -p HEAD -m "...")
git diff --stat HEAD "$c"          # confirm the commit is exactly what you meant
git update-ref refs/heads/main "$c" HEAD
```
Then append the same text to the working-tree file as well, so the file on disk matches the commit.

**4. Resynchronise the real index afterwards. This step is mandatory, and it is the one that gets
skipped.** A private index leaves `.git/index` describing the pre-commit tree, which any session's
next staging command can turn back into a revert. Immediately after committing, scoped so that other
sessions' staged work is untouched:

```bash
git reset -q HEAD -- <the exact paths you committed>
git status --porcelain <those paths>     # must be empty
```

**5. Prefer structures where no two sessions share a file.** A commit touching only files no other
session has any reason to edit carries none of this exposure, and a plain scoped `git add` of your
own new file is then genuinely safe. This is the real reason the night-shift to-do queue is one file
per item in a priority directory rather than one large shared document, and new tracking structures
should be designed the same way. Note also that `git add path1 path2 path3` validates every pathspec
before staging any of them, so one missing path -- a directory a `git rm` just removed, for instance
-- fails the whole command without staging anything.

**6. Never run `git reset` (unscoped), `git commit --amend`, or a rebase on the shared `main`.**
Another session's commit may already be sitting on it, and rewriting shared history orphans work
whose owner has no way to know it is gone. Scoped `git reset -q HEAD -- <paths>` for the
resynchronisation in decision 4 is the exception, because it names only your own paths and changes
no history.

**7. Re-check `git log --oneline origin/main..main` immediately before every push, not once at the
start of your work.** A peer can commit between your check and your push, and your push then
publishes their commit too. If the list shows commits that are not yours, do not push: the owner may
be holding them deliberately -- a relay change waiting on a local test run, for example, since any
push under `daemons/hams_local_relay/**` queues a full build matrix on the development box. Find the
owner (list the live sessions and message one; you cannot tell from authorship) and agree who
pushes.

## Consequences

Committing costs a few more commands than it otherwise would. That is the whole cost, and it is
small against the alternative, which is not hypothetical: broken trees on `main`, false commit
messages, wasted build capacity, and cross-session archaeology to work out what actually happened.

Sessions must be able to find this rule without already knowing it exists. The instances above kept
recurring partly because the material lived in one skill's operating notes, which a session running
a different skill never reads. This ADR is the single authoritative statement; agent skills and
per-repository conventions should link here rather than restating the recipes, since two copies of a
procedure are how the two copies drift apart.

## Related

* ADR MASTER 11 (Agile Development & Documentation Workflow) -- general development-workflow
  mandates, of which this is the concurrency-specific case.
* ADR MASTER 14 (LLM Context & Cognitive Load Management) -- rules governing how agents operate.
