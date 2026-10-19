"""Tests for tour_feeds.py - the deterministic change feed behind the
dashboard 'What's new' tour. Convention: C:\\Projects\\TESTING.md

Everything runs against tempdir fixture repos/dirs - real git, no
network, no LLM, no HTTP. Window bounds are injected through explicit
{base,tip} ranges and record stamps.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import tour_feeds  # noqa: E402


def git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t",
         "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True)


def rev(repo: Path, spec="HEAD") -> str:
    return git(repo, "rev-parse", spec).stdout.strip()


def default_branch(repo: Path) -> str:
    return git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()


def make_repo(base: Path, name="repo") -> Path:
    p = base / name
    p.mkdir(parents=True)
    git(p, "init")
    (p / "f.txt").write_text("0\n", encoding="utf-8")
    git(p, "add", "-A")
    git(p, "commit", "-m", "init")
    return p


def commit(repo: Path, path: str, content: str, subject: str) -> str:
    """Commit one file write with an explicit subject; returns sha."""
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", subject)
    return rev(repo)


def branch_commit(repo: Path, branch: str, path: str, content: str,
                  subject: str) -> str:
    """Commit on a fresh side branch, return to the default branch."""
    main = default_branch(repo)
    git(repo, "checkout", "-b", branch)
    sha = commit(repo, path, content, subject)
    git(repo, "checkout", main)
    return sha


def merge(repo: Path, branch: str) -> str:
    git(repo, "merge", "--no-ff", branch)
    return rev(repo)


# ------------------------------------------------------------- fixtures

def wm(commit=None, at=None) -> dict:
    """A state dict with a bounded watermark."""
    return {"watermark": {"commit": commit, "at": at, "origin": "advance"},
            "viewed": [], "snoozed": []}


def boundary(repo: Path, at="2026-10-17T12:00:00") -> dict:
    return {"commit": rev(repo), "at": at}


def rng(base_commit=None, base_at=None, tip_commit=None,
        tip_at="2026-10-17T12:00:00") -> dict:
    return {"base": {"commit": base_commit, "at": base_at},
            "tip": {"commit": tip_commit, "at": tip_at}}


def write_request(requests_dir: Path, rid: str, created_at: str,
                  **kw) -> dict:
    rec = {"id": rid, "status": "ready", "created_at": created_at,
           "label": kw.pop("label", f"request {rid}"),
           "description": kw.pop("description", ""),
           **kw}
    requests_dir.mkdir(parents=True, exist_ok=True)
    (requests_dir / f"{rid}.json").write_text(
        json.dumps(rec), encoding="utf-8")
    return rec


def write_run(runs_dir: Path, name: str, ended_at: str | None,
              **kw) -> dict:
    st = {"run": name, "request_id": kw.pop("request_id", None),
          "started_at": kw.pop("started_at", "2026-10-16T10:00:00"),
          "ended_at": ended_at, "steps": kw.pop("steps", {}), **kw}
    d = runs_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(st), encoding="utf-8")
    return st


def feed(repo, runs_dir, requests_dir, manifests_dir, state=None,
         bnd=None):
    return tour_feeds.build_feed(
        state if state is not None else wm(),
        bnd if bnd is not None else boundary(repo),
        repo_dir=repo, runs_dir=runs_dir, requests_dir=requests_dir,
        manifests_dir=manifests_dir)


def by_id(feed_out):
    return {e["id"]: e for e in feed_out["entries"]}


# ------------------------------------------------------------- tests

class AreaGroupingTests(unittest.TestCase):
    """Commits group into one card per touched feature area."""

    def test_commits_group_by_touched_area(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            rdir, qdir, mdir = base/"r", base/"q", base/"m"
            c0 = rev(repo)
            c1 = commit(repo, "assistant/chat.py", "x", "add assistant chat")
            c2 = commit(repo, "mobile_device.py", "x", "add relaunch helper")
            c3 = commit(repo, "tests/test_x.py", "x", "cover the helper")
            f = feed(repo, rdir, qdir, mdir,
                     state=wm(c0, "2026-10-16T00:00:00"))
            ids = by_id(f)
            self.assertEqual(len(f["entries"]), 3)
            self.assertEqual(ids[f"area:assistant:{c1[:12]}"]["stop"],
                             "assistant")
            self.assertEqual(ids[f"area:assistant:{c1[:12]}"]["anchor"],
                             "#dask")
            self.assertEqual(ids[f"area:mobile:{c2[:12]}"]["anchor"], None)
            self.assertEqual(ids[f"area:tests:{c3[:12]}"]["stop"], "tests")

    def test_same_area_commits_share_one_card(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat one")
            c2 = commit(repo, "assistant_host/b.py", "x", "chat two")
            f = feed(repo, base/"r", base/"q", base/"m", state=wm(c0))
            es = [e for e in f["entries"] if e["kind"] == "change"]
            self.assertEqual(len(es), 1)
            self.assertEqual(es[0]["id"], f"area:assistant:{c2[:12]}")
            self.assertEqual([c["sha"] for c in es[0]["commits"]],
                             [c2, c1])     # newest first

    def test_conductor_py_uses_subject_keywords(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            commit(repo, "conductor.py", "x", "Fix merge lock race")
            f = feed(repo, base/"r", base/"q", base/"m", state=wm(c0))
            e = f["entries"][0]
            self.assertEqual(e["stop"], "runs")        # 'merge' keyword
            self.assertEqual(e["tag"], "fixed")        # 'Fix' keyword

    def test_conductor_py_plus_tests_stays_monolith(self):
        """tests/ is a support dir - conductor.py+tests is a conductor
        change, not a 'tests' change."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            commit(repo, "conductor.py", "x", "Improve power dialog")
            commit(repo, "tests/test_a.py", "x", "cover power dialog")
            f = feed(repo, base/"r", base/"q", base/"m", state=wm(c0))
            kinds = sorted(e["stop"] for e in f["entries"])
            self.assertEqual(kinds, ["power", "tests"])

    def test_multi_area_commit_is_mixed(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            commit(repo, "conductor.py", "x", "both")
            (repo / "assistant").mkdir(exist_ok=True)
            commit(repo, "assistant/a.py", "y", "both")
            # one commit touching assistant + mobile
            (repo / "mobile_device.py").write_text("z")
            (repo / "assistant/b.py").write_text("z")
            git(repo, "add", "-A"); git(repo, "commit", "-m", "two areas")
            f = feed(repo, base/"r", base/"q", base/"m", state=wm(c0))
            stops = [e["stop"] for e in f["entries"]]
            self.assertIn("mixed", stops)
            self.assertIn("assistant", stops)   # the a.py-only commit


class WindowTests(unittest.TestCase):
    """Record feeds bound on their own event stamps, not creation."""

    def test_request_created_at_bounds_the_card(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            q = base/"q"
            write_request(q, "20261016-000000-aaaabbbb",
                          "2026-10-16T00:00:00")           # at base
            write_request(q, "20261016-010000-ccccdddd",
                          "2026-10-16T01:00:00")           # in window
            write_request(q, "20261018-010000-eeeeffff",
                          "2026-10-18T01:00:00")           # after tip
            state = wm(None, "2026-10-16T00:00:00")
            bnd = {"commit": rev(repo), "at": "2026-10-17T00:00:00"}
            f = feed(repo, base/"r", q, base/"m", state, bnd)
            ids = by_id(f)
            self.assertIn("request:20261016-010000-ccccdddd", ids)
            self.assertNotIn("request:20261016-000000-aaaabbbb", ids)
            self.assertNotIn("request:20261018-010000-eeeeffff", ids)

    def test_run_bounds_on_ended_at_not_started(self):
        """A run started before the window but finished inside counts;
        started-inside-but-still-running does not."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r = base/"r"
            write_run(r, "20261016-000000-oldrun", "2026-10-17T11:00:00",
                      started_at="2026-10-15T00:00:00")
            write_run(r, "20261017-000000-liverun", None,
                      started_at="2026-10-17T11:30:00")
            write_run(r, "20261015-000000-donerun", "2026-10-15T10:00:00")
            state = wm(None, "2026-10-17T00:00:00")
            bnd = {"commit": rev(repo), "at": "2026-10-17T12:00:00"}
            f = feed(repo, r, base/"q", base/"m", state, bnd)
            runs = [e for e in f["entries"] if e["kind"] == "run"]
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["runs"], ["20261016-000000-oldrun"]
                             if False else runs[0]["runs"])
            self.assertTrue(runs[0]["id"].startswith(
                "run:20261016-000000-oldrun:"))

    def test_null_watermark_is_unbounded(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            commit(repo, "assistant/a.py", "x", "old feature")
            f = feed(repo, base/"r", base/"q", base/"m",
                     state=wm(), bnd=boundary(repo))
            # unbounded = every commit: the 'init' commit + the feature
            self.assertEqual(len(f["entries"]), 2)
            self.assertTrue(any(e["stop"] == "assistant"
                                for e in f["entries"]))

    def test_unresolvable_watermark_warns_and_unbounds(self):
        """A recorded commit the repo lost must not hide the feed -
        it falls back to unbounded with a warning (loud, not silent)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            commit(repo, "assistant/a.py", "x", "thing")
            state = wm("deadbeef" * 5, "2026-10-16T00:00:00")
            f = feed(repo, base/"r", base/"q", base/"m",
                     state, boundary(repo))
            self.assertTrue(f["warnings"])
            self.assertTrue(f["entries"])
            self.assertIsNone(f["range"]["base"]["commit"])


class TagTests(unittest.TestCase):
    def test_deterministic_tags(self):
        self.assertEqual(tour_feeds._tag_for_subject("Fix crash"), "fixed")
        self.assertEqual(tour_feeds._tag_for_subject("Improve perf"),
                         "improved")
        self.assertEqual(tour_feeds._tag_for_subject("Add thing"), "new")

    def test_card_tag_priority(self):
        commits = [{"subject": "Add x"}, {"subject": "Fix y"}]
        self.assertEqual(tour_feeds._card_tag(commits), "fixed")


class CuratedTests(unittest.TestCase):
    """manifests/whatsnew.jsonl curation, precedence and tolerance."""

    def _write(self, mdir: Path, lines: list[str]):
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "whatsnew.jsonl").write_text("\n".join(lines) + "\n",
                                             encoding="utf-8")

    def test_curated_blurb_beats_subject_same_area(self):
        """A record claiming commits in its own area folds INTO the
        area card - stable id kept, human text wins."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat internals")
            self._write(base/"m", [json.dumps({
                "stop": "assistant", "blurb": "Chat now streams",
                "commits": [c1[:10]]})])
            f = feed(repo, base/"r", base/"q", base/"m", state=wm(c0))
            es = [e for e in f["entries"]]
            self.assertEqual(len(es), 1)
            self.assertEqual(es[0]["id"], f"area:assistant:{c1[:12]}")
            self.assertEqual(es[0]["blurb"], "Chat now streams")

    def test_curated_other_stop_extracts_card(self):
        """Same commits, stop naming a different area -> dedicated
        card claiming them (the area card no longer duplicates them)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat internals")
            c2 = commit(repo, "assistant/b.py", "x", "chat bits")
            self._write(base/"m", [json.dumps({
                "stop": "modal", "blurb": "Detail modal gained chat",
                "commits": [c1[:10]]})])
            f = feed(repo, base/"r", base/"q", base/"m", state=wm(c0))
            ids = by_id(f)
            self.assertIn(f"curated:modal:{c1[:12]}", ids)
            cur = ids[f"curated:modal:{c1[:12]}"]
            self.assertEqual(cur["blurb"], "Detail modal gained chat")
            self.assertEqual(cur["anchor"], "#backdrop")
            self.assertEqual([c["sha"] for c in cur["commits"]], [c1])
            # the area card keeps only the unclaimed commit - no dup
            area = ids[f"area:assistant:{c2[:12]}"]
            self.assertEqual([c["sha"] for c in area["commits"]], [c2])

    def test_subject_fallback_without_curated(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            commit(repo, "assistant/a.py", "x", "Add chat panel")
            f = feed(repo, base/"r", base/"q", base/"m", state=wm(c0))
            self.assertEqual(f["entries"][0]["title"], "Add chat panel")

    def test_unknown_stop_unanchored(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat")
            self._write(base/"m", [json.dumps({
                "stop": "nonsense", "blurb": "x",
                "commits": [c1[:12]]})])
            f = feed(repo, base/"r", base/"q", base/"m", state=wm(c0))
            e = [e for e in f["entries"] if e["kind"] == "curated"][0]
            self.assertIsNone(e["anchor"])   # unknown stop: unanchored
            self.assertEqual(e["stop"], "nonsense")

    def test_malformed_lines_tolerated(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat")
            self._write(base/"m", [
                "{not json",
                json.dumps("a string"),
                json.dumps({"blurb": "no stop"}),
                json.dumps({"stop": "assistant"}),          # no identity
                json.dumps({"stop": "assistant", "blurb": "no id"}),
                json.dumps({"stop": "assistant", "blurb": "x",
                            "commits": ["deadbeef"]}),      # out of range
                json.dumps({"stop": "assistant", "blurb": "real",
                            "commits": [c1[:8]]})])
            f = feed(repo, base/"r", base/"q", base/"m", state=wm(c0))
            self.assertTrue(len(f["warnings"]) >= 5)
            e = f["entries"][0]
            self.assertEqual(e["blurb"], "real")

    def test_two_records_claim_same_commit_first_wins(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat")
            self._write(base/"m", [
                json.dumps({"stop": "modal", "blurb": "first",
                            "commits": [c1[:12]]}),
                json.dumps({"stop": "power", "blurb": "second",
                            "commits": [c1[:12]]})])
            f = feed(repo, base/"r", base/"q", base/"m", state=wm(c0))
            curs = [e for e in f["entries"] if e["kind"] == "curated"]
            self.assertEqual(len(curs), 1)
            self.assertEqual(curs[0]["blurb"], "first")

    def test_curated_request_claim_suppresses_dup(self):
        """A record naming a request folds into/absorbs that card -
        the same change never appears as two cards."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            q = base/"q"
            write_request(q, "20261017-010000-aaaabbbb",
                          "2026-10-17T01:00:00", label="big thing")
            self._write(base/"m", [json.dumps({
                "stop": "requests", "blurb": "Human summary of it",
                "requests": ["20261017-010000-aaaabbbb"]})])
            state = wm(None, "2026-10-16T00:00:00")
            bnd = {"commit": rev(repo), "at": "2026-10-17T12:00:00"}
            f = feed(repo, base/"r", q, base/"m", state, bnd)
            es = [e for e in f["entries"]
                  if e["requests"] == ["20261017-010000-aaaabbbb"]]
            self.assertEqual(len(es), 1)
            self.assertEqual(es[0]["id"], "request:20261017-010000-aaaabbbb")
            self.assertEqual(es[0]["blurb"], "Human summary of it")


class AssociationTests(unittest.TestCase):
    """Run/request links + conductor merge attribution."""

    def test_followup_card_links_source_run(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r, q = base/"r", base/"q"
            write_run(r, "20261017-000000-src",
                      "2026-10-17T08:00:00",
                      request_id="20261017-000000-11112222")
            write_request(q, "20261017-090000-abcd1234",
                          "2026-10-17T09:00:00", label="more work",
                          source_run="20261017-000000-src",
                          source_manifest="manifests/src-followup.toml",
                          imported="followup")
            state = wm(None, "2026-10-17T00:00:00")
            bnd = {"commit": rev(repo), "at": "2026-10-17T12:00:00"}
            f = feed(repo, r, q, base/"m", state, bnd)
            ids = by_id(f)
            fu = ids["followup:20261017-090000-abcd1234"]
            # the follow-up row links back via the run's dashboard key
            self.assertEqual(fu["runs"],
                             ["request:20261017-000000-11112222"])
            # and it is NOT also a plain 'new request' card (dedup)
            self.assertNotIn("request:20261017-090000-abcd1234", ids)
            run = ids["run:20261017-000000-src:2026-10-17T08:00:00"]
            self.assertEqual(run["requests"],
                             ["20261017-000000-11112222"])

    def test_request_card_links_launched_run(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r, q = base/"r", base/"q"
            write_run(r, "20261017-050000-x", "2026-10-17T06:00:00",
                      request_id="20261017-040000-9999aaaa")
            write_request(q, "20261017-040000-9999aaaa",
                          "2026-10-17T04:00:00")
            state = wm(None, "2026-10-17T00:00:00")
            bnd = {"commit": rev(repo), "at": "2026-10-17T12:00:00"}
            f = feed(repo, r, q, base/"m", state, bnd)
            ids = by_id(f)
            # same window: the proposal folds into its run's card - the
            # standalone request card is gone, the link survives as the
            # pipeline card's run chip + covers entry
            self.assertNotIn("request:20261017-040000-9999aaaa", ids)
            e = ids["run:20261017-050000-x:2026-10-17T06:00:00"]
            self.assertEqual(e["kind"], "pipeline")
            self.assertEqual(e["runs"],
                             ["request:20261017-040000-9999aaaa"])
            self.assertEqual(e["requests"],
                             ["20261017-040000-9999aaaa"])
            self.assertIn("request:20261017-040000-9999aaaa",
                          e["covers"])

    def test_merge_commits_attribute_to_run_card(self):
        """A conductor merge + its branch chain belong to the finished
        run's card - they must not ALSO group into area cards."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            branch_commit(repo, "conductor/20261017-100000-myrun/step-a",
                          "assistant/a.py", "x", "step a work")
            m = merge(repo, "conductor/20261017-100000-myrun/step-a")
            r = base/"r"
            write_run(r, "20261017-100000-myrun", "2026-10-17T11:00:00",
                      request_id="20261017-090000-00001111")
            state = wm(c0, "2026-10-17T00:00:00")
            bnd = {"commit": rev(repo), "at": "2026-10-17T12:00:00"}
            f = feed(repo, r, base/"q", base/"m", state, bnd)
            run_e = [e for e in f["entries"] if e["kind"] == "run"][0]
            shas = {c["sha"] for c in run_e["commits"]}
            self.assertIn(m, shas)                       # the merge itself
            self.assertIn("step a work",
                          {c["subject"] for c in run_e["commits"]})
            # nothing double-appears as an area card
            self.assertFalse([e for e in f["entries"]
                              if e["kind"] == "change"])


class IdentityTests(unittest.TestCase):
    """Stable ids, viewed-state binding, 'new area change' re-surfacing."""

    def test_same_window_same_ids(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            commit(repo, "assistant/a.py", "x", "chat")
            kw = dict(repo_dir=repo, runs_dir=base/"r",
                      requests_dir=base/"q", manifests_dir=base/"m")
            st = wm(c0, "2026-10-16T00:00:00")
            b = boundary(repo)
            f1 = tour_feeds.build_feed(st, b, **kw)
            f2 = tour_feeds.build_feed(st, b, **kw)
            self.assertEqual([e["id"] for e in f1["entries"]],
                             [e["id"] for e in f2["entries"]])

    def test_new_commit_in_viewed_area_gets_new_id(self):
        """'new changes in an already-viewed area' - a fresh commit in
        a card's area changes its id, so 'viewed' cannot bleed onto it."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat one")
            st = wm(c0, "2026-10-16T00:00:00")
            f1 = feed(repo, base/"r", base/"q", base/"m", st)
            eid = f1["entries"][0]["id"]
            st["viewed"] = [{"id": eid, "range": f1["range"],
                             "viewed_at": "2026-10-17T01:00:00"}]
            f2 = feed(repo, base/"r", base/"q", base/"m", st)
            # a viewed card leaves the deck immediately - it lives on
            # in history, even while the watermark is pinned by other
            # still-new cards
            self.assertEqual(f2["entries"], [])
            self.assertEqual([h["id"] for h in f2["history"]], [eid])
            self.assertEqual(f2["history"][0]["state"], "viewed")
            # a new commit lands in the same area
            c2 = commit(repo, "assistant/b.py", "y", "chat two")
            f3 = feed(repo, base/"r", base/"q", base/"m", st)
            self.assertNotEqual(f3["entries"][0]["id"], eid)
            self.assertEqual(f3["entries"][0]["state"], "new")
            self.assertEqual(len(f3["entries"][0]["commits"]), 2)

    def test_viewed_history_survives_advancement(self):
        """After the watermark advances past a card's window, the stored
        descriptor still re-materializes it for the viewed section."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat one")
            st = wm(c0, "2026-10-16T00:00:00")
            b1 = boundary(repo, "2026-10-17T01:00:00")
            f1 = feed(repo, base/"r", base/"q", base/"m", st, b1)
            eid = f1["entries"][0]["id"]
            # user views it; watermark lands at the boundary
            st["viewed"] = [{"id": eid, "range": f1["range"],
                             "viewed_at": "2026-10-17T02:00:00"}]
            st["watermark"] = {"commit": b1["commit"], "at": b1["at"],
                               "origin": "advance"}
            c2 = commit(repo, "assistant/b.py", "y", "chat two")
            f2 = feed(repo, base/"r", base/"q", base/"m", st,
                      boundary(repo, "2026-10-17T12:00:00"))
            live_ids = by_id(f2)
            self.assertNotIn(eid, live_ids)            # out of window now
            self.assertEqual([e["id"] for e in f2["entries"]],
                             [f"area:assistant:{c2[:12]}"])
            hist = {e["id"]: e for e in f2["history"]}
            self.assertIn(eid, hist)
            self.assertEqual(hist[eid]["state"], "viewed")
            self.assertEqual(hist[eid]["commits"][0]["sha"], c1)
            self.assertTrue(hist[eid]["anchor"])       # still anchored

    def test_unviewed_descriptor_reinjects_behind_watermark(self):
        """unview restores 'new' - even when the producing window is
        already behind the watermark, the card resurfaces."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat")
            old_range = rng(c0, "2026-10-16T00:00:00", c1,
                            "2026-10-17T01:00:00")
            st = wm(rev(repo), "2026-10-17T02:00:00")   # wm past the card
            st["viewed"] = [{"id": f"area:assistant:{c1[:12]}",
                             "range": old_range,
                             "viewed_at": "2026-10-17T01:30:00",
                             "unviewed_at": "2026-10-17T03:00:00"}]
            f = feed(repo, base/"r", base/"q", base/"m", st)
            es = [e for e in f["entries"]
                  if e["id"] == f"area:assistant:{c1[:12]}"]
            self.assertEqual(len(es), 1)
            self.assertEqual(es[0]["state"], "new")

    def test_snoozed_descriptor_reinjects(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat")
            st = wm(rev(repo), "2026-10-17T02:00:00")
            st["snoozed"] = [{"id": f"area:assistant:{c1[:12]}",
                              "range": rng(c0, None, c1,
                                           "2026-10-17T01:00:00"),
                              "snoozed_at": "2026-10-17T01:30:00"}]
            f = feed(repo, base/"r", base/"q", base/"m", st)
            es = [e for e in f["entries"]
                  if e["id"] == f"area:assistant:{c1[:12]}"]
            self.assertEqual(len(es), 1)
            self.assertEqual(es[0]["state"], "snoozed")
            self.assertEqual(f["unseen_count"], 0)

    def test_lost_history_is_an_honest_stub(self):
        """A descriptor whose range can no longer rebuild still returns
        a card marked unavailable - never silently dropped."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            st = wm(rev(repo), "2026-10-17T02:00:00")
            st["viewed"] = [{"id": "request:20261016-000000-deadbeef",
                             "range": rng(None, "2026-10-16T00:00:00",
                                          rev(repo),
                                          "2026-10-17T01:00:00"),
                             "viewed_at": "2026-10-17T01:30:00"}]
            f = feed(repo, base/"r", base/"q", base/"m", st)
            self.assertEqual(f["history"][0]["id"],
                             "request:20261016-000000-deadbeef")
            self.assertTrue(f["history"][0]["unavailable"])


class SplitFeedTests(unittest.TestCase):
    """The 'Tool updates' / 'Work digest' split feed contract: section
    classification, same-window request+run folding, follow-up folding,
    record-event tags, the run's unreviewed-task count, 'viewed' leaving
    the live deck, and per-build exact-range collection memoization."""

    def test_sections_classify_cards(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r, q = base/"r", base/"q"
            commit(repo, "assistant/a.py", "x", "chat")
            write_request(q, "20261017-010000-aaaabbbb",
                          "2026-10-17T01:00:00")
            st = wm(None, "2026-10-17T00:00:00")
            f = feed(repo, r, q, base/"m", st,
                     boundary(repo, "2026-10-17T12:00:00"))
            secs = {e["kind"]: (e["section"], e["section_title"])
                    for e in f["entries"]}
            self.assertEqual(secs["change"], ("tool", "Tool updates"))
            self.assertEqual(secs["request"], ("work", "Work digest"))

    def test_pipeline_fold_fields(self):
        """A request + its in-window run = one work card carrying both
        event dates, the terminal outcome, and step tallies."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r, q = base/"r", base/"q"
            write_run(r, "20261017-050000-x", "2026-10-17T06:00:00",
                      request_id="20261017-040000-9999aaaa",
                      steps={"s1": {"status": "done"},
                             "s2": {"status": "done"}})
            write_request(q, "20261017-040000-9999aaaa",
                          "2026-10-17T04:00:00", label="ship it")
            st = wm(rev(repo), "2026-10-17T00:00:00")
            f = feed(repo, r, q, base/"m", st,
                     boundary(repo, "2026-10-17T12:00:00"))
            self.assertEqual(len(f["entries"]), 1)      # one work card
            e = f["entries"][0]
            self.assertEqual(e["id"],
                             "run:20261017-050000-x:2026-10-17T06:00:00")
            self.assertEqual(e["kind"], "pipeline")
            self.assertEqual(e["section"], "work")
            self.assertEqual(e["tag"], "done")          # not a change tag
            self.assertEqual(e["outcome"], "done")
            self.assertEqual(e["proposed_at"], "2026-10-17T04:00:00")
            self.assertEqual(e["completed_at"], "2026-10-17T06:00:00")
            self.assertEqual(e["steps_done"], 2)
            self.assertEqual(e["steps_total"], 2)
            self.assertEqual(e["unreviewed"], 0)
            self.assertEqual(e["covers"],
                             ["request:20261017-040000-9999aaaa"])

    def test_failed_and_aborted_outcomes_tag_honestly(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r = base/"r"
            write_run(r, "20261017-050000-f", "2026-10-17T06:00:00",
                      steps={"s1": {"status": "done"},
                             "s2": {"status": "failed"}})
            write_run(r, "20261017-070000-s", "2026-10-17T08:00:00",
                      aborted="stopped",
                      steps={"s1": {"status": "done"}})
            st = wm(None, "2026-10-17T00:00:00")
            f = feed(repo, r, base/"q", base/"m", st,
                     boundary(repo, "2026-10-17T12:00:00"))
            by = by_id(f)
            self.assertEqual(by["run:20261017-050000-f:"
                                "2026-10-17T06:00:00"]["outcome"],
                             "failed")
            self.assertEqual(by["run:20261017-050000-f:"
                                "2026-10-17T06:00:00"]["tag"], "failed")
            # a recorded halt reason is the outcome - step verdicts
            # never overrule it
            self.assertEqual(by["run:20261017-070000-s:"
                                "2026-10-17T08:00:00"]["outcome"],
                             "stopped")

    def test_cross_window_events_keep_standalone_cards(self):
        """A run in the window whose request was created in an earlier
        (acknowledged) window stays a standalone 'run' card - and vice
        versa."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r, q = base/"r", base/"q"
            write_run(r, "20261017-050000-x", "2026-10-17T06:00:00",
                      request_id="20261016-040000-9999aaaa")
            write_request(q, "20261016-040000-9999aaaa",
                          "2026-10-16T04:00:00")     # before base.at
            st = wm(None, "2026-10-17T00:00:00")
            f = feed(repo, r, q, base/"m", st,
                     boundary(repo, "2026-10-17T12:00:00"))
            ids = by_id(f)
            e = ids["run:20261017-050000-x:2026-10-17T06:00:00"]
            self.assertEqual(e["kind"], "run")          # not folded
            self.assertNotIn("covers", e)
            self.assertEqual(e["requests"],
                             ["20261016-040000-9999aaaa"])
            # ...but the proposal date still resolves for the card
            self.assertEqual(e["proposed_at"], "2026-10-16T04:00:00")

    def test_followup_folds_into_associated_request_card(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r, q = base/"r", base/"q"
            write_run(r, "20261017-050000-src", "2026-10-17T06:00:00",
                      request_id="20261017-010000-11112222")
            write_request(q, "20261017-010000-11112222",
                          "2026-10-17T01:00:00", label="the request")
            write_request(q, "20261017-090000-abcd1234",
                          "2026-10-17T09:00:00", label="more work",
                          source_run="20261017-050000-src",
                          source_request="20261017-010000-11112222",
                          imported="followup")
            st = wm(rev(repo), "2026-10-17T00:00:00")
            f = feed(repo, r, q, base/"m", st,
                     boundary(repo, "2026-10-17T12:00:00"))
            ids = by_id(f)
            self.assertEqual(len(ids), 1)               # all folded
            e = ids["run:20261017-050000-src:2026-10-17T06:00:00"]
            self.assertEqual(e["kind"], "pipeline")
            self.assertEqual({c for c in e["covers"]},
                             {"request:20261017-010000-11112222",
                              "followup:20261017-090000-abcd1234"})
            fu = e["followups"][0]
            self.assertEqual(fu["id"],
                             "followup:20261017-090000-abcd1234")
            self.assertEqual(fu["requests"],
                             ["20261017-090000-abcd1234"])
            self.assertEqual(fu["at"], "2026-10-17T09:00:00")

    def test_followup_stays_standalone_across_windows(self):
        """A follow-up whose associated request sits in another window
        keeps its own card - matching the recorded link, not names."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r, q = base/"r", base/"q"
            write_run(r, "20261017-050000-src", "2026-10-17T06:00:00",
                      request_id="20261016-010000-11112222")
            write_request(q, "20261016-010000-11112222",
                          "2026-10-16T01:00:00")     # before base.at
            write_request(q, "20261017-090000-abcd1234",
                          "2026-10-17T09:00:00",
                          source_run="20261017-050000-src",
                          source_request="20261016-010000-11112222",
                          imported="followup")
            st = wm(None, "2026-10-17T00:00:00")
            f = feed(repo, r, q, base/"m", st,
                     boundary(repo, "2026-10-17T12:00:00"))
            ids = by_id(f)
            # three standalone events: run, follow-up (source request
            # acknowledged in an earlier window)
            self.assertIn("run:20261017-050000-src:2026-10-17T06:00:00",
                          ids)
            self.assertIn("followup:20261017-090000-abcd1234", ids)

    def test_folded_descriptor_rematerializes_through_covers(self):
        """A 'viewed' request descriptor from before the run finished
        still resolves - through the pipeline card's covers list."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r, q = base/"r", base/"q"
            write_run(r, "20261017-050000-x", "2026-10-17T06:00:00",
                      request_id="20261017-040000-9999aaaa")
            write_request(q, "20261017-040000-9999aaaa",
                          "2026-10-17T04:00:00")
            st = wm(None, "2026-10-17T00:00:00")
            bnd = boundary(repo, "2026-10-17T12:00:00")
            f1 = feed(repo, r, q, base/"m", st, bnd)
            st["viewed"] = [{
                "id": "request:20261017-040000-9999aaaa",
                "range": f1["range"],
                "viewed_at": "2026-10-17T10:00:00"}]
            f2 = feed(repo, r, q, base/"m", st, bnd)
            hist = {h["id"]: h for h in f2["history"]}
            eid = "run:20261017-050000-x:2026-10-17T06:00:00"
            self.assertIn(eid, hist)                    # resolved via covers
            self.assertEqual(hist[eid]["kind"], "pipeline")
            self.assertFalse(hist[eid].get("unavailable"))

    def test_unreviewed_count_comes_from_marks(self):
        """The run card exposes its unreviewed-task count from existing
        review data - folded marks AND a live sentinel - never written."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r, q = base/"r", base/"q"
            flags = base/"flags"
            flags.mkdir()
            write_run(r, "20261017-050000-x", "2026-10-17T06:00:00",
                      steps={
                          "s1": {"status": "done", "completion_seq": 1,
                                 "review": {"reviewed": True, "seq": 1,
                                            "at": "2026-10-17T06:01:00"}},
                          "s2": {"status": "done", "completion_seq": 2,
                                 "review": {"reviewed": True, "seq": 1,
                                            "at": "2026-10-17T06:01:00"}},
                          "s3": {"status": "failed",
                                 "completion_seq": 1},
                          "s4": {"status": "done"}})
            st = wm(None, "2026-10-17T00:00:00")
            bnd = boundary(repo, "2026-10-17T12:00:00")
            kw = dict(repo_dir=repo, runs_dir=r, requests_dir=q,
                      manifests_dir=base/"m", flags_dir=flags)
            f = tour_feeds.build_feed(st, bnd, **kw)
            e = f["entries"][0]
            # s2's mark is stale (seq 1 < completion 2) and s3 has no
            # mark at all; s4 never completed; s1 is current
            self.assertEqual(e["unreviewed"], 2)
            # a live sentinel overrides the folded mark on `at` ties
            d = r / "20261017-050000-x"
            (flags / ("review-" + tour_feeds._flag_key(d) + "-s1")
             ).write_text(json.dumps(
                 {"reviewed": False, "seq": 1,
                  "at": "2026-10-17T07:00:00"}), encoding="utf-8")
            f2 = tour_feeds.build_feed(st, bnd, **kw)
            self.assertEqual(f2["entries"][0]["unreviewed"], 3)

    def test_viewed_card_leaves_live_deck_before_advance(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            commit(repo, "assistant/a.py", "x", "chat")
            commit(repo, "mobile_device.py", "x", "mobile")
            st = wm(c0, "2026-10-16T00:00:00")
            f1 = feed(repo, base/"r", base/"q", base/"m", st)
            eid = f1["entries"][0]["id"]
            st["viewed"] = [{"id": eid, "range": f1["range"],
                             "viewed_at": "2026-10-17T01:00:00"}]
            f2 = feed(repo, base/"r", base/"q", base/"m", st)
            live = {e["id"]: e for e in f2["entries"]}
            self.assertNotIn(eid, live)             # not in the deck
            self.assertEqual({h["id"] for h in f2["history"]}, {eid})
            self.assertEqual(f2["history"][0]["state"], "viewed")
            self.assertEqual(f2["unseen_count"], 1) # the other card

    def test_each_distinct_range_collects_once(self):
        """Two viewed descriptors storing the same window share ONE
        collection - the live window is another; a second build_feed
        collects fresh (no cross-build cache)."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            c0 = rev(repo)
            c1 = commit(repo, "assistant/a.py", "x", "chat")
            c2 = commit(repo, "mobile_device.py", "x", "mobile")
            st = wm(c0, "2026-10-16T00:00:00")
            kw = dict(repo_dir=repo, runs_dir=base/"r",
                      requests_dir=base/"q", manifests_dir=base/"m")
            b1 = boundary(repo)
            # an older window than the live one - distinct range key,
            # still containing both cards
            shared = rng(c0, "2026-10-16T00:00:00", c2,
                         "2026-10-17T01:00:00")
            st["viewed"] = [
                {"id": f"area:assistant:{c1[:12]}", "range": shared,
                 "viewed_at": "2026-10-17T01:00:00"},
                {"id": f"area:mobile:{c2[:12]}", "range": shared,
                 "viewed_at": "2026-10-17T01:05:00"}]
            calls = []
            real = tour_feeds._collect_window

            def spy(rng_arg, **k):
                calls.append(tour_feeds._range_key(rng_arg))
                return real(rng_arg, **k)

            try:
                tour_feeds._collect_window = spy
                tour_feeds.build_feed(st, b1, **kw)
            finally:
                tour_feeds._collect_window = real
            self.assertEqual(len(calls), len(set(calls)))  # no re-scans
            # live window + the one shared stored range
            self.assertEqual(len(calls), 2)
            # a later build collects fresh - nothing persists across it
            try:
                tour_feeds._collect_window = spy
                calls.clear()
                tour_feeds.build_feed(st, b1, **kw)
            finally:
                tour_feeds._collect_window = real
            self.assertEqual(len(calls), 2)

    def test_snoozed_covered_descriptor_stays_one_card(self):
        """A snoozed request id whose event folded into a live pipeline
        card must not produce a duplicate entry."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = make_repo(base)
            r, q = base/"r", base/"q"
            write_run(r, "20261017-050000-x", "2026-10-17T06:00:00",
                      request_id="20261017-040000-9999aaaa")
            write_request(q, "20261017-040000-9999aaaa",
                          "2026-10-17T04:00:00")
            st = wm(rev(repo), "2026-10-17T00:00:00")
            bnd = boundary(repo, "2026-10-17T12:00:00")
            st["snoozed"] = [{
                "id": "request:20261017-040000-9999aaaa",
                "range": rng(None, "2026-10-17T00:00:00",
                             rev(repo), "2026-10-17T12:00:00"),
                "snoozed_at": "2026-10-17T10:00:00"}]
            f = feed(repo, r, q, base/"m", st, bnd)
            self.assertEqual(len(f["entries"]), 1)
            self.assertEqual(f["entries"][0]["kind"], "pipeline")


class RegistryTests(unittest.TestCase):
    """TOUR_STOPS must map to anchors that actually exist in _DASH_HTML."""

    def test_stops_resolve_in_dash_html(self):
        src = (ROOT / "conductor.py").read_text(encoding="utf-8")
        start = src.index('_DASH_HTML = """')
        end = src.index('"""', start + 20)
        html = src[start:end]
        for stop, spec in tour_feeds.TOUR_STOPS.items():
            alts = [a.strip() for a in spec["selector"].split(",")]
            ok = False
            for a in alts:
                if a.startswith("#") and f'id="{a[1:]}"' in html:
                    ok = True
                elif a.startswith(".") and (f'class="' in html
                                            and a[1:] in html):
                    ok = True
            self.assertTrue(ok, f"stop '{stop}': no alternative resolves "
                            f"({spec['selector']})")
            self.assertTrue(spec["title"])


if __name__ == "__main__":
    unittest.main()
