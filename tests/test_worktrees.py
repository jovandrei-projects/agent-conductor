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
import argparse
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
    run_manifest, slow  # noqa: E402


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


@slow
class ManualMergeGuardTests(unittest.TestCase):
    """'Merge pending branches' must stay out of a run that is still
    executing: the pending branches ARE the run's live work surface, so a
    stale dashboard page or a direct `conductor.py merge` must hit the
    same gate the disabled button shows. Only a run whose driver is
    genuinely gone is mergeable - a stop request, a pause, or a
    half-finished shutdown is not 'stopped'."""

    def _sleeper(self):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            **conductor.hidden_subprocess_kwargs())

    def _dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"],
                             **conductor.hidden_subprocess_kwargs())
        p.wait()
        return p.pid

    def _set_state(self, rd: Path, **fields):
        st = json.loads((rd / "state.json").read_text(encoding="utf-8"))
        st.update(fields)
        (rd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        return st

    def _parked_run(self, td):
        """A real ENDED run holding one timed-out (pending) worktree
        branch that merges cleanly - the shape the manual merge exists
        for."""
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
        self.assertIsNot(st["steps"]["stuck"].get("merged"), True)
        self.assertTrue(st["steps"]["stuck"].get("branch"))
        return proj, runs, rd, m

    def test_refused_while_driver_alive_then_lands_once_stopped(self):
        """The core gate: a live driver - even with NO step currently
        'running' (the between-wave / verify-merge window the old
        step-only check missed) - refuses the merge. A raised stop
        sentinel is a REQUEST, not a completed stop: still refused. Once
        the driver is gone and the run is stamped stopped, it lands."""
        with tempfile.TemporaryDirectory() as td:
            proj, runs, rd, _m = self._parked_run(td)
            branch = json.loads(
                (rd / "state.json").read_text())["steps"]["stuck"]["branch"]
            drv = self._sleeper()
            try:
                self._set_state(rd,
                                driver_pid=drv.pid,
                                driver_started_at=conductor.now_iso())
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.merge_run(rd.name, runs, shlex.split(MOCK))
                self.assertEqual(e.exception.status, 409)
                self.assertIn("still executing", str(e.exception))
                # the parked branch was not touched
                self.assertFalse(conductor.merge_in_progress(proj))
                self.assertEqual(
                    git(proj, "rev-parse", "--verify", branch)
                    .returncode, 0)
                # stop requested -> the sentinel exists but the driver is
                # still mid-shutdown: not 'stopped' yet
                conductor.stop_flag(rd).parent.mkdir(exist_ok=True)
                conductor.stop_flag(rd).touch()
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.merge_run(rd.name, runs, shlex.split(MOCK))
                self.assertEqual(e.exception.status, 409)
            finally:
                conductor.stop_flag(rd).unlink(missing_ok=True)
                drv.terminate()
                drv.wait()
            # genuinely stopped: dead driver + ended_at + abort reason
            self._set_state(rd, driver_pid=self._dead_pid(),
                            ended_at=conductor.now_iso(),
                            aborted="stopped")
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["stuck"])
            self.assertTrue((proj / "A.txt").is_file())
            self.assertFalse(conductor.merge_in_progress(proj))
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["stuck"]["merged"])
            # the run's merge claim was released with the operation
            self.assertFalse(conductor.merge_claim_flag(rd).exists())

    def test_refused_in_the_queued_only_window(self):
        """Between waves every step reads 'queued'/'done' - nothing is
        'running', so the old check passed; a live driver still makes it
        executing."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "runs"
            rd = runs / "20261016-000000-t"
            rd.mkdir(parents=True)
            drv = self._sleeper()
            try:
                (rd / "state.json").write_text(json.dumps({
                    "run": "t",
                    "driver_pid": drv.pid,
                    "driver_started_at": conductor.now_iso(),
                    "steps": {
                        "done1": {"status": "done", "merged": True,
                                  "branch": "conductor/t/done1"},
                        "next": {"status": "queued",
                                 "branch": "conductor/t/next"}}}),
                    encoding="utf-8")
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.merge_run(rd.name, runs, ["devin"])
                self.assertEqual(e.exception.status, 409)
                self.assertIn("still executing", str(e.exception))
            finally:
                drv.terminate()
                drv.wait()

    def test_incomplete_shutdown_is_not_stopped(self):
        """The shutdown-acknowledged window: driver still alive, the run
        not yet stamped ended/aborted - 'stopping', not 'stopped'."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "runs"
            rd = runs / "20261016-000000-t"
            rd.mkdir(parents=True)
            drv = self._sleeper()
            try:
                (rd / "state.json").write_text(json.dumps({
                    "run": "t",
                    "driver_pid": drv.pid,
                    "driver_started_at": conductor.now_iso(),
                    "steps": {
                        "p": {"status": "failed", "merged": False,
                              "branch": "conductor/t/p",
                              "work_dir": str(base / "w")}}}),
                    encoding="utf-8")
                for flag in (conductor.stop_flag, conductor.pause_flag,
                             conductor.drain_flag):
                    f = flag(rd)
                    f.parent.mkdir(exist_ok=True)
                    f.touch()
                    try:
                        with self.assertRaises(conductor.RequestError) \
                                as e:
                            conductor.merge_run(rd.name, runs, ["devin"])
                        self.assertEqual(e.exception.status, 409)
                    finally:
                        f.unlink(missing_ok=True)
            finally:
                drv.terminate()
                drv.wait()

    def test_dead_interrupted_run_merges(self):
        """Interrupted is not executing: dead driver, a stale 'running'
        record backed by a dead pid - the run can be resumed, but the
        pending branches are durable and mergeable now."""
        with tempfile.TemporaryDirectory() as td:
            proj, runs, rd, _m = self._parked_run(td)
            st = self._set_state(rd, driver_pid=self._dead_pid())
            # an interrupted run may still carry a stale 'running' row
            st["steps"]["ghost"] = {
                "status": "running", "pid": self._dead_pid(),
                "started_at": conductor.now_iso(),
                "work_dir": str(Path(td) / "gone")}
            (rd / "state.json").write_text(json.dumps(st),
                                           encoding="utf-8")
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["stuck"])
            self.assertTrue((proj / "A.txt").is_file())

    def test_rechecks_state_under_the_claim(self):
        """Stale-client shape: state looked finished at first read, but a
        driver stamped itself before the claim landed - the check that
        runs UNDER the claim must catch it."""
        with tempfile.TemporaryDirectory() as td:
            proj, runs, rd, _m = self._parked_run(td)
            drv = self._sleeper()
            try:
                orig = conductor._claim_run_merge

                def flip_then_claim(d, by):
                    # a resume stamped itself in the gap between our first
                    # state read and the claim
                    self._set_state(
                        rd, driver_pid=drv.pid,
                        driver_started_at=conductor.now_iso())
                    return orig(d, by)

                with mock.patch.object(conductor, "_claim_run_merge",
                                       side_effect=flip_then_claim):
                    with self.assertRaises(conductor.RequestError) as e:
                        conductor.merge_run(rd.name, runs,
                                            shlex.split(MOCK))
                self.assertEqual(e.exception.status, 409)
                self.assertIn("still executing", str(e.exception))
                # nothing was merged and the claim was released
                self.assertFalse(conductor.merge_claim_flag(rd).exists())
                st, _ = state_of(runs)
                self.assertIsNot(st["steps"]["stuck"].get("merged"), True)
            finally:
                drv.terminate()
                drv.wait()

    def test_merge_claim_and_resume_are_mutually_exclusive(self):
        """The start/resume race goes through the shared claim: a merge in
        flight refuses resume, and a resume mid-stamp refuses merge."""
        with tempfile.TemporaryDirectory() as td:
            proj, runs, rd, m = self._parked_run(td)
            claim = conductor._claim_run_merge(rd, "resume")
            try:
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.merge_run(rd.name, runs, shlex.split(MOCK))
                self.assertEqual(e.exception.status, 409)
                self.assertIn("resume", str(e.exception))
            finally:
                claim.unlink()
            # the other direction: a held merge claim blocks the
            # dashboard resume endpoint - and a resume's own flag-clear
            # must never delete it out from under the merge
            claim = conductor._claim_run_merge(rd, "merge")
            try:
                conductor.clear_run_flags(rd)
                self.assertTrue(claim.is_file())
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.resume_run(rd.name, runs, ["devin"])
                self.assertEqual(e.exception.status, 409)
                # ...and the CLI resume refuses before stamping anything
                rc = run_manifest(m, runs, resume=str(rd))
                self.assertEqual(rc, 2)
                st, _ = state_of(runs)
                self.assertEqual(st["steps"]["stuck"]["status"],
                                 "timed_out")
            finally:
                claim.unlink()
            # with the claim released both directions are free again
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["stuck"])

    def test_stale_claim_from_a_dead_process_is_broken(self):
        """A claim left by a crashed merger must not wedge the run: the
        next operation breaks it once and proceeds."""
        with tempfile.TemporaryDirectory() as td:
            proj, runs, rd, _m = self._parked_run(td)
            f = conductor.merge_claim_flag(rd)
            f.parent.mkdir(exist_ok=True)
            f.write_text(json.dumps({"pid": self._dead_pid(),
                                     "at": conductor.now_iso(),
                                     "by": "merge"}), encoding="utf-8")
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["stuck"])
            self.assertFalse(f.exists())

    def test_second_merge_while_claimed_refuses(self):
        """Cross-process 'busy': a held live claim is a 409 for a second
        merge - _MERGE_BUSY only covers one process."""
        with tempfile.TemporaryDirectory() as td:
            proj, runs, rd, _m = self._parked_run(td)
            claim = conductor._claim_run_merge(rd, "merge")
            try:
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.merge_run(rd.name, runs, shlex.split(MOCK))
                self.assertEqual(e.exception.status, 409)
                self.assertIn("merge", str(e.exception))
            finally:
                claim.unlink()

    def test_resume_refused_while_driver_alive(self):
        """The dashboard resume also must not start over a live driver -
        the same race the merge claim covers from the other side."""
        with tempfile.TemporaryDirectory() as td:
            proj, runs, rd, m = self._parked_run(td)
            drv = self._sleeper()
            try:
                self._set_state(rd,
                                driver_pid=drv.pid,
                                driver_started_at=conductor.now_iso())
                with mock.patch.object(conductor, "_launch_process") \
                        as launch:
                    with self.assertRaises(conductor.RequestError) as e:
                        conductor.resume_run(rd.name, runs, ["devin"])
                    launch.assert_not_called()
                self.assertEqual(e.exception.status, 409)
            finally:
                drv.terminate()
                drv.wait()

    def test_dashboard_marks_executing_runs(self):
        """The button's gate is a server-computed payload field: a live
        driver marks the row executing even with no 'running' steps."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "runs"
            rd = runs / "20261016-000000-t"
            rd.mkdir(parents=True)
            drv = self._sleeper()
            try:
                (rd / "state.json").write_text(json.dumps({
                    "run": "t",
                    "driver_pid": drv.pid,
                    "driver_started_at": conductor.now_iso(),
                    "steps": {"a": {"status": "queued"}}}),
                    encoding="utf-8")
                with mock.patch.object(conductor, "refresh_prices"):
                    d = conductor.collect_dashboard(runs, ["x"])
                self.assertTrue(d["runs"][0]["executing"])
                # driver gone -> the same payload no longer marks it
                self._set_state(rd, driver_pid=self._dead_pid(),
                                ended_at=conductor.now_iso())
                with mock.patch.object(conductor, "refresh_prices"):
                    d = conductor.collect_dashboard(runs, ["x"])
                self.assertFalse(d["runs"][0]["executing"])
            finally:
                drv.terminate()
                drv.wait()


@slow
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


@slow
class DeferredMergeTests(unittest.TestCase):
    """merge="deferred": worktree isolation WITHOUT the automatic merge-back.

    Branches, worktrees, commits and verify results stay pending in
    state["pending_merges"] until merge_run (the explicit human merge)
    stages + verifies + lands them onto the recorded target. Sequential
    steps see verified predecessors via the per-run/per-project
    integration baseline; wave siblings stay separate alternatives.
    """

    def _deferred_manifest(self, base: Path, proj: Path,
                           steps: list[str], extra: str = "") -> Path:
        """steps: raw [[step]] TOML strings; merge=deferred set run-wide."""
        return write_manifest(base, "\n".join(steps),
                              extra='merge = "deferred"\n' + extra)

    def _dstep(self, proj: Path, name: str, prompt: str,
               more: str = "") -> str:
        return f'''[[step]]
name = "{name}"
project = "{proj.as_posix()}"
prompt = "{prompt}"
{more}'''

    def _pending(self, st: dict, proj: Path) -> dict:
        key = conductor._repo_key(proj)
        return (st.get("pending_merges") or {}).get(key) or {}

    # ------------------------------------------------ manifest validation

    def test_deferred_implies_worktree_and_validates(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            m = self._deferred_manifest(
                base, proj, [self._dstep(proj, "a", "write A.txt")])
            cfg = conductor.load_manifest(m)
            self.assertTrue(cfg.steps[0].worktree)      # implied
            self.assertEqual(cfg.steps[0].merge, "deferred")
            bad = write_manifest(base, f'''[[step]]
name = "x"
project = "{proj.as_posix()}"
prompt = "x"
worktree = false
merge = "deferred"
''')
            with self.assertRaises(conductor.ManifestError):
                conductor.load_manifest(bad)
            bad2 = write_manifest(base, f'''[[step]]
name = "x"
project = "{proj.as_posix()}"
prompt = "x"
cloud = true
merge = "deferred"
''')
            with self.assertRaises(conductor.ManifestError):
                conductor.load_manifest(bad2)
            bad3 = write_manifest(base, f'''[[step]]
name = "x"
project = "{proj.as_posix()}"
prompt = "x"
merge = "whenever"
''')
            with self.assertRaises(conductor.ManifestError):
                conductor.load_manifest(bad3)

    # ------------------------------------------------------- retention

    def test_deferred_retains_everything_as_pending(self):
        """The run ends with main untouched: branch, worktree, commits,
        verify result and pending-merge metadata all persist."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(
                base, proj, [self._dstep(
                    proj, "one", "write A.txt",
                    'verify = "if not exist A.txt exit 1"')])
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            rec = st["steps"]["one"]
            self.assertEqual(rec["status"], "done")
            self.assertTrue(rec["deferred"])
            self.assertTrue(rec["deferred_verified"])
            self.assertEqual(rec["verify_ok"], True)
            self.assertIsNot(rec.get("merged"), True)
            self.assertEqual(rec["merge_kind"], "sequential")
            self.assertTrue(Path(rec["work_dir"]).is_dir())
            self.assertTrue(rec.get("branch"))
            # main untouched, worktree has the work
            self.assertFalse((proj / "A.txt").exists())
            self.assertTrue((Path(rec["work_dir"]) / "A.txt").is_file())
            grp = self._pending(st, proj)
            self.assertEqual(grp["entries"], ["one"])
            self.assertEqual(grp["target_branch"],
                             git(proj, "rev-parse", "--abbrev-ref",
                                 "HEAD").stdout.strip())
            baseline = grp["baseline"]
            self.assertIn("baseline", baseline)
            # recorded target tip = where the target branch sat at prep
            self.assertEqual(
                grp["target_sha"],
                git(proj, "rev-parse", grp["target_branch"]).stdout.strip())
            # a verified SEQUENTIAL step fast-forwards the baseline over
            # its own tip
            self.assertEqual(
                git(proj, "rev-parse", baseline).stdout.strip(),
                git(proj, "rev-parse", rec["branch"]).stdout.strip())
            # retained work is never debris/deleted: worktree list shows
            # the step worktree AND nothing merged into the checkout
            self.assertIn(rd.name, git(proj, "worktree", "list",
                                       "--porcelain").stdout)

    # ------------------------------------------------- sequential chain

    def test_sequential_steps_see_verified_predecessors(self):
        """s2 branches off the baseline AFTER s1 verified - its worktree
        contains s1's A.txt even though main never merged it."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(base, proj, [
                self._dstep(proj, "s1", "write A.txt"),
                # gate fails unless s1's A.txt is visible in s2's worktree
                self._dstep(proj, "s2", "append A.txt",
                            'verify = "if not exist A.txt exit 1"')])
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            for n in ("s1", "s2"):
                self.assertEqual(st["steps"][n]["status"], "done")
                self.assertTrue(st["steps"][n]["deferred_verified"])
                self.assertEqual(st["steps"][n]["merge_kind"],
                                 "sequential")
            self.assertFalse((proj / "A.txt").exists())   # still pending
            # baseline advanced over BOTH verified sequential steps
            grp = self._pending(st, proj)
            self.assertEqual(
                git(proj, "rev-parse", grp["baseline"]).stdout.strip(),
                git(proj, "rev-parse",
                    st["steps"]["s2"]["branch"]).stdout.strip())
            # the s2 worktree really saw s1's file (2 mock lines on A.txt)
            wt2 = Path(st["steps"]["s2"]["work_dir"])
            self.assertEqual(
                len((wt2 / "A.txt").read_text().splitlines()), 2)
            # explicit merge lands both onto the recorded target
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["s1", "s2"])
            self.assertEqual(len((proj / "A.txt")
                                 .read_text().splitlines()), 2)
            st, _ = state_of(runs)
            grp = self._pending(st, proj)
            self.assertEqual(grp["entries"], [])
            for n in ("s1", "s2"):
                self.assertTrue(st["steps"][n]["merged"])
                self.assertFalse(
                    Path(st["steps"][n]["work_dir"]).is_dir())

    def test_failed_step_blocks_dependents_baseline(self):
        """s1 fails verify -> baseline does not advance -> s2 (its own
        work aside) does NOT see s1's file, and stays unverified itself
        when it depends on it. s2's verify gate trips."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(base, proj, [
                self._dstep(proj, "s1", "write A.txt",
                            'verify = "exit 1"'),   # red gate
                self._dstep(proj, "s2", "write B.txt",
                            'verify = "if not exist A.txt exit 1"')],
                extra='on_failure = "continue"\n')
            # s2's verify cannot pass - s1's output never reached it
            # (s2 writes its own B.txt, only CHECKS for A.txt)
            run_manifest(m, runs)
            st, _ = state_of(runs)
            self.assertEqual(st["steps"]["s1"]["verify_ok"], False)
            self.assertFalse(st["steps"]["s1"]["deferred_verified"])
            self.assertEqual(st["steps"]["s2"]["verify_ok"], False)
            self.assertFalse(st["steps"]["s2"]["deferred_verified"])
            self.assertTrue((Path(st["steps"]["s2"]["work_dir"])
                             / "B.txt").is_file())     # its own work done
            # baseline never advanced over s1's unverified tip (sync may
            # still fold in target-branch movement - what it must NOT
            # contain is the failed step's work)
            grp = self._pending(st, proj)
            self.assertNotEqual(
                git(proj, "merge-base", "--is-ancestor",
                    st["steps"]["s1"]["branch"],
                    grp["baseline"]).returncode, 0)

    # ---------------------------------------------------- alternatives

    def test_wave_alternatives_stay_separate_and_selectable(self):
        """Two same-project wave members are 'alternative' kind: neither
        joins the baseline, merge_run can land ONE without the other."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(base, proj, [
                self._dstep(proj, "altA", "write A.txt", "wave = 1"),
                self._dstep(proj, "altB", "write B.txt", "wave = 1")])
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            for n in ("altA", "altB"):
                self.assertEqual(st["steps"][n]["merge_kind"],
                                 "alternative")
                self.assertTrue(st["steps"][n]["deferred_verified"])
            grp = self._pending(st, proj)
            self.assertEqual(
                git(proj, "rev-parse", grp["baseline"]).stdout.strip(),
                grp["target_sha"])          # baseline untouched by them
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK),
                                      only=["altB"])
            self.assertEqual(out["merged"], ["altB"])
            self.assertTrue((proj / "B.txt").is_file())
            self.assertFalse((proj / "A.txt").exists())     # altA held back
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["altB"]["merged"])
            self.assertIsNot(st["steps"]["altA"].get("merged"), True)
            self.assertTrue(
                Path(st["steps"]["altA"]["work_dir"]).is_dir())
            self.assertEqual(self._pending(st, proj)["entries"], ["altA"])

    # -------------------------------------------------------- retries

    def test_retry_resume_keeps_pending_identity(self):
        """A failed deferred step re-runs on resume in the SAME worktree on
        the SAME branch; the pending entry survives, then verifies and can
        be landed."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(
                base, proj, [self._dstep(proj, "flaky",
                                         "mockfail write A.txt")],
                extra='on_failure = "continue"\n')
            self.assertEqual(run_manifest(m, runs), 1)
            st, rd = state_of(runs)
            rec = st["steps"]["flaky"]
            self.assertEqual(rec["status"], "failed")
            self.assertTrue(rec["deferred"])
            self.assertFalse(rec["deferred_verified"])
            self.assertTrue(Path(rec["work_dir"]).is_dir())
            self.assertEqual(self._pending(st, proj)["entries"], ["flaky"])
            # resume with the mockfail removed -> same branch+worktree
            m2 = self._deferred_manifest(
                base, proj, [self._dstep(proj, "flaky", "write A.txt")],
                extra='on_failure = "continue"\n')
            self.assertEqual(run_manifest(m2, runs, resume=str(rd)), 0)
            st, _ = state_of(runs)
            rec2 = st["steps"]["flaky"]
            self.assertEqual(rec2["status"], "done")
            self.assertTrue(rec2["deferred_verified"])
            self.assertEqual(rec2["branch"], rec["branch"])
            self.assertEqual(rec2["work_dir"], rec["work_dir"])
            self.assertEqual(self._pending(st, proj)["entries"], ["flaky"])
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["flaky"])
            self.assertTrue((proj / "A.txt").is_file())

    # ------------------------------------------------ restart recovery

    def test_restart_preserves_pending_merges(self):
        """After the run ends, a fresh process (--resume or merge_run)
        sees the same pending state - nothing is recomputed or lost."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(base, proj, [
                self._dstep(proj, "one", "write A.txt"),
                self._dstep(proj, "two", "write B.txt")])
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            grp0 = self._pending(st, proj)
            # --resume on an all-done deferred run: no step re-runs, no
            # merge happens, pending entries untouched
            self.assertEqual(run_manifest(m, runs, resume=str(rd)), 0)
            st, _ = state_of(runs)
            grp = self._pending(st, proj)
            self.assertEqual(grp["entries"], ["one", "two"])
            self.assertEqual(grp["baseline"], grp0["baseline"])
            self.assertEqual(grp["target_branch"], grp0["target_branch"])
            self.assertEqual(grp["target_sha"], grp0["target_sha"])
            self.assertFalse((proj / "A.txt").exists())
            # merge_run still works purely from disk state
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(sorted(out["merged"]), ["one", "two"])

    def test_interrupted_stage_is_never_clobbered(self):
        """A stage left mid-merge (human resolving, or a crash) is kept:
        the next merge_run reports 'kept' and changes nothing."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(
                base, proj, [self._dstep(proj, "one", "write A.txt")])
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            grp = self._pending(st, proj)
            # fabricate the stage dir with a merge in progress
            thash = __import__("hashlib").sha1(
                f"{conductor._repo_key(proj)}|{grp['target_branch']}"
                .encode()).hexdigest()[:10]
            stage = runs.parent / "worktrees" / rd.name / f"stage-{thash}"
            stage.mkdir(parents=True)
            git(proj, "worktree", "add", "--detach", str(stage),
                grp["target_sha"])
            (stage / "WORK.txt").write_text("conflict\n", encoding="utf-8")
            git(stage, "add", "-A")
            git(stage, "commit", "-m", "stage side")
            git(proj, "checkout", "-b", "tmp-other")
            (proj / "WORK.txt").write_text("other\n", encoding="utf-8")
            git(proj, "add", "-A")
            git(proj, "commit", "-m", "other")
            git(proj, "checkout", grp["target_branch"])
            git(stage, "merge", "--no-edit", "tmp-other")  # conflicts
            self.assertTrue(conductor.merge_in_progress(stage))
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["kept"], ["one"])
            self.assertEqual(out["merged"], [])
            self.assertTrue(conductor.merge_in_progress(stage))
            # nothing landed; entry still pending
            st, _ = state_of(runs)
            self.assertEqual(self._pending(st, proj)["entries"], ["one"])
            git(stage, "merge", "--abort")                 # fixture clean
            git(proj, "branch", "-D", "tmp-other")

    # --------------------------------------------------- non-main target

    def test_non_main_recorded_target(self):
        """Project checked out on 'release' when the run starts: pending
        merges record it as their target and land on it, not on master."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            git(proj, "checkout", "-b", "release")
            runs = base / "runs"
            m = self._deferred_manifest(
                base, proj, [self._dstep(proj, "one", "write A.txt")])
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            grp = self._pending(st, proj)
            self.assertEqual(grp["target_branch"], "release")
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["one"])
            self.assertTrue((proj / "A.txt").is_file())
            self.assertEqual(git(proj, "rev-parse", "--abbrev-ref",
                                 "HEAD").stdout.strip(), "release")
            # master never received it
            self.assertEqual(
                git(proj, "rev-parse", "master").stdout.strip(),
                git(proj, "rev-list", "--max-parents=0",
                    "master").stdout.strip())

    def test_unborn_or_detached_target_fails_clean(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            git(proj, "checkout", "--detach", "HEAD")
            runs = base / "runs"
            m = self._deferred_manifest(
                base, proj, [self._dstep(proj, "one", "write A.txt")])
            self.assertEqual(run_manifest(m, runs), 1)
            st, _ = state_of(runs)
            self.assertEqual(st["steps"]["one"]["status"], "failed")
            self.assertIn("detached", st["steps"]["one"]["note"])

    # ------------------------------------------- failed verification

    def test_failed_verify_never_merges_its_output(self):
        """A done-but-verify-red step stays pending and its branch's work
        never reaches the target - while a verified sibling still lands."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(base, proj, [
                self._dstep(proj, "bad", "write BAD.txt",
                            'verify = "exit 1"'),
                self._dstep(proj, "good", "write G.txt",
                            'verify = "exit 0"')],
                extra='on_failure = "continue"\n')
            run_manifest(m, runs)
            st, rd = state_of(runs)
            self.assertFalse(st["steps"]["bad"]["deferred_verified"])
            self.assertTrue(st["steps"]["good"]["deferred_verified"])
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["unverified"], ["bad"])
            self.assertEqual(out["merged"], ["good"])
            self.assertTrue((proj / "G.txt").is_file())
            self.assertFalse((proj / "BAD.txt").exists())   # excluded
            # retained for inspection, entry still pending
            st, _ = state_of(runs)
            self.assertTrue(
                Path(st["steps"]["bad"]["work_dir"]).is_dir())
            self.assertTrue((Path(st["steps"]["bad"]["work_dir"])
                             / "BAD.txt").is_file())
            self.assertEqual(self._pending(st, proj)["entries"], ["bad"])

    def test_combined_verify_failure_lands_nothing(self):
        """Each branch verified alone, but the UNION fails a step's gate:
        s2's GATE.txt trips s1's verify in the stage -> nothing lands,
        stage kept, all entries stay pending."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(base, proj, [
                self._dstep(proj, "s1", "write A.txt",
                            'verify = "if exist GATE.txt exit 1"'),
                self._dstep(proj, "s2", "write GATE.txt")])
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            self.assertTrue(st["steps"]["s1"]["deferred_verified"])
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["verify_failed"], ["s1"])
            self.assertEqual(out["merged"], [])
            self.assertFalse((proj / "A.txt").exists())
            self.assertFalse((proj / "GATE.txt").exists())
            st, _ = state_of(runs)         # merge_run persisted the updates
            grp = self._pending(st, proj)
            self.assertEqual(grp["entries"], ["s1", "s2"])
            self.assertTrue(Path(grp["stage_kept"]).is_dir())
            # branches/worktrees retained for inspection
            for n in ("s1", "s2"):
                self.assertTrue(
                    Path(st["steps"][n]["work_dir"]).is_dir())

    # --------------------------------------------- target movement + lock

    def test_target_movement_between_run_and_merge(self):
        """Main gained human commits after the run: the stage is built at
        the NEW tip and the merge still lands cleanly."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(
                base, proj, [self._dstep(proj, "one", "write A.txt")])
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            (proj / "HUMAN.txt").write_text("human work\n",
                                           encoding="utf-8")
            git(proj, "add", "-A")
            git(proj, "commit", "-m", "human commit")
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["one"])
            self.assertTrue((proj / "A.txt").is_file())
            self.assertTrue((proj / "HUMAN.txt").is_file())

    def test_land_recheck_blocks_a_moved_target(self):
        """_land_staged's recheck: a stage built on a stale base can never
        fast-forward the moved target - refuses, nothing updates."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            tip = git(proj, "rev-parse", "HEAD").stdout.strip()
            git(proj, "checkout", "-b", "stage-tmp")
            (proj / "X.txt").write_text("x\n", encoding="utf-8")
            git(proj, "add", "-A")
            git(proj, "commit", "-m", "staged")
            staged = git(proj, "rev-parse", "HEAD").stdout.strip()
            git(proj, "checkout", "master")
            # move the target past the staged base -> stage is a divergent
            # side, not a descendant: --ff-only must refuse
            (proj / "Y.txt").write_text("y\n", encoding="utf-8")
            git(proj, "add", "-A")
            git(proj, "commit", "-m", "moved")
            landed, why = conductor._land_staged(
                proj, "master", staged, tip, base / "runs", "x")
            self.assertFalse(landed)
            self.assertTrue(why)
            self.assertFalse((proj / "X.txt").exists())
            # a stage built ON the moved tip fast-forwards cleanly
            git(proj, "checkout", "-b", "stage-ok")
            (proj / "Z.txt").write_text("z\n", encoding="utf-8")
            git(proj, "add", "-A")
            git(proj, "commit", "-m", "staged2")
            staged2 = git(proj, "rev-parse", "HEAD").stdout.strip()
            git(proj, "checkout", "master")
            cur = git(proj, "rev-parse", "HEAD").stdout.strip()
            landed, _ = conductor._land_staged(
                proj, "master", staged2, cur, base / "runs", "x")
            self.assertTrue(landed)
            self.assertTrue((proj / "Z.txt").is_file())

    def test_repo_merge_lock_serializes(self):
        """A live conductor-merge.lock on the repo makes merge_run report
        busy and touch nothing; a dead-pid lock is broken and merged."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(
                base, proj, [self._dstep(proj, "one", "write A.txt")])
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            lock = proj / ".git" / "conductor-merge.lock"
            lock.write_text(json.dumps({"pid": os.getpid(),
                                        "at": conductor.now_iso()}))
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["busy"], ["proj"])
            self.assertEqual(out["merged"], [])
            self.assertFalse((proj / "A.txt").exists())
            # stale lock (dead pid) is broken and the merge proceeds
            dead = subprocess.Popen(["cmd", "/c", "exit"])
            dead.wait()
            lock.write_text(json.dumps({"pid": dead.pid,
                                        "at": "2020-01-01T00:00:00"}))
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["one"])
            self.assertTrue((proj / "A.txt").is_file())

    # --------------------------------------------- deferred merge-resolve

    def test_resolver_lands_conflicting_alternative(self):
        """Alternatives that conflict in the stage honor merge_resolver:
        a resolve-* session finishes it there and both land."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = self._deferred_manifest(base, proj, [
                self._dstep(proj, "altA", "append to WORK.txt",
                            "wave = 1"),
                self._dstep(proj, "altB", "append to WORK.txt",
                            "wave = 1")],
                extra='merge_resolver = true\non_failure = "continue"\n')
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(sorted(out["merged"]), ["altA", "altB"])
            self.assertEqual(len((proj / "WORK.txt")
                                 .read_text().splitlines()), 2)
            st, _ = state_of(runs)
            self.assertEqual(st["steps"]["resolve-altB"]["status"],
                             "done")

    # ----------------------------------------- backward-compatible auto

    def test_auto_mode_unchanged_no_pending(self):
        """Default (no merge= key): worktree steps merge post-wave exactly
        as before, and no pending_merges state appears."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [
                ("a", "write A.txt"), ("b", "write B.txt")]))
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["a"]["merged"])
            self.assertTrue(st["steps"]["b"]["merged"])
            self.assertNotIn("pending_merges", st)
            self.assertFalse(st["steps"]["a"].get("deferred"))
            self.assertTrue((proj / "A.txt").is_file())

    def test_mixed_auto_and_deferred_same_run(self):
        """A run can mix policies: the auto step lands post-wave, the
        deferred one parks - and the deferred step's baseline absorbed
        the auto merge (it branched after it)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "auto"
project = "{proj.as_posix()}"
prompt = "write AUTO.txt"
worktree = true
merge = "auto"

[[step]]
name = "dfr"
project = "{proj.as_posix()}"
prompt = "write DFR.txt"
merge = "deferred"
verify = "if not exist AUTO.txt exit 1"
''')
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            self.assertTrue(st["steps"]["auto"]["merged"])
            self.assertNotIn("deferred", st["steps"]["auto"])
            self.assertTrue(st["steps"]["dfr"]["deferred_verified"])
            # baseline absorbed the auto-mode merge: dfr saw AUTO.txt
            self.assertTrue(st["steps"]["dfr"]["deferred"])
            self.assertFalse((proj / "DFR.txt").exists())
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["dfr"])
            self.assertTrue((proj / "DFR.txt").is_file())


def add_branch(proj: Path, name: str, commits: dict[str, str] | None = None):
    """Create <name> at HEAD with an extra commit, back on the default
    branch. Returns the default branch's name."""
    main = git(proj, "symbolic-ref", "--short", "HEAD").stdout.strip()
    git(proj, "checkout", "-b", name)
    for f, txt in (commits or {}).items():
        (proj / f).write_text(txt, encoding="utf-8")
    git(proj, "add", "-A")
    git(proj, "commit", "-m", f"on {name}")
    git(proj, "checkout", main)
    return main


@slow
class MergeTargetTests(unittest.TestCase):
    """merge_target: a worktree step's branch is created from and merged
    back into an explicitly chosen local branch, not blindly into whatever
    the checkout has out. A non-default target is merged inside a
    conductor-owned 'merge worktree' (<step>.merge) so the main checkout's
    HEAD never moves; the chosen target is recorded in state.json and
    drives retries, resume and the manual Merge button."""

    def test_merge_lands_on_target_not_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            main = add_branch(proj, "develop")
            runs = base / "runs"
            m = write_manifest(base, wt_steps(
                proj, [("w1", "write A.txt")],
                extra='merge_target = "develop"'))
            self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            rec = st["steps"]["w1"]
            self.assertTrue(rec["merged"])
            # the choice was persisted at materialization, not re-derived
            self.assertEqual(rec["merge_target"], "develop")
            # work landed on develop, and ONLY there
            self.assertEqual(git(proj, "cat-file", "-e",
                                 "develop:A.txt").returncode, 0)
            self.assertNotEqual(git(proj, "cat-file", "-e",
                                    f"{main}:A.txt").returncode, 0)
            # both the step worktree and the merge worktree are gone;
            # the checkout never left its branch and no merge is open
            self.assertFalse(Path(rec["work_dir"]).is_dir())
            self.assertFalse(conductor.merge_wt_for(
                Path(rec["work_dir"])).is_dir())
            self.assertFalse(conductor.merge_in_progress(proj))
            self.assertEqual(conductor.current_branch(proj), main)

    def test_worktree_baselines_off_target(self):
        """The branch is cut at refs/heads/<target>: commits the checkout
        has but the target lacks must NOT leak into the merge."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            main = add_branch(proj, "develop")
            # master gains a commit develop never saw - a branch cut from
            # HEAD would carry it into the develop merge.
            (proj / "MASTERONLY.txt").write_text("x", encoding="utf-8")
            git(proj, "add", "-A")
            git(proj, "commit", "-m", "master-side work")
            runs = base / "runs"
            m = write_manifest(base, wt_steps(
                proj, [("w1", "write A.txt")],
                extra='merge_target = "develop"'))
            self.assertEqual(run_manifest(m, runs), 0)
            # develop: has the step's file, does NOT have master's extra
            self.assertEqual(git(proj, "cat-file", "-e",
                                 "develop:A.txt").returncode, 0)
            self.assertNotEqual(git(proj, "cat-file", "-e",
                                    "develop:MASTERONLY.txt").returncode, 0)

    def test_manifest_requires_worktree_for_step_target(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            m = write_manifest(base, f'''
[[step]]
name = "a"
project = "{proj.as_posix()}"
prompt = "x"
merge_target = "develop"
''')
            with self.assertRaises(conductor.ManifestError) as e:
                conductor.load_manifest(m)
            self.assertIn("worktree", str(e.exception))

    def test_run_level_target_only_defaults_worktree_steps(self):
        """[run].merge_target is a fallback for worktree steps: a
        non-worktree step in the same run simply ignores it (there is
        nothing to merge) instead of erroring the manifest."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            add_branch(proj, "develop")
            m = write_manifest(base, f'''
[[step]]
name = "direct"
project = "{proj.as_posix()}"
prompt = "write D.txt"

[[step]]
name = "iso"
project = "{proj.as_posix()}"
prompt = "write E.txt"
worktree = true
''', extra='merge_target = "develop"')
            cfg = conductor.load_manifest(m)
            steps = {s.name: s for s in cfg.steps}
            self.assertIsNone(steps["direct"].merge_target)
            self.assertEqual(steps["iso"].merge_target, "develop")
            self.assertEqual(cfg.merge_target, "develop")
            runs = base / "runs"
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["iso"]["merged"])
            self.assertEqual(st["steps"]["iso"]["merge_target"], "develop")
            self.assertEqual(git(proj, "cat-file", "-e",
                                 "develop:E.txt").returncode, 0)

    def test_missing_target_fails_before_state_exists(self):
        """A nonexistent target is an actionable manifest error - never a
        fallback to main, never a created branch."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            m = write_manifest(base, wt_steps(
                proj, [("w1", "write A.txt")],
                extra='merge_target = "ghost"'))
            cfg = conductor.load_manifest(m)   # name/shape is valid TOML
            self.assertEqual(cfg.steps[0].merge_target, "ghost")
            for cmd in (lambda: conductor.cmd_plan(argparse.Namespace(
                            manifest=str(m), devin=MOCK)),
                        lambda: conductor.cmd_run(argparse.Namespace(
                            manifest=str(m), resume=None, devin=MOCK,
                            runs_dir=str(base / "runs"), hub=False,
                            keep_awake=False))):
                with self.assertRaises(conductor.ManifestError) as e:
                    cmd()
                msg = str(e.exception)
                self.assertIn("ghost", msg)
                self.assertIn("does not exist", msg)
                self.assertIn("will not fall back", msg)
            self.assertEqual(git(proj, "branch", "--list",
                                 "ghost").stdout.strip(), "")
            self.assertFalse((base / "runs").exists())

    def test_target_checked_out_elsewhere_is_refused(self):
        """A target sitting in a worktree the conductor doesn't own can't
        be merged into: actionable failure, parked work, no touching the
        foreign checkout."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            add_branch(proj, "develop")
            foreign = base / "elsewhere"
            git(proj, "worktree", "add", str(foreign), "develop")
            runs = base / "runs"
            m = write_manifest(base, wt_steps(
                proj, [("w1", "write A.txt")],
                extra='merge_target = "develop"'))
            self.assertEqual(run_manifest(m, runs), 1)
            st, _ = state_of(runs)
            rec = st["steps"]["w1"]
            self.assertEqual(rec["status"], "failed")
            self.assertFalse(rec["merged"])
            self.assertIn("checked out", rec.get("note", ""))
            # step worktree+branch survive for a human; the foreign
            # checkout's develop did not get the work
            self.assertTrue(Path(rec["work_dir"]).is_dir())
            self.assertFalse((foreign / "A.txt").is_file())

    def test_multi_project_distinct_targets(self):
        """A wave can merge each project into its own target - no global
        branch name is forced onto unrelated repos."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pa = make_project(base, "pa")
            pb = make_project(base, "pb")
            main_a = add_branch(pa, "dev-a")
            main_b = add_branch(pb, "dev-b")
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "a1"
project = "{pa.as_posix()}"
prompt = "write A.txt"
wave = 1
worktree = true
merge_target = "dev-a"

[[step]]
name = "b1"
project = "{pb.as_posix()}"
prompt = "write B.txt"
wave = 1
worktree = true
merge_target = "dev-b"
''')
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            self.assertEqual(st["steps"]["a1"]["merge_target"], "dev-a")
            self.assertEqual(st["steps"]["b1"]["merge_target"], "dev-b")
            self.assertEqual(git(pa, "cat-file", "-e",
                                 "dev-a:A.txt").returncode, 0)
            self.assertNotEqual(git(pa, "cat-file", "-e",
                                    f"{main_a}:A.txt").returncode, 0)
            self.assertEqual(git(pb, "cat-file", "-e",
                                 "dev-b:B.txt").returncode, 0)
            self.assertNotEqual(git(pb, "cat-file", "-e",
                                    f"{main_b}:B.txt").returncode, 0)

    def _conflict_parked_on_target(self, base: Path):
        """Two appends to WORK.txt targeting 'develop': p1 merges, p2
        conflicts and parks. Returns (proj, runs, manifest, run_dir)."""
        proj = make_project(base)
        add_branch(proj, "develop")
        runs = base / "runs"
        m = write_manifest(base, wt_steps(
            proj, [("p1", "append to WORK.txt"),
                   ("p2", "append to WORK.txt")],
            extra='merge_target = "develop"'),
            extra='on_failure = "continue"')
        self.assertEqual(run_manifest(m, runs), 1)
        st, rd = state_of(runs)
        p2 = st["steps"]["p2"]
        self.assertEqual(p2["status"], "failed")
        self.assertFalse(p2["merged"])
        self.assertEqual(p2["merge_target"], "develop")
        # the merge worktree was created and survives for the retry
        self.assertEqual(Path(p2["merge_dir"]),
                         conductor.merge_wt_for(Path(p2["work_dir"])))
        return proj, runs, m, rd

    def _resolve_parked_branch(self, proj: Path, p2: dict):
        """Make the parked branch mergeable: merge develop into it inside
        its kept worktree and union-resolve WORK.txt."""
        wt = Path(p2["work_dir"])
        git(wt, "merge", "develop")
        lines = (wt / "WORK.txt").read_text(encoding="utf-8").splitlines()
        resolved = [l for l in lines
                    if not l.startswith(("<<<<<<<", "=======", ">>>>>>>"))]
        (wt / "WORK.txt").write_text("\n".join(sorted(resolved)) + "\n",
                                   encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "--no-edit")

    def test_manual_merge_honors_recorded_target(self):
        """merge_run (the dashboard Merge button) merges the parked branch
        into its recorded target - develop - not the checkout's branch."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj, runs, _m, rd = self._conflict_parked_on_target(base)
            st, _ = state_of(runs)
            main = conductor.current_branch(proj)
            self._resolve_parked_branch(proj, st["steps"]["p2"])
            out = conductor.merge_run(rd.name, runs, shlex.split(MOCK))
            self.assertEqual(out["merged"], ["p2"])
            st, _ = state_of(runs)
            rec = st["steps"]["p2"]
            self.assertTrue(rec["merged"])
            lines = git(proj, "show", "develop:WORK.txt").stdout.splitlines()
            self.assertEqual(len(lines), 2)   # p1 + p2, union-resolved
            # checkout's own branch got nothing
            self.assertEqual(git(proj, "show",
                                 f"{main}:WORK.txt").stdout.strip(), "")
            # merge worktree cleaned up on success
            self.assertFalse(conductor.merge_wt_for(
                Path(rec["work_dir"])).is_dir())
            self.assertFalse(conductor.merge_in_progress(proj))

    def test_resume_honors_recorded_target(self):
        """--resume finalizes the parked branch against the recorded
        target the same way the manual merge does."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj, runs, m, rd = self._conflict_parked_on_target(base)
            st, _ = state_of(runs)
            main = conductor.current_branch(proj)
            self._resolve_parked_branch(proj, st["steps"]["p2"])
            self.assertEqual(run_manifest(m, runs, resume=str(rd)), 0)
            st, _ = state_of(runs)
            self.assertTrue(st["steps"]["p2"]["merged"])
            lines = git(proj, "show", "develop:WORK.txt").stdout.splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(git(proj, "show",
                                 f"{main}:WORK.txt").stdout.strip(), "")

    def test_resolver_session_runs_in_merge_worktree(self):
        """With merge_resolver the resolve-* session is spawned mid-merge
        inside the merge worktree on the target - the main checkout stays
        untouched the whole time."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            main = add_branch(proj, "develop")
            runs = base / "runs"
            m = write_manifest(base, wt_steps(
                proj, [("p1", "append to WORK.txt"),
                       ("p2", "append to WORK.txt")],
                extra='merge_target = "develop"'),
                extra='on_failure = "continue"\nmerge_resolver = true')
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            p2 = st["steps"]["p2"]
            self.assertTrue(p2["merged"])
            res = st["steps"]["resolve-p2"]
            self.assertEqual(res["status"], "done")
            # the resolver's cwd was the merge worktree on develop, not
            # the main checkout
            self.assertEqual(Path(res["work_dir"]),
                             conductor.merge_wt_for(Path(p2["work_dir"])))
            lines = git(proj, "show", "develop:WORK.txt").stdout.splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(git(proj, "show",
                                 f"{main}:WORK.txt").stdout.strip(), "")
            self.assertFalse(conductor.merge_in_progress(proj))
            # merge worktree removed once the merge landed
            self.assertFalse(Path(res["work_dir"]).is_dir())

    def test_backcompat_unset_target_records_checkout_branch(self):
        """No merge_target anywhere: same behavior as always - merge into
        the checkout's branch - but the effective choice is now recorded."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            main = conductor.current_branch(proj)
            runs = base / "runs"
            m = write_manifest(base, wt_steps(proj, [("w1", "write A.txt")]))
            self.assertEqual(run_manifest(m, runs), 0)
            st, _ = state_of(runs)
            rec = st["steps"]["w1"]
            self.assertTrue(rec["merged"])
            self.assertEqual(rec["merge_target"], main)
            self.assertEqual(git(proj, "cat-file", "-e",
                                 f"{main}:A.txt").returncode, 0)

    def test_branch_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            main = add_branch(proj, "develop")
            self.assertTrue(conductor.branch_name_ok("feature/x.y"))
            self.assertFalse(conductor.branch_name_ok("bad..name"))
            self.assertFalse(conductor.branch_name_ok("-sneaky"))
            self.assertFalse(conductor.branch_name_ok("has space"))
            self.assertTrue(conductor.local_branch_exists(proj, "develop"))
            self.assertTrue(conductor.local_branch_exists(proj, main))
            self.assertFalse(conductor.local_branch_exists(proj, "ghost"))
            names = conductor.list_local_branches(proj)
            self.assertIn("develop", names)
            self.assertIn(main, names)
            git(proj, "branch", "conductor/2026/x")
            self.assertNotIn("conductor/2026/x",
                             conductor.list_local_branches(proj))
            self.assertEqual(conductor.current_branch(proj), main)


if __name__ == "__main__":
    unittest.main()
