"""Tests for conductor.py. Convention: C:\\Projects\\TESTING.md

Everything here runs against tests/mock_devin.py - no auth, no credits.
"""
import argparse
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import conductor  # noqa: E402

MOCK = f"{Path(sys.executable).as_posix()} {(HERE / 'mock_devin.py').as_posix()}"


def git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True)


def make_project(base: Path, name="proj") -> Path:
    p = base / name
    p.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(p), capture_output=True)
    (p / ".gitignore").write_text(".mock_sessions.json\n", encoding="utf-8")
    (p / "WORK.txt").write_text("", encoding="utf-8")
    (p / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    git(p, "add", "-A")
    git(p, "commit", "-m", "init")
    return p


def write_manifest(base: Path, steps: str, extra: str = "") -> Path:
    m = base / "night.toml"
    m.write_text(f'[run]\nname = "t"\n{extra}\n{steps}', encoding="utf-8")
    return m


def run_manifest(manifest: Path, runs_dir: Path, resume=None) -> int:
    return conductor.cmd_run(argparse.Namespace(
        manifest=str(manifest), resume=resume, devin=MOCK,
        runs_dir=str(runs_dir), hub=False, keep_awake=False))


class ManifestTests(unittest.TestCase):
    def test_defaults_merge_and_prompt_xor(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            m = write_manifest(base, f'''
[defaults]
model = "swe"

[[step]]
name = "a"
project = "{proj.as_posix()}"
prompt = "do a"
''')
            cfg = conductor.load_manifest(m)
            self.assertEqual(cfg.name, "t")
            self.assertEqual(cfg.steps[0].model, "swe")
            self.assertEqual(cfg.steps[0].mode, "bypass")

    def test_prompt_and_file_together_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            m = write_manifest(base, '''
[[step]]
name = "a"
project = "x"
prompt = "p"
prompt_file = "f.md"
''')
            with self.assertRaises(conductor.ManifestError):
                conductor.load_manifest(m)

    def test_bad_mode_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            m = write_manifest(Path(td), '''
[[step]]
name = "a"
project = "x"
prompt = "p"
mode = "yeet"
''')
            with self.assertRaises(conductor.ManifestError):
                conductor.load_manifest(m)

    def test_effort_parses_from_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            m = write_manifest(Path(td), '''
[defaults]
model = "gpt-6-luna"
effort = "min"

[[step]]
name = "a"
project = "."
prompt = "x"

[[step]]
name = "b"
project = "."
prompt = "y"
effort = "high"
''')
            cfg = conductor.load_manifest(m)
            self.assertEqual(cfg.steps[0].effort, "min")
            self.assertEqual(cfg.steps[1].effort, "high")   # step overrides

    def test_duplicate_names_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            m = write_manifest(Path(td), '''
[[step]]
name = "a"
project = "x"
prompt = "1"
[[step]]
name = "a"
project = "x"
prompt = "2"
''')
            with self.assertRaises(conductor.ManifestError):
                conductor.load_manifest(m)


class ResolutionTests(unittest.TestCase):
    def test_slug_and_literal_path(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            projects = {"slug": {"path": proj, "cloud_ok": False}}
            step = conductor.Step(name="a", project="slug", prompt="p")
            self.assertEqual(conductor.resolve_project(step, projects, base),
                             (proj, "slug"))
            step2 = conductor.Step(name="b", project=str(proj), prompt="p")
            self.assertEqual(conductor.resolve_project(step2, projects, base)[0],
                             proj)

    def test_cloud_blocked_without_cloud_ok(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            projects = {"slug": {"path": proj, "cloud_ok": False}}
            step = conductor.Step(name="a", project="slug", prompt="p",
                                  cloud=True)
            with self.assertRaises(conductor.ManifestError):
                conductor.resolve_project(step, projects, base)

    def test_cloud_blocked_for_literal_path(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            step = conductor.Step(name="a", project=str(proj), prompt="p",
                                  cloud=True)
            with self.assertRaises(conductor.ManifestError):
                conductor.resolve_project(step, {}, base)


class CommandTests(unittest.TestCase):
    def test_devin_cmd_shape(self):
        step = conductor.Step(name="a", project="x", prompt="p",
                              model="swe", mode="bypass")
        cmd = conductor.devin_cmd(["devin"], step, Path("pf"), Path("ex"))
        self.assertIn("--print", cmd)
        self.assertIn("--prompt-file", cmd)
        self.assertNotIn("--cloud", cmd)
        i = cmd.index("--model")
        self.assertEqual(cmd[i + 1], "swe")
        # manifest alias -> canonical flag value
        i = cmd.index("--permission-mode")
        self.assertEqual(cmd[i + 1], "dangerous")

    def test_cloud_flag(self):
        step = conductor.Step(name="a", project="x", prompt="p", cloud=True)
        cmd = conductor.devin_cmd(["devin"], step, Path("pf"), Path("ex"))
        self.assertEqual(cmd[1], "--cloud")

    def test_effort_fuses_into_model(self):
        step = conductor.Step(name="a", project="x", prompt="p",
                              model="gpt-6-luna", effort="min")
        cmd = conductor.devin_cmd(["devin"], step, Path("pf"), Path("ex"))
        i = cmd.index("--model")
        self.assertEqual(cmd[i + 1], "gpt-6-luna-min")
        step2 = conductor.Step(name="a", project="x", prompt="p",
                               model="swe")
        cmd = conductor.devin_cmd(["devin"], step2, Path("pf"), Path("ex"))
        i = cmd.index("--model")
        self.assertEqual(cmd[i + 1], "swe")          # no effort, no suffix


class GitTests(unittest.TestCase):
    def test_nested_dir_is_not_a_repo_root(self):
        """A dir inside a parent repo but with no .git of its own must not be
        checkpointed - `git add -A` would sweep the parent's files."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)              # base is now a repo
            nested = proj / "inner"
            nested.mkdir()
            self.assertTrue(conductor.is_git_repo(proj))
            self.assertFalse(conductor.is_git_repo(nested))


class RunTests(unittest.TestCase):
    def test_two_step_run(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "first"

[[step]]
name = "two"
project = "{proj.as_posix()}"
prompt = "second"
verify = "python -c \\"import pathlib;assert len(pathlib.Path('WORK.txt').read_text().splitlines())==2\\""
''')
            rc = run_manifest(m, runs)
            self.assertEqual(rc, 0)
            lines = (proj / "WORK.txt").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(lines[0].startswith("mock-step-1-"))
            self.assertTrue(lines[1].startswith("mock-step-2-"))
            run_dir = next(runs.iterdir())
            state = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(state["steps"]["one"]["status"], "done")
            self.assertEqual(state["steps"]["one"]["task"], "first")
            self.assertEqual(state["steps"]["two"]["status"], "done")
            self.assertEqual(state["steps"]["two"]["verify_ok"], True)
            self.assertTrue(state["steps"]["two"]["session_id"]
                            .startswith("mock-"))
            log = git(proj, "log", "--oneline").stdout
            self.assertIn("conductor: checkpoint before one", log)

    def test_failure_stops_run(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "first"
verify = "exit 1"

[[step]]
name = "two"
project = "{proj.as_posix()}"
prompt = "second"
''')
            rc = run_manifest(m, runs)
            self.assertEqual(rc, 1)
            state = json.loads(
                (next(runs.iterdir()) / "state.json").read_text())
            self.assertEqual(state["steps"]["one"]["status"], "failed")
            self.assertNotIn("two", state["steps"])

    def test_parallel_wave_worktrees_merge(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "p1"
project = "{proj.as_posix()}"
prompt = "Append a line to FILE_A.txt."
wave = 1
worktree = true

[[step]]
name = "p2"
project = "{proj.as_posix()}"
prompt = "Append a line to FILE_B.txt."
wave = 1
worktree = true

[[step]]
name = "after"
project = "{proj.as_posix()}"
prompt = "third"
''')
            rc = run_manifest(m, runs)
            self.assertEqual(rc, 0)
            # each parallel branch lands its own file; the sequential step too
            self.assertTrue((proj / "FILE_A.txt").read_text().strip())
            self.assertTrue((proj / "FILE_B.txt").read_text().strip())
            self.assertEqual(len((proj / "WORK.txt").read_text().splitlines()), 1)
            branches = git(proj, "branch", "--list", "conductor/*").stdout
            self.assertEqual(branches.strip(), "")   # merged branches deleted
            state = json.loads(
                (next(runs.iterdir()) / "state.json").read_text())
            self.assertTrue(state["steps"]["p1"]["merged"])
            self.assertTrue(state["steps"]["p2"]["merged"])

    def test_wave_merge_conflict_keeps_worktree(self):
        """Two worktree steps editing the SAME file in parallel: the first
        merges, the second conflicts -> step failed, merge aborted, and the
        worktree+branch are PRESERVED for manual merge (work not lost)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "p1"
project = "{proj.as_posix()}"
prompt = "Append a line to WORK.txt"
wave = 1
worktree = true

[[step]]
name = "p2"
project = "{proj.as_posix()}"
prompt = "Also append a line to WORK.txt"
wave = 1
worktree = true
''')
            rc = run_manifest(m, runs)
            self.assertEqual(rc, 1)
            run_dir = next(runs.iterdir())
            state = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(state["steps"]["p1"]["status"], "done")
            self.assertTrue(state["steps"]["p1"]["merged"])
            p2 = state["steps"]["p2"]
            self.assertEqual(p2["status"], "failed")
            self.assertFalse(p2["merged"])
            self.assertIn("merge conflict", p2["note"])
            wt = Path(p2["work_dir"])               # kept for manual merge
            self.assertTrue(wt.is_dir())
            # aborted merge: main checkout has only p1's line, not p2's
            lines = (proj / "WORK.txt").read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertTrue((proj / ".git" / "MERGE_HEAD").exists() is False)

    def test_resume_reruns_lost_step(self):
        """A step orphaned when its conductor died (status 'lost') re-runs
        on --resume, like any non-done step."""
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
            run_manifest(m, runs)
            run_dir = next(runs.iterdir())
            state = json.loads((run_dir / "state.json").read_text())
            state["steps"]["one"]["status"] = "lost"
            state["ended_at"] = None
            (run_dir / "state.json").write_text(json.dumps(state))
            rc = run_manifest(m, runs, resume=str(run_dir))
            self.assertEqual(rc, 0)
            state = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(state["steps"]["one"]["status"], "done")

    def test_resume_late_merges_unmerged_worktree(self):
        """Conductor dies between worker-exit and merge-back: step is
        'done' but its branch never merged. --resume must finish the
        merge instead of skipping the step and stranding the work."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            run_dir = runs / "20260101-000000-t"
            run_dir.mkdir(parents=True)
            branch = "conductor/20260101-000000-t/wtstep"
            # stranded work: a branch commit the main checkout lacks
            main = git(proj, "rev-parse", "--abbrev-ref",
                       "HEAD").stdout.strip()
            git(proj, "checkout", "-b", branch)
            (proj / "LATE.txt").write_text("worker output\n")
            git(proj, "add", "-A")
            git(proj, "commit", "-m", "wt work")
            git(proj, "checkout", main)
            state = {"run": "t", "run_dir": str(run_dir),
                     "started_at": "2026-01-01T00:00:00", "steps": {
                        "wtstep": {"name": "wtstep", "slug": "proj",
                                   "status": "done",
                                   "branch": branch}}}
            (run_dir / "state.json").write_text(json.dumps(state))
            m = write_manifest(base, f'''
[[step]]
name = "wtstep"
project = "{proj.as_posix()}"
prompt = "already done"
worktree = true
''')
            rc = run_manifest(m, runs, resume=str(run_dir))
            self.assertEqual(rc, 0)
            self.assertTrue((proj / "LATE.txt").is_file())  # merged late
            state = json.loads((run_dir / "state.json").read_text())
            self.assertTrue(state["steps"]["wtstep"]["merged"])

    def test_auth_lost_midrun_blocks_remaining(self):
        """Token dying mid-run: the NEXT group's pre-spawn probe fails ->
        remaining steps marked blocked, run aborts cleanly, --resume can
        pick them up."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            flag = base / "authdead"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "do one killauth"

[[step]]
name = "two"
project = "{proj.as_posix()}"
prompt = "do two"
''')
            os.environ["MOCK_AUTH_FILE"] = str(flag)
            try:
                rc = run_manifest(m, runs)
            finally:
                os.environ.pop("MOCK_AUTH_FILE", None)
            self.assertEqual(rc, 2)
            state = json.loads(
                (next(runs.iterdir()) / "state.json").read_text())
            self.assertEqual(state["steps"]["one"]["status"], "done")
            self.assertEqual(state["steps"]["two"]["status"], "blocked")
            self.assertEqual(state.get("aborted"), "auth_midrun")

    def test_wave_finish_order_keeps_manifest_merge_order(self):
        """p2 finishing FIRST must not change merge-back order: merges are
        sequential in manifest order regardless of worker speed."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "slow"
project = "{proj.as_posix()}"
prompt = "sleep=2 write SLOW.txt"
wave = 1
worktree = true

[[step]]
name = "fast"
project = "{proj.as_posix()}"
prompt = "write FAST.txt"
wave = 1
worktree = true
''')
            rc = run_manifest(m, runs)
            self.assertEqual(rc, 0)
            # slow ends after fast but still merged first - the first merge
            # fast-forwards (no merge commit), so read order from reflog:
            # newest entry first, so 'fast' must appear BEFORE 'slow'.
            # Branch names carry the run dir: conductor/<ts>-t/<step>.
            run_name = next(runs.iterdir()).name
            reflog = git(proj, "reflog", "--format=%gs").stdout
            self.assertLess(reflog.index(f"conductor/{run_name}/fast"),
                            reflog.index(f"conductor/{run_name}/slow"))

    def test_middle_failure_continue_runs_rest(self):
        """on_failure=continue: a failed middle step must not starve the
        steps after it."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "write A.txt"

[[step]]
name = "bad"
project = "{proj.as_posix()}"
prompt = "write B.txt"
verify = "exit 1"

[[step]]
name = "three"
project = "{proj.as_posix()}"
prompt = "write C.txt"
''', extra='on_failure = "continue"')
            rc = run_manifest(m, runs)
            self.assertEqual(rc, 1)   # run reports failure overall
            state = json.loads(
                (next(runs.iterdir()) / "state.json").read_text())
            self.assertEqual(state["steps"]["one"]["status"], "done")
            self.assertEqual(state["steps"]["bad"]["status"], "failed")
            self.assertEqual(state["steps"]["bad"]["verify_ok"], False)
            self.assertEqual(state["steps"]["three"]["status"], "done")

    def test_verify_soft_fail_still_done(self):
        """verify_must_pass=false: a failing verify records verify_ok=False
        but the step still counts as done (advisory gate)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "first"
verify = "exit 1"
verify_must_pass = false
''')
            self.assertEqual(run_manifest(m, runs), 0)
            state = json.loads(
                (next(runs.iterdir()) / "state.json").read_text())
            self.assertEqual(state["steps"]["one"]["status"], "done")
            self.assertEqual(state["steps"]["one"]["verify_ok"], False)

    def test_two_projects_same_wave_parallel_no_worktree(self):
        """Different project dirs in one wave run in the dirs themselves -
        no worktree needed, nothing to merge."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            p1 = make_project(base, "projA")
            p2 = make_project(base, "projB")
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "a"
project = "{p1.as_posix()}"
prompt = "write A.txt"
wave = 1

[[step]]
name = "b"
project = "{p2.as_posix()}"
prompt = "write B.txt"
wave = 1
''')
            self.assertEqual(run_manifest(m, runs), 0)
            self.assertTrue((p1 / "A.txt").read_text().strip())
            self.assertTrue((p2 / "B.txt").read_text().strip())

    def test_failed_worktree_step_keeps_unmerged_work(self):
        """A failed step whose branch DID get commits keeps its worktree
        (partial work is preserved, merge aborted)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "write A.txt"
worktree = true
verify = "exit 1"
''')
            self.assertEqual(run_manifest(m, runs), 1)
            run_dir = next(runs.iterdir())
            rec = json.loads((run_dir / "state.json").read_text())["steps"]["one"]
            self.assertEqual(rec["status"], "failed")
            self.assertTrue(Path(rec["work_dir"]).is_dir())
            self.assertIn("unmerged work", rec["note"])
            self.assertTrue(git(proj, "branch", "--list",
                                "conductor/*").stdout.strip())

    def test_dead_spawn_worktree_removed_as_debris(self):
        """A step that fails before spawning (bad devin cmd) leaves a
        worktree+branch with zero commits -> cleaned as debris."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "first"
worktree = true
''')
            rc = conductor.cmd_run(argparse.Namespace(
                manifest=str(m), resume=None, devin="nonexistent-xyz-abc",
                runs_dir=str(runs), hub=False, keep_awake=False))
            self.assertEqual(rc, 1)
            run_dir = next(runs.iterdir())
            rec = json.loads((run_dir / "state.json").read_text())["steps"]["one"]
            self.assertEqual(rec["status"], "failed")
            self.assertIn("no work product", rec["note"])
            self.assertEqual(git(proj, "worktree", "list",
                                 "--porcelain").stdout.count("worktree"), 1)
            self.assertEqual(git(proj, "branch", "--list",
                                 "conductor/*").stdout.strip(), "")

    def test_wave_collision_without_worktree_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            steps = [conductor.Step(name="a", project=str(proj), prompt="1",
                                    wave=1),
                     conductor.Step(name="b", project=str(proj), prompt="2",
                                    wave=1)]
            resolved = [(proj, "p", s) for s in steps]
            with self.assertRaises(conductor.ManifestError):
                conductor.validate_groups(conductor.group_waves(resolved))

    def test_resume_skips_done(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "first"
verify = "exit 1"

[[step]]
name = "two"
project = "{proj.as_posix()}"
prompt = "second"
''')
            run_manifest(m, runs)
            run_dir = next(runs.iterdir())
            state = json.loads((run_dir / "state.json").read_text())
            state["steps"]["one"]["status"] = "done"   # pretend fixed
            (run_dir / "state.json").write_text(json.dumps(state))
            lines_before = len((proj / "WORK.txt").read_text().splitlines())
            rc = run_manifest(m, runs, resume=str(run_dir))
            self.assertEqual(rc, 0)
            lines = (proj / "WORK.txt").read_text().splitlines()
            self.assertEqual(len(lines), lines_before + 1)  # only "two" re-ran


class ResourceTests(unittest.TestCase):
    def test_default_max_parallel_bounds(self):
        self.assertGreaterEqual(conductor.default_max_parallel(), 1)
        self.assertLessEqual(conductor.default_max_parallel(), 4)

    def test_max_parallel_parse_and_override(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            m = write_manifest(base, f'''
[[step]]
name = "a"
project = "{proj.as_posix()}"
prompt = "p"
''', extra='max_parallel = 3\n')
            cfg = conductor.load_manifest(m)
            self.assertEqual(cfg.max_parallel, 3)
            self.assertEqual(conductor.effective_max_parallel(cfg), 3)
            self.assertEqual(conductor.effective_max_parallel(cfg, 7), 7)
            with self.assertRaises(conductor.ManifestError):
                conductor.effective_max_parallel(cfg, 0)

    def test_max_parallel_zero_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            m = write_manifest(Path(td), '''
[[step]]
name = "a"
project = "x"
prompt = "p"
''', extra='max_parallel = 0\n')
            with self.assertRaises(conductor.ManifestError):
                conductor.load_manifest(m)

    def test_max_parallel_one_serializes_wave(self):
        """cap=1 must run wave members strictly one at a time: the events
        log shows each step fully ending before the next starts."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "p1"
project = "{proj.as_posix()}"
prompt = "Append a line to FILE_A.txt."
wave = 1
worktree = true

[[step]]
name = "p2"
project = "{proj.as_posix()}"
prompt = "Append a line to FILE_B.txt."
wave = 1
worktree = true
''', extra='max_parallel = 1\n')
            rc = run_manifest(m, runs)
            self.assertEqual(rc, 0)
            run_dir = next(runs.iterdir())
            state = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(state["max_parallel"], 1)
            seq = [(e["event"], e.get("step")) for e in
                   (json.loads(l) for l in
                    (run_dir / "events.jsonl").read_text().splitlines())]
            order = [(ev, st) for ev, st in seq
                     if ev in ("step_start", "step_end")]
            # strict serial: start,end pairs never interleave
            self.assertEqual(order[0][0], "step_start")
            self.assertEqual(order[1], ("step_end", order[0][1]))
            self.assertEqual(order[2][0], "step_start")
            self.assertEqual(order[3], ("step_end", order[2][1]))


class SpawnGuardTests(unittest.TestCase):
    """Regression for 2026-10-13: real `devin -p` workers died ~1-3s in with
    a clean exit and no STATUS. Cause: conductor's `save_state` between
    Popen() and wait() wrote state.json into run_dir - inside a self-hosted
    worker's watched cwd - and the CLI's user-edit awareness ended the
    fresh session. Invariant: nothing under run_dir may be written while a
    worker process is alive WHEN run_dir sits inside the worker's watched
    cwd. When run_dir is outside it, the write is invisible to the worker
    and the pid IS persisted mid-run (dashboard liveness + sweep need it).
    """

    def _fixture(self, base: Path, self_hosted: bool = False,
                 step_extra: str = ""):
        proj = make_project(base)
        run_dir = (proj if self_hosted else base) / "runs" / "r1"
        (run_dir / "prompts").mkdir(parents=True)
        cfg = conductor.load_manifest(write_manifest(base, f'''
[[step]]
name = "a"
project = "{proj.as_posix()}"
prompt = "p"
{step_extra}
'''))
        state = {"run": "t", "run_dir": str(run_dir), "steps": {}}
        return proj, run_dir, cfg, state

    def _pid_seen_on_disk(self, self_hosted: bool):
        """Run one fake step; returns the pid value state.json held while
        the 'worker' was inside wait() (None when never persisted)."""
        with tempfile.TemporaryDirectory() as td:
            proj, run_dir, cfg, state = self._fixture(
                Path(td), self_hosted=self_hosted)
            seen = {}

            class FakeProc:
                pid = 4242

                def __init__(self, *a, **kw):
                    self._out = kw["stdout"]

                def wait(self, timeout=None):
                    disk = json.loads(
                        (run_dir / "state.json").read_text())
                    seen["pid"] = disk["steps"]["a"].get("pid")
                    self._out.write(b"STATUS: done\n")
                    self._out.flush()
                    return 0

                def kill(self):
                    pass

            with mock.patch.object(conductor.subprocess, "Popen", FakeProc), \
                    mock.patch.object(conductor, "session_ids",
                                      lambda *a, **k: set()), \
                    mock.patch.object(conductor, "is_git_repo",
                                      lambda p: True), \
                    mock.patch.object(conductor, "git_commit",
                                      lambda *a, **k: True), \
                    mock.patch.object(conductor, "git_dirty",
                                      lambda p: False):
                st = conductor.run_step(cfg.steps[0], "s", proj, cfg,
                                        run_dir, ["devin"], state)
            self.assertEqual(st, "done")
            self.assertEqual(state["steps"]["a"]["pid"], 4242)
            return seen["pid"]

    def test_pid_not_persisted_while_worker_alive(self):
        """Self-hosted run: run_dir is inside the watched cwd, so the pid
        must NOT hit disk while the worker is alive."""
        self.assertIsNone(self._pid_seen_on_disk(self_hosted=True))

    def test_pid_persisted_while_worker_alive(self):
        """Cross-project run: run_dir is outside the watched cwd, so the
        pid IS persisted while the worker is alive."""
        self.assertEqual(self._pid_seen_on_disk(self_hosted=False), 4242)

    class _NeverExits:
        """FakeProc whose wait() always times out until kill_tree lands
        (on Windows kill_tree goes through taskkill - which fails on a
        fake pid - then falls back to proc.kill())."""
        pid = 999_999_999          # deliberately impossible: taskkill must
                                   # fail cleanly, never hit a real process

        def __init__(self, *a, **kw):
            self._out = kw["stdout"]
            self.killed = False

        def wait(self, timeout=None):
            if self.killed:
                return 0
            raise subprocess.TimeoutExpired("devin", timeout)

        def poll(self):
            return 0 if self.killed else None

        def kill(self):
            self.killed = True

    def _run_step_with(self, proc_cls, **kw):
        with tempfile.TemporaryDirectory() as td:
            proj, run_dir, cfg, state = self._fixture(Path(td), **kw)
            with mock.patch.object(conductor.subprocess, "Popen", proc_cls), \
                    mock.patch.object(conductor, "kill_tree",
                                      lambda p: p.kill()), \
                    mock.patch.object(conductor, "session_ids",
                                      lambda *a, **k: set()), \
                    mock.patch.object(conductor, "is_git_repo",
                                      lambda p: True), \
                    mock.patch.object(conductor, "git_commit",
                                      lambda *a, **k: True), \
                    mock.patch.object(conductor, "git_dirty",
                                      lambda p: False), \
                    mock.patch.object(conductor, "STATUS_GRACE_S", 0.05), \
                    mock.patch.object(conductor, "WAIT_POLL_S", 0.01):
                st = conductor.run_step(cfg.steps[0], "s", proj, cfg,
                                        run_dir, ["devin"], state)
            return st, state["steps"]["a"]

    def test_status_then_hang_kills_after_grace(self):
        """STATUS printed then the CLI lingers on a spawned child: grace-
        kill counts it as a completed turn (trap 12), never a timeout."""

        class Proc(self._NeverExits):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._out.write(b"STATUS: done\nNEXT: n\n")
                self._out.flush()

        st, rec = self._run_step_with(Proc)
        self.assertEqual(st, "done")
        self.assertIn("tree-killed after grace", rec["note"])

    def test_no_status_hang_still_times_out(self):
        """A worker that never prints STATUS is a real timeout no matter
        how long the process lingers."""
        st, rec = self._run_step_with(
            self._NeverExits, step_extra="timeout_min = 0.001")
        self.assertEqual(st, "timed_out")

    def test_no_status_line_is_failed(self):
        """rc 0 without a STATUS line is a no-op worker, never 'done'."""
        with tempfile.TemporaryDirectory() as td:
            proj, run_dir, cfg, state = self._fixture(Path(td))
            silent = [sys.executable, "-c", "pass"]
            with mock.patch.object(conductor, "session_ids",
                                   lambda *a, **k: set()):
                st = conductor.run_step(cfg.steps[0], "s", proj, cfg,
                                        run_dir, silent, state)
            self.assertEqual(st, "failed")
            self.assertIn("STATUS", state["steps"]["a"]["note"])

    def test_timeout_retries_in_fresh_session(self):
        """max_attempts>1: a timed-out step is committed then respawned in a
        fresh session whose prompt carries the continue-from-state note -
        the step can still end done."""
        calls = {"n": 0}
        rendered = []
        commits = []

        class Flaky(self._NeverExits):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                calls["n"] += 1
                if calls["n"] > 1:
                    self._out.write(b"STATUS: done\nNEXT: n\n")
                    self._out.flush()

            def wait(self, timeout=None):
                if calls["n"] > 1:
                    return 0
                return super().wait(timeout)

        with tempfile.TemporaryDirectory() as td:
            proj, run_dir, cfg, state = self._fixture(
                Path(td), step_extra="timeout_min = 0.001\n"
                                     "max_attempts = 2")
            orig_render = conductor.render_prompt

            def capture(*a, **kw):
                text = orig_render(*a, **kw)
                rendered.append(text)
                return text

            with mock.patch.object(conductor.subprocess, "Popen", Flaky), \
                    mock.patch.object(conductor, "kill_tree",
                                      lambda p: p.kill()), \
                    mock.patch.object(conductor, "session_ids",
                                      lambda *a, **k: set()), \
                    mock.patch.object(conductor, "is_git_repo",
                                      lambda p: True), \
                    mock.patch.object(conductor, "git_commit",
                                      lambda *a, **k:
                                      commits.append(a[1]) or True), \
                    mock.patch.object(conductor, "git_dirty",
                                      lambda p: True), \
                    mock.patch.object(conductor, "STATUS_GRACE_S", 0.05), \
                    mock.patch.object(conductor, "WAIT_POLL_S", 0.01), \
                    mock.patch.object(conductor, "render_prompt", capture):
                st = conductor.run_step(cfg.steps[0], "s", proj, cfg,
                                        run_dir, ["devin"], state)
            rec = state["steps"]["a"]
            self.assertEqual(st, "done")
            self.assertEqual(calls["n"], 2)
            self.assertEqual(rec["attempts"], 2)
            self.assertIn("attempt2", rec["prompt"])
            self.assertIn("retried after timeout", rec["note"])
            self.assertIn("CONTINUE from where it stopped", rendered[-1])
            self.assertEqual(
                sum("work from" in m for m in commits), 2)


class DashboardTests(unittest.TestCase):
    def test_collect_dashboard(self):
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
            rc = run_manifest(m, runs)
            self.assertEqual(rc, 0)
            d = conductor.collect_dashboard(runs)
            self.assertEqual(len(d["runs"]), 1)
            self.assertEqual(d["runs"][0]["run"], "t")
            self.assertTrue(d["runs"][0]["ended_at"])
            self.assertEqual(len(d["agents"]), 1)
            a = d["agents"][0]
            self.assertEqual(a["step"], "one")
            self.assertEqual(a["status"], "done")
            self.assertTrue(a["ended_at"])
            self.assertIsNotNone(a["elapsed_s"])
            self.assertIn("pid", a)
            self.assertFalse(a["alive"])   # mock child is long dead
            self.assertTrue(str(a["work_dir"]).endswith("proj"))
            self.assertIn("auth", d)       # live auth probe for the banner

    def test_dashboard_one_run_row_per_state(self):
        """Regression: runs.append must run once per state.json. A stray
        indent once nested it in the agents loop -> N copies of one run."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            for name in ("ra", "rb"):
                m = write_manifest(base, f'''
[defaults]
project = "{proj.as_posix()}"

[[step]]
name = "s1"
prompt = "x"

[[step]]
name = "s2"
prompt = "y"
''')
                # rename the manifest run so the two runs are distinct
                txt = m.read_text().replace('name = "t"', f'name = "{name}"')
                m.write_text(txt)
                run_manifest(m, runs)
            d = conductor.collect_dashboard(runs)
            self.assertEqual(len(d["runs"]), 2)
            self.assertEqual({r["run"] for r in d["runs"]}, {"ra", "rb"})
            self.assertEqual(len({r["dir"] for r in d["runs"]}), 2)
            self.assertEqual(len(d["agents"]), 4)

    def test_agent_row_carries_task_and_effort(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "  tidy   the  catalog "
effort = "min"
''')
            run_manifest(m, runs)
            d = conductor.collect_dashboard(runs)
            a = d["agents"][0]
            self.assertEqual(a["task"], "tidy the catalog")
            self.assertEqual(a["effort"], "min")

    def test_task_label_falls_back_to_prompt_file(self):
        """Old records have no `task` field - recover it from the rendered
        prompt between the '## Task' / '## Before you finish' markers."""
        with tempfile.TemporaryDirectory() as td:
            pf = Path(td) / "p.md"
            pf.write_text("preamble\n\n## Task\n\nrewrite the parser  "
                          "carefully\n\n## Before you finish\n\n- stuff\n",
                          encoding="utf-8")
            self.assertEqual(
                conductor.task_label({"prompt": str(pf)}),
                "rewrite the parser carefully")
            self.assertIsNone(conductor.task_label({}))
            self.assertIsNone(conductor.task_label(
                {"prompt": str(Path(td) / "gone.md")}))


    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_run_selection_facets_and_token_details(self):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,textContent:''}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,querySelectorAll:()=>[],
  addEventListener:(event,handler)=>{handlers[event]=handler;}};
const ctx=vm.createContext({document,location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'2026-10-14T00:00:00',agents:[
  {step:'alpha',run:'first',run_dir:'/first',slug:'p',status:'done',wt:true,merged:true,
   task:'implement one',tokens:{prompt:10000,cached:6000,completion:1000,llm_steps:2}},
  {step:'beta',run:'second',run_dir:'/second',slug:'p',status:'done',wt:false,
   task:'implement two'}],runs:[
  {run:'first',dir:'/first',description:'A proposal',projects:['p'],counts:{done:1}},
  {run:'second',dir:'/second',description:'A separate run',projects:['p'],counts:{done:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
assert.match(elem('filters').innerHTML,/1 wt/);
assert.match(elem('filters').innerHTML,/1 merged/);
assert.match(elem('agents').innerHTML,/>tokens<\/th>/);
assert.doesNotMatch(elem('agents').innerHTML,/tokens\?/);
const click=async(target)=>handlers.click({target,preventDefault:()=>{}});
const row={dataset:{runKey:'/first'}};
(async()=>{
  await click({dataset:{},closest:selector=>selector==='tr.runrow'?row:null});
  assert.match(elem('filters').innerHTML,/Clear run filter/);
  assert.match(elem('filters').innerHTML,/first ×/);
  assert.match(elem('runs').innerHTML,/runrow selected/);
  assert.doesNotMatch(elem('runs').innerHTML,/A separate run/);
  assert.match(elem('agents').innerHTML,/implement one/);
  assert.doesNotMatch(elem('agents').innerHTML,/implement two/);
  await click({dataset:{facet:'merged'}});
  assert.match(elem('filters').innerHTML,/merged ×/);
  await click({dataset:{clearRun:''}});
  assert.match(elem('runs').innerHTML,/A separate run/);
  assert.doesNotMatch(elem('agents').innerHTML,/implement two/);
  await click({dataset:{facet:'merged'}});
  assert.match(elem('agents').innerHTML,/implement two/);
  vm.runInContext('showDetail(0)',ctx);
  assert.match(elem('dbody').innerHTML,/Explain tokens and cost/);
  assert.match(elem('dbody').innerHTML,/\$0\.019/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_draft_and_review_modal_never_start_implementation(self):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,querySelectorAll:()=>[],
  addEventListener:()=>{}};
const id='20261014-100000-a1b2c3d4', seen=[];
const req={id,status:'ready',run:'catalog-cleanup',label:'Tidy catalog',raw:'Please tidy',
 project:'p',model:'swe-2',effort:'max',manifest:'manifests/a.toml',
 manifest_text:'[run]\nname = "catalog-cleanup"',proposed_steps:[1,2,3,4].map(i=>({
   step:'phase-'+i,slug:'p',model:'swe-2',effort:'max',task:'Do phase '+i}))};
const fetch=async path=>{seen.push(path);return {ok:true,json:async()=>req};};
const ctx=vm.createContext({document,location:{reload:()=>{}},setTimeout:()=>{},fetch});
vm.runInContext(src,ctx);
const d={serve_boot:'b',generated_at:'2026-10-14',agents:req.proposed_steps.map(s=>({
  ...s,run:'catalog-cleanup',run_key:'request:'+id,status:'planned'})),runs:[
  {run:'catalog-cleanup',key:'request:'+id,request_id:id,stage:'ready',counts:{planned:4}},
  {run:'New draft',key:'request:other',request_id:'other',stage:'draft',counts:{}}],
  project_options:[{slug:'p',cloud_ok:false}],defaults:{model:'swe-2',effort:'max'}};
vm.runInContext('render('+JSON.stringify(d)+')',ctx);
assert.match(elem('runs').innerHTML,/Ready to run/);
assert.match(elem('runs').innerHTML,/Draft · unsent/);
assert.match(elem('agents').innerHTML,/phase-4/);
(async()=>{
  await vm.runInContext('openRequest('+JSON.stringify(id)+')',ctx);
  assert.match(elem('dbody').innerHTML,/Review proposed run/);
  assert.match(elem('dbody').innerHTML,/Proposed steps \(4\)/);
  assert.match(elem('dbody').innerHTML,/Edit proposed TOML/);
  assert.match(elem('dbody').innerHTML,/data-action="run"/);
  assert.deepEqual(seen,['/api/requests/'+id]);
  vm.runInContext('openNew()',ctx);
  assert.match(elem('dbody').innerHTML,/Save draft/);
  assert.match(elem('dbody').innerHTML,/Generate proposal/);
  assert.doesNotMatch(elem('dbody').innerHTML,/data-action="run"/);
  assert.deepEqual(seen,['/api/requests/'+id]);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class RequestWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.proj = make_project(self.base)
        self.runs = self.base / "runs"
        self.requests = self.base / "requests"
        self.manifests = self.base / "manifests"
        self.manifests.mkdir()
        patch = mock.patch.object(conductor, "load_projects", return_value={
            "proj": {"path": self.proj, "cloud_ok": False}})
        patch.start()
        self.addCleanup(patch.stop)

    def proposal(self, description="Tidy the catalog", extra_steps=""):
        return f'''[run]
name = "catalog-cleanup"
description = "{description}"
on_failure = "continue"
checkpoint = true

[defaults]
project = "proj"
model = "swe-2"
effort = "max"
mode = "bypass"
max_attempts = 3

[[step]]
name = "organize"
prompt = "Organize the catalog and verify results."
{extra_steps}'''

    def new(self):
        return conductor.create_request(
            {"raw": "Please tidy the catalog carefully", "project": "proj",
             "model": "swe-2", "effort": "max"}, self.requests, self.runs)

    def ready(self):
        rec = self.new()
        return conductor.save_request(
            rec["id"], {"manifest": self.proposal()},
            self.requests, self.manifests, self.runs)

    def test_defaults_change_after_october_16(self):
        self.assertEqual(conductor.default_model_effort(
            conductor.dt.date(2026, 10, 16)), ("swe-2", "max"))
        self.assertEqual(conductor.default_model_effort(
            conductor.dt.date(2026, 10, 17)), ("gpt-6-luna", "max"))

    def test_draft_has_no_manifest_or_agent_and_ready_has_planned_steps(self):
        rec = self.new()
        self.assertEqual(rec["status"], "draft")
        self.assertFalse(list(self.manifests.iterdir()))
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual(d["runs"][0]["stage"], "draft")
        self.assertEqual(d["agents"], [])
        rec = conductor.save_request(
            rec["id"], {"manifest": self.proposal(extra_steps='''
[[step]]
name = "test"
prompt = "Run the relevant checks."
''')}, self.requests, self.manifests, self.runs)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["description"], "Tidy the catalog")
        self.assertTrue(Path(rec["manifest"]).name.startswith(rec["id"]))
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual(len(d["runs"]), 1)
        self.assertEqual(d["runs"][0]["counts"], {"planned": 2})
        self.assertEqual({a["status"] for a in d["agents"]}, {"planned"})
        self.assertEqual({a["step"] for a in d["agents"]}, {"organize", "test"})
        self.assertTrue(all(a["started_at"] is None and a["tokens"] is None
                            and a["session_id"] is None for a in d["agents"]))

    def test_raw_edits_require_regeneration_or_reviewed_manifest(self):
        rec = self.ready()
        conductor.save_request(rec["id"], {"raw": "Change the scope"},
                               self.requests, self.manifests, self.runs)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertTrue(rec["stale"])
        with self.assertRaises(conductor.RequestError):
            conductor.launch_request(rec["id"], self.requests,
                                     self.manifests, self.runs, ["devin"])
        updated = self.proposal("Tidy the catalog and add tests")
        rec = conductor.save_request(rec["id"], {"manifest": updated},
                                     self.requests, self.manifests, self.runs)
        self.assertFalse(rec["stale"])
        self.assertEqual(rec["description"], "Tidy the catalog and add tests")
        self.assertEqual(Path(rec["manifest"]).read_text(encoding="utf-8"),
                         updated)

    def test_interrupted_start_can_be_edited_and_discarded(self):
        rec = self.ready()
        rec.update(status="launching", launched_at="2020-01-01T00:00:00")
        conductor._save_request(rec, self.requests)
        self.assertEqual(conductor.request_details(
            rec["id"], self.requests, self.manifests, self.runs)["status"],
            "launch_failed")
        edited = conductor.save_request(rec["id"], {"raw": "Try another task"},
                                       self.requests, self.manifests, self.runs)
        self.assertEqual(edited["status"], "launch_failed")
        self.assertTrue(edited["stale"])
        conductor.discard_request(rec["id"], self.requests,
                                  self.manifests, self.runs)
        self.assertFalse(Path(rec["manifest"]).exists())

    def test_refuses_to_write_into_live_workers_workspace(self):
        run_dir = self.runs / "20261014-000000-live"
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(json.dumps({"steps": {
            "live": {"status": "running", "started_at": conductor.now_iso(),
                     "timeout_min": 60, "work_dir": str(self.base)}}}))
        with self.assertRaises(conductor.RequestError) as error:
            self.new()
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(self.requests.exists())

    def test_resume_run_respawns_manifest_against_same_dir(self):
        manifest = self.manifests / "m.toml"
        manifest.write_text(self.proposal(), encoding="utf-8")
        run_dir = self.runs / "20261014-000000-x"
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(json.dumps({
            "run": "x", "manifest": str(manifest), "ended_at": "t",
            "steps": {"s": {"status": "timed_out"}}}))
        with mock.patch.object(conductor, "_launch_process",
                               return_value=4321) as launch:
            out = conductor.resume_run(run_dir.name, self.runs, ["devin"])
        self.assertEqual(out["resumed"], run_dir.name)
        cmd = launch.call_args[0][0]
        self.assertEqual(cmd[cmd.index("run") + 1], str(manifest))
        self.assertEqual(cmd[cmd.index("--resume") + 1], str(run_dir))

    def test_resume_run_refuses_a_live_step(self):
        manifest = self.manifests / "m.toml"
        manifest.write_text(self.proposal(), encoding="utf-8")
        run_dir = self.runs / "20261014-000000-live"
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(json.dumps({
            "manifest": str(manifest),
            "steps": {"s": {"status": "running",
                            "started_at": conductor.now_iso(),
                            "timeout_min": 60, "work_dir": str(self.base)}}}))
        with self.assertRaises(conductor.RequestError) as error:
            conductor.resume_run(run_dir.name, self.runs, ["devin"])
        self.assertEqual(error.exception.status, 409)

    def test_generator_changes_only_a_proposal_until_explicit_launch(self):
        rec = self.new()
        with mock.patch.object(conductor, "_planner_response",
                               return_value=self.proposal()) as planner, \
                mock.patch.object(conductor, "_launch_process") as launcher:
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        self.assertEqual(planner.call_count, 1)
        launcher.assert_not_called()
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")
        self.assertFalse(list(self.runs.glob("*/state.json")))
        first = rec["manifest"]
        with mock.patch.object(conductor, "_planner_response",
                               return_value=self.proposal(extra_steps='''
[[step]]
name = "audit"
prompt = "Review the catalog tests."
''')):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["manifest"], first)
        self.assertEqual(len(rec["proposed_steps"]), 2)

    def test_failed_regeneration_keeps_old_manifest(self):
        rec = self.ready()
        original = Path(rec["manifest"]).read_text(encoding="utf-8")
        with mock.patch.object(conductor, "_planner_response",
                               side_effect=ValueError("unavailable")):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "error")
        self.assertIn("unavailable", rec["error"])
        self.assertEqual(Path(rec["manifest"]).read_text(encoding="utf-8"),
                         original)

    def test_planner_is_answer_only_and_keeps_output_outside_workspace(self):
        rec = self.new()
        seen = {}
        proposal = self.proposal()

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                seen["cmd"] = cmd
                seen["cwd"] = kwargs["cwd"]
                seen["prompt"] = Path(cmd[cmd.index("--prompt-file") + 1])
                seen["log"] = Path(kwargs["stdout"].name)
                seen["flags"] = kwargs.get("creationflags", 0)
                kwargs["stdout"].write(("BEGIN_MANIFEST\n" + proposal +
                                        "END_MANIFEST\nSTATUS: done\n").encode())
                kwargs["stdout"].flush()

        with mock.patch.object(conductor.subprocess, "Popen", FakeProc), \
                mock.patch.object(conductor, "wait_worker",
                                  return_value=(0, False, False)):
            text = conductor._planner_response(rec, {}, ["devin"])
        self.assertEqual(text.strip(), proposal.strip())
        self.assertIn("--model", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("--model") + 1],
                         "swe-2-max")
        self.assertEqual(seen["cmd"][seen["cmd"].index("--permission-mode") + 1],
                         "normal")
        self.assertEqual(seen["prompt"].parent, Path(seen["cwd"]))
        self.assertNotEqual(seen["log"].parent, Path(seen["cwd"]))
        if os.name == "nt":
            self.assertEqual(seen["flags"], subprocess.CREATE_NO_WINDOW)

    def test_planner_must_honor_selected_model_and_effort(self):
        rec = self.new()
        wrong = self.proposal().replace('model = "swe-2"',
                                        'model = "gpt-6-luna"')
        with mock.patch.object(conductor, "_planner_response",
                               return_value=wrong):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "error")
        self.assertIn("selected model", rec["error"])
        self.assertFalse(list(self.manifests.iterdir()))

    def test_unsafe_and_invalid_manifests_cannot_become_ready(self):
        rec = self.new()
        bad = (self.proposal().replace('project = "proj"',
                                       'project = "../../secret"'),
               self.proposal().replace('checkpoint = true',
                                       'checkpoint = false'),
               self.proposal().replace('effort = "max"', 'effort = "min"'),
               self.proposal().replace(
                   'prompt = "Organize the catalog and verify results."',
                   'prompt_file = "../outside.md"'),
               self.proposal().replace('name = "organize"',
                                       'name = "../../other"'),
               self.proposal().replace('project = "proj"',
                                       'project = "proj"\ncloud = true'))
        for text in bad:
            with self.subTest(text=text[:50]), self.assertRaises(
                    conductor.RequestError):
                conductor.save_request(rec["id"], {"manifest": text},
                                       self.requests, self.manifests,
                                       self.runs)
        self.assertFalse(list(self.manifests.iterdir()))

    def test_discard_removes_only_owned_proposal_not_launched_runs(self):
        rec = self.ready()
        manifest = Path(rec["manifest"])
        conductor.discard_request(rec["id"], self.requests,
                                  self.manifests, self.runs)
        self.assertFalse(manifest.exists())
        self.assertEqual(conductor.collect_dashboard(
            self.runs, ["no-such-devin"], self.requests)["runs"], [])
        with self.assertRaises(conductor.RequestError):
            conductor.launch_request(rec["id"], self.requests,
                                     self.manifests, self.runs, ["devin"])

    def test_started_run_keeps_all_proposed_steps_visible(self):
        rec = self.new()
        rec = conductor.save_request(rec["id"], {"manifest": self.proposal(
            extra_steps='''
[[step]]
name = "check"
prompt = "Check the catalog."

[[step]]
name = "polish"
prompt = "Polish the catalog."

[[step]]
name = "report"
prompt = "Report the outcome."
''')}, self.requests, self.manifests, self.runs)
        rd = self.runs / "20261014-000000-catalog-cleanup"
        rd.mkdir(parents=True)
        state = {"run": rec["run"], "request_id": rec["id"],
                 "run_dir": str(rd), "description": rec["description"],
                 "started_at": conductor.now_iso(), "steps": {
                     "organize": {"name": "organize", "slug": "proj",
                                  "status": "running",
                                  "started_at": conductor.now_iso(),
                                  "work_dir": str(self.proj)}}}
        (rd / "state.json").write_text(json.dumps(state))
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual(len(d["runs"]), 1)
        self.assertEqual(d["runs"][0]["counts"],
                         {"running": 1, "waiting": 3})
        self.assertEqual(len(d["agents"]), 4)
        pending = [a for a in d["agents"] if a["status"] == "waiting"]
        self.assertEqual({a["step"] for a in pending},
                         {"check", "polish", "report"})
        self.assertTrue(all(a["tokens"] is None and a["started_at"] is None
                            for a in pending))
        state["ended_at"] = conductor.now_iso()
        state["steps"]["organize"]["status"] = "failed"
        (rd / "state.json").write_text(json.dumps(state))
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual(d["runs"][0]["counts"],
                         {"failed": 1, "skipped": 3})
        state["steps"]["check"] = {"name": "check", "status": "blocked"}
        (rd / "state.json").write_text(json.dumps(state))
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        check = next(a for a in d["agents"] if a["step"] == "check")
        self.assertEqual(check["slug"], "proj")
        self.assertEqual(check["task"], "Check the catalog.")
        self.assertEqual(len(d["agents"]), 4)

    def test_launch_explicitly_passes_request_id_and_real_run_replaces_plan(self):
        rec = self.ready()
        with mock.patch.object(conductor, "_launch_process", return_value=42) as start:
            conductor.launch_request(rec["id"], self.requests,
                                     self.manifests, self.runs, ["devin"])
        self.assertIn("--request-id", start.call_args.args[0])
        self.assertFalse(list(self.runs.glob("*/state.json")))
        with self.assertRaises(conductor.RequestError):
            conductor.launch_request(rec["id"], self.requests,
                                     self.manifests, self.runs, ["devin"])
        args = argparse.Namespace(manifest=rec["manifest"], resume=None,
                                  devin=MOCK, runs_dir=str(self.runs),
                                  request_id=rec["id"], hub=False,
                                  keep_awake=False)
        self.assertEqual(conductor.cmd_run(args), 0)
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual(len(d["runs"]), 1)
        self.assertEqual(d["runs"][0]["description"], rec["description"])
        self.assertEqual(d["agents"][0]["status"], "done")
        self.assertEqual(d["agents"][0]["run_key"],
                         f"request:{rec['id']}")
        with self.assertRaises(conductor.RequestError):
            conductor.discard_request(rec["id"], self.requests,
                                      self.manifests, self.runs)

    def test_http_draft_review_discard_and_same_origin(self):
        handler = conductor.make_dashboard_handler(
            self.runs, ["no-such-devin"], self.requests, self.manifests)
        server = conductor.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port

        def call(method, path, data=None, origin=None, ctype="application/json",
                 host=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Content-Type": ctype,
                       "Origin": origin or f"http://127.0.0.1:{port}"}
            if host:
                headers["Host"] = host
            conn.request(method, path,
                         json.dumps(data).encode("utf-8") if data is not None
                         else None, headers)
            res = conn.getresponse()
            payload = json.loads(res.read().decode("utf-8"))
            conn.close()
            return res.status, payload

        fields = {"raw": "Clean the catalog", "project": "proj",
                  "model": "swe-2", "effort": "max"}
        status, rec = call("POST", "/api/requests", fields)
        self.assertEqual(status, 201)
        rid = rec["id"]
        self.assertEqual(call("GET", "/api/state")[1]["runs"][0]["stage"],
                         "draft")
        status, _ = call("POST", "/api/requests", fields,
                         origin="http://malicious.invalid")
        self.assertEqual(status, 403)
        status, _ = call("POST", "/api/requests", fields,
                         ctype="text/plain")
        self.assertEqual(status, 415)
        status, _ = call("GET", "/api/state", host=f"evil.invalid:{port}")
        self.assertEqual(status, 403)
        status, rec = call("POST", f"/api/requests/{rid}/save",
                           {"manifest": self.proposal()})
        self.assertEqual(status, 200)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(call("GET", f"/api/requests/{rid}")[1][
            "manifest_text"], self.proposal())
        status, _ = call("POST", f"/api/requests/{rid}/discard", {})
        self.assertEqual(status, 400)
        status, _ = call("POST", f"/api/requests/{rid}/discard",
                         {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(call("GET", "/api/state")[1]["runs"], [])
        with mock.patch.object(conductor.subprocess, "Popen") as popen:
            status, _ = call("POST", "/api/auth/login", {})
        self.assertEqual(status, 200)
        self.assertTrue(popen.call_args.kwargs["shell"])
        self.assertNotIn("creationflags", popen.call_args.kwargs)


class BackgroundProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows console flags")
    def test_periodic_dashboard_probes_are_headless(self):
        prior = list(conductor._AUTH_CACHE)
        self.addCleanup(lambda: conductor._AUTH_CACHE.__setitem__(slice(None),
                                                                   prior))
        conductor._AUTH_CACHE[0] = 0
        with mock.patch.object(conductor.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["devin"], 0, "[]", "")
            conductor.auth_status(["devin"])
            self.assertEqual(run.call_args.kwargs["creationflags"],
                             subprocess.CREATE_NO_WINDOW)
            conductor.session_activity(["devin"], Path.cwd())
            self.assertEqual(run.call_args.kwargs["creationflags"],
                             subprocess.CREATE_NO_WINDOW)
            conductor._git(Path.cwd(), "status")
            self.assertEqual(run.call_args.kwargs["creationflags"],
                             subprocess.CREATE_NO_WINDOW)


class PidAliveTests(unittest.TestCase):
    """2026-10-13: os.kill(pid, 0) is TerminateProcess on Windows - the old
    implementation KILLED every worker the dashboard probed. The probe must
    leave the process running."""

    def test_probe_does_not_kill(self):
        import time as _t
        p = subprocess.Popen([sys.executable, "-c",
                              "import time;time.sleep(30)"])
        try:
            self.assertTrue(conductor.pid_alive(p.pid))
            self.assertIsNone(p.poll())   # probe must not have killed it
        finally:
            p.terminate()
            p.wait()
        for _ in range(50):
            if not conductor.pid_alive(p.pid):
                break
            _t.sleep(0.1)
        self.assertFalse(conductor.pid_alive(p.pid))
        self.assertFalse(conductor.pid_alive(None))

    @unittest.skipUnless(os.name == "nt", "creation-time check is win32")
    def test_pid_reuse_guard(self):
        p = subprocess.Popen([sys.executable, "-c",
                              "import time;time.sleep(30)"])
        try:
            # a live pid whose creation predates the claimed start is a
            # recycled pid, not our worker
            self.assertFalse(
                conductor.pid_alive(p.pid, "2999-01-01T00:00:00"))
            self.assertIsNone(p.poll())
            self.assertTrue(conductor.pid_alive(
                p.pid, conductor.now_iso()))
        finally:
            p.terminate()
            p.wait()


class LivenessTests(unittest.TestCase):
    def _state(self, steps, **kw):
        s = {"run": "t", "started_at": conductor.now_iso(), "steps": steps}
        s.update(kw)
        return s

    def test_fresh_no_pid_running_is_live(self):
        rec = {"status": "running", "started_at": conductor.now_iso(),
               "timeout_min": 60}
        self.assertTrue(conductor.step_live(rec))

    def test_stale_no_pid_running_is_lost(self):
        old = (conductor.dt.datetime.now()
               - conductor.dt.timedelta(hours=3)).isoformat(timespec="seconds")
        st = self._state({"a": {"status": "running", "started_at": old,
                                "timeout_min": 60}})
        self.assertFalse(conductor.step_live(st["steps"]["a"]))
        self.assertEqual(conductor.effective_statuses(st)["a"], "lost")

    def test_dead_pid_running_is_lost(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        st = self._state({"a": {"status": "running",
                                "started_at": conductor.now_iso(),
                                "pid": p.pid}})
        self.assertEqual(conductor.effective_statuses(st)["a"], "lost")

    def test_queued_orphaned_when_run_over(self):
        st = self._state(
            {"a": {"status": "done", "started_at": conductor.now_iso(),
                   "ended_at": conductor.now_iso()},
             "b": {"status": "queued"}},
            ended_at=conductor.now_iso())
        self.assertEqual(conductor.effective_statuses(st)["b"], "lost")

    def test_queued_survives_while_sibling_live(self):
        st = self._state(
            {"a": {"status": "running", "started_at": conductor.now_iso(),
                   "timeout_min": 60},
             "b": {"status": "queued"}})
        self.assertEqual(conductor.effective_statuses(st)["b"], "queued")


class SweepTests(unittest.TestCase):
    def test_sweep_marks_stale_and_closes_run(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            rd = runs / "20990101-000000-t"
            rd.mkdir(parents=True)
            old = (conductor.dt.datetime.now()
                   - conductor.dt.timedelta(hours=5)).isoformat(
                       timespec="seconds")
            state = {"run": "t", "started_at": old, "steps": {
                "a": {"status": "running", "started_at": old,
                      "timeout_min": 60},
                "b": {"status": "queued"},
                "c": {"status": "done", "started_at": old,
                      "ended_at": old}}}
            (rd / "state.json").write_text(json.dumps(state))
            lines = conductor.sweep_runs(runs)
            self.assertTrue(any("a" in l and "lost" in l for l in lines))
            out = json.loads((rd / "state.json").read_text())
            self.assertEqual(out["steps"]["a"]["status"], "lost")
            self.assertEqual(out["steps"]["b"]["status"], "lost")
            self.assertEqual(out["steps"]["c"]["status"], "done")
            self.assertTrue(out["ended_at"])

    def test_sweep_dry_run_when_live_self_run_watches(self):
        """A live step whose work_dir contains runs_dir means a self-hosted
        worker is in flight - writing would trip trap 5. Report only."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "data" / "runs"
            runs.mkdir(parents=True)
            # dead run that WOULD be swept
            rd = runs / "20990101-000000-old"
            rd.mkdir()
            old = (conductor.dt.datetime.now()
                   - conductor.dt.timedelta(hours=5)).isoformat(
                       timespec="seconds")
            (rd / "state.json").write_text(json.dumps(
                {"run": "old", "started_at": old, "ended_at": old,
                 "steps": {"a": {"status": "running", "started_at": old,
                                 "timeout_min": 60}}}))
            # live self-hosted run: work_dir = base, contains runs_dir
            live = runs / "20990102-000000-self"
            live.mkdir()
            p = subprocess.Popen([sys.executable, "-c",
                                  "import time;time.sleep(30)"])
            try:
                (live / "state.json").write_text(json.dumps(
                    {"run": "self", "started_at": conductor.now_iso(),
                     "steps": {"x": {"status": "running", "pid": p.pid,
                                     "work_dir": str(base),
                                     "started_at": conductor.now_iso()}}}))
                lines = conductor.sweep_runs(runs)
                self.assertTrue(any(l.startswith("DRY-RUN") for l in lines))
                disk = json.loads((rd / "state.json").read_text())
                self.assertEqual(disk["steps"]["a"]["status"],
                                 "running")   # untouched
            finally:
                p.terminate()
                p.wait()


class StatusCommandTests(unittest.TestCase):
    def test_minimal_state_does_not_crash(self):
        """regression: a hand-made state.json (run+steps only) crashed
        cmd_status with KeyError 'started_at' (data/runs/repro3)."""
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td) / "repro"
            rd.mkdir()
            (rd / "state.json").write_text(json.dumps(
                {"run": "repro3", "steps": {}}))
            self.assertEqual(conductor.cmd_status(argparse.Namespace(
                run_dir=str(rd))), 0)


class SessionsCommandTests(unittest.TestCase):
    def _args(self, runs, rm=False, keep_hours=48):
        return argparse.Namespace(runs_dir=str(runs), devin=MOCK, rm=rm,
                                  keep_hours=keep_hours)

    def test_lists_and_removes_only_spawned_sessions(self):
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
            reg = json.loads((proj / ".mock_sessions.json").read_text())
            self.assertEqual(len(reg), 1)
            # recent session: kept (inside keep_hours)
            self.assertEqual(
                conductor.cmd_sessions(self._args(runs)), 0)
            self.assertEqual(
                len(json.loads((proj / ".mock_sessions.json").read_text())), 1)
            # keep_hours=0 -> eligible -> --rm deletes via devin rm --force
            self.assertEqual(
                conductor.cmd_sessions(self._args(runs, rm=True,
                                                  keep_hours=0)), 0)
            self.assertEqual(
                json.loads((proj / ".mock_sessions.json").read_text()), [])

    def test_session_of_unfinished_run_never_eligible(self):
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
            run_dir = next(runs.iterdir())
            state = json.loads((run_dir / "state.json").read_text())
            del state["ended_at"]          # pretend run still in flight
            (run_dir / "state.json").write_text(json.dumps(state))
            self.assertEqual(
                conductor.cmd_sessions(self._args(runs, rm=True,
                                                  keep_hours=0)), 0)
            reg = json.loads((proj / ".mock_sessions.json").read_text())
            self.assertEqual(len(reg), 1)   # still there

    def test_sweep_sessions_removes_eligible(self):
        """The automated path (sweep / serve startup) rms sessions of runs
        ended >keep_hours ago, whitelist-only, no --rm flag needed."""
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
            self.assertEqual(
                len(json.loads((proj / ".mock_sessions.json").read_text())), 1)
            devin = __import__("shlex").split(MOCK)
            # inside the grace window -> kept
            self.assertEqual(
                conductor.sweep_sessions(runs, devin, keep_hours=48), [])
            self.assertEqual(
                len(json.loads((proj / ".mock_sessions.json").read_text())), 1)
            # keep_hours=0 -> eligible -> removed automatically
            lines = conductor.sweep_sessions(runs, devin, keep_hours=0)
            self.assertEqual(len(lines), 1)
            self.assertIn("ok", lines[0])
            self.assertEqual(
                json.loads((proj / ".mock_sessions.json").read_text()), [])


if __name__ == "__main__":
    unittest.main()
