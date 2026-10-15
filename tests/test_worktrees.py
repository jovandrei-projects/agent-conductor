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
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
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


class SessionCleanupTests(unittest.TestCase):
    def test_gone_workdir_sessions_still_collected(self):
        """Worktree dirs are deleted after merge - their recorded session
        ids must still reach `sessions --rm` (devin rm needs only the id)."""
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            rd = runs / "r1"
            rd.mkdir(parents=True)
            (rd / "state.json").write_text(json.dumps({
                "run": "t", "ended_at": "2026-01-01T00:00:00",
                "steps": {"s": {"session_id": "abc",
                                "status": "done",
                                "work_dir": str(Path(td) / "deleted-wt")}}}))
            got = conductor.collect_sessions(runs, [MOCK])
            self.assertEqual([g["sid"] for g in got], ["abc"])
            self.assertTrue(got[0]["dir_gone"])

    def test_existing_workdir_sessions_unaffected(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            rd = runs / "r1"
            rd.mkdir(parents=True)
            wd = Path(td) / "proj"
            wd.mkdir()
            (rd / "state.json").write_text(json.dumps({
                "run": "t", "ended_at": "2026-01-01T00:00:00",
                "steps": {"s": {"session_id": "abc",
                                "status": "done", "work_dir": str(wd)}}}))
            got = conductor.collect_sessions(runs, [MOCK])
            self.assertEqual(got, [])   # 'abc' not in mock's list output

    def test_default_runs_dir_also_scans_worktrees(self):
        """A dev worktree's spawned sessions are whitelisted only in ITS
        data/runs - collect_sessions on the default RUNS_DIR must reach
        them (trap 13); a custom runs_dir must NOT."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            wt_runs = base / "wt" / "data" / "runs"
            (wt_runs / "r2").mkdir(parents=True)
            (wt_runs / "r2" / "state.json").write_text(json.dumps({
                "run": "wt", "ended_at": "2026-01-01T00:00:00",
                "steps": {"s": {"session_id": "wt-sess", "status": "done",
                                "work_dir": str(base / "deleted-wt")}}}))
            with mock.patch.object(conductor, "RUNS_DIR",
                                   base / "main" / "runs"), \
                    mock.patch.object(conductor,
                                      "_worktree_checkout_paths",
                                      lambda: [base / "wt"]):
                got = conductor.collect_sessions(
                    Path(conductor.RUNS_DIR), [MOCK])
                self.assertIn("wt-sess", [g["sid"] for g in got])
                # ...but a caller pointing at some other runs dir does
                # not reach into worktree checkouts
                got = conductor.collect_sessions(base / "other", [MOCK])
                self.assertNotIn("wt-sess", [g["sid"] for g in got])


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
                              "cached": 2300, "llm_steps": 8})
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


class MergeRunTests(unittest.TestCase):
    """The dashboard's 'Merge pending branches' button -> merge_run(): the
    manual counterpart to the post-wave merge loop, for branches parked by
    a conflict or left unmerged by a dead conductor."""

    def test_merge_run_lands_parked_branch(self):
        """A timed-out step's worktree holds committed work; a manual merge
        lands it and drops worktree+branch."""
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
            st, rd = state_of(runs)
            self.assertEqual(st["steps"]["stuck"]["status"], "timed_out")
            self.assertTrue(Path(st["steps"]["stuck"]["work_dir"]).is_dir())
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["stuck"])
            self.assertEqual(out["conflicted"], [])
            st, _ = state_of(runs)
            rec = st["steps"]["stuck"]
            self.assertTrue(rec["merged"])
            self.assertIn("merged via dashboard", rec["note"])
            self.assertTrue((proj / "A.txt").is_file())
            self.assertFalse(Path(rec["work_dir"]).is_dir())
            self.assertEqual(git(proj, "branch", "--list",
                                 "conductor/*").stdout.strip(), "")

    def test_merge_run_parks_still_conflicting_branch(self):
        """A real conflict stays conflicted: merge_run reports it, the
        worktree+branch are kept, and no merge is left in progress."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [
                ("p1", "append to WORK.txt"),
                ("p2", "append to WORK.txt")]),
                extra='on_failure = "continue"')
            self.assertEqual(run_manifest(m, runs), 1)
            st, rd = state_of(runs)
            self.assertEqual(st["steps"]["p2"]["status"], "failed")
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], [])
            self.assertEqual(out["conflicted"], ["p2"])
            st, _ = state_of(runs)
            self.assertFalse(st["steps"]["p2"]["merged"])
            self.assertTrue(Path(st["steps"]["p2"]["work_dir"]).is_dir())
            self.assertFalse(conductor.merge_in_progress(proj))

    def test_merge_run_resolver_finishes_conflict(self):
        """Flipping merge_resolver on in the manifest lets merge_run spawn
        the resolve-* session that completes a parked conflict."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [
                ("p1", "append to WORK.txt"),
                ("p2", "append to WORK.txt")]),
                extra='on_failure = "continue"')
            self.assertEqual(run_manifest(m, runs), 1)
            _, rd = state_of(runs)
            write_manifest(base, wt_steps(proj, [   # same path, flag flipped
                ("p1", "append to WORK.txt"),
                ("p2", "append to WORK.txt")]),
                extra='merge_resolver = true')
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["p2"])
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["p2"]["merged"])
            self.assertEqual(st["steps"]["resolve-p2"]["status"], "done")
            self.assertEqual(len((proj / "WORK.txt")
                                 .read_text().splitlines()), 2)
            self.assertFalse(conductor.merge_in_progress(proj))

    def test_merge_run_refuses_a_live_step(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "runs"
            rd = runs / "20261014-000000-t"
            rd.mkdir(parents=True)
            (rd / "state.json").write_text(json.dumps({
                "steps": {"s": {"status": "running",
                                "started_at": conductor.now_iso(),
                                "timeout_min": 60, "work_dir": str(base),
                                "branch": "conductor/x/s"}}}))
            with self.assertRaises(conductor.RequestError) as e:
                conductor.merge_run(rd.name, runs, ["devin"])
            self.assertEqual(e.exception.status, 409)

    def test_merge_run_no_pending_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "first"
''')
            self.assertEqual(run_manifest(m, runs), 0)
            _, rd = state_of(runs)
            out = conductor.merge_run(rd.name, runs, ["devin"])
            self.assertEqual(out["merged"], [])
            self.assertEqual(out["conflicted"], [])
            self.assertEqual(out["skipped"], [])


class StopControlTests(unittest.TestCase):
    """Dashboard stop controls: tempdir sentinels (never inside run_dir -
    trap 5) polled by wait_worker + the wave loop. Per-step stop, run stop,
    drain, pre-spawn skip, and resume clearing + interruption notes."""

    def _flag_soon(self, runs: Path, flag, delay=0.8, watch_for=None):
        """Raise `flag(rd_name, *rest)` once the run dir (and optionally a
        step record in state.json) exists - stop requests arrive late."""
        def work():
            for _ in range(600):
                try:
                    rd = next(runs.iterdir())
                    st = json.loads(
                        (rd / "state.json").read_text(encoding="utf-8"))
                    if watch_for is None or watch_for in (
                            st.get("steps") or {}):
                        flag(rd)
                        return
                except (StopIteration, OSError, ValueError):
                    pass
                time.sleep(0.05)
        threading.Thread(target=work, daemon=True).start()

    def test_step_stop_kills_worker_and_marks_stopped(self):
        """Flag mid-run: wait_worker tree-kills, status 'stopped', the
        committed work product survives."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            self._flag_soon(
                runs, lambda rn: conductor._flag_raise(
                    conductor.stop_flag(rn, "slow").name),
                watch_for="slow")
            m = write_manifest(base, f'''
[[step]]
name = "slow"
project = "{proj.as_posix()}"
prompt = "sleep_after=30 write A.txt"
timeout_min = 60
''')
            with mock.patch.object(conductor, "WAIT_POLL_S", 0.2):
                self.assertEqual(run_manifest(m, runs), 1)
            st, rd = state_of(runs)
            rec = st["steps"]["slow"]
            self.assertEqual(rec["status"], "stopped")
            self.assertIn("stopped by user", rec["note"])
            # work was committed before the kill - the product survived
            self.assertTrue((proj / "A.txt").is_file())

    def test_run_stop_flag_halts_between_waves(self):
        """Run-level stop: the live step is killed 'stopped', the next
        wave never launches and records 'skipped' for resume."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            self._flag_soon(
                runs, lambda rn: conductor._flag_raise(
                    conductor.stop_flag(rn).name),
                watch_for="one")
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "sleep_after=30 write A.txt"
timeout_min = 60

[[step]]
name = "two"
project = "{proj.as_posix()}"
prompt = "write B.txt"
''')
            with mock.patch.object(conductor, "WAIT_POLL_S", 0.2):
                self.assertEqual(run_manifest(m, runs), 1)
            st, rd = state_of(runs)
            self.assertEqual(st["steps"]["one"]["status"], "stopped")
            self.assertEqual(st["steps"]["two"]["status"], "skipped")
            self.assertEqual(st.get("aborted"), "stopped")
            self.assertFalse((proj / "B.txt").is_file())

    def test_drain_lets_step_finish_but_blocks_the_next_wave(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            self._flag_soon(
                runs, lambda rn: conductor._flag_raise(
                    conductor.drain_flag(rn).name),
                delay=0.1, watch_for="one")
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "sleep_after=1 write A.txt"
timeout_min = 60

[[step]]
name = "two"
project = "{proj.as_posix()}"
prompt = "write B.txt"
''')
            # rc 1: a drained run is intentionally incomplete
            self.assertEqual(run_manifest(m, runs), 1)
            st, rd = state_of(runs)
            self.assertEqual(st["steps"]["one"]["status"], "done")
            self.assertEqual(st["steps"]["two"]["status"], "skipped")
            self.assertEqual(st.get("aborted"), "drained")
            self.assertTrue((proj / "A.txt").is_file())
            self.assertFalse((proj / "B.txt").is_file())

    def test_queued_step_flag_skips_without_spawning(self):
        """A flag for a pool-queued step (max_parallel=1) is consumed at
        run_step entry: 'skipped', no mock session, work never written."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            self._flag_soon(
                runs, lambda rn: conductor._flag_raise(
                    conductor.stop_flag(rn, "b").name),
                watch_for="b")       # 'b' appears in state once queued
            m = write_manifest(base, wt_steps(proj, [
                ("a", "sleep=2 write A.txt"), ("b", "write B.txt")]),
                extra='max_parallel = 1')
            self.assertEqual(run_manifest(m, runs), 1)
            st, rd = state_of(runs)
            self.assertEqual(st["steps"]["a"]["status"], "done")
            self.assertEqual(st["steps"]["b"]["status"], "skipped")
            self.assertIn("stopped by user", st["steps"]["b"]["note"])
            self.assertFalse((proj / "B.txt").is_file())

    def test_resume_after_stop_reruns_with_interruption_note(self):
        """Stopped step + --resume: flags cleared, fresh session starts,
        prompt carries the 'ended abruptly' note."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            self._flag_soon(
                runs, lambda rn: conductor._flag_raise(
                    conductor.stop_flag(rn, "slow").name),
                watch_for="slow")
            m = write_manifest(base, f'''
[[step]]
name = "slow"
project = "{proj.as_posix()}"
prompt = "sleep_after=30 write A.txt"
timeout_min = 60
''')
            with mock.patch.object(conductor, "WAIT_POLL_S", 0.2):
                self.assertEqual(run_manifest(m, runs), 1)
            st, rd = state_of(runs)
            self.assertEqual(st["steps"]["slow"]["status"], "stopped")
            self.assertEqual(run_manifest(m, runs, resume=str(rd)), 0)
            st, _ = state_of(runs)
            self.assertEqual(st["steps"]["slow"]["status"], "done")
            prompt = (rd / "prompts" / "slow.md").read_text(
                encoding="utf-8")
            self.assertIn("ended abruptly", prompt)
            # sentinels were cleared by the resume - nothing lingers
            self.assertFalse(conductor.stop_flag(rd).is_file())
            self.assertFalse(
                conductor.stop_flag(rd, "slow").is_file())


class StopEndpointTests(unittest.TestCase):
    """stop_step/stop_run validation + flag semantics (the HTTP layer is a
    thin fullmatch -> call; these exercise it directly)."""

    def _state(self, base, run="r1", steps=None):
        runs = base / "runs"
        rd = runs / run
        rd.mkdir(parents=True)
        (rd / "state.json").write_text(json.dumps(
            {"run": "t", "steps": steps or {}}))
        return runs, rd

    def test_stop_step_writes_flag_and_rejects_dead(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs, rd = self._state(base, steps={
                "s": {"status": "queued", "name": "s"},
                "d": {"status": "done", "name": "d"}})
            out = conductor.stop_step(rd.name, "s", runs)
            self.assertEqual(out["stopping"], "s")
            self.assertTrue(conductor.stop_flag(rd, "s").is_file())
            conductor.stop_flag(rd, "s").unlink()
            with self.assertRaises(conductor.RequestError) as e:
                conductor.stop_step(rd.name, "d", runs)
            self.assertEqual(e.exception.status, 409)
            with self.assertRaises(conductor.RequestError):
                conductor.stop_step(rd.name, "nope", runs)
            with self.assertRaises(conductor.RequestError):
                conductor.stop_step("../x", "s", runs)

    def test_stop_run_drain_and_hard(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs, rd = self._state(base, steps={
                "s": {"status": "running", "name": "s",
                      "started_at": conductor.now_iso(),
                      "timeout_min": 60}})
            # dead pid-less 'running' record isn't live -> flags only
            out = conductor.stop_run(rd.name, {"drain": True}, runs)
            self.assertEqual(out["draining"], rd.name)
            self.assertTrue(conductor.drain_flag(rd).is_file())
            out = conductor.stop_run(rd.name, {}, runs)
            # a pid-less 'running' rec is still live to step_live (within
            # timeout): it is flagged for kill even though there is no pid
            self.assertEqual(out["stopping"], ["s"])
            self.assertTrue(conductor.stop_flag(rd).is_file())
            self.assertTrue(conductor.stop_flag(rd, "s").is_file())
            conductor.clear_run_flags(rd)
            self.assertFalse(conductor.drain_flag(rd).is_file())
            self.assertFalse(conductor.stop_flag(rd).is_file())

    def test_stop_run_refuses_ended_runs(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs, rd = self._state(base)
            st = json.loads((rd / "state.json").read_text())
            st["ended_at"] = conductor.now_iso()
            (rd / "state.json").write_text(json.dumps(st))
            with self.assertRaises(conductor.RequestError) as e:
                conductor.stop_run(rd.name, {}, runs)
            self.assertEqual(e.exception.status, 409)


class TimeoutOverrideTests(unittest.TestCase):
    """The dashboard timeout editor writes a minutes sentinel that
    wait_worker polls - the live deadline moves without a restart."""

    def _state(self, base, run="r1", steps=None):
        runs = base / "runs"
        rd = runs / run
        rd.mkdir(parents=True)
        (rd / "state.json").write_text(json.dumps(
            {"run": "t", "steps": steps or {}}))
        return runs, rd

    def test_wait_worker_honors_a_shortened_timeout(self):
        """Flag written mid-wait -> next poll recomputes the deadline; a
        value under elapsed tree-kills instead of waiting timeout_min."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "l.log"
            log.write_text("", encoding="utf-8")
            flag = Path(td) / "timeout.flag"
            flag.write_text("0.01", encoding="utf-8")   # 0.6s, already past
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"])
            try:
                with mock.patch.object(conductor, "WAIT_POLL_S", 0.1):
                    rc, timed_out, _hung, stopped, _ext = \
                        conductor.wait_worker(
                        proc, log, 600, timeout_flag=flag)
                self.assertTrue(timed_out)
                self.assertFalse(stopped)
                self.assertNotEqual(rc, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()

    def test_set_step_timeout_writes_flag_and_validates(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs, rd = self._state(base, steps={
                "s": {"name": "s", "status": "running",
                      "started_at": conductor.now_iso(),
                      "timeout_min": 60},
                "d": {"name": "d", "status": "done"}})
            out = conductor.set_step_timeout(
                rd.name, "s", {"minutes": 45}, runs)
            self.assertEqual(out["timeout_min"], 45.0)
            self.assertEqual(conductor.timeout_flag(
                rd, "s").read_text().strip(), "45.0")
            with self.assertRaises(conductor.RequestError):
                conductor.set_step_timeout(rd.name, "s",
                                           {"minutes": 0}, runs)
            with self.assertRaises(conductor.RequestError) as e:
                conductor.set_step_timeout(rd.name, "d",
                                           {"minutes": 45}, runs)
            self.assertEqual(e.exception.status, 409)
            with self.assertRaises(conductor.RequestError):
                conductor.set_step_timeout("../x", "s",
                                           {"minutes": 45}, runs)

    def _sleeper(self):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            **conductor.hidden_subprocess_kwargs())

    def test_active_log_extends_past_deadline_then_silence_kills(self):
        """timeout_min is a floor: a still-appending log keeps the worker
        alive past it; once output stops, stall_min ends it."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "l.log"
            log.write_text("", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 "import time,sys,pathlib;"
                 "p=pathlib.Path(sys.argv[1]);t0=time.time();"
                 "exec('while time.time()-t0<3:\\n"
                 " p.open(\"a\").write(\"x\");time.sleep(0.15)');"
                 "time.sleep(300)", str(log)],
                **conductor.hidden_subprocess_kwargs())
            try:
                with mock.patch.object(conductor, "WAIT_POLL_S", 0.05):
                    t0 = time.time()
                    rc, timed_out, _h, stopped, ext = \
                        conductor.wait_worker(proc, log, 1.0, stall_s=1.0)
            finally:
                if proc.poll() is None:
                    proc.kill()
            self.assertTrue(timed_out)
            self.assertFalse(stopped)
            # floor 1s + writing till ~3s + 1s silence -> died ~4s in
            self.assertGreater(ext, 1.5)
            self.assertLess(time.time() - t0, 15)

    def test_direct_pid_kill_still_reports_stopped(self):
        """The stop endpoint kills the pid immediately - proc.wait can
        return before the next sentinel poll, and the step must record
        'stopped', not a bare-rc 'failed'."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "l.log"
            log.write_text("", encoding="utf-8")
            rd = Path(td) / "runs" / "20260101-000000-r"
            rd.mkdir(parents=True)
            flag = conductor.stop_flag(rd)
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
            proc = self._sleeper()
            try:
                proc.kill()                    # the endpoint's _kill_pid
                with mock.patch.object(conductor, "WAIT_POLL_S", 0.05):
                    rc, timed_out, _h, stopped, _e = conductor.wait_worker(
                        proc, log, 60, stop_flags=(flag,))
            finally:
                flag.unlink(missing_ok=True)
                if proc.poll() is None:
                    proc.kill()
            self.assertTrue(stopped)
            self.assertFalse(timed_out)
            self.assertNotEqual(rc, 0)

    def test_silent_worker_dies_at_the_floor(self):
        """A worker that never writes gets deadline + one stall window,
        not a full extra floor."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "l.log"
            log.write_text("", encoding="utf-8")
            proc = self._sleeper()
            try:
                with mock.patch.object(conductor, "WAIT_POLL_S", 0.05):
                    t0 = time.time()
                    rc, timed_out, _h, _s, ext = conductor.wait_worker(
                        proc, log, 0.5, stall_s=1.0)
            finally:
                if proc.poll() is None:
                    proc.kill()
            self.assertTrue(timed_out)
            self.assertLess(time.time() - t0, 8)

    def test_ceiling_caps_an_always_active_log(self):
        """Even a nonstop log dies at the (1+TIMEOUT_MAX_EXT)x ceiling -
        activity is never an infinite lease."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "l.log"
            log.write_text("", encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 "import time,sys,pathlib;"
                 "p=pathlib.Path(sys.argv[1]);"
                 "exec('while True:\\n p.open(\"a\").write(\"x\");"
                 "time.sleep(0.1)')", str(log)],
                **conductor.hidden_subprocess_kwargs())
            try:
                with mock.patch.object(conductor, "WAIT_POLL_S", 0.05):
                    t0 = time.time()
                    rc, timed_out, _h, _s, ext = conductor.wait_worker(
                        proc, log, 1.0, stall_s=1.0)
            finally:
                if proc.poll() is None:
                    proc.kill()
            self.assertTrue(timed_out)
            # ceiling = 4x the 1s floor -> dead around 4s, never 60s+
            self.assertLess(time.time() - t0, 10)
            self.assertGreaterEqual(ext, 2.5)

    def test_override_flag_visible_in_dashboard_timeout(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs, rd = self._state(base, steps={
                "s": {"name": "s", "status": "running",
                      "started_at": conductor.now_iso(),
                      "timeout_min": 60, "work_dir": str(base / "p")}})
            with mock.patch.object(conductor, "refresh_prices"):
                d = conductor.collect_dashboard(runs, ["no-such-devin"])
            self.assertEqual(d["agents"][0]["timeout_min"], 60)
            conductor._flag_dir().mkdir(exist_ok=True)
            conductor.timeout_flag(rd, "s").write_text("90")
            try:
                with mock.patch.object(conductor, "refresh_prices"):
                    d = conductor.collect_dashboard(runs, ["no-such-devin"])
                self.assertEqual(d["agents"][0]["timeout_min"], 90.0)
            finally:
                conductor.timeout_flag(rd, "s").unlink(
                    missing_ok=True)


if __name__ == "__main__":
    unittest.main()
