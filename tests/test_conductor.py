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
            self.assertEqual(
                (proj / "WORK.txt").read_text().splitlines(),
                ["mock-step-1", "mock-step-2"])
            run_dir = next(runs.iterdir())
            state = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(state["steps"]["one"]["status"], "done")
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


if __name__ == "__main__":
    unittest.main()
