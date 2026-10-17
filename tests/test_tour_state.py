"""Tests for tour_state.py - the What's-new tour's persisted watermark +
viewed/snoozed state. Convention: C:\\Projects\\TESTING.md

Everything runs against tempdir fixture repos - real git, no network, no
auth. Clocks are injected (`now=`) so boundary semantics are
deterministic.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import tour_state  # noqa: E402


def git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t",
         "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True)


def make_repo(base: Path, name="repo") -> Path:
    """A tiny git repo fixture with one 'init' commit."""
    p = base / name
    p.mkdir(parents=True)
    git(p, "init")
    (p / "f.txt").write_text("0\n", encoding="utf-8")
    git(p, "add", "-A")
    git(p, "commit", "-m", "init")
    return p


def commit(repo: Path, n: int) -> str:
    """Append one commit, return its sha."""
    with open(repo / "f.txt", "a", encoding="utf-8") as f:
        f.write(f"{n}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", f"c{n}")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def rev(repo: Path, spec="HEAD") -> str:
    return git(repo, "rev-parse", spec).stdout.strip()


T0 = 1_700_000_000.0          # arbitrary fixed epoch for `now=` injection


def rng(state, boundary):
    return tour_state.feed_range(state, boundary)


class InitTests(unittest.TestCase):
    """First initialization + the 'never silently reset to HEAD' rule."""

    def test_absent_file_loads_default_without_writing(self):
        """A pure read fabricates nothing - a GET-shaped caller must not
        create state or pin a boundary."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "data" / "tour-state.json"
            st = tour_state.load(p)
            self.assertEqual(st, {"watermark": None, "viewed": [],
                                  "snoozed": []})
            self.assertFalse(p.exists())

    def test_first_init_writes_unbounded_watermark(self):
        """init records {commit:None, at:None, origin:'init'} - nothing
        acknowledged yet. Pinning HEAD here would silently acknowledge
        all existing history."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tour-state.json"
            st = tour_state.ensure_initialized(p)
            self.assertEqual(st["watermark"],
                             {"commit": None, "at": None,
                              "origin": "init"})
            self.assertEqual(st["viewed"], [])
            self.assertEqual(st["snoozed"], [])
            on_disk = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["watermark"]["origin"], "init")

    def test_init_never_overwrites_existing(self):
        """Re-init (e.g. a serve restart) must not touch recorded state."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tour-state.json"
            tour_state.ensure_initialized(p)
            tour_state.snooze("e1", path=p)
            before = p.read_bytes()
            st = tour_state.ensure_initialized(p)
            self.assertEqual(p.read_bytes(), before)
            self.assertEqual(tour_state.entry_state(st, "e1"), "snoozed")

    def test_init_without_git_repo_still_works(self):
        """capture_boundary on a non-repo yields commit=None - callers can
        still track record-only state."""
        with tempfile.TemporaryDirectory() as td:
            b = tour_state.capture_boundary(Path(td), now=T0)
            self.assertIsNone(b["commit"])
            self.assertTrue(b["at"])

    def test_corrupt_file_quarantined_not_reset(self):
        """An unparseable file is moved aside (bytes kept) and reads as
        the unacknowledged default - never trusted, never silently
        'fixed' to HEAD."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tour-state.json"
            p.write_text("{not json", encoding="utf-8")
            st = tour_state.load(p)
            self.assertIsNone(st["watermark"])
            leftovers = list(Path(td).glob("tour-state.json.corrupt-*"))
            self.assertEqual(len(leftovers), 1)
            self.assertIn(b"not json", leftovers[0].read_bytes())

    def test_watermark_never_serve_boot(self):
        """A timestamp-shaped commit (a serve_boot id leaked into the
        file) is invalid + unavailable - kept, reported, never advanced
        from, never reset to HEAD."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            p = Path(td) / "tour-state.json"
            p.write_text(json.dumps({
                "watermark": {"commit": "2026-10-17T14:57:26",
                              "at": "2026-10-17T14:57:26",
                              "origin": "advance"},
                "viewed": [], "snoozed": []}), encoding="utf-8")
            st = tour_state.load(p)
            status = tour_state.watermark_status(st, repo)
            self.assertFalse(status["valid"])
            self.assertFalse(status["available"])
            # advance is refused: the boundary can't be verified against
            # a garbage watermark
            b = {"commit": rev(repo), "at": tour_state._iso(T0)}
            r = tour_state.mark_viewed("e1", rng(st, b), b,
                                       remaining_new=[], path=p,
                                       repo_dir=repo)
            self.assertFalse(r["advanced"])
            self.assertEqual(r["advance_blocked"],
                             "watermark-commit-invalid")
            # and the recorded value was NOT rewritten
            self.assertEqual(
                tour_state.load(p)["watermark"]["commit"],
                "2026-10-17T14:57:26")

    def test_unavailable_watermark_revision_not_reset(self):
        """A recorded commit that no longer resolves (rebase/gc/foreign
        checkout) stays recorded and reports unavailable - advancement is
        refused, not silently rebased onto HEAD."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            p = Path(td) / "tour-state.json"
            dead = "0" * 40
            p.write_text(json.dumps({
                "watermark": {"commit": dead, "at": None,
                              "origin": "advance"},
                "viewed": [], "snoozed": []}), encoding="utf-8")
            st = tour_state.load(p)
            status = tour_state.watermark_status(st, repo)
            self.assertTrue(status["valid"])
            self.assertFalse(status["available"])
            b = {"commit": rev(repo), "at": tour_state._iso(T0)}
            r = tour_state.mark_viewed("e1", rng(st, b), b,
                                       remaining_new=[], path=p,
                                       repo_dir=repo)
            self.assertFalse(r["advanced"])
            self.assertEqual(r["advance_blocked"],
                             "watermark-commit-unavailable")
            self.assertEqual(tour_state.load(p)["watermark"]["commit"],
                             dead)


class WatermarkMathTests(unittest.TestCase):
    """Commit + time boundary semantics against real git history."""

    def test_advance_lands_on_captured_boundary_not_head(self):
        """New commits arriving AFTER the boundary was captured are never
        covered by the advance."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            c1, c2, c3 = commit(repo, 1), commit(repo, 2), commit(repo, 3)
            p = Path(td) / "tour-state.json"
            tour_state.ensure_initialized(p)
            # feed built at c3; c4 lands while the user is reading
            b = tour_state.capture_boundary(repo, now=T0)
            c4 = commit(repo, 4)
            r = tour_state.mark_viewed(
                "e1", rng(tour_state.load(p), b), b,
                remaining_new=[], path=p, repo_dir=repo, now=T0 + 1)
            self.assertTrue(r["advanced"])
            wm = tour_state.load(p)["watermark"]
            self.assertEqual(wm["commit"], c3)
            self.assertEqual(wm["origin"], "advance")
            # c4 is still unacknowledged - git log c3..HEAD finds it
            out = git(repo, "log", "--format=%H",
                      f"{c3}..HEAD").stdout.split()
            self.assertEqual(out, [c4])
            self.assertNotIn(c1, out)

    def test_advance_requires_descendant_boundary(self):
        """A stale boundary (behind the watermark) is refused - the
        watermark is monotone."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            c1 = rev(repo)
            c2 = commit(repo, 2)
            p = Path(td) / "tour-state.json"
            st = {"watermark": {"commit": c2, "at": tour_state._iso(T0),
                                "origin": "advance"},
                  "viewed": [], "snoozed": []}
            p.write_text(json.dumps(st), encoding="utf-8")
            stale = {"commit": c1, "at": tour_state._iso(T0 - 10)}
            r = tour_state.mark_viewed("e1", None, stale,
                                       remaining_new=[], path=p,
                                       repo_dir=repo)
            self.assertFalse(r["advanced"])
            self.assertEqual(r["advance_blocked"],
                             "boundary-not-descendant")
            self.assertEqual(tour_state.load(p)["watermark"]["commit"],
                             c2)

    def test_advance_to_same_commit_still_moves_time(self):
        """boundary.commit == wm.commit: the record horizon `at` still
        moves forward; `at` never moves backward."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            c1 = rev(repo)
            p = Path(td) / "tour-state.json"
            st = {"watermark": {"commit": c1, "at": tour_state._iso(T0),
                                "origin": "advance"},
                  "viewed": [], "snoozed": []}
            p.write_text(json.dumps(st), encoding="utf-8")
            fwd = {"commit": c1, "at": tour_state._iso(T0 + 60)}
            r = tour_state.mark_viewed("e1", None, fwd,
                                       remaining_new=[], path=p,
                                       repo_dir=repo)
            self.assertTrue(r["advanced"])
            self.assertEqual(tour_state.load(p)["watermark"]["at"],
                             tour_state._iso(T0 + 60))
            back = {"commit": c1, "at": tour_state._iso(T0 - 60)}
            r = tour_state.mark_viewed("e2", None, back,
                                       remaining_new=[], path=p,
                                       repo_dir=repo)
            self.assertTrue(r["advanced"])
            self.assertEqual(tour_state.load(p)["watermark"]["at"],
                             tour_state._iso(T0 + 60))

    def test_unbounded_watermark_advances_to_first_boundary(self):
        """wm.commit=None (init state): the first fully-acknowledged
        boundary becomes the commit floor."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            c1 = rev(repo)
            p = Path(td) / "tour-state.json"
            tour_state.ensure_initialized(p)
            b = {"commit": c1, "at": tour_state._iso(T0)}
            r = tour_state.mark_viewed("e1", None, b,
                                       remaining_new=[], path=p,
                                       repo_dir=repo)
            self.assertTrue(r["advanced"])
            self.assertEqual(r["watermark"]["commit"], c1)

    def test_missing_boundary_commit_never_advances(self):
        """A boundary captured when git was unavailable can't verify
        commit coverage - refused rather than acknowledging blindly."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            p = Path(td) / "tour-state.json"
            tour_state.ensure_initialized(p)
            b = {"commit": None, "at": tour_state._iso(T0)}
            r = tour_state.mark_viewed("e1", None, b,
                                       remaining_new=[], path=p,
                                       repo_dir=repo)
            self.assertFalse(r["advanced"])
            self.assertEqual(r["advance_blocked"],
                             "boundary-missing-commit")


class TransitionTests(unittest.TestCase):
    """viewed / snooze / unview, mutual exclusivity, idempotence."""

    def _repo_and_path(self, td):
        repo = make_repo(Path(td))
        p = Path(td) / "tour-state.json"
        tour_state.ensure_initialized(p)
        return repo, p

    def test_viewed_mark_lands_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            repo, p = self._repo_and_path(td)
            r = tour_state.mark_viewed("e1", None, None,
                                       remaining_new=["e2"],
                                       path=p, repo_dir=repo, now=T0)
            self.assertEqual(r["state"], "viewed")
            self.assertFalse(r["advanced"])
            st = tour_state.load(p)
            self.assertEqual(tour_state.entry_state(st, "e1"), "viewed")
            # second mark: still exactly one descriptor, no error
            tour_state.mark_viewed("e1", None, None,
                                   remaining_new=["e2"],
                                   path=p, repo_dir=repo, now=T0 + 5)
            st = tour_state.load(p)
            self.assertEqual(len(st["viewed"]), 1)
            self.assertEqual(tour_state.entry_state(st, "e1"), "viewed")

    def test_unread_guard_blocks_advance(self):
        """remaining_new non-empty -> mark lands but watermark stays."""
        with tempfile.TemporaryDirectory() as td:
            repo, p = self._repo_and_path(td)
            b = {"commit": rev(repo), "at": tour_state._iso(T0)}
            r = tour_state.mark_viewed("e1", None, b,
                                       remaining_new=["e2"],
                                       path=p, repo_dir=repo)
            self.assertFalse(r["advanced"])
            self.assertEqual(r["advance_blocked"], "unread-remain")
            self.assertIsNone(tour_state.load(p)["watermark"]["commit"])

    def test_snooze_defers_and_does_not_block(self):
        """Snoozed entries are deferred - not 'new', not acknowledged,
        and they don't pin the watermark."""
        with tempfile.TemporaryDirectory() as td:
            repo, p = self._repo_and_path(td)
            b = {"commit": rev(repo), "at": tour_state._iso(T0)}
            tour_state.snooze("e2", rng(tour_state.load(p), b),
                              path=p, now=T0)
            st = tour_state.load(p)
            self.assertEqual(tour_state.entry_state(st, "e2"), "snoozed")
            # caller's remaining_new lists e2, but it isn't 'new' - the
            # guard re-derives state and the advance still lands
            r = tour_state.mark_viewed("e1", None, b,
                                       remaining_new=["e2"],
                                       path=p, repo_dir=repo)
            self.assertTrue(r["advanced"])

    def test_membership_is_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as td:
            repo, p = self._repo_and_path(td)
            tour_state.mark_viewed("e1", None, None, remaining_new=None,
                                   path=p, repo_dir=repo)
            tour_state.snooze("e1", path=p)
            st = tour_state.load(p)
            self.assertEqual(len(st["viewed"]), 0)
            self.assertEqual(len(st["snoozed"]), 1)
            self.assertEqual(tour_state.entry_state(st, "e1"), "snoozed")
            tour_state.mark_viewed("e1", None, None, remaining_new=None,
                                   path=p, repo_dir=repo)
            st = tour_state.load(p)
            self.assertEqual(len(st["snoozed"]), 0)
            self.assertEqual(len(st["viewed"]), 1)
            self.assertEqual(tour_state.entry_state(st, "e1"), "viewed")

    def test_unview_restores_new(self):
        with tempfile.TemporaryDirectory() as td:
            repo, p = self._repo_and_path(td)
            tour_state.mark_viewed("e1", None, None, remaining_new=None,
                                   path=p, repo_dir=repo, now=T0)
            r = tour_state.unview("e1", path=p, now=T0 + 1)
            self.assertEqual(r["state"], "new")
            st = tour_state.load(p)
            self.assertEqual(tour_state.entry_state(st, "e1"), "new")
            # descriptor retained with its range for re-materialization
            d = tour_state.descriptor(st, "e1")
            self.assertIsNotNone(d)
            self.assertTrue(d["unviewed_at"])
            self.assertTrue(d["viewed_at"])
            # and re-viewing clears the flag cleanly
            tour_state.mark_viewed("e1", None, None, remaining_new=None,
                                   path=p, repo_dir=repo, now=T0 + 2)
            self.assertEqual(
                tour_state.entry_state(tour_state.load(p), "e1"),
                "viewed")

    def test_unview_unknown_id_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            repo, p = self._repo_and_path(td)
            r = tour_state.unview("never-seen", path=p)
            self.assertFalse(r["changed"])
            st = tour_state.load(p)
            self.assertEqual(st["viewed"], [])
            self.assertEqual(st["snoozed"], [])

    def test_invalid_entry_id_refused(self):
        with tempfile.TemporaryDirectory() as td:
            repo, p = self._repo_and_path(td)
            for bad in ("", "   ", None, 42, "x" * 600):
                with self.assertRaises(ValueError):
                    tour_state.mark_viewed(bad, None, None,
                                           remaining_new=None,
                                           path=p, repo_dir=repo)

    def test_restart_preserves_unacknowledged_state(self):
        """A reload (server restart) changes nothing: marks, snoozes and
        the watermark persist; unviewed entries stay 'new'."""
        with tempfile.TemporaryDirectory() as td:
            repo, p = self._repo_and_path(td)
            tour_state.mark_viewed("e1", None, None, remaining_new=None,
                                   path=p, repo_dir=repo, now=T0)
            tour_state.snooze("e2", path=p, now=T0)
            tour_state.mark_viewed("e3", None, None, remaining_new=None,
                                   path=p, repo_dir=repo, now=T0)
            tour_state.unview("e3", path=p, now=T0 + 1)
            st = tour_state.load(p)
            self.assertEqual(tour_state.entry_state(st, "e1"), "viewed")
            self.assertEqual(tour_state.entry_state(st, "e2"), "snoozed")
            self.assertEqual(tour_state.entry_state(st, "e3"), "new")
            self.assertIsNone(st["watermark"]["commit"])

    def test_remaining_new_none_never_advances(self):
        """Caller that doesn't know the unread set must not advance."""
        with tempfile.TemporaryDirectory() as td:
            repo, p = self._repo_and_path(td)
            b = {"commit": rev(repo), "at": tour_state._iso(T0)}
            r = tour_state.mark_viewed("e1", None, b,
                                       remaining_new=None,
                                       path=p, repo_dir=repo)
            self.assertFalse(r["advanced"])
            self.assertIsNone(tour_state.load(p)["watermark"]["commit"])


class AdvancementHistoryTests(unittest.TestCase):
    """Viewed history survives the watermark advancing past it; unview
    after advancement resurfaces the entry."""

    def test_unview_after_advancement_keeps_range(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            c1 = rev(repo)
            p = Path(td) / "tour-state.json"
            tour_state.ensure_initialized(p)
            b1 = {"commit": c1, "at": tour_state._iso(T0)}
            rng1 = rng(tour_state.load(p), b1)
            tour_state.mark_viewed("e1", rng1, b1, remaining_new=[],
                                   path=p, repo_dir=repo, now=T0)
            # watermark now at c1; e1's window is fully behind it
            self.assertEqual(tour_state.load(p)["watermark"]["commit"],
                             c1)
            r = tour_state.unview("e1", path=p, now=T0 + 10)
            self.assertEqual(r["state"], "new")
            st = tour_state.load(p)
            d = tour_state.descriptor(st, "e1")
            self.assertIsNotNone(d)
            self.assertEqual(d["range"]["tip"]["commit"], c1)
            self.assertTrue(d["unviewed_at"])
            self.assertEqual(tour_state.entry_state(st, "e1"), "new")
            # entries() still carries it so the feed can re-inject it
            es = {e["id"]: e["state"] for e in tour_state.entries(st)}
            self.assertEqual(es["e1"], "new")

    def test_viewed_history_persists_across_advances(self):
        """Multiple acknowledged windows each keep their own range."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            c1 = rev(repo)
            p = Path(td) / "tour-state.json"
            tour_state.ensure_initialized(p)
            b1 = {"commit": c1, "at": tour_state._iso(T0)}
            tour_state.mark_viewed("e1", rng(tour_state.load(p), b1), b1,
                                   remaining_new=[], path=p,
                                   repo_dir=repo, now=T0)
            c2 = commit(repo, 2)
            b2 = {"commit": c2, "at": tour_state._iso(T0 + 100)}
            tour_state.mark_viewed("e2", rng(tour_state.load(p), b2), b2,
                                   remaining_new=[], path=p,
                                   repo_dir=repo, now=T0 + 100)
            st = tour_state.load(p)
            self.assertEqual(st["watermark"]["commit"], c2)
            es = {e["id"]: e for e in tour_state.entries(st)}
            self.assertEqual(es["e1"]["range"]["tip"]["commit"], c1)
            self.assertEqual(es["e2"]["range"]["tip"]["commit"], c2)
            # e2's range base is the post-e1 watermark - feeds can replay
            # exactly the window that produced each entry
            self.assertEqual(es["e2"]["range"]["base"]["commit"], c1)

    def test_changes_arriving_during_acknowledgment_stay_new(self):
        """Commits landing between feed capture and the final mark are
        never swallowed by the advance."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            commit(repo, 1)
            c2 = commit(repo, 2)
            p = Path(td) / "tour-state.json"
            tour_state.ensure_initialized(p)
            b = tour_state.capture_boundary(repo, now=T0)
            # e1 acknowledged; while acknowledging, c3 lands
            c3 = commit(repo, 3)
            r = tour_state.mark_viewed(
                "e1", rng(tour_state.load(p), b), b,
                remaining_new=[], path=p, repo_dir=repo, now=T0 + 1)
            self.assertTrue(r["advanced"])
            self.assertEqual(r["watermark"]["commit"], c2)
            # c3 remains ahead of the watermark
            self.assertEqual(
                git(repo, "log", "--format=%H",
                    f"{c2}..HEAD").stdout.split(), [c3])


class ConcurrencyTests(unittest.TestCase):
    """Serialized read-modify-write: concurrent marks lose nothing."""

    def test_concurrent_marks_all_land(self):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            p = Path(td) / "tour-state.json"
            tour_state.ensure_initialized(p)
            errors = []

            def mark(i):
                try:
                    tour_state.mark_viewed(f"e{i}", None, None,
                                           remaining_new=None,
                                           path=p, repo_dir=repo)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=mark, args=(i,))
                       for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            st = tour_state.load(p)
            ids = {d["id"] for d in st["viewed"]}
            self.assertEqual(ids, {f"e{i}" for i in range(20)})

    def test_concurrent_mixed_transitions_consistent(self):
        """Mixed viewed/snooze/unview racing on the same ids ends in a
        coherent state - one list membership per id."""
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            p = Path(td) / "tour-state.json"
            tour_state.ensure_initialized(p)
            errors = []

            def act(i):
                try:
                    eid = f"e{i % 5}"
                    if i % 3 == 0:
                        tour_state.snooze(eid, path=p)
                    elif i % 3 == 1:
                        tour_state.mark_viewed(eid, None, None,
                                               remaining_new=None,
                                               path=p, repo_dir=repo)
                    else:
                        tour_state.unview(eid, path=p)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=act, args=(i,))
                       for i in range(30)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            st = tour_state.load(p)
            seen = set()
            for d in st["viewed"] + st["snoozed"]:
                self.assertNotIn(d["id"], seen)
                seen.add(d["id"])
            for eid in seen:
                self.assertIn(tour_state.entry_state(st, eid),
                              tour_state.STATES)


if __name__ == "__main__":
    unittest.main()
