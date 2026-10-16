"""Tests for conductor.py. Convention: C:\\Projects\\TESTING.md

Everything here runs against tests/mock_devin.py - no auth, no credits.
"""
import argparse
import http.client
import json
import os
import shlex
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

    def test_summarize_flag_parses(self):
        """run.summarize: absent/false = off, true = on with the CLI
        default model (""), a string pins the summarizer's model,
        anything else is a ManifestError."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            step = f'''
[[step]]
name = "a"
project = "{proj.as_posix()}"
prompt = "do a"
'''
            for extra, want in (("summarize = true", ""),
                                ('summarize = "swe-2"', "swe-2"),
                                ("summarize = false", None), ("", None)):
                m = write_manifest(base, step, extra=extra)
                self.assertEqual(
                    conductor.load_manifest(m).summarize, want, extra)
            m = write_manifest(base, step, extra="summarize = 7")
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

    def test_git_dirty_tracked_ignores_untracked(self):
        """The merge-resolver guard fears only tracked modifications -
        untracked junk (stray `nul`, a pending generated manifest) can
        never join a merge commit, so it must not refuse the resolver."""
        with tempfile.TemporaryDirectory() as td:
            proj = make_project(Path(td))
            # ('nul' can't be a real file on Windows - the name writes
            # to the null device; use an ordinary stray name)
            (proj / "stray.tmp").write_text("stray", encoding="utf-8")
            self.assertTrue(conductor.git_dirty(proj))
            self.assertFalse(conductor.git_dirty_tracked(proj))
            (proj / "WORK.txt").write_text("dirty", encoding="utf-8")
            self.assertTrue(conductor.git_dirty_tracked(proj))


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
            run_dir = next(runs.iterdir())
            state = json.loads((run_dir / "state.json").read_text())
            self.assertTrue(state["steps"]["p1"]["merged"])
            self.assertTrue(state["steps"]["p2"]["merged"])
            self.assertTrue(state["steps"]["p1"]["merged_at"])
            evs = [json.loads(l) for l in
                   (run_dir / "events.jsonl").read_text().splitlines()]
            merged = [e for e in evs if e["event"] == "step_merged"]
            self.assertEqual(
                {(e["step"], e["how"], e["was"]) for e in merged},
                {("p1", "wave", "done"), ("p2", "wave", "done")})

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
            evs = [json.loads(l) for l in
                   (run_dir / "events.jsonl").read_text().splitlines()]
            conflicts = [e for e in evs
                         if e["event"] == "step_merge_conflict"]
            self.assertEqual([(e["step"], e["how"], e["was"])
                              for e in conflicts],
                             [("p2", "wave", "done")])
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
            rec = state["steps"]["wtstep"]
            self.assertTrue(rec["merged"])
            self.assertTrue(rec["merged_at"])
            evs = [json.loads(l) for l in
                   (run_dir / "events.jsonl").read_text().splitlines()]
            merged = [e for e in evs if e["event"] == "step_merged"]
            self.assertEqual([(e["step"], e["how"], e["was"])
                              for e in merged],
                             [("wtstep", "resume", "done")])

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
            evs = [json.loads(l) for l in
                   (run_dir / "events.jsonl").read_text().splitlines()]
            kept = [e for e in evs if e["event"] == "step_merge_kept"]
            self.assertEqual([(e["step"], e["was"]) for e in kept],
                             [("one", "failed")])

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
  // the task column is modal-only now - rows are told apart by step name
  assert.match(elem('agents').innerHTML,/>alpha</);
  assert.doesNotMatch(elem('agents').innerHTML,/>beta</);
  await click({dataset:{facet:'merged'}});
  assert.match(elem('filters').innerHTML,/merged ×/);
  await click({dataset:{clearRun:''}});
  // the 'merged' tag chip still filters both tables: 'second' has no
  // merged step, so its run row hides too.
  assert.doesNotMatch(elem('runs').innerHTML,/A separate run/);
  assert.doesNotMatch(elem('agents').innerHTML,/>beta</);
  await click({dataset:{facet:'merged'}});   // toggles the tag back off
  assert.match(elem('runs').innerHTML,/A separate run/);
  assert.match(elem('agents').innerHTML,/>beta</);
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
        # Planner output is spooled under %TEMP% in production - keep the
        # suite hermetic by pointing the spool root at the sandbox, and
        # shrink the deferred-publish poll so waits resolve in ms.
        self.spool = self.base / "planner-spool"
        for target, value in (("PLANNER_SPOOL", self.spool),
                              ("WATCH_POLL_S", 0.02)):
            p = mock.patch.object(conductor, target, value)
            p.start()
            self.addCleanup(p.stop)

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
                # `devin list` session probes spawn around the planner -
                # only the planner's own --prompt-file spawn is of interest
                if "--prompt-file" not in cmd:
                    return
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
            # hidden window AND its own process group - a merge/serve
            # shutdown must not reach the planner's console
            self.assertTrue(seen["flags"] & subprocess.CREATE_NO_WINDOW)
            self.assertTrue(
                seen["flags"] & subprocess.CREATE_NEW_PROCESS_GROUP)

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

    # ---- 'ask me questions' questionnaire flow ----------------------------

    QS = [{"qid": "q0", "header": "Scope", "question": "Which projects?",
           "options": [{"label": "proj", "description": "the catalog"},
                       {"label": "other", "description": ""}],
           "multi_select": False},
          {"qid": "q1", "header": "", "question": "Any constraints?",
           "options": [], "multi_select": False}]

    def test_ask_field_is_an_opt_in_bool(self):
        rec = self.new()
        self.assertFalse(rec["ask"])
        rec = conductor.create_request(
            {"raw": "Tidy", "project": "proj", "ask": "yes"},
            self.requests, self.runs)
        self.assertTrue(rec["ask"])
        rec = conductor.save_request(rec["id"], {"ask": False},
                                     self.requests, self.manifests,
                                     self.runs)
        self.assertFalse(rec["ask"])

    def test_questions_park_generation_until_answered(self):
        rec = conductor.create_request(
            {"raw": "Tidy the catalog", "project": "proj", "ask": True},
            self.requests, self.runs)
        calls = []

        def fake(r, projects, argv, log_path=None, fix_note=None, qa=None):
            calls.append(qa)
            if qa is None:
                raise conductor.PlannerQuestions(list(self.QS))
            return self.proposal()

        with mock.patch.object(conductor, "_planner_response", fake):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "questions")
        self.assertEqual(len(rec["pending_questions"]), 2)
        self.assertEqual(rec["qa_rounds"], 1)
        self.assertFalse(list(self.manifests.iterdir()))   # no TOML yet
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual(d["runs"][0]["stage"], "questions")
        self.assertEqual(len(d["runs"][0]["questions"]), 2)

        # first answer parks, the second triggers the refine pass
        out = conductor.answer_request(
            rec["id"], {"qid": "q0", "selected": ["proj"]},
            self.requests, self.manifests, self.runs, ["devin"],
            background=False)
        self.assertEqual(out["status"], "questions")
        self.assertEqual(len(out["pending_questions"]), 1)
        with mock.patch.object(conductor, "_planner_response", fake):
            out = conductor.answer_request(
                rec["id"], {"qid": "q1", "skipped": True},
                self.requests, self.manifests, self.runs, ["devin"],
                background=False)
        self.assertEqual(out["status"], "ready")
        self.assertTrue(Path(out["manifest"]).is_file())
        self.assertEqual(len(calls), 2)
        qa = calls[1]
        self.assertEqual([a["qid"] for a in qa], ["q0", "q1"])
        self.assertEqual(qa[0]["answer"], ["proj"])
        self.assertTrue(qa[1]["skipped"])

    def test_answer_request_validation(self):
        rec = conductor.create_request(
            {"raw": "Tidy", "project": "proj", "ask": True},
            self.requests, self.runs)
        with mock.patch.object(
                conductor, "_planner_response",
                side_effect=conductor.PlannerQuestions(list(self.QS))):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        for payload, msg in (
                ({"qid": "nope", "skipped": True}, "not pending"),
                ({"qid": "q0"}, "or skip"),
                ({"qid": "q0", "selected": ["bogus"]}, "Unknown option"),
                ({"qid": "q0", "selected": ["proj", "other"]}, "single")):
            with self.subTest(payload=payload), self.assertRaises(
                    conductor.RequestError) as error:
                conductor.answer_request(
                    rec["id"], payload, self.requests, self.manifests,
                    self.runs, ["devin"], background=False)
            self.assertIn(msg, str(error.exception))
        # a non-questions request rejects answers outright
        rec2 = self.new()
        with self.assertRaises(conductor.RequestError):
            conductor.answer_request(rec2["id"], {"qid": "q0", "text": "x"},
                                     self.requests, self.manifests,
                                     self.runs, ["devin"], background=False)

    def test_skip_all_refines_immediately(self):
        rec = conductor.create_request(
            {"raw": "Tidy", "project": "proj", "ask": True},
            self.requests, self.runs)
        seen = []

        def fake(r, projects, argv, log_path=None, fix_note=None, qa=None):
            seen.append(qa)
            if qa is None:
                raise conductor.PlannerQuestions(list(self.QS))
            return self.proposal()

        with mock.patch.object(conductor, "_planner_response", fake):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
            out = conductor.answer_request(
                rec["id"], {"skip_all": True}, self.requests,
                self.manifests, self.runs, ["devin"], background=False)
        self.assertEqual(out["status"], "ready")
        self.assertTrue(all(a["skipped"] for a in out["qa"]))
        self.assertEqual(len(out["qa"]), 2)

    def test_raw_edit_while_questions_pending_resets_to_draft(self):
        rec = conductor.create_request(
            {"raw": "Tidy", "project": "proj", "ask": True},
            self.requests, self.runs)
        with mock.patch.object(
                conductor, "_planner_response",
                side_effect=conductor.PlannerQuestions(list(self.QS))):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        rec = conductor.save_request(rec["id"], {"raw": "Different ask"},
                                     self.requests, self.manifests,
                                     self.runs)
        self.assertEqual(rec["status"], "draft")
        self.assertEqual(rec["pending_questions"], [])
        self.assertEqual(rec["qa"], [])

    def test_regenerate_clears_a_parked_questionnaire(self):
        rec = conductor.create_request(
            {"raw": "Tidy", "project": "proj", "ask": True},
            self.requests, self.runs)
        with mock.patch.object(
                conductor, "_planner_response",
                side_effect=conductor.PlannerQuestions(list(self.QS))):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        with mock.patch.object(conductor, "_planner_response",
                               return_value=self.proposal()):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["pending_questions"], [])
        self.assertEqual(rec["qa"], [])

    def test_question_rounds_are_capped(self):
        rec = conductor.create_request(
            {"raw": "Tidy", "project": "proj", "ask": True},
            self.requests, self.runs)
        with mock.patch.object(
                conductor, "_planner_response",
                side_effect=conductor.PlannerQuestions(list(self.QS))):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
            # MAX_QUESTION_ROUNDS rounds of questions are tolerated; the
            # next emission errors out instead of parking a fourth set
            for _ in range(conductor.MAX_QUESTION_ROUNDS - 1):
                out = conductor.answer_request(
                    rec["id"], {"skip_all": True}, self.requests,
                    self.manifests, self.runs, ["devin"], background=False)
            self.assertEqual(out["status"], "questions")
            out = conductor.answer_request(
                rec["id"], {"skip_all": True}, self.requests,
                self.manifests, self.runs, ["devin"], background=False)
        self.assertEqual(out["status"], "error")
        self.assertIn("still has questions", out["error"])

    def test_planner_questions_surface_only_when_ask_checked(self):
        qs_block = ('BEGIN_QUESTIONS\n[{"question": "Scope?", '
                    '"options": [{"label": "A"}]}]\nEND_QUESTIONS\n'
                    "STATUS: done\n")
        for ask in (False, True):
            rec = conductor.create_request(
                {"raw": "Tidy", "project": "proj", "ask": ask},
                self.requests, self.runs)

            class FakeProc:
                def __init__(self, cmd, **kwargs):
                    kwargs["stdout"].write(qs_block.encode())
                    kwargs["stdout"].flush()

            with mock.patch.object(conductor.subprocess, "Popen", FakeProc), \
                    mock.patch.object(
                        conductor, "wait_worker",
                        return_value=(0, False, False, False, 0.0)):
                if ask:
                    with self.assertRaises(conductor.PlannerQuestions) as e:
                        conductor._planner_response(rec, {}, ["devin"])
                    self.assertEqual(e.exception.questions[0]["question"],
                                     "Scope?")
                else:
                    with self.assertRaises(conductor.RequestError) as e:
                        conductor._planner_response(rec, {}, ["devin"])
                    self.assertIn("no TOML", str(e.exception))

    def test_extract_questions_sanitizes_planner_json(self):
        good = ('BEGIN_QUESTIONS\n' + json.dumps(
            [{"question": "Q?", "header": "h", "options": [
                {"label": "A", "description": "x"}, {"nope": 1},
                "junk", {"label": ""}], "multi_select": True},
             {"question": ""}, "junk"]) +
            "\nEND_QUESTIONS\n")
        out = conductor._extract_questions(good)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["qid"], "q0")
        self.assertEqual(out[0]["options"],
                         [{"label": "A", "description": "x"}])
        self.assertTrue(out[0]["multi_select"])
        for bad in ('plain text only',
                    'BEGIN_QUESTIONS\nnot json\nEND_QUESTIONS\n',
                    'BEGIN_QUESTIONS\n[]\nEND_QUESTIONS\n',
                    'BEGIN_QUESTIONS\n[{"question": ""}]\nEND_QUESTIONS\n'):
            if bad == 'plain text only':
                self.assertIsNone(conductor._extract_questions(bad))
            else:
                with self.assertRaises(conductor.RequestError):
                    conductor._extract_questions(bad)

    def test_refine_prompt_carries_the_qa_block(self):
        rec = conductor.create_request(
            {"raw": "Tidy", "project": "proj", "ask": True},
            self.requests, self.runs)
        qa = [{"qid": "q0", "question": "Scope?", "answer": ["proj"],
               "text": "", "skipped": False},
              {"qid": "q1", "question": "Notes?", "answer": [],
               "text": "be gentle", "skipped": False},
              {"qid": "q2", "question": "Why?", "answer": [], "text": "",
               "skipped": True}]
        seen = {}
        proposal = self.proposal()

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                seen["prompt"] = Path(
                    cmd[cmd.index("--prompt-file") + 1]).read_text(
                        encoding="utf-8")
                kwargs["stdout"].write(
                    ("BEGIN_MANIFEST\n" + proposal +
                     "END_MANIFEST\nSTATUS: done\n").encode())
                kwargs["stdout"].flush()

        with mock.patch.object(conductor.subprocess, "Popen", FakeProc), \
                mock.patch.object(conductor, "wait_worker",
                                  return_value=(0, False, False, False, 0.0)):
            conductor._planner_response(rec, {}, ["devin"], qa=qa)
        self.assertIn("CLARIFYING Q&A", seen["prompt"])
        self.assertIn('chose "proj"', seen["prompt"])
        self.assertIn('wrote "be gentle"', seen["prompt"])
        self.assertIn("(skipped - your call)", seen["prompt"])
        self.assertIn("BEGIN_QUESTIONS", seen["prompt"])   # ask stays open

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

    def test_agents_carry_pipeline_seq_for_task_modal_nav(self):
        """`seq` is the manifest's step order - the pipeline the task
        modal's prev/next walks. A recorded step the manifest no longer
        names falls back to record order after it."""
        rec = conductor.save_request(
            self.new()["id"],
            {"manifest": self.proposal(extra_steps='''
[[step]]
name = "check"
prompt = "Check the catalog."

[[step]]
name = "polish"
prompt = "Polish the catalog."
''')}, self.requests, self.manifests, self.runs)
        # proposal previews get seq = proposed_steps order too
        d0 = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                         self.requests)
        plan = {a["step"]: a["seq"] for a in d0["agents"]}
        self.assertEqual(plan, {"organize": 0, "check": 1, "polish": 2})
        rd = self.runs / "20261015-000000-catalog-cleanup"
        rd.mkdir(parents=True)
        state = {"run": rec["run"], "request_id": rec["id"],
                 "manifest": rec["manifest"], "run_dir": str(rd),
                 "started_at": conductor.now_iso(), "steps": {
                     "polish": {"name": "polish", "slug": "proj",
                                "status": "done",
                                "work_dir": str(self.proj)},
                     "extra": {"name": "extra", "slug": "proj",
                               "status": "failed",
                               "work_dir": str(self.proj)}}}
        (rd / "state.json").write_text(json.dumps(state))
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        seq = {a["step"]: a["seq"] for a in d["agents"]}
        # pending rows keep manifest positions; 'extra' ran but is not in
        # the manifest, so it sorts after the declared pipeline
        self.assertEqual(seq, {"organize": 0, "check": 1, "polish": 2,
                               "extra": 3})

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

    # ---- planner isolation from merges ---------------------------------
    # A worktree merge can sit in the main checkout for minutes (a live
    # resolve-* session watches the repo root, or MERGE_HEAD is open).
    # Generation must still run: the planner phase is spooled outside
    # every watched cwd and only the short publish defers.

    def _until(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.02)
        return pred()

    def _watching_run(self, name="20261016-000000-merging"):
        """A state.json shaped like a live resolve-* session watching the
        whole sandbox: work_dir=self.base covers requests/ + manifests/."""
        rd = self.runs / name
        rd.mkdir(parents=True)
        (rd / "state.json").write_text(json.dumps({"steps": {
            "resolve-x": {"status": "running",
                          "started_at": conductor.now_iso(),
                          "timeout_min": 60, "work_dir": str(self.base)}}}))
        return rd

    def _release_watcher(self, rd):
        st = json.loads((rd / "state.json").read_text())
        st["steps"]["resolve-x"]["status"] = "done"
        st["ended_at"] = conductor.now_iso()
        (rd / "state.json").write_text(json.dumps(st))

    def test_generation_finishes_while_merge_resolver_is_live(self):
        """Regression: a resolve-* session in the checkout that owns
        data/requests used to make start_generation 409 for its whole
        life. Now the planner runs isolated, output lands in the spool,
        and only the short publish waits for the watcher to clear."""
        rec = self.new()
        rd = self._watching_run()
        disk_before = (self.requests / f"{rec['id']}.json").read_text()

        def fake(r, projects, argv, log_path=None, fix_note=None, qa=None):
            if log_path:
                Path(log_path).write_text(
                    "BEGIN_MANIFEST\n...\nEND_MANIFEST\nSTATUS: done\n",
                    encoding="utf-8")
            return self.proposal()

        with mock.patch.object(conductor, "_planner_response", fake):
            t = threading.Thread(
                target=conductor.start_generation,
                args=(rec["id"], self.requests, self.manifests, self.runs,
                      ["devin"]), kwargs={"background": False}, daemon=True)
            t.start()
            spool = self.spool / rec["id"]
            draft = spool / "draft.toml"
            self.assertTrue(self._until(draft.is_file),
                            "draft never reached the spool")
            d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                            self.requests)
            row = next(r for r in d["runs"]
                       if r.get("request_id") == rec["id"])
            self.assertEqual(row["stage"], "generating")
            # the running planner is surfaced as an agent row
            self.assertIn("plan-proposal",
                          {a["step"] for a in d["agents"]})
            # NOTHING was written into the watched checkout meanwhile:
            # no manifest, no status write, no gen.log in data/requests.
            self.assertFalse(list(self.manifests.iterdir()))
            self.assertFalse((self.requests / f"{rec['id']}.gen.log")
                             .exists())
            self.assertEqual((self.requests / f"{rec['id']}.json")
                             .read_text(), disk_before)
            self.assertTrue(t.is_alive())          # only publish deferred
            self._release_watcher(rd)
            t.join(15)
        self.assertFalse(t.is_alive(), "publish did not land after merge")
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")
        self.assertTrue(Path(rec["manifest"]).is_file())
        self.assertTrue((self.requests / f"{rec['id']}.gen.log").is_file())
        self.assertFalse(spool.exists())

    def test_publish_defers_only_while_merge_is_open(self):
        """A parked conductor/* branch must not block anything; an OPEN
        merge (MERGE_HEAD) defers only the publish, not the planner."""
        repo = make_project(self.base, "repo")
        manifests = repo / "manifests"
        manifests.mkdir()
        (manifests / "README.md").write_text("manifests\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "manifests dir")

        # merge pending: an unmerged conductor/* branch exists but no
        # merge is open -> a proposal publishes immediately
        git(repo, "checkout", "-b", "conductor/x")
        (repo / "WORK.txt").write_text("branch work\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "branch work")
        git(repo, "checkout", "-")     # back to the default branch
        self.assertNotEqual(git(repo, "branch", "--show-current").stdout,
                            "conductor/x")
        rec = self.new()
        with mock.patch.object(conductor, "_planner_response",
                               return_value=self.proposal()):
            conductor.start_generation(rec["id"], self.requests, manifests,
                                       self.runs, ["devin"],
                                       background=False)
        rec = conductor.request_details(rec["id"], self.requests, manifests)
        self.assertEqual(rec["status"], "ready")

        # merge active: conflicting change + open MERGE_HEAD
        (repo / "WORK.txt").write_text("main side\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "main work")
        r = git(repo, "merge", "--no-edit", "conductor/x")
        self.assertNotEqual(r.returncode, 0)     # real conflict, merge open
        rec2 = self.new()

        def fake(r_, projects, argv, log_path=None, fix_note=None, qa=None):
            if log_path:
                Path(log_path).write_text("STATUS: done\n")
            return self.proposal(
                description="Catalog tidy two") \
                .replace('name = "catalog-cleanup"',
                         'name = "catalog-cleanup-2"')

        with mock.patch.object(conductor, "_planner_response", fake):
            t = threading.Thread(
                target=conductor.start_generation,
                args=(rec2["id"], self.requests, manifests, self.runs,
                      ["devin"]), kwargs={"background": False}, daemon=True)
            t.start()
            draft = self.spool / rec2["id"] / "draft.toml"
            self.assertTrue(self._until(draft.is_file),
                            "draft never reached the spool")
            time.sleep(0.2)          # publish had a few poll windows
            self.assertFalse(list(manifests.glob(f"{rec2['id']}-*.toml")),
                             "publish landed mid-merge")
            self.assertTrue(t.is_alive())
            git(repo, "merge", "--abort")
            t.join(15)
        self.assertFalse(t.is_alive(), "publish did not land post-merge")
        rec2 = conductor.request_details(rec2["id"], self.requests,
                                         manifests)
        self.assertEqual(rec2["status"], "ready")
        self.assertTrue(Path(rec2["manifest"]).is_file())

    def _spool_fixture(self, rec, draft=None, log=None, status="generating",
                       draft_current=True, log_age_s=3600):
        """Build a spool as a crashed/interrupted generation left it."""
        spool = self.spool / rec["id"]
        spool.mkdir(parents=True)
        disk = json.loads((self.requests / f"{rec['id']}.json")
                          .read_text())
        started = conductor.now_iso()
        rec.update(status=status, generation_started_at=started)
        if draft is not None:
            rec["draft_started_at"] = (started if draft_current
                                       else "2000-01-01T00:00:00")
        (spool / "job.json").write_text(json.dumps({
            "request_id": rec["id"], "requests_dir": str(self.requests),
            "manifests_dir": str(self.manifests),
            "runs_dir": str(self.runs),
            "base_updated_at": disk.get("updated_at"),
            "base_status": disk.get("status")}), encoding="utf-8")
        (spool / "request.json").write_text(json.dumps(rec))
        if draft is not None:
            (spool / "draft.toml").write_text(draft, encoding="utf-8")
        if log is not None:
            lp = spool / "gen.log"
            lp.write_text(log, encoding="utf-8")
            old = time.time() - log_age_s       # a dead planner's log
            os.utime(lp, (old, old))
        return spool

    def test_drain_publishes_a_draft_left_by_a_restart(self):
        rec = self.new()
        spool = self._spool_fixture(rec, draft=self.proposal())
        adopted = conductor.drain_planner_spool(self.spool)
        self.assertEqual(adopted, [rec["id"]])
        # the flush is done when the spool is gone - status flips earlier
        self.assertTrue(self._until(lambda: not spool.exists()))
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")
        self.assertTrue(Path(rec["manifest"]).is_file())

    def test_drain_extracts_manifest_from_an_orphaned_log(self):
        """The planner finished writing BEGIN_MANIFEST but the server died
        before extracting: the spooled log alone is enough to publish."""
        rec = self.new()
        spool = self._spool_fixture(
            rec, log=("BEGIN_MANIFEST\n" + self.proposal() +
                      "END_MANIFEST\nSTATUS: done\n"))
        conductor.drain_planner_spool(self.spool)
        self.assertTrue(self._until(lambda: not spool.exists()))
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")

    def test_drain_marks_interrupted_generation_and_keeps_artifacts(self):
        rec = self.new()
        spool = self._spool_fixture(rec, log="planner died mid-thought\n")
        conductor.drain_planner_spool(self.spool)
        self.assertTrue(self._until(lambda: not spool.exists()))
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "error")
        self.assertIn("interrupted", rec["error"])
        # the partial log was preserved, not lost with the spool
        self.assertEqual((self.requests / f"{rec['id']}.gen.log")
                         .read_text(), "planner died mid-thought\n")

    def test_drain_never_republishes_a_superseded_draft(self):
        """A regenerate crashed before its own draft: the previous
        generation's draft.toml stays on disk but must NOT publish -
        the human asked for a new proposal."""
        rec = self.new()
        spool = self._spool_fixture(rec, draft=self.proposal(),
                                    draft_current=False)
        conductor.drain_planner_spool(self.spool)
        self.assertTrue(self._until(lambda: not spool.exists()))
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "error")
        self.assertFalse(list(self.manifests.glob("*.toml")))
        self.assertTrue((self.requests / f"{rec['id']}.draft.toml")
                        .is_file())

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

    def test_attachment_upload_serve_and_persistence(self):
        """POST /api/attachments stores raw image bytes under
        data/attachments (type sniffed from magic, not the header), GET
        serves it back for the thumbnail strip, and the [{name,path}] list
        round-trips through create/save/details."""
        attaches = self.base / "attachments"
        handler = conductor.make_dashboard_handler(
            self.runs, ["no-such-devin"], self.requests, self.manifests,
            attaches)
        server = conductor.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port

        def call(method, path, data=None, ctype="application/json",
                 raw=False):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = (data if raw else
                    json.dumps(data).encode("utf-8")
                    if data is not None else None)
            conn.request(method, path, body,
                         {"Content-Type": ctype,
                          "Origin": f"http://127.0.0.1:{port}"})
            res = conn.getresponse()
            payload = res.read()
            conn.close()
            return res.status, res.getheader("Content-Type"), payload

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
        status, _, payload = call("POST", "/api/attachments", png,
                                  ctype="image/png", raw=True)
        self.assertEqual(status, 201)
        att = json.loads(payload)
        self.assertTrue(att["name"].endswith(".png"))
        self.assertEqual((attaches / att["name"]).read_bytes(), png)
        self.assertEqual(att["path"],
                         str((attaches / att["name"]).resolve()))
        # thumbnails serve the stored bytes with the sniffed image type
        status, ctype, body = call("GET", f"/api/attachments/{att['name']}")
        self.assertEqual((status, ctype, body), (200, "image/png", png))
        # a lying Content-Type can't save a non-image; junk names 404
        status, _, _ = call("POST", "/api/attachments", b"MZ" + b"x" * 64,
                            ctype="image/png", raw=True)
        self.assertEqual(status, 415)
        status, _, _ = call("GET", "/api/attachments/..%2Fsecret.txt")
        self.assertEqual(status, 404)
        status, _, _ = call("GET", "/api/attachments/nope.png")
        self.assertEqual(status, 404)
        with mock.patch.object(conductor, "ATTACH_MAX_BYTES", 16):
            status, _, _ = call("POST", "/api/attachments", png,
                                ctype="image/png", raw=True)
        self.assertEqual(status, 413)
        # the attachment list persists through create -> details -> save
        status, _, payload = call("POST", "/api/requests", {
            "raw": "See the attached mockup", "project": "proj",
            "model": "swe-2", "effort": "max", "attachments": [att]})
        self.assertEqual(status, 201)
        rid = json.loads(payload)["id"]
        detail = json.loads(call("GET", f"/api/requests/{rid}")[2])
        self.assertEqual(detail["attachments"], [att])
        status, _, payload = call("POST", f"/api/requests/{rid}/save",
                                  {"attachments": []})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["attachments"], [])
        status, _, _ = call("POST", f"/api/requests/{rid}/save",
                            {"attachments": [{"name": "../x"}]})
        self.assertEqual(status, 400)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_request_attachments_strip_drop_paste_remove(self):
        """The request form's thumbnail strip renders saved attachments,
        attachFiles uploads + inserts the path at the caret, the per-image
        x removes it, and requestFields carries the list to the server."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={}, posts=[];
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',value:'',classList:{add(){},remove(){},toggle(){}}});
  return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,fn)=>{handlers[ev]=fn;}};
const fetch=async(path,opts)=>{posts.push({path,type:opts&&opts.headers&&
  opts.headers['Content-Type']});
  return {ok:true,status:201,json:async()=>(
    {name:'a1.png',path:'D:/data/attachments/a1.png'})};};
const ctx=vm.createContext({document,location:{reload:()=>{}},
  setTimeout:()=>{},fetch,Event:class Event{constructor(){}}});
vm.runInContext(src,ctx);
const d={serve_boot:'b',generated_at:'2026-10-14',agents:[],runs:[],
  project_options:[{slug:'p',cloud_ok:false}],
  defaults:{model:'swe-2',effort:'max'}};
vm.runInContext('render('+JSON.stringify(d)+')',ctx);
vm.runInContext('openNew()',ctx);
assert.match(elem('dbody').innerHTML,/id="rq-attach"/);
const ta=elem('rq-raw'); ta.value='Do this'; ta.selectionStart=7;
ta.selectionEnd=7;
// a reopened draft renders its saved attachments with remove controls
vm.runInContext('rqAttachments=[{name:"old.png",path:"D:/x/old.png"}];' +
  'renderAttach(true)',ctx);
assert.match(elem('rq-attach').innerHTML,/api\/attachments\/old\.png/);
assert.match(elem('rq-attach').innerHTML,/data-attach-rm="old\.png"/);
(async()=>{
  // file drop on the request dialog uploads + attaches + inserts the path
  let prevented=false;
  handlers.drop({dataTransfer:{files:[{type:'image/png'}]},
    preventDefault:()=>{prevented=true;}});
  await new Promise(r=>setImmediate(r));
  assert.equal(prevented,true);
  assert.equal(posts.length,1);
  assert.equal(posts[0].type,'image/png');
  assert.match(ta.value,/D:\/data\/attachments\/a1\.png/);
  assert.match(ta.value,/Do this\n/);
  assert.match(elem('rq-attach').innerHTML,/api\/attachments\/a1\.png/);
  assert.equal(elem('rq-attach').hidden,false);
  const f=vm.runInContext('requestFields()',ctx);
  // vm-boundary objects have foreign prototypes - compare serialized
  assert.equal(JSON.stringify(f.attachments),JSON.stringify(
    [{name:'old.png',path:'D:/x/old.png'},
     {name:'a1.png',path:'D:/data/attachments/a1.png'}]));
  // per-image remove works pre-submit
  await handlers.click({preventDefault:()=>{},target:{dataset:{},
    closest:s=>s==='[data-attach-rm]'?{dataset:{attachRm:'old.png'}}:null}});
  assert.equal(JSON.stringify(
    vm.runInContext('requestFields()',ctx).attachments),
    JSON.stringify([{name:'a1.png',path:'D:/data/attachments/a1.png'}]));
  // non-image drops are refused without an upload
  await vm.runInContext('attachFiles([{type:"text/plain"}])',ctx);
  assert.equal(posts.length,1);
  // a clipboard screenshot paste goes through the same path
  const item={kind:'file',type:'image/png',
    getAsFile:()=>({type:'image/png'})};
  prevented=false;
  handlers.paste({clipboardData:{items:[item]},
    preventDefault:()=>{prevented=true;}});
  await new Promise(r=>setImmediate(r));
  assert.equal(prevented,true);
  assert.equal(posts.length,2);
  // text pastes are untouched by the listener
  prevented=false;
  handlers.paste({clipboardData:{items:[{kind:'string',type:'text/plain'}]},
    preventDefault:()=>{prevented=true;}});
  assert.equal(prevented,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_request_attachments_file_picker(self):
        """The paperclip control opens the hidden input[type=file] and a
        selection takes the same upload path as drop/paste: a picked image
        POSTs to /api/attachments, thumbnails, and inserts its path. A
        canceled dialog uploads nothing and changes nothing; non-images
        get the shared validation error; a server-name duplicate stays one
        attachment; a saved draft reopens with its strip + remove."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={}, posts=[];
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',value:'',classList:{add(){},remove(){},toggle(){}}});
  return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,fn)=>{handlers[ev]=fn;}};
const REC={id:'r1',status:'draft',raw:'tidy',
  attachments:[{name:'a1.png',path:'D:/data/attachments/a1.png'}]};
const fetch=async(path,opts)=>{posts.push(String(path));
  if(path==='/api/attachments')
    return {ok:true,status:201,json:async()=>(
      {name:'a1.png',path:'D:/data/attachments/a1.png'})};
  if(String(path).startsWith('/api/requests/'))
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(REC))};
  return {ok:true,status:200,json:async()=>({})};};
const ctx=vm.createContext({document,location:{reload:()=>{}},
  confirm:()=>true,setTimeout:()=>{},fetch,
  Event:class Event{constructor(){}}});
vm.runInContext(src,ctx);
const d={serve_boot:'b',generated_at:'2026-10-14',agents:[],runs:[],
  project_options:[{slug:'p',cloud_ok:false}],
  defaults:{model:'swe-2',effort:'max'}};
vm.runInContext('render('+JSON.stringify(d)+')',ctx);
vm.runInContext('openNew()',ctx);
const html=elem('dbody').innerHTML;
// a real labeled button holding an svg glyph, plus a hidden image-only
// multi-select file input - present only while the form is editable
assert.match(html,/class="attach-btn" data-attach-pick/);
assert.match(html,/aria-label="Choose image files to attach"/);
assert.match(html,/<svg viewBox="0 0 24 24"/);
assert.match(html,/<input type="file" id="rq-file" accept="image\/\*" multiple\s+hidden>/);
const fi=elem('rq-file'); let picked=0; fi.click=()=>{picked++;};
const ta=elem('rq-raw'); ta.value='Do this'; ta.selectionStart=7;
ta.selectionEnd=7;
const atts=()=>JSON.stringify(vm.runInContext(
  'requestFields()',ctx).attachments);
const uploads=()=>posts.filter(p=>p==='/api/attachments').length;
const clickSel=(sel,dataset)=>handlers.click({preventDefault:()=>{},
  target:{dataset:{},closest:s=>s===sel?{dataset:dataset||{}}:null}});
const pick=files=>{fi.files=files;handlers.change({target:fi});
  return new Promise(r=>setImmediate(r));};
(async()=>{
  // the paperclip click drives the hidden input's click()
  await clickSel('[data-attach-pick]');
  assert.equal(picked,1);
  // canceling the dialog delivers no files: no upload, no thumbnail,
  // no dirty mark, the request text untouched
  await pick([]);
  assert.equal(uploads(),0);
  assert.equal(atts(),'[]');
  assert.equal(ta.value,'Do this');
  assert.notEqual(vm.runInContext('modalRequest.dirty',ctx),true);
  // a non-image selection is refused by the shared validation
  await pick([{type:'text/plain'}]);
  assert.equal(uploads(),0);
  assert.match(elem('form-msg').textContent,/Only image files/);
  // a picked image uploads, thumbnails, lands at the caret, marks the
  // form dirty, and the input value resets so re-picking re-fires
  await pick([{type:'image/png',name:'shot.png'}]);
  assert.equal(uploads(),1);
  assert.match(ta.value,/Do this\nD:\/data\/attachments\/a1\.png\n/);
  assert.match(elem('rq-attach').innerHTML,/api\/attachments\/a1\.png/);
  assert.equal(elem('rq-attach').hidden,false);
  assert.equal(vm.runInContext('modalRequest.dirty',ctx),true);
  assert.equal(fi.value,'');
  // re-picking re-fires 'change'; the name dedupe keeps ONE attachment
  // - the same contract drop/paste holds
  await pick([{type:'image/png',name:'shot.png'}]);
  assert.equal(uploads(),2);
  assert.equal(atts(),JSON.stringify(
    [{name:'a1.png',path:'D:/data/attachments/a1.png'}]));
  // a saved draft reopens with its attachments rendered + round-trips
  await vm.runInContext('openRequest("r1")',ctx);
  await new Promise(r=>setImmediate(r));
  assert.match(elem('rq-attach').innerHTML,/api\/attachments\/a1\.png/);
  assert.match(elem('rq-attach').innerHTML,/data-attach-rm="a1\.png"/);
  assert.equal(atts(),JSON.stringify(
    [{name:'a1.png',path:'D:/data/attachments/a1.png'}]));
  // the per-image remove control works on the reopened draft
  await clickSel('[data-attach-rm]',{attachRm:'a1.png'});
  assert.equal(atts(),'[]');
  assert.equal(elem('rq-attach').hidden,true);
  // non-editable stages render neither the picker nor its input
  vm.runInContext('showRequestForm({status:"launched",raw:"x"})',ctx);
  assert.equal(/data-attach-pick/.test(elem('dbody').innerHTML),false);
  assert.equal(/id="rq-file"/.test(elem('dbody').innerHTML),false);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class StopProposalTests(unittest.TestCase):
    """'Stop proposal' cancels an in-flight planner pass: the
    stopgen-<request_id> sentinel plus the _GEN_PROCS registry kill the
    devin subprocess, the record returns to its last editable state, and
    attempt-scoped (gen_id) writes keep a stale attempt's late output from
    overwriting restored state or a newer attempt."""

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

    def proposal(self, description="Tidy the catalog"):
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
'''

    def new(self):
        return conductor.create_request(
            {"raw": "Please tidy the catalog carefully", "project": "proj",
             "model": "swe-2", "effort": "max"}, self.requests, self.runs)

    def ready(self):
        rec = self.new()
        return conductor.save_request(
            rec["id"], {"manifest": self.proposal()},
            self.requests, self.manifests, self.runs)

    def cleanup_flag(self, rid):
        flag = conductor.gen_stop_flag(rid)
        self.addCleanup(lambda: flag.unlink(missing_ok=True))
        return flag

    def wait_calls(self, calls, n, timeout=10):
        deadline = time.time() + timeout
        while len(calls) < n and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(calls), n)

    def wait_attempts(self, n=0, timeout=10):
        deadline = time.time() + timeout
        while len(conductor._GENERATING) != n and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(conductor._GENERATING), n)

    def test_stop_restores_draft_and_late_output_is_discarded(self):
        rec = self.new()
        flag = self.cleanup_flag(rec["id"])
        gate = threading.Event()

        def fake(r, projects, argv, log_path=None, fix_note=None, qa=None):
            gate.wait(30)
            return self.proposal()

        with mock.patch.object(conductor, "_planner_response", fake):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs, ["devin"])
            out = conductor.stop_generation(rec["id"], self.requests,
                                            self.runs)
            self.assertEqual(out["status"], "draft")
            self.assertIsNone(out["error"])
            self.assertTrue(out.get("generation_stopped_at"))
            self.assertTrue(flag.is_file())
            gate.set()
            self.wait_attempts()
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests, self.runs)
        self.assertEqual(rec["status"], "draft")
        self.assertIsNone(rec["manifest"])
        # the stopped pass's late TOML was never published
        self.assertFalse(list(self.manifests.iterdir()))
        self.assertEqual(rec["raw"], "Please tidy the catalog carefully")
        self.assertEqual(rec["project"], "proj")
        self.assertEqual(rec["model"], "swe-2")

    def test_stop_with_previous_proposal_returns_to_ready(self):
        rec = self.ready()
        self.cleanup_flag(rec["id"])
        original = Path(rec["manifest"]).read_text(encoding="utf-8")
        gate = threading.Event()

        def fake(r, projects, argv, log_path=None, fix_note=None, qa=None):
            gate.wait(30)
            return self.proposal("A different plan")

        with mock.patch.object(conductor, "_planner_response", fake):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs, ["devin"])
            out = conductor.stop_generation(rec["id"], self.requests,
                                            self.runs)
            self.assertEqual(out["status"], "ready")
            gate.set()
            self.wait_attempts()
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")
        # the earlier valid proposal is byte-for-byte intact, not the
        # stopped pass's replacement
        self.assertEqual(Path(rec["manifest"]).read_text(encoding="utf-8"),
                         original)
        self.assertEqual(rec["description"], "Tidy the catalog")
        # and generation can be restarted right away (the sentinel is
        # cleared at start - a stale flag would kill the new pass)
        with mock.patch.object(conductor, "_planner_response",
                               return_value=self.proposal(
                                   "A different plan")):
            rec = conductor.start_generation(
                rec["id"], self.requests, self.manifests, self.runs,
                ["devin"], background=False)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["description"], "A different plan")

    def test_stop_tree_kills_the_live_planner_process(self):
        rec = self.new()
        self.cleanup_flag(rec["id"])
        procs = []

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                self.pid = 1            # taskkill of pid 1 errors ->
                self._dead = False      # kill_tree falls back to proc.kill
                procs.append(self)

            def poll(self):
                return 0 if self._dead else None

            def wait(self, timeout=None):
                if self._dead:
                    return 0
                raise subprocess.TimeoutExpired("planner", timeout or 0)

            def kill(self):
                self._dead = True

        def fake_wait(proc, log, timeout_s, stop_flags=(), timeout_flag=None,
                      stall_s=None):
            # honor the sentinel like the real wait_worker: raise -> kill
            for _ in range(500):
                if stop_flags and conductor._flag_set(*stop_flags):
                    proc.kill()
                    return proc.wait(), False, False, True, 0.0
                if proc.poll() is not None:
                    return 0, False, False, False, 0.0
                time.sleep(0.01)
            return 0, False, False, False, 0.0

        real_popen = subprocess.Popen

        def popen_router(cmd, **kwargs):
            # only the planner spawn is faked - taskkill (kill_tree's
            # Windows path) and probes spawn for real
            if isinstance(cmd, list) and "--prompt-file" in cmd:
                return FakeProc(cmd, **kwargs)
            return real_popen(cmd, **kwargs)

        with mock.patch.object(conductor.subprocess, "Popen",
                               popen_router), \
                mock.patch.object(conductor, "wait_worker", fake_wait):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs, ["devin"])
            deadline = time.time() + 10
            while rec["id"] not in conductor._GEN_PROCS \
                    and time.time() < deadline:
                time.sleep(0.02)
            self.assertIs(conductor._GEN_PROCS.get(rec["id"]), procs[0])
            out = conductor.stop_generation(rec["id"], self.requests,
                                            self.runs)
            self.assertEqual(out["status"], "draft")
            self.wait_attempts()
        self.assertTrue(procs[0]._dead)      # the sentinel kill landed
        self.assertNotIn(rec["id"], conductor._GEN_PROCS)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "draft")   # stopped, not 'error'

    def test_stopped_attempt_cannot_overwrite_a_later_attempt(self):
        rec = self.new()
        flag = self.cleanup_flag(rec["id"])
        gate1, gate2 = threading.Event(), threading.Event()
        calls = []

        def fake(r, projects, argv, log_path=None, fix_note=None, qa=None):
            calls.append(len(calls))
            (gate1 if len(calls) == 1 else gate2).wait(30)
            return (self.proposal("old plan") if len(calls) == 1
                    else self.proposal("new plan"))

        with mock.patch.object(conductor, "_planner_response", fake):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs, ["devin"])
            self.wait_calls(calls, 1)
            conductor.stop_generation(rec["id"], self.requests, self.runs)
            self.assertTrue(flag.is_file())
            # editable immediately - attempt 1 is still unwinding
            out = conductor.save_request(
                rec["id"], {"raw": "Edited: tidy AND test the catalog"},
                self.requests, self.manifests, self.runs)
            self.assertEqual(out["status"], "draft")
            # a fresh attempt clears the old sentinel before spawning
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs, ["devin"])
            self.assertFalse(flag.is_file())
            self.wait_calls(calls, 2)
            gate1.set()          # stale attempt unwinds over the live slot
            self.wait_attempts(1)          # only attempt 2 remains
            gate2.set()
            self.wait_attempts()
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["description"], "new plan")
        self.assertEqual(rec["raw"], "Edited: tidy AND test the catalog")
        self.assertIn("new plan", Path(rec["manifest"]).read_text(
            encoding="utf-8"))

    def test_repeated_and_idle_stops_are_409(self):
        rec = self.new()
        self.cleanup_flag(rec["id"])
        with self.assertRaises(conductor.RequestError) as e:
            conductor.stop_generation(rec["id"], self.requests, self.runs)
        self.assertEqual(e.exception.status, 409)
        gate = threading.Event()

        def fake(r, projects, argv, log_path=None, fix_note=None, qa=None):
            gate.wait(30)
            return self.proposal()

        with mock.patch.object(conductor, "_planner_response", fake):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs, ["devin"])
            conductor.stop_generation(rec["id"], self.requests, self.runs)
            with self.assertRaises(conductor.RequestError) as e:
                conductor.stop_generation(rec["id"], self.requests,
                                          self.runs)
            self.assertEqual(e.exception.status, 409)
            gate.set()
            self.wait_attempts()
        # a finished generation 409s the same way - nothing left to cancel
        with mock.patch.object(conductor, "_planner_response",
                               return_value=self.proposal()):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs, ["devin"],
                                       background=False)
        with self.assertRaises(conductor.RequestError):
            conductor.stop_generation(rec["id"], self.requests, self.runs)

    def test_planner_response_raises_generation_stopped_on_flag(self):
        rec = self.new()
        flag = self.cleanup_flag(rec["id"])

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                self.pid = 1

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("planner", timeout or 0)

            def kill(self):
                pass

        def fake_wait(proc, log, timeout_s, stop_flags=(), timeout_flag=None,
                      stall_s=None):
            conductor._flag_raise(flag.name)
            return -9, False, False, True, 0.0

        with mock.patch.object(conductor.subprocess, "Popen", FakeProc), \
                mock.patch.object(conductor, "wait_worker", fake_wait):
            with self.assertRaises(conductor.GenerationStopped):
                conductor._planner_response(rec, {}, ["devin"])
        self.assertNotIn(rec["id"], conductor._GEN_PROCS)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_dashboard_shows_stop_while_generating(self):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={}, posts=[];
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',value:''}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,fn)=>{handlers[ev]=fn;}};
const fetch=async(path,opts)=>{
  if (path==='/api/requests/r1/stop'){posts.push(path);
    return {ok:true,status:202,json:async()=>({id:'r1',status:'draft'})};}
  if (path.startsWith('/api/state')) return {ok:true,status:200,
    json:async()=>({serve_boot:'b',generated_at:'x',agents:[],runs:[],
      project_options:[],defaults:{model:'swe-2',effort:'max'}})};
  return {ok:true,status:200,
    json:async()=>({id:'r1',status:'draft',raw:'tidy',attachments:[]})};};
const ctx=vm.createContext({document,location:{reload:()=>{}},
  setTimeout:()=>{},fetch,confirm:()=>true,alert:()=>{},
  Event:class Event{constructor(){}}});
vm.runInContext(src,ctx);
vm.runInContext('modalRequest={id:"r1",status:"generating",dirty:false,' +
  'originalManifest:"",record:{id:"r1",status:"generating",raw:"tidy"}};' +
  'showRequestForm({id:"r1",status:"generating",raw:"tidy"})',ctx);
assert.match(elem('dbody').innerHTML,/data-action="stop"/);
assert.match(elem('dbody').innerHTML,/Stop proposal/);
(async()=>{
  await handlers.click({target:{dataset:{action:'stop'}}});
  assert.ok(posts.some(p=>p==='/api/requests/r1/stop'));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class RetainProposalSessionTests(unittest.TestCase):
    """The create-proposal session is kept as 'step 0' preparation history
    on the plan's task list. _finish_generation banks each attempt's
    session id + terminal status (plan_sessions) and marks the producer
    (plan_gen_id); collect_dashboard re-emits them every poll for a ready
    plan AND for its launched run. They are never runnable: launch,
    retry and resume operate on manifest steps/state.json only."""

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

    def proposal(self, description="Tidy the catalog"):
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
'''

    def new(self):
        return conductor.create_request(
            {"raw": "Please tidy the catalog carefully", "project": "proj",
             "model": "swe-2", "effort": "max"}, self.requests, self.runs)

    def cleanup_flag(self, rid):
        flag = conductor.gen_stop_flag(rid)
        self.addCleanup(lambda: flag.unlink(missing_ok=True))
        return flag

    def wait_attempts(self, n=0, timeout=10):
        deadline = time.time() + timeout
        while len(conductor._GENERATING) != n and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(conductor._GENERATING), n)

    def planner_popen(self, calls):
        """popen_router + FakeProc: the --prompt-file spawn writes the
        ATIF export (with the session id the real CLI stamps) and the
        manifest to stdout; git/devin-list probes spawn for real."""
        proposal = self.proposal()
        real_popen = subprocess.Popen

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                self.pid = 1
                self._dead = False
                calls.append(self)
                exp = Path(cmd[cmd.index("--export") + 1])
                exp.write_text(json.dumps({
                    "session_id": f"sess-plan-{len(calls)}",
                    "final_metrics": {"total_prompt_tokens": 10}}))
                kwargs["stdout"].write((
                    "BEGIN_MANIFEST\n" + proposal +
                    "END_MANIFEST\nSTATUS: done\n").encode())
                kwargs["stdout"].flush()

            def poll(self):
                return 0 if self._dead else None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self._dead = True

        def popen_router(cmd, **kwargs):
            if isinstance(cmd, list) and "--prompt-file" in cmd:
                return FakeProc(cmd, **kwargs)
            return real_popen(cmd, **kwargs)
        return popen_router

    def test_published_plan_keeps_planner_session_as_step0(self):
        rec = self.new()
        calls = []
        with mock.patch.object(conductor.subprocess, "Popen",
                               self.planner_popen(calls)), \
                mock.patch.object(conductor, "session_ids",
                                  return_value=set()), \
                mock.patch.object(
                    conductor, "wait_worker",
                    return_value=(0, False, False, False, 0.0)):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")
        # the attempt's terminal record is persisted on the request json -
        # the association survives a serve restart/UI reload
        self.assertEqual(len(rec["plan_sessions"]), 1)
        entry = rec["plan_sessions"][0]
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["session_id"], "sess-plan-1")
        self.assertEqual(rec["plan_gen_id"], entry["gen_id"])
        on_disk = json.loads(conductor._request_path(
            rec["id"], self.requests).read_text(encoding="utf-8"))
        self.assertEqual(on_disk["plan_sessions"][0]["session_id"],
                         "sess-plan-1")
        self.assertEqual(on_disk["plan_gen_id"], entry["gen_id"])
        # a fresh collect_dashboard (what a reload does) rebuilds the row
        # under the plan's own run_key, as step 0 of its task list
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        by_step = {a["step"]: a for a in d["agents"]}
        prep = by_step["plan-proposal"]
        self.assertEqual(prep["run_key"], f"request:{rec['id']}")
        self.assertEqual(prep["status"], "done")
        self.assertEqual(prep["session_id"], "sess-plan-1")
        self.assertEqual(prep["seq"], -1)
        self.assertIsNone(prep["run_dir"])        # history, not work
        self.assertIn("prepared this plan", prep["note"])
        self.assertFalse(prep["prompt_editable"])
        self.assertEqual(set(by_step) - {"plan-proposal"}, {"organize"})
        self.assertEqual(by_step["organize"]["status"], "planned")
        self.assertEqual(by_step["organize"]["seq"], 0)

    def test_stop_then_regenerate_attributes_each_attempt_to_itself(self):
        rec = self.new()
        self.cleanup_flag(rec["id"])
        calls = []
        proposal = self.proposal()
        real_popen = subprocess.Popen

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                self.pid = 1
                self._dead = False
                calls.append(self)
                n = len(calls)
                exp = Path(cmd[cmd.index("--export") + 1])
                exp.write_text(json.dumps(
                    {"session_id": f"sess-{n}"}))
                if n == 2:          # only the second attempt emits a TOML
                    kwargs["stdout"].write((
                        "BEGIN_MANIFEST\n" + proposal +
                        "END_MANIFEST\nSTATUS: done\n").encode())
                    kwargs["stdout"].flush()

            def poll(self):
                return 0 if self._dead else None

            def wait(self, timeout=None):
                if self._dead:
                    return 0
                raise subprocess.TimeoutExpired("planner", timeout or 0)

            def kill(self):
                self._dead = True

        def popen_router(cmd, **kwargs):
            if isinstance(cmd, list) and "--prompt-file" in cmd:
                return FakeProc(cmd, **kwargs)
            return real_popen(cmd, **kwargs)

        def fake_wait(proc, log, timeout_s, stop_flags=(),
                      timeout_flag=None, stall_s=None):
            if len(calls) == 1:      # attempt 1 dies on the stop sentinel
                for _ in range(500):
                    if stop_flags and conductor._flag_set(*stop_flags):
                        proc.kill()
                        return proc.wait(), False, False, True, 0.0
                    if proc.poll() is not None:
                        return 0, False, False, False, 0.0
                    time.sleep(0.01)
            return 0, False, False, False, 0.0

        with mock.patch.object(conductor.subprocess, "Popen",
                               popen_router), \
                mock.patch.object(conductor, "session_ids",
                                  return_value=set()), \
                mock.patch.object(conductor, "wait_worker", fake_wait):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"])
            deadline = time.time() + 10
            while not calls and time.time() < deadline:
                time.sleep(0.02)
            conductor.stop_generation(rec["id"], self.requests, self.runs)
            self.wait_attempts()
            # attempt 1 ended stopped with its own session banked
            rec = conductor.request_details(rec["id"], self.requests,
                                            self.manifests)
            self.assertEqual(rec["status"], "draft")
            self.assertEqual(len(rec["plan_sessions"]), 1)
            self.assertEqual(rec["plan_sessions"][0]["status"], "stopped")
            self.assertEqual(rec["plan_sessions"][0]["session_id"],
                             "sess-1")
            # attempt 2 publishes - the producer association moves to it
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(len(rec["plan_sessions"]), 2)
        old, new = rec["plan_sessions"]
        self.assertEqual((old["status"], old["session_id"]),
                         ("stopped", "sess-1"))
        self.assertEqual((new["status"], new["session_id"]),
                         ("done", "sess-2"))
        self.assertEqual(rec["plan_gen_id"], new["gen_id"])
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        by_step = {a["step"]: a for a in d["agents"]}
        # 'plan-proposal' is THIS plan's producer - never attempt 1
        self.assertEqual(by_step["plan-proposal"]["session_id"], "sess-2")
        self.assertEqual(by_step["plan-proposal"]["seq"], -1)
        self.assertEqual(by_step["plan-proposal-1"]["status"], "stopped")
        self.assertEqual(by_step["plan-proposal-1"]["session_id"],
                         "sess-1")
        self.assertIsNone(by_step["plan-proposal-1"]["seq"])

    def test_launch_runs_step1_and_keeps_prep_history_not_runnable(self):
        rec = self.new()
        calls = []
        with mock.patch.object(conductor.subprocess, "Popen",
                               self.planner_popen(calls)), \
                mock.patch.object(conductor, "session_ids",
                                  return_value=set()), \
                mock.patch.object(
                    conductor, "wait_worker",
                    return_value=(0, False, False, False, 0.0)):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(len(calls), 1)
        # launch spawns the real conductor; the run executes the manifest
        # starting at step 1 - proposal generation is never re-invoked
        args = argparse.Namespace(manifest=rec["manifest"], resume=None,
                                  devin=MOCK, runs_dir=str(self.runs),
                                  request_id=rec["id"], hub=False,
                                  keep_awake=False)
        self.assertEqual(conductor.cmd_run(args), 0)
        self.assertEqual(len(calls), 1)      # no planner respawn
        run_dir = next(self.runs.glob("*/state.json"))
        st = json.loads(run_dir.read_text(encoding="utf-8"))
        self.assertEqual(set(st["steps"]), {"organize"})
        self.assertEqual(st["steps"]["organize"]["status"], "done")
        # the project's session registry proves only step 1 spawned
        spawned = json.loads((self.proj / ".mock_sessions.json").read_text(
            encoding="utf-8"))
        self.assertEqual(len(spawned), 1)
        # the plan's task list still shows the planner session - now
        # attached to the real run under the same request:<id> key
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        by_step = {a["step"]: a for a in d["agents"]
                   if a["run_key"] == f"request:{rec['id']}"}
        self.assertEqual(by_step["organize"]["status"], "done")
        self.assertEqual(by_step["plan-proposal"]["status"], "done")
        self.assertEqual(by_step["plan-proposal"]["session_id"],
                         "sess-plan-1")
        run = next(r for r in d["runs"] if r.get("request_id") == rec["id"])
        self.assertEqual(run["counts"], {"done": 1})
        # resume sees only manifest steps - prep history is never
        # selected as runnable work
        args2 = argparse.Namespace(manifest=rec["manifest"],
                                   resume=str(run_dir.parent), devin=MOCK,
                                   runs_dir=str(self.runs),
                                   request_id=rec["id"], hub=False,
                                   keep_awake=False)
        self.assertEqual(conductor.cmd_run(args2), 0)
        self.assertEqual(len(calls), 1)
        st2 = json.loads(run_dir.read_text(encoding="utf-8"))
        self.assertEqual(set(st2["steps"]), {"organize"})

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_prep_row_is_nav_chain_step0(self):
        """The producer row (seq=-1) leads the task modal's prev/next
        pipeline - 'conceptually step 0': step 1's prev names it."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,
  {innerHTML:'',hidden:true,textContent:'',scrollTop:9});
  return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const ctx=vm.createContext({document,location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'2026-10-16T00:00:00',agents:[
  {step:'s1',run:'r',run_dir:null,run_key:'request:x',slug:'p',
   status:'done',task:'t1',seq:0},
  {step:'s2',run:'r',run_dir:null,run_key:'request:x',slug:'p',
   status:'planned',task:'t2',seq:1},
  {step:'plan-proposal',run:'r',run_dir:null,run_key:'request:x',slug:'p',
   status:'done',task:'planner',seq:-1,session_id:'sess-1'},
  {step:'plan-proposal-1',run:'r',run_dir:null,run_key:'request:x',
   slug:'p',status:'stopped',task:'older attempt',session_id:'sess-0'}],
  runs:[{run:'r',dir:'',key:'request:x',counts:{done:2,planned:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const open=step=>vm.runInContext(
  'showDetail(AGENTS.findIndex(a=>a.step==='+JSON.stringify(step)+'))',ctx);
(async()=>{
  await open('s1');
  let body=elem('dbody').innerHTML;
  assert.match(body,/task 2 of 3/);                       // step0 + s1 + s2
  assert.match(body,/data-nav="request:x\|plan-proposal"/);  // prev = step 0
  await open('plan-proposal');
  body=elem('dbody').innerHTML;
  assert.match(body,/task 1 of 3/);
  assert.match(body,/data-nav="request:x\|s1"/);          // next = step 1
  assert.match(body,/sess-1/);                            // session link kept
  // the numbered non-producer attempt stays out of the chain
  await open('plan-proposal-1');
  assert.doesNotMatch(elem('dbody').innerHTML,/class="dnav"/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class ProposalFlowIntegrationTests(unittest.TestCase):
    """The three proposal-flow changes exercised as ONE sequence:
    prepare -> Stop while preparing -> inputs editable again -> add info
    -> prepare again -> the completed create-proposal session stays on
    the plan's related list -> Start runs the real step 1 (never a
    generation replay). Plus the regression that used to break it: a
    stop landing in the same wall-clock second as generation start
    slipped _disk_untouched's updated_at check and let the killed
    attempt's TOML publish over the restored record."""

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

    def proposal(self, description="Tidy the catalog"):
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
'''

    def new(self):
        return conductor.create_request(
            {"raw": "Please tidy the catalog carefully", "project": "proj",
             "model": "swe-2", "effort": "max"}, self.requests, self.runs)

    def cleanup_flag(self, rid):
        flag = conductor.gen_stop_flag(rid)
        self.addCleanup(lambda: flag.unlink(missing_ok=True))
        return flag

    def wait_attempts(self, n=0, timeout=10):
        deadline = time.time() + timeout
        while len(conductor._GENERATING) != n and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(conductor._GENERATING), n)

    def wait_calls(self, calls, n, timeout=10):
        deadline = time.time() + timeout
        while len(calls) < n and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(calls), n)

    def planner_popen(self, calls, proposal, emit_from=2):
        """--prompt-file spawns get a FakeProc that writes the ATIF export
        (session id like the real CLI stamps) and, from call `emit_from`,
        the manifest; every other spawn (taskkill, git, devin list probes)
        is a real process."""
        real_popen = subprocess.Popen

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                self.pid = 1
                self._dead = False
                self._done = False
                calls.append(self)
                n = len(calls)
                exp = Path(cmd[cmd.index("--export") + 1])
                exp.write_text(json.dumps(
                    {"session_id": f"sess-plan-{n}",
                     "final_metrics": {"total_prompt_tokens": 10}}))
                if n >= emit_from:
                    kwargs["stdout"].write((
                        "BEGIN_MANIFEST\n" + proposal +
                        "END_MANIFEST\nSTATUS: done\n").encode())
                    kwargs["stdout"].flush()
                    self._done = True

            def poll(self):
                return 0 if (self._dead or self._done) else None

            def wait(self, timeout=None):
                if self._dead or self._done:
                    return 0
                raise subprocess.TimeoutExpired("planner", timeout or 0)

            def kill(self):
                self._dead = True

        def popen_router(cmd, **kwargs):
            if isinstance(cmd, list) and "--prompt-file" in cmd:
                return FakeProc(cmd, **kwargs)
            return real_popen(cmd, **kwargs)
        return popen_router

    def stop_aware_wait(self, proc, log, timeout_s, stop_flags=(),
                        timeout_flag=None, stall_s=None):
        """wait_worker stand-in that honors the stopgen sentinel like the
        real one: flag -> kill the proc -> stopped=True."""
        deadline = time.time() + 10
        while time.time() < deadline:
            if stop_flags and conductor._flag_set(*stop_flags):
                proc.kill()
                return proc.wait(), False, False, True, 0.0
            rc = proc.poll()
            if rc is not None:
                return rc, False, False, False, 0.0
            time.sleep(0.01)
        return 0, False, False, False, 0.0

    def test_stop_edit_reprepare_retain_then_launch_runs_step1(self):
        """The full user-facing sequence across all three changes."""
        rec = self.new()
        self.cleanup_flag(rec["id"])
        calls = []
        proposal = self.proposal("Tidy and test the catalog")

        with mock.patch.object(conductor.subprocess, "Popen",
                               self.planner_popen(calls, proposal)), \
                mock.patch.object(conductor, "session_ids",
                                  return_value=set()), \
                mock.patch.object(conductor, "wait_worker",
                                  self.stop_aware_wait):
            # 1. prepare a proposal (attempt 1 blocks on the stop sentinel)
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs, ["devin"])
            self.wait_calls(calls, 1)
            # 2. stop while preparing -> previous inputs editable at once:
            #    the record reads 'draft' immediately even though the
            #    attempt is still unwinding (stale 'generating' overlay is
            #    dropped, not just out-voted later)
            out = conductor.stop_generation(rec["id"], self.requests,
                                            self.runs)
            self.assertEqual(out["status"], "draft")
            now = conductor.request_details(rec["id"], self.requests,
                                            self.manifests, self.runs)
            self.assertEqual(now["status"], "draft")
            # 3. add information while attempt 1 is still unwinding
            out = conductor.save_request(
                rec["id"],
                {"raw": "Please tidy the catalog carefully AND run tests"},
                self.requests, self.manifests, self.runs)
            self.assertEqual(out["status"], "draft")
            self.wait_attempts()
            # the stopped attempt is banked as preparation history, and
            # its late exit could not clobber the edited record
            rec = conductor.request_details(rec["id"], self.requests,
                                            self.manifests, self.runs)
            self.assertEqual(rec["status"], "draft")
            self.assertIsNone(rec["manifest"])
            self.assertFalse(list(self.manifests.iterdir()))
            self.assertEqual(
                rec["raw"], "Please tidy the catalog carefully AND run tests")
            self.assertEqual(len(rec["plan_sessions"]), 1)
            self.assertEqual(rec["plan_sessions"][0]["status"], "stopped")
            self.assertEqual(rec["plan_sessions"][0]["session_id"],
                             "sess-plan-1")
            # 4. prepare again - the edited text is what gets planned
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)

        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests, self.runs)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["description"], "Tidy and test the catalog")
        # 5. reload persistence: a fresh read from disk keeps BOTH
        #    attempts, producer = the second (completed) session
        on_disk = json.loads(conductor._request_path(
            rec["id"], self.requests).read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk["plan_sessions"]), 2)
        old, new = on_disk["plan_sessions"]
        self.assertEqual((old["status"], old["session_id"]),
                         ("stopped", "sess-plan-1"))
        self.assertEqual((new["status"], new["session_id"]),
                         ("done", "sess-plan-2"))
        self.assertEqual(on_disk["plan_gen_id"], new["gen_id"])
        # 6. the completed create-proposal session is on the plan's related
        #    list, as step 0 - the stopped attempt keeps its own row
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        by_step = {a["step"]: a for a in d["agents"]}
        self.assertEqual(by_step["plan-proposal"]["session_id"],
                         "sess-plan-2")
        self.assertEqual(by_step["plan-proposal"]["seq"], -1)
        self.assertEqual(by_step["plan-proposal-1"]["status"], "stopped")
        self.assertEqual(by_step["plan-proposal-1"]["session_id"],
                         "sess-plan-1")
        self.assertIsNone(by_step["plan-proposal"]["run_dir"])
        # 7. start the plan - the run executes the real step 1; proposal
        #    generation is never replayed (still exactly 2 planner spawns)
        args = argparse.Namespace(manifest=rec["manifest"], resume=None,
                                  devin=MOCK, runs_dir=str(self.runs),
                                  request_id=rec["id"], hub=False,
                                  keep_awake=False)
        self.assertEqual(conductor.cmd_run(args), 0)
        self.assertEqual(len(calls), 2)
        run_dir = next(self.runs.glob("*/state.json"))
        st = json.loads(run_dir.read_text(encoding="utf-8"))
        self.assertEqual(set(st["steps"]), {"organize"})
        self.assertEqual(st["steps"]["organize"]["status"], "done")
        # the launched run's task list still shows the preparation row
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        by_step = {a["step"]: a for a in d["agents"]
                   if a["run_key"] == f"request:{rec['id']}"}
        self.assertEqual(by_step["organize"]["status"], "done")
        self.assertEqual(by_step["plan-proposal"]["session_id"],
                         "sess-plan-2")
        self.assertEqual(by_step["plan-proposal-1"]["status"], "stopped")

    def test_same_second_stop_cannot_publish_late_toml(self):
        """Regression: _disk_untouched compared only updated_at, a
        SECOND-granularity stamp - a stop_generation restore landing in
        the same second as generation start passed the gate and the
        killed attempt's TOML published over the restored record. With
        now_iso pinned to one value the write is indistinguishable by
        updated_at alone; the mtime_ns + generation_stopped_at compares
        still catch it."""
        gate = threading.Event()

        def fake(r, projects, argv, log_path=None, fix_note=None,
                 qa=None):
            gate.wait(30)
            return self.proposal("late TOML from a stopped attempt")

        frozen = "2026-10-16T03:00:00"
        # Frozen from request creation on: file updated_at, job.json's
        # base_updated_at and the stop's rewrite all carry the same
        # timestamp - the old updated_at-only gate's blind spot.
        with mock.patch.object(conductor, "now_iso",
                               return_value=frozen), \
                mock.patch.object(conductor, "_planner_response", fake):
            rec = self.new()
            self.cleanup_flag(rec["id"])
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs, ["devin"])
            spool = conductor._spool_dir(rec["id"])
            deadline = time.time() + 10
            while not (spool / "job.json").is_file() \
                    and time.time() < deadline:
                time.sleep(0.02)
            job = conductor._job_meta(spool)
            self.assertEqual(job.get("base_updated_at"), frozen)
            out = conductor.stop_generation(rec["id"], self.requests,
                                            self.runs)
            self.assertEqual(out["status"], "draft")
            gate.set()
            self.wait_attempts()
        rec = conductor.request_details(rec["id"], self.requests,
                                        self.manifests, self.runs)
        self.assertEqual(rec["status"], "draft")
        self.assertIsNone(rec["manifest"])
        self.assertFalse(list(self.manifests.iterdir()))

    def test_disk_untouched_gate_unit(self):
        """Unit-pins the hardened gate: identical updated_at, but a stop
        stamp / mtime change / outright rewrite all count as touched."""
        rec = self.new()
        path = conductor._request_path(rec["id"], self.requests)
        base = conductor._read_request_file(rec["id"], self.requests)
        job = {"base_updated_at": base["updated_at"],
               "base_stopped_at": None,
               "base_mtime_ns": path.stat().st_mtime_ns}
        self.assertTrue(conductor._disk_untouched(
            rec["id"], self.requests, job))
        frozen = base["updated_at"]          # same-second collision
        with mock.patch.object(conductor, "now_iso", return_value=frozen):
            stop = dict(base)
            stop["status"] = "draft"
            stop["generation_stopped_at"] = frozen
            conductor._save_request(stop, self.requests)
        self.assertFalse(conductor._disk_untouched(
            rec["id"], self.requests, job))


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

    def test_dead_worker_pid_stays_live_while_driver_runs(self):
        """The verify/merge window: the worker exited but the conductor
        is mid-finalize - driver_pid keeps 'running' alive instead of
        flashing 'lost'. Hit live on ux-controls 2026-10-15."""
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        drv = subprocess.Popen([sys.executable, "-c",
                                "import time;time.sleep(30)"])
        try:
            st = self._state(
                {"a": {"status": "running", "pid": dead.pid,
                       "started_at": conductor.now_iso()}},
                driver_pid=drv.pid,
                driver_started_at=conductor.now_iso())
            self.assertTrue(conductor.driver_alive(st))
            self.assertEqual(conductor.effective_statuses(st)["a"],
                             "running")
            # a queued sibling isn't an orphan while the driver is up
            st["steps"]["b"] = {"status": "queued"}
            st["ended_at"] = conductor.now_iso()  # even a ghost ended_at
            self.assertEqual(conductor.effective_statuses(st)["b"],
                             "queued")
        finally:
            drv.terminate()
            drv.wait()

    def test_dead_driver_falls_back_to_worker_liveness(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        st = self._state(
            {"a": {"status": "running", "pid": dead.pid,
                   "started_at": conductor.now_iso()}},
            driver_pid=dead.pid,
            driver_started_at=conductor.now_iso())
        self.assertFalse(conductor.driver_alive(st))
        self.assertEqual(conductor.effective_statuses(st)["a"], "lost")

    def test_old_runs_without_driver_pid_keep_old_semantics(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        st = self._state(
            {"a": {"status": "running", "pid": dead.pid,
                   "started_at": conductor.now_iso()}})
        self.assertFalse(conductor.driver_alive(st))
        self.assertEqual(conductor.effective_statuses(st)["a"], "lost")


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

    def test_sweep_never_touches_a_run_whose_driver_is_alive(self):
        """Regression: a serve-restart sweep stamped 'lost' + ended_at on
        a LIVE run while its conductor sat in the 5min verify gate with a
        dead worker pid (ux-controls, 2026-10-15)."""
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            rd = runs / "20990101-000000-t"
            rd.mkdir(parents=True)
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait()
            drv = subprocess.Popen([sys.executable, "-c",
                                    "import time;time.sleep(30)"])
            try:
                (rd / "state.json").write_text(json.dumps(
                    {"run": "t", "started_at": conductor.now_iso(),
                     "driver_pid": drv.pid,
                     "driver_started_at": conductor.now_iso(),
                     "steps": {
                         "a": {"status": "running", "pid": dead.pid,
                               "started_at": conductor.now_iso()},
                         "b": {"status": "queued"}}}))
                lines = conductor.sweep_runs(runs)
                self.assertFalse(any("lost" in l for l in lines))
                out = json.loads((rd / "state.json").read_text())
                self.assertEqual(out["steps"]["a"]["status"], "running")
                self.assertNotIn("ended_at", out)
            finally:
                drv.terminate()
                drv.wait()


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
// compact density: bare billed-in/out/cached + turns (in = prompt -
// cached, the billed part - Desktop's "input" number)
assert.strictEqual(vm.runInContext(
  "fmtTc({prompt:10000,completion:1000,cached:6000,llm_steps:2})",ctx),
  '4k/1k/6k 2L');
const click=async(t)=>handlers.click({target:t,preventDefault:()=>{}});
(async()=>{
  // clicking 'running' shows ONLY running agents and ONLY their run
  // (the task column is modal-only now - step names tell rows apart)
  await click({dataset:{st:'running'}});
  assert.match(elem('agents').innerHTML,/>alpha</);
  assert.doesNotMatch(elem('agents').innerHTML,/>beta</);
  assert.doesNotMatch(elem('agents').innerHTML,/>gamma</);
  assert.match(elem('runs').innerHTML,/run one/);
  assert.doesNotMatch(elem('runs').innerHTML,/run two/);
  assert.match(elem('filters').innerHTML,/chip active/);
  // clicking again returns to showing everything
  await click({dataset:{st:'running'}});
  assert.match(elem('agents').innerHTML,/>gamma</);
  assert.match(elem('runs').innerHTML,/run two/);
  // multi-select: done + running keeps both runs
  await click({dataset:{st:'done'}});
  await click({dataset:{st:'running'}});
  assert.match(elem('agents').innerHTML,/>alpha</);
  assert.match(elem('agents').innerHTML,/>gamma</);
  await click({dataset:{reset:''}});
  assert.match(elem('runs').innerHTML,/run two/);
  assert.match(elem('agents').innerHTML,/>gamma</);
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

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_regenerating_proposal_shows_planning_not_needs_review(self):
        """Regression: 'stale' survives a Regenerate click until the new
        proposal publishes - a mid-planning row must wear only the
        'Preparing proposal' chip, not 'Needs review' on top of it (the
        flag means something again only once planning stops)."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,textContent:''}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,querySelectorAll:()=>[],
  addEventListener:(event,handler)=>{handlers[event]=handler;}};
const reqs={
  regen:{id:'regen',status:'generating',run:'regen-run',label:'regen',raw:'r',
    project:'p',model:'swe-2',effort:'max',manifest:'manifests/m.toml',
    manifest_text:'[run]',stale:true,
    proposed_steps:[{step:'s',slug:'p',model:'m',effort:'e',task:'t'}],
    generation_started_at:'2026-10-14T00:00:00'},
  parked:{id:'parked',status:'questions',run:'parked-run',label:'parked',
    raw:'r',project:'p',model:'swe-2',effort:'max',manifest:'manifests/m.toml',
    manifest_text:'[run]',stale:true,
    pending_questions:[{qid:'q1',header:'Scope',question:'pick one'}],
    proposed_steps:[{step:'s',slug:'p',model:'m',effort:'e',task:'t'}]},
  review:{id:'review',status:'ready',run:'review-run',label:'review',raw:'r',
    project:'p',model:'swe-2',effort:'max',manifest:'manifests/m.toml',
    manifest_text:'[run]',stale:true,
    proposed_steps:[{step:'s',slug:'p',model:'m',effort:'e',task:'t'}]}};
const fetch=async path=>({ok:true,json:async()=>reqs[path.split('/').pop()]});
const ctx=vm.createContext({document,location:{reload:()=>{}},setTimeout:()=>{},fetch});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'2026-10-14T00:00:00',agents:[],runs:[
  {run:'regen-run',key:'request:regen',request_id:'regen',stage:'generating',
   stale:true,counts:{planned:1}},
  {run:'parked-run',key:'request:parked',request_id:'parked',stage:'questions',
   stale:true,counts:{planned:1}},
  {run:'review-run',key:'request:review',request_id:'review',stage:'ready',
   stale:true,counts:{planned:1}},
  {run:'failed-run',key:'request:failed',request_id:'failed',stage:'error',
   stale:true,counts:{planned:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const rows=elem('runs').innerHTML.split('runrow');
const row=name=>rows.find(r=>r.includes('>'+name+'<'));
assert.match(row('regen-run'),/Preparing proposal/);
assert.doesNotMatch(row('regen-run'),/Needs review/);
assert.match(row('parked-run'),/Needs your answers/);
assert.doesNotMatch(row('parked-run'),/Needs review/);
assert.match(row('review-run'),/Needs review/);   // stale + ready: awaiting review
assert.match(row('failed-run'),/Needs review/);   // planning failed, stale stays
(async()=>{
  await vm.runInContext('openRequest("regen")',ctx);
  assert.match(elem('dbody').innerHTML,/Planning in progress/);
  // the pending note stays silent mid-planning - the change is being
  // regenerated already (a legacy rec's lumped stale flag reports as the
  // opaque 'fields' marker, so it must not leak in here either)
  assert.equal(elem('pend-note').hidden,true);
  await vm.runInContext('openRequest("review")',ctx);
  assert.match(elem('pend-note').innerHTML,/Request fields changed/);
  assert.equal(elem('pend-note').hidden,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_pending_notice_buttons_and_revert(self):
        """The pending note, Apply and Start run follow a live field-vs-
        baseline diff: an option change enables Apply (and names the
        fields), a generation-input change disables it ('regenerate
        first'), reverting to the baseline clears the mark, and Start run
        stays disabled while anything is pending."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,
  {innerHTML:'',hidden:true,textContent:'',value:'',checked:false,
   disabled:false,title:''}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const BASE={raw:'tidy it',project:'proj',attachments:[],model:'swe-2',
  effort:'max',on_failure:'continue',max_attempts:3,worktrees:false,
  merge_resolver:false,followup:false};
const reqs={
  review:{id:'review',status:'ready',run:'review-run',label:'review',
    raw:'tidy it',project:'proj',model:'swe-2',effort:'max',
    on_failure:'continue',max_attempts:3,ask:false,plan_model:'',
    plan_effort:'',attachments:[],
    manifest:'manifests/m.toml',manifest_text:'[run]',
    pending:{gen:[],opts:[]},baseline:BASE,
    proposed_steps:[{step:'s',slug:'proj',model:'swe-2',effort:'max',
      task:'t'}]},
  state:{serve_boot:'b',generated_at:'x',agents:[],runs:[]}};
const calls=[];
const fetch=async(path,opt)=>{calls.push(path);
  return {ok:true,json:async()=>reqs[path.split('/').pop()]||{id:'x'}};};
const ctx=vm.createContext({document,location:{reload:()=>{}},
  setTimeout:()=>{},fetch,confirm:()=>true});
vm.runInContext(src,ctx);
// the stub DOM does not parse innerHTML into input values - mirror what
// a real form holds for this record
const fill=rec=>{ for (const k of
    ['raw','label','project','model','effort','on_failure','plan_model',
     'plan_effort']) elem('rq-'+k).value=rec[k]||'';
  for (const k of ['followup','worktrees','merge_resolver','ask'])
    elem('rq-'+k).checked=!!rec[k];
  elem('rq-attempts').value=String(rec.max_attempts??3);
  elem('rq-manifest').value=rec.manifest_text||''; };
const type=(id,v)=>{ elem(id).value=v;
  return handlers.input({target:{id}}); };
const check=(id,v)=>{ elem(id).checked=v;
  return handlers.input({target:{id}}); };
(async()=>{
  await vm.runInContext('openRequest("review")',ctx);
  fill(reqs.review);
  await vm.runInContext('refreshPending()',ctx);
  // matching the baselines: no note, Apply neutralized, Start available
  assert.equal(elem('pend-note').hidden,true);
  assert.equal(elem('rq-apply').disabled,true);
  assert.match(elem('rq-apply').title,/nothing to apply/);
  assert.equal(elem('rq-run').disabled,false);
  // Regenerate is a neutral secondary - never the primary Start run look
  assert.match(elem('dbody').innerHTML,
    /id="rq-generate" class="btn secondary"/);
  // an option change -> Apply-able, Start blocked, note names the field
  await type('rq-effort','high');
  assert.match(elem('pend-note').innerHTML,/step effort/);
  assert.match(elem('pend-note').innerHTML,/Apply options to TOML/);
  assert.equal(elem('pend-note').hidden,false);
  assert.equal(elem('rq-apply').disabled,false);
  assert.equal(elem('rq-run').disabled,true);
  // revert to baseline clears the mark live - no save needed
  await type('rq-effort','max');
  assert.equal(elem('pend-note').hidden,true);
  assert.equal(elem('rq-apply').disabled,true);
  assert.equal(elem('rq-run').disabled,false);
  // a checkbox the same: toggle on, toggle off
  await check('rq-worktrees',true);
  assert.match(elem('pend-note').innerHTML,/worktrees/);
  assert.equal(elem('rq-apply').disabled,false);
  await check('rq-worktrees',false);
  assert.equal(elem('pend-note').hidden,true);
  // a generation input -> Regenerate is the only way, Apply neutralized
  await type('rq-raw','widen the scope');
  assert.match(elem('pend-note').innerHTML,/request text/);
  assert.match(elem('pend-note').innerHTML,/Regenerate tasks/);
  assert.equal(elem('rq-apply').disabled,true);
  assert.equal(elem('rq-run').disabled,true);
  // both buckets -> the note explains the order
  await type('rq-effort','high');
  assert.match(elem('pend-note').innerHTML,/Two kinds/);
  // reverting BOTH clears everything again
  await type('rq-raw','tidy it');
  await type('rq-effort','max');
  assert.equal(elem('pend-note').hidden,true);
  assert.equal(elem('rq-run').disabled,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_regenerate_confirms_and_names_unsaved_toml_edits(self):
        """Regenerate replaces generated tasks, so it confirms every time;
        a cancel must not POST anything, and unsaved edits in the TOML
        editor are named in the confirm text so they are not silently
        dropped."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,
  {innerHTML:'',hidden:true,textContent:'',value:'',checked:false,
   disabled:false,title:''}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const reqs={
  review:{id:'review',status:'ready',run:'review-run',label:'review',
    raw:'tidy it',project:'proj',model:'swe-2',effort:'max',
    on_failure:'continue',max_attempts:3,attachments:[],
    manifest:'manifests/m.toml',manifest_text:'[run] x',
    pending:{gen:[],opts:[]},
    baseline:{raw:'tidy it',project:'proj',attachments:[],model:'swe-2',
      effort:'max',on_failure:'continue',max_attempts:3,worktrees:false,
      merge_resolver:false,followup:false},
    proposed_steps:[{step:'s',slug:'proj',model:'swe-2',effort:'max',
      task:'t'}]},
  state:{serve_boot:'b',generated_at:'x',agents:[],runs:[]}};
const calls=[], seen=[];
let allow=false;
const fetch=async(path,opt)=>{calls.push(path);
  return {ok:true,json:async()=>reqs[path.split('/').pop()]||{id:'x'}};};
const confirm=msg=>{seen.push(msg);return allow;};
const ctx=vm.createContext({document,location:{reload:()=>{}},
  setTimeout:()=>{},fetch,confirm});
vm.runInContext(src,ctx);
const fill=rec=>{ for (const k of
    ['raw','label','project','model','effort','on_failure','plan_model',
     'plan_effort']) elem('rq-'+k).value=rec[k]||'';
  for (const k of ['followup','worktrees','merge_resolver','ask'])
    elem('rq-'+k).checked=!!rec[k];
  elem('rq-attempts').value=String(rec.max_attempts??3);
  elem('rq-manifest').value=rec.manifest_text||''; };
(async()=>{
  await vm.runInContext('openRequest("review")',ctx);
  fill(reqs.review);
  calls.length=0;             // the open itself fetches - count POSTs only
  // cancelled confirm: nothing is saved, generated or touched
  await vm.runInContext('requestAction("generate")',ctx);
  assert.equal(calls.length,0);
  assert.match(seen[0],/Regenerate the proposal/);
  assert.doesNotMatch(seen[0],/unsaved TOML edits/);
  // unsaved TOML edits are named before they would be dropped
  elem('rq-manifest').value='[run] hand-edited';
  await vm.runInContext('requestAction("generate")',ctx);
  assert.match(seen[1],/unsaved TOML edits are dropped/);
  assert.equal(calls.length,0);
  // confirmed: fields save first, then the generation endpoint fires
  allow=true;
  await vm.runInContext('requestAction("generate")',ctx);
  assert.deepEqual(calls.slice(0,2),
    ['/api/requests/review/save','/api/requests/review/generate']);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_task_modal_prev_next_pipeline_nav(self):
        """The task modal's sticky bar walks the run's pipeline (`seq` =
        manifest order, not the time-sorted table): prev/next buttons
        name their target, 'task N of M' gives position, and
        ArrowLeft/ArrowRight do the same walk. Single-step pipelines and
        seq-less rows (the planner) get no bar."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,
  {innerHTML:'',hidden:true,textContent:'',scrollTop:9});
  return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const ctx=vm.createContext({document,location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
// AGENTS arrive time-sorted desc - seq (pipeline order), not row order,
// defines the walk: s1 -> s2 -> s3 inside run /r.
const data={serve_boot:'b',generated_at:'2026-10-14T00:00:00',agents:[
  {step:'s1',run:'r',run_dir:'/r',run_key:'/r',slug:'p',status:'done',task:'t1',seq:0},
  {step:'s3',run:'r',run_dir:'/r',run_key:'/r',slug:'p',status:'done',task:'t3',seq:2},
  {step:'s2',run:'r',run_dir:'/r',run_key:'/r',slug:'p',status:'done',task:'t2',seq:1},
  {step:'other',run:'r2',run_dir:'/r2',run_key:'/r2',slug:'q',status:'done',task:'tx',seq:0},
  {step:'plan-proposal',run:'req',run_key:'request:x',slug:'p',status:'running',task:'planner'}],
  runs:[{run:'r',dir:'/r',key:'/r',counts:{done:3}},
        {run:'r2',dir:'/r2',key:'/r2',counts:{done:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const click=async t=>handlers.click({target:t,preventDefault:()=>{}});
const key=async k=>handlers.keydown({key:k,target:{},preventDefault:()=>{}});
const open=step=>vm.runInContext(
  'showDetail(AGENTS.findIndex(a=>a.step==='+JSON.stringify(step)+'))',ctx);
(async()=>{
  await open('s2');                    // middle of the pipeline
  let body=elem('dbody').innerHTML;
  assert.match(body,/p \/ s2/);
  assert.match(body,/task 2 of 3/);
  assert.match(body,/data-nav="\/r\|s1"/);   // prev names its target
  assert.match(body,/data-nav="\/r\|s3"/);   // next names its target
  // clicking prev jumps to s1 - first position disables the prev arrow
  await click({dataset:{},closest:sel=>sel==='[data-nav]'
    ?{dataset:{nav:'/r|s1'}}:null});
  body=elem('dbody').innerHTML;
  assert.match(body,/p \/ s1/);
  assert.match(body,/task 1 of 3/);
  assert.match(body,/class="dnav-btn" disabled>/);
  assert.equal(elem('detail').scrollTop,0);  // nav lands at the task top
  // arrow keys walk the same chain: Right, Right, Right (clamps), Left
  await key('ArrowRight'); assert.match(elem('dbody').innerHTML,/p \/ s2/);
  await key('ArrowRight'); assert.match(elem('dbody').innerHTML,/task 3 of 3/);
  await key('ArrowRight'); assert.match(elem('dbody').innerHTML,/task 3 of 3/);
  await key('ArrowLeft');  assert.match(elem('dbody').innerHTML,/task 2 of 3/);
  // a single-step pipeline gets no bar, and neither does the seq-less
  // planner row (it shares a run_key with its planned steps but is not
  // a pipeline task)
  await open('other');
  assert.doesNotMatch(elem('dbody').innerHTML,/class="dnav"/);
  await open('plan-proposal');
  assert.doesNotMatch(elem('dbody').innerHTML,/class="dnav"/);
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
    def test_compact_genline_live_output(self):
        """Compact density hides the run row's .sub planner-output line,
        so the same gen_last also renders inline as a .genline span -
        display:none at full density, inline under body.compact. It
        reuses the prevTail diff for the .fresh flash: a changed gen_last
        flashes, an unchanged one does not, first paint never flashes."""
        html = conductor._DASH_HTML
        self.assertRegex(html, r'\.genline \{[^}]*display:\s*none')
        self.assertRegex(
            html, r'body\.compact td \.genline \{[^}]*display:\s*inline')
        js = html.split("<script>\n", 1)[1].split("</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,
  {innerHTML:'',hidden:true,textContent:''}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
// the questions auto-popup fetches the request - reject is caught inside
// openQuestions, so a throwaway stub keeps the row renderable
const fetch=async()=>{throw new Error('no net')};
const ctx=vm.createContext({document,fetch,location:{reload:()=>{}},
  setTimeout:()=>{}});
vm.runInContext(src,ctx);
const mk=last=>({serve_boot:'b',generated_at:'2026-10-14T00:00:00',
  agents:[],runs:[
  {run:'req',key:'request:r1',request_id:'r1',stage:'generating',
   gen_last:last,dir:'',counts:{planned:2},projects:['p']},
  {run:'plain',dir:'/plain',counts:{done:1}}]});
const render=d=>vm.runInContext('render('+JSON.stringify(d)+')',ctx);
// first paint: the line carries the planner's latest output; no flash -
// there is nothing to diff against yet
render(mk('reading the repo'));
let runs=elem('runs').innerHTML;
assert.match(runs,/class="genline" title="reading the repo">reading the repo</);
// the same value still renders as the full-view .sub line
assert.match(runs,/class="sub">reading the repo</);
// exactly one genline - the plain run row has none
assert.equal((runs.match(/class="genline/g)||[]).length,1);
// a streamed update flashes both the compact line and the .sub line
render(mk('drafting step two'));
runs=elem('runs').innerHTML;
assert.match(runs,/class="genline fresh"[^>]*>drafting step two</);
assert.match(runs,/class="sub fresh">drafting step two</);
// an unchanged value does not re-flash - bump an unrelated field so the
// render sig differs and the row actually rebuilds
const same=mk('drafting step two'); same.runs[1].counts={done:2};
render(same);
runs=elem('runs').innerHTML;
assert.match(runs,/class="genline" title="drafting step two">/);
assert.doesNotMatch(runs,/sub fresh/);
// a newline-less narration blob keeps only its tail - the fresh end
const blob='x'.repeat(200)+'tail-end';
render(mk(blob));
runs=elem('runs').innerHTML;
assert.match(runs,/class="genline fresh" title="[^"]*tail-end">…x+tail-end</);
// before the first output, the line says the planner is warming up
// (gen_last flipped to null - that change flashes too)
render(mk(null));
assert.match(elem('runs').innerHTML,
  /class="genline[^"]*"[^>]*>planner warming up</);
// a questions-stage row shows the waiting hint in the same slot
const q=mk(null); q.runs[0].stage='questions';
q.runs[0].questions=[{qid:'q0',header:'Scope',question:'Which?'}];
render(q);
assert.match(elem('runs').innerHTML,
  /class="genline[^"]*"[^>]*>planner waits on 1 answer\(s\)</);
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_compact_run_row_labels_stay_visible(self):
        """Regression: two compact-density losses in the runs table.

        (a) Compact hides td <br> and squeezes td.wrap to one ellipsized
        line, so a run row's chips collapsed INLINE after the run name
        and clipped off - intermittently, since it depended on name
        length and column width. The label block is a .rlabels div now:
        a plain block box keeps the chips (+ 'Needs review') on their
        own line under the name in both densities.

        (b) The planner:/steps:/est token lines were .sub spans, hidden
        by the blanket 'td .sub' compact rule even though fmtTc() was
        written as their compact form (dead branch since the compact
        counter change). They carry .toksub now - same look, exempt
        from the hide, block-stacked in compact because their <br> is
        gone - and 'not planned' follows the same rule."""
        html = conductor._DASH_HTML
        self.assertRegex(
            html, r'body\.compact td \.toksub \{[^}]*display:\s*block')
        self.assertRegex(html, r'\.toksub \{[^}]*color:#8b949e')
        self.assertRegex(
            html, r'body\.compact td \.sub \{[^}]*display:\s*none')
        js = html.split("<script>\n", 1)[1].split("</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true}); return nodes.get(id); };
const cls={_s:new Set(),toggle(c,on){on?this._s.add(c):this._s.delete(c)},
  contains(c){return this._s.has(c)}};
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],addEventListener:(ev,h)=>{handlers[ev]=h;},
  body:{classList:cls},documentElement:{style:{setProperty:(k,v)=>{}}}};
const ctx=vm.createContext({document,location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'2026-10-16T00:00:00',agents:[],
runs:[
 {run:'stale-paused',dir:'/sp',aborted:'paused',
  ended_at:'2026-10-15T01:00:00',counts:{done:1,stopped:1},stale:true},
 {run:'tokrun',dir:'/tok',counts:{done:2,running:1},
  started_at:'2026-10-16T00:00:00',
  plan_tokens:{prompt:10000,cached:6000,completion:1000,llm_steps:2},
  plan_cost:{usd:0.02,label:'m',uid:'m'},
  tokens:{prompt:200000,cached:150000,completion:9000,llm_steps:11},
  cost:{usd:1.5,uid:'m',label:'m'}},
 {run:'req',key:'request:r1',request_id:'r1',stage:'generating',
  gen_last:'reading the repo',dir:'',counts:{},stale:true}]};
const render=()=>vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const row=name=>elem('runs').innerHTML.split('runrow')
  .find(r=>r.includes('>'+name+'<'));
render();
let runs=elem('runs').innerHTML;
// (a) the label block is a .rlabels div: the name button sits before
// it, every chip inside it - chips can no longer go inline after the
// name and clip on an ellipsized line
assert.match(runs,/<div class="rlabels">/);
assert.ok(runs.indexOf('run-name') < runs.indexOf('class="rlabels"'));
const sp=row('stale-paused');
assert.match(sp,/class="rlabels">[^]*?class="stage paused"[^>]*>paused - resume ready</);
assert.match(sp,/class="stage error"[^>]*>Needs review</);
// 'Needs review' groups with the chips BEFORE the hidden dir .sub -
// an end-of-row position put it on the compact-clipped part
assert.ok(sp.indexOf('Needs review') < sp.indexOf('>sp<'),
          'stale chip must sit inside the labels line');
// a questions/generating proposal row: the stage chip and genline
// also live inside the labels block
const rq=row('req');
assert.match(rq,/class="rlabels">[^]*?class="stage generating"[^>]*>Preparing proposal/);
assert.match(rq,/class="genline"[^>]*>reading the repo</);
// (b) the token labels carry .toksub, exempt from the compact hide
const tok=row('tokrun');
assert.match(tok,/class="toksub"[^>]*>planner: in 4k · out 1k · cached 6k · 2 turns · \$0\.02</);
assert.match(tok,/class="toksub"[^>]*>steps: in 50k · out 9k · cached 150k · 11 turns · \$1\.50</);
assert.match(tok,/class="toksub"[^>]*>est \$1\.52</);
assert.doesNotMatch(tok,/class="sub"[^>]*>steps:/);
// 'not planned' is a label too - same compact exemption
assert.match(row('req'),/class="toksub">not planned</);
// compact pass: same labels in fmtTc short form (fmtTc finally reaches
// the runs table - it was dead code inside the hidden .sub spans)
vm.runInContext("setDensity('compact')",ctx);
render();
runs=elem('runs').innerHTML;
const tokc=row('tokrun');
assert.match(tokc,/class="toksub"[^>]*>planner: 4k\/1k\/6k 2L · \$0\.02</);
assert.match(tokc,/class="toksub"[^>]*>steps: 50k\/9k\/150k 11L · \$1\.50</);
assert.match(tokc,/class="toksub"[^>]*>est \$1\.52</);
const spc=row('stale-paused');
assert.match(spc,/class="rlabels">[^]*?class="stage paused"[^>]*>paused - resume ready</);
assert.match(spc,/class="stage error"[^>]*>Needs review</);
// back to full: the long labeled form returns, markup unchanged
vm.runInContext("setDensity('full')",ctx);
render();
assert.match(row('tokrun'),/steps: in 50k · out 9k · cached 150k · 11 turns/);
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

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_planner_questionnaire_modal(self):
        """A request parked at stage 'questions' auto-opens the
        questionnaire modal: one question at a time, option toggles +
        free text + Skip; each answer POSTs /answer and the next question
        renders; the last answer closes the modal."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={}, posts=[];
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,
  {innerHTML:'',hidden:true,textContent:'',value:'',focus:()=>{}});
  return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const id='req-q1';
const req={id,status:'questions',label:'Tidy',pending_questions:[
  {qid:'q0',header:'Scope',question:'Which projects?',
   options:[{label:'proj',description:'the catalog'},{label:'other'}]},
  {qid:'q1',header:'',question:'Any constraints?',options:[]}],qa:[]};
const state={serve_boot:'b',generated_at:'2026-10-14',agents:[],runs:[
  {run:'Tidy',key:'request:'+id,request_id:id,stage:'questions',
   questions:req.pending_questions.map(q=>({qid:q.qid,header:q.header,
     question:q.question})),counts:{}}],
  project_options:[],defaults:{model:'swe-2',effort:'max'}};
const fetch=async(path,opt)=>{
  if(opt&&opt.method==='POST'){
    posts.push({path,body:JSON.parse(opt.body)});
    const n=posts.length;
    return {ok:true,json:async()=>({...req,
      status:n<2?'questions':'generating',
      pending_questions:n<2?req.pending_questions.slice(n):[],
      qa:req.pending_questions.slice(0,n).map(q=>({qid:q.qid,
        question:q.question,answer:[],text:'',skipped:true}))})};
  }
  // after the last answer the server flips to 'generating' everywhere
  const st=posts.length<2?'questions':'generating';
  if (path.startsWith('/api/requests/'))
    return {ok:true,json:async()=>({...req,status:st,
      pending_questions:req.pending_questions.slice(
        Math.min(posts.length,2))})};
  return {ok:true,json:async()=>({...state,
    runs:state.runs.map(r=>({...r,stage:st,
      questions:st==='questions'?r.questions:[]}))})};
};
const ctx=vm.createContext({document,location:{reload:()=>{}},
  setTimeout:()=>{},fetch,confirm:()=>true});
vm.runInContext(src,ctx);
vm.runInContext('render('+JSON.stringify(state)+')',ctx);
const click=t=>handlers.click({target:{...t,closest:()=>null},
  preventDefault:()=>{}});
(async()=>{
  await new Promise(r=>setImmediate(r));   // openQuestions' fetch lands
  await new Promise(r=>setImmediate(r));
  const runs=elem('runs').innerHTML;
  assert.match(runs,/Needs your answers/);
  assert.match(runs,/waits on 2 answer/);
  assert.equal(elem('backdrop').hidden,false);
  assert.match(elem('dbody').innerHTML,/Planner question 1 of 2/);
  assert.match(elem('dbody').innerHTML,/data-qopt="proj"/);
  // pick the suggested option, then Send
  await click({dataset:{qopt:'proj'}});
  assert.match(elem('dbody').innerHTML,/qopt on/);
  await click({dataset:{qsend:''}});
  assert.equal(posts[0].path,'/api/requests/'+id+'/answer');
  assert.equal(posts[0].body.qid,'q0');
  assert.deepEqual(posts[0].body.selected,['proj']);
  // second question has no options: free text or skip
  assert.match(elem('dbody').innerHTML,/question 2 of 2/);
  await click({dataset:{qsend:''}});   // empty -> rejected client-side
  assert.equal(posts.length,1);
  await click({dataset:{qskip:''}});
  assert.equal(posts[1].body.qid,'q1');
  assert.equal(posts[1].body.skipped,true);
  await new Promise(r=>setImmediate(r));
  // queue empty -> server flipped to generating -> modal closed
  assert.equal(elem('backdrop').hidden,true);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class ContextMenuTests(unittest.TestCase):
    """Right-click / Shift+F10 row context menus on the runs and agents
    tables. Menu items reuse the modal's data-* hooks (one delegated click
    handler, one set of server endpoints); availability mirrors the modal
    via shared runCtl. The native menu stays for selections, editable
    fields, non-row targets and Shift+right-click."""

    def test_context_menu_element_is_in_the_page(self):
        html = conductor._DASH_HTML
        self.assertIn('id="ctxmenu"', html)
        self.assertIn('role="menu"', html)
        # rows advertise the menu to AT and are keyboard-focusable
        self.assertIn('aria-haspopup="menu"', html)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_run_menu_state_actions_and_dispatch(self):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={}, posts=[], alerts=[];
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',scrollTop:0,style:{},querySelector:()=>null,
  querySelectorAll:()=>[]}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const sel={isCollapsed:true,toString:()=>''};
const window={innerWidth:1200,innerHeight:800,addEventListener:()=>{},
  getSelection:()=>sel};
const data={serve_boot:'b',generated_at:'2026-10-16T00:00:00',agents:[
  {step:'live-step',run:'live',run_dir:'/runs/live',run_key:'/runs/live',
   slug:'p',status:'running',task:'t1'},
  {step:'queued-step',run:'live',run_dir:'/runs/live',run_key:'/runs/live',
   slug:'p',status:'queued',task:'t2',prompt_editable:true}],runs:[
  {run:'live',dir:'/runs/live',key:'/runs/live',counts:{running:1,queued:1}},
  {run:'done',dir:'/runs/done',key:'/runs/done',counts:{done:1,failed:1},
   ended_at:'2026-10-16T01:00:00'},
  {run:'req',key:'request:r1',request_id:'r1',stage:'generating',
   counts:{planned:1}}]};
const fetch=async(url,opt)=>{
  posts.push({url,body:opt&&opt.body?JSON.parse(opt.body):null});
  if(url.startsWith('/api/state')) return {ok:true,json:async()=>data};
  return {ok:true,json:async()=>({pausing:['live-step'],
    stopping:['live-step']}),text:async()=>''}; };
const ctx=vm.createContext({document,window,fetch,confirm:()=>true,
  alert:m=>alerts.push(m),location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const cls=c=>{let s=c;return {contains:x=>s.split(' ').includes(x),
  add:x=>{s+=' '+x;},remove:x=>{s=s.split(' ').filter(v=>v!==x).join(' ');},
  has:x=>s.split(' ').includes(x)};};
const runRow=key=>({cls:'runrow',dataset:{runKey:key},classList:cls('runrow'),
  isConnected:true,focused:0,focus(){this.focused++;},
  matches:s=>s==='tr.runrow'||s==='tr.runrow,tr.agen',
  getBoundingClientRect:()=>({left:10,top:20,bottom:30})});
const onRow=row=>({isContentEditable:false,dataset:{},
  closest:sel=>{
    if(sel==='#ctxmenu')return null;
    if(sel.indexOf('input')===0)return null;
    if(sel==='tr.runrow')return row.cls==='runrow'?row:null;
    if(sel==='tr.agen')return row.cls==='agen'?row:null;
    if(sel==='tr.runrow,tr.agen')return row;
    return null;}});
const rclick=(t,o)=>{let pd=0;handlers.contextmenu({target:t,clientX:60,
  clientY:70,shiftKey:!!(o&&o.shift),preventDefault:()=>{pd++;}});return pd;};
const menuBtn=dataset=>({id:'',dataset,disabled:false,
  closest:sel=>{
    if(sel==='#ctxmenu')return {inMenu:1};
    const m=sel.match(/^\[data-([a-z-]+)\]$/);
    if(m){const k=m[1].replace(/-([a-z])/g,(s,c)=>c.toUpperCase());
      return k in dataset?{dataset}:null;}
    return null;}});
(async()=>{
  // live run: Open details + Filter + Pause/Stop, Archive, disabled Delete
  assert.equal(rclick(onRow(runRow('/runs/live'))),1);
  const m=elem('ctxmenu');
  assert.equal(m.hidden,false);
  assert.match(m.innerHTML,/data-run-details="\/runs\/live"[^>]*>Open details/);
  assert.match(m.innerHTML,/data-filter-run="\/runs\/live"[^>]*>Filter by this item/);
  assert.match(m.innerHTML,/data-pause-run="live"[^>]*>Pause run/);
  assert.match(m.innerHTML,/data-stop-run="live"[^>]*>Stop run now/);
  assert.match(m.innerHTML,/data-arch-run="live"[^>]*>Archive run/);
  assert.match(m.innerHTML,/disabled[^>]*data-delete-run="live"/);
  assert.doesNotMatch(m.innerHTML,/Resume unfinished steps/);
  assert.doesNotMatch(m.innerHTML,/Merge pending branches/);
  assert.match(m.innerHTML,/Shift\+F10/);            // escape-hatch hint
  // clamped to the viewport (menu estimated at 240px wide in the harness)
  assert.equal(m.style.left,'60px');
  // finished run: Pause/Stop absent, Resume + enabled Delete present
  assert.equal(rclick(onRow(runRow('/runs/done'))),1);
  assert.match(m.innerHTML,/data-resume-run="done"[^>]*>Resume unfinished steps/);
  assert.doesNotMatch(m.innerHTML,/data-pause-run/);
  assert.doesNotMatch(m.innerHTML,/data-stop-run/);
  assert.match(m.innerHTML,/data-delete-run="done"[^>]*>Delete run/);
  assert.doesNotMatch(m.innerHTML,/disabled[^>]*data-delete-run/);
  // proposal row: request modal + Stop proposal, never lifecycle buttons
  assert.equal(rclick(onRow(runRow('request:r1'))),1);
  assert.match(m.innerHTML,/data-open-request="r1"[^>]*>Open details/);
  assert.match(m.innerHTML,/data-stop-gen="r1"[^>]*>Stop proposal/);
  assert.match(m.innerHTML,/data-filter-run="request:r1"/);
  assert.doesNotMatch(m.innerHTML,/data-pause-run|data-arch-run|data-delete-run/);
  // 'Filter by this item' selects the run through the shared handler
  await handlers.click({target:menuBtn({filterRun:'/runs/done'}),
    preventDefault:()=>{}});
  assert.equal(m.hidden,true);                       // item click closed it
  assert.equal(vm.runInContext('selectedRun',ctx),'/runs/done');
  assert.match(elem('runs').innerHTML,/runrow selected/);
  // and the filtered row now offers Clear run filter instead
  assert.equal(rclick(onRow(runRow('/runs/done'))),1);
  assert.match(m.innerHTML,/>Clear run filter</);
  // Pause run posts to the existing endpoint via the shared handler
  assert.equal(rclick(onRow(runRow('/runs/live'))),1);
  posts.length=0;
  await handlers.click({target:menuBtn({pauseRun:'live'}),
    preventDefault:()=>{}});
  assert.equal(posts[0].url,'/api/runs/live/pause');
  assert.equal(m.hidden,true);
  // 'Open details' from the menu is the same modal the name button opens
  assert.equal(rclick(onRow(runRow('/runs/done'))),1);
  await handlers.click({target:menuBtn({runDetails:'/runs/done'}),
    preventDefault:()=>{}});
  assert.equal(elem('backdrop').hidden,false);
  assert.match(elem('dbody').innerHTML,/>done</);
  // outside click is a pure dismissal: menu gone, and the row under the
  // click did NOT toggle its filter (no accidental navigation)
  vm.runInContext('selectedRun=null; render(lastData)',ctx);
  assert.equal(rclick(onRow(runRow('/runs/live'))),1);
  assert.equal(m.hidden,false);
  await handlers.click({target:onRow(runRow('/runs/done')),
    preventDefault:()=>{}});
  assert.equal(m.hidden,true);
  assert.equal(vm.runInContext('selectedRun',ctx),null);   // swallowed
  // the next identical click really is on the row now
  await handlers.click({target:onRow(runRow('/runs/done')),
    preventDefault:()=>{}});
  assert.equal(vm.runInContext('selectedRun',ctx),'/runs/done');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_task_menu_state_actions_and_dispatch(self):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={}, posts=[], alerts=[];
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',scrollTop:0,style:{},querySelector:()=>null,
  querySelectorAll:()=>[]}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const sel={isCollapsed:true,toString:()=>''};
const window={innerWidth:1200,innerHeight:800,addEventListener:()=>{},
  getSelection:()=>sel};
const data={serve_boot:'b',generated_at:'2026-10-16T00:00:00',agents:[
  {step:'live-step',run:'live',run_dir:'/runs/live',run_key:'/runs/live',
   slug:'p',status:'running',task:'t1'},
  {step:'queued-step',run:'live',run_dir:'/runs/live',run_key:'/runs/live',
   slug:'p',status:'queued',task:'t2',prompt_editable:true},
  {step:'held',run:'done',run_dir:'/runs/done',run_key:'/runs/done',
   slug:'p',status:'skipped',skipped_by_user:true,task:'t3'},
  {step:'plan-proposal',run:'req',run_key:'request:r1',slug:'p',
   status:'running',request_id:'r1',task:'planner'}],runs:[
  {run:'live',dir:'/runs/live',key:'/runs/live',counts:{running:1,queued:1}},
  {run:'done',dir:'/runs/done',key:'/runs/done',counts:{skipped:1},
   ended_at:'2026-10-16T01:00:00'}]};
const fetch=async(url,opt)=>{
  posts.push({url,body:opt&&opt.body?JSON.parse(opt.body):null});
  if(url.startsWith('/api/state')) return {ok:true,json:async()=>data};
  return {ok:true,json:async()=>({}),text:async()=>'<p>sum</p>'}; };
const ctx=vm.createContext({document,window,fetch,confirm:()=>true,
  alert:m=>alerts.push(m),location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const cls=c=>({contains:x=>c.split(' ').includes(x),add(){},remove(){}});
const agenRow=i=>({cls:'agen',dataset:{i:String(i)},classList:cls('agen'),
  isConnected:true,focused:0,focus(){this.focused++;},
  matches:s=>s==='tr.agen'||s==='tr.runrow,tr.agen',
  getBoundingClientRect:()=>({left:10,top:20,bottom:30})});
const onRow=row=>({isContentEditable:false,dataset:{},
  closest:sel=>{
    if(sel==='#ctxmenu')return null;
    if(sel.indexOf('input')===0)return null;
    if(sel==='tr.agen')return row.cls==='agen'?row:null;
    if(sel==='tr.runrow')return null;
    if(sel==='tr.runrow,tr.agen')return row;
    return null;}});
const rclick=t=>{let pd=0;handlers.contextmenu({target:t,clientX:60,
  clientY:70,preventDefault:()=>{pd++;}});return pd;};
const menuBtn=dataset=>({id:'',dataset,disabled:false,
  closest:sel=>{
    if(sel==='#ctxmenu')return {inMenu:1};
    const m=sel.match(/^\[data-([a-z-]+)\]$/);
    if(m){const k=m[1].replace(/-([a-z])/g,(s,c)=>c.toUpperCase());
      return k in dataset?{dataset}:null;}
    return null;}});
(async()=>{
  const m=elem('ctxmenu');
  // running task: Stop this step; queued: Skip + Edit prompt; skipped:
  // Unskip - the same gates the modal applies
  assert.equal(rclick(onRow(agenRow(0))),1);
  assert.match(m.innerHTML,/data-agent-detail="\/runs\/live\|live-step"[^>]*>Open details/);
  assert.match(m.innerHTML,/data-summary="\/runs\/live\|live-step"[^>]*>Session summary/);
  assert.match(m.innerHTML,/data-filter-run="\/runs\/live"[^>]*>Filter by this item/);
  assert.match(m.innerHTML,/data-stop-agent="live"[^>]*data-step="live-step"[^>]*>Stop this step/);
  assert.doesNotMatch(m.innerHTML,/Skip step|Unskip|Edit prompt/);
  assert.equal(rclick(onRow(agenRow(1))),1);
  assert.match(m.innerHTML,/data-skip-step="live"[^>]*data-step="queued-step"[^>]*>Skip step/);
  assert.match(m.innerHTML,/data-edit-prompt="\/runs\/live\|queued-step"[^>]*>Edit prompt/);
  assert.doesNotMatch(m.innerHTML,/Stop this step/);
  assert.equal(rclick(onRow(agenRow(2))),1);
  assert.match(m.innerHTML,/data-unskip-step="done"[^>]*data-step="held"[^>]*>Unskip step/);
  // planner row (no run_dir): request link + Stop proposal; no step kill,
  // no summary, no prompt editing
  assert.equal(rclick(onRow(agenRow(3))),1);
  assert.match(m.innerHTML,/data-open-request="r1"[^>]*>View request \/ runbook/);
  assert.match(m.innerHTML,/data-stop-gen="r1"[^>]*>Stop proposal/);
  assert.doesNotMatch(m.innerHTML,/data-stop-agent|data-summary=|Edit prompt/);
  // 'Open details' is the same task modal the row click opens
  await handlers.click({target:menuBtn({agentDetail:'/runs/live|queued-step'}),
    preventDefault:()=>{}});
  assert.equal(elem('backdrop').hidden,false);
  assert.match(elem('dbody').innerHTML,/p \/ queued-step/);
  assert.equal(m.hidden,true);
  // 'Stop this step' hits the existing sentinel endpoint
  assert.equal(rclick(onRow(agenRow(0))),1);
  posts.length=0;
  await handlers.click({target:menuBtn({stopAgent:'live',step:'live-step'}),
    preventDefault:()=>{}});
  assert.equal(posts[0].url,'/api/runs/live/steps/live-step/stop');
  // task filtering narrows the table to its run through the shared handler
  assert.equal(rclick(onRow(agenRow(2))),1);
  await handlers.click({target:menuBtn({filterRun:'/runs/done'}),
    preventDefault:()=>{}});
  assert.equal(vm.runInContext('selectedRun',ctx),'/runs/done');
  assert.doesNotMatch(elem('agents').innerHTML,/>live-step</);
  assert.match(elem('agents').innerHTML,/>held</);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_context_menu_native_and_keyboard(self):
        """The browser menu is preserved on editable fields, live text
        selections and Shift+right-click; the Menu key / Shift+F10 opens
        ours from a focused row; Esc dismisses and returns focus; the
        scroll path and outside clicks close it."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',scrollTop:0,style:{},querySelector:()=>null,
  querySelectorAll:()=>[]}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const sel={isCollapsed:true,toString:()=>''};
const window={innerWidth:1200,innerHeight:800,addEventListener:()=>{},
  getSelection:()=>sel};
const ctx=vm.createContext({document,window,location:{reload:()=>{}},
  setTimeout:()=>{},fetch:async()=>({ok:true,json:async()=>({})})});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'2026-10-16T00:00:00',agents:[
  {step:'s1',run:'r',run_dir:'/r',run_key:'/r',slug:'p',status:'queued',
   task:'t',prompt_editable:true}],runs:[
  {run:'r',dir:'/r',key:'/r',counts:{queued:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const cls=c=>({contains:x=>c.split(' ').includes(x),add(){},remove(){}});
const runRow=key=>({cls:'runrow',dataset:{runKey:key},classList:cls('runrow'),
  isConnected:true,focused:0,focus(){this.focused++;},
  matches:s=>s==='tr.runrow'||s==='tr.runrow,tr.agen',
  getBoundingClientRect:()=>({left:10,top:20,bottom:30})});
const agenRow=i=>({cls:'agen',dataset:{i:String(i)},classList:cls('agen'),
  isConnected:true,focused:0,focus(){this.focused++;},
  matches:s=>s==='tr.agen'||s==='tr.runrow,tr.agen',
  getBoundingClientRect:()=>({left:10,top:20,bottom:30})});
const onRow=row=>({isContentEditable:false,dataset:{},
  closest:sel=>{
    if(sel==='#ctxmenu')return null;
    if(sel.indexOf('input')===0)return null;
    if(sel==='tr.runrow')return row.cls==='runrow'?row:null;
    if(sel==='tr.agen')return row.cls==='agen'?row:null;
    if(sel==='tr.runrow,tr.agen')return row;
    return null;}});
const rclick=(t,o)=>{let pd=0;handlers.contextmenu({target:t,clientX:60,
  clientY:70,shiftKey:!!(o&&o.shift),preventDefault:()=>{pd++;}});return pd;};
const key=(k,t,o)=>{let pd=0;handlers.keydown(
  {key:k,shiftKey:!!(o&&o.shift),ctrlKey:false,altKey:false,
   target:t||{closest:()=>null,matches:()=>false},
   preventDefault:()=>{pd++;}});return pd;};
(async()=>{
  const m=elem('ctxmenu');
  const rr=runRow('/r'), ar=agenRow(0);
  // editable fields keep the native menu entirely (no preventDefault) -
  // the editable check wins even when the field sits inside a row
  const inp={isContentEditable:false,dataset:{},
    closest:sel=>{
      if(sel.indexOf('input')===0)return {tag:'INPUT'};
      return onRow(rr).closest(sel);}};
  assert.equal(rclick(inp),0);
  assert.equal(m.hidden,true);
  // a live text selection keeps the native menu (copy stays native)
  sel.isCollapsed=false; sel.toString=()=>'selected text';
  assert.equal(rclick(onRow(rr)),0);
  assert.equal(m.hidden,true);
  sel.isCollapsed=true;
  // Shift+right-click is the documented escape route to the native menu
  assert.equal(rclick(onRow(rr),{shift:true}),0);
  assert.equal(m.hidden,true);
  // right-click off a row: nothing happens (native menu everywhere else)
  assert.equal(rclick({isContentEditable:false,dataset:{},
    closest:()=>null}),0);
  assert.equal(m.hidden,true);
  // keyboard: Menu key on a focused run row opens the same menu...
  assert.equal(key('ContextMenu',onRow(rr)),1);
  assert.equal(m.hidden,false);
  assert.match(m.innerHTML,/data-run-details="\/r"/);
  // ...Esc dismisses and hands focus back to the row
  assert.equal(key('Escape'),1);
  assert.equal(m.hidden,true);
  assert.equal(rr.focused,1);
  // Shift+F10 on a task row does the same for task actions
  assert.equal(key('F10',onRow(ar),{shift:true}),1);
  assert.equal(m.hidden,false);
  assert.match(m.innerHTML,/data-agent-detail="\/r\|s1"/);
  // Menu key nowhere near a row does not hijack the keyboard
  assert.equal(key('ContextMenu'),0);
  // Enter/Space outside the open menu dismiss it instead of toggling the row
  key('ContextMenu',onRow(rr));
  assert.equal(key('Enter',onRow(rr)),1);
  assert.equal(m.hidden,true);
  assert.equal(vm.runInContext('selectedRun',ctx),null);
  // scroll hides the menu with the view it belongs to
  key('ContextMenu',onRow(rr));
  assert.equal(m.hidden,false);
  handlers.scroll();
  assert.equal(m.hidden,true);
  // Enter on a focused task row remains the left-click detail open
  // (the focused element IS the row - e.target, not a cell inside it)
  assert.equal(key('Enter',ar),1);
  assert.equal(elem('backdrop').hidden,false);
  assert.match(elem('dbody').innerHTML,/p \/ s1/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class TaskTableNavTests(unittest.TestCase):
    """Task-table click targets: the step name opens the task modal
    (data-agent-detail, keyed by agentKey), the run name applies the run
    filter (data-filter-run, run_key before run_dir), and a bare click on
    any other part of a task row does nothing. While a modal is open its
    row keeps a .dsel mark keyed by agentKey/runKey - moved by sibling
    nav, re-applied on refresh, cleared on close."""

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_task_row_click_targets(self):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',scrollTop:0}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const ctx=vm.createContext({document,location:{reload:()=>{}},
  setTimeout:()=>{},fetch:async()=>({ok:true,json:async()=>({})}),
  confirm:()=>true,alert:()=>{}});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'x',agents:[
  {step:'one',run:'runA',run_dir:'/rA',run_key:'request:q1',slug:'p',
   status:'done',task:'t1',seq:0,request_id:'q1'},
  {step:'two',run:'runA',run_dir:'/rA',run_key:'request:q1',slug:'p',
   status:'done',task:'t2',seq:1,request_id:'q1'},
  {step:'three',run:'runB',run_dir:'/rB',run_key:'/rB',slug:'p',
   status:'done',task:'t3',seq:0},
  {step:'orphan',run:null,run_dir:null,run_key:'',slug:'p',
   status:'skipped',task:'t4'}],
 runs:[{run:'runA',dir:'/rA',key:'request:q1',request_id:'q1',
        counts:{done:2}},
       {run:'runB',dir:'/rB',key:'/rB',counts:{done:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const agents=()=>elem('agents').innerHTML;
// the step name is a real button keyed by agentKey (stable across
// re-sorts); the run name is a filter button keyed by the stable run_key
// (request:q1), NOT the dir path
assert.match(agents(),
  /<button type="button" class="linkbtn step-name"[^>]*data-agent-detail="request:q1\|one"[^>]*>one<\/button>/);
assert.match(agents(),
  /<button type="button" class="linkbtn run-link"[^>]*data-filter-run="request:q1"[^>]*>runA<\/button>/);
// a task with no run association keeps a plain-text run cell - there is
// no filter target to apply
const orphanRow=agents().split('<tr ').find(r=>r.includes('>orphan<'));
assert.ok(orphanRow);
assert.doesNotMatch(orphanRow,/run-link/);
assert.match(orphanRow,/<td>-<\/td>/);
// the row still advertises its context menu and stays focusable
assert.match(agents(),/<tr class="agen"[^>]*tabindex="0"[^>]*aria-haspopup="menu"/);
const click=async t=>handlers.click({target:t,preventDefault:()=>{}});
const el=(sel,dataset)=>({dataset:{},
  closest:s=>s===sel?{dataset}:null});
(async()=>{
  // a bare click anywhere else on the task row (e.g. the result cell, or
  // while selecting text) must NOT open the modal - the row catch-all is
  // gone
  await click({dataset:{},
    closest:s=>s==='tr.agen'?{dataset:{i:'0'}}:null});
  assert.equal(elem('backdrop').hidden,true);
  // the step-name button opens this task's modal
  await click(el('[data-agent-detail]',{agentDetail:'request:q1|one'}));
  assert.equal(elem('backdrop').hidden,false);
  assert.match(elem('dbody').innerHTML,/p \/ one/);
  await click({id:'dx',dataset:{},closest:()=>null});
  assert.equal(elem('backdrop').hidden,true);
  // the run-name button applies the run filter by its stable key
  await click(el('[data-filter-run]',{filterRun:'request:q1'}));
  assert.equal(vm.runInContext('selectedRun',ctx),'request:q1');
  assert.match(elem('filters').innerHTML,/runA ×/);
  assert.match(agents(),/>one</);
  assert.doesNotMatch(agents(),/>three</);
  assert.doesNotMatch(elem('runs').innerHTML,/>runB</);
  // a click on inner markup of the run-name button (closest() dispatch)
  // reaches the same handler instead of falling to the row
  await click({id:'',dataset:{clearRun:''},closest:()=>null});
  const inner={dataset:{},closest:s=>{
    if(s==='[data-filter-run]')return {dataset:{filterRun:'/rB'}};
    if(s==='tr.agen')return {dataset:{i:'2'}};
    return null;}};
  await click(inner);
  assert.equal(vm.runInContext('selectedRun',ctx),'/rB');
  assert.equal(elem('backdrop').hidden,true);   // never a modal
  assert.match(elem('filters').innerHTML,/runB ×/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_task_row_selection_highlight_lifecycle(self):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',scrollTop:0}); return nodes.get(id); };
// model the row DOM honestly: every render() rewrites the table HTML, so
// re-parse fresh row objects ONLY when the markup changed - classList
// toggles then persist exactly like real nodes between renders and are
// recreated clean (no dsel baked into markup) on each rebuild.
let parsed={html:null,rows:[]};
function domRows(){
  const html=(elem('runs').innerHTML||'')+'\n'+(elem('agents').innerHTML||'');
  if (html===parsed.html) return parsed.rows;
  const rows=[...html.matchAll(/<tr [^>]*>/g)].map(m=>{
    const tag=m[0];
    const attr=n=>{const a=tag.match(new RegExp(n+'="([^"]*)"'));
      return a?a[1]:'';};
    const set=new Set(attr('class').split(/\s+/).filter(Boolean));
    const row={dataset:{runKey:attr('data-run-key'),akey:attr('data-akey'),
      i:attr('data-i')},_set:set,
      classList:{contains:c=>set.has(c),
        toggle:(c,on)=>{on?set.add(c):set.delete(c)},
        add:c=>set.add(c),remove:c=>set.delete(c)}};
    row.has=c=>set.has(c);
    return row;
  }).filter(r=>r._set.has('runrow')||r._set.has('agen'));
  parsed={html,rows};
  return rows;
}
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:sel=>
    /data-run-key|data-akey|runrow|agen/.test(sel)?domRows():[],
  activeElement:null,addEventListener:(ev,h)=>{handlers[ev]=h;}};
const ctx=vm.createContext({document,location:{reload:()=>{}},
  setTimeout:()=>{},fetch:async()=>({ok:true,json:async()=>({}),
    text:async()=>''}),confirm:()=>true,alert:()=>{}});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'x',agents:[
  {step:'s1',run:'rA',run_dir:'/rA',run_key:'/rA',slug:'p',status:'done',
   task:'t1',seq:0},
  {step:'s2',run:'rA',run_dir:'/rA',run_key:'/rA',slug:'p',status:'done',
   task:'t2',seq:1},
  {step:'x1',run:'rB',run_dir:'/rB',run_key:'/rB',slug:'p',status:'done',
   task:'t3',seq:0}],
 runs:[{run:'rA',dir:'/rA',key:'/rA',counts:{done:2}},
       {run:'rB',dir:'/rB',key:'/rB',counts:{done:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const dsel=()=>domRows().filter(r=>r._set.has('dsel'))
  .map(r=>r.dataset.akey||r.dataset.runKey).sort();
const click=async t=>handlers.click({target:t,preventDefault:()=>{}});
const el=(sel,dataset)=>({dataset:{},
  closest:s=>s===sel?{dataset}:null});
const key=k=>handlers.keydown({key:k,
  target:{closest:()=>null,matches:()=>false},preventDefault:()=>{}});
(async()=>{
  assert.deepEqual(dsel(),[]);
  // open a task modal via its step-name button - that row alone marks
  await click(el('[data-agent-detail]',{agentDetail:'/rA|s1'}));
  assert.equal(elem('backdrop').hidden,false);
  assert.deepEqual(dsel(),['/rA|s1']);
  // sibling nav moves the mark to the task now displayed
  await click(el('[data-nav]',{nav:'/rA|s2'}));
  assert.match(elem('dbody').innerHTML,/p \/ s2/);
  assert.deepEqual(dsel(),['/rA|s2']);
  // ArrowRight walks back the other way; ArrowLeft from s1 stops at end
  key('ArrowLeft');
  assert.match(elem('dbody').innerHTML,/p \/ s1/);
  assert.deepEqual(dsel(),['/rA|s1']);
  key('ArrowLeft');
  assert.deepEqual(dsel(),['/rA|s1']);          // first task - no move
  // a re-sorted refresh cannot paint the wrong row: keys, not indexes
  const d2=JSON.parse(JSON.stringify(data)); d2.agents.reverse();
  vm.runInContext('render('+JSON.stringify(d2)+')',ctx);
  assert.deepEqual(dsel(),['/rA|s1']);
  // filtering the task's run out leaves no visible row marked; the modal
  // stays open and unfiltering brings its mark back
  vm.runInContext('selectedRun="/rB"; render(lastData)',ctx);
  assert.deepEqual(dsel(),[]);
  assert.equal(elem('backdrop').hidden,false);
  vm.runInContext('selectedRun=null; render(lastData)',ctx);
  assert.deepEqual(dsel(),['/rA|s1']);
  // the filter's own .hl/.selected marks never impersonate the modal mark
  vm.runInContext('selectedRun="/rA"; render(lastData)',ctx);
  const selRow=domRows().find(r=>r.dataset.runKey==='/rA'&&r._set.has('runrow'));
  assert.ok(selRow._set.has('selected'));
  assert.ok(domRows().every(r=>r._set.has('dsel')===
    (r.dataset.akey==='/rA|s1')));
  vm.runInContext('selectedRun=null; render(lastData)',ctx);
  // closing the dialog clears the mark
  key('Escape');
  assert.equal(elem('backdrop').hidden,true);
  assert.deepEqual(dsel(),[]);
  // run details get the equivalent mark on their run row
  vm.runInContext('showRunDetails("/rA")',ctx);
  assert.deepEqual(dsel(),['/rA']);
  vm.runInContext('closeDetail(true)',ctx);
  assert.deepEqual(dsel(),[]);
  // Enter on a focused task row still opens details (deliberate keyboard
  // activation - not the removed arbitrary-CLICK path)
  handlers.keydown({key:'Enter',target:{matches:s=>s==='tr.agen',
    dataset:{i:'1'}},preventDefault:()=>{}});
  assert.equal(elem('backdrop').hidden,false);
  assert.deepEqual(dsel(),['/rA|s2']);
})().catch(e=>{console.error(e);process.exitCode=1;});
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


class PendingStateTests(unittest.TestCase):
    """Semantic staleness: 'gen' inputs (request text, project,
    attachments) diff against the publish-time gen_inputs baseline and
    'opts' (model/effort/attempts/failure policy/worktrees/resolver/
    followup) diff against what the proposal TOML itself encodes.
    Reverting a field to its baseline clears only that mark, Apply is
    refused while generation inputs are pending, a failed operation keeps
    the pending state, and launch refuses while either side diverges."""

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
        for target, value in (("PLANNER_SPOOL", self.base / "spool"),
                              ("WATCH_POLL_S", 0.02)):
            p = mock.patch.object(conductor, target, value)
            p.start()
            self.addCleanup(p.stop)

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

    def ready(self, **fields):
        rec = conductor.create_request(
            {"raw": "Please tidy the catalog carefully", "project": "proj",
             "model": "swe-2", "effort": "max", **fields},
            self.requests, self.runs)
        return conductor.save_request(
            rec["id"], {"manifest": self.proposal()},
            self.requests, self.manifests, self.runs)

    def save(self, rid, payload):
        return conductor.save_request(rid, payload, self.requests,
                                      self.manifests, self.runs)

    def pending(self, rid):
        return conductor.request_details(rid, self.requests,
                                         self.manifests)["pending"]

    def test_opt_change_and_revert_clears_pending(self):
        rec = self.ready()
        out = self.save(rec["id"], {"worktrees": True})
        self.assertEqual(out["pending"], {"gen": [], "opts": ["worktrees"]})
        self.assertTrue(out["stale"])
        out = self.save(rec["id"], {"worktrees": False})
        self.assertEqual(out["pending"], {"gen": [], "opts": []})
        self.assertFalse(out["stale"])

    def test_gen_change_and_revert_clears_pending(self):
        rec = self.ready()
        out = self.save(rec["id"], {"raw": "widen the scope"})
        self.assertEqual(out["pending"], {"gen": ["raw"], "opts": []})
        out = self.save(rec["id"],
                        {"raw": "Please tidy the catalog carefully"})
        self.assertEqual(out["pending"], {"gen": [], "opts": []})
        self.assertFalse(out["stale"])

    def test_attachments_are_a_gen_input(self):
        att = [{"name": "shot.png", "path": "C:/x/shot.png"}]
        rec = self.ready(attachments=att)
        out = self.save(rec["id"], {"attachments": []})
        self.assertEqual(out["pending"]["gen"], ["attachments"])
        # a deterministic TOML patch cannot re-interpret a dropped image
        with self.assertRaises(conductor.RequestError) as e:
            conductor.apply_request(rec["id"], {}, self.requests,
                                    self.manifests, self.runs)
        self.assertEqual(e.exception.status, 409)
        out = self.save(rec["id"], {"attachments": att})
        self.assertEqual(out["pending"]["gen"], [])

    def test_gen_and_opts_pending_independently(self):
        rec = self.ready()
        out = self.save(rec["id"], {"effort": "high"})
        self.assertEqual(out["pending"], {"gen": [], "opts": ["effort"]})
        out = self.save(rec["id"], {"raw": "widen the scope"})
        self.assertEqual(out["pending"]["gen"], ["raw"])
        self.assertEqual(out["pending"]["opts"], ["effort"])
        # reverting one side clears only that side
        out = self.save(rec["id"], {"effort": "max"})
        self.assertEqual(out["pending"], {"gen": ["raw"], "opts": []})

    def test_saved_gen_divergence_still_refuses_apply(self):
        """The old check compared the payload to the last save, so a
        divergence that was already saved slipped through Apply. The
        refusal now runs against the publish-time baseline."""
        rec = self.ready()
        self.save(rec["id"], {"raw": "widen the scope"})
        with self.assertRaises(conductor.RequestError) as e:
            conductor.apply_request(rec["id"], {"effort": "high"},
                                    self.requests, self.manifests,
                                    self.runs)
        self.assertEqual(e.exception.status, 409)
        self.assertIn("regenerate", str(e.exception))

    def test_apply_resolves_opt_pending_and_keeps_manual_toml_edits(self):
        rec = self.ready()
        p = Path(rec["manifest"])
        edited = p.read_text(encoding="utf-8").replace(
            'description = "Tidy the catalog"',
            'description = "Tidy the catalog by hand"')
        out = conductor.apply_request(
            rec["id"], {"manifest": edited, "effort": "high"},
            self.requests, self.manifests, self.runs)
        text = Path(out["manifest"]).read_text(encoding="utf-8")
        self.assertIn('description = "Tidy the catalog by hand"', text)
        self.assertIn('effort = "high"', text)
        self.assertFalse(out["stale"])

    def test_launch_blocked_while_pending(self):
        rec = self.ready()
        self.save(rec["id"], {"effort": "high"})
        with self.assertRaises(conductor.RequestError) as e:
            conductor.launch_request(rec["id"], self.requests,
                                     self.manifests, self.runs, ["devin"])
        self.assertEqual(e.exception.status, 409)
        self.assertIn("options", str(e.exception))
        out = conductor.apply_request(rec["id"], {"effort": "high"},
                                      self.requests, self.manifests,
                                      self.runs)
        self.assertFalse(out["stale"])
        with mock.patch.object(conductor, "_launch_process",
                               return_value=4321):
            out = conductor.launch_request(rec["id"], self.requests,
                                           self.manifests, self.runs,
                                           ["devin"])
        self.assertEqual(out["status"], "launching")

    def test_failed_apply_keeps_pending_and_manual_toml(self):
        rec = self.ready()
        self.save(rec["id"], {"effort": "high"})
        Path(rec["manifest"]).write_text("[run\nbroken = ", encoding="utf-8")
        with self.assertRaises(conductor.RequestError):
            conductor.apply_request(rec["id"], {"effort": "high"},
                                    self.requests, self.manifests,
                                    self.runs)
        det = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertIn("manifest", det["pending"]["opts"])
        self.assertTrue(det["stale"])
        # the hand-broken manifest is left alone - a failed apply must not
        # touch the TOML or reset the pending state
        self.assertEqual(Path(rec["manifest"]).read_text(encoding="utf-8"),
                         "[run\nbroken = ")

    def test_failed_regeneration_keeps_gen_pending(self):
        rec = self.ready()
        original = Path(rec["manifest"]).read_text(encoding="utf-8")
        self.save(rec["id"], {"raw": "widen the scope"})
        with mock.patch.object(conductor, "_planner_response",
                               side_effect=ValueError("unavailable")):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        det = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(det["status"], "error")
        self.assertEqual(det["pending"]["gen"], ["raw"])
        self.assertTrue(det["stale"])
        self.assertEqual(Path(det["manifest"]).read_text(encoding="utf-8"),
                         original)

    def test_regenerate_resolves_gen_and_opt_pending(self):
        """Both pending: Apply is refused (it cannot rewrite step prompts),
        so the order is Regenerate first - the published proposal is built
        around the new request AND carries the pending options, clearing
        both buckets in one pass."""
        rec = self.ready()
        self.save(rec["id"], {"raw": "widen the scope", "effort": "high"})
        det = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(det["pending"]["gen"], ["raw"])
        self.assertIn("effort", det["pending"]["opts"])
        with self.assertRaises(conductor.RequestError):
            conductor.apply_request(rec["id"], {"effort": "high"},
                                    self.requests, self.manifests,
                                    self.runs)
        proposal = self.proposal().replace('effort = "max"',
                                           'effort = "high"')
        with mock.patch.object(conductor, "_planner_response",
                               return_value=proposal):
            conductor.start_generation(rec["id"], self.requests,
                                       self.manifests, self.runs,
                                       ["devin"], background=False)
        det = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(det["status"], "ready")
        self.assertEqual(det["pending"], {"gen": [], "opts": []})
        self.assertIn('effort = "high"',
                      Path(det["manifest"]).read_text(encoding="utf-8"))

    def test_planner_only_and_label_changes_never_pending(self):
        rec = self.ready()
        out = self.save(rec["id"], {"plan_model": "gpt-6-luna",
                                   "plan_effort": "low", "ask": True,
                                   "label": "renamed"})
        self.assertEqual(out["pending"], {"gen": [], "opts": []})
        self.assertFalse(out["stale"])

    def test_manual_toml_edit_rebaselines_options(self):
        """A hand-edited manifest moves the option baseline itself: the
        form field now diverges from what the TOML encodes, so the drift
        stays flagged until the form is brought in line or Apply rewrites
        the TOML. Nothing is silently discarded - the edit is the new
        baseline."""
        rec = self.ready()
        p = Path(rec["manifest"])
        edited = p.read_text(encoding="utf-8").replace(
            'effort = "max"', 'effort = "high"')
        out = self.save(rec["id"], {"manifest": edited})
        self.assertEqual(out["pending"]["opts"], ["effort"])
        self.assertTrue(out["stale"])
        with self.assertRaises(conductor.RequestError):
            conductor.launch_request(rec["id"], self.requests,
                                     self.manifests, self.runs, ["devin"])
        out = self.save(rec["id"], {"effort": "high"})
        self.assertEqual(out["pending"], {"gen": [], "opts": []})

    def test_pending_state_survives_reopen(self):
        rec = self.ready()
        self.save(rec["id"], {"raw": "widen the scope", "effort": "high"})
        det = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(det["pending"]["gen"], ["raw"])
        self.assertEqual(det["pending"]["opts"], ["effort"])
        self.assertEqual(det["baseline"]["raw"],
                         "Please tidy the catalog carefully")
        self.assertEqual(det["baseline"]["effort"], "max")
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual(d["runs"][0]["pending"]["gen"], ["raw"])
        self.assertTrue(d["runs"][0]["stale"])

    def test_legacy_stale_record_keeps_opaque_pending(self):
        """A pre-baseline record whose stored flag is set cannot be
        attributed - it reports the opaque 'fields' marker until a publish
        re-baselines it."""
        rec = self.ready()
        rec.pop("gen_inputs")
        rec["stale"] = True
        conductor._save_request(rec, self.requests)
        self.assertEqual(self.pending(rec["id"])["gen"], ["fields"])
        self.assertTrue(conductor.request_details(
            rec["id"], self.requests, self.manifests)["stale"])

    def test_legacy_unstale_record_materializes_baseline(self):
        """A never-stale pre-baseline record's stored fields ARE the
        publish-time ones - the next save pins them so later diffs are
        attributable."""
        rec = self.ready()
        rec.pop("gen_inputs")
        conductor._save_request(rec, self.requests)
        out = self.save(rec["id"], {"label": "renamed"})
        self.assertFalse(out["stale"])
        self.assertIn("gen_inputs", out)
        out = self.save(rec["id"], {"raw": "widen the scope"})
        self.assertEqual(out["pending"]["gen"], ["raw"])

    def test_apply_without_changes_is_harmless(self):
        rec = self.ready()
        out = conductor.apply_request(rec["id"], {}, self.requests,
                                      self.manifests, self.runs)
        self.assertFalse(out["stale"])
        self.assertEqual(out["pending"] if "pending" in out else
                         self.pending(rec["id"]), {"gen": [], "opts": []})


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

    def test_trajectory_arc_groups_consecutive_phases(self):
        """The modal's trajectory section groups an ATIF export's raw
        steps into labeled phase spans."""
        steps = [
            {"step_id": 1, "source": "system", "message": "You are Devin"},
            {"step_id": 2, "source": "agent",
             "message": "Let me check the run modal code"},
            {"step_id": 3, "source": "agent", "message": "",
             "tool_calls": [{"function_name": "read",
                             "arguments": {"file_path": "/x/conductor.py"}}]},
            {"step_id": 4, "source": "agent",
             "message": "Now add the /skip route"},
            {"step_id": 5, "source": "agent",
             "message": "Now run the test suite"},
            {"step_id": 6, "source": "agent", "message": "",
             "tool_calls": [{"function_name": "exec",
                             "arguments": {"command": "python run_tests.py"}}]},
            {"step_id": 7, "source": "agent",
             "message": "commit in logical groups"},
        ]
        with tempfile.TemporaryDirectory() as td:
            ex = Path(td) / "s.export.json"
            ex.write_text(json.dumps({"steps": steps}), encoding="utf-8")
            arc = conductor.trajectory_arc(ex)
        self.assertEqual(arc["steps"], 7)
        got = [(g["span"], g["phase"]) for g in arc["groups"]]
        self.assertEqual(got, [("1", "setup"), ("2-3", "audit"),
                               ("4", "implement"), ("5-6", "test"),
                               ("7", "commit")])
        # consecutive same-phase merge keeps the FIRST step's narration
        self.assertIn("check the run modal", arc["groups"][1]["head"])
        # empty-message step falls back to a tool-call summary
        self.assertEqual(conductor._arc_head(steps[5]), "exec")

    def test_trajectory_arc_entries_carry_times_and_tokens(self):
        """Expanded view data: per-entry timestamp, gap-to-next duration
        and REAL per-turn metrics from ATIF (never estimates); groups
        carry token sums + first timestamp."""
        steps = [
            {"step_id": 1, "source": "agent",
             "timestamp": "2026-01-01T10:00:00+00:00",
             "message": "audit the code",
             "metrics": {"prompt_tokens": 1000, "completion_tokens": 50,
                         "cached_tokens": 900}},
            {"step_id": 2, "source": "agent",
             "timestamp": "2026-01-01T10:01:40+00:00",
             "message": "check one more file",
             "metrics": {"prompt_tokens": 2000, "completion_tokens": 60,
                         "cached_tokens": 1900}},
            {"step_id": 3, "source": "agent",
             "timestamp": "2026-01-01T10:02:00+00:00",
             "message": "commit"},  # no metrics -> no token keys
        ]
        with tempfile.TemporaryDirectory() as td:
            ex = Path(td) / "s.export.json"
            ex.write_text(json.dumps({"steps": steps}), encoding="utf-8")
            arc = conductor.trajectory_arc(ex)
        g1, g2 = arc["groups"]
        self.assertEqual((g1["span"], g1["phase"]), ("1-2", "audit"))
        self.assertEqual((g1["tin"], g1["tout"], g1["tcached"]),
                         (3000, 110, 2800))
        self.assertEqual(g1["t0"], "2026-01-01T10:00:00+00:00")
        self.assertEqual(g1["t1"], "2026-01-01T10:01:40+00:00")
        e1, e2 = g1["entries"]
        self.assertEqual((e1["in"], e1["out"], e1["cached"]),
                         (1000, 50, 900))
        self.assertEqual(e1["dur_s"], 100)          # gap to entry 2
        self.assertNotIn("dur_s", e2)               # last entry in group
        self.assertNotIn("in", g2["entries"][0])    # no metrics -> none

    def test_trajectory_arc_tolerates_junk(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(conductor.trajectory_arc(td))
            bad = Path(td) / "b.json"
            bad.write_text("{no", encoding="utf-8")
            self.assertIsNone(conductor.trajectory_arc(bad))
            empty = Path(td) / "e.json"
            empty.write_text('{"steps": []}', encoding="utf-8")
            self.assertIsNone(conductor.trajectory_arc(empty))
            # a live step has no export yet -> no arc, no crash
            self.assertIsNone(conductor.cached_arc(
                str(Path(td) / "nope.json")))

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


class TrajectoryDetailTests(unittest.TestCase):
    """The modal's per-entry expansion serves the FULL retained text from
    the ATIF export (message/reasoning/tool args/results) - not the
    ~110-char arc head. Lazy via /trajectory endpoints, honest notes when
    nothing was retained."""

    def _export(self, td: str, steps) -> Path:
        ex = Path(td) / "s.export.json"
        ex.write_text(json.dumps({"steps": steps}), encoding="utf-8")
        return ex

    def _rich_steps(self):
        return [
            {"step_id": 1, "source": "system",
             "timestamp": "2026-01-01T10:00:00",
             "message": "sys prompt line one\nline two with   spacing"},
            {"step_id": 4, "source": "agent",
             "timestamp": "2026-01-01T10:01:00",
             "model_name": "swe-2",
             "message": "Detailed step:\n  1. audit\n  2. edit\n" +
                      "very long line " * 30,
             "reasoning_content": "thinking\nmore thinking",
             "tool_calls": [
                 {"tool_call_id": "c1", "function_name": "edit",
                  "arguments": {"file_path": "/x/f.py",
                                "content": "a\nb\n  indented"}},
                 {"tool_call_id": "c2", "function_name": "exec",
                  "arguments": {"command": "python run_tests.py"}}],
             "observation": {"results": [
                 {"source_call_id": "c1", "content": "edit ok\nmore"},
                 {"source_call_id": "c2", "content": "suite green"},
                 {"source_call_id": "cX", "content": "orphan result"}]},
             "metrics": {"prompt_tokens": 10, "completion_tokens": 5}},
            {"step_id": 5, "source": "agent",
             "timestamp": "2026-01-01T10:02:00", "message": ""},
        ]

    def test_detail_returns_complete_text(self):
        """Expansion shows the whole stored field, not the arc's head -
        including long multiline messages (the 'detailed step' case)."""
        with tempfile.TemporaryDirectory() as td:
            det = conductor.trajectory_detail(self._export(
                td, self._rich_steps()))
        ents = det["entries"]
        e4 = ents["4"]
        self.assertIn("very long line " * 30, e4["msg"])  # untruncated
        self.assertIn("\n  1. audit\n  2. edit\n", e4["msg"])  # ws kept
        self.assertEqual(e4["think"], "thinking\nmore thinking")
        self.assertEqual(e4["src"], "agent")
        self.assertEqual(e4["model"], "swe-2")
        # args render as 'key: value' lines - multiline values stay
        # literal (an edit payload reads as written, not JSON-escaped)
        c1, c2 = e4["calls"]
        self.assertEqual(c1["name"], "edit")
        self.assertIn("file_path: /x/f.py", c1["args"])
        self.assertIn("content:\na\nb\n  indented", c1["args"])
        self.assertEqual(c1["result"], "edit ok\nmore")
        self.assertEqual(c2["result"], "suite green")
        # an observation with no matching call still surfaces
        self.assertEqual(e4["extra_obs"], ["orphan result"])
        # system step keeps its full message; content-less step has none
        self.assertIn("line two with   spacing", ents["1"]["msg"])
        self.assertIsNone(ents["5"]["msg"])
        self.assertIsNone(ents["5"]["think"])

    def test_detail_caps_overlong_fields_honestly(self):
        with tempfile.TemporaryDirectory() as td:
            big = "x" * (conductor.ARC_DETAIL_CAP + 500)
            ex = self._export(td, [{"step_id": 1, "source": "agent",
                                    "message": big}])
            det = conductor.trajectory_detail(ex)
        e = det["entries"]["1"]
        self.assertEqual(len(e["msg"]), conductor.ARC_DETAIL_CAP)
        self.assertEqual(e["trunc"], {"message":
                                     conductor.ARC_DETAIL_CAP + 500})

    def test_detail_tolerates_junk(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(conductor.trajectory_detail(td))
            bad = Path(td) / "b.json"
            bad.write_text("{no", encoding="utf-8")
            self.assertIsNone(conductor.trajectory_detail(bad))
            # a mock-shaped export has no steps -> None, never a fake row
            mock_ex = Path(td) / "m.export.json"
            mock_ex.write_text('{"mock": true, "session": "s"}')
            self.assertIsNone(conductor.trajectory_detail(mock_ex))
            # steps without step_id are skipped, not keyed 'None'
            ex = self._export(td, [{"source": "agent", "message": "x"},
                                   {"step_id": 2, "message": "y"}])
            det = conductor.trajectory_detail(ex)
            self.assertEqual(list(det["entries"]), ["2"])
            self.assertIsNone(
                conductor.cached_arc_detail(Path(td) / "none.json"))

    def _run_with_export(self, td: str, steps, **rec_extra):
        runs = Path(td) / "runs"
        rd = runs / "20261016-000000-traj"
        rd.mkdir(parents=True)
        exp = rd / "s1.export.json"
        exp.write_text(json.dumps({"steps": steps}), encoding="utf-8")
        st = {"run": "traj",
              "steps": {"s1": {"status": "done", "export": str(exp),
                               **rec_extra}}}
        (rd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        return runs, rd

    def test_step_trajectory_endpoint(self):
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run_with_export(td, self._rich_steps())
            det = conductor.step_trajectory(rd.name, "s1", runs)
            self.assertIn("very long line", det["entries"]["4"]["msg"])
            # second call hits the mtime memo, same object
            self.assertIs(det, conductor.step_trajectory(
                rd.name, "s1", runs))

    def test_step_trajectory_404s(self):
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run_with_export(td, self._rich_steps())
            raises = lambda *a: self.assertRaises(conductor.RequestError,
                conductor.step_trajectory, *a)
            raises(rd.name, "nope", runs)                # unknown step
            raises("../x", "s1", runs)                   # bad run name
            raises(rd.name, "../x", runs)                # bad step name
            st = json.loads((rd / "state.json").read_text())
            st["steps"]["s1"]["export"] = str(           # escapes the dir
                Path(td) / "outside.json")
            (rd / "state.json").write_text(json.dumps(st))
            raises(rd.name, "s1", runs)
            st["steps"]["s1"]["export"] = str(           # gone from disk
                rd / "deleted.export.json")
            (rd / "state.json").write_text(json.dumps(st))
            raises(rd.name, "s1", runs)
            st["steps"]["s1"].pop("export")              # never recorded
            (rd / "state.json").write_text(json.dumps(st))
            raises(rd.name, "s1", runs)
            # a mock export (no steps) reports 'nothing retained'
            mock_ex = rd / "s1.export.json"
            mock_ex.write_text('{"mock": true}', encoding="utf-8")
            st["steps"]["s1"]["export"] = str(mock_ex)
            (rd / "state.json").write_text(json.dumps(st))
            raises(rd.name, "s1", runs)

    def test_request_trajectory_endpoint(self):
        rid = "20261016-013719-deadbeef"
        with tempfile.TemporaryDirectory() as td:
            reqs = Path(td) / "requests"
            reqs.mkdir()
            (reqs / f"{rid}.gen.export.json").write_text(
                json.dumps({"steps": self._rich_steps()}),
                encoding="utf-8")
            det = conductor.request_trajectory(rid, reqs)
            self.assertIn("4", det["entries"])
            with self.assertRaises(conductor.RequestError):
                conductor.request_trajectory(
                    "20261016-013719-00000000", reqs)    # no export file
            with self.assertRaises(conductor.RequestError):
                conductor.request_trajectory("../../etc", reqs)  # bad id

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_arc_expansion_markup_and_state(self):
        """Entries render as closed <details class='arcent'> (compact
        default preserved); Expand/Collapse all + per-entry toggles live
        in trajOpen keyed per agent, so a rebuild or a run switch never
        leaks expansion across agents."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={}, fetches=[];
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',scrollTop:9}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,fn)=>{handlers[ev]=fn;}};
const fetch=async(path)=>{fetches.push(path);return {ok:true,
  json:async()=>({entries:{'4':{sid:4,src:'agent',model:'m',
    msg:'LINE1\nLINE2   spaced <b>html</b>',think:'r',
    calls:[{name:'edit',args:'{\n "a": 1\n}',result:'ok'}],
    extra_obs:[],trunc:null}}})};};
const ctx=vm.createContext({document,location:{reload:()=>{}},
  setTimeout:(fn)=>{},fetch});
vm.runInContext(src,ctx);
const arc={steps:3,em:1,groups:[{span:'1-4',phase:'implement',n:3,
  t0:'2026-01-01T10:00:00',tin:100,tout:5,tcached:50,head:'h',
  entries:[{sid:4,ts:'2026-01-01T10:00:00',dur_s:60,in:10,out:5,
    cached:1,head:'<img src=x onerror=alert(1)>'},
   {sid:5,ts:'2026-01-01T10:01:00',in:2,out:1,cached:0,head:'e5'}]}]};
const mk=(step,key,dir)=>({step,run:'r',run_dir:dir,run_key:key,
  slug:'p',status:'done',task:'t',export:dir+'/s.export.json',arc,seq:1});
const data={serve_boot:'b',generated_at:'2026-01-01',agents:[
  mk('s1','rk1','D:/runs/r1'), mk('s1','rk2','D:/runs/r2')],
  runs:[{run:'r',dir:'D:/runs/r1',key:'rk1',counts:{done:1}},
        {run:'r',dir:'D:/runs/r2',key:'rk2',counts:{done:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const open=k=>vm.runInContext(
  'showDetail(AGENTS.findIndex(a=>agentKey(a)==='+JSON.stringify(k)+'))',
  ctx);
(async()=>{
  await open('rk1|s1');
  let body=elem('dbody').innerHTML;
  // compact default: entries are <details>, none open
  assert.match(body,/data-arc-all/);
  assert.match(body,/data-arc-none/);
  assert.match(body,/<details class="arcgrp" data-ag="0">/);
  assert.match(body,/<details class="arcent" data-asid="4">/);
  assert.doesNotMatch(body,/class="arcent" data-asid="4" open/);
  // hostile head text stays escaped in the row
  assert.doesNotMatch(body,/<img src=x onerror/);
  assert.match(body,/onerror=alert\(1\)&gt;/);
  // expanding an entry: toggle persists it, kicks ONE fetch, fills body
  const det4={classList:{contains:c=>c==='arcent'},
    dataset:{asid:'4'},open:true,
    closest:s=>({dataset:{akey:'rk1|s1'}}),
    querySelector:()=>null};
  handlers.toggle({target:det4});
  await new Promise(r=>setImmediate(r));
  assert.equal(fetches.length,1);
  assert.match(fetches[0],/\/api\/runs\/r1\/steps\/s1\/trajectory/);
  vm.runInContext('render('+JSON.stringify(data)+')',ctx);
  await open('rk1|s1');
  body=elem('dbody').innerHTML;
  assert.match(body,/class="arcent" data-asid="4" open/);
  // the expanded body carries the FULL text - multiline, html-escaped,
  // with a copy affordance; never the truncated head only
  assert.match(body,/LINE1\nLINE2   spaced/);
  assert.match(body,/&lt;b&gt;html&lt;\/b&gt;/);
  assert.match(body,/data-arc-copy="4"/);
  assert.match(body,/tool call 1 · edit/);
  // second fetch? no - cached by export@mtime
  assert.equal(fetches.length,1);
  // collapse-all returns to the compact closed state
  handlers.click({preventDefault:()=>{},target:{dataset:{},
    closest:s=>s==='[data-arc-none]'?{dataset:{}}:null}});
  body=elem('dbody').innerHTML;
  assert.doesNotMatch(body,/class="arcent" data-asid="4" open/);
  // expand-all opens every group+entry; run switch isolates the state
  handlers.click({preventDefault:()=>{},target:{dataset:{},
    closest:s=>s==='[data-arc-all]'?{dataset:{}}:null}});
  body=elem('dbody').innerHTML;
  assert.match(body,/class="arcgrp" data-ag="0" open/);
  assert.match(body,/class="arcent" data-asid="5" open/);
  await open('rk2|s1');                    // same step name, other run
  body=elem('dbody').innerHTML;
  assert.doesNotMatch(body,/data-asid="4" open/);   // no state leak
  assert.equal(fetches.length,1);                   // nothing fetched yet
  // expanding here hits the OTHER export - a different cache entry
  handlers.toggle({target:{classList:{contains:c=>c==='arcent'},
    dataset:{asid:'4'},open:true,
    closest:s=>({dataset:{akey:'rk2|s1'}}),querySelector:()=>null}});
  await new Promise(r=>setImmediate(r));
  assert.equal(fetches.length,2);
  assert.match(fetches[1],/\/api\/runs\/r2\/steps\/s1\/trajectory/);
  // closing the modal + reopening keeps the expansion per agent key
  await open('rk1|s1');
  body=elem('dbody').innerHTML;
  assert.match(body,/data-asid="4" open/);           // rk1 still expanded
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_copy_payload_is_verbatim_text(self):
        """arcPlainText composes the complete retained text (all
        sections, unescaped) - the Copy button never round-trips through
        HTML."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const document={getElementById:()=>({innerHTML:'',hidden:true}),
  querySelector:()=>null,querySelectorAll:()=>[],activeElement:null,
  addEventListener:()=>{}};
const ctx=vm.createContext({document,location:{reload:()=>{}},
  setTimeout:()=>{}});
vm.runInContext(src,ctx);
const ent={sid:4,src:'agent',model:'m',
  msg:'line <b>one</b>\nline two',think:'why',
  calls:[{name:'edit',args:'{"a":"<x>"}',result:'done\nok'}],
  extra_obs:['loose'],trunc:{reasoning:300000}};
const txt=vm.runInContext('arcPlainText('+JSON.stringify(ent)+')',ctx);
assert.ok(txt.includes('line <b>one</b>\nline two'));   // verbatim
assert.ok(txt.includes('{"a":"<x>"}'));
assert.ok(txt.includes('done\nok'));
assert.ok(txt.includes('--- observation ---\nloose'));
assert.ok(txt.includes('reasoning=300000'));
// and the HTML form escapes the same text
const h=vm.runInContext(
  'trajDetail["e@1"]={entries:{4:'+JSON.stringify(ent)+'}};'+
  'arcFullHTML({export:"e",arc:{em:1}},4)',ctx);
assert.ok(h.includes('&lt;b&gt;one&lt;/b&gt;'));
assert.ok(!h.includes('line <b>one</b>'));
assert.ok(h.includes('truncated for display'));
// an empty entry explains rather than fabricating content
const h2=vm.runInContext(
  'arcFullHTML({export:"e",arc:{em:1}},99)',ctx);
assert.ok(h2.includes('no detailed text was recorded'));
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)




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


class InterruptInstructTests(_RunControlBase):
    """'Interrupt + instruct': a durable intrq-* request sentinel the
    driver polls in the same slot as a stop flag. A confirmed submission
    kills the worker, folds the operator's text (+image refs) into the
    rnote channel, and respawns the SAME step - never 'stopped', never a
    retry attempt. Races resolve on the driver: a request landing after
    turn end banks into notes instead of respawning, and a pending flag
    survives a driver restart to apply exactly once."""

    def _intrq(self, d: Path, step: str, instructions="more context",
               attachments=None):
        f = conductor.intrq_flag(d, step)
        f.parent.mkdir(exist_ok=True)
        f.write_text(json.dumps(
            {"instructions": instructions,
             "attachments": attachments or [],
             "submitted_at": conductor.now_iso()}), encoding="utf-8")

    def _fake_worker_popen(self):
        """Patch subprocess.Popen so ONLY the step worker spawn is faked
        (its cmd is the only one carrying --prompt-file) - subprocess.run
        users like check_auth/git keep the real Popen, and no mock
        process lingers past the test."""
        real = conductor.subprocess.Popen

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                self.pid = None

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        def shim(cmd, **kw):
            if isinstance(cmd, (list, tuple)) and "--prompt-file" in cmd:
                return FakeProc(cmd, **kw)
            return real(cmd, **kw)
        return mock.patch.object(conductor.subprocess, "Popen", shim)

    def test_interrupt_respawns_with_instructions_and_attachments(self):
        """The end-to-end path: worker dies mid-sleep, the request folds
        into notes exactly once, and the SAME step respawns carrying
        INTERRUPT_NOTE + text + image paths - recorded 'done' with
        interrupts=1, sibling steps untouched, killed work committed."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "sleep=2 then write WORK.txt"

[[step]]
name = "s2"
project = "{proj.as_posix()}"
prompt = "write OTHER.txt"
''')
            # the flag poll rides WAIT_POLL_S slices - shrink it so the
            # worker is still mid-sleep when the request lands
            with mock.patch.object(conductor, "WAIT_POLL_S", 0.5):
                t, _ = self._start_run(m, runs)
                run_dir = self._wait_run_dir(runs, "s1")
                out = conductor.interrupt_step(
                    run_dir.name, "s1",
                    {"instructions": "also paint it blue",
                     "attachments": [{"name": "shot.png",
                                      "path": "C:/tmp/shot.png"}]},
                    runs)
                self.assertEqual(out["interrupting"], "s1")
                t.join(timeout=60)
            self.assertFalse(t.is_alive(), "run never finished")
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s1"]["status"], "done")
            self.assertEqual(st["steps"]["s1"].get("interrupts"), 1)
            # a respawn is NOT a retry: no attempt was spent
            self.assertEqual(st["steps"]["s1"].get("attempts"), 1)
            self.assertEqual(st["steps"]["s2"]["status"], "done")
            # the respawn prompt carries interrupt context + the text
            p = (run_dir / "prompts" / "s1.interrupt1.md").read_text(
                encoding="utf-8")
            self.assertIn("interrupted this step and resumed", p)
            self.assertIn("also paint it blue", p)
            self.assertIn("C:/tmp/shot.png", p)
            self.assertIn("IMAGE ATTACHMENTS", p)
            # the request is consumed: sentinel gone, the text lives in
            # the notes channel exactly once
            self.assertFalse(
                conductor.intrq_flag(run_dir, "s1").exists())
            notes = conductor.rnote_flag(run_dir, "s1").read_text(
                encoding="utf-8")
            self.assertIn("INTERRUPT", notes)
            self.assertIn("also paint it blue", notes)
            conductor.rnote_flag(run_dir, "s1").unlink(missing_ok=True)
            # only the RESPAWNED worker wrote - the killed one died
            # mid-sleep before producing its line
            self.assertEqual(
                len((proj / "WORK.txt").read_text().splitlines()), 1)
            self.assertIn("interrupted and resumed",
                          st["steps"]["s1"].get("note") or "")

    def test_interrupt_endpoint_gates(self):
        """Only a live running step on a driver that polls intrq accepts:
        finished/dead/old-driver steps, bad payloads, and a second
        submit while one is pending are all refused."""
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
            d = runs / "20260101-000000-t"
            d.mkdir(parents=True)
            live = {"run": "t", "manifest": str(m), "intrq": True,
                    "started_at": conductor.now_iso(),
                    "driver_pid": os.getpid(),
                    "steps": {
                        "s1": {"name": "s1", "status": "running",
                               "started_at": conductor.now_iso(),
                               "timeout_min": 60},
                        "s2": {"name": "s2", "status": "done"}}}
            (d / "state.json").write_text(json.dumps(live))
            try:
                out = conductor.interrupt_step(
                    d.name, "s1", {"instructions": "go slower"}, runs)
                self.assertEqual(out["interrupting"], "s1")
                req = json.loads(
                    conductor.intrq_flag(d, "s1").read_text())
                self.assertEqual(req["instructions"], "go slower")
                self.assertIn("submitted_at", req)
                # a second request while one is pending is refused -
                # the text must apply exactly once
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.interrupt_step(
                        d.name, "s1", {"instructions": "again"}, runs)
                self.assertEqual(e.exception.status, 409)
                # and the first request is untouched by the refusal
                self.assertEqual(
                    json.loads(conductor.intrq_flag(d, "s1").read_text())
                    ["instructions"], "go slower")
            finally:
                conductor.intrq_flag(d, "s1").unlink(missing_ok=True)
            # finished step: there is nothing to interrupt - the UI
            # offers resume notes instead
            with self.assertRaises(conductor.RequestError) as e:
                conductor.interrupt_step(
                    d.name, "s2", {"instructions": "x"}, runs)
            self.assertEqual(e.exception.status, 409)
            with self.assertRaises(conductor.RequestError) as e:
                conductor.interrupt_step(
                    d.name, "nope", {"instructions": "x"}, runs)
            self.assertEqual(e.exception.status, 404)
            # payload shape: instructions are required non-empty text,
            # attachments ride the same validation as requests
            for body in ({}, {"instructions": "   "},
                         {"instructions": 42},
                         {"instructions": "x" * 20001},
                         {"instructions": "x",
                          "attachments": [{"name": "../evil",
                                           "path": "p"}]}):
                with self.assertRaises(conductor.RequestError):
                    conductor.interrupt_step(d.name, "s1", body, runs)
            self.assertFalse(conductor.intrq_flag(d, "s1").exists())
            # a driver that predates the feature is refused
            live.pop("intrq")
            (d / "state.json").write_text(json.dumps(live))
            with self.assertRaises(conductor.RequestError) as e:
                conductor.interrupt_step(
                    d.name, "s1", {"instructions": "x"}, runs)
            self.assertEqual(e.exception.status, 409)

    def test_banked_request_folds_into_the_next_dispatch(self):
        """Restart recovery: an intrq flag left pending (the driver died
        before consuming it) applies exactly once to the step's next
        dispatch via the notes channel - the sentinel is deleted only
        after the notes write lands."""
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
            fixed = runs / "20260101-000000-t"

            def _fixed_run_dir(_cfg, _runs):
                fixed.mkdir(parents=True)
                return fixed
            self._intrq(fixed, "s1", "banked instructions")
            with mock.patch.object(conductor, "new_run_dir",
                                   side_effect=_fixed_run_dir):
                rc = run_manifest(m, runs)
            self.assertEqual(rc, 0)
            st = json.loads((fixed / "state.json").read_text())
            self.assertEqual(st["steps"]["s1"]["status"], "done")
            p = (fixed / "prompts" / "s1.md").read_text(encoding="utf-8")
            self.assertEqual(p.count("banked instructions"), 1)
            self.assertIn("NOTE FROM THE OPERATOR", p)
            self.assertIn("INTERRUPT", p)
            self.assertFalse(conductor.intrq_flag(fixed, "s1").exists())
            self.assertIn("banked interrupt",
                          st["steps"]["s1"].get("note") or "")
            conductor.rnote_flag(fixed, "s1").unlink(missing_ok=True)

    def test_interrupt_after_turn_end_banks_instead_of_respawning(self):
        """Natural-completion race: the request lands while the log
        already carries STATUS - the step records its real end state
        and the instructions bank into resume notes; no second worker
        ever spawns."""
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
            run_dir_box: list[Path] = []

            def fake_wait(proc, log, timeout_s, stop_flags=(),
                          timeout_flag=None, stall_s=None):
                # the turn ended, THEN the request landed - wait_worker
                # reports stopped because the sentinel was set at wait
                # return; run_step must see the STATUS and not respawn
                run_dir_box.append(Path(log).parent)
                log.write_text("STATUS: done\n", encoding="utf-8")
                self._intrq(Path(log).parent, "s1", "too late")
                return 0, False, False, True, 0.0

            with self._fake_worker_popen(), \
                 mock.patch.object(conductor, "session_ids",
                                   return_value=set()), \
                 mock.patch.object(conductor, "wait_worker", fake_wait):
                rc = run_manifest(m, runs)
            self.assertEqual(rc, 0)
            run_dir = run_dir_box[0]
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s1"]["status"], "done")
            self.assertIsNone(st["steps"]["s1"].get("interrupts"))
            self.assertFalse(
                (run_dir / "prompts" / "s1.interrupt1.md").exists())
            # banked into the notes channel for the next dispatch
            notes = conductor.rnote_flag(run_dir, "s1").read_text(
                encoding="utf-8")
            self.assertIn("too late", notes)
            self.assertFalse(
                conductor.intrq_flag(run_dir, "s1").exists())
            self.assertIn("after the worker",
                          st["steps"]["s1"].get("note") or "")
            conductor.rnote_flag(run_dir, "s1").unlink(missing_ok=True)

    def test_stop_beats_a_simultaneous_interrupt(self):
        """Stop + interrupt landing together: the run-level stop stays
        terminal ('stopped'), but the request still folds into notes so
        a later resume carries it - nothing is dropped on the floor."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "sleep=4 then write WORK.txt"
''')
            run_dir_box: list[Path] = []

            def fake_wait(proc, log, timeout_s, stop_flags=(),
                          timeout_flag=None, stall_s=None):
                # both sentinels land inside the same poll slice - the
                # run-level stop is terminal, the request still banks
                run_dir_box.append(Path(log).parent)
                self._intrq(Path(log).parent, "s1", "keep")
                conductor._flag_raise(
                    conductor.stop_flag(Path(log).parent).name)
                return 0, False, False, True, 0.0

            with self._fake_worker_popen(), \
                 mock.patch.object(conductor, "session_ids",
                                   return_value=set()), \
                 mock.patch.object(conductor, "wait_worker", fake_wait):
                rc = run_manifest(m, runs)
            run_dir = run_dir_box[0]
            st = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(st["steps"]["s1"]["status"], "stopped")
            self.assertIsNone(st["steps"]["s1"].get("interrupts"))
            self.assertFalse(
                (run_dir / "prompts" / "s1.interrupt1.md").exists())
            self.assertIn("banked into resume notes",
                          st["steps"]["s1"].get("note") or "")
            notes = conductor.rnote_flag(run_dir, "s1").read_text(
                encoding="utf-8")
            self.assertIn("keep", notes)
            conductor.rnote_flag(run_dir, "s1").unlink(missing_ok=True)
            conductor.stop_flag(run_dir).unlink(missing_ok=True)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_interrupt_composer_open_cancel_send_and_tombstone(self):
        """Dashboard side: the row menu offers 'Interrupt + instruct' on a
        running task; opening shows a composer, Cancel closes it without
        any POST, and the confirmed send hits /interrupt once with the
        instructions + attachment refs, then shows transition progress.
        A step that ends while the composer is open degrades to a
        tombstone instead of re-opening editable."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={}, posts=[], alerts=[];
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',scrollTop:0,style:{},querySelector:()=>null,
  querySelectorAll:()=>[]}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const sel={isCollapsed:true,toString:()=>''};
const window={innerWidth:1200,innerHeight:800,addEventListener:()=>{},
  getSelection:()=>sel};
const data={serve_boot:'b',generated_at:'2026-10-16T00:00:00',agents:[
  {step:'live-step',run:'live',run_dir:'/runs/live',run_key:'/runs/live',
   slug:'p',status:'running',task:'t1'},
  {step:'dead-step',run:'live',run_dir:'/runs/live',run_key:'/runs/live',
   slug:'p',status:'done',task:'t2'}],runs:[
  {run:'live',dir:'/runs/live',key:'/runs/live',counts:{running:1}}]};
const fetch=async(url,opt)=>{
  posts.push({url,body:opt&&opt.body?JSON.parse(opt.body):null});
  if(url.startsWith('/api/state')) return {ok:true,json:async()=>data};
  return {ok:true,json:async()=>({interrupting:'live-step'}),
    text:async()=>''}; };
const ctx=vm.createContext({document,window,fetch,confirm:()=>true,
  alert:m=>alerts.push(m),location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const cls=c=>({contains:x=>c.split(' ').includes(x),add(){},remove(){}});
const agenRow=i=>({cls:'agen',dataset:{i:String(i)},classList:cls('agen'),
  isConnected:true,focus(){},matches:s=>s==='tr.agen'||s==='tr.runrow,tr.agen',
  getBoundingClientRect:()=>({left:10,top:20,bottom:30})});
const onRow=row=>({isContentEditable:false,dataset:{},
  closest:sel=>{
    if(sel==='#ctxmenu')return null;
    if(sel.indexOf('input')===0)return null;
    if(sel==='tr.agen')return row.cls==='agen'?row:null;
    if(sel==='tr.runrow,tr.agen')return row;
    return null;}});
const rclick=t=>{let pd=0;handlers.contextmenu({target:t,clientX:60,
  clientY:70,preventDefault:()=>{pd++;}});return pd;};
const btn=dataset=>({id:'',dataset,disabled:false,
  closest:sel=>{
    if(sel==='#ctxmenu')return {inMenu:1};
    const m=sel.match(/^\[data-([a-z-]+)\]$/);
    if(m){const k=m[1].replace(/-([a-z])/g,(s,c)=>c.toUpperCase());
      return k in dataset?{dataset}:null;}
    return null;}});
const KEY='/runs/live|live-step';
(async()=>{
  const m=elem('ctxmenu'), body=()=>elem('dbody').innerHTML;
  // the running row's menu leads with 'Interrupt + instruct' + Stop -
  // a finished step offers neither
  assert.equal(rclick(onRow(agenRow(0))),1);
  assert.match(m.innerHTML,/data-intr-open="\/runs\/live\|live-step"[^>]*>Interrupt \+ instruct/);
  assert.match(m.innerHTML,/data-stop-agent="live"[^>]*>Stop this step/);
  assert.equal(rclick(onRow(agenRow(1))),1);
  assert.doesNotMatch(m.innerHTML,/data-intr-open|data-stop-agent/);
  // open -> the composer section appears with the instruction textarea;
  // composing is free - no POST happened anywhere
  await handlers.click({target:btn({intrOpen:KEY}),
    preventDefault:()=>{}});
  assert.match(body(),/id="d-instr"/);
  assert.match(body(),/Interrupt \+ resume/);
  posts.length=0;
  // Cancel is a pure view change: the section closes, still no POST
  await handlers.click({target:btn({intrCancel:''}),
    preventDefault:()=>{}});
  assert.doesNotMatch(body(),/id="d-instr"/);
  assert.equal(posts.length,0);
  // reopen + send: instructions text and an attached image ride the
  // POST body - the request went exactly once
  await handlers.click({target:btn({intrOpen:KEY}),
    preventDefault:()=>{}});
  vm.runInContext('intrAttach["'+KEY+'"]=' +
    '[{name:"shot.png",path:"C:/tmp/shot.png"}];' +
    'intrComposerHTML(AGENTS[0])',ctx);
  elem('d-instr').value='do X differently';
  posts.length=0;
  await handlers.click({target:btn({intrSend:'live',step:'live-step'}),
    preventDefault:()=>{}});
  const intr=posts.filter(p=>p.url.includes('/interrupt'));
  assert.equal(intr.length,1);          // exactly once - no double submit
  assert.equal(intr[0].url,'/api/runs/live/steps/live-step/interrupt');
  assert.deepEqual(intr[0].body,{instructions:'do X differently',
    attachments:[{name:'shot.png',path:'C:/tmp/shot.png'}]});
  // post-submit the section follows the transition - the worker-stopped
  // phase shows until the respawn counter moves
  assert.match(body(),/Interrupt \+ instruct|resum|requested/);
  // a pending request greys the send control - no double submit
  data.agents[0].intr_pending=true;
  vm.runInContext('intrSent["'+KEY+'"]=null',ctx);
  vm.runInContext('render(lastData)',ctx);
  assert.match(body(),/Interrupt pending/);
  assert.match(body(),/disabled[^>]*>Interrupt pending|Interrupt pending[^<]*<\/button>/);
  // a step that ends while the composer is open degrades to a
  // tombstone - the textarea never comes back editable
  data.agents[0].intr_pending=false;
  data.agents[0].status='done';
  vm.runInContext('render(lastData)',ctx);
  assert.match(body(),/finished \(done\) while the composer was open/);
  assert.doesNotMatch(body(),/<textarea id="d-instr"/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


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
// task rows no longer open the modal on a bare click - the step-name
// button (data-agent-detail, keyed by agentKey) is the way in
const row=i=>({dataset:{},closest:sel=>sel==='[data-agent-detail]'
  ?{dataset:{agentDetail:'/r|s'+String(i+1)}}:null,preventDefault:()=>{}});
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


class RunLifecycleCleanupTests(unittest.TestCase):
    """Auto-archive: a run whose ended_at is older than ARCHIVE_AFTER_S is
    stamped state['archived'] and drops out of the default dashboard view
    (kept on disk; ?archived=1 / Unarchive brings it back). 'Delete run'
    moves the whole record to recoverable trash (48h, Undo/Restore), gated
    on {confirm:true} and no live work."""

    def _ended_run(self, runs: Path, name: str, hours_ago: float,
                   **extra) -> Path:
        rd = runs / name
        rd.mkdir(parents=True, exist_ok=True)
        t = (conductor.dt.datetime.now()
             - conductor.dt.timedelta(hours=hours_ago)).isoformat(
                 timespec="seconds")
        st = {"run": name, "started_at": t, "ended_at": t,
              "steps": {"a": {"status": "done", "started_at": t,
                              "ended_at": t}},
              **extra}
        (rd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        (rd / "events.jsonl").write_text("{}\n", encoding="utf-8")
        return rd

    def test_archive_marks_only_old_finished_runs(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            old = self._ended_run(runs, "20260101-000000-old", 49)
            new = self._ended_run(runs, "20260102-000000-new", 1)
            live = self._ended_run(runs, "20260103-000000-unf", 99)
            st = json.loads((live / "state.json").read_text())
            del st["ended_at"]           # interrupted/unfinished: untouched
            (live / "state.json").write_text(json.dumps(st))
            lines = conductor.archive_runs(runs)
            self.assertTrue(any("old" in l for l in lines))
            self.assertTrue(json.loads((old / "state.json").read_text())
                            .get("archived"))
            for d in (new, live):
                self.assertNotIn("archived",
                                 json.loads((d / "state.json").read_text()))

    def test_archive_threshold_and_disable(self):
        """ARCHIVE_AFTER_S is the knob (CONDUCTOR_ARCHIVE_HOURS at boot);
        0 disables the pass entirely."""
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            mid = self._ended_run(runs, "20260101-000000-mid", 2)
            with mock.patch.object(conductor, "ARCHIVE_AFTER_S", 0):
                self.assertEqual(conductor.archive_runs(runs), [])
            self.assertNotIn("archived",
                             json.loads((mid / "state.json").read_text()))
            with mock.patch.object(conductor, "ARCHIVE_AFTER_S", 3600):
                conductor.archive_runs(runs)
            self.assertTrue(json.loads((mid / "state.json").read_text())
                            .get("archived"))

    def test_archived_runs_hidden_then_listed(self):
        """Default /api/state omits archived runs and their agents but
        still counts them; include_archived lists them stamped."""
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            self._ended_run(runs, "20260101-000000-old", 49,
                            archived=conductor.now_iso())
            self._ended_run(runs, "20260102-000000-new", 1)
            d = conductor.collect_dashboard(runs)
            self.assertEqual({r["run"] for r in d["runs"]},
                             {"20260102-000000-new"})
            self.assertEqual(d["archived_runs"], 1)
            self.assertEqual(len(d["agents"]), 1)
            d = conductor.collect_dashboard(runs, include_archived=True)
            self.assertEqual(len(d["runs"]), 2)
            self.assertEqual(len(d["agents"]), 2)
            old = next(r for r in d["runs"]
                       if r["run"] == "20260101-000000-old")
            self.assertTrue(old["archived"])
            self.assertEqual(d["archived_runs"], 1)

    def test_set_run_archived_toggle(self):
        """Manual Archive/Unarchive on a finished run flips the stamp; a
        live step refuses to be hidden."""
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            rd = self._ended_run(runs, "20260101-000000-t", 1)
            out = conductor.set_run_archived(rd.name, {}, runs)
            self.assertTrue(out["archived"])
            self.assertTrue(json.loads((rd / "state.json").read_text())
                            ["archived"])
            out = conductor.set_run_archived(rd.name, {"archived": False},
                                             runs)
            self.assertFalse(out["archived"])
            self.assertNotIn("archived",
                             json.loads((rd / "state.json").read_text()))
            # live step -> 409
            p = subprocess.Popen([sys.executable, "-c",
                                  "import time;time.sleep(30)"])
            try:
                st = json.loads((rd / "state.json").read_text())
                st["steps"]["a"] = {"status": "running", "pid": p.pid,
                                    "started_at": conductor.now_iso()}
                (rd / "state.json").write_text(json.dumps(st))
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.set_run_archived(rd.name, {}, runs)
                self.assertEqual(e.exception.status, 409)
            finally:
                p.terminate()
                p.wait()

    def test_delete_run_removes_the_record(self):
        """'Delete' now lands the record in data/trash - the run dir leaves
        runs/, the entry carries meta + payload, and Undo restores it."""
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            trash = Path(td) / "trash"
            rd = self._ended_run(runs, "20260101-000000-t", 1)
            (rd / "prompts").mkdir()
            (rd / "prompts" / "a.md").write_text("p", encoding="utf-8")
            with self.assertRaises(conductor.RequestError):
                conductor.delete_run(rd.name, {}, runs)      # unconfirmed
            self.assertTrue((rd / "state.json").is_file())
            out = conductor.delete_run(rd.name, {"confirm": True}, runs,
                                       trash_dir=trash)
            self.assertEqual(out["deleted"], rd.name)
            self.assertTrue(out["trashed"])
            self.assertFalse(rd.exists())
            entry = trash / out["trash_id"]
            self.assertTrue((entry / "meta.json").is_file())
            self.assertTrue((entry / "payload" / "00-run" /
                             "state.json").is_file())
            self.assertTrue((entry / "payload" / "00-run" / "prompts" /
                             "a.md").is_file())
            with self.assertRaises(conductor.RequestError) as e:
                conductor.delete_run(rd.name, {"confirm": True}, runs,
                                     trash_dir=trash)
            self.assertEqual(e.exception.status, 404)
            out = conductor.restore_trash(out["trash_id"], trash, runs,
                                          Path(td) / "requests",
                                          Path(td) / "manifests")
            self.assertEqual(out["restored"], rd.name)
            self.assertTrue((rd / "state.json").is_file())
            self.assertFalse(entry.exists())

    def test_delete_run_refuses_a_live_step(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            runs = base / "data" / "runs"
            rd = self._ended_run(runs, "20260101-000000-t", 1)
            p = subprocess.Popen([sys.executable, "-c",
                                  "import time;time.sleep(30)"])
            try:
                st = json.loads((rd / "state.json").read_text())
                st["steps"]["a"] = {"status": "running", "pid": p.pid,
                                    "started_at": conductor.now_iso()}
                (rd / "state.json").write_text(json.dumps(st))
                with self.assertRaises(conductor.RequestError) as e:
                    conductor.delete_run(rd.name, {"confirm": True}, runs)
                self.assertEqual(e.exception.status, 409)
                self.assertTrue(rd.exists())
            finally:
                p.terminate()
                p.wait()

    def test_sweep_also_archives_and_resume_unarchives(self):
        """sweep folds the archive pass in (so serve startup gets it), and
        --resume drops the stamp - a re-entered run is visible again."""
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
            self.assertEqual(run_manifest(m, runs), 0)
            rd = next(runs.iterdir())
            st = json.loads((rd / "state.json").read_text())
            old = (conductor.dt.datetime.now()
                   - conductor.dt.timedelta(hours=49)).isoformat(
                       timespec="seconds")
            st["ended_at"] = old
            (rd / "state.json").write_text(json.dumps(st))
            lines = conductor.sweep_runs(runs)
            self.assertTrue(any("archived" in l for l in lines))
            st = json.loads((rd / "state.json").read_text())
            self.assertTrue(st.get("archived"))
            self.assertEqual(run_manifest(m, runs, resume=str(rd)), 0)
            st = json.loads((rd / "state.json").read_text())
            self.assertNotIn("archived", st)

    def test_control_names_in_dashboard_html(self):
        html = conductor._DASH_HTML
        for name in ("Archive run", "Unarchive", "Delete run",
                     "archived"):
            self.assertIn(name, html)


class RecoverableTrashTests(unittest.TestCase):
    """Delete/discard is a move to data/trash (48h retention), never an
    rmtree: entries carry meta + payload, restore puts each back at its
    recorded destination (collisions rename, never overwrite), a bounded
    sweep purges only the expired, and 'Empty trash' is the one
    irreversible op behind an explicit confirm. All fixtures are
    temporary."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.proj = make_project(self.base)
        self.runs = self.base / "runs"
        self.requests = self.base / "requests"
        self.manifests = self.base / "manifests"
        self.manifests.mkdir()
        self.trash = self.base / "trash"
        patch = mock.patch.object(conductor, "load_projects", return_value={
            "proj": {"path": self.proj, "cloud_ok": False}})
        patch.start()
        self.addCleanup(patch.stop)
        self.spool = self.base / "planner-spool"
        for target, value in (("PLANNER_SPOOL", self.spool),
                              ("WATCH_POLL_S", 0.02)):
            p = mock.patch.object(conductor, target, value)
            p.start()
            self.addCleanup(p.stop)

    # ---- fixtures ------------------------------------------------------
    def _ended_run(self, name, hours_ago=1, **extra):
        rd = (self.runs / name).resolve()
        rd.mkdir(parents=True, exist_ok=True)
        t = (conductor.dt.datetime.now()
             - conductor.dt.timedelta(hours=hours_ago)).isoformat(
                 timespec="seconds")
        st = {"run": name, "started_at": t, "ended_at": t,
              "run_dir": str(rd),
              "steps": {"a": {"status": "done", "started_at": t,
                              "ended_at": t,
                              "prompt": str(rd / "prompts" / "a.md"),
                              "stdout_log": str(rd / "a.stdout.log"),
                              "export": str(rd / "a.export.json")}},
              **extra}
        (rd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        (rd / "events.jsonl").write_text("{}\n", encoding="utf-8")
        (rd / "a.stdout.log").write_text("log\n", encoding="utf-8")
        (rd / "prompts").mkdir(exist_ok=True)
        (rd / "prompts" / "a.md").write_text("prompt\n", encoding="utf-8")
        return rd

    def _proposal(self, description="Tidy the catalog"):
        return f'''[run]
name = "catalog-cleanup"
description = "{description}"
on_failure = "continue"

[defaults]
project = "proj"
model = "swe-2"
effort = "max"
mode = "bypass"

[[step]]
name = "organize"
prompt = "Organize the catalog and verify results."
'''

    def _ready(self, **fields):
        payload = {"raw": "Please tidy the catalog carefully",
                   "project": "proj", "model": "swe-2", "effort": "max"}
        payload.update(fields)
        rec = conductor.create_request(payload, self.requests, self.runs)
        return conductor.save_request(
            rec["id"], {"manifest": self._proposal()},
            self.requests, self.manifests, self.runs)

    def _entry_meta(self, entry_id):
        return json.loads((self.trash / entry_id / "meta.json").read_text(
            encoding="utf-8"))

    # ---- run trash ------------------------------------------------------
    def test_delete_run_moves_everything_to_a_recorded_entry(self):
        rd = self._ended_run("20260101-000000-t",
                             request_id="20260101-000000-abcdef01")
        out = conductor.delete_run(rd.name, {"confirm": True}, self.runs,
                                   trash_dir=self.trash)
        self.assertFalse(rd.exists())
        meta = self._entry_meta(out["trash_id"])
        self.assertEqual(meta["kind"], "run")
        self.assertEqual(meta["ref"], rd.name)
        self.assertEqual(meta["origin"], str(rd.resolve()))
        self.assertEqual([i["slot"] for i in meta["items"]], ["run"])
        # 48h retention, persisted on disk so a restart still knows
        self.assertEqual(
            round((conductor._iso_ts(meta["expires_at"])
                   - conductor._iso_ts(meta["deleted_at"])) / 3600), 48)
        # hidden from normal listings, visible in trash
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual(d["runs"], [])
        self.assertEqual(d["trash_count"], 1)
        entries = conductor.list_trash(self.trash, self.requests)
        self.assertEqual([e["id"] for e in entries], [out["trash_id"]])
        self.assertGreater(entries[0]["expires_in_s"], 0)
        # audit trail survives the record's own disappearance
        log = (self.trash / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("trash_add", log)

    def test_restore_run_returns_the_record_byte_for_byte(self):
        rd = self._ended_run("20260101-000000-t")
        out = conductor.delete_run(rd.name, {"confirm": True}, self.runs,
                                   trash_dir=self.trash)
        res = conductor.restore_trash(out["trash_id"], self.trash,
                                      self.runs, self.requests,
                                      self.manifests)
        self.assertEqual(res["restored_as"], rd.name)
        self.assertFalse(res["renamed"])
        self.assertTrue((rd / "state.json").is_file())
        self.assertTrue((rd / "prompts" / "a.md").is_file())
        st = json.loads((rd / "state.json").read_text())
        self.assertEqual(st["steps"]["a"]["stdout_log"],
                         str(rd / "a.stdout.log"))
        events = (rd / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("run_restored", events)
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual([r["run"] for r in d["runs"]], [rd.name])
        self.assertEqual(d["trash_count"], 0)

    def test_restore_run_name_collision_renames_without_overwriting(self):
        rd = self._ended_run("20260101-000000-t")
        out = conductor.delete_run(rd.name, {"confirm": True}, self.runs,
                                   trash_dir=self.trash)
        # newer work took the name while the old record sat in trash
        newer = self._ended_run(rd.name, 0)
        (newer / "state.json").write_text(
            json.dumps({"run": rd.name, "steps": {}}), encoding="utf-8")
        res = conductor.restore_trash(out["trash_id"], self.trash,
                                      self.runs, self.requests,
                                      self.manifests)
        self.assertTrue(res["renamed"])
        self.assertEqual(res["restored_as"], rd.name + "-restored")
        # the newer run is untouched, the restored dir's self-paths moved
        self.assertEqual(json.loads((newer / "state.json").read_text())
                         ["steps"], {})
        restored = self.runs / res["restored_as"]
        st = json.loads((restored / "state.json").read_text())
        self.assertEqual(st["run_dir"], str(restored))
        self.assertEqual(st["steps"]["a"]["stdout_log"],
                         str(restored / "a.stdout.log"))

    def test_delete_run_refuses_watched_trash_destination(self):
        """A live self-hosted worker watching the sandbox covers runs/ AND
        trash/ - the move would write into its cwd, so 409 (trap 5)."""
        self._ended_run("20260101-000000-t")
        watcher = self.runs / "20260102-000000-live"
        watcher.mkdir(parents=True)
        (watcher / "state.json").write_text(json.dumps({"steps": {
            "live": {"status": "running",
                     "started_at": conductor.now_iso(), "timeout_min": 60,
                     "work_dir": str(self.base)}}}))
        with self.assertRaises(conductor.RequestError) as e:
            conductor.delete_run("20260101-000000-t", {"confirm": True},
                                 self.runs, trash_dir=self.trash)
        self.assertEqual(e.exception.status, 409)
        self.assertTrue((self.runs / "20260101-000000-t").is_dir())

    def test_live_step_and_active_merge_still_refuse(self):
        rd = self._ended_run("20260101-000000-t")
        st = json.loads((rd / "state.json").read_text())
        st["steps"]["a"]["status"] = "running"
        st["steps"]["a"]["started_at"] = conductor.now_iso()
        (rd / "state.json").write_text(json.dumps(st))
        with self.assertRaises(conductor.RequestError) as e:
            conductor.delete_run(rd.name, {"confirm": True}, self.runs,
                                 trash_dir=self.trash)
        self.assertEqual(e.exception.status, 409)
        with conductor._MERGE_LOCK:
            conductor._MERGE_BUSY.add(rd.name)
        try:
            st["steps"]["a"]["status"] = "done"
            (rd / "state.json").write_text(json.dumps(st))
            with self.assertRaises(conductor.RequestError) as e:
                conductor.delete_run(rd.name, {"confirm": True}, self.runs,
                                     trash_dir=self.trash)
            self.assertEqual(e.exception.status, 409)
        finally:
            conductor._MERGE_BUSY.discard(rd.name)
        self.assertTrue(rd.is_dir())

    # ---- request trash --------------------------------------------------
    def test_discard_moves_the_whole_bundle_and_restores_it(self):
        rec = self._ready()
        rid = rec["id"]
        manifest = Path(rec["manifest"])
        # side artifacts a planner pass leaves behind
        (self.requests / f"{rid}.gen.log").write_text("planner log\n")
        (self.requests / f"{rid}.draft.toml").write_text("draft\n")
        spool = conductor._spool_dir(rid)
        spool.mkdir(parents=True)
        (spool / "gen.log").write_text("live spool\n")
        # the parked overlay is authoritative - distinguish it on restore
        (spool / "request.json").write_text(
            json.dumps({**rec, "status": "ready", "id": rid,
                        "raw": "overlay wins"}))
        out = conductor.discard_request(rid, self.requests,
                                        self.manifests, self.runs,
                                        self.trash)
        self.assertTrue(out["trashed"])
        self.assertFalse((self.requests / f"{rid}.json").exists())
        self.assertFalse(manifest.exists())
        self.assertFalse(spool.exists())
        self.assertFalse((self.requests / f"{rid}.gen.log").exists())
        meta = self._entry_meta(out["trash_id"])
        self.assertEqual(meta["kind"], "request")
        slots = {i["slot"] for i in meta["items"]}
        self.assertEqual(slots, {"record", "manifest", "artifact", "spool"})
        # the record payload is the parked overlay, not the stale shadow
        payload = self.trash / out["trash_id"] / "payload"
        recp = next(payload.glob("*-record"))
        self.assertEqual(json.loads(recp.read_text())["raw"],
                         "overlay wins")
        self.assertTrue((payload / f"{meta['items'][0]['payload']}" /
                         "gen.log").is_file())
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual(d["runs"], [])
        self.assertEqual(d["trash_count"], 1)
        res = conductor.restore_trash(out["trash_id"], self.trash,
                                      self.runs, self.requests,
                                      self.manifests)
        self.assertEqual(res["restored"], rid)
        det = conductor.request_details(rid, self.requests,
                                        self.manifests)
        self.assertEqual(det["status"], "ready")
        self.assertEqual(det["manifest_text"], self._proposal())
        self.assertEqual(det["proposed_steps"],
                         rec["proposed_steps"])
        self.assertTrue(spool.is_dir())          # parked spool restored
        self.assertTrue((self.requests / f"{rid}.gen.log").is_file())
        self.assertTrue((self.requests / f"{rid}.draft.toml").is_file())

    def test_discard_preserves_attachments_and_followup_flag(self):
        att = self.base / "attachments" / "shot.png"
        att.parent.mkdir(parents=True)
        att.write_bytes(b"\x89PNG")
        rec = self._ready(attachments=[{"name": "shot.png",
                                      "path": str(att)}], followup=True)
        manifest = Path(rec["manifest"])
        self.assertIn("follow-up-plan", manifest.read_text())
        out = conductor.discard_request(rec["id"], self.requests,
                                        self.manifests, self.runs,
                                        self.trash)
        conductor.restore_trash(out["trash_id"], self.trash, self.runs,
                                self.requests, self.manifests)
        det = conductor.request_details(rec["id"], self.requests,
                                        self.manifests)
        self.assertEqual(det["attachments"],
                         [{"name": "shot.png", "path": str(att)}])
        self.assertTrue(det["followup"])
        self.assertIn("follow-up-plan", det["manifest_text"])
        self.assertTrue(att.is_file())      # shared file never moved

    def test_manifest_collision_restores_beside_newer_work(self):
        rec = self._ready()
        old_manifest = Path(rec["manifest"])
        out = conductor.discard_request(rec["id"], self.requests,
                                        self.manifests, self.runs,
                                        self.trash)
        # a republish wrote a newer file at the same name
        old_manifest.write_text("[run]\nname = \"newer\"\n")
        res = conductor.restore_trash(out["trash_id"], self.trash,
                                      self.runs, self.requests,
                                      self.manifests)
        self.assertEqual(res["restored"], rec["id"])
        self.assertEqual(old_manifest.read_text(),
                         "[run]\nname = \"newer\"\n")
        restored = conductor._read_request_file(rec["id"], self.requests)
        self.assertTrue(Path(restored["manifest"]).name.startswith(
            f"{rec['id']}-"))
        self.assertIn("-restored", restored["manifest"])
        self.assertIn("catalog-cleanup",
                      Path(restored["manifest"]).read_text())

    def test_occupied_request_record_refuses_the_whole_restore(self):
        rec = self._ready()
        out = conductor.discard_request(rec["id"], self.requests,
                                        self.manifests, self.runs,
                                        self.trash)
        (self.requests / f"{rec['id']}.json").write_text(
            json.dumps({"id": rec["id"], "status": "draft"}))
        with self.assertRaises(conductor.RequestError) as e:
            conductor.restore_trash(out["trash_id"], self.trash,
                                    self.runs, self.requests,
                                    self.manifests)
        self.assertEqual(e.exception.status, 409)
        # nothing moved - the entry is intact for a retry
        self.assertEqual(len(conductor.list_trash(self.trash,
                                                  self.requests)), 1)
        self.assertTrue((self.trash / out["trash_id"]).is_dir())

    # ---- expiry / sweep ---------------------------------------------------
    def test_expired_entries_purge_on_a_controllable_clock(self):
        rd = self._ended_run("20260101-000000-t")
        out = conductor.delete_run(rd.name, {"confirm": True}, self.runs,
                                   trash_dir=self.trash)
        meta = self._entry_meta(out["trash_id"])
        exp_ts = conductor._iso_ts(meta["expires_at"])
        self.assertIsNotNone(exp_ts)
        t = time.time()
        # not yet expired -> kept
        self.assertEqual(conductor.sweep_trash(
            self.trash, self.requests, self.runs, now=t), [])
        self.assertTrue((self.trash / out["trash_id"]).is_dir())
        # past expiry -> purged, payload gone for real this time
        lines = conductor.sweep_trash(
            self.trash, self.requests, self.runs,
            now=exp_ts + 1)
        self.assertEqual(len(lines), 1)
        self.assertIn(out["trash_id"], lines[0])
        self.assertFalse((self.trash / out["trash_id"]).exists())
        self.assertIn("trash_purge", (self.trash / "events.jsonl")
                      .read_text(encoding="utf-8"))

    def test_sweep_is_bounded_and_skips_unexpired_and_inflight(self):
        ids = []
        for i in range(3):
            rd = self._ended_run(f"2026010{i + 1}-000000-t{i}")
            ids.append(conductor.delete_run(
                rd.name, {"confirm": True}, self.runs,
                trash_dir=self.trash)["trash_id"])
        fresh = self._ended_run("20260110-000000-fresh")
        fresh_id = conductor.delete_run(
            fresh.name, {"confirm": True}, self.runs,
            trash_dir=self.trash)["trash_id"]
        # an entry with no meta yet is an in-flight move, never purged
        inflight = self.trash / "20260101-000000-ffffff"
        (inflight / "payload").mkdir(parents=True)
        # age the first three entries' expiries into the past
        for eid in ids:
            mp = self.trash / eid / "meta.json"
            meta = json.loads(mp.read_text())
            meta["expires_at"] = (conductor.dt.datetime.now()
                                  - conductor.dt.timedelta(hours=1)
                                  ).isoformat(timespec="seconds")
            mp.write_text(json.dumps(meta))
        lines = conductor.sweep_trash(self.trash, self.requests,
                                      self.runs, limit=2)
        self.assertEqual(len(lines), 2)
        left = {d.name for d in self.trash.iterdir() if d.is_dir()}
        self.assertEqual(len(left & set(ids)), 1)       # cap held
        self.assertIn(fresh_id, left)
        self.assertIn("20260101-000000-ffffff", left)
        lines = conductor.sweep_trash(self.trash, self.requests,
                                      self.runs, limit=64)
        self.assertEqual(len(lines), 1)
        self.assertIn(fresh_id, {d.name for d in self.trash.iterdir()
                                 if d.is_dir()})

    def test_sweep_folds_into_sweep_runs(self):
        rd = self._ended_run("20260101-000000-t")
        out = conductor.delete_run(rd.name, {"confirm": True}, self.runs,
                                   trash_dir=self.trash)
        mp = self.trash / out["trash_id"] / "meta.json"
        meta = json.loads(mp.read_text())
        meta["expires_at"] = "2020-01-01T00:00:00"
        mp.write_text(json.dumps(meta))
        lines = conductor.sweep_runs(self.runs)
        self.assertTrue(any("purged" in l for l in lines))
        self.assertFalse((self.trash / out["trash_id"]).exists())

    def test_sweep_dry_runs_while_the_data_tree_is_watched(self):
        rd = self._ended_run("20260101-000000-t")
        out = conductor.delete_run(rd.name, {"confirm": True}, self.runs,
                                   trash_dir=self.trash)
        mp = self.trash / out["trash_id"] / "meta.json"
        meta = json.loads(mp.read_text())
        meta["expires_at"] = "2020-01-01T00:00:00"
        mp.write_text(json.dumps(meta))
        watcher = self.runs / "20260102-000000-live"
        watcher.mkdir(parents=True)
        (watcher / "state.json").write_text(json.dumps({"steps": {
            "live": {"status": "running",
                     "started_at": conductor.now_iso(), "timeout_min": 60,
                     "work_dir": str(self.base)}}}))
        lines = conductor.sweep_trash(self.trash, self.requests, self.runs)
        self.assertTrue(any(l.startswith("DRY-RUN") for l in lines))
        self.assertTrue((self.trash / out["trash_id"]).is_dir())

    # ---- empty trash -----------------------------------------------------
    def test_empty_trash_requires_confirm_and_is_final(self):
        rd = self._ended_run("20260101-000000-t")
        out = conductor.delete_run(rd.name, {"confirm": True}, self.runs,
                                   trash_dir=self.trash)
        with self.assertRaises(conductor.RequestError) as e:
            conductor.empty_trash({}, self.trash, self.requests,
                                  self.runs)
        self.assertEqual(e.exception.status, 400)
        self.assertTrue((self.trash / out["trash_id"]).is_dir())
        res = conductor.empty_trash({"confirm": True}, self.trash,
                                    self.requests, self.runs)
        self.assertEqual(res["emptied"], 1)
        self.assertFalse((self.trash / out["trash_id"]).exists())
        # a restored-after-empty id 404s - nothing recoverable remains
        with self.assertRaises(conductor.RequestError) as e:
            conductor.restore_trash(out["trash_id"], self.trash,
                                    self.runs, self.requests,
                                    self.manifests)
        self.assertEqual(e.exception.status, 404)
        self.assertIn("trash_empty", (self.trash / "events.jsonl")
                      .read_text(encoding="utf-8"))

    # ---- legacy tombstones -----------------------------------------------
    def _legacy_discard(self, hours_ago=1):
        rec = self._ready()
        manifest = Path(rec["manifest"])
        manifest.unlink()
        rec.update(status="discarded", proposed_steps=[], manifest=None)
        conductor._save_request(rec, self.requests)
        return rec

    def test_legacy_discards_list_restore_and_expire(self):
        rec = self._legacy_discard()
        entries = conductor.list_trash(self.trash, self.requests)
        self.assertEqual([e["id"] for e in entries],
                         [f"legacy-{rec['id']}"])
        self.assertTrue(entries[0]["legacy"])
        res = conductor.restore_trash(f"legacy-{rec['id']}", self.trash,
                                      self.runs, self.requests,
                                      self.manifests)
        self.assertEqual(res["restored"], rec["id"])
        self.assertEqual(conductor.request_details(
            rec["id"], self.requests, self.manifests)["status"], "draft")
        # expiry is measured from the recorded deletion (updated_at)
        rec = self._legacy_discard()
        lines = conductor.sweep_trash(self.trash, self.requests,
                                    self.runs,
                                    now=time.time() +
                                    conductor.TRASH_RETAIN_S + 10)
        self.assertTrue(any(f"legacy-{rec['id']}" in l for l in lines))
        self.assertFalse((self.requests / f"{rec['id']}.json").exists())

    # ---- partial failure / path validation --------------------------------
    def test_partial_restore_failure_keeps_the_remaining_items(self):
        rec = self._ready()
        (self.requests / f"{rec['id']}.gen.log").write_text("log\n")
        out = conductor.discard_request(rec["id"], self.requests,
                                        self.manifests, self.runs,
                                        self.trash)
        # make every requests/ destination unwritable: the dir is a file
        self.requests.rename(self.base / "requests-away")
        (self.requests).write_text("blocked")
        try:
            with self.assertRaises(conductor.RequestError) as e:
                conductor.restore_trash(out["trash_id"], self.trash,
                                        self.runs, self.requests,
                                        self.manifests)
            self.assertIn("partial", str(e.exception))
        finally:
            self.requests.unlink()
            (self.base / "requests-away").rename(self.requests)
        # the entry kept ONLY what failed - restore retries cleanly
        meta = self._entry_meta(out["trash_id"])
        self.assertTrue(any(i["slot"] == "record" for i in meta["items"]))
        res = conductor.restore_trash(out["trash_id"], self.trash,
                                      self.runs, self.requests,
                                      self.manifests)
        self.assertEqual(res["restored"], rec["id"])
        self.assertTrue((self.requests / f"{rec['id']}.gen.log")
                        .is_file())

    def test_a_crafted_meta_cannot_escape_the_managed_roots(self):
        rec = self._ready()
        out = conductor.discard_request(rec["id"], self.requests,
                                        self.manifests, self.runs,
                                        self.trash)
        entry = self.trash / out["trash_id"]
        mp = entry / "meta.json"
        meta = json.loads(mp.read_text())
        # A real payload whose recorded destination escapes its root...
        (entry / "payload" / "99-evil").write_text("x")
        meta["items"].append({"slot": "artifact", "payload": "99-evil",
                              "dest": "requests", "name": "../evil.json",
                              "dir": False})
        mp.write_text(json.dumps(meta))
        with self.assertRaises(conductor.RequestError):
            conductor.restore_trash(out["trash_id"], self.trash,
                                    self.runs, self.requests,
                                    self.manifests)
        # ...and a payload path that escapes payload/ itself - both are
        # refused BEFORE anything moves
        meta["items"][-1] = {"slot": "artifact", "payload": "../state.json",
                             "dest": "requests", "name": "safe.json",
                             "dir": False}
        mp.write_text(json.dumps(meta))
        with self.assertRaises(conductor.RequestError):
            conductor.restore_trash(out["trash_id"], self.trash,
                                    self.runs, self.requests,
                                    self.manifests)
        meta["items"].pop()
        mp.write_text(json.dumps(meta))
        self.assertFalse((self.base / "evil.json").exists())
        self.assertFalse((self.base / "safe.json").exists())
        self.assertTrue((entry / "meta.json").is_file())
        # un-poison the entry -> it restores normally
        res = conductor.restore_trash(out["trash_id"], self.trash,
                                      self.runs, self.requests,
                                      self.manifests)
        self.assertEqual(res["restored"], rec["id"])
        # and a non-regex entry id never reaches the filesystem
        with self.assertRaises(conductor.RequestError) as e:
            conductor.restore_trash("../runs", self.trash, self.runs,
                                    self.requests, self.manifests)
        self.assertEqual(e.exception.status, 404)

    # ---- dashboard surface -------------------------------------------------
    def test_state_carries_trash_fields_and_the_view_list(self):
        rd = self._ended_run("20260101-000000-t")
        out = conductor.delete_run(rd.name, {"confirm": True}, self.runs,
                                   trash_dir=self.trash)
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        self.assertEqual(d["trash_count"], 1)
        self.assertEqual(d["trash_ids"], [out["trash_id"]])
        self.assertIsNone(d["trash"])
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests, include_trash=True)
        self.assertEqual([e["id"] for e in d["trash"]],
                         [out["trash_id"]])
        self.assertIn("expires_in_s", d["trash"][0])

    def test_dashboard_html_surfaces_trash_and_undo(self):
        html = conductor._DASH_HTML
        for marker in ('data-trash', 'data-undo-trash', 'data-restore-trash',
                       'data-empty-trash', 'id="undobar"', 'id="trashwrap"',
                       'Empty trash', 'Undo', 'trash_count'):
            self.assertIn(marker, html)

    def test_http_trash_routes(self):
        handler = conductor.make_dashboard_handler(
            self.runs, ["no-such-devin"], self.requests, self.manifests,
            trash_dir=self.trash)
        server = conductor.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port

        def call(method, path, data=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(method, path,
                         json.dumps(data).encode("utf-8")
                         if data is not None else None,
                         {"Content-Type": "application/json",
                          "Origin": f"http://127.0.0.1:{port}"})
            res = conn.getresponse()
            payload = json.loads(res.read().decode("utf-8"))
            conn.close()
            return res.status, payload

        rd = self._ended_run("20260101-000000-t")
        status, out = call("POST", f"/api/runs/{rd.name}/delete",
                           {"confirm": True})
        self.assertEqual(status, 200)
        trash_id = out["trash_id"]
        status, d = call("GET", "/api/state?trash=1")
        self.assertEqual([e["id"] for e in d["trash"]], [trash_id])
        self.assertEqual(d["trash_count"], 1)
        status, out = call("POST", f"/api/trash/{trash_id}/restore", {})
        self.assertEqual(status, 200)
        self.assertEqual(out["restored_as"], rd.name)
        status, out = call("POST", "/api/trash/empty", {})
        self.assertEqual(status, 400)
        status, out = call("POST", f"/api/trash/{trash_id}/restore", {})
        self.assertEqual(status, 404)
        call("POST", f"/api/runs/{rd.name}/delete", {"confirm": True})
        status, out = call("POST", "/api/trash/empty", {"confirm": True})
        self.assertEqual(status, 200)
        self.assertEqual(out["emptied"], 1)
        self.assertEqual(call("GET", "/api/state?trash=1")[1]["trash"], [])


class PurposeMarkerTests(unittest.TestCase):
    """A `<!-- conductor-purpose: X -->` comment at the HEAD of a
    conductor-written task body is machine metadata (RESOLVE_BODY leads
    with `resolve-merge`). It must reach the worker in the prompt file
    but never render as task text - surfaces show the real body, or a
    purpose label when no ordinary body exists."""

    MARK = "<!-- conductor-purpose: resolve-merge -->"

    def test_split_purpose_head_marker_and_whitespace(self):
        self.assertEqual(
            conductor.split_purpose(f"{self.MARK}\nreal task"),
            ("resolve-merge", "real task"))
        # surrounding whitespace in/around the marker is tolerated
        self.assertEqual(
            conductor.split_purpose(
                "  \n<!-- conductor-purpose :  resolve-merge  -->\n\n"
                "  body text"),
            ("resolve-merge", "body text"))
        self.assertEqual(
            conductor.split_purpose("<!--conductor-purpose:x-->y"),
            ("x", "y"))

    def test_split_purpose_leaves_mentions_and_other_comments(self):
        # mid-text the marker is a mention, not metadata (this very
        # step's prompt names it) - nothing is stripped
        p, body = conductor.split_purpose(
            f"fix the {self.MARK} rendering")
        self.assertIsNone(p)
        self.assertIn("conductor-purpose", body)
        p, body = conductor.split_purpose("<!-- a note --> do a thing")
        self.assertIsNone(p)
        self.assertEqual(body, "<!-- a note --> do a thing")
        self.assertEqual(conductor.split_purpose("plain task"),
                         (None, "plain task"))

    def test_task_display_body_or_label(self):
        self.assertEqual(
            conductor.task_display(f"{self.MARK}\nresolve the conflict"),
            "resolve the conflict")
        # marker-only resolver task -> a clear purpose label, not a blank
        self.assertEqual(
            conductor.task_display(self.MARK),
            "conductor task: merge conflict resolution")
        self.assertEqual(
            conductor.task_display("<!-- conductor-purpose: new-kind -->"),
            "conductor task: new-kind")
        self.assertEqual(conductor.task_display("do a thing"),
                         "do a thing")
        self.assertEqual(conductor.task_display(None), "")

    def test_task_summary_marker_does_not_eat_the_budget(self):
        body = self.MARK + "\n" + "word " * 60
        s = conductor.task_summary(body)
        self.assertNotIn("conductor-purpose", s)
        self.assertTrue(s.startswith("word"))
        self.assertEqual(len(s), 160)      # full budget of real text
        self.assertEqual(conductor.task_summary("plain " * 60)[:160],
                         " ".join(("plain " * 60).split())[:160])

    def test_task_label_translates_marker(self):
        rec = {"task": f"{self.MARK} do internal cleanup"}
        self.assertEqual(conductor.task_label(rec), "do internal cleanup")
        with tempfile.TemporaryDirectory() as td:
            pf = Path(td) / "p.md"
            pf.write_text("preamble\n\n## Task\n\n" + self.MARK +
                          "\nfull body\n\n## Before you finish\n- x\n",
                          encoding="utf-8")
            # prompt-file path (old rec without `task`) + full mode
            self.assertEqual(conductor.task_label({"prompt": str(pf)}),
                             "full body")
            self.assertEqual(
                conductor.task_label({"prompt": str(pf)}, full=True),
                "full body")

    def test_run_records_purpose_and_clean_task(self):
        """Dispatch path: `purpose` is the machine-readable field, `task`
        holds display text - while the prompt file still ships the raw
        marker to the worker."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "a"
project = "{proj.as_posix()}"
prompt = "<!-- conductor-purpose: housekeeping -->\\nFILE.txt - tidy it"
''')
            self.assertEqual(run_manifest(m, runs), 0)
            rd = next(runs.iterdir())
            st = json.loads((rd / "state.json").read_text())
            rec = st["steps"]["a"]
            self.assertEqual(rec["purpose"], "housekeeping")
            self.assertEqual(rec["task"], "FILE.txt - tidy it")
            # the marker is still IN the dispatched prompt (consumers
            # like the mock resolver key on it) - only displays strip it
            prompt_txt = (rd / "prompts" / "a.md").read_text(
                encoding="utf-8")
            self.assertIn("<!-- conductor-purpose: housekeeping -->",
                          prompt_txt)
            d = conductor.collect_dashboard(runs, ["no-such-devin"],
                                            base / "requests")
            a = d["agents"][0]
            self.assertEqual(a["purpose"], "housekeeping")
            self.assertEqual(a["task"], "FILE.txt - tidy it")
            self.assertEqual(a["task_full"], "FILE.txt - tidy it")


class StepSummaryTests(unittest.TestCase):
    """GET /api/runs/<run>/steps/<step>/summary renders a markdown
    report per step (status/elapsed/tokens/trajectory/commits/stdout
    tail) and persists <step>.summary.md once the step is not live."""

    def _run(self, td: str, step: dict, **st_extra):
        runs = Path(td) / "runs"
        rd = runs / "20261015-000000-sum"
        rd.mkdir(parents=True)
        st = {"run": "sum", "steps": {"s1": step}, **st_extra}
        (rd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        return runs, rd

    def _export(self, rd: Path) -> Path:
        exp = rd / "s1.export.json"
        exp.write_text(json.dumps({"steps": [
            {"step_id": 1, "timestamp": "2026-10-15T01:00:00",
             "metrics": {"prompt_tokens": 1000, "completion_tokens": 50,
                         "cached_tokens": 800}},
            {"step_id": 2, "timestamp": "2026-10-15T01:02:00",
             "metrics": {"prompt_tokens": 1500, "completion_tokens": 70,
                         "cached_tokens": 1200}}]}), encoding="utf-8")
        return exp

    def test_report_has_core_sections(self):
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run(td, {
                "status": "done", "started_at": "2026-10-15T00:00:00",
                "ended_at": "2026-10-15T00:33:52", "elapsed_s": 2032.4,
                "model": "swe-2", "effort": "max", "mode": "bypass",
                "session_id": "sess-1", "verify_ok": True,
                "exit_code": 0, "task": "do the thing",
                "tokens": {"prompt": 1000, "completion": 50,
                           "cached": 800, "llm_steps": 2}})
            md = conductor.step_summary(rd.name, "s1", runs)
            self.assertIn("# 20261015-000000-sum / s1", md)
            self.assertIn("**status:** done", md)
            self.assertIn("**elapsed:** 33m52s", md)
            self.assertIn("sess-1", md)
            self.assertIn("billed in 200", md)   # prompt - cached
            self.assertIn("do the thing", md)
            # persisted once the step is not live
            self.assertEqual((rd / "s1.summary.md").read_text(
                encoding="utf-8"), md)

    def test_trajectory_and_tail_rendered(self):
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run(td, {
                "status": "done", "elapsed_s": 60,
                "tokens": {"prompt": 5, "completion": 1,
                           "cached": 0, "llm_steps": 2}})
            exp = self._export(rd)
            st = json.loads((rd / "state.json").read_text())
            st["steps"]["s1"]["export"] = str(exp)
            log = rd / "s1.stdout.log"
            log.write_text("line1\nline2\nSTATUS: done\n")
            st["steps"]["s1"]["stdout_log"] = str(log)
            (rd / "state.json").write_text(json.dumps(st))
            md = conductor.step_summary(rd.name, "s1", runs)
            self.assertIn("## Trajectory (2 turns", md)
            self.assertIn("## stdout tail", md)
            self.assertIn("STATUS: done", md)

    def test_live_step_serves_but_never_writes(self):
        """trap 5: a file write into data/runs mid-step can kill a
        self-hosted worker - live steps render fresh, persist nothing."""
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run(td, {
                "status": "running",
                "started_at": conductor.now_iso(), "timeout_min": 60})
            md = conductor.step_summary(rd.name, "s1", runs)
            self.assertIn("**status:** running", md)
            self.assertFalse((rd / "s1.summary.md").exists())

    def test_missing_step_and_bad_names_404(self):
        with tempfile.TemporaryDirectory() as td:
            runs, _ = self._run(td, {"status": "done"})
            with self.assertRaises(conductor.RequestError):
                conductor.step_summary("20261015-000000-sum", "nope", runs)
            with self.assertRaises(conductor.RequestError):
                conductor.step_summary("../escape", "s1", runs)
            with self.assertRaises(conductor.RequestError):
                conductor.step_summary("20261015-000000-sum",
                                       "s1/../../x", runs)

    def test_summary_link_in_modal_js(self):
        self.assertIn("/summary", conductor._DASH_HTML)
        self.assertIn("Session summary", conductor._DASH_HTML)

    def test_summary_html_escapes_agent_text(self):
        md = ("# run / s1\n\n- **status:** done\n\n## Task\n\n"
              "inject <script>alert(1)</script> **here**\n\n"
              "## stdout tail\n\n```\n<tag> raw\n```\n")
        page = conductor.summary_html(md, "t")
        self.assertIn("<h1>run / s1</h1>", page)
        self.assertIn("<h2>Task</h2>", page)
        self.assertIn("<strong>here</strong>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;tag&gt; raw", page)
        self.assertNotIn("<script>", page)

    MARK = "<!-- conductor-purpose: resolve-merge -->"

    def test_summary_reported_marker_becomes_purpose_not_text(self):
        """The reported shape (resolve-task-modal-navigation): rec['task']
        led with the raw `<!-- conductor-purpose -->` comment, which
        rendered literally in ## Task and ate a third of the 160-char
        budget. Now the token lands on a **purpose:** bullet and Task
        shows the real body."""
        task = (self.MARK + " A git merge is in progress in this repo "
                "and needs a human-style resolution. Conductor tried to "
                "merge branch `conductor")
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run(td, {"status": "done", "task": task})
            md = conductor.step_summary(rd.name, "s1", runs)
            self.assertIn("- **purpose:** `resolve-merge` "
                          "(merge conflict resolution)", md)
            self.assertIn("## Task\n\nA git merge is in progress "
                          "in this repo", md)
            self.assertNotIn("conductor-purpose", md)
            self.assertNotIn("<!--", md)
            page = conductor.summary_html(md, "t")
            self.assertIn("A git merge is in progress", page)
            self.assertNotIn("conductor-purpose", page)
            # API behavior unchanged: the report still persists verbatim
            # once the step is no longer live
            self.assertEqual((rd / "s1.summary.md").read_text(
                encoding="utf-8"), md)

    def test_summary_purpose_field_without_marker_in_task(self):
        """New records store `purpose` explicitly and `task` already
        marker-free - the bullet renders from the field alone."""
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run(td, {
                "status": "done", "purpose": "resolve-merge",
                "task": "A git merge is in progress"})
            md = conductor.step_summary(rd.name, "s1", runs)
            self.assertIn("- **purpose:** `resolve-merge` "
                          "(merge conflict resolution)", md)
            self.assertIn("## Task\n\nA git merge is in progress", md)

    def test_summary_marker_only_task_gets_purpose_label(self):
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run(td, {"status": "done", "task": self.MARK})
            md = conductor.step_summary(rd.name, "s1", runs)
            self.assertIn("- **purpose:** `resolve-merge`", md)
            self.assertIn("## Task\n\nconductor task: "
                          "merge conflict resolution", md)
            self.assertNotIn("<!--", md)

    def test_summary_keeps_literal_comments_and_mentions(self):
        """Only a head-of-body conductor-purpose marker is metadata: an
        HTML comment inside task text (a code example) or a mid-sentence
        marker mention stays - escaped, but never stripped."""
        task = ("keep `<!-- html -->` comments in code samples; also fix "
                f"the {self.MARK} marker doc")
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run(td, {"status": "done", "task": task})
            md = conductor.step_summary(rd.name, "s1", runs)
            self.assertNotIn("**purpose:**", md)   # mention != metadata
            self.assertIn("`<!-- html -->`", md)          # kept verbatim
            self.assertIn("conductor-purpose", md)         # mention kept
            page = conductor.summary_html(md, "t")
            self.assertIn("&lt;!-- html --&gt;", page)     # escaped, safe
            self.assertNotIn("<!--", page)                 # never raw

    def test_summary_marker_plus_injection_stays_safe(self):
        task = self.MARK + " resolve it <script>alert(1)</script> now"
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run(td, {"status": "done", "task": task})
            page = conductor.summary_html(
                conductor.step_summary(rd.name, "s1", runs), "t")
            self.assertNotIn("conductor-purpose", page)
            self.assertIn("&lt;script&gt;", page)
            self.assertNotIn("<script>", page)

    def test_modal_purpose_row_wired(self):
        # the marker's token reaches the modal via the 'purpose' field
        self.assertIn("a.purpose", conductor._DASH_HTML)


class RunSummaryTests(unittest.TestCase):
    """The worker's own closing report - the trailing agent prose in the
    ATIF export, minus the STATUS:/NEXT: contract lines - leads the step
    summary, and the run-level view rolls them up (deterministically, or
    an LLM-written run.summary.md when a summarizer session ran)."""

    def _closing_export(self, rd: Path, msg: str,
                        tail_toolcall: bool = False) -> Path:
        exp = rd / "s1.export.json"
        steps = [{"step_id": 1, "source": "agent",
                  "tool_calls": [{"function_name": "exec"}],
                  "message": "working on it"}]
        if tail_toolcall:
            steps.append({"step_id": 2, "source": "agent",
                          "tool_calls": [{"function_name": "exec"}],
                          "message": ""})
        else:
            steps.append({"step_id": 2, "source": "agent",
                          "tool_calls": [], "message": msg})
        exp.write_text(json.dumps({"steps": steps}), encoding="utf-8")
        return exp

    def _run_state(self, td: str, steps: dict) -> tuple[Path, Path]:
        runs = Path(td) / "runs"
        rd = runs / "20261015-000000-sum"
        rd.mkdir(parents=True)
        st = {"run": "sum", "started_at": "2026-10-15T00:00:00",
              "ended_at": "2026-10-15T01:00:00", "steps": steps}
        (rd / "state.json").write_text(json.dumps(st), encoding="utf-8")
        return runs, rd

    def test_final_report_strips_contract_lines(self):
        with tempfile.TemporaryDirectory() as td:
            exp = self._closing_export(
                Path(td), "Did the work.\n\nSTATUS: done\nNEXT: nothing")
            self.assertEqual(conductor.final_report(exp), "Did the work.")

    def test_final_report_no_closing_prose_is_none(self):
        """A turn that ends on a tool call (crash mid-work) or never
        wrote an export has no closing report - not an empty string, so
        callers fall back explicitly."""
        with tempfile.TemporaryDirectory() as td:
            exp = self._closing_export(Path(td), "x", tail_toolcall=True)
            self.assertIsNone(conductor.final_report(exp))
            self.assertIsNone(
                conductor.final_report(Path(td) / "nope.json"))
            bad = Path(td) / "bad.export.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertIsNone(conductor.final_report(bad))

    def test_step_summary_leads_with_report(self):
        with tempfile.TemporaryDirectory() as td:
            exp = self._closing_export(
                Path(td), "## What happened\nAll landed; two bugs fixed."
                          "\n\nSTATUS: done\nNEXT: polish")
            runs, rd = self._run_state(td, {
                "s1": {"status": "done", "task": "do the thing",
                       "export": str(exp)}})
            md = conductor.step_summary(rd.name, "s1", runs)
            self.assertIn("## Report\n\n## What happened\nAll landed; "
                          "two bugs fixed.", md)
            # contract lines are lifted out of the prose - structured
            # fields carry them
            self.assertNotIn("All landed; two bugs fixed.\n\nSTATUS:",
                             md)

    def test_run_report_md_rolls_up(self):
        with tempfile.TemporaryDirectory() as td:
            exp = self._closing_export(Path(td), "did the work")
            runs, rd = self._run_state(td, {
                "s1": {"status": "done", "elapsed_s": 60,
                       "export": str(exp), "merged": True,
                       "tokens": {"prompt": 10, "completion": 5,
                                  "cached": 0}},
                "s2": {"status": "failed",
                       "note": "exited 0 without a STATUS: line"}})
            md = conductor.run_summary_doc("20261015-000000-sum", runs)
            self.assertIn("# sum - run summary", md)
            self.assertIn("## s1 - done", md)
            self.assertIn("did the work", md)
            self.assertIn("merged", md)
            self.assertIn("## s2 - failed", md)
            self.assertIn("ended without a closing report", md)
            # an LLM-written run.summary.md takes precedence on disk
            (rd / "run.summary.md").write_text("# llm roll-up\n",
                                               encoding="utf-8")
            self.assertEqual(
                conductor.run_summary_doc("20261015-000000-sum", runs),
                "# llm roll-up\n")

    def test_summary_prompt_carries_reports(self):
        with tempfile.TemporaryDirectory() as td:
            rd = Path(td)
            exp = self._closing_export(rd, "did the work")
            st = {"run": "sum", "description": "ship it",
                  "steps": {"s1": {"status": "done", "task": "task one",
                                   "export": str(exp)},
                            "s2": {"status": "failed",
                                   "task": "task two"}}}
            prompt = conductor._summary_prompt(st)
            self.assertIn("conductor-run-summary", prompt)
            self.assertIn("task one", prompt)
            self.assertIn("did the work", prompt)
            self.assertIn("task two", prompt)
            self.assertIn("ended without one", prompt)

    def test_summarize_run_writes_llm_summary(self):
        """The end-of-run summarizer is a fresh devin session (here the
        mock): it answers the conductor-run-summary prompt, its closing
        message lands as run.summary.md, and the event + state fields
        record it."""
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
            rd = next(runs.iterdir())
            self.assertTrue(conductor.summarize_run(
                rd, shlex.split(MOCK)))
            out = (rd / "run.summary.md").read_text(encoding="utf-8")
            self.assertIn("Executive summary (mock)", out)
            st = json.loads((rd / "state.json").read_text())
            self.assertIn("run_summary", st)
            self.assertTrue(st["run_summary"]["session"]
                            .startswith("mock-"))
            ev = (rd / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"run_summarized"', ev)
            # marker is cleaned up; the endpoint now serves the file
            self.assertFalse((rd / ".summarizing").exists())
            self.assertIn("Executive summary (mock)",
                          conductor.run_summary_doc(rd.name, runs))

    def test_summarize_manifest_autospawns(self):
        """[run] summarize = true fires the detached `conductor.py
        summarize` subprocess at run end - no dashboard needed."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "first"
''', extra="summarize = true")
            self.assertEqual(run_manifest(m, runs), 0)
            rd = next(runs.iterdir())
            st = json.loads((rd / "state.json").read_text())
            self.assertEqual(st["summarize_model"], "")
            for _ in range(450):
                if (rd / "run.summary.md").is_file():
                    break
                time.sleep(0.1)
            self.assertTrue((rd / "run.summary.md").is_file(),
                            "summarizer subprocess did not produce "
                            "run.summary.md")
            self.assertIn("Executive summary (mock)",
                          (rd / "run.summary.md").read_text(
                              encoding="utf-8"))
            self.assertFalse((rd / ".summarizing").exists())

    def test_summarize_endpoint_guards(self):
        with tempfile.TemporaryDirectory() as td:
            runs, rd = self._run_state(td, {})
            with self.assertRaises(conductor.RequestError):
                conductor.summarize_run_start(rd.name, runs)
            # a live step blocks the launch (trap 5: writes into a run
            # dir a self-hosted worker may be watching) - fresh no-pid
            # 'running' is a live shape in its own right
            runs, rd = self._run_state(Path(td) / "t2", {
                "s1": {"status": "running",
                       "started_at": conductor.now_iso(),
                       "timeout_min": 60}})
            with self.assertRaises(conductor.RequestError):
                conductor.summarize_run_start(rd.name, runs)

    def test_summary_renderer_widened(self):
        """The report body handles the markdown shapes agent prose
        actually uses: ###/#### headings, numbered lists, ---, italics -
        still escaped, still a narrow subset."""
        md = ("# t\n\n### sub head\n\n1. one\n2. two\n\n---\n\n"
              "*em* and **bold** and <x>\n")
        frag = conductor._summary_body(md)
        self.assertIn("<h3>sub head</h3>", frag)
        self.assertIn("<ol><li>one</li><li>two</li></ol>", frag)
        self.assertIn("<hr>", frag)
        self.assertIn("<em>em</em>", frag)
        self.assertIn("<strong>bold</strong>", frag)
        self.assertIn("&lt;x&gt;", frag)

    def test_run_summary_buttons_wired(self):
        self.assertIn('data-run-summary="', conductor._DASH_HTML)
        self.assertIn('data-summarize-run="', conductor._DASH_HTML)
        self.assertIn("Run summary", conductor._DASH_HTML)
        self.assertIn("Generate LLM summary", conductor._DASH_HTML)
        self.assertIn("/summary?frag=1", conductor._DASH_HTML)


class ModalDetailNavTests(unittest.TestCase):
    """The 'Session summary' report opens in-app as a modal view (fetching
    the ?frag=1 body) instead of a separate browser window, with the same
    pipeSibs prev/next ordering as the task modal, and modal-to-modal jumps
    (task -> request/runbook, request -> run details) leave a Back trail."""

    def test_summary_frag_endpoint(self):
        """?frag=1 returns just the escaped report body for the modal;
        ?raw=1 (markdown) and the default full-page render are unchanged,
        and a missing step still 404s as JSON."""
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            rd = runs / "20261015-000000-sum"
            rd.mkdir(parents=True)
            (rd / "state.json").write_text(json.dumps({
                "run": "sum", "steps": {"s1": {
                    "status": "done", "task": "inject <b>x</b> safely",
                    "elapsed_s": 5}}}), encoding="utf-8")
            handler = conductor.make_dashboard_handler(
                runs, ["no-such-devin"], Path(td) / "reqs",
                Path(td) / "manifests")
            server = conductor.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            port = server.server_port

            def get(path):
                conn = http.client.HTTPConnection("127.0.0.1", port,
                                                  timeout=5)
                conn.request("GET", path, headers={
                    "Origin": f"http://127.0.0.1:{port}"})
                res = conn.getresponse()
                data = res.read().decode("utf-8")
                ctype = res.getheader("Content-Type")
                conn.close()
                return res.status, data, ctype

            base = "/api/runs/20261015-000000-sum/steps/s1/summary"
            status, frag, ctype = get(base + "?frag=1")
            self.assertEqual(status, 200)
            self.assertIn("text/html", ctype)
            self.assertIn("<h1>20261015-000000-sum / s1</h1>", frag)
            self.assertIn("&lt;b&gt;x&lt;/b&gt;", frag)   # still escaped
            self.assertNotIn("<!doctype", frag.lower())   # body only
            self.assertNotIn("<style>", frag)
            status, page, _ = get(base)
            self.assertEqual(status, 200)
            self.assertIn("<!doctype html>", page)        # full page intact
            self.assertIn(frag.strip().split("\n")[0], page)
            status, raw, ctype = get(base + "?raw=1")
            self.assertEqual(status, 200)
            self.assertIn("text/markdown", ctype)
            self.assertIn("# 20261015-000000-sum / s1", raw)
            status, body, _ = get(
                "/api/runs/20261015-000000-sum/steps/nope/summary?frag=1")
            self.assertEqual(status, 404)
            self.assertIn("error", json.loads(body))

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_summary_modal_and_modal_back_js(self):
        """Summary opens as an in-app view: same .dnav chain as the task
        modal (sibling names + 'task N of M' + disabled ends), Arrow keys
        walk it, a 404 renders a notice with Back intact, and Back pops the
        modal history to the exact originating view (task/run/request) with
        its scroll offset - Escape still tears the whole dialog down."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,
  {innerHTML:'',hidden:true,textContent:'',scrollTop:0});
  return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const fetches=[];
const fetch=async(url)=>{
  fetches.push(url);
  if (url.includes('/summary')) {
    const step=decodeURIComponent(url.match(/steps\/([^/?]+)/)[1]);
    if (step==='x1') return {ok:false,status:404,
      json:async()=>({error:'Step not in this run'}),text:async()=>''};
    return {ok:true,status:200,
      text:async()=>'<h1>r / '+step+'</h1><ul><li><strong>status:</strong> done</li></ul>',
      json:async()=>({})};
  }
  if (url.startsWith('/api/requests/')) return {ok:true,status:200,
    json:async()=>({id:'req1',status:'launched',raw:'do it',project:'p',
      proposed_steps:[],manifest:'m.toml',manifest_text:'x',
      max_attempts:3})};
  throw new Error('no net '+url);
};
const ctx=vm.createContext({document,fetch,confirm:()=>true,alert:()=>{},
  location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
// run 'r' is a launched request (key request:req1) with a 3-step pipeline;
// run 'r2' is a foreign single-step run whose step the server 404s.
const data={serve_boot:'b',generated_at:'2026-10-14T00:00:00',agents:[
  {step:'s1',run:'r',run_dir:'/r',run_key:'request:req1',slug:'p',
   status:'done',task:'t1',seq:0,request_id:'req1'},
  {step:'s3',run:'r',run_dir:'/r',run_key:'request:req1',slug:'p',
   status:'done',task:'t3',seq:2,request_id:'req1'},
  {step:'s2',run:'r',run_dir:'/r',run_key:'request:req1',slug:'p',
   status:'done',task:'t2',seq:1,request_id:'req1'},
  {step:'x1',run:'r2',run_dir:'/r2',run_key:'/r2',slug:'q',
   status:'done',task:'tx',seq:0}],
  runs:[{run:'r',dir:'/r',key:'request:req1',request_id:'req1',
         counts:{done:3}},
        {run:'r2',dir:'/r2',key:'/r2',counts:{done:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const click=async t=>handlers.click({target:t,preventDefault:()=>{}});
const key=async k=>handlers.keydown({key:k,target:{},preventDefault:()=>{}});
const open=step=>vm.runInContext(
  'showDetail(AGENTS.findIndex(a=>a.step==='+JSON.stringify(step)+'))',ctx);
const el=(sel,ds)=>({dataset:{},closest:s=>s===sel?{dataset:ds}:null});
const flush=()=>new Promise(r=>setTimeout(r,0));
const body=()=>elem('dbody').innerHTML;
(async()=>{
  // task modal -> in-app summary (the link keeps its real URL + _blank)
  await open('s2');
  assert.match(body(),/data-summary="request:req1\|s2"/);
  assert.match(body(),/href="\/api\/runs\/r\/steps\/s2\/summary"/);
  assert.match(body(),/target="_blank"/);
  elem('detail').scrollTop=42;                 // task modal was scrolled
  await click(el('[data-summary]',{summary:'request:req1|s2'}));
  await flush();
  assert.equal(elem('backdrop').hidden,false); // same modal, no new window
  assert.match(body(),/Session summary - p \/ s2/);
  assert.match(body(),/<div class="sumdoc"><h1>r \/ s2<\/h1>/);
  assert.match(body(),/task 2 of 3/);          // same ordering as task nav
  assert.match(body(),/data-snav="request:req1\|s1"/);
  assert.match(body(),/data-snav="request:req1\|s3"/);
  assert.match(body(),/data-modal-back/);
  assert.ok(fetches.some(u=>u==='/api/runs/r/steps/s2/summary?frag=1'));
  assert.equal(elem('detail').scrollTop,0);    // fresh view lands on top
  // sibling nav to the chain end - the next button disables there
  await click(el('[data-snav]',{snav:'request:req1|s3'})); await flush();
  assert.match(body(),/r \/ s3/);
  assert.match(body(),/task 3 of 3/);
  // arrow keys share the walk; Right clamps at the end of the list
  await key('ArrowLeft'); await flush();
  assert.match(body(),/r \/ s2/); assert.match(body(),/task 2 of 3/);
  await key('ArrowRight'); await flush(); assert.match(body(),/task 3 of 3/);
  await key('ArrowRight'); await flush(); assert.match(body(),/task 3 of 3/);
  // Back pops to the ORIGINATING task - same key, same scroll offset
  await click(el('[data-modal-back]',{})); await flush();
  assert.match(body(),/p \/ s2/);
  assert.match(body(),/<div class="kv">/);
  assert.equal(elem('detail').scrollTop,42);
  assert.doesNotMatch(body(),/data-modal-back/);   // history consumed

  // task -> request/runbook -> run details, Back walking each level
  await open('s1');
  await click(el('[data-open-request]',{openRequest:'req1'})); await flush();
  assert.match(body(),/Original request/);
  assert.match(body(),/Back: s1/);
  await click(el('[data-run-details]',{runDetails:'request:req1'}));
  assert.match(body(),/<h3 id="modal-title">r<\/h3>/);
  assert.match(body(),/Back: request/);
  await click(el('[data-modal-back]',{})); await flush();
  assert.match(body(),/Original request/);
  assert.match(body(),/Back: s1/);               // older entry survived
  await click(el('[data-modal-back]',{})); await flush();
  assert.match(body(),/p \/ s1/);
  assert.doesNotMatch(body(),/data-modal-back/);

  // missing item: the server 404s -> notice, no dead nav, Back survives
  await open('x1');
  await click(el('[data-summary]',{summary:'/r2|x1'})); await flush();
  assert.match(body(),/Summary unavailable: Step not in this run/);
  assert.match(body(),/class="notice error"/);
  assert.doesNotMatch(body(),/data-snav/);       // single-step run: no nav
  assert.match(body(),/data-modal-back/);
  await click(el('[data-modal-back]',{})); await flush();
  assert.match(body(),/q \/ x1/);

  // Escape tears the whole stack down; nothing leaks into the next modal
  await open('s2');
  await click(el('[data-open-request]',{openRequest:'req1'})); await flush();
  await key('Escape');
  assert.equal(elem('backdrop').hidden,true);
  await open('s1');
  assert.doesNotMatch(body(),/data-modal-back/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class ModalScrollStabilityTests(unittest.TestCase):
    """An open task modal must survive the 2s background repaint: same
    scroll position, same focus, no DOM churn on identical paint, no
    inherited position on deliberate task switches, and stale /api/state
    responses never repaint an older view."""

    _STUB = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
let landmarks=[], focusables=[], activeEl=null, pending=null;
let dbodyWrites=0, backdropHides=0, dtailSets=0;
// Fake geometry, honestly modeled: a landmark carries a content-y
// coordinate (cy) + height (h); its viewport rect derives from the live
// scrollTop. `pending` says what the NEXT dbody.innerHTML swap does to
// layout (a fresh landmark set) and to scrollTop (a browser clamp).
const detailEl={innerHTML:'',hidden:false,textContent:'',scrollTop:0,
  getBoundingClientRect:()=>({top:0,bottom:654,left:0,right:900}),
  contains:()=>true};
const backdrop={_hidden:true,innerHTML:'',textContent:'',scrollTop:0,
  set hidden(v){ this._hidden=v; if(v===true) backdropHides++; },
  get hidden(){ return this._hidden; }};
const dbody={_h:'',textContent:'',hidden:false,scrollTop:0,
  set innerHTML(v){ this._h=v; dbodyWrites++;
    if(pending){ if(pending.landmarks) landmarks=pending.landmarks;
      if(pending.clamp!=null) detailEl.scrollTop=pending.clamp;
      pending=null; } },
  get innerHTML(){ return this._h; },
  querySelectorAll:()=>landmarks};
const dtail={hidden:false,innerHTML:'',textContent:'',_s:0,
  set scrollTop(v){ this._s=v; dtailSets++; },
  get scrollTop(){ return this._s; }};
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,
  textContent:'',scrollTop:0}); return nodes.get(id); };
nodes.set('detail',detailEl); nodes.set('backdrop',backdrop);
nodes.set('dbody',dbody); nodes.set('dtail',dtail);
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:sel=>sel.startsWith('#detail')?focusables:[],
  get activeElement(){ return activeEl; },
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const lm=o=>Object.assign({id:'',dataset:{},tagName:'DIV',className:'',
  textContent:'',cy:0,h:20,
  classList:{contains:c=>(o.cls||'').split(' ').includes(c)},
  getBoundingClientRect(){ const t=this.cy-detailEl.scrollTop;
    return {top:t,bottom:t+this.h,left:0,right:0}; }},o);
let queue=[],defers=null,fetches=[];
const fetch=u=>{ fetches.push(u);
  if(defers) return new Promise(r=>defers.push({u,r}));
  const r=queue.shift()||{ok:true,json:async()=>({}),text:async()=>''};
  return Promise.resolve(r); };
const ctx=vm.createContext({document,fetch,confirm:()=>true,alert:()=>{},
  location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'g0',agents:[
  {step:'one',run:'rA',run_dir:'/rA',run_key:'/rA',slug:'p',
   status:'failed',task:'t1',task_full:'tf1',seq:0,note:'n1',
   timeout_min:60},
  {step:'two',run:'rA',run_dir:'/rA',run_key:'/rA',slug:'p',
   status:'done',task:'t2',seq:1}],
  runs:[{run:'rA',dir:'/rA',key:'/rA',counts:{failed:1,done:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const click=async t=>handlers.click({target:t,preventDefault:()=>{}});
const el=(sel,ds)=>({dataset:{},closest:s=>s===sel?{dataset:ds}:null});
const open=key=>click(el('[data-agent-detail]',{agentDetail:key}));
const tailView=()=>{
  landmarks=[lm({cls:'k',textContent:'note',cy:200,h:500}),
             lm({tagName:'H3',textContent:'stdout tail',cy:1050,h:24}),
             lm({id:'dtail',cy:1074,h:180})];
  detailEl.scrollTop=1050; };
'''

    def _node(self, body):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        p = subprocess.run(["node", "-e", self._STUB + body], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_same_task_repaint_keeps_scroll_anchor(self):
        """A scrolled modal keeps its place through same-task repaints:
        content growth above the viewport shifts scrollTop so the same
        landmark stays put, a mid-swap browser clamp is un-clamped back
        to the anchor, the #dtail pre's own scroll offset survives, the
        dialog never hides mid-repaint, and a byte-identical repaint
        skips the DOM write entirely."""
        self._node(r'''
(async()=>{
  await open('/rA|one');
  assert.equal(backdrop.hidden,false);
  assert.match(dbody.innerHTML,/p \/ one/);
  tailView();                                   // scrolled: tail at top
  dtail._s=37; dtailSets=0;
  // same-task update where content above the viewport grew 500px: the
  // browser keeps numeric scrollTop, so without anchoring the tail the
  // user was reading slides off-screen
  const d2=JSON.parse(JSON.stringify(data));
  d2.agents[0].note='n'.repeat(40);
  pending={landmarks:[
    lm({cls:'k',textContent:'note',cy:200,h:1000}),
    lm({tagName:'H3',textContent:'stdout tail',cy:1550,h:24}),
    lm({id:'dtail',cy:1574,h:180})]};
  const h0=backdropHides;
  vm.runInContext('render('+JSON.stringify(d2)+')',ctx);
  assert.equal(backdropHides,h0);            // no hide/show round-trip
  assert.equal(detailEl.scrollTop,1550);     // anchor followed growth
  assert.ok(dtailSets>=1);                   // inner <pre> scroll carried
  assert.equal(dtail._s,37);
  // shrink back to the original note - a browser clamp mid-swap
  // (scrollTop 1550 -> 802) must NOT be permanent: the anchor restores
  const d3=JSON.parse(JSON.stringify(data));
  pending={landmarks:[
    lm({cls:'k',textContent:'note',cy:200,h:500}),
    lm({tagName:'H3',textContent:'stdout tail',cy:1050,h:24}),
    lm({id:'dtail',cy:1074,h:180})],
    clamp:802};
  vm.runInContext('render('+JSON.stringify(d3)+')',ctx);
  assert.equal(detailEl.scrollTop,1050);     // un-clamped to the anchor
  // a repaint that produces identical markup leaves the DOM alone
  const d4=JSON.parse(JSON.stringify(d3));
  d4.agents[1].note='sibling changed';       // sig bust, modal unchanged
  const w0=dbodyWrites;
  vm.runInContext('render('+JSON.stringify(d4)+')',ctx);
  assert.equal(dbodyWrites,w0);
  // a focused control is re-found on the new DOM by its stable data-*
  // signature and re-focused (preventScroll, so the anchor isn't fought)
  const btn={id:'',tagName:'BUTTON',dataset:{saveNotes:'/rA'},focusN:0,
    focus(){this.focusN++;},getAttribute:()=>null};
  focusables=[btn]; activeEl=btn;
  const d5=JSON.parse(JSON.stringify(d4));
  d5.agents[0].note='n'.repeat(41);
  pending={landmarks:[
    lm({cls:'k',textContent:'note',cy:200,h:510}),
    lm({tagName:'H3',textContent:'stdout tail',cy:1060,h:24}),
    lm({id:'dtail',cy:1084,h:180})]};
  vm.runInContext('render('+JSON.stringify(d5)+')',ctx);
  assert.equal(btn.focusN,1);
  assert.equal(detailEl.scrollTop,1060);     // focus didn't win the scroll
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_task_switch_is_a_deliberate_top_reset(self):
        """Navigating to a different task always lands at the top - the
        previous task's offset can never bleed in (a display:none
        round-trip does not reset scrollTop in modern browsers). A
        same-task repaint or re-click is NOT a switch and keeps its
        place; Escape + reopen also starts at the top."""
        self._node(r'''
(async()=>{
  await open('/rA|one');
  detailEl.scrollTop=600;
  await open('/rA|two');
  assert.match(dbody.innerHTML,/p \/ two/);
  assert.equal(detailEl.scrollTop,0);
  detailEl.scrollTop=450;
  await open('/rA|two');                       // same task again: repaint
  assert.equal(detailEl.scrollTop,450);
  await open('/rA|one');
  detailEl.scrollTop=450;
  await click(el('[data-nav]',{nav:'/rA|two'})); // sibling nav -> top
  assert.match(dbody.innerHTML,/p \/ two/);
  assert.equal(detailEl.scrollTop,0);
  handlers.keydown({key:'Escape',
    target:{closest:()=>null,matches:()=>false},preventDefault:()=>{}});
  assert.equal(backdrop.hidden,true);
  detailEl.scrollTop=500;                      // stale leftover offset
  await open('/rA|one');
  assert.equal(detailEl.scrollTop,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_modal_action_and_stale_refresh_guard(self):
        """A POST+refresh action inside the modal (Save notes - same
        round trip as review actions take) repaints the same task without
        moving it. And /api/state responses are sequenced: an older body
        landing after a newer one is dropped, so a stale payload can
        never resurrect an older view mid-repaint."""
        self._node(r'''
(async()=>{
  await open('/rA|one');
  tailView();
  // a modal action's POST + refresh round trip keeps the same view
  queue.push({ok:true,json:async()=>({})});
  const d6=JSON.parse(JSON.stringify(data));
  d6.agents[0].note='note saved';
  queue.push({ok:true,json:async()=>d6});
  await click({dataset:{saveNotes:'/rA',step:'one'},closest:()=>null,
               disabled:false});
  assert.ok(fetches.some(u=>u.includes('/steps/one/notes')));
  assert.equal(backdrop.hidden,false);
  assert.equal(detailEl.scrollTop,1050);
  assert.match(dbody.innerHTML,/note saved/);
  // out-of-order /api/state: the newer response lands first, the older
  // one last - the older body must not repaint on top
  const dOld=JSON.parse(JSON.stringify(data)); dOld.generated_at='STALE';
  const dNew=JSON.parse(JSON.stringify(data)); dNew.generated_at='NEW';
  defers=[];
  const p1=vm.runInContext('refresh()',ctx);
  const p2=vm.runInContext('refresh()',ctx);
  defers[1].r({ok:true,json:async()=>dNew});   // newer lands first
  await p2;
  defers[0].r({ok:true,json:async()=>dOld});   // stale lands last
  await p1;
  assert.equal(vm.runInContext('lastData.generated_at',ctx),'NEW');
  assert.equal(backdrop.hidden,false);         // modal still up, same task
  assert.equal(detailEl.scrollTop,1050);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


class ModelComboboxTests(unittest.TestCase):
    """Planner/Step model fields are free-text comboboxes: typing filters a
    Recommendations group on top of a separated, scrollable All models
    group - both fed by modelCatalog() (the same `devin models list`
    parse). The full list stays put on zero matches, and a model present
    in both groups is two distinct options so aria-activedescendant is
    never ambiguous."""

    def test_model_fields_render_as_comboboxes(self):
        html = conductor._DASH_HTML
        self.assertIn('role="combobox"', html)
        self.assertIn('aria-autocomplete="list"', html)
        self.assertIn('role="listbox"', html)
        self.assertIn('aria-activedescendant', html)
        # the shared datalist is gone; effort fields keep theirs
        self.assertNotIn('datalist id="rq-models"', html)
        self.assertIn('datalist id="rq-efforts"', html)
        self.assertIn('datalist id="rq-plan_efforts"', html)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_combobox_filter_sections_keyboard_and_persistence(self):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const handlers={}, nodes=new Map(), posts=[];
const mkCls=init=>{const s=new Set((init||'').split(' ').filter(Boolean));
  return {contains:c=>s.has(c),add:c=>s.add(c),remove:c=>s.delete(c),
    toggle:(c,f)=>{const on=f===undefined?!s.has(c):f;
      on?s.add(c):s.delete(c);return on;}};};
const elem=id=>{
  if(!nodes.has(id)){
    const n={id,innerHTML:'',hidden:true,textContent:'',value:'',style:{},
      attrs:{},dataset:{},disabled:false,scrollTop:0,classList:mkCls(''),
      setAttribute(k,v){this.attrs[k]=String(v);},
      getAttribute(k){return this.attrs[k];},
      removeAttribute(k){delete this.attrs[k];},
      querySelector:()=>null,querySelectorAll:()=>[],closest:()=>null,
      focus(){this.focused=(this.focused||0)+1;},
      scrollIntoView(){this.scrolled=(this.scrolled||0)+1;},
      dispatchEvent(ev){const h=handlers[ev&&ev.type];
        if(h)h({target:n});return true;},
      getBoundingClientRect:()=>({left:0,top:0,bottom:0,right:0})};
    nodes.set(id,n);}
  return nodes.get(id);};
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const sel={isCollapsed:true,toString:()=>''};
const window={innerWidth:1200,innerHeight:800,addEventListener:()=>{},
  getSelection:()=>sel};
class Event{constructor(type,init){this.type=type;Object.assign(this,init);}}
const CATALOG={'swe-2':['high','medium','max'],
  'gpt-6-luna':['min','low','medium','high','max'],
  'gpt-6-astra':['low','high'],'claude-opus-5.5':['low','high','max']};
const data={serve_boot:'b',generated_at:'2026-10-16T00:00:00',
  model_options:CATALOG,agents:[],runs:[],defaults:{model:'swe-2',effort:'max'}};
const rec={id:'r1',status:'ready',label:'x',raw:'do it',project:'p',
  model:'swe-2',effort:'max',plan_model:'gpt-6-luna',plan_effort:'high',
  manifest:'manifests/r1.toml',manifest_text:'[run]',stale:false};
const fetch=async(url,opt)=>{
  if(opt&&opt.body)posts.push({url,body:JSON.parse(opt.body)});
  return {ok:true,json:async()=>(url.startsWith('/api/state')?data:{})};};
const ctx=vm.createContext({document,window,fetch,Event,
  confirm:()=>true,alert:()=>{},location:{reload:()=>{}},
  setTimeout:()=>{}});
vm.runInContext(src,ctx);
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
vm.runInContext('modalRequest={id:"r1",status:"ready",dirty:false,' +
  'originalManifest:"[run]",record:'+JSON.stringify(rec)+'};' +
  'showRequestForm('+JSON.stringify(rec)+')',ctx);
const body=()=>elem('dbody').innerHTML;
const mopts=html=>(html.match(/class="mopt"/g)||[]).length;
const opt=(mi,i)=>({dataset:{mfor:mi,mi:String(i)},
  closest:s=>s==='.mopt'?{dataset:{mfor:mi,mi:String(i)}}:null});
const key=(t,k)=>{let pd=0;
  handlers.keydown({target:t,key:k,preventDefault:()=>{pd++;}});
  return pd;};
const input=t=>handlers.input({target:t});
(async()=>{
  // markup: two combobox inputs, no datalist, listbox popups hidden
  assert.equal((body().match(/role="combobox"/g)||[]).length,2);
  assert.doesNotMatch(body(),/datalist id="rq-models"/);
  assert.match(body(),/aria-controls="rq-model-pop"/);
  assert.equal(elem('rq-model-pop').hidden,true);

  // both fields carry the class hook + the saved values
  const mi=elem('rq-model'), pi=elem('rq-plan_model');
  for (const inp of [mi,pi]) inp.classList.add('mcombo-in');
  mi.value='swe-2'; pi.value='gpt-6-luna';

  // empty input: no Recommendations group, whole catalog listed once
  mi.value='';
  handlers.focusin({target:mi});
  const pop=elem('rq-model-pop');
  assert.equal(pop.hidden,false);
  assert.equal(mi.attrs['aria-expanded'],'true');
  assert.doesNotMatch(pop.innerHTML,/Recommendations/);
  assert.match(pop.innerHTML,/Supported models/);
  assert.equal(mopts(pop.innerHTML),5);          // 4 families + adaptive
  assert.match(pop.innerHTML,/>adaptive</);      // routing mode included
  handlers.focusout({target:mi});
  assert.equal(pop.hidden,true);

  // partial match: recs on top, separator, full list intact below
  mi.value='swe'; input(mi);
  assert.match(pop.innerHTML,/Recommendations/);
  assert.match(pop.innerHTML,/All models/);
  assert.equal(mopts(pop.innerHTML),6);          // 1 rec + 5 all
  assert.ok(pop.innerHTML.indexOf('Recommendations') <
            pop.innerHTML.indexOf('All models'));
  // 'swe-2' shows in BOTH groups - distinct option ids keep
  // aria-activedescendant unambiguous
  const rIds=[...pop.innerHTML.matchAll(/id="(rq-model-opt-[ra]\d+)"/g)]
    .map(m=>m[1]);
  assert.equal(new Set(rIds).size,6);
  // 'swe' is no exact hit -> nothing active until the first ArrowDown,
  // which lands on the recs occurrence; the next one walks into the
  // all-list duplicate - same value, different option id
  assert.equal(mi.attrs['aria-activedescendant'],undefined);
  key(mi,'ArrowDown');
  assert.equal(mi.attrs['aria-activedescendant'],'rq-model-opt-r0');
  key(mi,'ArrowDown');                           // off the rec, into list
  assert.equal(mi.attrs['aria-activedescendant'],'rq-model-opt-a1');
  key(mi,'ArrowUp');
  assert.equal(mi.attrs['aria-activedescendant'],'rq-model-opt-r0');
  // the two occurrences carry the same value; Enter picks the active one
  assert.equal(vm.runInContext(
    'mcombo(document.getElementById("rq-model")).items[0].value',ctx),
    'swe-2');
  key(mi,'Enter');
  assert.equal(mi.value,'swe-2');
  assert.equal(pop.hidden,true);
  assert.equal(vm.runInContext('modalRequest.dirty',ctx),true);
  // picking a real family re-synced the effort datalist for it
  assert.match(elem('rq-efforts').innerHTML,/value="max"/);

  // no match: full list still offered, free text kept
  mi.value='zzz'; input(mi);
  assert.match(pop.innerHTML,/no matches/);
  assert.equal(mopts(pop.innerHTML),5);
  assert.equal(mi.value,'zzz');
  key(mi,'Enter');                               // nothing active -> close
  assert.equal(pop.hidden,true);
  assert.equal(mi.value,'zzz');                  // free text survives

  // Escape closes the popup only - the modal stays up
  handlers.focusin({target:mi});
  assert.equal(pop.hidden,false);
  key(mi,'Escape');
  assert.equal(pop.hidden,true);
  assert.equal(elem('backdrop').hidden,false);

  // ArrowDown on a closed popup opens it; -1 active -> first item
  mi.value='';
  key(mi,'ArrowDown');
  assert.equal(pop.hidden,false);
  assert.equal(mi.attrs['aria-activedescendant'],'rq-model-opt-a0');
  key(mi,'End');
  assert.equal(mi.attrs['aria-activedescendant'],'rq-model-opt-a4');
  // click an All-list option (flat items idx 2 = gpt-6-astra)
  await handlers.click({target:opt('rq-model',2),preventDefault:()=>{}});
  assert.equal(mi.value,'gpt-6-astra');
  assert.equal(pop.hidden,true);
  // adaptive pick disables its effort field through the shared handler
  mi.value=''; input(mi);
  await handlers.click({target:opt('rq-model',4),preventDefault:()=>{}});
  assert.equal(mi.value,'adaptive');
  assert.equal(elem('rq-effort').disabled,true);

  // planner field is its own combobox with its own option ids
  handlers.focusin({target:pi});
  const ppop=elem('rq-plan_model-pop');
  assert.equal(ppop.hidden,false);
  assert.match(ppop.innerHTML,/id="rq-plan_model-opt-r0"/);
  assert.equal(pi.attrs['aria-activedescendant'],'rq-plan_model-opt-r0');
  pi.value='opus'; input(pi);
  assert.match(ppop.innerHTML,/>claude-opus-5\.5</);
  key(pi,'ArrowDown'); key(pi,'Enter');
  assert.equal(pi.value,'claude-opus-5.5');
  // the planner pick synced the PLANNER effort list only
  assert.match(elem('rq-plan_efforts').innerHTML,/value="high"/);
  assert.equal(elem('rq-effort').disabled,true);   // adaptive stays n/a

  // outside click dismisses an open popup; a click on the input doesn't
  handlers.focusin({target:mi});
  assert.equal(pop.hidden,false);
  await handlers.click({target:mi,preventDefault:()=>{}});
  assert.equal(pop.hidden,false);
  await handlers.click({target:{id:'rq-label',dataset:{},closest:()=>null},
    preventDefault:()=>{}});
  assert.equal(pop.hidden,true);

  // persistence: both picked values land in requestFields + the save body
  elem('rq-raw').value='do it';
  mi.value='gpt-6-luna'; pi.value='claude-opus-5.5';
  const f=vm.runInContext('requestFields()',ctx);
  assert.equal(f.model,'gpt-6-luna');
  assert.equal(f.plan_model,'claude-opus-5.5');
  posts.length=0;
  await vm.runInContext('requestAction("save")',ctx);
  const saved=posts.find(p=>p.url==='/api/requests/r1/save');
  assert.equal(saved.body.model,'gpt-6-luna');
  assert.equal(saved.body.plan_model,'claude-opus-5.5');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class OutcomeSummaryTests(unittest.TestCase):
    """The worker report's third contract line: OUTCOME: - a human-facing
    one-to-two-sentence summary of what the step actually did. Parsed like
    NEXT:, persisted as rec['outcome'] (newest) plus rec['outcomes']
    (earlier attempts/dispatches), and surfaced in the task modal, the
    agents-table result column, the /summary report and `status`."""

    def test_preamble_contract_requires_all_three_lines(self):
        for line in ("STATUS: done | partial | blocked",
                     "NEXT: <one line",
                     "OUTCOME: <"):
            self.assertIn(line, conductor.PREAMBLE)

    def test_tail_outcome_parsing(self):
        """Last match wins, the whole log is scanned (a long closing
        report must not hide it), and missing/garbage input is None."""
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "s.log"
            self.assertIsNone(conductor.tail_outcome(log))   # no file
            log.write_text("no report here\n", encoding="utf-8")
            self.assertIsNone(conductor.tail_outcome(log))
            log.write_text(
                "OUTCOME: first draft landed\n" + "x" * 20000 +
                "\nOUTCOME:  finished all of it; verify red on flakes  \n"
                "more closing prose\n", encoding="utf-8")
            self.assertEqual(conductor.tail_outcome(log),
                             "finished all of it; verify red on flakes")
            log.write_text("OUTCOME: " + "y" * 500 + "\n",
                           encoding="utf-8")
            self.assertEqual(len(conductor.tail_outcome(log)), 300)
            # anchored at column zero like STATUS/NEXT: prose and markdown
            # bullets mentioning the token are not reports
            log.write_text("the fix: OUTCOME: faked in a sentence\n"
                           "- OUTCOME: faked in a bullet\n",
                           encoding="utf-8")
            self.assertIsNone(conductor.tail_outcome(log))

    def test_record_outcome_history_dedupes_and_preserves(self):
        rec = {}
        conductor._record_outcome(rec, "first attempt: parser only",
                                  status="timed_out", attempt=1)
        conductor._record_outcome(rec, "finished; suite green",
                                  status="done", attempt=2)
        self.assertEqual(rec["outcome"], "finished; suite green")
        self.assertEqual([e["outcome"] for e in rec["outcomes"]],
                         ["first attempt: parser only",
                          "finished; suite green"])
        self.assertEqual(rec["outcomes"][0]["status"], "timed_out")
        # a verbatim re-report re-stamps instead of duplicating
        conductor._record_outcome(rec, "finished; suite green",
                                  status="done", attempt=3)
        self.assertEqual(len(rec["outcomes"]), 2)
        # a dispatch that reports nothing keeps the last real summary
        conductor._record_outcome(rec, None, status="failed", attempt=4)
        self.assertEqual(rec["outcome"], "finished; suite green")
        self.assertEqual(len(rec["outcomes"]), 2)

    def test_mock_run_records_outcome_and_keeps_status_next(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "work"
''')
            self.assertEqual(run_manifest(m, runs), 0)
            run_dir = next(runs.iterdir())
            state = json.loads((run_dir / "state.json").read_text())
            rec = state["steps"]["one"]
            self.assertEqual(rec["status"], "done")
            self.assertEqual(rec["outcome"],
                             "appended a work line to WORK.txt (mock)")
            self.assertEqual(len(rec["outcomes"]), 1)
            self.assertEqual(rec["outcomes"][0]["status"], "done")
            self.assertEqual(rec["outcomes"][0]["attempt"], 1)
            self.assertEqual(rec["next"], "nothing - mock")   # unchanged
            # and it reaches the dashboard payload
            d = conductor.collect_dashboard(runs)
            a = d["agents"][0]
            self.assertEqual(a["outcome"], rec["outcome"])
            self.assertEqual(a["outcomes"], rec["outcomes"])
            self.assertEqual(a["next"], "nothing - mock")

    def test_outcome_attributed_to_its_own_step(self):
        """Each step's report lands on ITS record - a wave can't leak one
        step's summary onto a sibling's row."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "alpha"
project = "{proj.as_posix()}"
prompt = "work A\\noutcome=alpha did the parser; tests pending"

[[step]]
name = "beta"
project = "{proj.as_posix()}"
prompt = "work B\\noutcome=beta wrote the docs only"
''')
            self.assertEqual(run_manifest(m, runs), 0)
            state = json.loads(
                (next(runs.iterdir()) / "state.json").read_text())
            self.assertEqual(state["steps"]["alpha"]["outcome"],
                             "alpha did the parser; tests pending")
            self.assertEqual(state["steps"]["beta"]["outcome"],
                             "beta wrote the docs only")
            d = conductor.collect_dashboard(runs)
            by_step = {a["step"]: a for a in d["agents"]}
            self.assertEqual(by_step["alpha"]["outcome"],
                             "alpha did the parser; tests pending")
            self.assertEqual(by_step["beta"]["outcome"],
                             "beta wrote the docs only")

    def test_partial_status_keeps_its_outcome(self):
        """A partial report is exactly what the line is for: concrete
        progress + the named limit, not a green-looking 'done'."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "work\\nstatus=partial\\noutcome=parser landed; the edge cases are still untested"
''')
            self.assertEqual(run_manifest(m, runs), 0)
            rec = json.loads((next(runs.iterdir()) /
                              "state.json").read_text())["steps"]["one"]
            self.assertEqual(rec["status"], "partial")
            self.assertEqual(
                rec["outcome"],
                "parser landed; the edge cases are still untested")

    def test_missing_outcome_stays_honest_everywhere(self):
        """No OUTCOME: line -> nothing is invented: the record, the
        payload and the summary report all just say nothing was reported
        (NEXT:/notes are never substituted for it)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "work\\nnooutcome"
''')
            self.assertEqual(run_manifest(m, runs), 0)
            state = json.loads(
                (next(runs.iterdir()) / "state.json").read_text())
            rec = state["steps"]["one"]
            self.assertEqual(rec["status"], "done")
            self.assertIsNone(rec.get("outcome"))
            self.assertIsNone(rec.get("outcomes"))
            self.assertEqual(rec["next"], "nothing - mock")
            d = conductor.collect_dashboard(runs)
            self.assertIsNone(d["agents"][0]["outcome"])
            md = conductor.step_summary_md({**rec, "name": "one"},
                                         state, "t")
            self.assertNotIn("**outcome:**", md)
            self.assertNotIn("**earlier outcome**", md)

    def test_resume_updates_outcome_and_keeps_earlier(self):
        """A resumed step's fresh report replaces the displayed outcome;
        the first attempt's accomplishment stays in the history."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "work\\noutcome=first pass: parser landed, tests pending"
''')
            self.assertEqual(run_manifest(m, runs), 0)
            run_dir = next(runs.iterdir())
            state = json.loads((run_dir / "state.json").read_text())
            state["steps"]["one"]["status"] = "failed"   # force a re-run
            (run_dir / "state.json").write_text(json.dumps(state))
            # same manifest file, refreshed report text for the resume
            m.write_text(f'''[run]
name = "t"

[[step]]
name = "one"
project = "{proj.as_posix()}"
prompt = "work\\noutcome=second pass: tests landed too"
''')
            self.assertEqual(run_manifest(m, runs, resume=str(run_dir)), 0)
            rec = json.loads((run_dir / "state.json").read_text()
                             )["steps"]["one"]
            self.assertEqual(rec["outcome"],
                             "second pass: tests landed too")
            self.assertEqual(
                [e["outcome"] for e in rec["outcomes"]],
                ["first pass: parser landed, tests pending",
                 "second pass: tests landed too"])

    def test_merge_notes_never_clobber_outcome(self):
        rec = {"status": "done"}
        conductor._record_outcome(rec, "landed the feature",
                                  status="done", attempt=1)
        conductor._append_note(rec, "merge skipped: staged verify red")
        conductor._append_note(rec, "merge still conflicts; resolve the "
                                    "kept worktree by hand")
        self.assertEqual(rec["outcome"], "landed the feature")
        self.assertIn("merge skipped", rec["note"])
        self.assertEqual(len(rec["outcomes"]), 1)

    def test_retry_attempts_keep_each_outcome(self):
        """max_attempts retries: the timed-out attempt's report and the
        winning attempt's report are BOTH kept, newest displayed."""
        calls = {"n": 0}

        class FakeProc:
            pid = 4242

            def __init__(self, *a, **kw):
                self._out = kw["stdout"]
                self.killed = False
                calls["n"] += 1
                if calls["n"] == 1:
                    # attempt 1 reports an outcome but never STATUS ->
                    # killed at the timeout floor
                    self._out.write(b"OUTCOME: first try got the "
                                    b"scaffold only\n")
                    self._out.flush()
                else:
                    self._out.write(b"OUTCOME: feature complete, "
                                    b"suite green\nSTATUS: done\n"
                                    b"NEXT: none\n")
                    self._out.flush()

            def wait(self, timeout=None):
                if calls["n"] > 1 or self.killed:
                    return 0
                raise subprocess.TimeoutExpired("devin", timeout)

            def poll(self):
                return 0 if self.killed else None

            def kill(self):
                self.killed = True

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            run_dir = base / "runs" / "r1"
            (run_dir / "prompts").mkdir(parents=True)
            cfg = conductor.load_manifest(write_manifest(base, f'''
[[step]]
name = "a"
project = "{proj.as_posix()}"
prompt = "p"
timeout_min = 0.001
max_attempts = 2
'''))
            state = {"run": "t", "run_dir": str(run_dir), "steps": {}}
            with mock.patch.object(conductor.subprocess, "Popen",
                                   FakeProc), \
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
                    mock.patch.object(conductor, "WAIT_POLL_S", 0.01):
                st = conductor.run_step(cfg.steps[0], "s", proj, cfg,
                                        run_dir, ["devin"], state)
            self.assertEqual(st, "done")
            rec = state["steps"]["a"]
            self.assertEqual(rec["attempts"], 2)
            self.assertEqual(rec["outcome"],
                             "feature complete, suite green")
            self.assertEqual(
                [(e["attempt"], e["status"]) for e in rec["outcomes"]],
                [(1, "timed_out"), (2, "done")])

    def test_outcome_in_summary_report(self):
        rec = {"status": "done", "task": "t",
               "outcome": "landed the thing",
               "outcomes": [
                   {"outcome": "early draft", "attempt": 1,
                    "status": "timed_out",
                    "ended_at": "2026-10-16T01:00:00"},
                   {"outcome": "landed the thing", "attempt": 2,
                    "status": "done",
                    "ended_at": "2026-10-16T02:00:00"}],
               "note": "n", "next": "watch it",
               "tokens": {}}
        md = conductor.step_summary_md(rec, {"steps": {"s": rec}}, "r")
        self.assertIn("- **outcome:** landed the thing", md)
        self.assertIn("- **earlier outcome** (attempt 1, timed_out): "
                      "early draft", md)
        self.assertIn("- **worker says next:** watch it", md)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_outcome_in_modal_and_result_column_js(self):
        """Modal gets an 'outcome' row (honest muted state when absent,
        'earlier' rows for prior attempts) and the agents-table result
        column leads with it - everything escaped, NEXT stays its own
        row under its own name."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true,textContent:''}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,querySelectorAll:()=>[],
  activeElement:null,addEventListener:(e,h)=>{handlers[e]=h;}};
const ctx=vm.createContext({document,location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'2026-10-16T00:00:00',agents:[
  {step:'one',run:'r',run_dir:'/r',slug:'p',status:'done',task:'t1',
   outcome:'landed the <img src=x onerror=alert(1)> parser; tests pending',
   outcomes:[{outcome:'first try: scaffold only',attempt:1,
              status:'timed_out',ended_at:'2026-10-16T01:00:00'},
             {outcome:'landed the <img src=x onerror=alert(1)> parser; tests pending',
              attempt:2,status:'done',ended_at:'2026-10-16T02:00:00'}],
   note:'retried after timeout', next:'re-run the flaky suite'},
  {step:'two',run:'r',run_dir:'/r',slug:'p',status:'done',task:'t2',
   note:'verify gate is red'},
  {step:'three',run:'r',run_dir:'/r',slug:'p',status:'running',task:'t3',
   tail_last:'still working'}],
 runs:[{run:'r',dir:'/r',counts:{done:2,running:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const agents=elem('agents').innerHTML;
// result column: outcome leads, conductor note survives on a sub line,
// worker text is always escaped, no outcome falls back to the note
assert.match(agents,/<th title="the worker-reported OUTCOME: summary/);
assert.match(agents,/landed the &lt;img src=x onerror=alert\(1\)&gt; parser; tests pending/);
assert.doesNotMatch(agents,/<img src=x onerror/);
assert.match(agents,/<span class="sub">retried after timeout<\/span>/);
assert.match(agents,/>verify gate is red</);
assert.match(agents,/still working/);
const open=step=>vm.runInContext(
  'showDetail(AGENTS.findIndex(a=>a.step==='+JSON.stringify(step)+'))',ctx);
(async()=>{
  await open('one');
  const b=()=>elem('dbody').innerHTML;
  assert.match(b(),/<div class="k">outcome<\/div>/);
  assert.match(b(),/landed the &lt;img src=x onerror=alert\(1\)&gt; parser; tests pending/);
  assert.doesNotMatch(b(),/<img src=x onerror/);
  assert.match(b(),/<div class="k">earlier<\/div><div class="wrap">attempt 1 \(timed_out, 2026-10-16T01:00\): first try: scaffold only<\/div>/);
  assert.match(b(),/<div class="k">next<\/div><div class="wrap">re-run the flaky suite<\/div>/);
  assert.match(b(),/<div class="k">note<\/div><div class="wrap">retried after timeout<\/div>/);
  assert.doesNotMatch(b(),/<div class="k">result<\/div>/);
  await open('two');
  assert.match(b(),/<div class="k">outcome<\/div><div class="wrap"><span class="sub">not reported/);
  assert.match(b(),/predates the OUTCOME: line or ended before printing it/);
  assert.doesNotMatch(b(),/<div class="k">earlier<\/div>/);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class ReviewWorkflowTests(_RunControlBase):
    """Review acknowledgment for finalized runs + tasks: completion_seq
    bumps once per terminal-outcome write; review-* sentinels carry the
    human's mark (trap-5 safe) and fold into state.json on the next
    save; an ack binds the seq it saw so a re-completion re-flags while
    repeated saves of the same outcome never do. Run and task marks are
    independent, and 'unreviewed' never masquerades as success."""

    @staticmethod
    def _state(run_dir: Path) -> dict:
        return json.loads((run_dir / "state.json").read_text(
            encoding="utf-8"))

    def _collect(self, runs: Path) -> dict:
        with mock.patch.object(conductor, "refresh_prices"):
            return conductor.collect_dashboard(
                runs, ["no-such-devin"],
                requests_dir=runs.parent / "requests")

    @staticmethod
    def _dead_driver(run_dir: Path):
        """The run ended, so its conductor pid is dead. Mock runs execute
        in-process (driver_pid = this test process, still alive) - stamp a
        dead pid so the immediate state.json fold can run like it would
        after a real run's driver exits."""
        sj = run_dir / "state.json"
        st = json.loads(sj.read_text(encoding="utf-8"))
        st["driver_pid"] = 999_999_999
        sj.write_text(json.dumps(st), encoding="utf-8")

    def _finish_run(self, base: Path, prompt: str = "first",
                    want_rc: int = 0):
        """A one-step mock run to completion; returns manifest, runs dir,
        and the run dir it created."""
        proj = make_project(base)
        runs = base / "runs"
        m = write_manifest(base, f'''
[[step]]
name = "s1"
project = "{proj.as_posix()}"
prompt = "{prompt}"
''')
        self.assertEqual(run_manifest(m, runs), want_rc)
        run_dir = next(runs.glob("*/state.json")).parent
        self._dead_driver(run_dir)
        self.addCleanup(lambda: [
            conductor.review_flag(run_dir, s).unlink(missing_ok=True)
            for s in (None, "s1", "s2")])
        return m, runs, run_dir

    def test_completion_seq_stamped_on_terminal_writes(self):
        with tempfile.TemporaryDirectory() as td:
            _m, runs, run_dir = self._finish_run(Path(td))
            st = self._state(run_dir)
            self.assertEqual(st["steps"]["s1"]["completion_seq"], 1)
            self.assertEqual(st["completion_seq"], 1)
            # ...and the dashboard reports both as needing review,
            # with status untouched.
            d = self._collect(runs)
            a = next(x for x in d["agents"] if x["step"] == "s1")
            r = d["runs"][0]
            self.assertEqual(a["status"], "done")
            self.assertTrue(a["needs_review"])
            self.assertEqual(a["completion_seq"], 1)
            self.assertTrue(r["needs_review"])

    def test_failed_outcome_still_needs_review(self):
        """'unreviewed' is never a success badge: a failed step keeps its
        red status AND the review flag."""
        with tempfile.TemporaryDirectory() as td:
            _m, runs, run_dir = self._finish_run(Path(td), "mockfail", 1)
            st = self._state(run_dir)
            self.assertEqual(st["steps"]["s1"]["status"], "failed")
            self.assertTrue(st["steps"]["s1"]["completion_seq"])
            a = next(x for x in self._collect(runs)["agents"]
                     if x["step"] == "s1")
            self.assertEqual(a["status"], "failed")
            self.assertTrue(a["needs_review"])

    def test_ack_reflag_and_persistence(self):
        """ack clears the indicator; a tombstone restores it; the mark
        folds into state.json and survives a fresh read (restart)."""
        with tempfile.TemporaryDirectory() as td:
            _m, runs, run_dir = self._finish_run(Path(td))
            out = conductor.set_step_review(
                run_dir.name, "s1", {"reviewed": True}, runs)
            self.assertFalse(out["needs_review"])
            self.assertEqual(out["review"]["seq"], 1)
            # folded into state.json right away (nothing live)
            rec = self._state(run_dir)["steps"]["s1"]
            self.assertEqual(rec["review"]["reviewed"], True)
            self.assertEqual(rec["review"]["seq"], 1)
            self.assertTrue(conductor.review_flag(run_dir, "s1").is_file())
            a = next(x for x in self._collect(runs)["agents"]
                     if x["step"] == "s1")
            self.assertFalse(a["needs_review"])
            # tombstone re-arms it; a second ack clears it again
            out = conductor.set_step_review(
                run_dir.name, "s1", {"reviewed": False}, runs)
            self.assertTrue(out["needs_review"])
            self.assertTrue(self._state(run_dir)["steps"]["s1"]
                            ["review"]["reviewed"] is False)
            conductor.set_step_review(
                run_dir.name, "s1", {"reviewed": True}, runs)
            a = next(x for x in self._collect(runs)["agents"]
                     if x["step"] == "s1")
            self.assertFalse(a["needs_review"])

    def test_same_completion_updates_never_reset_ack(self):
        """Repeated saves of the same outcome and unrelated metadata
        edits keep the ack - the seq never moves on them."""
        with tempfile.TemporaryDirectory() as td:
            _m, runs, run_dir = self._finish_run(Path(td))
            conductor.set_step_review(
                run_dir.name, "s1", {"reviewed": True}, runs)
            conductor.set_run_review(
                run_dir.name, {"reviewed": True}, runs)
            st = self._state(run_dir)
            # metadata-only churn: a note edit plus repeated saves
            rec = st["steps"]["s1"]
            rec["note"] = "operator note edit after the fact"
            conductor.save_state(run_dir, st)
            st = self._state(run_dir)
            conductor.save_state(run_dir, st)       # plain re-save
            st = self._state(run_dir)
            self.assertEqual(st["steps"]["s1"]["completion_seq"], 1)
            self.assertEqual(st["completion_seq"], 1)
            a = next(x for x in self._collect(runs)["agents"]
                     if x["step"] == "s1")
            self.assertFalse(a["needs_review"])
            self.assertFalse(self._collect(runs)["runs"][0]["needs_review"])

    def test_run_and_step_marks_are_independent(self):
        with tempfile.TemporaryDirectory() as td:
            _m, runs, run_dir = self._finish_run(Path(td))
            # ack the run only -> the task stays unreviewed
            conductor.set_run_review(
                run_dir.name, {"reviewed": True}, runs)
            d = self._collect(runs)
            self.assertFalse(d["runs"][0]["needs_review"])
            a = next(x for x in d["agents"] if x["step"] == "s1")
            self.assertTrue(a["needs_review"])
            # ack the task only -> the run stays unreviewed
            conductor.set_run_review(
                run_dir.name, {"reviewed": False}, runs)
            conductor.set_step_review(
                run_dir.name, "s1", {"reviewed": True}, runs)
            d = self._collect(runs)
            self.assertTrue(d["runs"][0]["needs_review"])
            a = next(x for x in d["agents"] if x["step"] == "s1")
            self.assertFalse(a["needs_review"])

    def test_legacy_records_never_autoflag_but_take_marks(self):
        """A seq-less historical record is not presented as freshly
        completed - though a human can still mark it both ways."""
        with tempfile.TemporaryDirectory() as td:
            _m, runs, run_dir = self._finish_run(Path(td))
            st = self._state(run_dir)
            st["steps"]["s1"].pop("completion_seq", None)
            st.pop("completion_seq", None)
            (run_dir / "state.json").write_text(
                json.dumps(st), encoding="utf-8")
            d = self._collect(runs)
            a = next(x for x in d["agents"] if x["step"] == "s1")
            self.assertFalse(a["needs_review"])
            self.assertFalse(d["runs"][0]["needs_review"])
            # 'mark reviewed' on a seq-less record with no tombstone:
            # nothing to review -> refused
            with self.assertRaises(conductor.RequestError) as ctx:
                conductor.set_step_review(
                    run_dir.name, "s1", {"reviewed": True}, runs)
            self.assertEqual(ctx.exception.status, 409)
            # tombstone, then clear it by hand - both are allowed
            conductor.set_step_review(
                run_dir.name, "s1", {"reviewed": False}, runs)
            a = next(x for x in self._collect(runs)["agents"]
                     if x["step"] == "s1")
            self.assertTrue(a["needs_review"])
            conductor.set_step_review(
                run_dir.name, "s1", {"reviewed": True}, runs)
            a = next(x for x in self._collect(runs)["agents"]
                     if x["step"] == "s1")
            self.assertFalse(a["needs_review"])

    def test_older_ack_never_hides_a_newer_completion(self):
        with tempfile.TemporaryDirectory() as td:
            _m, runs, run_dir = self._finish_run(Path(td))
            # simulate a mark bound to an older seq than the record's
            rec = self._state(run_dir)["steps"]["s1"]
            self.assertTrue(
                conductor._needs_review(
                    2, {"reviewed": True, "seq": 1,
                        "at": conductor.now_iso()}))
            self.assertTrue(
                conductor._needs_review(rec["completion_seq"], None))
            self.assertFalse(
                conductor._needs_review(
                    1, {"reviewed": True, "seq": 1,
                        "at": conductor.now_iso()}))
            # tombstone always shows, seq or not
            self.assertTrue(
                conductor._needs_review(
                    None, {"reviewed": False, "seq": None,
                           "at": conductor.now_iso()}))
            # and the endpoint's seq guard: an ack made against a stale
            # view is refused instead of masking the newer completion
            with self.assertRaises(conductor.RequestError) as ctx:
                conductor.set_step_review(
                    run_dir.name, "s1",
                    {"reviewed": True, "seq": 99}, runs)
            self.assertEqual(ctx.exception.status, 409)

    def test_flag_overlay_reads_live_and_folds_on_save(self):
        """A sentinel written 'while the driver is up' is honored by
        collect_dashboard before any save, then lands in state.json on
        the next save_state - restart-proof."""
        with tempfile.TemporaryDirectory() as td:
            _m, runs, run_dir = self._finish_run(Path(td))
            flag = conductor.review_flag(run_dir, "s1")
            flag.parent.mkdir(exist_ok=True)
            flag.write_text(json.dumps(
                {"reviewed": True, "seq": 1, "at": conductor.now_iso()}),
                encoding="utf-8")
            # live overlay: no fold yet - state.json still has no mark
            self.assertNotIn("review", self._state(run_dir)["steps"]["s1"])
            a = next(x for x in self._collect(runs)["agents"]
                     if x["step"] == "s1")
            self.assertFalse(a["needs_review"])
            # next driver save folds the mark into the durable record
            conductor.save_state(run_dir, self._state(run_dir))
            rec = self._state(run_dir)["steps"]["s1"]
            self.assertEqual(rec["review"]["seq"], 1)
            # a stale flag never masks a newer folded mark
            flag.write_text(json.dumps(
                {"reviewed": False, "seq": 1,
                 "at": "2001-01-01T00:00:00"}), encoding="utf-8")
            a = next(x for x in self._collect(runs)["agents"]
                     if x["step"] == "s1")
            self.assertFalse(a["needs_review"])

    def test_clear_run_flags_preserves_review(self):
        with tempfile.TemporaryDirectory() as td:
            _m, runs, run_dir = self._finish_run(Path(td))
            conductor.set_step_review(
                run_dir.name, "s1", {"reviewed": True}, runs)
            conductor._flag_raise(conductor.stop_flag(run_dir).name)
            conductor._flag_raise(
                conductor.review_flag(run_dir, "s2").name)
            conductor.clear_run_flags(run_dir)
            self.assertFalse(conductor.stop_flag(run_dir).is_file())
            self.assertTrue(
                conductor.review_flag(run_dir, "s1").is_file())
            self.assertTrue(
                conductor.review_flag(run_dir, "s2").is_file())

    def test_sweep_lost_mark_bumps_seq(self):
        """A stale 'running' record swept to 'lost' is a new outcome -
        it gets its own completion_seq and flags for review."""
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            rd = runs / "20260101-000000-t"
            rd.mkdir(parents=True)
            (rd / "state.json").write_text(json.dumps({
                "run": "t", "started_at": "2026-01-01T00:00:00",
                "driver_pid": 999_999_999,
                "steps": {"s1": {"name": "s1", "status": "running",
                                 "started_at": "2026-01-01T00:00:00",
                                 "ended_at": "2026-01-01T00:10:00",
                                 "stdout_log": "gone.log"}}}),
                encoding="utf-8")
            lines = conductor.sweep_runs(runs)
            self.assertTrue(any("lost" in ln for ln in lines))
            st = self._state(rd)
            self.assertEqual(st["steps"]["s1"]["status"], "lost")
            self.assertEqual(st["steps"]["s1"]["completion_seq"], 1)
            self.assertEqual(st["completion_seq"], 1)   # run closed too

    def test_resume_recompletion_requires_review_again(self):
        """Stop mid-run -> stop marks seq 1 -> ack -> resume re-runs the
        step -> seq 2; the old ack no longer covers it."""
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
            self.addCleanup(lambda: [
                conductor.review_flag(run_dir, s).unlink(missing_ok=True)
                for s in (None, "s1", "s2")])
            conductor.stop_run(run_dir.name, {}, runs)
            t.join(timeout=30)
            self._dead_driver(run_dir)
            st = self._state(run_dir)
            self.assertEqual(st["steps"]["s1"]["status"], "stopped")
            self.assertEqual(st["steps"]["s1"]["completion_seq"], 1)
            # human acks the stopped outcome - flag survives resume
            conductor.set_step_review(
                run_dir.name, "s1", {"reviewed": True}, runs)
            self.assertEqual(
                self._state(run_dir)["steps"]["s1"]["review"]["seq"], 1)
            rc = run_manifest(m, runs, resume=str(run_dir))
            self.assertEqual(rc, 0)
            st = self._state(run_dir)
            rec = st["steps"]["s1"]
            self.assertEqual(rec["status"], "done")
            self.assertEqual(rec["completion_seq"], 2)
            self.assertEqual(rec["review"]["seq"], 1)   # folded, kept
            self.assertTrue(
                conductor.review_flag(run_dir, "s1").is_file())
            d = self._collect(runs)
            a = next(x for x in d["agents"] if x["step"] == "s1")
            self.assertTrue(a["needs_review"])
            self.assertTrue(d["runs"][0]["needs_review"])

    def test_review_endpoints_over_http(self):
        """POST /api/runs/<d>/review and .../steps/<s>/review carry the
        marks; unknown runs/steps 404, a stale-seq ack 409s, a non-bool
        'reviewed' 422s, and 'mark reviewed' with nothing to review 409s."""
        with tempfile.TemporaryDirectory() as td:
            _m, runs, run_dir = self._finish_run(Path(td))
            reqs = Path(td) / "reqs"
            handler = conductor.make_dashboard_handler(
                runs, ["no-such-devin"], reqs, Path(td) / "manifests")
            server = conductor.http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever,
                             daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            port = server.server_port

            def call(method, path, data=None):
                conn = http.client.HTTPConnection("127.0.0.1", port,
                                                  timeout=5)
                conn.request(
                    method, path,
                    json.dumps(data).encode("utf-8") if data is not None
                    else None,
                    {"Content-Type": "application/json",
                     "Origin": f"http://127.0.0.1:{port}"})
                res = conn.getresponse()
                payload = json.loads(res.read().decode("utf-8"))
                conn.close()
                return res.status, payload

            status, out = call("POST",
                               f"/api/runs/{run_dir.name}/review",
                               {"reviewed": True})
            self.assertEqual(status, 200)
            self.assertFalse(out["needs_review"])
            self.assertEqual(out["review"]["seq"], 1)
            status, out = call(
                "POST",
                f"/api/runs/{run_dir.name}/steps/s1/review",
                {"reviewed": False})
            self.assertEqual(status, 200)
            self.assertTrue(out["needs_review"])
            # the run mark above never covered the task
            status, _ = call("POST", "/api/runs/nope/review",
                             {"reviewed": True})
            self.assertEqual(status, 404)
            status, _ = call(
                "POST", f"/api/runs/{run_dir.name}/steps/nope/review",
                {"reviewed": True})
            self.assertEqual(status, 404)
            status, _ = call("POST", f"/api/runs/{run_dir.name}/review",
                             {"reviewed": "yes"})
            self.assertEqual(status, 422)
            status, _ = call("POST", f"/api/runs/{run_dir.name}/review",
                             {"reviewed": True, "seq": 42})
            self.assertEqual(status, 409)

    def test_nothing_to_review_is_refused(self):
        """A run with no completion yet can't be 'marked reviewed' -
        only a tombstone (explicit 'needs review') is legal there."""
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            rd = runs / "20260101-000000-t"
            rd.mkdir(parents=True)
            (rd / "state.json").write_text(json.dumps({
                "run": "t", "started_at": "2026-01-01T00:00:00",
                "driver_pid": 999_999_999,
                "steps": {"s1": {"name": "s1", "status": "failed",
                                 "ended_at": "2026-01-01T00:10:00"}}}),
                encoding="utf-8")
            self.addCleanup(lambda: [
                conductor.review_flag(rd, s).unlink(missing_ok=True)
                for s in (None, "s1")])
            with self.assertRaises(conductor.RequestError) as ctx:
                conductor.set_run_review(
                    rd.name, {"reviewed": True}, runs)
            self.assertEqual(ctx.exception.status, 409)
            # tombstone IS legal (flag a run for attention)
            out = conductor.set_run_review(
                rd.name, {"reviewed": False}, runs)
            self.assertTrue(out["needs_review"])

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_js_review_badges_modal_and_ctx(self):
        """Table pills, row accents + a11y text, the modal's review kv
        and buttons, ctx-menu parity, and the delegated POST dispatch -
        including that a failed row keeps its status text next to the
        pill."""
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map(), handlers={};
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,
  {innerHTML:'',hidden:true,textContent:'',scrollTop:0});
  return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],activeElement:null,
  addEventListener:(ev,h)=>{handlers[ev]=h;}};
const posts=[], calls=[];
const fetch=async(url,opt)=>{
  calls.push([url,opt&&opt.body?JSON.parse(opt.body):null]);
  if (url.startsWith('/api/state'))
    return {ok:true,status:200,json:async()=>data};
  posts.push([url,opt&&opt.body?JSON.parse(opt.body):null]);
  return {ok:true,status:200,json:async()=>({needs_review:false})};
};
const ctx=vm.createContext({document,fetch,confirm:()=>true,alert:()=>{},
  location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'2026-10-14T00:00:00',agents:[
  {step:'s1',run:'r1',run_dir:'/r1',run_key:'k1',slug:'p',status:'done',
   task:'t1',completion_seq:1,needs_review:true},
  {step:'s2',run:'r1',run_dir:'/r1',run_key:'k1',slug:'p',status:'failed',
   task:'t2',completion_seq:3,needs_review:true,
   review:{reviewed:true,seq:1,at:'2026-10-14T00:30:00'}},
  {step:'s3',run:'r2',run_dir:'/r2',run_key:'k2',slug:'p',status:'done',
   task:'t3',completion_seq:1,needs_review:false,
   review:{reviewed:true,seq:1,at:'2026-10-14T01:00:00'}},
  {step:'plan-proposal',run:'rq',run_dir:null,run_key:'request:q1',
   slug:'p',status:'done',task:'planner',needs_review:false}],runs:[
  {run:'r1',dir:'/r1',counts:{done:1,failed:1},
   ended_at:'2026-10-14T01:00:00',
   completion_seq:2,needs_review:true},
  {run:'r2',dir:'/r2',counts:{done:1},ended_at:'2026-10-14T02:00:00',
   completion_seq:1,needs_review:false,
   review:{reviewed:true,seq:1,at:'2026-10-14T02:00:00'}},
  {run:'rq',dir:'/q.toml',key:'request:q1',request_id:'q1',stage:'ready',
   counts:{planned:1}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const runs=elem('runs').innerHTML;
// run row: unreviewed class + pill text + aria mention; a reviewed run
// carries none of them; a proposal/stage row never flags
const r1row=runs.split('<tr').find(x=>x.includes('>r1<'));
assert.match(r1row,/runrow[^"]*unreviewed/);
assert.match(r1row,/class="stage unreviewed"[^>]*>unreviewed</);
assert.match(r1row,/aria-label="Filter to run r1 · unreviewed"/);
const r2row=runs.split('<tr').find(x=>x.includes('>r2<'));
assert.doesNotMatch(r2row,/unreviewed/);
const rqrow=runs.split('<tr').find(x=>x.includes('>rq<'));
assert.doesNotMatch(rqrow,/unreviewed/);
const agents=elem('agents').innerHTML;
// task rows: failed step keeps its red dot+text AND the pill - the
// review flag never reads as success
const s2row=agents.split('<tr').find(x=>x.includes('>s2<'));
assert.match(s2row,/unreviewed/);
assert.match(s2row,/>failed/);
assert.match(s2row,/aria-label="task s2 · unreviewed"/);
const s3row=agents.split('<tr').find(x=>x.includes('>s3<'));
assert.doesNotMatch(s3row,/unreviewed/);
const s4row=agents.split('<tr').find(x=>x.includes('>plan-proposal<'));
assert.doesNotMatch(s4row,/unreviewed/);
const click=async(ds)=>handlers.click({target:{dataset:ds,
  closest:()=>null,disabled:false},preventDefault:()=>{}});
const open=async(step)=>{ const i=vm.runInContext(
  'AGENTS.findIndex(a=>a.step==='+JSON.stringify(step)+')',ctx);
  vm.runInContext('showDetail('+i+')',ctx); };
(async()=>{
  // task modal: review kv line + both buttons for an unreviewed task
  await open('s1');
  let body=elem('dbody').innerHTML;
  assert.match(body,/<div class="k">review<\/div><div class="wrap">needs review<\/div>/);
  assert.match(body,/data-review-step="r1"[^>]*data-step="s1"[^>]*data-review-set="1"[^>]*data-review-seq="1"/);
  assert.match(body,/>Mark as reviewed</);
  // clicking it posts to the step endpoint with its seq
  await click({reviewStep:'r1',step:'s1',reviewSet:'1',reviewSeq:'1'});
  assert.deepEqual(posts.pop(),
    ['/api/runs/r1/steps/s1/review',{reviewed:true,seq:1}]);
  // a reviewed task shows the reviewed line + the reversal button
  await open('s3');
  body=elem('dbody').innerHTML;
  assert.match(body,/<div class="k">review<\/div><div class="wrap">reviewed 2026-10-14T01:00:00<\/div>/);
  assert.match(body,/>Mark as needing review</);
  await click({reviewStep:'r2',step:'s3',reviewSet:'0',reviewSeq:'1'});
  assert.deepEqual(posts.pop(),
    ['/api/runs/r2/steps/s3/review',{reviewed:false,seq:1}]);
  // run modal: same control pair at run level
  vm.runInContext("showRunDetails('/r1')",ctx);
  body=elem('dbody').innerHTML;
  assert.match(body,/<div class="k">review<\/div><div class="wrap">needs review<\/div>/);
  assert.match(body,/data-review-run="r1"[^>]*data-review-set="1"[^>]*data-review-seq="2"/);
  await click({reviewRun:'r1',reviewSet:'1',reviewSeq:'2'});
  assert.deepEqual(posts.pop(),['/api/runs/r1/review',{reviewed:true,seq:2}]);
  // a reviewed run offers the reversal; its kv names the mark
  vm.runInContext("showRunDetails('/r2')",ctx);
  body=elem('dbody').innerHTML;
  assert.match(body,/<div class="k">review<\/div><div class="wrap">reviewed 2026-10-14T02:00:00<\/div>/);
  assert.match(body,/>Mark as needing review</);
  // proposal modal carries no review controls at all
  vm.runInContext("showRunDetails('request:q1')",ctx);
  body=elem('dbody').innerHTML;
  assert.doesNotMatch(body,/data-review-/);
  assert.doesNotMatch(body,/class="k">review/);
  // ctx menus: same hooks at both levels
  const ritems=vm.runInContext(
    'ctxRunItems(lastData.runs[0]).map(i=>i.label)',ctx);
  assert.ok(ritems.includes('Mark as reviewed'));
  const r2items=vm.runInContext(
    'ctxRunItems(lastData.runs[1]).map(i=>i.label)',ctx);
  assert.ok(r2items.includes('Mark as needing review'));
  const aitems=vm.runInContext(
    'ctxAgentItems(AGENTS.find(a=>a.step===\'s1\')).map(i=>i.label)',ctx);
  assert.ok(aitems.includes('Mark as reviewed'));
  // planner row: run_dir is null -> no review item
  const pitems=vm.runInContext(
    'ctxAgentItems(AGENTS.find(a=>a.step===\'plan-proposal\')).map(i=>i.label)',ctx);
  assert.ok(!pitems.includes('Mark as reviewed'));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)


class FollowupDiscoveryTests(unittest.TestCase):
    """A produced <run>-followup.toml is intake, not execution: discovery
    registers it as a persisted proposal request for a human to review and
    explicitly Start - malformed/missing/empty outputs land as honest,
    dismissible statuses, and registration never spawns work."""
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

    def proposal(self, name="rest-followup",
                 description="Finish the leftover work"):
        return f'''[run]
name = "{name}"
description = "{description}"
checkpoint = true

[defaults]
project = "proj"
model = "swe-2"
effort = "max"
max_attempts = 2

[[step]]
name = "leftover"
prompt = "Finish the leftovers."
'''

    def run_dir(self, name="rest", steps=None, **state):
        d = self.runs / f"20261017-000000-{name}"
        d.mkdir(parents=True, exist_ok=True)
        st = {"run": name, "manifest": str(self.manifests / f"{name}.toml"),
              "ended_at": "2026-10-17T01:00:00",
              "steps": steps if steps is not None else {
                  "follow-up-plan": {"status": "done",
                                     "ended_at": "2026-10-17T01:00:00"}}}
        st.update(state)
        (d / "state.json").write_text(json.dumps(st), encoding="utf-8")
        return d

    def followup_file(self, name="rest", text=None, manifests=None):
        p = (manifests or self.manifests) / f"{name}-followup.toml"
        p.write_text(text if text is not None
                     else self.proposal(f"{name}-followup"),
                     encoding="utf-8")
        return p

    def recs(self):
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in self.requests.glob("*.json")]

    def test_valid_registers_ready_and_durable(self):
        run = self.run_dir("rest")
        src = self.followup_file("rest")
        lines = conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        self.assertTrue(any("registered" in l for l in lines), lines)
        recs = self.recs()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["source_run"], run.name)
        self.assertEqual(rec["source_step"], "follow-up-plan")
        self.assertEqual(rec["source_manifest"], str(src))
        self.assertEqual(rec["imported"], "followup")
        self.assertEqual(len(rec["proposed_steps"]), 1)
        owned = Path(rec["manifest"])
        self.assertTrue(owned.name.startswith(rec["id"] + "-"))
        self.assertTrue(owned.is_file())
        self.assertEqual(owned.read_text(encoding="utf-8"),
                         src.read_text(encoding="utf-8"))
        self.assertTrue(src.is_file())          # source is only read
        # durable listing: the row reads 'Ready to run' across collection
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        row = next(r for r in d["runs"]
                   if r.get("request_id") == rec["id"])
        self.assertEqual(row["stage"], "ready")
        self.assertEqual(row["counts"], {"planned": 1})
        self.assertEqual(row["followup_src"]["run"], run.name)
        self.assertEqual({a["status"] for a in d["agents"]
                          if a["step"] == "leftover"}, {"planned"})
        self.assertTrue(any(
            a["status"] == "done" and a["step"] == "follow-up-plan"
            for a in d["agents"]))       # the producer step itself
        run_row = next(r for r in d["runs"] if r.get("dir") == str(run))
        self.assertEqual(run_row["followup"]["status"], "registered")
        self.assertEqual(run_row["followup"]["request"], rec["id"])
        st = json.loads((run / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["followup"]["request"], rec["id"])

    def test_repeated_passes_never_duplicate_or_launch(self):
        self.run_dir("rest")
        self.followup_file("rest")
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        rid = self.recs()[0]["id"]
        # retries, refresh rescans and restarts all reconcile to one record
        for _ in range(3):
            conductor.discover_followups(
                self.runs, self.requests, self.manifests)
        self.assertEqual([r["id"] for r in self.recs()], [rid])
        self.assertEqual(len(list(self.runs.iterdir())), 1)   # no run dir
        rec = self.recs()[0]
        self.assertIsNone(conductor._request_run_dir(rec, self.runs))
        # the strongest guarantee: the spawn path was never touched
        src2 = self.followup_file("other")
        with mock.patch.object(conductor, "_launch_process") as lp:
            rec2 = conductor.import_followup_file(
                src2, self.requests, self.manifests, self.runs)
            lp.assert_not_called()
        self.assertEqual(rec2["status"], "ready")
        self.assertEqual(len(list(self.runs.iterdir())), 1)

    def test_rewrite_updates_same_request(self):
        self.run_dir("rest")
        src = self.followup_file("rest")
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        rid = self.recs()[0]["id"]
        src.write_text(self.proposal(
            "rest-followup", "Finish the leftover work v2"),
            encoding="utf-8")
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        recs = self.recs()
        self.assertEqual([r["id"] for r in recs], [rid])
        self.assertEqual(recs[0]["description"],
                         "Finish the leftover work v2")
        self.assertEqual(recs[0]["status"], "ready")

    def test_human_edit_to_owned_copy_beats_source_rewrite(self):
        self.run_dir("rest")
        src = self.followup_file("rest")
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        rec = self.recs()[0]
        owned = Path(rec["manifest"])
        owned.write_text(
            "# human review notes\n" + owned.read_text(encoding="utf-8"),
            encoding="utf-8")
        # a rewrite of the SOURCE must not clobber the edit: the owned
        # copy no longer matches the last imported sha
        src.write_text(self.proposal("rest-followup", "v3 rewrite"),
                       encoding="utf-8")
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        self.assertIn("# human review notes",
                      owned.read_text(encoding="utf-8"))
        self.assertEqual(len(self.recs()), 1)

    def test_malformed_file_is_error_not_runnable(self):
        self.run_dir("rest")
        self.followup_file("rest", text='[run\nname = "broken"\n[[step]\n')
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        rec = self.recs()[0]
        self.assertEqual(rec["status"], "error")
        self.assertIn("TOML", rec["error"])
        self.assertTrue(Path(rec["manifest"]).is_file())  # inspectable
        with self.assertRaises(conductor.RequestError):
            conductor.launch_request(rec["id"], self.requests,
                                     self.manifests, self.runs, ["devin"])

    def test_invalid_proposal_is_error_not_ready(self):
        self.run_dir("rest")
        self.followup_file("rest", text='''[run]
name = "rest-followup"
description = "x"
checkpoint = true

[[step]]
name = "s"
project = "no-such-slug"
prompt = "p"
''')
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        rec = self.recs()[0]
        self.assertEqual(rec["status"], "error")
        self.assertIn("no-such-slug", rec["error"])
        with self.assertRaises(conductor.RequestError):
            conductor.launch_request(rec["id"], self.requests,
                                     self.manifests, self.runs, ["devin"])

    def test_missing_output_lands_as_actionable_status(self):
        run = self.run_dir("rest")           # done step, nothing written
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        rec = self.recs()[0]
        self.assertEqual(rec["status"], "missing")
        self.assertIn("import", rec["error"])
        self.assertIsNone(rec["manifest"])
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        row = next(r for r in d["runs"]
                   if r.get("request_id") == rec["id"])
        self.assertEqual(row["stage"], "missing")
        st = json.loads((run / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["followup"]["status"], "missing")
        # ...and the file landing later resolves the SAME row
        self.followup_file("rest")
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        recs = self.recs()
        self.assertEqual([r["id"] for r in recs], [rec["id"]])
        self.assertEqual(recs[0]["status"], "ready")
        self.assertTrue(recs[0]["manifest"])

    def test_empty_file_means_no_work_remains(self):
        self.run_dir("rest")
        self.followup_file("rest", text='''# the run finished everything
[run]
name = "rest-followup"
description = "nothing remains"
''')
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        rec = self.recs()[0]
        self.assertEqual(rec["status"], "empty")
        self.assertIsNone(rec["manifest"])
        self.assertFalse(rec["proposed_steps"])
        d = conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                        self.requests)
        row = next(r for r in d["runs"]
                   if r.get("request_id") == rec["id"])
        self.assertEqual(row["stage"], "empty")
        with self.assertRaises(conductor.RequestError):
            conductor.launch_request(rec["id"], self.requests,
                                     self.manifests, self.runs, ["devin"])

    def test_empty_report_without_file_is_not_missing(self):
        self.run_dir("rest", steps={"follow-up-plan": {
            "status": "done", "ended_at": "2026-10-17T01:00:00",
            "outcome": "All work landed - no remaining work to propose"}})
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        rec = self.recs()[0]
        self.assertEqual(rec["status"], "empty")
        self.assertIsNone(rec["error"])

    def test_failed_step_marks_absent_without_record(self):
        run = self.run_dir("rest", steps={"follow-up-plan": {
            "status": "failed", "ended_at": "2026-10-17T01:00:00"}})
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        self.assertEqual(self.recs(), [])
        st = json.loads((run / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["followup"]["status"], "absent")

    def test_worktree_produced_file_registers(self):
        wt = self.base / "wt"
        wt_man = wt / "manifests"
        wt_man.mkdir(parents=True)
        src = self.followup_file("rest", manifests=wt_man)
        self.run_dir("rest", steps={"follow-up-plan": {
            "status": "done", "ended_at": "2026-10-17T01:00:00",
            "work_dir": str(wt)}})
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        rec = self.recs()[0]
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["source_manifest"], str(src))
        self.assertTrue(Path(rec["manifest"]).is_file())
        self.assertEqual(Path(rec["manifest"]).parent, self.manifests)

    def test_orphan_file_imports_without_run_link(self):
        self.followup_file("stray")          # no run dir at all
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        rec = self.recs()[0]
        self.assertEqual(rec["status"], "ready")
        self.assertIsNone(rec["source_run"])
        self.assertEqual(rec["run"], "stray-followup")

    def test_owned_copy_never_reimports(self):
        self.followup_file(
            "rest", manifests=self.manifests,
            text=self.proposal("rest-followup")).unlink()
        owned = self.manifests / ("20261017-010203-a1b2c3d4-"
                                "rest-followup.toml")
        owned.write_text(self.proposal("rest-followup"), encoding="utf-8")
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        self.assertEqual(self.recs(), [])

    def test_discard_vetoes_rescan_but_explicit_import_overrides(self):
        self.run_dir("rest")
        src = self.followup_file("rest")
        rec = conductor.import_followup_file(
            src, self.requests, self.manifests, self.runs)
        self.assertEqual(rec["status"], "ready")
        self.assertEqual(rec["source_run"], "20261017-000000-rest")
        conductor.discard_request(rec["id"], self.requests,
                                  self.manifests, self.runs)
        conductor.discover_followups(
            self.runs, self.requests, self.manifests)
        self.assertEqual(self.recs(), [])          # veto held
        rec2 = conductor.import_followup_file(
            src, self.requests, self.manifests, self.runs)
        self.assertEqual(rec2["status"], "ready")  # deliberate import wins
        self.assertNotEqual(rec2["id"], rec["id"])

    def test_run_end_registration_hook(self):
        """cmd_run registers the file its closing step was meant to write -
        the driver's own pass, not a later sweep."""
        m = write_manifest(self.base, f'''
[[step]]
name = "s1"
project = "{self.proj.as_posix()}"
prompt = "s1 work"

[[step]]
name = "follow-up-plan"
project = "{self.proj.as_posix()}"
timeout_min = 5
prompt = "the manifest was already written; say done"
''')
        self.followup_file("t")      # producer writes <run>-followup.toml
        with mock.patch.object(conductor, "MANIFESTS_DIR", self.manifests):
            rc = run_manifest(m, self.runs)
        self.assertEqual(rc, 0)
        recs = self.recs()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["status"], "ready")
        run = next(self.runs.iterdir())
        self.assertEqual(recs[0]["source_run"], run.name)
        st = json.loads((run / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["followup"]["status"], "registered")


class DeviceOwnershipTests(unittest.TestCase):
    """The `device` step key leases ONE adb serial machine-wide for a
    step's whole life: explicit serial assignment, cross-run ownership,
    queuing on a shared device, stale-owner recovery, AVD boot where
    tooling allows, and cleanup that only ever stops what we booted."""

    def _patches(self, devdir, devices, avds=None, poll=0.02):
        if callable(devices):
            adb_fn = devices
        else:
            adb_fn = (lambda: None if devices is None else list(devices))
        if callable(avds):
            avd_fn = avds
        else:
            avd_fn = (lambda: None if avds is None else list(avds))
        return (mock.patch.object(conductor, "adb_devices", adb_fn),
                mock.patch.object(conductor, "emulator_avds", avd_fn),
                mock.patch.object(conductor, "_device_dir",
                                  lambda: devdir),
                mock.patch.object(conductor, "DEVICE_POLL_S", poll),
                mock.patch.object(conductor, "DEVICE_WAIT_SAVE_S", 0.0))

    def _write_lease(self, devdir: Path, serial: str, **over):
        devdir.mkdir(exist_ok=True)
        info = {"serial": serial, "token": "foreign-token", "spec": "any",
                "run_dir": "C:/other-run/artifacts", "run_key": "other-00",
                "step": "foreign-step", "owner_pid": os.getpid(),
                # None skips the creation-window check in pid_alive -
                # os.getpid() predates the stamp by more than its window
                "owner_started_at": None,
                "worker_pid": None, "worker_started_at": None,
                "leased_at": conductor.now_iso(), "booted": None}
        info.update(over)
        (devdir / (conductor._safe_name(serial) + ".json")).write_text(
            json.dumps(info), encoding="utf-8")

    def _dead_pid(self) -> int:
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def _rec(self, runs: Path, step="mob") -> dict:
        st = json.loads((next(runs.iterdir()) / "state.json").read_text(
            encoding="utf-8"))
        return st["steps"][step]

    def _run_dir(self, runs: Path) -> Path:
        return next(runs.iterdir())

    def test_any_assigns_serial_and_propagates_env(self):
        """A `device` step gets an explicit serial before spawn: the lease
        is recorded on the step rec, ANDROID_SERIAL +
        CONDUCTOR_DEVICE_SERIAL reach the worker env, and the prompt
        carries the device note."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            patches = self._patches(
                devdir, [("emulator-5554", "device"),
                         ("RFGL11NN0QV", "device")])
            for p in patches:
                p.start()
            try:
                m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "do mobile things"
device = "any"
''')
                self.assertEqual(run_manifest(m, runs), 0)
            finally:
                for p in patches:
                    p.stop()
            rec = self._rec(runs)
            self.assertEqual(rec["status"], "done")
            # sorted() pick: 'RFGL11NN0QV' precedes 'emulator-5554'
            self.assertEqual(rec["device"], "RFGL11NN0QV")
            self.assertEqual(rec["device_spec"], "any")
            sess = json.loads(
                (proj / ".mock_sessions.json").read_text(encoding="utf-8"))
            self.assertEqual(sess[0]["android_serial"], "RFGL11NN0QV")
            self.assertEqual(sess[0]["device_serial"], "RFGL11NN0QV")
            prompt = (self._run_dir(runs) / "prompts/mob.md").read_text(
                encoding="utf-8")
            self.assertIn("owns Android device `RFGL11NN0QV`", prompt)
            # the note also points at the shipped relaunch/dialog helper
            self.assertIn("mobile_device.py", prompt)
            self.assertIn("Confirm Navigation", prompt)
            self.assertEqual(list(devdir.glob("*.json")), [])
            evs = (self._run_dir(runs) / "events.jsonl").read_text(
                encoding="utf-8")
            self.assertIn('"device_acquired"', evs)
            self.assertIn('"device_released"', evs)

    def test_explicit_serial_targets_named_device(self):
        """`device = "emulator-5554"` takes exactly that serial even when
        another device sorts first."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            patches = self._patches(
                devdir, [("emulator-5554", "device"),
                         ("RFGL11NN0QV", "device")])
            for p in patches:
                p.start()
            try:
                m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "on the emulator"
device = "emulator-5554"
''')
                self.assertEqual(run_manifest(m, runs), 0)
            finally:
                for p in patches:
                    p.stop()
            self.assertEqual(self._rec(runs)["device"], "emulator-5554")

    def test_competing_projects_queue_on_shared_device(self):
        """Two wave-1 steps on different projects, one attached device:
        the loser queues (device_wait recorded) until the winner releases,
        then takes the SAME serial - serialized, never concurrent."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            p1 = make_project(base, "p1")
            p2 = make_project(base, "p2")
            runs = base / "runs"
            devdir = base / "devleases"
            patches = self._patches(
                devdir, [("emulator-5554", "device")], avds=[])
            for p in patches:
                p.start()
            try:
                m = write_manifest(base, f'''
[[step]]
name = "hold"
project = "{p1.as_posix()}"
prompt = "sleep=2 slow device work"
wave = 1
device = "any"

[[step]]
name = "waiter"
project = "{p2.as_posix()}"
prompt = "quick device work"
wave = 1
device = "any"
''')
                self.assertEqual(run_manifest(m, runs), 0)
            finally:
                for p in patches:
                    p.stop()
            run_dir = self._run_dir(runs)
            st = json.loads((run_dir / "state.json").read_text(
                encoding="utf-8"))
            recs = st["steps"]
            self.assertEqual(recs["hold"]["device"], "emulator-5554")
            self.assertEqual(recs["waiter"]["device"], "emulator-5554")
            self.assertEqual(recs["hold"]["status"], "done")
            self.assertEqual(recs["waiter"]["status"], "done")
            waiter = next(r for r in recs.values() if r.get("device_wait"))
            holder = next(r for r in recs.values()
                          if not r.get("device_wait"))
            # Serialization invariant: the waiter's lease acquisition
            # event is never earlier than the holder's release event.
            # (ended_at lags release by seconds of merge/save work, so the
            # event stream is the honest clock.)
            evs = [json.loads(l) for l in (run_dir / "events.jsonl")
                   .read_text(encoding="utf-8").splitlines() if l.strip()]
            acq = next(e for e in evs
                       if e.get("event") == "device_acquired"
                       and e.get("step") == waiter["name"])
            rel = next(e for e in evs
                       if e.get("event") == "device_released"
                       and e.get("step") == holder["name"])
            self.assertGreaterEqual(
                conductor._iso_ts(acq["ts"]), conductor._iso_ts(rel["ts"]))
            self.assertIn('"device_wait"',
                          (run_dir / "events.jsonl").read_text(
                              encoding="utf-8"))

    def test_unavailable_device_blocks(self):
        """A device class that never appears ends 'blocked' after
        device_wait_s - it does not silently fall back to an unrelated
        device, and no session is ever spawned."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            patches = self._patches(
                devdir, [("emulator-5554", "device")], avds=[])
            for p in patches:
                p.start()
            try:
                m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "needs a real phone"
device = "physical"
device_wait_s = 0.3
''')
                self.assertEqual(run_manifest(m, runs), 1)
            finally:
                for p in patches:
                    p.stop()
            rec = self._rec(runs)
            self.assertEqual(rec["status"], "blocked")
            self.assertIn("physical", rec["note"])
            self.assertIn("no Android device", rec["note"])
            self.assertFalse((proj / ".mock_sessions.json").exists())
            evs = (self._run_dir(runs) / "events.jsonl").read_text(
                encoding="utf-8")
            self.assertIn('"device_wait"', evs)
            self.assertIn('"device_blocked"', evs)

    def test_adb_down_blocks_without_fallback(self):
        """adb unreachable (devices=None) is 'unknown', not 'none
        attached': the step waits then blocks, never spawning."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            patches = self._patches(devdir, None, avds=None)
            for p in patches:
                p.start()
            try:
                m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "needs adb"
device = "any"
device_wait_s = 0.3
''')
                self.assertEqual(run_manifest(m, runs), 1)
            finally:
                for p in patches:
                    p.stop()
            rec = self._rec(runs)
            self.assertEqual(rec["status"], "blocked")
            self.assertIn("no Android device matching 'any'",
                          rec["note"])

    def test_stale_owner_broken_then_leased(self):
        """A lease whose owner+worker pids are both dead is broken once
        and the serial is re-leased - a crashed run can't wedge a device
        for later runs."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            dead = self._dead_pid()
            self._write_lease(devdir, "emulator-5554", owner_pid=dead)
            patches = self._patches(
                devdir, [("emulator-5554", "device")], avds=[])
            for p in patches:
                p.start()
            try:
                m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "on the emu"
device = "any"
''')
                self.assertEqual(run_manifest(m, runs), 0)
            finally:
                for p in patches:
                    p.stop()
            self.assertEqual(self._rec(runs)["device"], "emulator-5554")
            self.assertEqual(list(devdir.glob("*.json")), [])

    def test_live_owner_queues_until_release(self):
        """Cross-run ownership: a lease written by ANOTHER live process
        (same shape a second conductor would write) blocks our step in
        'queued' until the file is released - no stealing, no collision."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            self._write_lease(devdir, "emulator-5554",
                              owner_pid=os.getpid())
            patches = self._patches(
                devdir, [("emulator-5554", "device")], avds=[])
            for p in patches:
                p.start()
            try:
                m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "waits politely"
device = "any"
''')
                box = {}
                th = threading.Thread(
                    target=lambda: box.setdefault(
                        "rc", run_manifest(m, runs)), daemon=True)
                th.start()
                deadline = time.time() + 15
                rec = None
                while time.time() < deadline:
                    dirs = list(runs.glob("*/state.json"))
                    if dirs:
                        rec = json.loads(dirs[0].read_text(
                            encoding="utf-8"))["steps"].get("mob") or {}
                        if rec.get("device_wait"):
                            break
                    time.sleep(0.05)
                self.assertTrue(rec and rec.get("device_wait"),
                                f"step never queued for device: {rec}")
                self.assertEqual(rec["status"], "queued")
                self.assertFalse((proj / ".mock_sessions.json").exists())
                lease = devdir / (conductor._safe_name("emulator-5554")
                                  + ".json")
                lease.unlink()          # foreign owner released
                th.join(timeout=30)
                self.assertEqual(box.get("rc"), 0)
            finally:
                for p in patches:
                    p.stop()
                th.join(timeout=30)
            self.assertEqual(self._rec(runs)["device"], "emulator-5554")

    def test_stop_during_device_wait_skips(self):
        """A Stop raised while a step queues on a device ends it
        'skipped' - it never spawns a worker and never takes a serial."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            self._write_lease(devdir, "emulator-5554",
                              owner_pid=os.getpid())
            patches = self._patches(
                devdir, [("emulator-5554", "device")], avds=[])
            for p in patches:
                p.start()
            try:
                m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "never runs"
device = "any"
''')
                box = {}
                th = threading.Thread(
                    target=lambda: box.setdefault(
                        "rc", run_manifest(m, runs)), daemon=True)
                th.start()
                run_dir = None
                deadline = time.time() + 15
                while time.time() < deadline:
                    dirs = list(runs.glob("*/state.json"))
                    if dirs:
                        rec = json.loads(dirs[0].read_text(
                            encoding="utf-8"))["steps"].get("mob") or {}
                        if rec.get("device_wait"):
                            run_dir = dirs[0].parent
                            break
                    time.sleep(0.05)
                self.assertIsNotNone(run_dir)
                conductor._flag_raise(conductor.stop_flag(run_dir).name)
                th.join(timeout=30)
                self.assertEqual(box.get("rc"), 1)
            finally:
                for p in patches:
                    p.stop()
                th.join(timeout=30)
                if run_dir:
                    conductor.stop_flag(run_dir).unlink(missing_ok=True)
                (devdir / (conductor._safe_name("emulator-5554")
                           + ".json")).unlink(missing_ok=True)
            rec = self._rec(runs)
            self.assertEqual(rec["status"], "skipped")
            self.assertIn("while waiting for a device",
                          rec.get("note") or "")
            self.assertFalse((proj / ".mock_sessions.json").exists())
            self.assertIsNone(rec.get("device"))

    def test_emulator_boot_and_release_kills_only_ours(self):
        """`device = "emulator"` boots a free AVD on a free console port
        when nothing is attached, leases the new serial, and kills exactly
        that instance on release - never a device it didn't create."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"

            class FakeEmu:
                pid = 43210

                def poll(self):
                    return None

            spawned = []
            kills = []
            patches = self._patches(devdir, [], avds=["AvdA", "AvdB"]) \
                + (mock.patch.object(
                    conductor, "_spawn_emulator",
                    lambda avd, port, log: spawned.append(
                        (avd, port)) or FakeEmu()),
                   mock.patch.object(conductor, "_wait_emu_boot",
                                     lambda s, p, timeout=180: True),
                   mock.patch.object(
                       conductor, "_device_kill_emu",
                       lambda s, p, rd: kills.append((s, p))))
            for p in patches:
                p.start()
            try:
                m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "on a fresh emu"
device = "emulator"
''')
                self.assertEqual(run_manifest(m, runs), 0)
            finally:
                for p in patches:
                    p.stop()
            self.assertEqual(spawned, [("AvdA", 5554)])
            rec = self._rec(runs)
            self.assertEqual(rec["device"], "emulator-5554")
            self.assertEqual(rec["device_booted"], "AvdA")
            # the release killed exactly the instance we booted, and every
            # lease file (serial + avd slot) is gone
            self.assertEqual(kills, [("emulator-5554", 43210)])
            self.assertEqual(list(devdir.glob("*.json")), [])
            evs = (self._run_dir(runs) / "events.jsonl").read_text(
                encoding="utf-8")
            self.assertIn('"device_booted"', evs)

    def test_stale_booted_emu_reaped_with_image_guard(self):
        """Stale-lease cleanup only kills a pid that still looks like an
        emulator - a recycled pid running something else is left alone."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            sleeper = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(60)"])
            self.addCleanup(lambda: sleeper.kill())
            kills = []
            patches = self._patches(
                devdir, [("emulator-5554", "device")], avds=[]) \
                + (mock.patch.object(conductor, "_proc_image_name",
                                     lambda pid: "python.exe"),
                   mock.patch.object(
                       conductor, "_device_kill_emu",
                       lambda s, p, rd: kills.append((s, p))))
            for p in patches:
                p.start()
            try:
                self._write_lease(
                    devdir, "emulator-5554", owner_pid=self._dead_pid(),
                    booted={"avd": "Old", "port": 5554,
                            "emu_pid": sleeper.pid})
                m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "on the emu"
device = "any"
''')
                self.assertEqual(run_manifest(m, runs), 0)
            finally:
                for p in patches:
                    p.stop()
            # 'python.exe' is NOT an emulator image - the stale pid was
            # deliberately spared even though the lease claimed we booted it
            self.assertEqual(kills, [])
            self.assertIsNone(sleeper.poll())
            self.assertEqual(self._rec(runs)["device"], "emulator-5554")

    def test_stale_booted_emu_reaped_when_image_matches(self):
        """...and when the recorded pid IS an emulator image, breaking the
        stale lease kills that one orphaned instance (by serial + pid,
        never a blanket kill)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            kills = []
            patches = self._patches(
                devdir, [("emulator-5554", "device")], avds=[]) \
                + (mock.patch.object(conductor, "_proc_image_name",
                                     lambda pid: "qemu-system-x86_64"),
                   mock.patch.object(
                       conductor, "_device_kill_emu",
                       lambda s, p, rd: kills.append((s, p))))
            for p in patches:
                p.start()
            try:
                dead = self._dead_pid()
                self._write_lease(
                    devdir, "emulator-5554", owner_pid=dead,
                    booted={"avd": "Old", "port": 5554, "emu_pid": 55555})
                with mock.patch.object(conductor, "pid_alive",
                                       lambda pid, since=None:
                                           pid == 55555):
                    m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "on the emu"
device = "any"
''')
                    self.assertEqual(run_manifest(m, runs), 0)
            finally:
                for p in patches:
                    p.stop()
            self.assertEqual(kills, [("emulator-5554", 55555)])
            self.assertEqual(self._rec(runs)["device"], "emulator-5554")

    def test_verify_gate_inherits_serial(self):
        """The verify command runs inside the lease too - a device-side
        gate sees ANDROID_SERIAL just like the worker."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            patches = self._patches(
                devdir, [("emulator-5554", "device")], avds=[])
            for p in patches:
                p.start()
            try:
                m = write_manifest(
                    base,
                    '[[step]]\nname = "mob"\n'
                    f'project = "{proj.as_posix()}"\n'
                    'prompt = "v"\n'
                    'device = "any"\n'
                    'verify = "python -c \\"import os;'
                    'assert os.environ.get(\'ANDROID_SERIAL\')'
                    '==\'emulator-5554\'\\""\n')
                self.assertEqual(run_manifest(m, runs), 0)
            finally:
                for p in patches:
                    p.stop()
            self.assertTrue(self._rec(runs)["verify_ok"])

    def test_device_manifest_validation(self):
        """device+cloud is a hard manifest error (a cloud worker can't see
        local adb), bad serials are rejected, and the project registry can
        carry a slug-level `device` default like `verify`."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            bad = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "x"
cloud = true
device = "any"
''')
            with self.assertRaises(conductor.ManifestError):
                conductor.load_manifest(bad)
            bad2 = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "x"
device = "not a serial!"
''')
            with self.assertRaises(conductor.ManifestError):
                conductor.load_manifest(bad2)
            ok = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "x"
device = true
''')
            self.assertEqual(
                conductor.load_manifest(ok).steps[0].device, "any")
            pf = base / "projects.toml"
            pf.write_text(
                f'[project.mob]\npath = "{proj.as_posix()}"\n'
                'cloud_ok = false\ndevice = "emulator"\n'
                'device_wait_s = 5\n', encoding="utf-8")
            with mock.patch.object(conductor, "PROJECTS_FILE", pf):
                inh = write_manifest(base, '''
[[step]]
name = "mob"
project = "mob"
prompt = "x"
''')
                st = conductor.load_manifest(inh).steps[0]
                self.assertEqual(st.device, "emulator")
                self.assertEqual(st.device_wait_s, 5)
                # a cloud step does not inherit (local serials are
                # meaningless on the VM) - but doesn't error either
                inh_cloud = write_manifest(base, '''
[[step]]
name = "mob"
project = "mob"
prompt = "x"
cloud = true
''')
                st = conductor.load_manifest(inh_cloud).steps[0]
                self.assertIsNone(st.device)

    def test_no_device_step_unaffected(self):
        """A step without `device` spawns as before: no serial env, no
        lease files, device fields stay null."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            devdir = base / "devleases"
            patches = self._patches(
                devdir, [("emulator-5554", "device")])
            for p in patches:
                p.start()
            try:
                m = write_manifest(base, f'''
[[step]]
name = "mob"
project = "{proj.as_posix()}"
prompt = "plain step"
''')
                self.assertEqual(run_manifest(m, runs), 0)
            finally:
                for p in patches:
                    p.stop()
            rec = self._rec(runs)
            self.assertIsNone(rec.get("device"))
            sess = json.loads(
                (proj / ".mock_sessions.json").read_text(encoding="utf-8"))
            self.assertIsNone(sess[0]["android_serial"])
            self.assertFalse(devdir.exists()
                             and list(devdir.glob("*.json")))


class ExecutionContextLabelTests(unittest.TestCase):
    """Runs-table 'wt' chip (configured worktree isolation) + the
    workspace-watcher 409 naming its owning agent.

    run['iso'] is {wt,local,cloud} counts over the run's CONFIGURED steps -
    the parsed manifest is authoritative (load_manifest folds
    merge='deferred' into worktree and rejects worktree+cloud), the
    request's proposed_steps snapshot and per-record 'wt' (stamped at
    queue/dispatch) are fallbacks. Branch names and liveness never feed
    it, so a run whose steps haven't spawned still reports its scheduled
    isolation and a stray branch on a legacy run invents nothing.

    _assert_unwatched still guards every request/manifest/trash write; the
    refusal now names each watching step as '<run-dir>:<step>' plus slug
    and a short session id when recorded, lists up to three and counts
    the rest."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.proj = make_project(self.base)
        self.runs = self.base / "runs"
        self.requests = self.base / "requests"
        self.requests.mkdir()
        patch = mock.patch.object(conductor, "load_projects", return_value={
            "proj": {"path": self.proj, "cloud_ok": False}})
        patch.start()
        self.addCleanup(patch.stop)

    def manifest(self, steps: str, name="cfg") -> Path:
        m = self.base / f"{name}.toml"
        m.write_text(f'[run]\nname = "{name}"\n{steps}', encoding="utf-8")
        return m

    def write_run(self, dirname, manifest=None, steps=None, **extra):
        d = self.runs / dirname
        d.mkdir(parents=True, exist_ok=True)
        st = {"run": dirname, "steps": steps or {}}
        if manifest is not None:
            st["manifest"] = str(manifest)
        st.update(extra)
        (d / "state.json").write_text(json.dumps(st), encoding="utf-8")
        return d

    def live_step(self, work_dir: Path, **rec) -> dict:
        # no pid + fresh started_at = the no-pid live shape (a self-hosted
        # record mid-run): step_live stays true without probing a process
        return {"status": "running", "work_dir": str(work_dir),
                "started_at": conductor.now_iso(), "timeout_min": 120,
                **rec}

    def dash(self):
        return conductor.collect_dashboard(self.runs, ["no-such-devin"],
                                           self.requests)

    # ------------------------------------------------------------- iso

    def test_iso_from_manifest_before_any_dispatch(self):
        """Nothing has run (no step records at all) and the label still
        reports the scheduled isolation - config, not liveness."""
        m = self.manifest('''
[[step]]
name = "a"
project = "proj"
prompt = "x"
worktree = true

[[step]]
name = "b"
project = "proj"
prompt = "y"
worktree = true
''')
        self.write_run("20261016-120000-allwt", manifest=m)
        self.assertEqual(self.dash()["runs"][0]["iso"],
                         {"wt": 2, "local": 2, "cloud": 0})

    def test_iso_mixed_and_deferred_counted(self):
        m = self.manifest('''
[[step]]
name = "a"
project = "proj"
prompt = "x"
worktree = true

[[step]]
name = "b"
project = "proj"
prompt = "y"
merge = "deferred"

[[step]]
name = "c"
project = "proj"
prompt = "z"
''')
        self.write_run("20261016-120100-mixed", manifest=m)
        # merge='deferred' implies worktree at parse time -> wt=2 of 3
        self.assertEqual(self.dash()["runs"][0]["iso"],
                         {"wt": 2, "local": 3, "cloud": 0})

    def test_iso_absent_for_plain_and_cloud_only_runs(self):
        m = self.manifest('''
[[step]]
name = "a"
project = "proj"
prompt = "x"

[[step]]
name = "b"
project = "proj"
prompt = "y"
cloud = true
''')
        self.write_run("20261016-120200-plain", manifest=m)
        self.assertIsNone(self.dash()["runs"][0]["iso"])

    def test_iso_falls_back_to_proposed_steps_then_records(self):
        gone = self.base / "gone.toml"          # recorded path, file deleted
        # request-launched run: the proposal snapshot carries the config
        rid = "20261016-121212-abcd1234"
        (self.requests / f"{rid}.json").write_text(json.dumps({
            "id": rid, "status": "launched", "run": "snap",
            "proposed_steps": [
                {"step": "a", "slug": "proj", "wt": True, "cloud": False},
                {"step": "b", "slug": "proj", "wt": False,
                 "cloud": False}]}), encoding="utf-8")
        self.write_run("20261016-120300-snap", manifest=gone,
                       request_id=rid,
                       steps={"a": {"status": "done",
                                    "ended_at": "2026-10-16T12:30:00"}})
        # CLI run, new-code records carry 'wt' even without the file
        self.write_run("20261016-120400-recrec", manifest=gone,
                       steps={"a": {"status": "done", "wt": True,
                                    "ended_at": "2026-10-16T12:30:00"}})
        iso = {r["run"]: r["iso"] for r in self.dash()["runs"]}
        self.assertEqual(iso["20261016-120300-snap"],
                         {"wt": 1, "local": 2, "cloud": 0})
        self.assertEqual(iso["20261016-120400-recrec"],
                         {"wt": 1, "local": 1, "cloud": 0})

    def test_iso_never_reads_branch_names(self):
        """Legacy records have no 'wt' key - a parked conductor branch is
        evidence a worktree EXISTED, not that the config scheduled one,
        so the run stays unlabeled rather than guessing."""
        self.write_run(
            "20261016-120500-legacy", manifest=self.base / "gone.toml",
            steps={"a": {"status": "done",
                         "branch": "conductor/legacy/a",
                         "ended_at": "2026-10-16T12:30:00"}})
        self.assertIsNone(self.dash()["runs"][0]["iso"])

    def test_iso_on_unlaunched_request_row(self):
        rid = "20261016-122222-feedbeef"
        m = self.manifest('''
[[step]]
name = "a"
project = "proj"
prompt = "x"
worktree = true
''', name="plan")
        (self.requests / f"{rid}.json").write_text(json.dumps({
            "id": rid, "status": "ready", "run": "plan",
            "manifest": str(m),
            "proposed_steps": [
                {"step": "a", "slug": "proj", "wt": True,
                 "cloud": False}]}), encoding="utf-8")
        self.assertEqual(self.dash()["runs"][0]["iso"],
                         {"wt": 1, "local": 1, "cloud": 0})

    def test_dispatch_records_wt_config(self):
        m = self.manifest(f'''
[[step]]
name = "iso"
project = "{self.proj.as_posix()}"
prompt = "work"
worktree = true

[[step]]
name = "plain"
project = "{self.proj.as_posix()}"
prompt = "work"
''')
        self.assertEqual(run_manifest(m, self.runs), 0)
        st = json.loads((next(self.runs.iterdir()) /
                         "state.json").read_text())
        self.assertIs(st["steps"]["iso"]["wt"], True)
        self.assertIs(st["steps"]["plain"]["wt"], False)

    # ------------------------------------------------------ wt chip (js)

    @unittest.skipUnless(shutil.which("node"), "Node unavailable")
    def test_run_row_wt_chip_render(self):
        js = conductor._DASH_HTML.split("<script>\n", 1)[1].split(
            "</script>", 1)[0]
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const src=require('node:fs').readFileSync(0,'utf8').replace(/\ntick\(\);\s*$/, '');
const nodes=new Map();
const elem=id=>{ if(!nodes.has(id)) nodes.set(id,{innerHTML:'',hidden:true}); return nodes.get(id); };
const document={getElementById:elem,querySelector:()=>null,
  querySelectorAll:()=>[],addEventListener:()=>{},
  body:{classList:{toggle:()=>{},contains:()=>false}},
  documentElement:{style:{setProperty:(k,v)=>{}}}};
const ctx=vm.createContext({document,location:{reload:()=>{}},setTimeout:()=>{}});
vm.runInContext(src,ctx);
const data={serve_boot:'b',generated_at:'2026-10-16T00:00:00',agents:[],
runs:[
 {run:'allwt',dir:'/a',counts:{done:2},ended_at:'2026-10-16T01:00:00',
  iso:{wt:2,local:2,cloud:0}},
 {run:'mixed',dir:'/m',counts:{done:3},ended_at:'2026-10-16T01:00:00',
  iso:{wt:1,local:2,cloud:1}},
 {run:'plain',dir:'/p',counts:{done:1},ended_at:'2026-10-16T01:00:00'},
 {run:'req',key:'request:r1',request_id:'r1',stage:'ready',
  dir:'/m.toml',counts:{planned:1},iso:{wt:1,local:1,cloud:0}}]};
vm.runInContext('render('+JSON.stringify(data)+')',ctx);
const row=name=>elem('runs').innerHTML.split('runrow')
  .find(r=>r.includes('>'+name+'<'));
// all-local-worktree run: compact 'wt' inside .rlabels, explanatory title
const a=row('allwt');
assert.match(a,/class="rlabels">[^]*?class="stage wt"[^>]*>wt</);
assert.match(a,/title="worktree isolation: all 2 local step\(s\) run in git worktrees"/);
// mixed isolation: the count is IN the label, not tooltip-only
const m=row('mixed');
assert.match(m,/class="stage wt"[^>]*>wt 1\/2</);
assert.match(m,/1 of 2 local steps run in git worktrees/);
assert.match(m,/the rest write in the project checkout/);
assert.match(m,/1 step\(s\) run in the cloud/);
// no configured worktrees: no chip at all
assert.doesNotMatch(row('plain'),/stage wt/);
// unlaunched proposal rows carry the label too - it is config, not runtime
assert.match(row('req'),/class="stage wt"[^>]*>wt</);
'''
        p = subprocess.run(["node", "-e", harness], input=js,
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)

    # -------------------------------------------------- watcher warning

    def test_watcher_warning_reachable_and_names_owner(self):
        """The guard is still reachable: every managed dir lives under
        ROOT, so a live main-checkout step watches them all. The 409 now
        names run:step, slug and the short session id from state.json -
        the authoritative ownership record."""
        watched = self.base / "repo"          # the agent's checkout
        watched.mkdir()
        self.write_run("20261016-130000-selfhost", steps={
            "fix-parser": self.live_step(
                watched, slug="repo",
                session_id="abcdef0123456789")})
        dest = watched / "data" / "requests"
        with self.assertRaises(conductor.RequestError) as cm:
            conductor._assert_unwatched(self.runs, dest)
        exc = cm.exception
        self.assertEqual(exc.status, 409)
        msg = str(exc)
        self.assertIn("live agent is watching this workspace", msg)
        self.assertIn("20261016-130000-selfhost:fix-parser", msg)
        self.assertIn("repo", msg)
        self.assertIn("session abcdef01", msg)

    def test_watcher_warning_surfaces_through_create_request(self):
        watched = self.base / "repo"
        watched.mkdir()
        req_dir = watched / "data" / "requests"
        self.write_run("20261016-130100-own", steps={
            "hold": self.live_step(watched)})
        with self.assertRaises(conductor.RequestError) as cm:
            conductor.create_request({"raw": "anything"},
                                     req_dir, self.runs)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("20261016-130100-own:hold", str(cm.exception))
        self.assertFalse(req_dir.exists())   # nothing was written

    def test_watcher_warning_lists_multiple_and_caps(self):
        watched = self.base / "repo"
        watched.mkdir()
        for i in range(5):
            self.write_run(f"20261016-130{i}00-w{i}", steps={
                f"s{i}": self.live_step(
                    watched, session_id=f"{i:016x}")})
        with self.assertRaises(conductor.RequestError) as cm:
            conductor._assert_unwatched(self.runs, watched / "manifests")
        msg = str(cm.exception)
        self.assertIn(":s0", msg)
        self.assertIn(":s2", msg)
        self.assertIn("+2 more", msg)
        self.assertNotIn(":s4", msg)         # past the cap

    def test_watcher_warning_missing_identity_degrades(self):
        """Old records may lack slug/session_id - the label still carries
        run-dir:step and never prints placeholder 'None'/'session'."""
        watched = self.base / "repo"
        watched.mkdir()
        self.write_run("20261016-131000-bare", steps={
            "only": self.live_step(watched)})
        with self.assertRaises(conductor.RequestError) as cm:
            conductor._assert_unwatched(self.runs, watched / "manifests")
        msg = str(cm.exception)
        self.assertIn("20261016-131000-bare:only", msg)
        self.assertNotIn("None", msg)
        self.assertNotIn("session", msg)

    def test_watcher_warning_ignores_dead_and_foreign(self):
        watched = self.base / "repo"
        watched.mkdir()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.write_run("20261016-131100-done", steps={
            "fin": {"status": "done", "work_dir": str(watched),
                    "ended_at": "2026-10-16T13:20:00"},
            "other": self.live_step(elsewhere)})
        conductor._assert_unwatched(
            self.runs, watched / "manifests")     # must not raise


if __name__ == "__main__":
    unittest.main()
