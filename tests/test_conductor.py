"""Tests for conductor.py. Convention: C:\\Projects\\TESTING.md

Everything here runs against tests/mock_devin.py - no auth, no credits.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
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
    worker process is alive."""

    def _fixture(self, base: Path):
        proj = make_project(base)
        run_dir = base / "runs" / "r1"
        (run_dir / "prompts").mkdir(parents=True)
        cfg = conductor.load_manifest(write_manifest(base, f'''
[[step]]
name = "a"
project = "{proj.as_posix()}"
prompt = "p"
'''))
        state = {"run": "t", "run_dir": str(run_dir), "steps": {}}
        return proj, run_dir, cfg, state

    def test_pid_not_persisted_while_worker_alive(self):
        with tempfile.TemporaryDirectory() as td:
            proj, run_dir, cfg, state = self._fixture(Path(td))

            class FakeProc:
                pid = 4242

                def __init__(self, *a, **kw):
                    self._out = kw["stdout"]

                def wait(self, timeout=None):
                    disk = json.loads(
                        (run_dir / "state.json").read_text())
                    self._out.write(b"STATUS: done\n")
                    self._out.flush()
                    # while the child is alive the on-disk state must not
                    # have been rewritten with the pid
                    assert disk["steps"]["a"].get("pid") is None
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
