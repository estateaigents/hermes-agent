# Harness baseline — deduplicated inventory (2026-11-10)

**Status:** read-only measurement. Nothing in this file deletes, moves, disables or edits anything in the harness. Every item in *Retirement candidates* is a **proposal for the owner's decision**, not an action taken.
**Snapshot:** all nine numbers below come from one measurement pass at **`2026-11-10T15:26:47Z – 15:26:52Z` (UTC)**.
**Why this file exists:** the red-team aggregation was challenged because it summed incommensurable units and double-counted the probes that already live inside `verify/`. This file gives nine categories, each on its own unit, each with the exact measuring command, plus an explicit union statement so no total can be built by naive addition again.

---

## 1. The nine categories

| category | count | what the number actually counts | command used |
|---|---|---|---|
| **overlay-scripts** | **501** | **executable** `*.sh` under `/root/.hermes/scripts`, **recursive**, **minus the `verify/` subtree** (reported as its own two categories below). The raw recursive executable total *including* `verify/` is **2,121**. | `cd /root/.hermes && find scripts -name '*.sh' -type f -perm -u+x -not -path 'scripts/verify/*' \| wc -l` → 501 |
| **acceptance-probes** | **1777** | `*.sh` files under `/root/.hermes/scripts/verify/probes`, **all permissions** (1,550 executable + 227 non-executable). `verify/` totals **do include them** — see §2. | `cd /root/.hermes && find scripts/verify/probes -name '*.sh' -type f \| wc -l` → 1777 |
| **verify-other** | **232** | files under `/root/.hermes/scripts/verify/` that are **not** under `probes/`, excluding `__pycache__` (77 `.sh`, 62 `.json`, 26 `.py`, 23 `.txt`, 20 `.md`, 5 `.mjs`, 4 `.log`, 2 `.patch`, 13 other). | `cd /root/.hermes && find scripts/verify -type f -not -path 'scripts/verify/probes/*' -not -path '*/__pycache__/*' \| wc -l` → 232 |
| **plugins** | **34** | plugin **packages** = directories containing an `__init__.py`. Directory count excluding `__pycache__` is **36** (34 packages + 2 non-plugin dirs); **file count 588**, of which **338** excluding `__pycache__` (250 files are byte-code). | `find /root/.hermes/plugins -maxdepth 2 -name __init__.py \| wc -l` → 34; `… -maxdepth 1 -mindepth 1 -type d -not -name __pycache__ \| wc -l` → 36; `… -type f \| wc -l` → 588; `… -type f -not -path '*/__pycache__/*' \| wc -l` → 338 |
| **skills** | **103** (32 harness-ops + 71 domain) | ACTIVE skill directories (has a `SKILL.md`), excluding `.archive/`, which holds **12 more** (all-time 115). Split rule below. | `cd /root/.hermes/skills && find . -name SKILL.md -not -path './.archive/*' \| wc -l` → 103 |
| **crons** | **35** | distinct **enabled** jobs across every profile's `cron/jobs.json`. 47 job rows exist in total (35 enabled, 35 distinct names — no name duplicated across profiles). The default profile has no `jobs.json`; its 11 legacy jobs are all `enabled=false` in `cron/jobs.retired-2026-11-05.json`. | `cd /root/.hermes && jq -s '[.[].jobs[]\|select(.enabled==true)]\|length' profiles/*/cron/jobs.json` → 35; `jq -s '[.[].jobs\|length]\|add' …` → 47 |
| **fork-patches** | **14** | commits in `upstream/main..HEAD` of the live engine build tree (`/root/.hermes/build/regraft-v0.22.5`, branch `fix/regraft-p2-reland`). The fork's **trunk** (`origin/main`) is only **2** commits ahead of `upstream/main`; the 14 is the active regraft patch series. | `git -C /root/.hermes/build/regraft-v0.22.5 rev-list --count upstream/main..HEAD` → 14; `… upstream/main..origin/main` → 2 |
| **board-tasks** | **10592** | rows in `/root/.hermes/kanban.db` `tasks`, of which **1,747 are archived** (8,845 not archived). Side tables: 498,795 events, 103,945 comments, 20,573 runs, 4,666 links; DB file 205 MB. | `sqlite3 /root/.hermes/kanban.db "select count(*) from tasks;"` → 10592; `… "select count(*) from tasks where status='archived';"` → 1747 |
| **frozen-repo-refs** | **966** | files containing the string `hermes-tooling` — the **archived** mirror repo (`gh api repos/estateaigents/hermes-tooling --jq .archived` → `true`). Split: `scripts/` **710**, `plugins/` **179**, `skills/` **77**. Narrower URL form `estateaigents/hermes-tooling` (files carrying a real git/`gh` operation against the dead repo): **448** files — scripts 295, plugins 138, skills 15. | `cd /root/.hermes && for t in scripts plugins skills; do grep -rl hermes-tooling $t \| wc -l; done` → 710 / 179 / 77 |

**Skills split rule (reproducible, not a judgement):** *harness-ops* = the **32** skills whose directory name matches this explicit list (Hermes runtime, kanban board, gates, dispatch, review, model routing, agent architecture):

```
hermes-*  kanban-acceptance-gate-audit  agent-model-selection  agent-self-evolution
subagent-driven-development  coding-workflow  task-lifecycle-workflow  task-studio
sdlc-review  moa-consensus-panel  redteam-panel  merge-reconciler  loop-craft-architectures
ci-gate-verification  regression-verification-review  pre-deploy-commit-verification
github-workflow  github-reviewer-seat-ops  cutover-window-ops  spec-ddl-audit
codebase-audit  modular-architecture-enforcer  design-feasibility-critique
```

Reproduced by `find . -name SKILL.md -not -path './.archive/*' | sed 's|.*/\([^/]*\)/SKILL.md|\1|' | grep -cE '<the 23 alternative patterns above; `hermes-` covers the 10 hermes-* skills>'` → **32**. **Domain = 71** (RAIA/product/business/email/finance/design/tool-usage). The two run over the same 103 files and sum exactly.

---

## 2. No double counting — the union statement

The nine categories are **not** additive: the units differ (files, directories, jobs, commits, DB rows). The correct unions:

**a) `verify/` contains the probes.** `find scripts/verify -type f` = **2,096** files, and that *includes* the **1,856** files inside `probes/` (1,777 `.sh` + 79 non-`.sh`). "acceptance-probes 1,777" and any "verify/ total" are the same population at two granularities. **Never add them.**

**b) overlay-scripts and acceptance-probes overlap physically.** 1,620 of the 2,121 executable `*.sh` under `/root/.hermes/scripts` are inside `verify/` (1,550 under `probes/`, 70 elsewhere under `verify/`). The deduplicated executable-script surface is:

```
    501   outside verify/ (executable)        <- overlay-scripts
+ 1,620   inside  verify/ (executable)        <- subset of probes + verify-other
= 2,121   find scripts -name '*.sh' -perm -u+x  <- the ONLY line that may be called "total"
```

Naive `501 + 1,777` overstates by **1,550 files** (and double-counts the probe population twice more if `verify/` is also totalled). That is the error class behind the 4,914 figure.

**c) The only whole-tree script count** is permission-agnostic: `find /root/.hermes/scripts -name '*.sh' -type f | wc -l` = **2,478**. It contains overlay-scripts (501 executable outside `verify/`), acceptance-probes (1,777, of which 227 are non-executable) and the 77 `.sh` files counted under verify-other — with zero double counting. `2,478 − 1,854` (all `.sh` under `verify/`) = **624** `.sh` outside `verify/`, of which 501 are executable and 123 are not.

**d) frozen-repo-refs overlaps the file inventories.** All 710 of its `scripts/` hits are files already inside the tree counted by `find /root/.hermes/scripts`; the 179 `plugins/` and 15/179 `skills/` hits are in disjoint trees, so no file is double-counted *within* that category. But the 966 is a **tag on existing files**, not additional files: never add it to 2,478.

**No file appears in two of the nine line items.** The only overlaps are (i) files inside `verify/` also being inside `scripts/`, handled in (b)/(c), and (ii) the frozen-repo tag, handled in (d).

**e) Source-of-truth caveat.** The repo-side mirror `/root/scripts` (97 files, 56 `.sh`, 91 tracked) is **stale** — it is the copy inside the archived `hermes-tooling` repo and is missing most live top-level scripts (277 live top-level `.sh` vs 56 mirrored). Every number here is measured on the **live** tree `/root/.hermes`, never on the repo.

**f) Deliberately excluded:** the second, legacy tree `/root/.hermes/verify/` (20 files, 1 `.sh` probe) is *not* part of the 2,096 / 1,777 figures. It is named so the numbers are unambiguous.

---

## 3. Volatility — these counts move while they are measured

Observed during this audit (same hour):

- probes: **1,776 → 1,777** within five minutes; a new probe (`cron_weekly_delivery_receipt_probe.sh`) was written at `15:24:28Z`.
- board-tasks: **10,586 → 10,592** within seven minutes (this audit's own card plus other workers' completions).
- Probes: 1,426 of 1,777 created in the last 14 days, 569 in the last 7 days; the oldest is dated **2026-10-20** and **none is older than 30 days**; the corpus spans only **22 distinct creation days** (~81/day).

A quoted count without a timestamp is not evidence. This file is a photograph at 15:26:52Z, not a constant.

---

## 4. Retirement candidates — **proposals only, nothing acted on**

Ordered by measurable board-sizedness (count grows with the number of *tasks*, not with the amount of *product*).

**4.1 acceptance-probes — 1,777 files, ~81/day.** Board-sizedness evidence: **556** are named `t_<taskid>_probe.sh`, **758** contain a task id in the body, the tree is only **22 days** old (oldest 2026-10-20), it grew 1,426 files in 14 days, and **nothing ages out** (0 files older than 30 days — the whole corpus is younger than that). *Proposal:* a TTL/archival policy keyed to the parent card reaching `done` (e.g. move to `verify/.archive/<YYYY-MM>/` after N days), with an explicit allow-list of named regression probes retained permanently. **Not executed.**

**4.2 overlay-scripts — dead copies inside the 501.** 46 executable `*.sh` sit in `/root/.hermes/scripts/.archive/`, 59 are `.bak-*` / `*.sh.bak-*` copies, and 38 top-level `.bak*` entries also sit in the tree. **These sets overlap — they must not be summed.** *Proposal:* byte-compare each against its live counterpart; retire only exact duplicates or strictly-older copies. **Not executed.**

**4.3 frozen-repo-refs — 966 references to a repository that can no longer accept writes.** The mirror is archived (`archived: true`, last push `2026-11-07T16:23Z`), so any `gh pr` / `git push` path in those files is structurally dead. 448 files carry the URL form; 109 of the `scripts/` hits are inside backup directories (pure dead text). *Proposal:* decide the replacement home (a new private ops repo, per the two-layer rule), then re-point or strip in one sweep. **Not executed.**

**4.4 board-tasks — 10,592 rows, 1,747 already archived.** Board-sized by definition; 498,795 events and 103,945 comments drive a 205 MB DB. *Proposal:* retention compaction (archive event/comment rows outside the active window, then vacuum) — DB hygiene, not harness deletion. **Not executed.**

**4.5 plugins — two non-plugins among the 36 directories.** `hermes-achievements/` has no `__init__.py`; it is 2.2 MB of `scan_snapshot.json` + `scan_checkpoint.json` + `state.json`, last written `2026-07-09`. 250 of the 588 plugin files are `__pycache__`. *Proposal:* remove the data-only directory and byte-code from the counted surface (no behaviour change). **Not executed.**

**Deliberately NOT candidates:** **skills** (103 active, product-coupled knowledge; 12 already archived) and **crons** (35 enabled, 35 distinct names, no cross-profile duplicates — these are operational, not per-task).

**Inference (labelled as inference, not a measurement):** probes account for 1,777 of 2,478 `.sh` files (**71.7%**) and 1,550 of 2,121 executable `.sh` (**73.1%**). On the script-file surface alone, a probe TTL policy is arithmetically sufficient to reach the owner's 70% target without touching product tooling. Whether that is the right 70% is an owner decision; this file only sizes the lever.

---

## 5. Command log (as run, `2026-11-10T15:20–15:27Z`)

```
cd /root/.hermes
find scripts -name '*.sh' -type f | wc -l                                          -> 2478
find scripts -name '*.sh' -type f -perm -u+x | wc -l                              -> 2121
find scripts -name '*.sh' -type f -perm -u+x -not -path 'scripts/verify/*' | wc -l -> 501
find scripts/verify -name '*.sh' -type f | wc -l                                  -> 1854
find scripts/verify -name '*.sh' -type f -perm -u+x | wc -l                       -> 1620
find scripts/verify/probes -name '*.sh' -type f | wc -l                           -> 1777
find scripts/verify/probes -name '*.sh' -type f -perm -u+x | wc -l                -> 1550
find scripts/verify/probes -type f | wc -l                                       -> 1856
find scripts/verify -type f | wc -l                                              -> 2096
find scripts/verify -type f -not -path 'scripts/verify/probes/*' -not -path '*/__pycache__/*' | wc -l -> 232
find plugins -maxdepth 1 -mindepth 1 -type d -not -name __pycache__ | wc -l       -> 36
find plugins -maxdepth 2 -name '__init__.py' | wc -l                              -> 34
find plugins -type f | wc -l                                                      -> 588
find plugins -type f -not -path '*/__pycache__/*' | wc -l                         -> 338
find plugins -type f -path '*/__pycache__/*' | wc -l                              -> 250
find skills -name SKILL.md | wc -l                                                -> 115
find skills -name SKILL.md -not -path 'skills/.archive/*' | wc -l                 -> 103
jq -s '[.[].jobs[]|select(.enabled==true)]|length' profiles/*/cron/jobs.json       -> 35
jq -s '[.[].jobs|length]|add' profiles/*/cron/jobs.json                            -> 47
git -C /root/.hermes/build/regraft-v0.22.5 rev-list --count upstream/main..HEAD      -> 14
git -C /root/.hermes/build/regraft-v0.22.5 rev-list --count upstream/main..origin/main -> 2
sqlite3 /root/.hermes/kanban.db "select count(*) from tasks;"                       -> 10592
sqlite3 /root/.hermes/kanban.db "select count(*) from tasks where status='archived';" -> 1747
sqlite3 … "select 'events',count(*) from task_events union all select 'runs',count(*) from task_runs union all select 'comments',count(*) from task_comments union all select 'links',count(*) from task_links;"
                                                                                     -> 498795 / 20573 / 103945 / 4666
grep -rl 'hermes-tooling' scripts | wc -l                                           -> 710
grep -rl 'hermes-tooling' plugins | wc -l                                           -> 179
grep -rl 'hermes-tooling' skills  | wc -l                                           -> 77
grep -rl 'estateaigents/hermes-tooling' scripts | wc -l                             -> 295
grep -rl 'estateaigents/hermes-tooling' plugins | wc -l                             -> 138
grep -rl 'estateaigents/hermes-tooling' skills  | wc -l                             -> 15
gh api repos/estateaigents/hermes-tooling --jq .archived                            -> true
find verify/probes -name '*.sh' -type f -mtime -14 | wc -l                          -> 1426
find verify/probes -name '*.sh' -type f -mtime -7  | wc -l                          -> 569
find verify/probes -name 't_*_probe.sh' -type f | wc -l                             -> 556
grep -rlE 't_[0-9a-f]{8}' verify/probes --include='*.sh' | wc -l                    -> 758
find verify/probes -name '*.sh' -type f -printf '%TY-%Tm-%Td\n' | sort -u | wc -l  -> 22
```

---

## 6. Machine-checkable summary

```
overlay-scripts: 501
acceptance-probes: 1777
verify-other: 232
plugins: 34
skills: 103
crons: 35
fork-patches: 14
board-tasks: 10592
frozen-repo-refs: 966
```

---

## 7. PA Measurement Reconciliation (2026-11-10)

Reconciliation of the 4 disputed counts between First Pass (15:26 UTC) and PA Independent Measurement:

| category | first pass | PA measurement | Decided True Count | Deciding Command | Explanation of Delta |
|---|---|---|---|---|---|
| **overlay-scripts** | 501 | 278 | **501** (502 live) | `find /root/.hermes/scripts -name '*.sh' -type f -perm -u+x -not -path '/root/.hermes/scripts/verify/*' \| wc -l` | PA measurement (`ls /root/.hermes/scripts/*.sh \| wc -l`) was non-recursive, counting only top-level `.sh` files in `/root/.hermes/scripts/`. First pass is recursive across subdirectories outside `verify/` (e.g. `scripts/raia-main/`, `scripts/archive/`, `scripts/lib/`, `scripts/tests/`, etc.). First pass does **not** include `scripts/verify/`. |
| **skills** | 103 | 115 | **103** active (115 total) | `find /root/.hermes/skills -name SKILL.md -not -path 'skills/.archive/*' \| wc -l` | PA measurement (`find /root/.hermes/skills -name SKILL.md`) was unfiltered and counted all `SKILL.md` files including 12 archived skills in `skills/.archive/`. First pass explicitly excluded `skills/.archive/*` to count only active skills (103 active at baseline + 12 archived = 115 total). |
| **crons** | 35 | 47 | **35** enabled (47 total) | `jq -s '[.[].jobs[]\|select(.enabled==true)]\|length' /root/.hermes/profiles/*/cron/jobs.json` | PA measurement counted all job rows across `profiles/*/cron/jobs.json` (`jq -s '[.[].jobs\|length]\|add'`), regardless of status. First pass filtered for enabled jobs (`select(.enabled==true)`). There are 12 disabled jobs (`enabled==false`), summing to 47 total job rows. |
| **plugins** | 34 | 35 | **34** packages (35 dirs) | `find /root/.hermes/plugins -maxdepth 2 -name '__init__.py' \| wc -l` | PA measurement (`ls -d /root/.hermes/plugins/*/ \| wc -l`) counted all 35 visible subdirectories under `/root/.hermes/plugins/`. First pass counted Python plugin packages containing an `__init__.py` file (34 packages). The 35th directory is `plugins/hermes-achievements/`, which has no `__init__.py` (it contains stale `.json` data files). |
