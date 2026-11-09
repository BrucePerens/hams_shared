# Lint preflight: the hams_shared checkout is brought up to date, or the run fails at once

Every worktree of hams_open and hams_com links its `hams_shared` to the MAIN hams_shared checkout, so a main checkout that has
fallen behind `origin/main` makes every lint run use stale tools. The run used to stop with "N commit(s) behind origin/main" after a
wait in the queue, and each agent had to fix it by hand and restart. `run_linters.py` now checks first, before it builds a layout, takes a
batch place or queues for a lint slot, and fast-forwards the checkout when that cannot lose anything.

| Situation | What happens |
| --- | --- |
| main checkout behind, no modified tracked files, HEAD an ancestor of `origin/main` | `git merge --ff-only origin/main`, a line `fast-forwarded the hams_shared checkout <old> -> <new>`, and the run restarts itself once with the new files |
| modified tracked files, or HEAD not an ancestor of `origin/main` | exit 2 within seconds with the reason and the manual fix; nothing is changed |
| a linked worktree of hams_shared behind | a warning only |
| `HAMS_ALLOW_STALE_SHARED=1` | the check is skipped |

Only `merge --ff-only` is ever run: never a reset, checkout or discard. Untracked files do not block it.

## `run_linters:fast_forward_shared`
`fast_forward_shared(shared_root) -> (ok, text)`. `shared_root` (str): the main hams_shared checkout, borrowed. Returns `(True, "fast-forwarded the hams_shared checkout <old> -> <new>")`
or `(False, reason)` for: tracked files modified (`git status --porcelain --untracked-files=no`), HEAD not an ancestor of `origin/main`, or git refusing
(a lost race with another run doing the same counts as success when the checkout is current afterwards). Special concerns: moves HEAD of that checkout, which every worktree
linking to it sees at once; a detached submodule checkout stays detached; no network (the caller fetched); seconds. See also `run_linters:ensure_shared_current`; tests in `tools/test_run_linters_staleness.py`.

## `run_linters:ensure_shared_current`
`ensure_shared_current(shared_root) -> (status, text)`. Fetches (20 s limit), then returns `current` (nothing to do), `fast_forwarded` (the caller restarts the run with `os.execv`, once, guarded
by `HAMS_SHARED_FF_RESTARTED`), `worktree_behind` (`text` is the warning) or `refused` (`text` names the checkout, how far behind, why it was not fast-forwarded and the manual fix; the caller prints it and exits 2).
Takes no lock and queues nothing, so a refusal costs seconds.
