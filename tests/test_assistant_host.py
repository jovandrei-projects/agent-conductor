"""Conductor host-adapter tests: view-context enrichment and bounded
retrieval over run/request records.

Conventions match test_conductor.py - a synthetic data tree under
TemporaryDirectory (runs/, requests/, a shared projects map), no writes
outside it. The fixtures are deliberately minimal but field-faithful to
what run_step records in state.json.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import conductor  # noqa: E402
from assistant import context as ctxmod  # noqa: E402
from assistant.core import AssistantCore  # noqa: E402
from assistant.retrieval import MAX_EXCERPT_BYTES  # noqa: E402
from assistant.store import SCHEMA_VERSION, Store  # noqa: E402
from assistant_host.context import ConductorContext  # noqa: E402
from assistant_host.retrieval import (ConductorRetrieval,  # noqa: E402
                                      interpret_window)

NOW = time.mktime((2026, 10, 17, 12, 0, 0, 0, 0, 0))   # fixed clock seam


def _step(name, **kw):
    rec = {"name": name, "status": "done", "slug": "apartment-3d",
           "started_at": "2026-10-15T04:39:05",
           "ended_at": "2026-10-15T04:58:12", "elapsed_s": 1147.0,
           "model": "gpt-6-luna", "effort": "high", "mode": "bypass",
           "session_id": "sess-fixture-1", "exit_code": 0,
           "verify_ok": True, "attempts": 1, "max_attempts": 2,
           "task": f"task text for {name}",
           "outcome": f"{name} finished the thing",
           "tokens": {"prompt": 40000, "completion": 8000,
                      "cached": 12000, "llm_steps": 40}}
    rec.update(kw)
    return rec


def _write_run(runs: Path, dirname: str, run: str, steps: dict, **extra):
    d = runs / dirname
    d.mkdir(parents=True, exist_ok=True)
    st = {"run": run, "run_dir": str(d), "manifest": "m.toml",
          "started_at": "2026-10-14T23:56:31",
          "ended_at": "2026-10-16T04:00:00", "steps": steps,
          "driver_pid": None}
    st.update(extra)
    (d / "state.json").write_text(json.dumps(st), encoding="utf-8")
    return d, st


def make_tree(root: Path):
    """A small but faithful data tree:

    - a Sept run on apartment-3d with a camera step + summary,
    - the real-named Oct run 'apartment-play-edit' with camera steps,
    - a model-compare-mobile run whose summary carries a Verification
      paragraph (the manifest's required report example, condensed),
    - one archived run, one request record, one corrupt state.json.
    """
    runs = root / "data" / "runs"
    reqs = root / "data" / "requests"
    reqs.mkdir(parents=True, exist_ok=True)

    # September apartment-3d run (out of October, exercises 'last month')
    d, _ = _write_run(
        runs, "20260910-120000-apartment-sept", "apartment-sept",
        {"camera-tuning-sept": _step(
            "camera-tuning-sept", slug="apartment-3d",
            started_at="2026-09-10T12:05:00",
            ended_at="2026-09-10T12:40:00",
            task="Retune the orbit camera damping",
            outcome="camera damping retuned")},
        started_at="2026-09-10T12:00:00", ended_at="2026-09-10T13:00:00")
    (d / "camera-tuning-sept.summary.md").write_text(
        "# apartment-sept / camera-tuning-sept\n\n## Report\n\n"
        "Camera damping verified on-device.\n", encoding="utf-8")

    # October apartment run with several camera-ish steps
    d2, _ = _write_run(
        runs, "20261014-235630-apartment-play-edit", "apartment-play-edit",
        {"baseline-and-reference-audit": _step(
            "baseline-and-reference-audit",
            started_at="2026-10-14T23:56:31"),
         "third-person-camera-stability": _step(
             "third-person-camera-stability",
             started_at="2026-10-15T04:39:05",
             task="Stabilize the third-person camera orbit",
             outcome="orbit no longer fights player input"),
         "kitchen-and-scene-cleanup": _step(
             "kitchen-and-scene-cleanup",
             started_at="2026-10-15T05:50:07")},
        request_id="20261014-235500-abcd1234",
        started_at="2026-10-14T23:56:31", ended_at="2026-10-16T04:00:00")
    (d2 / "third-person-camera-stability.summary.md").write_text(
        "# apartment-play-edit / third-person-camera-stability\n\n"
        "## Task\n\n```\nStabilize the third-person camera orbit\n```\n\n"
        "## Report\n\nDone - camera follows without jitter.\n",
        encoding="utf-8")

    # model-compare-mobile: the required report example, condensed but
    # keeping the '**Verification (all green):**' lead paragraph shape
    d3, _ = _write_run(
        runs, "20261016-013639-model-compare-mobile", "model-compare-mobile",
        {"mobile-visualizations-and-filters": _step(
            "mobile-visualizations-and-filters", slug="model-compare",
            started_at="2026-10-16T02:10:19",
            ended_at="2026-10-16T03:07:00",
            task="Work in the model-compare repository, inside the mobile/ "
                 "Expo app - add charts and filters",
            outcome="charts + filters verified on web at 390px")},
        request_id="20261015-232400-85544f1c",
        started_at="2026-10-16T01:36:41", ended_at="2026-10-16T04:00:00")
    (d3 / "mobile-visualizations-and-filters.summary.md").write_text(
        "# 20261016-013639-model-compare-mobile / "
        "mobile-visualizations-and-filters\n\n"
        "- **status:** done · **verify:** pass · **exit:** 0\n\n"
        "## Task\n\n```\nWork in the model-compare repository...\n```\n\n"
        "## Report\n\nDone - the work is verified, documented, and "
        "committed in 4 clean groups.\n\n"
        "## What happened this session\n\n"
        "**Verification (all green):**\n"
        "- `tsc --noEmit` clean, `eslint` clean\n"
        "- `expo export` bundles clean for android + web\n"
        "- `run_tests.py --quick`: 36 tests OK\n"
        "- Playwright smoke at 390px: zero console errors\n\n"
        "More sections would follow here.\n",
        encoding="utf-8")
    (d3 / "mobile-visualizations-and-filters.stdout.log").write_text(
        "narration line one\nverification ran: tsc --noEmit clean\n"
        "api_key=sk-live-secret-9f8e7d done\n", encoding="utf-8")
    # point the record at the log
    st3 = json.loads((d3 / "state.json").read_text(encoding="utf-8"))
    st3["steps"]["mobile-visualizations-and-filters"]["stdout_log"] = \
        str(d3 / "mobile-visualizations-and-filters.stdout.log")
    st3["steps"]["mobile-visualizations-and-filters"]["env"] = \
        {"DEVIN_API_TOKEN": "sekret"}
    (d3 / "state.json").write_text(json.dumps(st3), encoding="utf-8")

    # archived run - excluded by default
    _write_run(runs, "20261013-093553-smoke", "smoke",
               {"s1": _step("s1", slug="demo-project",
                            started_at="2026-10-13T09:36:00")},
               archived=True)

    # corrupt state.json counts in coverage, never crashes a scan
    bad = runs / "20261012-000000-badstate"
    bad.mkdir(exist_ok=True)
    (bad / "state.json").write_text("{not json", encoding="utf-8")

    # a saved request record
    (reqs / "20261016-194008-703a9675.json").write_text(json.dumps({
        "id": "20261016-194008-703a9675", "label": "Camera rework",
        "raw": "rework the apartment-3d camera stack",
        "project": "apartment-3d", "status": "ready",
        "model": "gpt-6-luna", "effort": "high",
        "created_at": "2026-10-16T19:40:08",
        "updated_at": "2026-10-16T19:50:00",
        "proposed_steps": [{"step": "camera-work",
                            "slug": "apartment-3d",
                            "task": "camera work"}],
        "pending_questions": []}), encoding="utf-8")
    return runs, reqs


PROJECTS = {"apartment-3d": {"path": Path("C:/Projects/apartment-3d")},
            "model-compare": {"path": Path("C:/Projects/model-compare")}}


def make_retrieval(root: Path) -> ConductorRetrieval:
    runs, reqs = make_tree(root)
    return ConductorRetrieval(runs_dir=runs, requests_dir=reqs,
                              projects=PROJECTS, now=lambda: NOW)


def make_ctx(root: Path) -> ConductorContext:
    runs, reqs = make_tree(root)
    return ConductorContext(runs_dir=runs, requests_dir=reqs,
                            projects=PROJECTS, now=lambda: NOW)


def run_key_for(runs: Path, dirname: str) -> str:
    st = json.loads((runs / dirname / "state.json").read_text(
        encoding="utf-8"))
    return (f"request:{st['request_id']}" if st.get("request_id")
            else str(runs / dirname))


def _d(y, m, day, hh=0, mm=0, ss=0):
    import datetime as _dt
    return _dt.datetime(y, m, day, hh, mm, ss).timestamp()


class WindowInterpretTests(unittest.TestCase):
    def test_named_and_relative_windows(self):
        self.assertEqual(interpret_window("last month", NOW)[2],
                         "last calendar month")
        a, b, _ = interpret_window("last month", NOW)
        self.assertEqual(a, _d(2026, 9, 1))
        self.assertEqual(b, _d(2026, 10, 1) - 1)
        a, b, lbl = interpret_window("past 7 days", NOW)
        self.assertAlmostEqual(b - a, 7 * 86400, places=0)
        a, b, _ = interpret_window("in September 2026", NOW)
        self.assertEqual(a, _d(2026, 9, 1))
        a, b, _ = interpret_window("2026-09", NOW)
        self.assertEqual(a, _d(2026, 9, 1))
        a, b, _ = interpret_window("on 2026-09-15", NOW)
        self.assertAlmostEqual(b - a, 86399, places=0)
        a, b, _ = interpret_window("2026-09-01..2026-09-10", NOW)
        self.assertEqual(a, _d(2026, 9, 1))
        self.assertEqual(b, _d(2026, 9, 10, 23, 59, 59))
        self.assertIsNone(interpret_window("no dates here", NOW))


class RetrievalSearchTests(unittest.TestCase):
    def test_subject_plus_project_plus_window(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("camera changes to apartment-3d last month")
            meta = r.last_search_meta()
            interp = meta["interpreted"]
            # 'last month' from Oct 17 -> September 2026
            self.assertTrue(interp["date_from"].startswith("2026-09-01"))
            self.assertTrue(interp["date_to"].startswith("2026-09-30"))
            self.assertEqual(interp["project"], "apartment-3d")
            keys = [s["key"] for s in srcs]
            self.assertTrue(any("camera-tuning-sept" in k for k in keys))
            # October camera steps must NOT be claimed for September
            self.assertFalse(
                any("third-person-camera" in k for k in keys))
            self.assertFalse(
                any("mobile-play-and-cameras" in k for k in keys))

    def test_this_month_window_hits_october(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("camera in apartment-3d this month")
            names = [s["step"] for s in srcs if s.get("step")]
            self.assertIn("third-person-camera-stability", names)
            self.assertIn("baseline-and-reference-audit", names)
            # September run is outside this month
            self.assertNotIn("camera-tuning-sept", names)

    def test_no_subject_no_match_claims(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("zebra quantum flux capacitor")
            self.assertEqual(srcs, [])
            meta = r.last_search_meta()
            self.assertEqual(meta["matched"], 0)
            self.assertGreater(meta["scanned"]["runs"], 0)

    def test_run_and_step_filters(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("", filters={
                "run": "model-compare-mobile",
                "step": "mobile-visualizations"})
            kinds = {s["kind"] for s in srcs}
            self.assertIn("agent", kinds)
            ag = next(s for s in srcs if s["kind"] == "agent")
            self.assertEqual(ag["step"], "mobile-visualizations-and-filters")
            # nav is a stable modal descriptor: request:<id>|<step>
            self.assertEqual(
                ag["nav"],
                {"view": "agent",
                 "key": "request:20261015-232400-85544f1c"
                        "|mobile-visualizations-and-filters"})

    def test_archived_excluded_but_counted(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("", filters={"run": "smoke"})
            self.assertEqual(srcs, [])
            self.assertEqual(
                r.last_search_meta()["scanned"]["archived_skipped"], 1)
            srcs = r.search("", filters={"run": "smoke",
                                         "include_archived": True})
            self.assertTrue(any(s["run"] == "smoke" for s in srcs))

    def test_summary_source_when_subject_hits_report(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("verification noEmit tsc",
                            filters={"run": "model-compare-mobile"})
            kinds = {(s["kind"], s.get("step")) for s in srcs}
            self.assertIn(("summary",
                           "mobile-visualizations-and-filters"), kinds)
            meta = r.last_search_meta()
            self.assertGreater(meta["scanned"]["summaries_read"], 0)

    def test_request_records_searchable(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("camera rework",
                            filters={"project": "apartment-3d"})
            reqs = [s for s in srcs if s["kind"] == "request"]
            self.assertTrue(reqs)
            self.assertEqual(reqs[0]["request_id"],
                             "20261016-194008-703a9675")
            self.assertEqual(reqs[0]["nav"]["view"], "request")

    def test_limit_and_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("", filters={"limit": 3})
            self.assertLessEqual(len(srcs), 3)
            keys = [(s["kind"], s["key"]) for s in srcs]
            self.assertEqual(len(keys), len(set(keys)))

    def test_invalid_limit_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("", filters={"limit": "bogus"})
            self.assertLessEqual(len(srcs), 12)


class RetrievalFetchTests(unittest.TestCase):
    def test_agent_excerpt_has_record_and_tail(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("", filters={
                "run": "model-compare-mobile",
                "step": "mobile-visualizations"})
            ag = next(s for s in srcs if s["kind"] == "agent")
            full = r.fetch(ag)
            ex = full["excerpt"]
            self.assertIn("mobile-visualizations-and-filters", ex)
            self.assertIn("status:", ex)
            self.assertIn("task:", ex)
            self.assertIn("log tail:", ex)
            # sensitive fields never leak into the excerpt text
            self.assertNotIn("DEVIN_API_TOKEN", ex)
            self.assertIn("api_key=", ex)     # masked, key name survives
            self.assertIn("***", ex)
            self.assertNotIn("sk-live-secret", ex)
            self.assertLessEqual(full["bytes"], MAX_EXCERPT_BYTES)

    def test_summary_fetch_locates_verification_section(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("verification",
                            filters={"run": "model-compare-mobile"})
            su = next(s for s in srcs if s["kind"] == "summary")
            self.assertEqual(su["nav"]["view"], "summary")
            full = r.fetch(su)
            ex = full["excerpt"]
            # the window keeps the section title AND the match
            self.assertIn("Verification (all green)", ex)
            self.assertIn("tsc --noEmit", ex)

    def test_fetch_budget_and_missing_records(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("", filters={"run": "model-compare-mobile"})
            ag = next(s for s in srcs if s["kind"] == "agent")
            small = r.fetch(ag, budget=600)
            self.assertLessEqual(small["bytes"], 600)
            # a source whose record vanished reports honestly
            ghost = r.fetch({"kind": "agent", "run_dir": "no-such-run",
                             "step": "ghost", "key": "k", "label": "g",
                             "nav": {"view": "agent", "key": "k"}})
            self.assertIn("not found", ghost["excerpt"])

    def test_fetch_by_key_only_resolves_the_record(self):
        """The tool contract hands fetch only {kind,key[,nav]} echoes -
        the server must re-resolve run_dir/step/request_id from the
        stable key itself (search stubs never expose run_dir)."""
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("verification",
                            filters={"run": "model-compare-mobile"})
            su = next(s for s in srcs if s["kind"] == "summary")
            echo = {"kind": su["kind"], "key": su["key"]}   # tool echo
            full = r.fetch(echo)
            self.assertIn("tsc --noEmit", full["excerpt"])
            self.assertEqual(full["step"],
                             "mobile-visualizations-and-filters")
            self.assertTrue(full["nav"]["key"])   # chip can navigate
            # an agent source the same way
            ag = next(s for s in srcs if s["kind"] == "agent")
            full = r.fetch({"kind": ag["kind"], "key": ag["key"]})
            self.assertIn("mobile-visualizations-and-filters",
                          full["excerpt"])
            # a run-kind source keyed by 'request:<id>'
            rn = next(s for s in srcs if s["kind"] == "run")
            full = r.fetch({"kind": rn["kind"], "key": rn["key"]})
            self.assertIn("model-compare-mobile", full["excerpt"])
            # a request-kind source resolves request:<id> -> the record
            req_srcs = r.search("", filters={"project": "apartment-3d"})
            rq = next(s for s in req_srcs if s["kind"] == "request")
            full = r.fetch({"kind": rq["kind"], "key": rq["key"]})
            self.assertNotIn("not found", full["excerpt"])

    def test_fetch_synthesizes_label_and_nav_for_echoes(self):
        """An echoed {kind,key} leaves the exchange's stored chips
        readable AND navigable - _resolve_src derives the label/nav
        the search stub originally carried."""
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("verification",
                            filters={"run": "model-compare-mobile"})
            su = next(s for s in srcs if s["kind"] == "summary")
            full = r.fetch({"kind": su["kind"], "key": su["key"]})
            self.assertIn("mobile-visualizations-and-filters",
                          full["label"])
            self.assertIn("session summary", full["label"])
            self.assertEqual(full["nav"], su["nav"])
            rn = next(s for s in srcs if s["kind"] == "run")
            full = r.fetch({"kind": rn["kind"], "key": rn["key"]})
            self.assertIn("model-compare-mobile", full["label"])
            self.assertEqual(full["nav"]["view"], "run")

    def test_fetch_many_shares_budget(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("", filters={"run": "apartment-play-edit"})
            self.assertGreaterEqual(len(srcs), 3)
            # the first excerpt (~400B record+task) eats the budget; the
            # rest must be marked omitted rather than silently dropped
            out = r.fetch_many(srcs, total=450)
            self.assertEqual(len(out), len(srcs))
            total = sum(s.get("bytes") or 0 for s in out)
            self.assertLessEqual(total, 450)
            self.assertTrue(any(s.get("omitted") == "budget" for s in out))

    def test_run_and_request_excerpts(self):
        with tempfile.TemporaryDirectory() as td:
            r = make_retrieval(Path(td))
            srcs = r.search("", filters={"run": "apartment-play-edit"})
            rn = next(s for s in srcs if s["kind"] == "run")
            ex = r.fetch(rn)["excerpt"]
            self.assertIn("apartment-play-edit", ex)
            self.assertIn("camera", ex)
            srcs = r.search("camera rework",
                            filters={"project": "apartment-3d"})
            rq = next(s for s in srcs if s["kind"] == "request")
            ex = r.fetch(rq)["excerpt"]
            self.assertIn("Camera rework", ex)
            self.assertIn("camera-work", ex)


class ContextEnrichTests(unittest.TestCase):
    def test_agent_view_enrichment(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = make_ctx(Path(td))
            runs = Path(td) / "data" / "runs"
            rk = run_key_for(runs, "20261016-013639-model-compare-mobile")
            env = ctx.enrich({
                "view": "agent",
                "refs": {"run_key": rk,
                         "step": "mobile-visualizations-and-filters"}})
            self.assertIn("mobile-visualizations", env["title"])
            rec = env["record"]
            self.assertEqual(rec["status"], "done")
            self.assertEqual(rec["slug"], "model-compare")
            self.assertTrue(rec["verify_ok"])
            self.assertIn("tsc --noEmit", env.get("log_tail", ""))
            self.assertIn("model-compare", env["task"])
            self.assertNotIn("DEVIN_API_TOKEN", json.dumps(env))
            self.assertIn("fields", env)
            self.assertIn("verify_ok", env["fields"])
            self.assertTrue(any("Session summary" in w
                                for w in env["workflows"]))

    def test_run_view_enrichment(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = make_ctx(Path(td))
            runs = Path(td) / "data" / "runs"
            rk = run_key_for(runs, "20261014-235630-apartment-play-edit")
            env = ctx.enrich({"view": "run", "refs": {"run_key": rk}})
            self.assertIn("apartment-play-edit", env["title"])
            steps = env["record"]["steps"]
            self.assertTrue(any(s["step"] == "third-person-camera-stability"
                                for s in steps))
            self.assertEqual(env["record"]["counts"]["done"], 3)

    def test_summary_view_uses_persisted_file(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = make_ctx(Path(td))
            runs = Path(td) / "data" / "runs"
            rk = run_key_for(runs, "20261016-013639-model-compare-mobile")
            env = ctx.enrich({
                "view": "summary",
                "refs": {"summary_key": rk + "|"
                                       "mobile-visualizations-and-filters"}})
            self.assertIn("Verification (all green)", env["excerpt"])
            self.assertTrue(env["excerpt_origin"].endswith(".summary.md"))

    def test_request_and_unknown_views(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = make_ctx(Path(td))
            env = ctx.enrich({"view": "request",
                              "refs": {"request_id":
                                       "20261016-194008-703a9675"}})
            self.assertEqual(env["record"]["stage"], "ready")
            self.assertIn("camera-work", json.dumps(env["record"]))
            env2 = ctx.enrich({"view": "weird-panel", "refs": {}})
            self.assertIn("no server-side provider", env2["note"])
            # unresolvable refs degrade honestly, not to guesses
            env3 = ctx.enrich({"view": "agent",
                               "refs": {"run_key": "nonexistent"}})
            self.assertIn("not_found", env3)

    def test_custom_provider_registration(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = make_ctx(Path(td))
            ctx.register_provider(
                "builds", lambda r, f, s: {"title": "builds panel",
                                           "record": {"n": 3}})
            env = ctx.enrich({"view": "builds", "refs": {}})
            self.assertEqual(env["record"]["n"], 3)


class CoreIntegrationTests(unittest.TestCase):
    def _core(self, td, enricher=None, retrieval=None):
        data = Path(td) / "data"
        runs, reqs = make_tree(Path(td))
        store = Store(data / "assistant.db",
                      now=lambda: 1900000000.0)
        return AssistantCore(
            store,
            enricher=enricher,
            retrieval=retrieval), runs

    def test_post_message_enriches_and_freezes(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = make_ctx(Path(td))
            core, runs = self._core(td, enricher=ctx.enrich)
            rk = run_key_for(runs, "20261014-235630-apartment-play-edit")
            cv = core.create_conversation({"view": "agent",
                                         "refs": {"run_key": rk,
                                                  "step": "s"}})
            r = core.post_message(
                cv["id"], "what ran?", client_msg_id="cm-host-0001",
                client_ctx={"view": "agent",
                            "refs": {"run_key": rk,
                                     "step":
                                     "third-person-camera-stability"},
                            "enriched": {"spoofed": "fake data"},
                            "filters": {"only": "failed"}})
            env = ctxmod.thaw(r["message"]["ctx_ref"])
            self.assertEqual(env["view"], "agent")
            self.assertIn("third-person-camera-stability",
                          json.dumps(env["enriched"]))
            # client-claimed enrichment is REPLACED, never trusted
            self.assertNotIn("spoofed", json.dumps(env["enriched"]))
            core.close()

    def test_enricher_failure_is_recorded_not_fatal(self):
        def boom(env):
            raise RuntimeError("no runs")
        with tempfile.TemporaryDirectory() as td:
            core, _ = self._core(td, enricher=boom)
            cv = core.create_conversation({"view": "dashboard"})
            r = core.post_message(cv["id"], "hi", client_msg_id="cm-err-0001")
            env = ctxmod.thaw(r["message"]["ctx_ref"])
            self.assertIn("context_error", env["enriched"])
            core.close()

    def test_per_turn_provenance_survives_view_changes(self):
        with tempfile.TemporaryDirectory() as td:
            ctx = make_ctx(Path(td))
            core, runs = self._core(td, enricher=ctx.enrich)
            rk = run_key_for(runs, "20261014-235630-apartment-play-edit")
            cv = core.create_conversation({"view": "dashboard"})
            m1 = core.post_message(
                cv["id"], "q1", client_msg_id="cm-prov-001",
                client_ctx={"view": "agent",
                            "refs": {"run_key": rk,
                                     "step":
                                     "third-person-camera-stability"}})
            m2 = core.post_message(cv["id"], "q2", client_msg_id="cm-prov-002",
                                   client_ctx={"view": "dashboard"})
            e1 = ctxmod.thaw(m1["message"]["ctx_ref"])
            e2 = ctxmod.thaw(m2["message"]["ctx_ref"])
            self.assertEqual(e1["view"], "agent")
            self.assertEqual(e2["view"], "dashboard")
            # turn-1 context is frozen history, unchanged by later nav
            self.assertIn("third-person-camera-stability",
                          e1["enriched"]["title"])
            core.close()

    def test_exchange_sources_persist_and_thaw(self):
        with tempfile.TemporaryDirectory() as td:
            core, _ = self._core(td)
            self.assertEqual(core.store.user_version(), SCHEMA_VERSION)
            cv = core.create_conversation({"view": "dashboard"})
            r = core.post_message(cv["id"], "q", client_msg_id="cm-src-0001")
            xid = r["exchange"]["id"]
            sources = [{"kind": "agent", "key": "request:x|s1",
                        "label": "run / s1",
                        "nav": {"view": "agent",
                                "key": "request:x|s1"},
                        "excerpt": "...", "bytes": 10}]
            core.begin_exchange(xid)
            core.record_sources(xid, sources)
            core.complete_exchange(xid, "done text")
            v = core.exchange_view(xid)
            self.assertEqual(v["sources"][0]["key"], "request:x|s1")
            self.assertEqual(v["sources"][0]["nav"]["view"], "agent")
            # and still there through the raw store row
            self.assertIsInstance(
                json.loads(core.store.get_exchange(xid)["sources"]), list)
            core.close()

    def test_open_core_defaults_wire_host_adapters(self):
        import assistant_host.conductor_adapters as host
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data" / "assistant"
            core = host.open_core(
                data_dir=data, runs_dir=Path(td) / "data" / "runs",
                requests_dir=Path(td) / "data" / "requests")
            self.assertIsInstance(core.retrieval, ConductorRetrieval)
            self.assertTrue(host.static_preamble())
            cv = core.create_conversation({"view": "dashboard"})
            r = core.post_message(cv["id"], "hi", client_msg_id="cm-wire-001",
                                  client_ctx={"view": "dashboard"})
            env = ctxmod.thaw(r["message"]["ctx_ref"])
            self.assertIn("runs_total",
                          env["enriched"]["record"])
            core.close()


if __name__ == "__main__":
    unittest.main()
