# A template database for test runs: measured, and not worth building first

Bruce, 2026-11-08: "set up a test that [a pre-built template database] actually provides the speed increase theorized."
Harness: `tools/benchmark_template_db.py` (documented in `tools/README.md`, tested by `tools/test_benchmark_template_db.py`).
Measured on the shared dev box (16 cores / 32 threads, PostgreSQL 18.6, Odoo 19.0-20261107, btrfs) at a load average of 26 to 74
(`/dev/shm/loadscreen/stats.txt` is stored with every run). Odoo ran as `odoo` through `test.py`'s own `main()`.

## Verdict

**Do not build it as a speed-up. A template database removes the install, and the install is about 3 percent of a real whole-system run.**
It saves about 51 s of a 136 s run of one small module, 131 s of a 309 s run of a medium selection, and about 136 s of a 4700 s whole-system run. What costs the time is
elsewhere: browser tests, each starting and stopping its own Chrome, and in the full pipeline a 3 to 5 second wait for a zombie
process in every one of them. Fixing that wait roughly halves the time of the browser tests that were measured
(5.1 s to 2.3 s per test) and is a three-line change. Bruce's memory that an earlier cloning/sharding idea "didn't see a speed
increase" is right, and for the reason below.

| Where the time goes | small (`parking`, 37 tests) | whole system (66 modules, 3703 tests) |
|---|---|---|
| Install (start to "Modules loaded.") | 52 s (48-61) | 137 s (133-149, measured here); 105 s in the earlier full run |
| Web asset bundles, every fresh database, before the first test | 28 s | 27-30 s |
| Tests | 55 s | 4505 s (75 min) |
| **Install as a share of the run** | **38 %** | **3.0 %** (137 of 4505 + 137 + 28) |
| **Install plus assets** | **59 %** | **3.6 %** |

The whole-system column is from `~/workspace/tmp/wholesys/rerun1.log` (install 11:17:50 to 11:19:35 UTC, first test 11:20:03, 3703
post-tests in 4505.6 s). The coordinator's combined run of five modules (`dob-year-test4.log`: 135 modules loaded in 106 s, 1858
tests in about 5330 s) gives the same 2 %.

## What was measured

Every arm goes through `test.py`'s real `main()` (same slot, same command line, same Odoo launcher). Only two things differ from today's
run in the clone arms: `rebuild_db` is replaced by a clone, and `-i <modules>` is removed from the Odoo command line. That is also the
whole of a minimal production change. Repetitions interleave the arms so that load drift hits all of them. Timings are wall seconds
from the start of `rebuild_db` with the median and [min-max] of n runs; CPU is the user plus system time of the runner tree
(`wait4`) and, for PostgreSQL, a sampled lower bound.

### Small set: `parking` (85 modules installed, 37 tests, 3 repetitions unless noted)

| arm | DB ready | modules loaded | first test | total | runner CPU | PG CPU (sampled) |
|---|---|---|---|---|---|---|
| fresh (today) | 0.2 | 52.3 [47.8-61.2] | 80.4 [73.6-89.3] | 136.5 [123.8-148.1] | 91.5 | 20.4 |
| clone, SSD | 0.6 | 2.5 [2.3-2.7] | 30.5 [29.4-34.6] | 85.4 [80.5-103.8] | 56.6 | 1.4 |
| clone, template and clone in a tmpfs tablespace (n=2) | 0.4 | 2.2 | 29.6 [26.4-32.8] | 81.4 [74.8-88.0] | 54.4 | 1.4 |
| fresh, database moved to the tmpfs tablespace (n=2) | 15.8 | 68.9 | 98.1 | 155.1 | 91.7 | 20.6 |
| clone then `odoo -u` (n=2) | 0.5 | 5.6 | 37.2 | 94.1 | 63.0 | 3.0 |
| clone with `-i` left on the command line (n=2) | 0.6 | 3.3 | never | 4.3, exit 0 | 3.4 | 0.7 |
| template build (once per key) | 8.4 | 61.6 | - | 63.0 [58.2-102.1] | 37.8 | 20.5 |

1. **A clone saves 51 s of 136 s (37 %) for the smallest module, and 35 CPU-seconds plus 19 PostgreSQL CPU-seconds.** The saving is the
   install, and it is a constant: the same module set costs the same to install whatever the tests are.
2. **The remaining 28 to 30 s before the first test is the web asset bundles**, which Odoo regenerates in every fresh database when the
   first HTTP test is about to run (`odoo.service.server`: `_pregenerate_assets_bundles`). It is as large as the saving of the clone
   and the clone does not touch it (fresh 28 s, clone 28 s). A template could carry the bundles (Odoo re-checks bundle checksums
   against the sources on lookup). **Not measured**: the attempt to pregenerate them with `odoo shell` once at build time hung (the
   shell sat idle in a transaction with 32 idle threads), and the option was removed from the harness rather than shipped unproven.
3. **A RAM (tmpfs) tablespace does not help.** Clone in RAM 81.4 s against clone on SSD 85.4 s: inside the noise. Moving a fresh install
   into RAM was slower (155 s against 136 s) because of the `ALTER DATABASE SET TABLESPACE` copy and a checkpoint; install is CPU
   bound (91 CPU-seconds in 136 s), not I/O bound.
4. **The `CREATE DATABASE` itself is nothing**: 0.53 s [0.45-0.65] on SSD with the default `WAL_LOG` strategy, 0.14 s in RAM. The
   `FILE_COPY` strategy is slower on this box (22.7 s, also with `file_copy_method = clone` / reflink on btrfs: 24 s) because it forces a
   checkpoint on a busy server. Use the default. The filestore copy is 0.05 s for 1 MB, the secret re-key 0.04 s, copying the template
   into RAM 0.27 s, the key hash 0.69 s (45 modules).
5. **Outcome identity.** Every clone repetition ended with the same `odoo.tests.result` (0 failed, 0 errors of 37) and the same set of
   started tests as the fresh run (`report` prints one line per run). Parking's tests are post_install; a plain load of an installed
   database runs both the at_install and the post_install suites.
6. **The trap.** `test.py` passes `-i`. On a clone, `-i` of installed modules changes nothing, so Odoo runs **no tests at all and exits 0**
   in 4 s ("clone with `-i`" above; `registry.updated_modules` is empty, which is what Odoo runs its post-install tests for). A clone
   must run without `-i` (plain load) or with `-u`. The run looks like a pass (exit 0, `0 failed, 0 error(s) of 0 tests`); only a
   check that the test count is not zero catches it, and an implementation must make that check.
7. **Variance.** Load average 26 to 74 on 32 threads; the spread of identical runs is 13 to 20 percent. The 51 s saving is larger than the
   spread of either arm.

### Medium and large sets

### Medium set: `ham_onboarding` (124 modules installed, 33 tests of two classes, 3 repetitions unless noted)

| arm | modules loaded | first test | total | runner CPU | PG CPU (sampled) |
|---|---|---|---|---|---|
| fresh (today) | 133.8 [113.8-151.1] | 165.8 [143.8-196.0] | 309.0 [266.3-375.3] | 217.7 | 49.2 |
| clone, SSD | 4.3 [3.8-4.9] | 37.9 [32.5-40.5] | 177.5 [154.9-182.0] | 124.3 | 11.0 |
| clone, tmpfs tablespace | 4.4 [4.0-7.2] | 39.4 [32.7-39.7] | 192.0 [170.8-215.3] | 134.5 | 13.0 |
| clone then `odoo -u` (n=2) | 43.0 [40.9-45.1] | 75.9 [71.5-80.4] | 221.8 [220.4-223.1] | 159.3 | 16.8 |
| clone with `-i` kept (n=2) | 4.5 | never | 6.0, exit 0, 0 tests | 6.3 | 0.8 |
| template build | 108.4 | - | 110.7 | 84.0 | 29.1 |

A clone saves 131 s of 309 s (42 %) and 93 runner CPU-seconds plus 38 PostgreSQL CPU-seconds, for a 33-test selection. The tests
themselves took 143 s (fresh) and 140 s (clone). Template: database 95.7 MB, filestore 1.4 MB, build 111 s, `CREATE DATABASE` 0.53 s
[0.38-0.54] on SSD and 0.30 s in RAM, `FILE_COPY` 43 s, filestore copy 0.04 s, re-key 0.03 s, key hash 0.80 s (81 modules). All clone
runs but `-i` had the same outcome as the fresh run (0 failed, 0 errors of 33, the same tests started). A tmpfs tablespace is again no
faster (192 s against 177 s, inside the spread). `-u` after a clone costs 40 s of module update and 44 s of the saving.

The full `ham_onboarding` run is 640 tests, about 26 minutes at the load of the coordinator's measurement, so for it the same 131 s
is 8 percent, not 42.

### Large set: the whole-system install list (67 modules, 3 repetitions): start-up only

The tests of the large set cannot be repeated three times each (75 minutes per run); start-up was measured with `--test-tags /parking`
(the same 37 tests as the small set) after installing the whole-system list:

| arm | modules loaded | first test | total (37 `parking` tests) | runner CPU | PG CPU (sampled) |
|---|---|---|---|---|---|
| fresh (today) | 136.5 [133.5-148.9] | 166.8 [162.8-181.8] | 226.2 [218.8-246.3] | 168.4 | 39.2 |
| clone, SSD | 4.8 [4.7-5.0] | 34.8 [34.8-39.2] | 90.5 [89.6-95.8] | 66.9 | 2.0 |
| clone, tmpfs tablespace | 4.3 [4.1-4.4] | 33.9 [33.4-35.0] | 94.8 [88.7-105.7] | 66.8 | 2.2 |
| template build | 136.9 | - | 139.1 | 106.9 | 36.0 |

Load average 18 to 37 during these runs (the quietest of the three sets). A clone saves **136 s of 226 s here, and the same 136 s of a
whole-system run**: the install of the 67-module list is 137 s, and it is 137 s whatever the tests are. Template: database 109.1 MB,
filestore 1.4 MB, build 139 s. All six clone runs had the same outcome as the fresh runs (0 failed, 0 errors of 37, the same tests
started). 137 s is **3.0 percent of the 4505 s of tests of the full whole-system run** (and 2.5 percent of 5400 s of the five-module run), so for
the run that matters most the template removes 2 to 3 percent.


## Cost of a template

* **Size.** `parking` closure (85 modules): database 63 MB, filestore 1 MB; the whole system is about 100 MB of database plus 80 MB of
  filestore (other sessions' 135-module databases on this box: 107 MB, filestore 82 MB). In a tmpfs tablespace the same again in RAM, per
  template and per concurrent clone.
* **Build.** 63 s [58-102] for the small set; the whole-system list builds in 139 s. Built once per key, by a run that holds a slot.
* **Hashing the key** reads every file of the closure: 0.69 s for 45 modules; the whole system is a few seconds.

## How a template stays valid

The name is the key: `hams_tpl_<first 16 hex digits of sha256(key)>`. The key (`TemplateKey`) holds the install list in order, a hash of
every file of every module in the dependency closure (data XML/CSV, Python, migrations, static files, tests), the Odoo version, the
PostgreSQL major version, the `vector` extension version, the install options (demo data) and a double-hashed fingerprint of
`HAMS_CRYPTO_KEY`. A changed key has a different name, so an old template cannot be found by accident; the unit tests change a data
file, add a migration, rename a file with equal bytes, and change every other field, and each gives a different name. The database comment
repeats the key and `verify_template` compares it (catches a hand-made database, a truncated name collision, and a build that did not
finish, because the comment is written last). Unchanged: a template is never updated, only replaced, and old ones are dropped.

State of a clone, checked:

| State | Clone behaviour | Handling |
|---|---|---|
| `ir.module.module` states, `ir_model_data`, demo data | Identical to the template | none |
| Sequences | Copied with the tables | none |
| `ir_cron` next-call times | Those of the build time | harmless: the runner starts Odoo with `--max-cron-threads=0` |
| `database.uuid`, `database.secret`, `form_shield.signing_key`, `database.create_date` | Shared by every clone of the template | **must be re-keyed on each clone**: `rekey_sql()` (0.04 s) |
| `hams_crypto` data | Encrypted under `HAMS_CRYPTO_KEY`; same key in the clone | the key fingerprint is in the template key |
| Redis keys named by database (`<db>:distributed_cache:...`) | The clone has another name, so none exist | the runner already flushes the slot's Redis database |
| Filestore (`/var/lib/odoo/.local/share/Odoo/filestore/<db>`) | **Not in the database**; must be copied | `cp -a --reflink=auto` (0.05 s). In the full pipeline `/var` is a per-run overlay, so it is copied into the overlay each run |
| Web asset bundles | Absent unless built into the template | regenerated by every run, 28 to 30 s (see above) |
| Test-only models | A table created only when the module is installed with tests | none exist in these repositories (`tests_define_models` is empty and checked by the harness); a template built without `--test-enable` is correct only while that holds |
| `-i` on the command line | **runs no tests** | remove it (plain load) |

**What cannot run on a clone.** Nothing is lost with a plain load: it runs the at_install suite as each module loads and the post_install
suite afterwards, and the measured outcome was identical. It is not the same moment (at_install tests normally run in the middle of the
install, before later modules exist), so a test that depends on a later module being absent could differ. `odoo -u` restores the original
timing at a cost of 6 s more. Whether any at_install test of these repositories depends on it was not investigated beyond the sets measured.

## Where the time actually goes: browser tests

Per-test cost, from the start times of consecutive `Starting Class.test ...` lines of two real runs:

| Run | browser tests (each starts its own Chrome) | other tests |
|---|---|---|
| Whole system, flat, `HAMS_ISOLATED_NS=1` (`rerun1.log`) | 987 tests, 3056 s, mean 3.1 s (median 2.75 s) | 2714 tests, 822 s, mean 0.30 s |
| Five modules, full pipeline, slot 4 (`dob-year-test4.log`) | 1088 tests, 6922 s, mean 6.4 s (median 8.2 s) | 1227 tests, 494 s, mean 0.40 s |

**79 percent (flat) and 93 percent (full pipeline) of the test time is browser tests.** The 0.3 to 0.4 s of everything else is not
where to look. The difference between the two runs, about 3 s per browser test, is a defect of the full pipeline.

### The 3 to 5 second zombie wait

`ham_relay_bridge`'s `TestRelayApi`: 8.85 s per test. One test, from its log: the body (the HTTP request) finishes 1.25 s after the
start; Chrome starts in 0.7 s; then `Terminating chrome headless`, followed 5.0 s later by `Killing chrome descendants-or-self ...: 1
remaining - unshare (zombie)`, then another second. Odoo's `ChromeBrowser` (`odoo/tests/common.py`, `psutil.wait_procs(procs, 5)`) waits up
to 5 s for the browser tree to disappear. `zero_sudo/tests/common.py` launches Chrome as `setsid -w unshare ... chrome`. In the full
pipeline the zombie `unshare` is reparented to the namespace's init, which is `test.py` itself, and `reap_orphans_forever` reaps a zombie
only after it has been one for `min_age=3.0` s, scanning every 1.0 s. So **every browser test pays about 3 s, and 5 s plus 1 s whenever
the scan is late**; 429 of 920 browser tests in the logged run hit the full 5 s (2156 s, 36 minutes, of the run). The flat run
(`HAMS_ISOLATED_NS=1`, host init reaps at once) has none: 0 warnings in 1035 browser tests.

Verified, not just read: the same 15 tests (`ham_relay_bridge` `TestRelayRevocationApi`, full pipeline, same box, same load level):

| `reap_orphans_forever` | first test start to result | per test |
|---|---|---|
| `interval=1.0, min_age=3.0` (today) | 77 s | 5.1 s |
| `interval=0.2, min_age=0.3` | 34 s | 2.3 s |

The patch is two default arguments (saved as `results_shake/reaper_min_age.diff`; not applied: `test.py` is being edited by other
agents). It is the largest single saving found: for the 1000-browser-test whole-system run about 2.8 s times 1000, 47 minutes of
about 77. Whether `min_age` can go to zero depends on why it exists ("a child whose parent code is about to wait for it is left to that
code"); 0.3 s kept the 15 tests green, and a full gate run should confirm it.

What remains of the 2.3 s is Chrome itself: one headless Chrome is started and stopped per `HttpCase` test (0.7 s start, 0.5 to 1 s
stop). Sharing one browser per test class would remove most of it and is a change to the test base class, not measured here.

### Quiet slot against the loaded one

No quiet slot existed: the load average was 26 to 74 for the whole measurement. The evidence that exists: whole-system flat run
(early morning) 3.1 s per browser test against 6.4 s in the full pipeline at load 25-75; `ham_onboarding` the same 640 tests 1.56 s per
test flat against 2.48 s in the pipeline, which is explained by the reaper wait, not by load. The harness records `load1` and CPU
seconds with every run; the small-set runner CPU (91.5 s) against wall (136.5 s) shows the processes were not CPU-starved while running.
A genuine quiet-slot comparison needs the box idle and is not claimed.

## Sharding one module list across slots

@@SHARDING@@

## What a minimal production implementation would be, if it is built anyway

Only worth doing after the reaper change, and its value is larger if the asset bundles can be put in the template (unproven, above). In `test.py`:

1. `rebuild_db(db_name)`: when a template exists for the key, `DROP DATABASE ...; CREATE DATABASE "db" TEMPLATE "hams_tpl_<hash>"`
   (default strategy), keep the slot marker comment, copy the filestore, run `rekey_sql`. Otherwise build the template first with
   `-i` and no tests under the slot lock, comment it with the key, then clone. About 40 lines (`clone_for_run` in the harness is the
   prototype).
2. The Odoo command: no `-i` when cloning.
3. `TemplateKey` and `verify_template` as in the harness; drop templates whose key no longer matches any module list (one `DROP
   DATABASE` per stale name).
4. A host without the module-level `tests_define_models` guarantee must not use it.

## Recommendation

1. Apply the reaper change (two default arguments in `reap_orphans_forever`) and run a full gate to confirm: about half of the browser-test
   time of every full-pipeline run, so the largest gain by far.
2. Do not build the template database for speed. If it is built later, it is a 50 s saving per run for small module sets (large relative
   to a 90 s run, 2 percent of a whole-system run), and it adds a stale-state class of failure that the key above only mostly prevents.
3. Measure one-Chrome-per-class before anything else is considered for the per-test cost.
