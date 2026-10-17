"""Worker service ports + worktree context propagation.

Each dispatched LOCAL step leases one loopback port from a registry-free
range for its whole lifetime (lease files under the machine temp dir,
O_EXCL claims, bind probes as the OS truth). The worker learns its real
checkout, the shared repo root, and the leased port through prompt text
plus CONDUCTOR_* env vars; the lease is released - and probed for a leaked
listener - when the step ends.

Everything runs against tests/mock_devin.py - no auth, no credits. The
mock echoes the CONDUCTOR_* env it inherits so tests can assert the launch
env from the step's stdout log.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))   # sibling helper module test_conductor

import conductor  # noqa: E402
from test_conductor import MOCK, git, make_project, write_manifest, \
    run_manifest  # noqa: E402


def state_of(runs_dir: Path) -> tuple[dict, Path]:
    rd = next(runs_dir.iterdir())
    return json.loads((rd / "state.json").read_text()), rd


def _dead_pid() -> int:
    """A pid that is definitely not alive right now."""
    p = subprocess.Popen([sys.executable, "-c", "pass"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.wait()
    return p.pid


def _write_lease(lease_dir: Path, port: int, owner: dict,
                 age_s: float = 0) -> Path:
    """Write a lease file; age_s backdates its mtime past the claim-grace
    window so _lease_owner_alive evaluates its contents instead of
    treating it as a claim in flight."""
    f = lease_dir / f"svc-{port}.json"
    f.write_text(json.dumps(owner), encoding="utf-8")
    if age_s:
        old = time.time() - age_s
        os.utime(f, (old, old))
    return f


def _env_line(stdout_log: Path) -> str:
    """The mock's echoed CONDUCTOR_* env line, or ''."""
    try:
        for ln in stdout_log.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            if ln.startswith("CONDUCTOR_"):
                return ln
    except OSError:
        pass
    return ""


def _env_val(line: str, key: str) -> str | None:
    m = {kv.split("=", 1)[0]: kv.split("=", 1)[1]
         for kv in line.split() if "=" in kv}
    return m.get(key)


class ServicePortLeaseTests(unittest.TestCase):
    """Unit-level allocator behavior: range, distinctness, contention,
    staleness, release."""

    def test_alloc_returns_bindable_port_and_writes_lease(self):
        with tempfile.TemporaryDirectory() as td:
            ld = Path(td)
            port = conductor.alloc_service_port(
                {"run": "r", "step": "s", "work_dir": "C:/x"}, ld)
            self.assertIsNotNone(port)
            self.assertLessEqual(conductor.SERVICE_PORT_MIN, port)
            self.assertLessEqual(port, conductor.SERVICE_PORT_MAX)
            # The lease records owner + caller-supplied context.
            info = json.loads(
                (ld / f"svc-{port}.json").read_text(encoding="utf-8"))
            self.assertEqual(info["pid"], os.getpid())
            self.assertEqual(info["step"], "s")
            self.assertEqual(info["work_dir"], "C:/x")
            self.assertTrue(info.get("created_at"))
            # The allocator does not hold the port itself - the worker can
            # actually bind it.
            self.assertTrue(conductor._port_free(port))

    def test_concurrent_allocations_never_collide(self):
        with tempfile.TemporaryDirectory() as td:
            ld = Path(td)
            got: list[int] = []
            lock = threading.Lock()

            def one(i):
                p = conductor.alloc_service_port({"step": f"s{i}"}, ld)
                with lock:
                    got.append(p)

            ts = [threading.Thread(target=one, args=(i,))
                  for i in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(len(got), 8)
            self.assertEqual(len(set(got)), 8)   # no port leased twice

    def test_bound_port_is_skipped_not_taken_over(self):
        with tempfile.TemporaryDirectory() as td:
            ld = Path(td)
            held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            held.bind(("127.0.0.1", conductor.SERVICE_PORT_MIN))
            held.listen(1)
            try:
                port = conductor.alloc_service_port({}, ld)
            finally:
                held.close()
            self.assertIsNotNone(port)
            self.assertNotEqual(port, conductor.SERVICE_PORT_MIN)
            # No lease lingers on the port we could not have.
            self.assertFalse(
                (ld / f"svc-{conductor.SERVICE_PORT_MIN}.json").is_file())

    def test_live_lease_is_respected(self):
        with tempfile.TemporaryDirectory() as td:
            ld = Path(td)
            first = conductor.alloc_service_port({"step": "a"}, ld)
            second = conductor.alloc_service_port({"step": "b"}, ld)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second)

    def test_dead_owner_lease_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as td:
            ld = Path(td)
            port = conductor.SERVICE_PORT_MIN
            _write_lease(ld, port,
                         {"pid": _dead_pid(),
                          "pid_created": "2020-01-01T00:00:00",
                          "step": "ghost"}, age_s=3600)
            got = conductor.alloc_service_port({"step": "new"}, ld)
            self.assertEqual(got, port)
            info = json.loads(
                (ld / f"svc-{port}.json").read_text(encoding="utf-8"))
            self.assertEqual(info["step"], "new")
            self.assertEqual(info["pid"], os.getpid())

    def test_fresh_unparseable_lease_is_a_claim_in_flight(self):
        """An empty lease file that is seconds old is a concurrent
        allocator mid-claim - skip it, don't delete-then-double-lease."""
        with tempfile.TemporaryDirectory() as td:
            ld = Path(td)
            claim = ld / f"svc-{conductor.SERVICE_PORT_MIN}.json"
            claim.write_bytes(b"")   # O_EXCL'd, content not yet written
            got = conductor.alloc_service_port({"step": "x"}, ld)
            self.assertEqual(got, conductor.SERVICE_PORT_MIN + 1)
            self.assertTrue(claim.is_file())          # left alone

    def test_old_unparseable_lease_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as td:
            ld = Path(td)
            claim = ld / f"svc-{conductor.SERVICE_PORT_MIN}.json"
            claim.write_bytes(b"not json")
            old = time.time() - 3600
            os.utime(claim, (old, old))
            got = conductor.alloc_service_port({"step": "x"}, ld)
            self.assertEqual(got, conductor.SERVICE_PORT_MIN)

    def test_release_drops_lease_and_reports_still_bound(self):
        with tempfile.TemporaryDirectory() as td:
            ld = Path(td)
            port = conductor.alloc_service_port({"step": "s"}, ld)
            # Simulate a worker that leaked its dev server: something still
            # holds the leased port at release time.
            held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            held.bind(("127.0.0.1", port))
            held.listen(1)
            try:
                self.assertFalse(
                    conductor.release_service_port(port, ld))
                self.assertFalse((ld / f"svc-{port}.json").is_file())
            finally:
                held.close()
            # Free again -> True; no lease -> still True (idempotent).
            self.assertTrue(conductor.release_service_port(port, ld))
            self.assertIsNone(conductor.release_service_port(None, ld))

    def test_sweep_drops_dead_leases_keeps_live(self):
        with tempfile.TemporaryDirectory() as td:
            ld = Path(td)
            live = conductor.alloc_service_port({"step": "live"}, ld)
            dead = _write_lease(
                ld, conductor.SERVICE_PORT_MAX,
                {"pid": _dead_pid(),
                 "pid_created": "2020-01-01T00:00:00"}, age_s=3600)
            lines = conductor.sweep_service_ports(ld)
            self.assertFalse(dead.is_file())
            self.assertTrue((ld / f"svc-{live}.json").is_file())
            self.assertTrue(any("reclaimed" in ln for ln in lines))

    def test_env_service_port_parsing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONDUCTOR_SERVICE_PORT", None)
            self.assertIsNone(conductor.env_service_port())
            os.environ["CONDUCTOR_SERVICE_PORT"] = "8899"
            self.assertEqual(conductor.env_service_port(), 8899)
            os.environ["CONDUCTOR_SERVICE_PORT"] = "nope"
            self.assertIsNone(conductor.env_service_port())
            os.environ["CONDUCTOR_SERVICE_PORT"] = "99999"
            self.assertIsNone(conductor.env_service_port())


class WorkerContextE2ETests(unittest.TestCase):
    """run_manifest-level: the prompt and the spawned process env carry the
    checkout/repo/port context; state.json retains it for review; the
    lease is dropped at step end."""

    def _lease_dir(self, base: Path):
        return mock.patch.object(conductor, "_port_lease_dir",
                                 lambda: base / "leases")

    def test_step_leases_port_into_prompt_env_and_state(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "s"
project = "{proj.as_posix()}"
prompt = "write A.txt"
''')
            with self._lease_dir(base):
                self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            rec = st["steps"]["s"]
            self.assertEqual(rec["status"], "done")
            port = rec["service_port"]
            self.assertIsInstance(port, int)
            self.assertEqual(rec["service_url"],
                             f"http://127.0.0.1:{port}")
            # prompt text + worker env both carry the lease.
            prompt = (rd / "prompts" / "s.md").read_text(encoding="utf-8")
            self.assertIn(f"127.0.0.1:{port}", prompt)
            env = _env_line(rd / "s.stdout.log")
            self.assertEqual(_env_val(env, "CONDUCTOR_SERVICE_PORT"),
                             str(port))
            self.assertEqual(_env_val(env, "CONDUCTOR_SERVICE_URL"),
                             f"http://127.0.0.1:{port}")
            self.assertEqual(
                _env_val(env, "CONDUCTOR_WORK_DIR"), str(proj.resolve()))
            self.assertEqual(
                _env_val(env, "CONDUCTOR_REPO_DIR"), str(proj.resolve()))
            self.assertEqual(_env_val(env, "CONDUCTOR_STEP"), "s")
            # The lease is released when the step ends.
            self.assertFalse(
                (base / "leases" / f"svc-{port}.json").exists())
            # A plain (non-worktree) step does not claim worktree context:
            # no "isolated git worktree" note in its prompt.
            self.assertNotIn("isolated", prompt.lower()
                             .split("## Task")[0])

    def test_worktree_wave_members_get_distinct_ports_and_checkouts(self):
        """Two overlapping worktree workers on the same project: distinct
        leased ports, each told its own worktree path + the shared repo,
        and the env carries the step branch."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, """
[[step]]
name = "w1"
project = "%s"
prompt = "sleep=1 write A.txt"
wave = 1
worktree = true

[[step]]
name = "w2"
project = "%s"
prompt = "sleep=1 write B.txt"
wave = 1
worktree = true
""" % (proj.as_posix(), proj.as_posix()))
            with self._lease_dir(base):
                self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            p1 = st["steps"]["w1"]["service_port"]
            p2 = st["steps"]["w2"]["service_port"]
            self.assertIsNotNone(p1)
            self.assertIsNotNone(p2)
            self.assertNotEqual(p1, p2)
            for name in ("w1", "w2"):
                rec = st["steps"][name]
                self.assertEqual(rec["status"], "done")
                self.assertTrue(rec["merged"])
                wt = rec["work_dir"]
                self.assertIn("worktrees", wt)
                prompt = (rd / "prompts" / f"{name}.md").read_text(
                    encoding="utf-8")
                # The prompt names the isolated checkout AND the shared
                # repo it must not touch.
                self.assertIn(wt, prompt)
                self.assertIn("isolated", prompt.lower())
                self.assertIn(str(proj.resolve()), prompt)
                env = _env_line(rd / f"{name}.stdout.log")
                self.assertEqual(_env_val(env, "CONDUCTOR_WORK_DIR"), wt)
                self.assertEqual(_env_val(env, "CONDUCTOR_REPO_DIR"),
                                 str(proj.resolve()))
                self.assertEqual(
                    _env_val(env, "CONDUCTOR_BRANCH"), rec["branch"])
                self.assertEqual(
                    _env_val(env, "CONDUCTOR_SERVICE_PORT"),
                    str(rec["service_port"]))
            # Both leases released; no svc-*.json remains.
            self.assertEqual(
                list((base / "leases").glob("svc-*.json")), [])

    def test_retry_keeps_the_leased_port(self):
        """A timed-out step retried in a fresh session keeps its lease -
        the port is the step's, not the attempt's."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            m = write_manifest(base, f'''
[[step]]
name = "slow"
project = "{proj.as_posix()}"
prompt = "sleep_after=30 write A.txt"
timeout_min = 0.01
stall_min = 0
max_attempts = 2
''')
            with self._lease_dir(base):
                with mock.patch.object(conductor, "WAIT_POLL_S", 0.2):
                    self.assertEqual(run_manifest(m, runs), 1)
            st, rd = state_of(runs)
            rec = st["steps"]["slow"]
            self.assertEqual(rec["status"], "timed_out")
            self.assertEqual(rec["attempts"], 2)
            port = rec["service_port"]
            self.assertIsNotNone(port)
            p1 = (rd / "prompts" / "slow.md").read_text(encoding="utf-8")
            p2 = (rd / "prompts" / "slow.attempt2.md").read_text(
                encoding="utf-8")
            self.assertIn(f"127.0.0.1:{port}", p1)
            self.assertIn(f"127.0.0.1:{port}", p2)
            self.assertIn("previous session attempt", p2)
            self.assertFalse(
                (base / "leases" / f"svc-{port}.json").exists())

    def test_stopped_step_releases_its_lease(self):
        """Cancellation: a step killed via its stop flag drops the lease -
        the port goes back for other runs. The flag is raised inside
        run_step's session probe (post pre-spawn gate, pre-Popen) so the
        kill is deterministic - wait_worker's first poll sees it."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            real_ids = conductor.session_ids

            def flagging_ids(*a, **k):
                out = real_ids(*a, **k)
                try:
                    rd = next(runs.iterdir())
                    conductor._flag_raise(
                        conductor.stop_flag(rd, "slow").name)
                except (StopIteration, OSError):
                    pass
                return out

            m = write_manifest(base, f'''
[[step]]
name = "slow"
project = "{proj.as_posix()}"
prompt = "sleep_after=30 write A.txt"
timeout_min = 60
''')
            with self._lease_dir(base):
                with mock.patch.object(conductor, "WAIT_POLL_S", 0.2), \
                        mock.patch.object(conductor, "session_ids",
                                          flagging_ids):
                    self.assertEqual(run_manifest(m, runs), 1)
                st, rd = state_of(runs)
                rec = st["steps"]["slow"]
                self.assertEqual(rec["status"], "stopped")
                port = rec["service_port"]
                self.assertIsNotNone(port)
                self.assertFalse(
                    (base / "leases" / f"svc-{port}.json").exists())

    def test_skipped_step_never_touches_a_port(self):
        """A run-level halt at the wave gate means the step is never
        dispatched - no lease is allocated, no service fields recorded."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            halted = base / "halt-flag"
            halted.touch()
            real_sf = conductor.stop_flag
            calls: list = []

            def fake_sf(run_dir, step=None):
                return halted if step is None else real_sf(run_dir, step)

            def spy(owner, lease_dir=None):
                calls.append(owner)
                return conductor.SERVICE_PORT_MIN

            m = write_manifest(base, f'''
[[step]]
name = "s"
project = "{proj.as_posix()}"
prompt = "write A.txt"
''')
            with self._lease_dir(base):
                with mock.patch.object(conductor, "stop_flag", fake_sf), \
                        mock.patch.object(conductor,
                                          "alloc_service_port", spy):
                    self.assertEqual(run_manifest(m, runs), 1)
            st, _ = state_of(runs)
            self.assertEqual(st["steps"]["s"]["status"], "skipped")
            self.assertEqual(st.get("aborted"), "stopped")
            self.assertEqual(calls, [])

    def test_serve_port_prefers_step_lease_then_registry_default(self):
        """`serve` launched by a worker inherits CONDUCTOR_SERVICE_PORT;
        --port still wins; unset+unset -> SERVE_PORT."""
        with mock.patch.dict(os.environ,
                             {"CONDUCTOR_SERVICE_PORT": "8817"}):
            self.assertEqual(
                conductor.env_service_port(), 8817)
            # the resolution chain used by cmd_serve
            self.assertEqual(
                None or conductor.env_service_port()
                or conductor.SERVE_PORT, 8817)
            self.assertEqual(
                9000 or conductor.env_service_port()
                or conductor.SERVE_PORT, 9000)
        os.environ.pop("CONDUCTOR_SERVICE_PORT", None)
        self.assertIsNone(conductor.env_service_port())
        self.assertEqual(
            None or conductor.env_service_port()
            or conductor.SERVE_PORT, conductor.SERVE_PORT)

    def test_cloud_step_gets_no_service_port(self):
        """Cloud workers bind nothing local - no lease, no port fields."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            proj = make_project(base)
            runs = base / "runs"
            pf = base / "projects.toml"
            pf.write_text(
                f'[project.p]\npath = "{proj.as_posix()}"\n'
                'cloud_ok = true\n', encoding="utf-8")
            m = write_manifest(base, '''
[[step]]
name = "c"
project = "p"
prompt = "write A.txt"
cloud = true
''')
            with mock.patch.object(conductor, "PROJECTS_FILE", pf):
                with self._lease_dir(base):
                    self.assertEqual(run_manifest(m, runs), 0)
            st, rd = state_of(runs)
            rec = st["steps"]["c"]
            self.assertEqual(rec["status"], "done")
            self.assertIsNone(rec["service_port"])
            self.assertIsNone(rec["service_url"])
            prompt = (rd / "prompts" / "c.md").read_text(encoding="utf-8")
            self.assertNotIn("CONDUCTOR_SERVICE_PORT", prompt)
            self.assertNotIn("CONDUCTOR_SERVICE_PORT",
                             _env_line(rd / "c.stdout.log"))
            self.assertFalse((base / "leases").exists())


if __name__ == "__main__":
    unittest.main()
