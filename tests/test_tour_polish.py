"""Tests for tour_polish.py - the OPTIONAL, disabled-by-default LLM
enrichment of the 'What's new' tour, plus its feed/wiring integration.

Convention: C:\\Projects\\TESTING.md - real git fixture repos, no
network, no real provider: every test injects a fake provider callable
(or disables the feature). No LLM is ever contacted.
"""
import json
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

import conductor  # noqa: E402
import tour_feeds  # noqa: E402
import tour_polish  # noqa: E402
import tour_state  # noqa: E402


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
    """(prompt)->str seam: records prompts, answers with a canned reply
    (str or callable) or raises."""

    def __init__(self, reply="", exc=None, delay=0.0):
        self.calls: list[str] = []
        self.reply = reply
        self.exc = exc
        self.delay = delay
        self.entered = threading.Event()

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        self.entered.set()
        if self.delay:
            time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.reply(prompt) if callable(self.reply) else self.reply


class PolishFixture(unittest.TestCase):
    """Fixture: repo with an init commit (watermark) + 'Improve power
    dialog' (conductor.py) + 'Add assistant chat panel' (assistant/) ->
    two deterministic change cards; data dirs for state/cache/records."""

    def setUp(self):
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
        self.cache_path = self.data / "tour-cache.json"
        self.state_path.write_text(json.dumps(
            {"watermark": {"commit": self.c0,
                           "at": "2020-01-01T00:00:00",
                           "origin": "advance"},
             "viewed": [], "snoozed": []}), encoding="utf-8")
        self.state = tour_state.load(self.state_path)
        self.boundary = tour_state.capture_boundary(self.repo)
        self.rng = tour_state.feed_range(self.state, self.boundary)

    def polisher(self, **kw) -> tour_polish.Polisher:
        kw.setdefault("enabled", True)
        return tour_polish.Polisher(self.cache_path, self.repo, **kw)

    def feed(self, polish=None):
        return tour_feeds.build_feed(
            self.state, self.boundary, repo_dir=self.repo,
            runs_dir=self.runs, requests_dir=self.requests,
            manifests_dir=self.manifests, polish=polish)

    def det_titles(self):
        return {e["title"] for e in self.feed()["entries"]}


# ------------------------------------------------------------- module


class ParseReplyTests(unittest.TestCase):

    def test_bare_and_fenced_json(self):
        doc = {"cards": []}
        self.assertEqual(tour_polish.parse_reply(json.dumps(doc)), doc)
        self.assertEqual(tour_polish.parse_reply(
            "Here you go:\n```json\n" + json.dumps(doc) + "\n```"), doc)
        self.assertEqual(tour_polish.parse_reply(
            "Sure! " + json.dumps(doc) + " done"), doc)

    def test_garbage_is_none(self):
        self.assertIsNone(tour_polish.parse_reply(""))
        self.assertIsNone(tour_polish.parse_reply("no json here"))
        self.assertIsNone(tour_polish.parse_reply("{unclosed"))
        self.assertIsNone(tour_polish.parse_reply(None))


class SanitizeTests(unittest.TestCase):

    def setUp(self):
        self.shas = ["a1b2c3d4e5", "f6e7d8c9b0", "1122334455"]
        self.stops = set(tour_polish._stops_doc())

    def cards(self, *cs):
        return tour_polish.sanitize_cards(
            list(cs), window_shas=self.shas, known_stops=self.stops)[0]

    def test_valid_card_prefixes_resolve_to_full_shas(self):
        cards, drops = tour_polish.sanitize_cards(
            [{"stop": "power", "title": "T", "blurb": "B", "tag": "fixed",
              "commits": ["a1b2c3"]}],
            window_shas=self.shas, known_stops=self.stops)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["commits"], ["a1b2c3d4e5"])
        self.assertEqual(drops, [])

    def test_invented_sha_unknown_stop_url_and_bad_tag(self):
        cards, drops = tour_polish.sanitize_cards(
            [{"stop": "power", "title": "T", "blurb": "B",
              "commits": ["deadbeef99"]},                    # invented
             {"stop": "nonsense", "title": "T", "blurb": "B",
              "commits": ["a1b2c3"]},                        # bad stop
             {"stop": "power", "title": "T",
              "blurb": "see https://evil.example",
              "commits": ["f6e7d8"]},                        # URL
             {"stop": "power", "title": "T", "blurb": "B",
              "tag": "amazing", "commits": ["1122334"]}],    # bad tag
            window_shas=self.shas, known_stops=self.stops)
        self.assertEqual(len(cards), 1)
        self.assertIsNone(cards[0]["tag"])          # invalid tag dropped
        self.assertEqual(cards[0]["commits"], ["1122334455"])
        self.assertEqual(len(drops), 3)

    def test_one_commit_one_card_and_card_cap(self):
        dup = [{"stop": "power", "title": "T", "blurb": "B",
                "commits": ["a1b2c3"]},
               {"stop": "modal", "title": "T2", "blurb": "B2",
                "commits": ["a1b2c3"]}]                     # re-claim
        cards, _ = tour_polish.sanitize_cards(
            dup, window_shas=self.shas, known_stops=self.stops)
        self.assertEqual(len(cards), 1)              # second loses the sha
        many = [{"stop": "power", "title": "T", "blurb": "B",
                 "commits": [s[:7]]}
                for s in self.shas] * (tour_polish.MAX_CARDS + 2)
        cards, _ = tour_polish.sanitize_cards(
            many, window_shas=self.shas + ["ff" + "%06d" % i
                                           for i in range(60)],
            known_stops=self.stops)
        self.assertLessEqual(len(cards), tour_polish.MAX_CARDS)


# ----------------------------------------------------------- generation


class GenerateTests(PolishFixture):

    def test_disabled_never_calls_or_writes(self):
        fp = FakeProvider(reply='{"cards": []}')
        p = self.polisher(enabled=False, provider=fp)
        self.assertEqual(p.attempt(self.rng)["status"], "disabled")
        self.assertIsNone(p.cards_for(self.rng))
        self.assertFalse(p.kick(self.rng))
        self.assertEqual(fp.calls, [])
        self.assertFalse(self.cache_path.exists())

    def test_unavailable_provider_persists_fallback(self):
        fp = FakeProvider(exc=RuntimeError("no auth"))
        p = self.polisher(provider=fp)
        rec = p.attempt(self.rng)
        self.assertEqual(rec["status"], "fallback")
        self.assertIn("no auth", rec["error"])
        doc = json.loads(self.cache_path.read_text(encoding="utf-8"))
        key = p.range_key(self.rng)
        self.assertEqual(doc["ranges"][key]["status"], "fallback")
        # a later attempt does NOT retry the provider
        rec2 = p.attempt(self.rng)
        self.assertEqual(rec2["status"], "fallback")
        self.assertEqual(len(fp.calls), 1)
        # and the feed stays deterministic
        self.assertIsNone(p.cards_for(self.rng))
        self.assertEqual(len(self.feed()["entries"]), 2)

    def test_input_is_bounded(self):
        for i in range(40):
            commit(self.repo, "conductor.py", str(i),
                   f"Improve power dialog v{i}")
        boundary = tour_state.capture_boundary(self.repo)
        rng = tour_state.feed_range(self.state, boundary)
        seen = []
        p = self.polisher(provider=lambda pr: seen.append(pr) or
                          '{"cards": []}')
        p.attempt(rng)
        self.assertEqual(len(seen), 1)
        prompt = seen[0]
        self.assertLessEqual(len(prompt),
                             tour_polish.PROMPT_MAX_CHARS + 8000)
        # ~30 commit cap: the window has 42 commits, the prompt shows a
        # bounded subset (oneline rows only - stat lines have no sha col)
        oneline_rows = [ln for ln in prompt.splitlines()
                        if "Improve power dialog v" in ln]
        self.assertLessEqual(len(oneline_rows),
                             tour_polish.MAX_COMMITS + 2)

    def test_valid_output_applies_grouped_card(self):
        reply = {"cards": [
            {"stop": "power", "title": "Power & chat polish",
             "blurb": "Nicer power dialog and a new chat panel.",
             "tag": "improved",
             "commits": [self.c_power[:8], self.c_chat[:8]]}]}
        p = self.polisher(provider=FakeProvider(reply=json.dumps(reply)))
        rec = p.attempt(self.rng)
        self.assertEqual(rec["status"], "ok", rec)
        feed = self.feed(polish=p.cards_for)
        changes = [e for e in feed["entries"] if e["kind"] == "change"]
        self.assertEqual(len(changes), 1)        # grouped into one card
        e = changes[0]
        self.assertEqual(e["id"], f"area:power:{self.c_chat[:12]}")
        self.assertEqual(e["title"], "Power & chat polish")
        self.assertEqual(e["blurb"],
                         "Nicer power dialog and a new chat panel.")
        self.assertEqual(e["tag"], "improved")
        self.assertTrue(e["polished"])
        self.assertEqual({c["sha"] for c in e["commits"]},
                         {self.c_power, self.c_chat})
        # unseen counting still works over polished ids
        self.assertEqual(feed["unseen_count"], 1)

    def test_leftover_commits_stay_deterministic(self):
        reply = {"cards": [
            {"stop": "power", "title": "Power tweak", "blurb": "B",
             "tag": "improved", "commits": [self.c_power[:8]]}]}
        p = self.polisher(provider=FakeProvider(reply=json.dumps(reply)))
        p.attempt(self.rng)
        feed = self.feed(polish=p.cards_for)
        ids = {e["id"] for e in feed["entries"]}
        self.assertIn(f"area:power:{self.c_power[:12]}", ids)
        self.assertIn(f"area:assistant:{self.c_chat[:12]}", ids)

    def test_malformed_reply_persists_fallback(self):
        p = self.polisher(provider=FakeProvider(reply="sorry, can't"))
        rec = p.attempt(self.rng)
        self.assertEqual(rec["status"], "fallback")
        self.assertIn("malformed", rec["error"])
        self.assertIsNone(p.cards_for(self.rng))
        self.assertEqual(len(self.feed(polish=p.cards_for)["entries"]), 2)

    def test_all_invalid_cards_is_fallback(self):
        reply = {"cards": [
            {"stop": "bogus", "title": "T", "blurb": "B",
             "commits": ["deadbeef"]}]}
        p = self.polisher(provider=FakeProvider(reply=json.dumps(reply)))
        rec = p.attempt(self.rng)
        self.assertEqual(rec["status"], "fallback")
        self.assertIn("no usable cards", rec["error"])

    def test_invented_links_filtered_at_apply(self):
        rid = "20261017-010000-aaaabbbb"
        (self.requests / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "label": "real", "created_at":
             self.boundary["at"]}), encoding="utf-8")
        reply = {"cards": [
            {"stop": "power", "title": "T", "blurb": "B", "tag": "new",
             "commits": [self.c_power[:8]],
             "requests": [rid, "99999999-999999-ffffffff"],
             "runs": ["request:ghost", "data/runs/nope"]}]}
        p = self.polisher(provider=FakeProvider(reply=json.dumps(reply)))
        p.attempt(self.rng)
        feed = self.feed(polish=p.cards_for)
        e = {x["id"]: x for x in feed["entries"]}[
            f"area:power:{self.c_power[:12]}"]
        self.assertEqual(e["requests"], [rid])     # invented id dropped
        self.assertEqual(e["runs"], [])            # invented runs dropped

    def test_curated_blurb_still_wins_over_polish(self):
        (self.manifests / "whatsnew.jsonl").write_text(json.dumps(
            {"stop": "power", "blurb": "CURATED words about power",
             "commits": [self.c_power[:8]]}) + "\n", encoding="utf-8")
        reply = {"cards": [
            {"stop": "power", "title": "LLM title", "blurb": "LLM blurb",
             "tag": "new", "commits": [self.c_power[:8]]}]}
        p = self.polisher(provider=FakeProvider(reply=json.dumps(reply)))
        p.attempt(self.rng)
        feed = self.feed(polish=p.cards_for)
        e = {x["id"]: x for x in feed["entries"]}[
            f"area:power:{self.c_power[:12]}"]
        self.assertEqual(e["blurb"], "CURATED words about power")

    def test_cache_hit_skips_provider(self):
        p = self.polisher(provider=FakeProvider(
            reply=json.dumps({"cards": [
                {"stop": "power", "title": "T", "blurb": "B",
                 "tag": "new", "commits": [self.c_power[:8]]}]})))
        self.assertEqual(p.attempt(self.rng)["status"], "ok")
        p.provider = FakeProvider(exc=AssertionError("must not call"))
        rec = p.attempt(self.rng)
        self.assertEqual(rec["status"], "ok")

    def test_new_tip_is_a_new_range_key(self):
        p = self.polisher(provider=FakeProvider(
            reply='{"cards": [{"stop": "power", "title": "T",'
                  ' "blurb": "B", "tag": "new", "commits": []}]}'))
        # first range resolves (no usable commits -> fallback stored)
        p.attempt(self.rng)
        commit(self.repo, "conductor.py", "z", "Improve power dialog 2")
        b2 = tour_state.capture_boundary(self.repo)
        rng2 = tour_state.feed_range(self.state, b2)
        self.assertNotEqual(p.range_key(rng2), p.range_key(self.rng))
        p2_calls = []
        p.provider = lambda pr: p2_calls.append(pr) or '{"cards":[]}'
        p.attempt(rng2)
        self.assertEqual(len(p2_calls), 1)         # changed range: re-gen
        doc = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(len(doc["ranges"]), 2)    # both ranges kept

    def test_concurrent_attempts_coalesce(self):
        gate = threading.Event()
        calls = []

        def slow(prompt):
            calls.append(prompt)
            gate.wait(5)
            return '{"cards": []}'

        p = self.polisher(provider=slow)
        results = []

        def worker():
            results.append(p.attempt(self.rng)["status"])

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        # deterministic: wait until t1 is inside the provider, so t2's
        # attempt must observe the in-flight mark
        self.assertTrue(_poll(lambda: len(calls) == 1, 5))
        t2.start()
        t2.join(5)
        self.assertIn("generating", [r for r in results]) # coalesced
        gate.set()
        t1.join(5)
        self.assertEqual(len(calls), 1)    # exactly one provider call
        rec = p.record(self.rng)
        self.assertIsNotNone(rec)          # outcome persisted once

    def test_kick_is_async_and_persists(self):
        p = self.polisher(provider=FakeProvider(
            reply='{"cards": []}'))
        self.assertTrue(p.kick(self.rng))
        self.assertFalse(p.kick(self.rng))     # in-flight: coalesced
        self.assertTrue(_poll(lambda: p.record(self.rng) is not None))
        self.assertEqual(p.record(self.rng)["status"], "fallback")
        # persisted record means the next kick is a no-op too
        self.assertFalse(p.kick(self.rng))

    def test_history_rematerializes_polished_ids(self):
        """A viewed entry recorded under a polished feed re-materializes
        to the SAME card as long as the cache holds the range - entry
        identity survives watermark advancement."""
        reply = {"cards": [
            {"stop": "power", "title": "Both at once", "blurb": "B",
             "tag": "new",
             "commits": [self.c_power[:8], self.c_chat[:8]]}]}
        p = self.polisher(provider=FakeProvider(reply=json.dumps(reply)))
        p.attempt(self.rng)
        feed = self.feed(polish=p.cards_for)
        eid = f"area:power:{self.c_chat[:12]}"
        e = {x["id"]: x for x in feed["entries"]}[eid]
        tour_state.mark_viewed(eid, e["range"], self.boundary,
                               remaining_new=[], path=self.state_path,
                               repo_dir=self.repo)
        st = tour_state.load(self.state_path)
        feed2 = tour_feeds.build_feed(
            st, self.boundary, repo_dir=self.repo, runs_dir=self.runs,
            requests_dir=self.requests, manifests_dir=self.manifests,
            polish=p.cards_for)
        self.assertEqual(feed2["entries"], [])
        self.assertEqual(feed2["history"][0]["id"], eid)
        self.assertEqual(feed2["history"][0]["title"], "Both at once")
        self.assertFalse(feed2["history"][0].get("unavailable"))


# ----------------------------------------------------- conductor wiring


class ServeWiringTests(PolishFixture):
    """The tour_ctx/_tour_build integration: polish applies from the
    persisted cache only, kicks a background attempt when uncached, and
    never delays the response on provider work."""

    def ctx(self, polisher):
        c = conductor.tour_ctx(self.runs, self.requests, self.manifests,
                               self.repo)
        c["polisher"] = polisher
        return c

    def test_disabled_polisher_plain_feed(self):
        fp = FakeProvider(reply="x")
        ctx = self.ctx(self.polisher(enabled=False, provider=fp))
        d = conductor._tour_build(ctx)
        self.assertEqual(d["enrichment"], {"enabled": False})
        self.assertEqual(len(d["entries"]), 2)
        self.assertEqual(fp.calls, [])
        self.assertFalse(self.cache_path.exists())

    def test_enabled_kicks_then_polishes_next_build(self):
        reply = {"cards": [
            {"stop": "power", "title": "Shiny", "blurb": "B",
             "tag": "new",
             "commits": [self.c_power[:8], self.c_chat[:8]]}]}
        p = self.polisher(provider=FakeProvider(reply=json.dumps(reply)))
        ctx = self.ctx(p)
        d = conductor._tour_build(ctx)           # uncached: deterministic
        self.assertEqual(d["enrichment"]["status"], "pending")
        self.assertFalse(ctx["feed"] is None)
        # background attempt lands -> cache invalidated -> next build
        # serves the polished card
        self.assertTrue(_poll(lambda: ctx["feed"] is None))
        d2 = conductor._tour_build(ctx)
        self.assertEqual(d2["enrichment"]["status"], "ok")
        changes = [e for e in d2["entries"] if e["kind"] == "change"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["title"], "Shiny")

    def test_failed_attempt_falls_back_and_stays_cached(self):
        p = self.polisher(provider=FakeProvider(exc=OSError("gone")))
        ctx = self.ctx(p)
        conductor._tour_build(ctx)
        self.assertTrue(_poll(lambda: p.record(self.rng) is not None))
        d = conductor._tour_build(ctx)
        self.assertEqual(d["enrichment"]["status"], "fallback")
        self.assertEqual(len(d["entries"]), 2)   # deterministic cards

    def test_get_path_never_blocks_on_generation(self):
        slow = FakeProvider(reply='{"cards": []}', delay=5.0)
        ctx = self.ctx(self.polisher(provider=slow))
        t0 = time.time()
        d = conductor._tour_build(ctx)
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(len(d["entries"]), 2)
        # let the daemon finish so the test doesn't leak work
        self.assertTrue(slow.entered.wait(5))
        self.assertTrue(_poll(lambda: p_rec(ctx, self.rng), 15))

    def test_handler_ctx_carries_polisher(self):
        handler = conductor.make_dashboard_handler(
            self.runs, ["no-such-devin"], self.requests, self.manifests,
            tour_repo=self.repo)
        # lazily built on first build - module present here
        conductor._tour_build(handler.tour_ctx)
        self.assertIsNotNone(handler.tour_ctx["polisher"])
        self.assertEqual(handler.tour_ctx["devin_argv"],
                         ["no-such-devin"])


def p_rec(ctx, rng):
    return ctx["polisher"].record(rng) is not None


if __name__ == "__main__":
    unittest.main()
