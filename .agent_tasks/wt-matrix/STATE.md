# Task: Worktree merge matrix

**Status:** in progress
**Started:** 2026-10-14   **Last touched:** 2026-10-14

## Objective

Prove the wave/worktree/merge machinery across the full combination space
(3-member waves, multi-wave ordering, mixed projects, conflict variants,
resume-after-conflict), and add `merge_resolver`: on a merge conflict the
conductor spawns a resolver `devin -p` session in the main checkout to finish
the merge instead of only parking the worktree for a human.

## Not in scope

- Live real-devin runs of the lab manifest (apartment-3d run holds the box;
  `manifests/lab-merge-stress.toml` is staged for the next free window).
- Auto `-X union`/theirs merge strategies - an agent resolution preserves
  intent better than a blind text union; revisit if resolver cost matters.
- Cross-run merging (one run's branch into another run's wave).

## Decisions already made

- Merges stay parent-owned: workers never merge; the conductor merges every
  branch itself, sequentially, in manifest order, after the whole wave ends.
  The resolver only handles the conflict *exception*, delegated to a session.
- Resolver runs in the MAIN checkout mid-merge (`git status` shows the
  conflict), not in the worktree - the conflict lives there.
- `merge_resolver` default off (`[run]` or per-step opt-in): a bad silent
  resolution is worse than a kept worktree; overnight manifests opt in.
- Branch `dev/merge-tests` in worktree `C:\Projects\agent-conductor-wt` -
  apartment-3d's live run owns the main checkout (trap 10 hygiene).

## Phases

### Phase 1 - machinery

- [x] mock_devin keywords: `mockfail`, `prepend`, `sleep_after`, `skipwork`,
      `resolve-merge` (+ `MOCK_RESOLVE_FAIL` env for resolver failure)
- [x] conductor.py `merge_resolver` ([run] + per-step cascade), resolver
      session via run_step(post_commit=False) on a synthetic
      `resolve-<step>` step, `abort_own_merge` on resume/stale-merge
      (conductor/* MERGE_HEAD only), resume finalizes unmerged branches for
      done AND failed/blocked/partial statuses
- [x] per-step `tokens` in state.json from ATIF `final_metrics` + dashboard
      column + per-run totals (answers the "are worker tokens visible" ask:
      yes, via the export - 258k prompt/235k cached on the smoke readme step)

### Phase 2 - the matrix (tests/test_worktrees.py)

- [x] 3-member same-project wave: disjoint files clean; all-append conflict
      (first merges, rest kept); same file different regions clean
- [x] mixed waves: wt+wt on A + plain on B; wt on A + plain on B + plain on C;
      non-adjacent same wave numbers do NOT group
- [x] multi-wave: wave-2 worktrees branch off post-wave-1-merge HEAD (sees
      wave-1 files); wave member fails while sibling merges
- [x] conflict+resume: conflicted branch is parked not re-run (same merge
      base); resume+resolver completes the merge; timed_out keeps worktree;
      done-with-no-commits cleans up; capped wave still merges all
- [x] resolver: conflict resolves -> merged=true, both contents present;
      resolver failure -> abort + worktree kept; resolver off -> old behavior

### Phase 3 - live lab + docs

- [x] `examples/make_lab_projects.py` (lab-a easy / lab-b medium / lab-c
      hard) + projects.toml slugs + `manifests/lab-merge-stress.toml`
      (4 waves + audits, swe-2-medium - valid variant, free tier)
- [x] AGENTS.md (parallel/tracking paragraphs + trap 11: no auto-commit
      around a merge in progress), ROADMAP ticks + log entry,
      manifests/README parallel section, examples/night.example.toml
- [ ] efficiency_stats row at session end; commit on dev/merge-tests

## Live proof - lab-merge-stress run (20261014-034119)

`python conductor.py run manifests\lab-merge-stress.toml` from the worktree:
**21/21 done, 0 failed** (~12 min, alongside the live apartment run).
- Waves 1-3: 9 worktree members, all `merged=True`. One clean merge per
  wave; the other 8 all hit REAL conflicts (every worker edited the
  shared `.agent_tasks/QUEUE.md`) and every one was finished by a
  `resolve-<step>` session - semantically correct unions (all queue rows
  marked done, no markers, trees clean).
- Wave 4 + audits: all done; audit steps committed real work post-merge.
- Token totals across 21 sessions: 3.03M prompt / 2.50M cached (82%) /
  25.9k completion / 340 LLM steps - ~144k prompt per trivial task,
  all visible on dashboard :8774.
- Sessions spawned: scythe-longan, rare-soap, habitual-spirit,
  nonchalant-mitten, melted-cashew, + 10 more (names in state.json).

## Where it stopped

Committed `5ba30c4` on `dev/merge-tests`. Task effectively complete:
machinery + matrix + lab + live proof + docs all landed, 75/75 green.
Deferred: point the main checkout's dashboard at the wt `data/runs` (or
merge the branch) so :8773 shows token columns - needs the main checkout
free of the other agent.

## Deferred or blocked

- Live `lab-merge-stress` run: blocked until the apartment-3d views-floorplan
  run ends (same box; real devin sessions).

## Verification

`python run_tests.py` (all-mock, ~7s). New suite: tests/test_worktrees.py.
