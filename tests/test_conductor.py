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
import time
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


class ProjectsRegistryTests(unittest.TestCase):
    def test_projects_local_toml_merges_over_base(self):
        """A worktree checkout breaks '../other-project' relatives - an
        untracked projects.local.toml re-points slugs without editing the
        tracked registry."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real = base / "real-proj"
            real.mkdir()
            pf = base / "projects.toml"
            pf.write_text(
                '[project.demo]\npath = "../somewhere-else"\n'
                'cloud_ok = false\n', encoding="utf-8")
            local = base / "projects.local.toml"
            with mock.patch.object(conductor, "PROJECTS_FILE", pf):
                # base file alone resolves relative to its own dir
                got = conductor.load_projects()
                self.assertEqual(got["demo"]["path"],
                                 (base / "../somewhere-else").resolve())
                local.write_text(
                    f'[project.demo]\npath = "{real.as_posix()}"\n'
                    'cloud_ok = true\n', encoding="utf-8")
                got = conductor.load_projects()
                self.assertEqual(got["demo"]["path"], real.resolve())
                self.assertTrue(got["demo"]["cloud_ok"])


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

    def test_verify_inherited_from_projects_toml(self):
        """A step on a registered slug inherits its project's verify gate
        (the planner cannot know it - it never inspects the project).
        Step-level verify wins; verify = "" opts out; cloud steps never
        inherit (a local verify proves nothing about the VM's run)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            reg = base / "projects.toml"
            reg.write_text('[project.demo]\npath = "."\n'
                           'verify = "python run_tests.py"\n',
                           encoding="utf-8")
            m = write_manifest(base, '''
[[step]]
name = "a"
project = "demo"
prompt = "x"

[[step]]
name = "b"
project = "demo"
prompt = "y"
verify = "echo ok"

[[step]]
name = "c"
project = "demo"
prompt = "z"
verify = ""

[[step]]
name = "d"
project = "demo"
prompt = "w"
cloud = true
''')
            old = conductor.PROJECTS_FILE
            conductor.PROJECTS_FILE = reg
            try:
                cfg = conductor.load_manifest(m)
            finally:
                conductor.PROJECTS_FILE = old
            steps = {s.name: s for s in cfg.steps}
            self.assertEqual(steps["a"].verify, "python run_tests.py")
            self.assertEqual(steps["b"].verify, "echo ok")
            self.assertEqual(steps["c"].verify, "")      # explicit opt-out
            self.assertIsNone(steps["d"].verify)         # cloud never inherits


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

    def test_no_status_with_tokens_is_not_called_noop(self):
        """rc 0 + no STATUS but a nonzero-token export: real work ran, so
        the note must not claim 'nothing was done'."""
        with tempfile.TemporaryDirectory() as td:
            proj, run_dir, cfg, state = self._fixture(Path(td))
            fake = [sys.executable, "-c",
                    "import sys,json;"
                    "p=sys.argv[sys.argv.index('--export')+1];"
                    "json.dump({'final_metrics':{'total_prompt_tokens':9,"
                    "'total_completion_tokens':4}},open(p,'w'))"]
            with mock.patch.object(conductor, "session_ids",
                                   lambda *a, **k: set()):
                st = conductor.run_step(cfg.steps[0], "s", proj, cfg,
                                        run_dir, fake, state)
            self.assertEqual(st, "failed")
            note = state["steps"]["a"]["note"]
            self.assertIn("real work ran", note)
            self.assertNotIn("no-op", note)

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
    def test_model_options_parse(self):
        sample = (
            "Available models (2 families)\n\n"
            "SWE-2 (swe-2)\n"
            "  aliases: swe\n"
            "  swe-2-high          SWE-2 High  [262K context]\n"
            "  swe-2-max           SWE-2 Max\n\n"
            "SWE-1.7 Lightning (swe-1.7-lightning)\n"
            "  swe-1-7-lightning          Lightning Max\n"
            "  swe-1-7-lightning-medium   Lightning Medium\n\n"
            "Claude Opus 5.5 (claude-opus-5.5)\n"
            "  claude-opus-5-5-high       Opus High\n"
            "  claude-opus-5-5-low        Opus Low\n"
            "  claude-opus-5-5-low-fast   Opus Low Fast\n"
            "  claude-opus-5-5-max        Opus Max\n"
            "  claude-opus-5-5-medium     Opus Medium\n"
            "  claude-opus-5-5-xhigh      Opus XHigh\n")
        fake = subprocess.CompletedProcess([], 0, stdout=sample, stderr="")
        with mock.patch.object(conductor.subprocess, "run",
                               return_value=fake), \
                mock.patch.object(conductor, "_MODELS_CACHE", [0.0, {}]):
            opts = conductor.model_options(["devin"])
        self.assertEqual(opts["swe-2"], ["high", "max"])
        self.assertEqual(opts["swe-1-7-lightning"], ["", "medium"])
        # sorted by effort level, not alphabetically; 'low-fast' with 'low'
        self.assertEqual(opts["claude-opus-5-5"],
                         ["low", "low-fast", "medium", "high",
                          "xhigh", "max"])

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
  // the 'merged' tag chip still filters both tables: 'second' has no
  // merged step, so its run row hides too.
  assert.doesNotMatch(elem('runs').innerHTML,/A separate run/);
  assert.doesNotMatch(elem('agents').innerHTML,/implement two/);
  await click({dataset:{facet:'merged'}});   // toggles the tag back off
  assert.match(elem('runs').innerHTML,/A separate run/);
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
                                  return_value=(0, False, False, False, 0.0)):
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

    def test_planner_tokens_recorded_on_request(self):
        rec = self.new()
        proposal = self.proposal()

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                exp = Path(cmd[cmd.index("--export") + 1])
                exp.write_text(json.dumps({"final_metrics": {
                    "total_prompt_tokens": 1000,
                    "total_completion_tokens": 50,
                    "total_cached_tokens": 100,
                    "total_steps": 4}}))
                kwargs["stdout"].write(("BEGIN_MANIFEST\n" + proposal +
                                        "END_MANIFEST\nSTATUS: done\n"
                                        ).encode())
                kwargs["stdout"].flush()

        real_popen = subprocess.Popen

        def popen_router(cmd, **kwargs):
            # Only the planner spawn is faked; git/probe subprocesses run
            # for real (validate_proposal checks git roots).
            if isinstance(cmd, list) and "--prompt-file" in cmd \
                    and "--export" in cmd:
                return FakeProc(cmd, **kwargs)
            return real_popen(cmd, **kwargs)

        with mock.patch.object(conductor.subprocess, "Popen",
                               popen_router), \
                mock.patch.object(conductor, "wait_worker",
                                  return_value=(0, False, False, False, 0.0)):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["plan_tokens"],
                         {"prompt": 1000, "completion": 50,
                          "cached": 100, "llm_steps": 4})

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


class PricingTests(unittest.TestCase):
    TABLE_SNIPPET = (
        "<script>const modelCostData = [\n"
        ' {"model_uid":"swe-2-max","label":"SWE-2 Max","tier":"TEAMS_TIER_PRO",'
        '"input_cost_per_million_usd":0,"output_cost_per_million_usd":0,'
        '"cache_read_cost_per_million_usd":0},\n'
        ' {"model_uid":"swe-2-max","label":"SWE-2 Max","tier":"CORE_TIER",'
        '"input_cost_per_million_usd":9,"output_cost_per_million_usd":9,'
        '"cache_read_cost_per_million_usd":9},\n'
        ' {"model_uid":"gpt-6-luna-max","label":"Luna Max",'
        '"tier":"TEAMS_TIER_PRO","input_cost_per_million_usd":2.5,'
        '"output_cost_per_million_usd":10.0,'
        '"cache_read_cost_per_million_usd":0.25}\n'
        "];</script>")

    def test_parse_price_table_picks_self_serve_tier(self):
        t = conductor._parse_price_table(self.TABLE_SNIPPET)
        self.assertEqual(set(t), {"swe-2-max", "gpt-6-luna-max"})
        self.assertEqual(t["swe-2-max"]["in"], 0)      # PRO, not CORE's 9
        self.assertEqual(t["gpt-6-luna-max"]["out"], 10.0)
        self.assertEqual(t["gpt-6-luna-max"]["cr"], 0.25)

    def test_parse_price_table_tolerates_garbage(self):
        self.assertEqual(conductor._parse_price_table("no table here"), {})
        self.assertEqual(conductor._parse_price_table(
            "modelCostData = [not json];"), {})

    def test_estimate_cost_splits_cached_from_prompt(self):
        table = {"m-x": {"label": "M X", "in": 2.0, "out": 10.0, "cr": 0.2}}
        c = conductor.estimate_cost(
            {"prompt": 1_000_000, "cached": 200_000,
             "completion": 100_000, "llm_steps": 3}, "m", "x", table)
        # (1M-200k)*$2/M + 200k*$0.2/M + 100k*$10/M = 1.6 + 0.04 + 1.0
        self.assertAlmostEqual(c["usd"], 2.64, places=3)
        self.assertEqual(c["uid"], "m-x")
        self.assertEqual(c["label"], "M X")
        # bare model uid is the fallback when the effort variant is unlisted
        self.assertEqual(conductor.estimate_cost(
            {"prompt": 5}, "m", None, table)["uid"], "m")
        # unpriced model -> usd None; no tokens -> cost None
        self.assertIsNone(conductor.estimate_cost(
            {"prompt": 5}, "zzz", "x", table)["usd"])
        self.assertIsNone(conductor.estimate_cost(None, "m", "x", table))

    def test_price_table_disk_cache_and_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            runs.mkdir()
            (Path(td) / "pricing.json").write_text(json.dumps({
                "fetched_at": 123.0, "url": "u",
                "table": {"m-x": {"label": "M", "in": 1, "out": 2, "cr": 0}}}))
            with mock.patch.object(conductor, "_PRICE_CACHE",
                                   {"fetched": 0.0, "table": {},
                                    "source": ""}):
                t = conductor.price_table(runs)
                self.assertEqual(t["m-x"]["in"], 1)

            snippet = self.TABLE_SNIPPET.encode()

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return snippet

            cache = {"fetched": 0.0, "table": {}, "source": ""}
            with mock.patch.object(conductor, "_PRICE_CACHE", cache), \
                    mock.patch.object(conductor, "_PRICE_TRIED", 0.0), \
                    mock.patch.object(conductor, "_PRICE_BUSY", False), \
                    mock.patch.object(conductor.urllib.request, "urlopen",
                                      return_value=_Resp()):
                conductor.refresh_prices(runs)
                # the refresh thread updates memory before the disk mirror
                # - poll the file, not just the cache
                deadline = __import__("time").time() + 5
                disk = {}
                while __import__("time").time() < deadline:
                    try:
                        disk = json.loads(
                            (Path(td) / "pricing.json").read_text(
                                encoding="utf-8"))
                    except (OSError, ValueError):
                        disk = {}
                    if "gpt-6-luna-max" in (disk.get("table") or {}):
                        break
                    __import__("time").sleep(0.05)
                self.assertIn("swe-2-max", cache["table"])
                self.assertEqual(cache["source"], conductor._PRICES_URL)
                self.assertIn("gpt-6-luna-max", disk["table"])

    def test_collect_dashboard_costs_agents_and_runs(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "runs"
            rd = runs / "20261014-000000-t"
            rd.mkdir(parents=True)
            (rd / "state.json").write_text(json.dumps({
                "run": "t", "manifest": "m.toml",
                "steps": {"s": {"name": "s", "status": "done",
                                "model": "swe-2", "effort": "max",
                                "tokens": {"prompt": 5000,
                                           "completion": 1000,
                                           "cached": 0, "llm_steps": 2}}}}))
            table = {"swe-2-max": {"label": "SWE-2 Max", "in": 2.0,
                                   "out": 10.0, "cr": 0.1}}
            with mock.patch.object(conductor, "_PRICE_CACHE",
                                   {"fetched": 1.0, "table": table,
                                    "source": "t"}), \
                    mock.patch.object(conductor, "refresh_prices"):
                d = conductor.collect_dashboard(runs, ["no-such-devin"])
            a = d["agents"][0]
            # 5000 * 2 + 1000 * 10 = $0.02
            self.assertAlmostEqual(a["cost"]["usd"], 0.02, places=3)
            self.assertEqual(a["cost"]["uid"], "swe-2-max")
            self.assertAlmostEqual(d["runs"][0]["cost"]["usd"], 0.02)
            self.assertEqual(d["runs"][0]["cost"]["unpriced"], 0)
            self.assertEqual(d["pricing"]["models"], 1)


class SessionGoneGraceTests(unittest.TestCase):
    """A recorded session_id missing from `devin list` minutes after spawn
    is registration/cache lag - 'gone' waits out SESSION_GONE_GRACE_S."""

    def _collect(self, runs: Path, rd: Path, rec: dict) -> bool:
        (rd / "state.json").write_text(json.dumps(
            {"run": "t", "started_at": conductor.now_iso(),
             "steps": {"s": rec}}))
        with mock.patch.object(conductor, "live_sessions_by_dir",
                               return_value={rec["work_dir"]: {}}), \
                mock.patch.object(conductor, "refresh_prices"):
            d = conductor.collect_dashboard(runs, ["no-such-devin"])
        return d["agents"][0]["session_gone"]

    def test_grace_and_expiry(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "runs"
            rd = runs / "20261014-000000-t"
            rd.mkdir(parents=True)
            wd = str(base / "proj")
            now = conductor.now_iso()
            # fresh running step: id recorded, list hasn't caught up yet
            self.assertFalse(self._collect(runs, rd, {
                "name": "s", "status": "running", "session_id": "abc",
                "started_at": now, "timeout_min": 60, "work_dir": wd}))
            # ended -> honest immediately, grace does not apply
            self.assertTrue(self._collect(runs, rd, {
                "name": "s", "status": "done", "session_id": "abc",
                "started_at": now, "ended_at": now, "work_dir": wd}))
            # still running but spawned before the grace window
            old = (conductor.dt.datetime.now() - conductor.dt.timedelta(
                seconds=conductor.SESSION_GONE_GRACE_S + 60)).isoformat(
                    timespec="seconds")
            self.assertTrue(self._collect(runs, rd, {
                "name": "s", "status": "running", "session_id": "abc",
                "started_at": old, "timeout_min": 60, "work_dir": wd}))

    def test_live_pid_suppresses_gone_dead_pid_shows_now(self):
        """While the recorded worker pid is alive the session must exist -
        `devin list` lag alone never marks it gone, however old. A dead pid
        means the session really is gone - show it without waiting out the
        grace window."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "runs"
            rd = runs / "20261014-000000-t"
            rd.mkdir(parents=True)
            wd = str(base / "proj")
            now = conductor.now_iso()
            old = (conductor.dt.datetime.now() - conductor.dt.timedelta(
                seconds=conductor.SESSION_GONE_GRACE_S + 60)).isoformat(
                    timespec="seconds")
            sleeper = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                alive_pid = sleeper.pid
            finally:
                pass
            dead_shell = subprocess.Popen(
                [sys.executable, "-c", "pass"])
            dead_shell.wait()
            dead_pid = dead_shell.pid
            try:
                # brand-new step, recorded pid dead -> real deletion: no wait
                self.assertTrue(self._collect(runs, rd, {
                    "name": "s", "status": "running", "session_id": "abc",
                    "started_at": now, "timeout_min": 60, "pid": dead_pid,
                    "work_dir": wd}))
                # ancient step, pid still alive -> lag, not deletion
                self.assertFalse(self._collect(runs, rd, {
                    "name": "s", "status": "running", "session_id": "abc",
                    "started_at": old, "timeout_min": 60, "pid": alive_pid,
                    "work_dir": wd}))
            finally:
                sleeper.kill(); sleeper.wait()


class DashboardJsFilterTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_status_chips_filter_agents_and_runs(self):
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
  {step:'alpha',run:'first',run_dir:'/first',slug:'p',status:'running',wt:true,branch:'b1',task:'work one'},
  {step:'beta',run:'first',run_dir:'/first',slug:'p',status:'done',task:'work two',
   tokens:{prompt:1000,cached:0,completion:200,llm_steps:2},
   cost:{usd:0.1234,uid:'m-x',label:'M X'}},
  {step:'gamma',run:'second',run_dir:'/second',slug:'q',status:'done',task:'work three'}],runs:[
  {run:'first',dir:'/first',description:'run one',projects:['p'],
   counts:{running:1,done:1},
   tokens:{prompt:1000,cached:0,completion:200,llm_steps:2},
   cost:{usd:1.5,unpriced:0}},
  {run:'second',dir:'/second',description:'run two',projects:['q'],counts:{done:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
// run row: per-status step counts, live pulse, est cost
assert.match(elem('runs').innerHTML,/>1 running</);
assert.match(elem('runs').innerHTML,/>1 done</);
assert.match(elem('runs').innerHTML,/class="dot run"/);
assert.match(elem('runs').innerHTML,/est \$1\.50/);
// run row: step-token line names every field under the 'steps:' label
assert.match(elem('runs').innerHTML,/steps: in 1k · out 200 · cached 0 · 2 turns · \$1\.50/);
// agent row: cost cell; tokens render as labeled fields (in/out/cached/
// turns) - the bare '1k/200 ·2st' compact form is gone.
assert.match(elem('agents').innerHTML,/\$0\.12/);
assert.match(elem('agents').innerHTML,/in 1k · out 200 · cached 0 · 2 turns/);
assert.doesNotMatch(elem('agents').innerHTML,/1k\/200/);
const click=async(t)=>handlers.click({target:t,preventDefault:()=>{}});
(async()=>{
  // clicking 'running' shows ONLY running agents and ONLY their run
  await click({dataset:{st:'running'}});
  assert.match(elem('agents').innerHTML,/work one/);
  assert.doesNotMatch(elem('agents').innerHTML,/work two/);
  assert.doesNotMatch(elem('agents').innerHTML,/work three/);
  assert.match(elem('runs').innerHTML,/run one/);
  assert.doesNotMatch(elem('runs').innerHTML,/run two/);
  assert.match(elem('filters').innerHTML,/chip active/);
  // clicking again returns to showing everything
  await click({dataset:{st:'running'}});
  assert.match(elem('agents').innerHTML,/work three/);
  assert.match(elem('runs').innerHTML,/run two/);
  // multi-select: done + running keeps both runs
  await click({dataset:{st:'done'}});
  await click({dataset:{st:'running'}});
  assert.match(elem('agents').innerHTML,/work one/);
  assert.match(elem('agents').innerHTML,/work three/);
  await click({dataset:{reset:''}});
  assert.match(elem('runs').innerHTML,/run two/);
  assert.match(elem('agents').innerHTML,/work three/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_run_state_chips_and_retry_marks(self):
        """Run rows always carry a state label: running / paused /
        interrupted / halted; agent rows mark retried (xN) and
        timeout-extended (+ext) steps."""
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
  {step:'s1',run:'paused',run_dir:'/paused',slug:'p',status:'stopped',task:'t1'},
  {step:'s2',run:'paused',run_dir:'/paused',slug:'p',status:'done',task:'t2',
   attempts:2,timeout_ext_s:300},
  {step:'s3',run:'live',run_dir:'/live',slug:'p',status:'running',task:'t3'}],runs:[
  {run:'paused',dir:'/paused',aborted:'paused',ended_at:'2026-10-14T01:00:00',
   counts:{done:1,stopped:1,skipped:2}},
  {run:'crashed',dir:'/crashed',counts:{lost:1}},
  {run:'live',dir:'/live',counts:{running:1}},
  {run:'clean',dir:'/clean',ended_at:'2026-10-14T01:00:00',counts:{done:2}},
  // resumed-and-finished before the aborted-clear fix: nothing left to
  // resume, so the stale label must not render
  {run:'healed',dir:'/healed',aborted:'paused',
   ended_at:'2026-10-14T02:00:00',counts:{done:4}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const runs=elem('runs').innerHTML;
assert.match(runs,/paused - resume ready/);
assert.match(runs,/interrupted - resume ready/);
assert.match(runs,/class="stage live"[^>]*>running</);
// run name is the details affordance - no separate Details link
assert.match(runs,/class="linkbtn run-name"[^>]*data-run-details=/);
assert.doesNotMatch(runs,/>Details</);
const cleanRow=runs.split('runrow').find(r=>r.includes('>clean<'));
assert.ok(cleanRow && !/stage (live|paused|interrupted|halted)/.test(cleanRow));
const healedRow=runs.split('runrow').find(r=>r.includes('>healed<'));
assert.ok(healedRow && !/stage (live|paused|interrupted|halted)/.test(healedRow));
const agents=elem('agents').innerHTML;
assert.match(agents,/×2/);
assert.match(agents,/> \+ext</);
// a click landing on the <b> INSIDE the name button still opens the
// modal - e.target.dataset is empty, so the handler must closest() up
const btn={dataset:{runDetails:'/paused'}}, btn2={dataset:{runDetails:'/healed'}};
(async()=>{
  await handlers.click({target:{dataset:{},
    closest:sel=>sel==='[data-run-details]'?btn:null,
    preventDefault:()=>{}}});
  assert.match(elem('dbody').innerHTML,/>paused</);
  assert.equal(elem('backdrop').hidden,false);
  // ...but a resumed-and-done run drops the stale 'Paused by user' notice
  await handlers.click({target:{dataset:{},
    closest:sel=>sel==='[data-run-details]'?btn2:null,
    preventDefault:()=>{}}});
  assert.doesNotMatch(elem('dbody').innerHTML,/Paused by user/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_state_md_finds_project_task_file(self):
        """The agent modal links the project's own .agent_tasks STATE.md
        (step name == task folder name by convention), falling back from
        a gone worktree dir to the registered project path."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            wt = base / "wt" / "mystep"
            proj = base / "proj"
            (proj / ".agent_tasks" / "mystep").mkdir(parents=True)
            (proj / ".agent_tasks" / "mystep" / "STATE.md").write_text(
                "state\n", encoding="utf-8")
            # worktree dir gone -> registered project path wins
            self.assertEqual(
                conductor._state_md(str(wt), "mystep", str(proj)),
                str(proj / ".agent_tasks" / "mystep" / "STATE.md"))
            # inside an existing worktree it resolves there first
            (wt / ".agent_tasks" / "mystep").mkdir(parents=True)
            (wt / ".agent_tasks" / "mystep" / "STATE.md").write_text(
                "x", encoding="utf-8")
            self.assertEqual(
                conductor._state_md(str(wt), "mystep", str(proj)),
                str(wt / ".agent_tasks" / "mystep" / "STATE.md"))
            self.assertIsNone(
                conductor._state_md(str(proj), "nope", str(proj)))

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_sticky_topbar_and_density_toggle(self):
        """The header/filters live in a sticky topbar; every table header
        pins just under it (--topbar-h measured in JS). A Compact|Full
        toggle flips body.compact, collapsing wrap cells to one line.
        No .wrapw may carry overflow - it would become the scroll
        container and break th stickiness."""
        html = conductor._DASH_HTML
        self.assertIn('class="topbar"', html)
        self.assertIn('id="density"', html)
        self.assertIn('data-density="compact"', html)
        self.assertIn('data-density="full"', html)
        self.assertRegex(html, r'\.topbar \{[^}]*position:sticky')
        self.assertRegex(html, r'th \{[^}]*position:sticky')
        self.assertIn('top:var(--topbar-h', html)
        self.assertIn('body.compact td.wrap', html)
        self.assertNotRegex(html, r'\.wrapw \{[^}]*overflow')
        js = html.split("<script>\n", 1)[1].split("</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={}, props={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true}); return nodes.get(id); };
const cls={_s:new Set(),toggle(c,on){on?this._s.add(c):this._s.delete(c)},
  contains(c){return this._s.has(c)}};
const document={getElementById:elem,
  querySelector:s=>s==='.topbar'?{offsetHeight:96}:null,
  querySelectorAll:()=>[],
  addEventListener:(ev,h)=>{handlers[ev]=h;},
  body:{classList:cls},
  documentElement:{style:{setProperty:(k,v)=>{props[k]=v;}}}};
const ctx=vm.createContext({document,location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
assert.equal(props['--topbar-h'],'96px');     // measured, not hardcoded
assert.ok(!cls.contains('compact'));          // default = full
(async()=>{
  await handlers.click({target:{dataset:{density:'compact'},
    preventDefault:()=>{}}});
  assert.ok(cls.contains('compact'));
  await handlers.click({target:{dataset:{density:'full'},
    preventDefault:()=>{}}});
  assert.ok(!cls.contains('compact'));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_run_dir_basename_on_windows_paths(self):
        """Regression: _DASH_HTML is a Python string, so '\\\\' in the
        source reached JS as '\\' - /[\\\\/]/ split on '/' only and
        Windows run dirs posted the whole C:\\path to the API
        (RUN_DIR_RE -> 404 'Invalid run dir' on Resume/Pause/Stop/
        Merge). base() uses fromCharCode(92): no backslash literal."""
        html = conductor._DASH_HTML
        self.assertNotIn("split(/[", html)   # no regex path-split left
        self.assertIn("fromCharCode(92)", html)
        js = html.split("<script>\n", 1)[1].split("</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map();
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,querySelectorAll:()=>[],
  addEventListener:()=>{}};
const ctx=vm.createContext({document,location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
const B='String.fromCharCode(92)';   // no raw backslash in the code string
assert.equal(vm.runInContext(
  "base('C:'+"+B+"+'a'+"+B+"+'dir-name')",ctx),'dir-name');
assert.equal(vm.runInContext("base('/x/y/dir-name')",ctx),'dir-name');
assert.equal(vm.runInContext("base('dir-name')",ctx),'dir-name');
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class ApplyRequestTests(unittest.TestCase):
    """The deterministic 'Apply options to TOML' path: option fields patch
    the proposal in place; request text and project still need a planner."""

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

    def proposal(self, extra_steps=""):
        return f'''[run]
name = "catalog-cleanup"
description = "Tidy the catalog"
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

    def ready(self):
        rec = conductor.create_request(
            {"raw": "Please tidy the catalog carefully", "project": "proj",
             "model": "swe-2", "effort": "max"}, self.requests, self.runs)
        return conductor.save_request(
            rec["id"], {"manifest": self.proposal()},
            self.requests, self.manifests, self.runs)

    def test_apply_patches_options_and_clears_stale(self):
        rec = self.ready()
        out = conductor.apply_request(
            rec["id"], {"effort": "high", "worktrees": True,
                        "merge_resolver": True, "followup": True,
                        "max_attempts": 5},
            self.requests, self.manifests, self.runs)
        text = Path(out["manifest"]).read_text(encoding="utf-8")
        cfg = conductor.load_manifest(Path(out["manifest"]))
        self.assertIn('merge_resolver = true', text)
        self.assertIn('effort = "high"', text)
        self.assertIn('max_attempts = 5', text)
        self.assertTrue(all(s.worktree for s in cfg.steps))
        self.assertTrue(cfg.merge_resolver)
        self.assertTrue(any(s.name == "follow-up-plan" for s in cfg.steps))
        self.assertFalse(out["stale"])
        self.assertEqual(out["status"], "ready")

    def test_apply_refuses_request_text_or_project_changes(self):
        rec = self.ready()
        with self.assertRaises(conductor.RequestError) as e:
            conductor.apply_request(rec["id"], {"raw": "different scope"},
                                    self.requests, self.manifests,
                                    self.runs)
        self.assertEqual(e.exception.status, 409)
        self.assertIn("regenerate", str(e.exception))
        with self.assertRaises(conductor.RequestError):
            conductor.apply_request(rec["id"], {"project": "other"},
                                    self.requests, self.manifests,
                                    self.runs)

    def test_option_changes_stale_but_planner_model_does_not(self):
        rec = self.ready()
        out = conductor.save_request(
            rec["id"], {"plan_model": "gpt-6-luna"}, self.requests,
            self.manifests, self.runs)
        self.assertFalse(out["stale"])   # planner-only, nothing in the TOML
        out = conductor.save_request(rec["id"], {"worktrees": True},
                                     self.requests, self.manifests,
                                     self.runs)
        self.assertTrue(out["stale"])
        out = conductor.apply_request(rec["id"], {"worktrees": True},
                                      self.requests, self.manifests,
                                      self.runs)
        self.assertFalse(out["stale"])
        self.assertIn("worktree = true",
                      Path(out["manifest"]).read_text(encoding="utf-8"))

    def test_apply_strips_step_overrides_the_form_owns(self):
        text = self.proposal('''
worktree = true
model = "gpt-6-luna"
effort = "min"
max_attempts = 9
merge_resolver = true
''')
        rec = conductor.create_request(
            {"raw": "x", "project": "proj", "model": "swe-2",
             "effort": "max"}, self.requests, self.runs)
        rec = conductor.save_request(rec["id"], {"manifest": text},
                                     self.requests, self.manifests,
                                     self.runs)
        out = conductor.apply_request(
            rec["id"], {"model": "gpt-6-astra", "effort": "low",
                        "merge_resolver": False},
            self.requests, self.manifests, self.runs)
        cfg = conductor.load_manifest(Path(out["manifest"]))
        s = cfg.steps[0]
        self.assertEqual(s.model, "gpt-6-astra")   # form wins over per-step
        self.assertEqual(s.effort, "low")
        self.assertFalse(s.merge_resolver)
        # the unchecked worktrees box owns the option: a planner-added
        # per-step flag is stripped (except same-project wave members)
        self.assertFalse(s.worktree)
        body = Path(out["manifest"]).read_text(encoding="utf-8")
        step_body = body.split("[[step]]")[1]
        self.assertNotIn("max_attempts = 9", step_body)

    def test_apply_worktrees_never_marks_cloud_steps(self):
        cproj = make_project(self.base, "cproj")
        with mock.patch.object(conductor, "load_projects", return_value={
                "proj": {"path": self.proj, "cloud_ok": False},
                "cproj": {"path": cproj, "cloud_ok": True}}):
            rec = conductor.create_request(
                {"raw": "x", "project": "proj", "model": "swe-2",
                 "effort": "max"}, self.requests, self.runs)
            rec = conductor.save_request(rec["id"], {"manifest": self.proposal('''
[[step]]
name = "sky"
project = "cproj"
cloud = true
prompt = "check the thing"
''')}, self.requests, self.manifests, self.runs)
            out = conductor.apply_request(rec["id"], {"worktrees": True},
                                          self.requests, self.manifests,
                                          self.runs)
            by_name = {s.name: s for s in
                       conductor.load_manifest(Path(out["manifest"])).steps}
            self.assertTrue(by_name["organize"].worktree)
            self.assertFalse(by_name["sky"].worktree)  # cloud+wt is invalid

    def test_apply_toggles_followup_step_both_ways(self):
        rec = self.ready()
        out = conductor.apply_request(rec["id"], {"followup": True},
                                      self.requests, self.manifests,
                                      self.runs)
        self.assertIn("follow-up-plan",
                      Path(out["manifest"]).read_text(encoding="utf-8"))
        out = conductor.apply_request(rec["id"], {"followup": False},
                                      self.requests, self.manifests,
                                      self.runs)
        self.assertNotIn("follow-up-plan",
                         Path(out["manifest"]).read_text(encoding="utf-8"))

    def test_checkboxes_guarantee_flags_at_publish(self):
        """worktrees/merge_resolver are guarantees: a planner (or a hand
        edit) that omits them still gets the flags appended at publish."""
        rec = conductor.create_request(
            {"raw": "x", "project": "proj", "model": "swe-2",
             "effort": "max", "worktrees": True, "merge_resolver": True},
            self.requests, self.runs)
        out = conductor.save_request(rec["id"], {"manifest": self.proposal()},
                                     self.requests, self.manifests,
                                     self.runs)
        cfg = conductor.load_manifest(Path(out["manifest"]))
        self.assertTrue(cfg.merge_resolver)
        self.assertTrue(all(s.worktree for s in cfg.steps))
        self.assertTrue(all(s["wt"] for s in out["proposed_steps"]))


class LifecycleDisplayTests(unittest.TestCase):
    """Display/parsing helpers added for lifecycle + adaptive support."""

    def test_stdout_tail_splits_cr_redraws(self):
        """The CLI streams narration as one giant line separated by \\r
        (progress redraws). A naive splitlines tail reads the START of
        that mega-line; the freshest text is at the END."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "s.stdout.log"
            log.write_bytes(
                b"I'll start by reading the repo\rstep 2\rstep 3\r"
                b"tail-most line")
            t = conductor.stdout_tail(str(log), lines=2)
            # \r-separated chunks count as lines: the tail is the END
            self.assertEqual(t, "step 3\ntail-most line")
            self.assertNotIn("I'll start", t)

    def test_task_label_full_reads_prompt_file(self):
        with tempfile.TemporaryDirectory() as td:
            pf = Path(td) / "s.md"
            long_task = ("do a very long thing " * 30).strip()
            pf.write_text(
                f"# head\n## Task\n\n{long_task}\n\n## Before you finish\nx",
                encoding="utf-8")
            rec = {"task": long_task[:160], "prompt": str(pf)}
            short = conductor.task_label(rec)
            full = conductor.task_label(rec, full=True)
            self.assertEqual(short, " ".join(long_task.split())[:160])
            self.assertEqual(full, long_task)
            self.assertGreater(len(full), len(short))
            # file gone -> stored summary survives
            pf.unlink()
            self.assertEqual(conductor.task_label(rec, full=True),
                             rec["task"])

    def test_tail_agent_model_reads_export(self):
        with tempfile.TemporaryDirectory() as td:
            ex = Path(td) / "s.export.json"
            ex.write_text(json.dumps(
                {"agent": {"model_name": "GPT-6 Astra"},
                 "final_metrics": {}}), encoding="utf-8")
            self.assertEqual(conductor.tail_agent_model(ex),
                             "GPT-6 Astra")
            self.assertIsNone(conductor.tail_agent_model(
                Path(td) / "nope.json"))
            bad = Path(td) / "bad.json"
            bad.write_text("{no", encoding="utf-8")
            self.assertIsNone(conductor.tail_agent_model(bad))

    def test_estimate_cost_resolves_adaptive_actual(self):
        table = {"adaptive": {"label": "Adaptive", "in": 0, "out": 0,
                              "cr": 0},
                 "gpt-6-astra": {"label": "GPT-6 Astra", "in": 3.0,
                                 "out": 15.0, "cr": 0.3}}
        # 'adaptive' in the table wins for the adaptive row itself
        c = conductor.estimate_cost(
            {"prompt": 1000, "completion": 100}, "adaptive", None, table,
            model_actual="GPT-6 Astra")
        self.assertEqual(c["uid"], "adaptive")
        # but with no adaptive entry, the export's resolved name maps by
        # label so the rate is the model that actually ran
        del table["adaptive"]
        c = conductor.estimate_cost(
            {"prompt": 1_000_000, "completion": 1_000_000},
            "adaptive", None, table, model_actual="GPT-6 Astra")
        self.assertEqual(c["uid"], "gpt-6-astra")
        self.assertAlmostEqual(c["usd"], 18.0, places=3)

    def test_devin_cmd_adaptive_takes_no_effort_suffix(self):
        st = conductor.Step(name="s", project="p", prompt="x",
                            model="adaptive", effort="max")
        cmd = conductor.devin_cmd(["devin"], st, Path("p.md"),
                                  Path("e.json"))
        self.assertEqual(cmd[cmd.index("--model") + 1], "adaptive")
        st2 = conductor.Step(name="s", project="p", prompt="x",
                             model="swe-2", effort="max")
        cmd = conductor.devin_cmd(["devin"], st2, Path("p.md"),
                                  Path("e.json"))
        self.assertEqual(cmd[cmd.index("--model") + 1], "swe-2-max")

    def test_dashboard_tail_last_is_the_log_end(self):
        """The running-agent 'result' cell shows the freshest output, not
        the first line the worker ever wrote."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "runs"
            rd = runs / "20261014-000000-t"
            rd.mkdir(parents=True)
            log = rd / "s.stdout.log"
            log.write_text("I'll start by reading\r" +
                           "still working on it " * 60, encoding="utf-8")
            (rd / "state.json").write_text(json.dumps({
                "run": "t",
                "steps": {"s": {
                    "name": "s", "status": "done",
                    "stdout_log": str(log),
                    "task": "the task"}}}))
            with mock.patch.object(conductor, "refresh_prices"):
                d = conductor.collect_dashboard(runs, ["no-such-devin"])
            a = d["agents"][0]
            self.assertTrue(a["tail_last"].startswith("…"))
            self.assertIn("working on it", a["tail_last"])
            self.assertNotIn("I'll start", a["tail_last"])
            self.assertEqual(a["task_full"], "the task")

    def test_launch_process_detaches_from_server_console(self):
        """Dashboard-spawned run/resume subprocesses must not share the
        server's Ctrl+C/SIGHUP fate - CREATE_NEW_PROCESS_GROUP on nt,
        start_new_session on posix."""
        with tempfile.TemporaryDirectory() as td:
            seen = {}

            class _P:
                pid = 4242

            def fake_popen(cmd, **kw):
                seen.update(kw)
                return _P()

            with mock.patch.object(conductor.subprocess, "Popen",
                                   fake_popen):
                pid = conductor._launch_process(
                    ["x"], Path(td) / "l.log")
            self.assertEqual(pid, 4242)
            if os.name == "nt":
                self.assertTrue(
                    seen["creationflags"] &
                    subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                self.assertTrue(seen.get("start_new_session"))

    def test_dashboard_surfaces_model_actual(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "runs"
            rd = runs / "20261014-000000-t"
            rd.mkdir(parents=True)
            (rd / "state.json").write_text(json.dumps({
                "run": "t",
                "steps": {"s": {"name": "s", "status": "done",
                                "model": "adaptive",
                                "model_actual": "GPT-6 Astra"}}}))
            with mock.patch.object(conductor, "refresh_prices"):
                d = conductor.collect_dashboard(runs, ["no-such-devin"])
            self.assertEqual(d["agents"][0]["model"], "adaptive")
            self.assertEqual(d["agents"][0]["model_actual"],
                             "GPT-6 Astra")


class _RunControlBase(unittest.TestCase):
    """Shared helpers for driving a manifest run in a thread and poking
    its sentinels from the 'dashboard'."""

    def _start_run(self, m: Path, runs: Path):
        rc_box: list[int] = []
        t = threading.Thread(
            target=lambda: rc_box.append(run_manifest(m, runs)),
            daemon=True)
        t.start()
        return t, rc_box

    def _wait_run_dir(self, runs: Path, step: str,
                      timeout: float = 10.0) -> Path:
        """The run dir once `step` shows 'running' in state.json."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            dirs = [d for d in runs.glob("*/state.json")] \
                if runs.exists() else []
            for d in dirs:
                st = json.loads(d.read_text())
                if (st.get("steps") or {}).get(step, {}).get(
                        "status") == "running":
                    return d.parent
            time.sleep(0.05)
        self.fail(f"{step} never reached running")


class PauseRunTests(_RunControlBase):
    """Run-level controls - 'Pause run' is a GRACEFUL hold: in-flight
    steps finish, nothing new launches, the run records aborted='paused'
    (the deliberate machine-restart case). 'Stop run now' is the hard
    variant: live steps are tree-killed, aborted='stopped'. Both resume."""

    def test_pause_lets_inflight_finish_then_holds(self):
        """Pause mid-run: s1 completes -> 'done', s2/s3 never launch ->
        'skipped', aborted='paused' - even under on_failure='continue'."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "sleep=3 then work"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "second"

[[step]]
name = "s3"
project = "{proj.as_posix()}"
prompt = "third"
''', 'on_failure = "continue"\n')
            t, _rc = self._start_run(m, runs)
            run_dir = self._wait_run_dir(runs, "s1")
            out = conductor.stop_run(run_dir.name, {"pause": True}, runs)
            self.assertEqual(out["pausing"], ["s1"])
            t.join(timeout=30)
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st.get("aborted"), "paused")
            self.assertEqual(st["steps"]["s1"]["status"], "done")
            self.assertEqual(st["steps"]["s2"]["status"], "skipped")
            self.assertEqual(st["steps"]["s3"]["status"], "skipped")
            self.assertTrue(st.get("ended_at"))

    def test_pause_then_resume_finishes_the_run(self):
        """A paused run resumes: the held steps launch in fresh sessions
        (skipped is not an interruption - no abrupt-ending note)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "sleep=3 then work"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "second"
''')
            t, _rc = self._start_run(m, runs)
            run_dir = self._wait_run_dir(runs, "s1")
            conductor.stop_run(run_dir.name, {"pause": True}, runs)
            t.join(timeout=30)
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s1"]["status"], "done")
            self.assertEqual(st["steps"]["s2"]["status"], "skipped")
            # --resume: s2 proceeds, run ends clean.
            rc = run_manifest(m, runs, resume=str(run_dir))
            self.assertEqual(rc, 0)
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s1"]["status"], "done")
            self.assertEqual(st["steps"]["s2"]["status"], "done")
            # resume drops the stale 'aborted'/'ended_at' from the pause -
            # the dashboard would otherwise keep showing the 'Paused'
            # notice on a finished run.
            self.assertNotIn("aborted", st)
            self.assertIn("ended_at", st)

    def test_pause_before_launch_skips_everything(self):
        """Pause landing before the first wave: no worker ever spawns."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "work"
''')
            # Pin the run dir name so the flags can be raised against
            # the real path before cmd_run starts (the conductor sees
            # them at its very first wave-loop check).
            fixed = runs / "20260101-000000-t"

            def _fixed_run_dir(_cfg, _runs):
                fixed.mkdir(parents=True)
                (fixed / "prompts").mkdir(exist_ok=True)
                return fixed

            with mock.patch.object(conductor, "new_run_dir",
                                   side_effect=_fixed_run_dir):
                for f in conductor.pause_flags_for(fixed, {}):
                    conductor._flag_raise(f.name)
                rc = run_manifest(m, runs)
            self.assertEqual(rc, 1)
            st = json.loads((fixed / "state.json").read_text())
            self.assertEqual(st.get("aborted"), "paused")
            self.assertEqual(st["steps"]["s1"]["status"], "skipped")
            self.assertFalse(list(fixed.glob("*.stdout.log")))

    def test_stop_run_now_kills_then_resumes(self):
        """'Stop run now' is the hard stop: s1 dies mid-sleep -> 'stopped',
        s2 -> 'skipped', aborted='stopped'. Resume re-runs the stopped
        step with the abrupt-ending note and finishes the run."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "sleep=30 then work"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "second"
''')
            t, _rc = self._start_run(m, runs)
            run_dir = self._wait_run_dir(runs, "s1")
            out = conductor.stop_run(run_dir.name, {}, runs)
            self.assertEqual(out["stopping"], ["s1"])
            t.join(timeout=30)
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st.get("aborted"), "stopped")
            self.assertEqual(st["steps"]["s1"]["status"], "stopped")
            self.assertEqual(st["steps"]["s2"]["status"], "skipped")
            rc = run_manifest(m, runs, resume=str(run_dir))
            self.assertEqual(rc, 0)
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s1"]["status"], "done")
            self.assertEqual(st["steps"]["s2"]["status"], "done")
            prompt = (run_dir / "prompts" / "s1.md").read_text(
                encoding="utf-8")
            self.assertIn("previous session attempt", prompt)

    def test_stop_run_dual_writes_legacy_flag_names(self):
        """A run launched by OLD code (no state['flags']='v2' marker)
        polls bare-name sentinels - a new dashboard must write both.
        Pause also raises drain flags: that is the graceful-hold shape
        an old-code driver understands."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "runs"
            d = runs / "20260101-000000-t"
            d.mkdir(parents=True)
            (d / "state.json").write_text(json.dumps(
                {"run": "t", "steps": {}}))
            conductor.stop_run(d.name, {"pause": True}, runs)
            self.assertTrue(conductor.pause_flag(d).is_file())
            self.assertTrue(
                (conductor._flag_dir() / f"pause-{d.name}").is_file(),
                "legacy bare-name pause flag not written")
            self.assertTrue(conductor.drain_flag(d).is_file(),
                            "old drivers halt gracefully on drain")
            self.assertFalse(conductor.stop_flag(d).is_file(),
                             "graceful pause must not raise a stop flag")
            conductor.clear_run_flags(d)
            self.assertFalse(conductor.pause_flag(d).exists())
            self.assertFalse(conductor.drain_flag(d).exists())
            # a v2 run writes ONLY the path-keyed name
            legacy = conductor._flag_dir() / f"stop-{d.name}"
            (d / "state.json").write_text(json.dumps(
                {"run": "t", "flags": "v2", "steps": {}}))
            conductor.stop_run(d.name, {}, runs)
            self.assertFalse(legacy.exists())
            self.assertTrue(conductor.stop_flag(d).is_file())
            conductor.clear_run_flags(d)
            self.assertFalse(conductor.stop_flag(d).exists())


class StepControlTests(_RunControlBase):
    """Step-level controls - 'Stop this step' ends one task while the run
    proceeds; 'Skip step' marks a pending step so it never launches
    (durable across resume); 'Unskip step' puts it back."""

    def test_stop_this_step_lets_the_run_proceed(self):
        """A per-step stop is a veto on ONE task, not a failure: even
        under on_failure='stop' the run proceeds to the next step."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "sleep=30 then work"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "second"

[[step]]
name = "s3"
project = "{proj.as_posix()}"
prompt = "third"
''')
            t, _rc = self._start_run(m, runs)
            run_dir = self._wait_run_dir(runs, "s1")
            # stop_step kills by recorded pid - wait for it to land.
            t0 = time.time()
            while time.time() - t0 < 10:
                st = json.loads((run_dir / "state.json").read_text())
                if (st.get("steps") or {}).get("s1", {}).get("pid"):
                    break
                time.sleep(0.05)
            out = conductor.stop_step(run_dir.name, "s1", runs)
            self.assertEqual(out["stopping"], "s1")
            t.join(timeout=30)
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s1"]["status"], "stopped")
            self.assertEqual(st["steps"]["s2"]["status"], "done")
            self.assertEqual(st["steps"]["s3"]["status"], "done")
            self.assertNotIn("aborted", st)

    def test_skip_pending_step_keeps_queue_position(self):
        """Skip on a not-yet-spawned step: it records 'skipped' when its
        turn comes while the rest of the run proceeds in order."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "sleep=3 then work"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "second"

[[step]]
name = "s3"
project = "{proj.as_posix()}"
prompt = "third"
''')
            t, _rc = self._start_run(m, runs)
            run_dir = self._wait_run_dir(runs, "s1")
            out = conductor.skip_step(run_dir.name, "s2",
                                      {"skip": True}, runs)
            self.assertEqual(out["skipped"], "s2")
            t.join(timeout=30)
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s1"]["status"], "done")
            self.assertEqual(st["steps"]["s2"]["status"], "skipped")
            self.assertIn("skipped by user",
                          st["steps"]["s2"].get("note") or "")
            self.assertEqual(st["steps"]["s3"]["status"], "done")
            self.assertNotIn("aborted", st)
            self.assertTrue(
                conductor.skip_flag(run_dir, "s2").is_file())
            conductor.skip_flag(run_dir, "s2").unlink(missing_ok=True)

    def test_skip_survives_resume_until_unskipped(self):
        """The skip mark is durable: a plain --resume keeps the step
        skipped (clear_run_flags leaves it); only Unskip restores it,
        and the restored step re-runs on the next resume."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "sleep=3 then work"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "second"
''')
            t, _rc = self._start_run(m, runs)
            run_dir = self._wait_run_dir(runs, "s1")
            conductor.skip_step(run_dir.name, "s2", {"skip": True}, runs)
            t.join(timeout=30)
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s2"]["status"], "skipped")
            # resume WITHOUT unskipping: s2 is skipped again, not run.
            rc = run_manifest(m, runs, resume=str(run_dir))
            self.assertEqual(rc, 1)
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s2"]["status"], "skipped")
            # Unskip: the flag lifts; the next resume runs it in place.
            out = conductor.skip_step(run_dir.name, "s2",
                                      {"skip": False}, runs)
            self.assertTrue(out["turn_passed"])
            self.assertFalse(
                conductor.skip_flag(run_dir, "s2").exists())
            rc = run_manifest(m, runs, resume=str(run_dir))
            self.assertEqual(rc, 0)
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s2"]["status"], "done")

    def test_skip_endpoint_validation(self):
        """Skip only works on steps that have not launched: a running
        step wants 'Stop this step' instead; a finished step is 409;
        a name the run does not know is 404."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "work"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "two"
''')
            d = runs / "20260101-000000-t"
            d.mkdir(parents=True)
            (d / "state.json").write_text(json.dumps({
                "run": "t", "manifest": str(m), "ended_at": "x",
                "steps": {
                    "s1": {"name": "s1", "status": "done"},
                    "s2": {"name": "s2", "status": "running"}}}),
                encoding="utf-8")
            with self.assertRaises(conductor.RequestError) as e:
                conductor.skip_step(d.name, "s1", {"skip": True}, runs)
            self.assertEqual(e.exception.status, 409)
            with self.assertRaises(conductor.RequestError) as e:
                conductor.skip_step(d.name, "s2", {"skip": True}, runs)
            self.assertEqual(e.exception.status, 409)
            self.assertIn("Stop this step", str(e.exception))
            with self.assertRaises(conductor.RequestError) as e:
                conductor.skip_step(d.name, "nope", {"skip": True}, runs)
            self.assertEqual(e.exception.status, 404)

    def test_dashboard_shows_manifest_pending_and_skip_mark(self):
        """CLI-launched runs show declared-but-unlaunched steps as
        'waiting' (live) / 'skipped' (ended); a Skip mark forces
        'skipped' with skipped_by_user so the modal offers Unskip."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "first"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "second"

[[step]]
name = "s3"
project = "{proj.as_posix()}"
prompt = "third"
''')
            d = runs / "20260101-000000-t"
            d.mkdir(parents=True)
            live_state = {"run": "t", "manifest": str(m),
                          "started_at": "2026-01-01T00:00:00",
                          "steps": {"s1": {"name": "s1",
                                           "status": "done"}}}
            (d / "state.json").write_text(
                json.dumps(live_state), encoding="utf-8")
            conductor._flag_raise(conductor.skip_flag(d, "s3").name)
            try:
                with mock.patch.object(conductor, "refresh_prices"):
                    dash = conductor.collect_dashboard(
                        runs, ["no-such-devin"],
                        requests_dir=base / "requests")
                by_step = {a["step"]: a for a in dash["agents"]}
                self.assertEqual(by_step["s2"]["status"], "waiting")
                self.assertFalse(by_step["s2"].get("skipped_by_user"))
                self.assertEqual(by_step["s3"]["status"], "skipped")
                self.assertTrue(by_step["s3"]["skipped_by_user"])
                self.assertEqual(dash["runs"][0]["counts"],
                                 {"done": 1, "waiting": 1, "skipped": 1})
                # ended run: pending rows render 'skipped'
                live_state["ended_at"] = "2026-01-01T01:00:00"
                (d / "state.json").write_text(
                    json.dumps(live_state), encoding="utf-8")
                with mock.patch.object(conductor, "refresh_prices"):
                    dash = conductor.collect_dashboard(
                        runs, ["no-such-devin"],
                        requests_dir=base / "requests")
                by_step = {a["step"]: a for a in dash["agents"]}
                self.assertEqual(by_step["s2"]["status"], "skipped")
                self.assertFalse(by_step["s2"].get("skipped_by_user"))
            finally:
                conductor.skip_flag(d, "s3").unlink(missing_ok=True)

    def test_control_names_in_dashboard_html(self):
        """One name per action: Pause run / Stop run now / Resume
        unfinished steps / Stop this step / Skip step / Unskip step.
        The merged-away names are gone."""
        html = conductor._DASH_HTML
        for name in ("Pause run", "Stop run now", "Resume unfinished",
                     "Stop this step", "Skip step", "Unskip step"):
            self.assertIn(name, html)
        self.assertNotIn("Drain &amp; stop", html)
        self.assertNotIn("Stop this agent", html)


class PromptEditingTests(_RunControlBase):
    """Prompt edit control (pre-dispatch only) + resume notes (post-start
    follow-up) - both ride tempdir sentinels that run_step folds into
    the prompt body, so the manifest stays untouched."""

    def _run_dir(self, runs: Path, manifest: Path,
                 steps: dict) -> Path:
        d = runs / "20260101-000000-t"
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps(
            {"run": "t", "manifest": str(manifest), "ended_at": "x",
             "steps": steps}), encoding="utf-8")
        return d

    def test_prompt_edit_gated_to_never_dispatched(self):
        """queued/skipped records and rec-less manifest steps are editable;
        running/done 409 - the prompt was already dispatched."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "one"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "two"

[[step]]
name = "s3"
project = "{proj.as_posix()}"
prompt = "three"

[[step]]
name = "s4"
project = "{proj.as_posix()}"
prompt = "four"
''')
            d = self._run_dir(runs, m, {
                "s1": {"name": "s1", "status": "done"},
                "s2": {"name": "s2", "status": "queued"},
                "s4": {"name": "s4", "status": "running",
                       "started_at": "2026-01-01T00:00:00"}})
            try:
                out = conductor.set_step_prompt(
                    d.name, "s2", {"prompt": "edited body"}, runs)
                self.assertEqual(out["prompt_override"], "edited body")
                self.assertEqual(
                    conductor.prompt_flag(d, "s2").read_text(
                        encoding="utf-8"), "edited body")
                # a manifest step with no record at all is pending too
                conductor.set_step_prompt(d.name, "s3",
                                          {"prompt": "third edit"}, runs)
                self.assertTrue(
                    conductor.prompt_flag(d, "s3").is_file())
                # dispatched already -> locked
                for step in ("s1", "s4"):
                    with self.assertRaises(conductor.RequestError) as e:
                        conductor.set_step_prompt(
                            d.name, step, {"prompt": "x"}, runs)
                    self.assertEqual(e.exception.status, 409)
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.set_step_prompt(d.name, "nope",
                                              {"prompt": "x"}, runs)
                self.assertEqual(e.exception.status, 404)
                # revert deletes the override
                conductor.set_step_prompt(d.name, "s2",
                                          {"prompt": None}, runs)
                self.assertFalse(
                    conductor.prompt_flag(d, "s2").exists())
            finally:
                for s in ("s2", "s3"):
                    conductor.prompt_flag(d, s).unlink(missing_ok=True)

    def test_resume_notes_gate_and_clear(self):
        """Notes attach to ran-but-unfinished steps; done/running 409 and
        a pending step is told to use Edit prompt instead."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "one"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "two"
''')
            d = self._run_dir(runs, m, {
                "s1": {"name": "s1", "status": "done"},
                "s2": {"name": "s2", "status": "stopped"}})
            try:
                out = conductor.set_step_notes(
                    d.name, "s2", {"notes": "VPN is restored"}, runs)
                self.assertEqual(out["resume_notes"], "VPN is restored")
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.set_step_notes(d.name, "s1",
                                             {"notes": "x"}, runs)
                self.assertEqual(e.exception.status, 409)
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.set_step_notes(d.name, "nope",
                                             {"notes": "x"}, runs)
                self.assertEqual(e.exception.status, 404)
                # empty clears
                conductor.set_step_notes(d.name, "s2", {"notes": ""}, runs)
                self.assertFalse(
                    conductor.rnote_flag(d, "s2").exists())
            finally:
                conductor.rnote_flag(d, "s2").unlink(missing_ok=True)

    def test_edit_and_notes_survive_clear_run_flags(self):
        """Resume clears halt sentinels only - a pending prompt override
        and stored resume notes must reach the resume they were set for."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "one"
''')
            d = self._run_dir(runs, m, {})
            try:
                conductor.prompt_flag(d, "s1").parent.mkdir(exist_ok=True)
                conductor.prompt_flag(d, "s1").write_text("edit")
                conductor.rnote_flag(d, "s1").write_text("notes")
                conductor._flag_raise(conductor.stop_flag(d).name)
                conductor._flag_raise(conductor.skip_flag(d, "s1").name)
                conductor.clear_run_flags(d)
                self.assertTrue(conductor.prompt_flag(d, "s1").is_file())
                self.assertTrue(conductor.rnote_flag(d, "s1").is_file())
                self.assertTrue(conductor.skip_flag(d, "s1").is_file())
                self.assertFalse(conductor.stop_flag(d).exists())
            finally:
                for f in (conductor.prompt_flag(d, "s1"),
                          conductor.rnote_flag(d, "s1"),
                          conductor.skip_flag(d, "s1"),
                          conductor.stop_flag(d)):
                    f.unlink(missing_ok=True)

    def test_edited_prompt_is_what_dispatches(self):
        """An override saved while the step is pending replaces the task
        body the session actually receives."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "first task"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "ORIGINAL body"
''')
            d = self._run_dir(runs, m, {
                "s1": {"name": "s1", "status": "done"},
                "s2": {"name": "s2", "status": "skipped"}})
            try:
                conductor.set_step_prompt(d.name, "s2",
                                          {"prompt": "EDITED body"}, runs)
                rc = run_manifest(m, runs, resume=str(d))
                self.assertEqual(rc, 0)
                sent = (d / "prompts" / "s2.md").read_text(
                    encoding="utf-8")
                self.assertIn("EDITED body", sent)
                self.assertNotIn("ORIGINAL body", sent)
                st = json.loads((d / "state.json").read_text())
                self.assertEqual(st["steps"]["s2"]["status"], "done")
                self.assertTrue(st["steps"]["s2"]["prompt_edited"])
            finally:
                conductor.prompt_flag(d, "s2").unlink(missing_ok=True)

    def test_resume_notes_append_under_original(self):
        """On resume the step gets the original prompt + INTERRUPTED_NOTE
        + the operator's notes block appended - the manifest prompt is
        never rewritten."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "first task"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "ORIGINAL body"
''')
            d = self._run_dir(runs, m, {
                "s1": {"name": "s1", "status": "done"},
                "s2": {"name": "s2", "status": "failed",
                       "work_dir": str(proj)}})
            try:
                conductor.set_step_notes(
                    d.name, "s2", {"notes": "VPN is restored"}, runs)
                rc = run_manifest(m, runs, resume=str(d))
                self.assertEqual(rc, 0)
                sent = (d / "prompts" / "s2.md").read_text(
                    encoding="utf-8")
                self.assertIn("ORIGINAL body", sent)
                self.assertIn("ended abruptly", sent)   # INTERRUPTED_NOTE
                self.assertIn("NOTE FROM THE OPERATOR", sent)
                self.assertIn("VPN is restored", sent)
                self.assertLess(sent.index("ORIGINAL body"),
                                sent.index("VPN is restored"))
                self.assertIn('prompt = "ORIGINAL body"',
                              m.read_text(encoding="utf-8"))
            finally:
                conductor.rnote_flag(d, "s2").unlink(missing_ok=True)

    def test_dashboard_editability_and_full_body(self):
        """The payload tells the modal which control is legal: pending
        rows are editable with the FULL manifest body (not the 160-char
        summary); a stopped step takes notes instead."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            long_prompt = "do the long thing " * 30     # >160 chars
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "first"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "{long_prompt}"

[[step]]
name = "s3"
project = "{proj.as_posix()}"
prompt = "third"
''')
            d = self._run_dir(runs, m, {
                "s1": {"name": "s1", "status": "stopped"}})
            try:
                conductor.set_step_notes(d.name, "s1",
                                         {"notes": "n-note"}, runs)
                with mock.patch.object(conductor, "refresh_prices"):
                    dash = conductor.collect_dashboard(
                        runs, ["no-such-devin"],
                        requests_dir=base / "requests")
                by = {a["step"]: a for a in dash["agents"]}
                self.assertFalse(by["s1"]["prompt_editable"])
                self.assertTrue(by["s1"]["notes_enabled"])
                self.assertEqual(by["s1"]["resume_notes"], "n-note")
                self.assertTrue(by["s2"]["prompt_editable"])
                self.assertFalse(by["s2"]["notes_enabled"])
                self.assertEqual(by["s2"]["task_full"], long_prompt)
                self.assertGreater(len(by["s2"]["task_full"]), 160)
            finally:
                conductor.rnote_flag(d, "s1").unlink(missing_ok=True)

    def test_task_label_full_keeps_body_sections_and_lines(self):
        """A task body containing its own '## ' section was cut at the
        first heading; full mode now keeps the whole body (and its
        line breaks) up to the prompt file's real trailer."""
        with tempfile.TemporaryDirectory() as td:
            pf = Path(td) / "s.md"
            body = "first line\n\n## Own section\nsecond line"
            pf.write_text(
                f"# head\n\n## Task\n\n{body}\n\n## Before you finish\nx",
                encoding="utf-8")
            rec = {"task": "summary", "prompt": str(pf)}
            self.assertEqual(conductor.task_label(rec, full=True), body)
            self.assertIn("\n", conductor.task_label(rec, full=True))

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_modal_edit_form_locks_when_step_starts(self):
        """The edit control exists only pre-dispatch: a waiting step gets
        the textarea + Save; when the same row re-renders 'running' the
        form is gone and the unsaved draft is kept read-only with a
        'not applied' notice."""
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
const data={serve_boot:'b',generated_at:'t',agents:[
  {step:'s1',run:'r',run_dir:'/r',slug:'p',status:'waiting',
   task:'a long prompt',task_full:'the whole task body',
   prompt_editable:true,notes_enabled:false},
  {step:'s2',run:'r',run_dir:'/r',slug:'p',status:'stopped',
   task:'ran before',task_full:'dispatched body',
   prompt_editable:false,notes_enabled:true,resume_notes:'old note'}],
  runs:[{run:'r',dir:'/r',counts:{waiting:1,stopped:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const row=i=>({dataset:{},closest:sel=>sel==='tr.agen'?{dataset:{i:String(i)}}:null,preventDefault:()=>{}});
(async()=>{
  // pending step: full prompt text, editable form, labels disambiguated
  await handlers.click({target:row(0)});
  let html=elem('dbody').innerHTML;
  assert.match(html,/id="d-prompt"/);
  assert.match(html,/Save prompt/);
  assert.match(html,/the whole task body/);
  assert.match(html,/<div class="k">prompt file<\/div>/);
  assert.match(html,/<div class="k">task prompt<\/div>/);
  // typing records a draft keyed to this agent
  handlers.input({target:{id:'d-prompt',value:'unsaved edit text'}});
  // the step starts while the form is open -> the 2s rebuild locks it
  data.agents[0].status='running'; data.agents[0].prompt_editable=false;
  vm.runInContext('render('+JSON.stringify(data)+')',ctx);
  html=elem('dbody').innerHTML;
  assert.doesNotMatch(html,/id="d-prompt"/);
  assert.match(html,/NOT applied/);
  assert.match(html,/unsaved edit text/);
  // a stopped step: no edit form, a resume-notes field instead
  await handlers.click({target:row(1)});
  html=elem('dbody').innerHTML;
  assert.doesNotMatch(html,/id="d-prompt"/);
  assert.match(html,/id="d-rnotes"/);
  assert.match(html,/old note/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
