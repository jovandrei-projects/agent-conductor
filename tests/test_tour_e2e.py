"""End-to-end tests for the assembled 'What's new' tour: the full
lifecycle over REAL HTTP against a fixture repo + isolated data dirs -
git/record discovery, curated preference, transitions, watermark
advancement, restart persistence, the ~60s feed cache, mutation/count
consistency (including a mutating POST racing an in-flight background
build), enrichment range keying, and no-LLM default operation.

Convention: C:\\Projects\\TESTING.md - real git fixture repos, a real
ThreadingHTTPServer on 127.0.0.1:0, no network, no LLM (the polish
provider seam is a fake callable; enrichment is forced off unless a
test opts in).
"""
import http.client
import json
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
import tour_feeds  # noqa: E402
import tour_polish  # noqa: E402


def git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t",
         "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True)


def rev(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def commit(repo: Path, path: str, content: str, subject: str) -> str:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", subject)
    return rev(repo)


def _poll(pred, timeout=8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class FakeProvider:
    """(prompt)->str seam for tour_polish: records prompts, replies
    canned or raises."""

    def __init__(self, reply="", exc=None):
        self.calls: list[str] = []
        self.reply = reply
        self.exc = exc

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.reply(prompt) if callable(self.reply) else self.reply


class TourE2ETests(unittest.TestCase):
    """The assembled walkthrough, exercised end to end over HTTP.

    Fixture repo (watermark = c0):
      c_power  conductor.py  'Improve power dialog'      -> power card
      c_chat   assistant/    'Add assistant chat panel'  -> assistant
               card; a curated whatsnew.jsonl record folds its human
               blurb into it
      c_tour   tour_state.py 'Add tour state'            -> tour card
      c_mobile mobile_device.py 'Fix mobile relaunch'    -> mobile card
      branch conductor/<run>/s1 merged back              -> attributed to
               the finished-run card below, not a separate change card
    Records: one request (rid1), one run that launched it and finished
    in-window (its card absorbs the merge chain AND the request +
    follow-up events - same-window work folds into one pipeline card),
    one follow-up request (rid2) registered from that run. Five new
    cards total.
    """

    RUN_DIR = "20261017-020000-somename"
    RID1 = "20261017-010000-aaaabbbb"
    RID2 = "20261017-030000-ccccdddd"
    N_CARDS = 5

    def setUp(self):
        # the shipped default: no env opt-in -> enrichment stays off;
        # tests that exercise it inject an enabled Polisher explicitly
        self._enabled_patch = mock.patch.object(tour_polish, "ENABLED",
                                                False)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)

        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        base = Path(self._td.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        (self.repo / "f.txt").write_text("0\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "init")
        self.c0 = rev(self.repo)
        self.main_br = git(self.repo, "rev-parse",
                           "--abbrev-ref", "HEAD").stdout.strip()
        self.c_power = commit(self.repo, "conductor.py", "x",
                              "Improve power dialog")
        self.c_chat = commit(self.repo, "assistant/chat.py", "x",
                             "Add assistant chat panel")
        self.c_tour = commit(self.repo, "tour_state.py", "x",
                             "Add tour state")
        self.c_mobile = commit(self.repo, "mobile_device.py", "x",
                               "Fix mobile relaunch guard")
        # a finished run's worktree branch merged back - its merge commit
        # + chain belong to the run card, not to the area grouping
        git(self.repo, "checkout", "-b",
            f"conductor/{self.RUN_DIR}/s1")
        commit(self.repo, "conductor.py", "y", "Runs table tweak")
        self.c_chain = rev(self.repo)
        git(self.repo, "checkout", self.main_br)
        git(self.repo, "merge", "--no-ff",
            f"conductor/{self.RUN_DIR}/s1")
        self.c_merge = rev(self.repo)
        self.tip = self.c_merge

        self.data = base / "data"
        self.runs = self.data / "runs"
        self.requests = self.data / "requests"
        self.manifests = base / "manifests"
        for d in (self.runs, self.requests, self.manifests):
            d.mkdir(parents=True)
        self.state_path = self.data / "tour-state.json"
        self.cache_path = self.data / "tour-cache.json"

        (self.manifests / "whatsnew.jsonl").write_text(json.dumps({
            "stop": "assistant",
            "blurb": "CURATED assistant words",
            "commits": [self.c_chat[:8]]}) + "\n", encoding="utf-8")
        (self.requests / f"{self.RID1}.json").write_text(json.dumps(
            {"id": self.RID1, "status": "ready", "label": "req one",
             "created_at": "2026-10-17T01:00:00"}), encoding="utf-8")
        rd = self.runs / self.RUN_DIR
        rd.mkdir(parents=True)
        (rd / "state.json").write_text(json.dumps(
            {"run": "somename", "request_id": self.RID1,
             "ended_at": "2026-10-17T02:00:00",
             "steps": {"s1": {"status": "done"},
                       "s2": {"status": "failed"}}}), encoding="utf-8")
        (self.requests / f"{self.RID2}.json").write_text(json.dumps(
            {"id": self.RID2, "status": "ready", "label": "follow up",
             "source_run": self.RUN_DIR,
             "created_at": "2026-10-17T03:00:00"}), encoding="utf-8")

        self.state_path.write_text(json.dumps(
            {"watermark": {"commit": self.c0,
                           "at": "2020-01-01T00:00:00",
                           "origin": "advance"},
             "viewed": [], "snoozed": []}), encoding="utf-8")
        self._serve()

    def _serve(self):
        """A real dashboard server on the fixture dirs - a second call
        with the same dirs is a restart (fresh tour_ctx, same state
        file)."""
        handler = conductor.make_dashboard_handler(
            self.runs, ["no-such-devin"], self.requests, self.manifests,
            tour_repo=self.repo)
        self.tour = handler.tour_ctx
        self.server = conductor.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def restart(self):
        self.server.shutdown()
        self.server.server_close()
        self._serve()

    # ----------------------------------------------------------- http

    def call(self, method, path, data=None, ctype="application/json"):
        # generous timeout - the assertions carry the contract; the box
        # is shared and git spawns can queue behind other agents
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=60)
        conn.request(method, path,
                     json.dumps(data).encode("utf-8")
                     if data is not None else None,
                     {"Content-Type": ctype,
                      "Origin": f"http://127.0.0.1:{self.port}"})
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            return res.status, json.loads(raw)
        except ValueError:
            return res.status, raw

    def get(self, path="/api/tour"):
        s, d = self.call("GET", path)
        self.assertEqual(s, 200, d)
        return d

    def post(self, entry_id, action):
        return self.call("POST", f"/api/tour/{entry_id}/{action}", {})

    def ids(self, payload=None):
        return {e["id"]: e
                for e in (payload or self.get())["entries"]}

    def state_file(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def expire_cache(self):
        """Force the ~60s feed cache to look stale (None is already a
        miss)."""
        if self.tour["feed"] is not None:
            self.tour["feed"]["at"] -= conductor.TOUR_FEED_TTL_S + 1

    def poll_status(self, want, timeout=8.0):
        """Poll GET /api/tour until enrichment.status == want (the cache
        is expired first so a fresh build answers)."""
        def probe():
            self.expire_cache()
            feed = self.get()
            return feed.get("enrichment", {}).get("status") == want
        return _poll(probe, timeout)

    # ------------------------------------------------------ lifecycle

    def test_full_lifecycle_over_http(self):
        """Discover git + record changes end to end: area cards, a
        curated blurb winning over the commit subject, the run card
        absorbing its merge chain, the request + follow-up cards - then
        view/snooze/unview the whole deck."""
        feed = self.get()
        ids = self.ids(feed)

        power = ids[f"area:power:{self.c_power[:12]}"]
        self.assertEqual(power["anchor"], "#power-btn")
        self.assertEqual(power["tag"], "improved")
        self.assertEqual(power["state"], "new")
        tour = ids[f"area:tour:{self.c_tour[:12]}"]
        self.assertEqual(tour["anchor"], "#tour-btn, #power-btn")
        mobile = ids[f"area:mobile:{self.c_mobile[:12]}"]
        self.assertIsNone(mobile["anchor"])           # unanchored area
        self.assertEqual(mobile["tag"], "fixed")

        # curated text wins over the commit subject, in place
        chat = ids[f"area:assistant:{self.c_chat[:12]}"]
        self.assertEqual(chat["blurb"], "CURATED assistant words")
        self.assertEqual(chat["anchor"], "#dask")

        # the finished run's card absorbed its merge commit + chain
        run = ids[f"run:{self.RUN_DIR}:2026-10-17T02:00:00"]
        self.assertEqual(run["runs"], [f"request:{self.RID1}"])
        self.assertEqual(run["requests"], [self.RID1])
        self.assertIn({"sha": self.c_merge,
                       "subject": "Merge branch "
                                  f"'conductor/{self.RUN_DIR}/s1'"},
                      run["commits"])
        self.assertIn(self.c_chain,
                      {c["sha"] for c in run["commits"]})
        # ...so the merge produces no separate change card
        self.assertFalse(any(self.c_merge[:12] in k for k in ids))

        # the request + follow-up folded into the run's work card (all
        # three events share the window)
        self.assertEqual(run["kind"], "pipeline")
        self.assertEqual(run["section"], "work")
        self.assertEqual(run["tag"], "failed")      # s2 failed
        self.assertEqual(run["outcome"], "failed")
        self.assertEqual(run["proposed_at"], "2026-10-17T01:00:00")
        self.assertEqual(run["steps_done"], 1)
        self.assertEqual(run["steps_total"], 2)
        self.assertEqual({c for c in run["covers"]},
                         {f"request:{self.RID1}", f"followup:{self.RID2}"})
        fu = run["followups"][0]
        self.assertEqual(fu["id"], f"followup:{self.RID2}")
        self.assertEqual(fu["requests"], [self.RID2])
        self.assertNotIn(f"request:{self.RID1}", ids)
        self.assertNotIn(f"followup:{self.RID2}", ids)

        # open/dismiss is read-only - the state file's bytes are
        # identical after any number of GETs
        self.assertEqual(feed["unseen_count"], self.N_CARDS)
        before = self.state_path.read_bytes()
        self.get(); self.call("GET", "/api/state")
        time.sleep(0.4)                    # let the bg refresh land
        self.assertEqual(self.state_path.read_bytes(), before)

        # snooze one (deferred, never counts as 'new'), view the rest
        # one by one; only the last 'new' clears the way
        snoozed = f"area:mobile:{self.c_mobile[:12]}"
        s, d = self.post(snoozed, "snooze")
        self.assertEqual(d["state"], "snoozed")
        ordered = [k for k in ids if k != snoozed]
        for k in ordered[:-1]:
            s, d = self.post(k, "viewed")
            self.assertEqual(s, 200, d)
            self.assertFalse(d["advanced"], k)
        s, d = self.post(ordered[-1], "viewed")
        self.assertEqual(s, 200, d)
        self.assertTrue(d["advanced"], d)   # snoozed doesn't pin it
        self.assertEqual(self.state_file()["watermark"]["commit"],
                         self.tip)          # lands at the boundary

        # the viewed section reopened behind the watermark
        feed = self.get()
        self.assertEqual(feed["unseen_count"], 0)
        hist = {h["id"] for h in feed["history"]}
        self.assertTrue(set(ordered) <= hist)
        # the snoozed card is still retrievable, never acknowledged
        e = self.ids(feed)[snoozed]
        self.assertEqual(e["state"], "snoozed")

        # unview a viewed entry behind the watermark: it resurfaces 'new'
        s, d = self.post(ordered[0], "unview")
        self.assertEqual(d["unseen_count"], 1)
        self.assertEqual(self.ids()[ordered[0]]["state"], "new")
        # re-marking it clears the deck again
        s, d = self.post(ordered[0], "viewed")
        self.assertTrue(d["advanced"])
        self.assertEqual(d["unseen_count"], 0)

    def test_change_during_acknowledgment_stays_unacknowledged(self):
        """A commit landing while the user is marking cards joins the
        deck as 'new': it re-groups the pending power card to a NEW id
        (the old id 404s - it no longer exists), the unread guard blocks
        the watermark advance, and acknowledging the renamed card lands
        the watermark on a boundary that includes it."""
        feed = self.get()                          # boundary = self.tip
        self.assertEqual(feed["boundary"]["commit"], self.tip)
        old_ids = list(self.ids(feed))
        c_late = commit(self.repo, "conductor.py", "z",
                        "Improve power dialog once more")
        gone, last = [], None
        for k in old_ids:
            s, d = self.post(k, "viewed")
            if s == 404:
                gone.append(k)          # renamed under us: the late
                continue                # commit re-grouped the card
            self.assertEqual(s, 200, d)
            last = d
        # only the power card disappeared; every mark that landed still
        # saw the renamed card as 'new' and refused to advance
        self.assertEqual(gone, [f"area:power:{self.c_power[:12]}"])
        self.assertIsNotNone(last)
        self.assertFalse(last["advanced"])
        self.assertEqual(self.state_file()["watermark"]["commit"],
                         self.c0)            # never skipped over it

        feed = self.get()                    # rebuilt after the marks
        new_id = f"area:power:{c_late[:12]}"
        fresh = self.ids(feed)[new_id]
        self.assertEqual(fresh["state"], "new")
        # the renamed card carries BOTH commits - the previously-viewed
        # area could not hide the new commit behind the old id
        self.assertNotIn(new_id, old_ids)
        self.assertEqual({c["sha"] for c in fresh["commits"]},
                         {self.c_power, c_late})
        self.assertEqual(feed["unseen_count"], 1)

        s, d = self.post(new_id, "viewed")
        self.assertTrue(d["advanced"], d)
        self.assertEqual(self.state_file()["watermark"]["commit"],
                         c_late)
        self.assertEqual(d["unseen_count"], 0)

    def test_restart_keeps_unread_and_viewed_intact(self):
        """State lives in data/tour-state.json, not the serve: a fresh
        handler on the same dirs re-derives the identical view - a
        restart neither acknowledges nor erases anything."""
        feed = self.get()
        ids = self.ids(feed)
        s, d = self.post(f"area:power:{self.c_power[:12]}", "viewed")
        self.assertFalse(d["advanced"])
        self.post(f"area:mobile:{self.c_mobile[:12]}", "snooze")
        st_bytes = self.state_path.read_bytes()

        self.restart()                             # new ctx, same file
        self.assertEqual(self.state_path.read_bytes(), st_bytes)

        feed = self.get()
        self.assertEqual(feed["unseen_count"], self.N_CARDS - 2)
        e = self.ids(feed)
        # viewed cards leave the live deck - readable back via history
        self.assertNotIn(f"area:power:{self.c_power[:12]}", e)
        self.assertEqual(
            {h["id"] for h in feed["history"]},
            {f"area:power:{self.c_power[:12]}"})
        self.assertEqual(e[f"area:mobile:{self.c_mobile[:12]}"]["state"],
                         "snoozed")
        # a restart never moved the watermark
        self.assertEqual(self.state_file()["watermark"]["commit"],
                         self.c0)

    # ------------------------------------------- consistency + weight

    def test_state_poll_and_paint_never_wait_on_feed(self):
        """The ~60s feed build never rides /api/state or the page:
        state answers with tour_unseen=null while the daemon build is
        still gated, and / serves the dashboard HTML immediately."""
        gate = threading.Event()
        entered = threading.Event()
        real = tour_feeds.build_feed
        first = [True]

        def slow(*a, **kw):
            if first[0]:
                first[0] = False
                entered.set()
                gate.wait(10)
            return real(*a, **kw)

        with mock.patch.object(tour_feeds, "build_feed",
                               side_effect=slow):
            t0 = time.time()
            s, d = self.call("GET", "/api/state")
            self.assertEqual(s, 200, d)
            self.assertLess(time.time() - t0, 5)
            self.assertIsNone(d["tour_unseen"])    # unknown, not a lie
            self.assertTrue(entered.wait(5))       # daemon mid-build
            # the page itself is a static string - it paints while the
            # feed is still being computed
            s, html = self.call("GET", "/")
            self.assertEqual(s, 200)
            self.assertIn(b'id="tour-btn"', html)
            self.assertIn(b'id="tour-badge"', html)
            self.assertIn(b'id="tourwrap"', html)
            gate.set()
            self.assertTrue(_poll(
                lambda: self.tour["count"] is not None))
            s, d = self.call("GET", "/api/state")
            self.assertEqual(d["tour_unseen"], self.N_CARDS)

    def test_mutation_wins_over_inflight_daemon_build(self):
        """A background feed build that read PRE-mutation state must
        never overwrite the post-mutation cache: after the daemon lands
        late, /api/state and /api/tour still reflect the mark."""
        gate = threading.Event()
        entered = threading.Event()
        real = tour_feeds.build_feed
        first = [True]

        def slow(*a, **kw):
            if first[0]:
                first[0] = False
                entered.set()
                gate.wait(10)
            return real(*a, **kw)

        eid = f"area:power:{self.c_power[:12]}"
        with mock.patch.object(tour_feeds, "build_feed",
                               side_effect=slow):
            s, _ = self.call("GET", "/api/state")  # arms the daemon build
            self.assertEqual(s, 200)
            self.assertTrue(entered.wait(5))       # it holds S0 in flight
            s, d = self.post(eid, "viewed")        # mutation + rebuild
            self.assertEqual(s, 200, d)
            self.assertEqual(d["unseen_count"], self.N_CARDS - 1)
            gate.set()                             # stale build lands now
            self.assertTrue(_poll(lambda: not self.tour["busy"]))
            # the late S0 write must not resurrect a stale count or state
            s, d = self.call("GET", "/api/state")
            self.assertEqual(d["tour_unseen"], self.N_CARDS - 1)
            feed = self.get()
            self.assertNotIn(eid, {e["id"] for e in feed["entries"]})
            self.assertEqual({h["id"] for h in feed["history"]}, {eid})
            self.assertEqual(feed["unseen_count"], self.N_CARDS - 1)

    def test_mutations_are_immediately_consistent(self):
        """Within the TTL, every transition drops both caches and
        rebuilds once - responses and the count are never pre-mutation."""
        self.get()                                    # warm the cache
        eid = f"area:power:{self.c_power[:12]}"
        s, d = self.post(eid, "snooze")
        self.assertEqual(d["unseen_count"], self.N_CARDS - 1)
        self.assertEqual(self.ids()[eid]["state"], "snoozed")
        s, d = self.post(eid, "unview")
        self.assertEqual(d["unseen_count"], self.N_CARDS)
        self.assertEqual(self.ids()[eid]["state"], "new")

    # ------------------------------------------------------ enrichment

    def test_enrichment_disabled_by_default_over_http(self):
        """No env opt-in: the tour serves deterministic cards, reports
        enrichment disabled, never calls a provider, never writes the
        cache file."""
        self.tour.pop("polisher", None)   # force lazy construction
        feed = self.get()
        self.assertEqual(feed["enrichment"], {"enabled": False})
        self.assertEqual(len(feed["entries"]), self.N_CARDS)
        self.assertFalse(self.cache_path.exists())
        # transitions are equally LLM-free
        s, d = self.post(f"area:power:{self.c_power[:12]}", "viewed")
        self.assertEqual(s, 200, d)
        self.assertFalse(self.cache_path.exists())

    def test_enrichment_range_keying_and_fallback_over_http(self):
        """Enabled + mocked: an uncached range is deterministic while the
        background attempt lands; the ok record polishes the next build;
        a new tip means a new range key and a new attempt; a provider
        failure persists 'fallback' and is never retried."""
        reply = {"cards": [
            {"stop": "power", "title": "Power & chat, polished",
             "blurb": "Grouped by the model.", "tag": "improved",
             "commits": [self.c_power[:8], self.c_chat[:8]]}]}
        fp = FakeProvider(reply=json.dumps(reply))
        pol = tour_polish.Polisher(self.cache_path, self.repo,
                                   provider=fp, enabled=True)
        self.tour["polisher"] = pol

        feed = self.get()                       # uncached: deterministic
        key1 = pol.range_key(feed["range"])
        self.assertTrue(feed["enrichment"]["enabled"])
        self.assertEqual(feed["enrichment"]["status"], "pending")
        self.assertIn(f"area:power:{self.c_power[:12]}", self.ids(feed))
        self.assertTrue(_poll(
            lambda: pol.record(feed["range"]) is not None))
        self.assertEqual(len(fp.calls), 1)

        self.assertTrue(self.poll_status("ok"))
        feed = self.get()                        # polished now
        changes = [e for e in feed["entries"] if e["kind"] == "change"]
        # power+chat claimed into ONE polished card, the rest stay
        polished = [e for e in changes if e.get("polished")]
        self.assertEqual(len(polished), 1)
        self.assertEqual(polished[0]["id"],
                         f"area:power:{self.c_chat[:12]}")
        self.assertEqual(polished[0]["title"], "Power & chat, polished")
        # the curated record named the assistant stop but polish grouped
        # its commit under 'power' - the human blurb still wins
        self.assertEqual(polished[0]["blurb"], "CURATED assistant words")
        self.assertIn(f"area:tour:{self.c_tour[:12]}",
                      {e["id"] for e in changes})
        self.assertEqual(len(fp.calls), 1)       # same range: no re-call

        # a moved tip is a different range key -> one new attempt
        commit(self.repo, "conductor.py", "z",
               "Improve power dialog again")
        self.assertTrue(self.poll_status("pending"))
        feed = self.get()
        self.assertNotEqual(pol.range_key(feed["range"]), key1)
        self.assertTrue(_poll(lambda: len(fp.calls) == 2))
        self.assertTrue(_poll(
            lambda: pol.record(feed["range"]) is not None))
        doc = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(len(doc["ranges"]), 2)

        # provider failure -> persisted 'fallback' -> deterministic cards
        # and NO further attempts for that range
        fp.exc = RuntimeError("provider down")
        fp.reply = ""
        c_newer = commit(self.repo, "conductor.py", "w",
                         "Improve power dialog yet again")
        self.assertTrue(self.poll_status("pending"))
        feed = self.get()
        rng3 = feed["range"]
        self.assertTrue(_poll(lambda: len(fp.calls) == 3))
        self.assertTrue(_poll(lambda: pol.record(rng3) is not None))
        self.assertTrue(self.poll_status("fallback"))
        feed = self.get()
        self.assertIn(f"area:power:{c_newer[:12]}", self.ids(feed))
        self.expire_cache()
        self.get(); self.get()
        self.assertEqual(len(fp.calls), 3)       # failures don't retry

        # deterministic feed worked throughout and GETs never wrote
        # tour state (the watermark is still the seeded one)
        self.assertEqual(self.state_file()["watermark"]["commit"],
                         self.c0)


if __name__ == "__main__":
    unittest.main()
