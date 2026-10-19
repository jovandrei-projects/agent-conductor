"""Tests for the 'What's new' tour HTTP layer in conductor.py:
GET /api/tour, POST /api/tour/<id>/{viewed,snooze,unview}, the ~60s
feed cache, and the tiny tour_unseen field on /api/state.

Convention: C:\\Projects\\TESTING.md - real git fixture repos, a real
ThreadingHTTPServer on 127.0.0.1:0, no network, no LLM.
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
import tour_state  # noqa: E402

TAGS = ("new", "improved", "fixed")
STATES = ("new", "viewed", "snoozed")


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


def _poll(pred, timeout=5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TourApiTests(unittest.TestCase):
    """Endpoint tests over a real dashboard handler. The fixture repo
    carries an init commit (watermarked) plus two feature commits ->
    exactly two 'new' area cards ('power' improved, 'assistant' new)
    plus any record cards a test writes."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        base = Path(self._td.name)
        self.base = base
        self.repo = base / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        (self.repo / "f.txt").write_text("0\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "init")
        self.c0 = rev(self.repo)
        self.c_power = commit(self.repo, "conductor.py", "x",
                              "Improve power dialog")
        self.c_chat = commit(self.repo, "assistant/chat.py", "x",
                             "Add assistant chat panel")
        self.data = base / "data"
        self.runs = self.data / "runs"
        self.requests = self.data / "requests"
        self.manifests = base / "manifests"
        for d in (self.runs, self.requests, self.manifests):
            d.mkdir(parents=True)
        self.state_path = self.data / "tour-state.json"
        self.seed_state()
        handler = conductor.make_dashboard_handler(
            self.runs, ["no-such-devin"], self.requests, self.manifests,
            tour_repo=self.repo)
        self.tour = handler.tour_ctx
        self.server = conductor.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def seed_state(self, watermark=None, viewed=(), snoozed=()):
        """Write data/tour-state.json directly - the seeded watermark is
        the init commit + a long-ago `at`, so only the two feature
        commits (and windowed records) feed."""
        wm = watermark or {"commit": self.c0,
                           "at": "2020-01-01T00:00:00",
                           "origin": "advance"}
        self.state_path.write_text(json.dumps(
            {"watermark": wm, "viewed": list(viewed),
             "snoozed": list(snoozed)}), encoding="utf-8")

    def state_file(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def call(self, method, path, data=None, ctype="application/json",
             origin=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=10)
        headers = {"Content-Type": ctype,
                   "Origin": origin or f"http://127.0.0.1:{self.port}"}
        if host:
            headers["Host"] = host
        conn.request(method, path,
                     json.dumps(data).encode("utf-8")
                     if data is not None else None, headers)
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

    # ------------------------------------------------------- contract

    def test_response_contract(self):
        d = self.get()
        for k in ("entries", "history", "unseen_count"):
            self.assertIn(k, d)
        self.assertEqual(len(d["entries"]), 2)
        self.assertEqual(d["unseen_count"], 2)
        for e in d["entries"]:
            self.assertIn(e["tag"], TAGS)
            self.assertIn(e["state"], STATES)
            for k in ("id", "title", "blurb", "anchor", "commits",
                      "runs", "requests"):
                self.assertIn(k, e)
            for c in e["commits"]:
                self.assertEqual(sorted(c), ["sha", "subject"])
        ids = self.ids(d)
        power = ids[f"area:power:{self.c_power[:12]}"]
        self.assertEqual(power["anchor"], "#power-btn")
        self.assertEqual(power["tag"], "improved")
        self.assertEqual([c["sha"] for c in power["commits"]],
                         [self.c_power])
        chat = ids[f"area:assistant:{self.c_chat[:12]}"]
        self.assertEqual(chat["anchor"], "#dask")
        self.assertEqual(chat["tag"], "new")

    def test_records_and_ride_alongs(self):
        """A windowed request + finished run fold into one pipeline work
        card keyed on the run event (the proposal is covered, not a
        second card)."""
        rid = "20261017-010000-aaaabbbb"
        (self.requests / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "status": "ready", "label": "new stuff",
             "created_at": "2026-10-17T01:00:00"}), encoding="utf-8")
        rd = self.runs / "20261017-020000-somename"
        rd.mkdir(parents=True)
        (rd / "state.json").write_text(json.dumps(
            {"run": "somename", "request_id": rid,
             "ended_at": "2026-10-17T02:00:00", "steps": {}}),
            encoding="utf-8")
        ids = self.ids()
        self.assertNotIn(f"request:{rid}", ids)      # folded away
        run = ids["run:20261017-020000-somename:2026-10-17T02:00:00"]
        self.assertEqual(run["kind"], "pipeline")
        self.assertEqual(run["section"], "work")
        self.assertEqual(run["runs"], [f"request:{rid}"])
        self.assertEqual(run["requests"], [rid])
        self.assertIn(f"request:{rid}", run["covers"])
        self.assertEqual(run["outcome"], "ended")     # no steps recorded

    # ---------------------------------------------------- transitions

    def test_viewed_then_unview_round_trip(self):
        ids = self.ids()
        eid = f"area:power:{self.c_power[:12]}"
        s, d = self.post(eid, "viewed")
        self.assertEqual(s, 200, d)
        self.assertEqual(d["state"], "viewed")
        self.assertEqual(d["unseen_count"], 1)
        self.assertFalse(d["advanced"])          # one still 'new'
        # immediately consistent - no TTL wait; a viewed card leaves
        # the deck (only unviewed cards ride entries) and shows up in
        # the history section instead
        feed = self.get()
        self.assertNotIn(eid, self.ids(feed))
        self.assertEqual(
            {h["id"]: h["state"] for h in feed["history"]}[eid],
            "viewed")
        st = self.state_file()
        self.assertEqual([v["id"] for v in st["viewed"]], [eid])
        s, d = self.post(eid, "unview")
        self.assertEqual(s, 200, d)
        self.assertEqual(d["state"], "new")
        self.assertEqual(d["unseen_count"], 2)
        self.assertEqual(self.ids()[eid]["state"], "new")
        self.assertTrue(self.state_file()["viewed"][0].get("unviewed_at"))

    def test_snooze_defers_without_acknowledging(self):
        self.get()                          # the deck a client acks from
        eid = f"area:assistant:{self.c_chat[:12]}"
        s, d = self.post(eid, "snooze")
        self.assertEqual(s, 200, d)
        self.assertEqual(d["state"], "snoozed")
        self.assertEqual(d["unseen_count"], 1)   # snoozed isn't 'new'
        self.assertEqual(self.ids()[eid]["state"], "snoozed")
        st = self.state_file()
        self.assertEqual([x["id"] for x in st["snoozed"]], [eid])
        # a snooze never moves the watermark
        self.assertEqual(st["watermark"]["commit"], self.c0)

    def test_last_new_viewed_advances_watermark(self):
        ids = self.ids()
        s, d = self.post(f"area:power:{self.c_power[:12]}", "viewed")
        self.assertFalse(d["advanced"])
        s, d = self.post(f"area:assistant:{self.c_chat[:12]}", "viewed")
        self.assertEqual(s, 200, d)
        self.assertTrue(d["advanced"], d)
        # the watermark lands at the feed's captured boundary = HEAD
        st = self.state_file()
        self.assertEqual(st["watermark"]["commit"], rev(self.repo))
        self.assertEqual(st["watermark"]["origin"], "advance")
        # both entries moved to the viewed history the section needs
        feed = self.get()
        self.assertEqual(feed["unseen_count"], 0)
        self.assertEqual(feed["entries"], [])
        self.assertEqual({h["id"] for h in feed["history"]}, set(ids))
        for h in feed["history"]:
            self.assertEqual(h["state"], "viewed")
            self.assertTrue(h.get("viewed_at"))

    def test_snoozed_entry_does_not_pin_the_watermark(self):
        """Snooze one, view the other: the deferred card is not 'new'
        so nothing blocks the advance."""
        self.get()
        self.post(f"area:assistant:{self.c_chat[:12]}", "snooze")
        s, d = self.post(f"area:power:{self.c_power[:12]}", "viewed")
        self.assertTrue(d["advanced"], d)
        # ...but the snoozed card is still retrievable, not acked
        feed = self.get()
        e = self.ids(feed)[f"area:assistant:{self.c_chat[:12]}"]
        self.assertEqual(e["state"], "snoozed")

    def test_unview_after_advancement_resurfaces_new(self):
        """An entry behind the advanced watermark keeps its range, so
        unview re-injects it into entries and the unseen count."""
        self.get()
        for eid in (f"area:power:{self.c_power[:12]}",
                    f"area:assistant:{self.c_chat[:12]}"):
            self.post(eid, "viewed")
        self.assertEqual(self.get()["unseen_count"], 0)
        s, d = self.post(f"area:power:{self.c_power[:12]}", "unview")
        self.assertEqual(s, 200, d)
        self.assertEqual(d["unseen_count"], 1)
        e = self.ids()[f"area:power:{self.c_power[:12]}"]
        self.assertEqual(e["state"], "new")

    def test_new_commit_in_viewed_area_is_new_again(self):
        """A fresh change in an already-viewed area gets a NEW id - it
        can never silently inherit 'viewed'."""
        self.get()
        self.post(f"area:power:{self.c_power[:12]}", "viewed")
        c_new = commit(self.repo, "conductor.py", "y",
                       "Improve power dialog again")
        # the ~60s cache may still hold the pre-commit feed - expire it
        # the way the TTL would (the transition's refresh may already
        # have dropped it), then the fresh card surfaces
        if self.tour["feed"] is not None:
            self.tour["feed"]["at"] -= conductor.TOUR_FEED_TTL_S + 1
        ids = self.ids(self.call("GET", "/api/tour")[1])
        fresh = ids[f"area:power:{c_new[:12]}"]
        self.assertEqual(fresh["state"], "new")
        self.assertNotEqual(fresh["id"],
                            f"area:power:{self.c_power[:12]}")

    def test_viewed_idempotent_and_snooze_moves_lists(self):
        self.get()
        eid = f"area:power:{self.c_power[:12]}"
        self.post(eid, "viewed")
        s, d = self.post(eid, "viewed")          # re-mark: still one row
        self.assertEqual(s, 200, d)
        self.assertEqual(len(self.state_file()["viewed"]), 1)
        s, d = self.post(eid, "snooze")          # viewed -> snoozed
        self.assertEqual(d["state"], "snoozed")
        st = self.state_file()
        self.assertEqual(len(st["viewed"]), 0)
        self.assertEqual(len(st["snoozed"]), 1)

    # ------------------------------------------------- invalid inputs

    def test_invalid_requests_never_mutate(self):
        before = self.state_path.read_bytes()
        s, _ = self.post("no-such-entry", "viewed")
        self.assertEqual(s, 404)
        s, _ = self.post("no-such-entry", "snooze")
        self.assertEqual(s, 404)
        s, _ = self.call("POST", "/api/tour/x/obliterate", {})
        self.assertEqual(s, 404)                  # route whitelist
        s, _ = self.call("GET",
                         f"/api/tour/area:power:{self.c_power[:12]}/viewed")
        self.assertEqual(s, 404)                  # GET never transitions
        s, _ = self.call("POST",
                         f"/api/tour/area:power:{self.c_power[:12]}/viewed",
                         {}, ctype="text/plain")
        self.assertEqual(s, 415)                  # JSON body required
        s, _ = self.call("POST",
                         f"/api/tour/area:power:{self.c_power[:12]}/viewed",
                         {}, origin="http://evil.invalid")
        self.assertEqual(s, 403)                  # origin protection
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_get_is_read_only(self):
        """GET /api/tour + /api/state must not write the state file,
        mark entries, or move the watermark."""
        self.state_path.unlink()
        d = self.get()
        self.assertEqual(d["unseen_count"], 3)   # unbounded: init too
        self.assertFalse(self.state_path.exists())
        self.call("GET", "/api/state")
        time.sleep(0.3)                          # bg refresh completes
        self.assertFalse(self.state_path.exists())
        # with a seeded file: bytes identical after reads
        self.seed_state()
        before = self.state_path.read_bytes()
        self.get(); self.call("GET", "/api/state")
        self.assertEqual(self.state_path.read_bytes(), before)

    # --------------------------------------------------------- caching

    def test_feed_cached_then_expires(self):
        with mock.patch.object(tour_feeds, "build_feed",
                               wraps=tour_feeds.build_feed) as spy:
            d1 = self.get()
            d2 = self.get()
            self.assertEqual(d1["generated_at"], d2["generated_at"])
            self.assertEqual(spy.call_count, 1)  # second hit served cache
            self.tour["feed"]["at"] -= conductor.TOUR_FEED_TTL_S + 1
            self.get()
            self.assertEqual(spy.call_count, 2)

    def test_mutation_invalidates_without_waiting_for_ttl(self):
        eid = f"area:power:{self.c_power[:12]}"
        self.get()                                # warm the cache
        self.post(eid, "viewed")
        d = self.get()                            # still inside the TTL
        # the acknowledged card is out of the live deck, in history
        self.assertNotIn(eid, self.ids(d))
        self.assertEqual({h["id"] for h in d["history"]}, {eid})
        self.assertEqual(d["unseen_count"], 1)

    def test_transition_refreshes_cached_count(self):
        self.get()
        self.assertEqual(self.tour["count"]["value"], 2)
        self.post(f"area:power:{self.c_power[:12]}", "viewed")
        # the refresh is deferred: the cached count converges once the
        # daemon rebuild lands, not inside the POST
        self.assertTrue(_poll(
            lambda: self.tour["count"] is not None
            and self.tour["count"]["value"] == 1))

    # -------------------------------------------------- /api/state

    def test_state_field_is_tiny_and_eventually_filled(self):
        """The badge field rides /api/state: None while cold, an int
        once the daemon build lands - and /api/state itself never
        writes tour state."""
        s, d = self.call("GET", "/api/state")
        self.assertEqual(s, 200, d)
        self.assertIsNone(d["tour_unseen"])      # cold cache: unknown
        self.assertTrue(_poll(
            lambda: self.tour["count"] is not None))
        s, d = self.call("GET", "/api/state")
        self.assertEqual(d["tour_unseen"], 2)

    def test_state_never_waits_on_feed_collection(self):
        """A slow feed build must not stall the state poll - the
        response returns the last count while a daemon refreshes."""
        gate = threading.Event()
        real = tour_feeds.build_feed

        def slow(*a, **kw):
            gate.wait(10)
            return real(*a, **kw)

        with mock.patch.object(tour_feeds, "build_feed",
                               side_effect=slow) as spy:
            t0 = time.time()
            s, d = self.call("GET", "/api/state")
            self.assertEqual(s, 200, d)
            self.assertLess(time.time() - t0, 5)
            self.assertIsNone(d["tour_unseen"])
            # a second poll joins the same in-flight build, not another
            self.call("GET", "/api/state")
            gate.set()
            self.assertTrue(_poll(
                lambda: self.tour["count"] is not None))
            self.assertEqual(spy.call_count, 1)

    def test_count_updates_after_mutation_via_state_poll(self):
        self.get()
        self.assertEqual(self.call("GET", "/api/state")[1]
                         ["tour_unseen"], 2)
        self.post(f"area:power:{self.c_power[:12]}", "viewed")
        # the state poll reports the last-known count until the deferred
        # refresh lands, then converges on the post-mutation value
        self.assertTrue(_poll(
            lambda: self.call("GET", "/api/state")[1]
            ["tour_unseen"] == 1))

    def test_transition_result_carries_count(self):
        """The response's unseen_count is the count the retained feed
        implies under the just-saved state - last-known, not rebuilt."""
        self.get()
        s, d = self.post(f"area:power:{self.c_power[:12]}", "viewed")
        self.assertEqual(d["unseen_count"], 1)
        s, d = self.post("definitely-missing", "unview")
        self.assertEqual(s, 404)

    def test_transition_returns_before_refresh_lands(self):
        """The POST persists the mark and answers immediately - it never
        waits on the deferred feed rebuild; the count cache and the
        /api/state badge field converge once the daemon lands."""
        self.get()                                # a deck to ack from
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
            t0 = time.time()
            s, d = self.post(eid, "viewed")
            self.assertLess(time.time() - t0, 5)   # prompt return
            self.assertEqual(s, 200, d)
            self.assertEqual(d["state"], "viewed")
            self.assertEqual(d["unseen_count"], 1)   # last-known, not 0
            self.assertTrue(d["count_pending"])
            # the mark is durable even while the rebuild is still gated
            self.assertEqual(self.state_file()["viewed"][0]["id"], eid)
            self.assertTrue(entered.wait(5))          # daemon refreshing
            gate.set()
            self.assertTrue(_poll(
                lambda: self.tour["count"] is not None
                and self.tour["count"]["value"] == 1))
            self.assertEqual(self.call("GET", "/api/state")[1]
                             ["tour_unseen"], 1)

    def test_transition_on_cold_ctx_uses_descriptors(self):
        """No feed has ever built (a restart clears every cache, not the
        file): a recorded descriptor still transitions - the mark lands
        with a deferred (None) count and the daemon rebuild converges."""
        eid = f"area:power:{self.c_power[:12]}"
        self.seed_state(viewed=[{"id": eid, "range": {
            "base": {"commit": self.c0, "at": "2020-01-01T00:00:00"},
            "tip": {"commit": self.c_chat, "at": "2026-10-17T00:00:00"}},
            "viewed_at": "2026-10-17T01:00:00"}])
        s, d = self.post(eid, "unview")              # cold ctx, no feed
        self.assertEqual(s, 200, d)
        self.assertEqual(d["state"], "new")
        self.assertIsNone(d["unseen_count"])         # nothing built yet
        self.assertTrue(self.state_file()["viewed"][0]
                        .get("unviewed_at"))
        self.assertTrue(_poll(                       # converges on refresh
            lambda: self.tour["count"] is not None
            and self.tour["count"]["value"] == 2))   # both cards 'new'

    def test_concurrent_transitions_coalesce_refresh(self):
        """Two marks landing while the deferred rebuild is gated: both
        persist (serialized updates lose nothing), the in-flight
        pre-mutation build can never store over the post-mutation
        state, and exactly ONE wanted re-run follows - never a build
        per transition."""
        e1 = f"area:power:{self.c_power[:12]}"
        e2 = f"area:assistant:{self.c_chat[:12]}"
        self.get()                          # the deck both acks see
        gate, entered = threading.Event(), threading.Event()
        real = tour_feeds.build_feed
        first = [True]

        def slow(*a, **kw):
            if first[0]:
                first[0] = False
                entered.set()
                gate.wait(10)
            return real(*a, **kw)

        self.addCleanup(gate.set)   # a failed assert never parks a daemon
        with mock.patch.object(tour_feeds, "build_feed",
                               side_effect=slow) as spy:
            results = {}

            def mark(eid):
                results[eid] = self.post(eid, "viewed")

            threads = [threading.Thread(target=mark, args=(e,))
                       for e in (e1, e2)]
            for t in threads:
                t.start()
            self.assertTrue(entered.wait(5))        # daemon mid-build
            for t in threads:
                t.join(10)
            for eid in (e1, e2):
                s, d = results[eid]
                self.assertEqual(s, 200, d)
                self.assertEqual(d["state"], "viewed")
            st = self.state_file()
            self.assertEqual({v["id"] for v in st["viewed"]}, {e1, e2})
            gate.set()
            # the gated pre-mutation build + exactly one coalesced
            # rerun - then busy and wanted both clear
            self.assertTrue(_poll(lambda: not self.tour["busy"]))
            self.assertEqual(spy.call_count, 2)
            self.assertFalse(self.tour["wanted"])
            # converged caches reflect BOTH marks - the stale build's
            # payload never overwrote the post-mutation state
            self.assertEqual(self.tour["count"]["value"], 0)
            s, d = self.call("GET", "/api/state")
            self.assertEqual(d["tour_unseen"], 0)
            self.assertEqual(self.get()["unseen_count"], 0)
            # the last 'new' mark moved the watermark to the payload's
            # captured boundary
            self.assertEqual(self.state_file()["watermark"]["commit"],
                             rev(self.repo))

    def test_daemon_refresh_failure_recovers(self):
        """A raised build inside the daemon refresh only logs: the
        persisted mark stands, busy clears instead of wedging, the
        response still answers from last-known state, and the next
        poll's re-armed build converges the caches."""
        self.get()                                # warm feed + count = 2
        calls = [0]
        failed = threading.Event()
        real = tour_feeds.build_feed

        def flaky(*a, **kw):
            calls[0] += 1
            if calls[0] == 1:
                failed.set()
                raise RuntimeError("simulated feed failure")
            return real(*a, **kw)

        eid = f"area:power:{self.c_power[:12]}"
        with mock.patch.object(tour_feeds, "build_feed",
                               side_effect=flaky):
            s, d = self.post(eid, "viewed")
            self.assertEqual(s, 200, d)
            self.assertEqual(d["state"], "viewed")
            # last-known count under the just-saved mark + the deferred
            # marker - not a fresh collection
            self.assertEqual(d["unseen_count"], 1)
            self.assertTrue(d["count_pending"])
            self.assertTrue(failed.wait(5))        # the daemon ran+failed
            self.assertTrue(_poll(lambda: not self.tour["busy"]))
            # the mark stands - a refresh failure never un-saves it
            self.assertEqual(self.state_file()["viewed"][0]["id"], eid)
            s, d = self.call("GET", "/api/state")
            # still the count the warm build computed - honest
            # last-known, not a fake 0
            self.assertEqual(d["tour_unseen"], 2)
            # the same call re-armed a build; it recovers and converges
            self.assertTrue(_poll(
                lambda: self.tour["count"] is not None
                and self.tour["count"]["value"] == 1))
            self.assertEqual(calls[0], 2)
            feed = self.get()                       # served from cache
            self.assertNotIn(eid, self.ids(feed))
            self.assertEqual({h["id"] for h in feed["history"]}, {eid})
            self.assertEqual(feed["unseen_count"], 1)
            self.assertEqual(calls[0], 2)

    def test_mark_viewed_never_touches_task_review(self):
        """Acknowledging a run's work card is notification-only: the
        run's state.json is byte-identical after the POST (no folded
        review mark) and no review-* sentinel lands for the run."""
        rd = self.runs / "20261017-020000-rev"
        rd.mkdir(parents=True)
        (rd / "state.json").write_text(json.dumps(
            {"run": "rev", "ended_at": "2026-10-17T02:00:00",
             "steps": {"s1": {"status": "done", "completion_seq": 1},
                       "s2": {"status": "done", "completion_seq": 1}}}),
            encoding="utf-8")
        feed = self.get()
        eid = "run:20261017-020000-rev:2026-10-17T02:00:00"
        card = self.ids(feed)[eid]
        self.assertEqual(card["kind"], "run")
        self.assertEqual(card["section"], "work")
        self.assertEqual(card["unreviewed"], 2)      # counted, read-only
        before = (rd / "state.json").read_bytes()
        s, d = self.post(eid, "viewed")
        self.assertEqual(s, 200, d)
        self.assertEqual(d["state"], "viewed")
        self.assertEqual(d["unseen_count"], 2)   # the two tool cards
        # notification state changed; task review state did not
        self.assertEqual(self.state_file()["viewed"][0]["id"], eid)
        self.assertEqual((rd / "state.json").read_bytes(), before)
        key = tour_feeds._flag_key(rd)
        flag_dir = tour_feeds.DEFAULT_FLAGS_DIR
        hits = list(flag_dir.glob(f"review-{key}*")) \
            if flag_dir.exists() else []
        self.assertEqual(hits, [])

    def test_state_file_stays_single_shape(self):
        """The persisted contract is one file with exactly
        {watermark, viewed[], snoozed[]} - descriptors carry only
        id/range/stamp fields; no per-section state ever appears."""
        self.get()
        self.post(f"area:power:{self.c_power[:12]}", "viewed")
        self.post(f"area:assistant:{self.c_chat[:12]}", "snooze")
        st = self.state_file()
        self.assertEqual(set(st), {"watermark", "viewed", "snoozed"})
        self.assertEqual(set(st["watermark"]), {"commit", "at",
                                               "origin"})
        allowed = {"id", "range", "viewed_at", "snoozed_at",
                   "unviewed_at"}
        for d in st["viewed"] + st["snoozed"]:
            self.assertLessEqual(set(d), allowed)
            self.assertEqual(set(d["range"]), {"base", "tip"})
            self.assertEqual(set(d["range"]["base"]), {"commit", "at"})
            self.assertEqual(set(d["range"]["tip"]), {"commit", "at"})

    def test_legacy_viewed_descriptor_still_binds(self):
        """Descriptors recorded before ranges existed (id + stamp only)
        still bind: the live card leaves the unread deck, history
        re-materializes what the empty window can, and unview puts the
        card back in the deck as new."""
        rid = "20261017-010000-aaaabbbb"
        (self.requests / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "status": "ready", "label": "legacy",
             "created_at": "2026-10-17T01:00:00"}), encoding="utf-8")
        eid_area = f"area:power:{self.c_power[:12]}"
        self.seed_state(viewed=[
            {"id": f"request:{rid}",
             "viewed_at": "2026-10-17T05:00:00"},        # no range key
            {"id": eid_area,
             "viewed_at": "2026-10-17T05:00:00"}])
        feed = self.get()
        # both live cards are already acknowledged - only the chat card
        # stays in the unread deck, even though the watermark is pinned
        self.assertEqual([e["id"] for e in feed["entries"]],
                         [f"area:assistant:{self.c_chat[:12]}"])
        hist = {h["id"]: h for h in feed["history"]}
        self.assertEqual(hist[f"request:{rid}"]["state"], "viewed")
        self.assertFalse(hist[f"request:{rid}"].get("unavailable",
                                                  False))
        self.assertEqual(hist[eid_area]["state"], "viewed")
        self.assertTrue(hist[eid_area]["unavailable"])  # honest stub
        # and the descriptor still transitions - unview resurfaces the
        # card into the deck
        s, d = self.post(f"request:{rid}", "unview")
        self.assertEqual(s, 200, d)
        self.assertEqual(d["state"], "new")
        self.assertEqual(d["unseen_count"], 2)
        self.assertIn(f"request:{rid}", self.ids())


if __name__ == "__main__":
    unittest.main()
