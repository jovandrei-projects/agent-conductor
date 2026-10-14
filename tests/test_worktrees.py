"""Worktree/wave/merge matrix - the combination space conductor must survive.

Everything runs against tests/mock_devin.py - no auth, no credits. Fixture
repos are real git repos in temp dirs and every merge is a real `git merge`,
so conflict behavior is the real thing, not a simulation.

Mock prompt keywords used here (see tests/mock_devin.py docstring):
mockfail, prepend, sleep=N, sleep_after=N, skipwork, resolve-merge.

The space, roughly:

    members:      1 | 2 | 3+            wave shape: singleton | wave | waves
    projects:     same | mixed (A,B,C)  files:      disjoint | same-region
                                                | same-file-diff-region
    outcomes:     done | fail | timeout | merge:    clean | conflict |
                                                conflict+resolver
    lifecycle:    fresh | resume-after-conflict | resume-after-done-unmerged
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))   # sibling helper module test_conductor

import conductor  # noqa: E402
from test_conductor import MOCK, git, make_project, write_manifest, \
    run_manifest  # noqa: E402


def state_of(runs_dir: Path) -> dict:
    rd = next(runs_dir.iterdir())
    return json.loads((rd / "state.json").read_text()), rd


def wt_steps(proj: Path, names_files: list[tuple[str, str]],
             wave: int = 1, extra: str = "") -> str:
    """Manifest text: worktree steps on proj, each writing its prompt's file."""
    out = []
    for name, prompt in names_files:
        out.append(f'''
[[step]]
name = "{name}"
project = "{proj.as_posix()}"
prompt = "{prompt}"
wave = {wave}
worktree = true
{extra}''')
    return "".join(out)


class WaveShapeTests(unittest.TestCase):
    def test_nonadjacent_wave_numbers_do_not_group(self):
        """wave=1, plain step, wave=1 later: the two wave-1 steps must NOT
        run in parallel (groups are ADJACENT only) - the manifest order is
        sequential semantics."""
        resolved = [(Path("p"), "s", conductor.Step(
            name=n, project="x", prompt="p", wave=w))
            for n, w in (("a", 1), ("mid", None), ("b", 1))]
        groups = conductor.group_waves(resolved)
        self.assertEqual([[s.name for *_d, s in g] for g in groups],
                         [["a"], ["mid"], ["b"]])

    def test_single_member_wave_is_singleton(self):
        with tempfile.TemporaryDirectory() as td:
            base, proj = Path(td), None
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [("solo", "write A.txt")]))
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["solo"]["merged"])
            self.assertTrue((proj / "A.txt").is_file())


class SameProjectWaveTests(unittest.TestCase):
    def test_three_member_wave_all_merge_in_manifest_order(self):
        """Three worktree steps on the same project, disjoint files: all
        merge; merge order is manifest order regardless of finish order."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [
                ("w1", "write A.txt"),
                ("w2", "sleep=0.5 write B.txt"),   # finishes last, merges 2nd
                ("w3", "write C.txt")]))
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            for n in ("w1", "w2", "w3"):
                self.assertEqual(st["steps"][n]["status"], "done")
                self.assertTrue(st["steps"][n]["merged"])
            for f in ("A.txt", "B.txt", "C.txt"):
                self.assertTrue((proj / f).read_text().strip())
            # merge order w1,w2,w3 -> reflog (newest first) shows w3..w2..w1
            reflog = git(proj, "reflog", "--format=%gs").stdout
            self.assertLess(
                reflog.index(f"conductor/{rd.name}/w3"),
                reflog.index(f"conductor/{rd.name}/w1"))

    def test_same_file_different_regions_merge_clean(self):
        """Same file, disjoint regions (prepend vs append): NOT a conflict.
        'Both touched FILE' is not the conflict test - same *region* is."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            (proj / "SHARED.txt").write_text("seed\n" * 10, encoding="utf-8")
            git(proj, "add", "-A")
            git(proj, "commit", "-m", "seed shared")
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [
                ("top", "prepend to SHARED.txt"),
                ("bottom", "append to SHARED.txt")]))
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["top"]["merged"])
            self.assertTrue(st["steps"]["bottom"]["merged"])
            lines = (proj / "SHARED.txt").read_text().splitlines()
            self.assertEqual(len(lines), 12)
            self.assertTrue(lines[0].startswith("mock-step-"))   # prepend
            self.assertTrue(lines[-1].startswith("mock-step-"))  # append
            self.assertNotEqual(lines[0], lines[-1])            # 2 sessions

    def test_three_way_append_conflict(self):
        """Three appends to the same tail region: first lands, the other two
        conflict and keep their worktrees. Nothing is lost."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [
                ("w1", "append to WORK.txt"),
                ("w2", "append to WORK.txt"),
                ("w3", "append to WORK.txt")]))
            self.assertEqual(run_manifest(m, runs), 1)
            st, _ = state_of(runs)
            self.assertEqual(st["steps"]["w1"]["status"], "done")
            self.assertTrue(st["steps"]["w1"]["merged"])
            for n in ("w2", "w3"):
                self.assertEqual(st["steps"][n]["status"], "failed")
                self.assertFalse(st["steps"][n]["merged"])
                self.assertTrue(Path(st["steps"][n]["work_dir"]).is_dir())
            self.assertEqual(len((proj / "WORK.txt")
                                 .read_text().splitlines()), 1)

    def test_wave_member_failure_sibling_still_merges(self):
        """mockfail on ONE member: that member fails (its empty branch is
        debris-cleaned), the sibling's branch still merges, on_failure=
        continue keeps the run going."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [
                ("bad", "mockfail write A.txt"),
                ("good", "write B.txt")]),
                extra='on_failure = "continue"')
            self.assertEqual(run_manifest(m, runs), 1)
            st, _ = state_of(runs)
            self.assertEqual(st["steps"]["bad"]["status"], "failed")
            self.assertEqual(st["steps"]["good"]["status"], "done")
            self.assertTrue(st["steps"]["good"]["merged"])
            self.assertTrue((proj / "B.txt").is_file())
            self.assertFalse(Path(st["steps"]["bad"]["work_dir"]).is_dir())

    def test_timed_out_step_keeps_worktree_with_work(self):
        """sleep_after: the file lands but STATUS never prints -> timed_out.
        The post-kill checkpoint commits the dirty worktree, so the branch
        carries work product -> kept for a human, not debris-cleaned."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "stuck"
project = "{proj.as_posix()}"
prompt = "sleep_after=30 write A.txt"
worktree = true
timeout_min = 0.02
''')
            self.assertEqual(run_manifest(m, runs), 1)
            st, _ = state_of(runs)
            rec = st["steps"]["stuck"]
            self.assertEqual(rec["status"], "timed_out")
            self.assertTrue(Path(rec["work_dir"]).is_dir())
            self.assertIn("unmerged work", rec.get("note", ""))
            self.assertTrue(git(proj, "branch", "--list",
                                "conductor/*").stdout.strip())

    def test_done_step_with_no_commits_cleans_up(self):
        """skipwork: worker exits done with zero work. The branch is HEAD
        itself -> 'Already up to date' merge, worktree+branch removed."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [("noop", "skipwork")]))
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["noop"]["merged"])
            self.assertEqual(git(proj, "branch", "--list",
                                 "conductor/*").stdout.strip(), "")
            self.assertEqual(git(proj, "worktree", "list",
                                 "--porcelain").stdout.count("worktree"), 1)


class MixedProjectWaveTests(unittest.TestCase):
    def test_two_worktree_projects_in_one_wave(self):
        """projA + projB each get a worktree step in one wave; merges land
        in their own repos independently."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pa, pb = make_project(base, "projA"), make_project(base, "projB")
            runs = base / "runs"
            m = write_manifest(base,
                               wt_steps(pa, [("a", "write A.txt")])
                               + wt_steps(pb, [("b", "write B.txt")]))
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["a"]["merged"])
            self.assertTrue(st["steps"]["b"]["merged"])
            self.assertTrue((pa / "A.txt").is_file())
            self.assertTrue((pb / "B.txt").is_file())

    def test_mixed_wave_worktree_and_plain_members(self):
        """One wave: 2 wt steps on A + plain on B + plain on C. Plain
        members run in their project dirs; wt members merge into A."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pa = make_project(base, "projA")
            pb = make_project(base, "projB")
            pc = make_project(base, "projC")
            runs = base / "runs"
            m = write_manifest(base, wt_steps(pa, [
                ("a1", "write A1.txt"), ("a2", "write A2.txt")]) + f'''
[[step]]
name = "b"
project = "{pb.as_posix()}"
prompt = "write B.txt"
wave = 1

[[step]]
name = "c"
project = "{pc.as_posix()}"
prompt = "write C.txt"
wave = 1
''')
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            for n in ("a1", "a2"):
                self.assertTrue(st["steps"][n]["merged"])
            self.assertTrue((pa / "A1.txt").is_file())
            self.assertTrue((pb / "B.txt").is_file())
            self.assertTrue((pc / "C.txt").is_file())


class MultiWaveTests(unittest.TestCase):
    def test_second_wave_branches_off_first_wave_merge(self):
        """Wave-2 worktrees must see wave-1's merged work: they branch off
        the post-merge HEAD. w2's append lands on top of w1's line."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base,
                               wt_steps(proj, [("w1", "write BASE.txt")])
                               + wt_steps(proj, [("w2", "append BASE.txt"),
                                                 ("w3", "write C.txt")],
                                          wave=2))
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            for n in ("w1", "w2", "w3"):
                self.assertTrue(st["steps"][n]["merged"])
            self.assertEqual(len((proj / "BASE.txt")
                                 .read_text().splitlines()), 2)

    def test_conflict_in_wave_two_leaves_wave_one_landed(self):
        """A wave-2 conflict must not disturb wave-1's already-merged work."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base,
                               wt_steps(proj, [("w1", "write BASE.txt")])
                               + wt_steps(proj, [("w2", "append BASE.txt"),
                                                 ("w3", "append BASE.txt")],
                                          wave=2),
                               extra='on_failure = "continue"')
            self.assertEqual(run_manifest(m, runs), 1)
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["w1"]["merged"])
            self.assertTrue(st["steps"]["w2"]["merged"])
            self.assertEqual(st["steps"]["w3"]["status"], "failed")
            self.assertTrue((proj / "BASE.txt").read_text().strip())


class MergeResolverTests(unittest.TestCase):
    """merge_resolver: a conflict spawns a resolver session in the main
    checkout (mid-merge) instead of only parking the worktree."""

    def _conflict_manifest(self, base: Path, proj: Path, resolver: bool):
        flag = f'merge_resolver = {"true" if resolver else "false"}'
        return write_manifest(base, wt_steps(proj, [
            ("p1", "append to WORK.txt"),
            ("p2", "append to WORK.txt")]), extra=flag)

    def test_resolver_completes_conflict_merge(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._conflict_manifest(base, proj, resolver=True)
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["p1"]["merged"])
            p2 = st["steps"]["p2"]
            self.assertEqual(p2["status"], "done")          # NOT failed
            self.assertTrue(p2["merged"])
            self.assertIn("resolved by resolve-p2", p2.get("note", ""))
            # resolver ran as its own recorded agent, in the main checkout
            res = st["steps"]["resolve-p2"]
            self.assertEqual(res["status"], "done")
            self.assertEqual(res["work_dir"], str(proj))
            # union resolution: BOTH lines present, p1's first
            lines = (proj / "WORK.txt").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            # merge state fully concluded, worktree + branch gone
            self.assertFalse(conductor.merge_in_progress(proj))
            self.assertFalse(Path(p2["work_dir"]).is_dir())
            self.assertEqual(git(proj, "branch", "--list",
                                 "conductor/*").stdout.strip(), "")

    def test_resolver_off_keeps_park_behavior(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._conflict_manifest(base, proj, resolver=False)
            self.assertEqual(run_manifest(m, runs), 1)
            st, _ = state_of(runs)
            p2 = st["steps"]["p2"]
            self.assertEqual(p2["status"], "failed")
            self.assertFalse(p2["merged"])
            self.assertTrue(Path(p2["work_dir"]).is_dir())
            self.assertNotIn("resolve-p2", st["steps"])

    def test_resolver_failure_parks_worktree(self):
        """Resolver session itself fails -> merge aborted, worktree kept.
        Same final shape as no-resolver (work preserved, marked failed)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._conflict_manifest(base, proj, resolver=True)
            os.environ["MOCK_RESOLVE_FAIL"] = "1"
            try:
                rc = run_manifest(m, runs)
            finally:
                os.environ.pop("MOCK_RESOLVE_FAIL", None)
            self.assertEqual(rc, 1)
            st, _ = state_of(runs)
            p2 = st["steps"]["p2"]
            self.assertEqual(p2["status"], "failed")
            self.assertFalse(p2["merged"])
            self.assertTrue(Path(p2["work_dir"]).is_dir())
            self.assertEqual(st["steps"]["resolve-p2"]["status"], "failed")
            self.assertFalse(conductor.merge_in_progress(proj))
            self.assertEqual(len((proj / "WORK.txt")
                                 .read_text().splitlines()), 1)

    def test_resolver_run_flag_enables_all_steps(self):
        """merge_resolver under [run] (not [defaults]) also cascades."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [
                ("p1", "append to WORK.txt"),
                ("p2", "append to WORK.txt")]),
                extra='merge_resolver = true')
            cfg = conductor.load_manifest(m)
            self.assertTrue(all(s.merge_resolver for s in cfg.steps))


class ResumeAfterConflictTests(unittest.TestCase):
    def _conflict_run(self, base: Path):
        proj = make_project(base)
        runs = base / "runs"
        m = write_manifest(base, wt_steps(proj, [
            ("p1", "append to WORK.txt"),
            ("p2", "append to WORK.txt")]))
        self.assertEqual(run_manifest(m, runs), 1)
        st, rd = state_of(runs)
        self.assertEqual(st["steps"]["p2"]["status"], "failed")
        return proj, runs, m, rd

    def test_resume_does_not_rerun_conflicted_branch(self):
        """Re-running on the same stale branch shares the conflict's merge
        base - it can never help. Resume must park it, not retry the step."""
        with tempfile.TemporaryDirectory() as td:
            proj, runs, m, rd = self._conflict_run(Path(td))
            before = len((proj / "WORK.txt").read_text().splitlines())
            self.assertEqual(run_manifest(m, runs, resume=str(rd)), 1)
            st, _ = state_of(runs)
            p2 = st["steps"]["p2"]
            self.assertEqual(p2["status"], "failed")
            self.assertFalse(p2["merged"])
            self.assertIn("still conflicts", p2.get("note", ""))
            # no extra append happened on the parked branch's file in main
            self.assertEqual(len((proj / "WORK.txt")
                                 .read_text().splitlines()), before)

    def test_resume_with_resolver_finishes_merge(self):
        """The flagship recovery: run once (conflict parks p2), flip
        merge_resolver on, --resume: the resolver session completes the
        merge and BOTH appends land."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [
                ("p1", "append to WORK.txt"),
                ("p2", "append to WORK.txt")]))
            self.assertEqual(run_manifest(m, runs), 1)
            _, rd = state_of(runs)
            # same manifest + merge_resolver on -> resume resolves p2
            m2 = write_manifest(base, wt_steps(proj, [
                ("p1", "append to WORK.txt"),
                ("p2", "append to WORK.txt")]),
                extra='merge_resolver = true')
            self.assertEqual(run_manifest(m2, runs, resume=str(rd)), 0)
            st, _ = state_of(runs)
            self.assertEqual(st["steps"]["p2"]["status"], "done")
            self.assertTrue(st["steps"]["p2"]["merged"])
            self.assertEqual(len((proj / "WORK.txt")
                                 .read_text().splitlines()), 2)

    def test_late_merge_conflict_marks_failed_and_parks(self):
        """done-but-never-merged + resume where the branch now conflicts:
        status flips to failed, worktree kept (existing contract)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            rd = runs / "20260101-000000-t"
            rd.mkdir(parents=True)
            branch = "conductor/20260101-000000-t/wt"
            main = git(proj, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            git(proj, "checkout", "-b", branch)
            (proj / "WORK.txt").write_text("branch line\n", encoding="utf-8")
            git(proj, "add", "-A")
            git(proj, "commit", "-m", "branch work")
            git(proj, "checkout", main)
            (proj / "WORK.txt").write_text("main line\n", encoding="utf-8")
            git(proj, "add", "-A")
            git(proj, "commit", "-m", "main moved")
            state = {"run": "t", "run_dir": str(rd),
                     "started_at": "2026-01-01T00:00:00", "steps": {
                        "wt": {"name": "wt", "slug": "proj", "status": "done",
                               "branch": branch}}}
            (rd / "state.json").write_text(json.dumps(state))
            m = write_manifest(base, f'''
[[step]]
name = "wt"
project = "{proj.as_posix()}"
prompt = "x"
worktree = true
''')
            self.assertEqual(run_manifest(m, runs, resume=str(rd)), 1)
            st = json.loads((rd / "state.json").read_text())
            self.assertEqual(st["steps"]["wt"]["status"], "failed")
            self.assertFalse(st["steps"]["wt"]["merged"])


class StaleMergeCleanupTests(unittest.TestCase):
    """abort_own_merge: a conductor merge left in progress (conductor died
    mid-resolution) must be backed out on the next pass - and ONLY ours."""

    def _repo_with_merge(self, base: Path, branch_prefix: str):
        proj = make_project(base)
        main = git(proj, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        git(proj, "checkout", "-b", f"{branch_prefix}/x")
        (proj / "WORK.txt").write_text("side\n", encoding="utf-8")
        git(proj, "add", "-A")
        git(proj, "commit", "-m", "side")
        git(proj, "checkout", main)
        (proj / "WORK.txt").write_text("main\n", encoding="utf-8")
        git(proj, "add", "-A")
        git(proj, "commit", "-m", "main")
        git(proj, "merge", "--no-edit", f"{branch_prefix}/x")  # conflicts
        return proj

    def test_aborts_conductor_merge(self):
        with tempfile.TemporaryDirectory() as td:
            proj = self._repo_with_merge(Path(td), "conductor/run1")
            self.assertTrue(conductor.merge_in_progress(proj))
            self.assertTrue(conductor.abort_own_merge(proj))
            self.assertFalse(conductor.merge_in_progress(proj))
            self.assertEqual((proj / "WORK.txt").read_text().strip(), "main")

    def test_leaves_foreign_merge_alone(self):
        with tempfile.TemporaryDirectory() as td:
            proj = self._repo_with_merge(Path(td), "feature")
            self.assertFalse(conductor.abort_own_merge(proj))
            self.assertTrue(conductor.merge_in_progress(proj))
            git(proj, "merge", "--abort")   # fixture cleanup

    def test_no_merge_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            proj = make_project(Path(td))
            self.assertFalse(conductor.abort_own_merge(proj))


class TokenMetricTests(unittest.TestCase):
    """ATIF final_metrics -> state.json tokens -> dashboard payload."""

    def test_tail_metrics_parses_export(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "e.json"
            p.write_text(json.dumps({"final_metrics": {
                "total_prompt_tokens": 258004,
                "total_completion_tokens": 2412,
                "total_cached_tokens": 235620, "total_steps": 18}}))
            self.assertEqual(conductor.tail_metrics(p), {
                "prompt": 258004, "completion": 2412,
                "cached": 235620, "llm_steps": 18})
            self.assertIsNone(conductor.tail_metrics(Path(td) / "gone"))
            bare = Path(td) / "bare.json"
            bare.write_text(json.dumps({"mock": True}))
            self.assertIsNone(conductor.tail_metrics(bare))  # no final_metrics

    def test_tokens_reach_dashboard_run_totals(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            rd = runs / "20260101-000000-t"
            rd.mkdir(parents=True)
            (rd / "state.json").write_text(json.dumps({
                "run": "t", "started_at": "2026-01-01T00:00:00",
                "ended_at": "2026-01-01T00:10:00", "steps": {
                    "a": {"name": "a", "status": "done",
                          "tokens": {"prompt": 1000, "completion": 100,
                                     "cached": 800, "llm_steps": 3}},
                    "b": {"name": "b", "status": "done",
                          "tokens": {"prompt": 2000, "completion": 200,
                                     "cached": 1500, "llm_steps": 5}},
                    "c": {"name": "c", "status": "done"}}}))
            d = conductor.collect_dashboard(runs)
            self.assertEqual(d["runs"][0]["tokens"],
                             {"prompt": 3000, "completion": 300,
                              "cached": 2300})
            by_name = {a["step"]: a for a in d["agents"]}
            self.assertEqual(by_name["a"]["tokens"]["llm_steps"], 3)
            self.assertIsNone(by_name["c"]["tokens"])


class CapAndOrderingTests(unittest.TestCase):
    def test_capped_wave_still_merges_every_member(self):
        """max_parallel=1 on a 3-member worktree wave: serialized execution,
        and the merge pass still lands all three branches in order."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [
                ("w1", "write A.txt"), ("w2", "write B.txt"),
                ("w3", "write C.txt")]), extra='max_parallel = 1')
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            for n in ("w1", "w2", "w3"):
                self.assertTrue(st["steps"][n]["merged"])


if __name__ == "__main__":
    unittest.main()
