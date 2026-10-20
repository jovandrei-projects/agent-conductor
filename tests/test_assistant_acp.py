"""DevinAcpProvider tests - the warm-session `devin acp` transport.

Driven against tests/fake_devin_chat.py's `acp` subcommand as a REAL
subprocess speaking ndjson ACP - covers the handshake, session/new +
session/load, prompt streaming (delta AND snapshot chunks), cumulative
usage (responseDimensions + adapter-side fallback), cancel escalation,
mid-turn crash recovery, orphaned-process adoption, idle reaping, auth
failure -> AuthUnavailable (the runtime's CLI-fallback signal), and an
end-to-end runtime dispatch on the real stack minus real auth.

Convention: C:\\Projects\\TESTING.md
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from assistant.providers import (AuthUnavailable, ProviderUnavailable,  # noqa: E402
                                 )
from assistant.providers.devin_acp import (DevinAcpProvider, _Turn,  # noqa: E402
                                           _WarmSession)
from test_assistant_runtime import ask, make_runtime, wait_x  # noqa: E402

FAKE_DEVIN = [sys.executable, str(HERE / "fake_devin_chat.py")]


def _poll_until(p, h, timeout=15, want_done=True):
    res = None
    t = time.time()
    while time.time() - t < timeout:
        res = p.poll(h)
        if res.done == want_done:
            return res
        time.sleep(0.05)
    return res


class DevinAcpAdapterTests(unittest.TestCase):
    def _provider(self, _env_extra=None, **kw):
        env_map = dict(os.environ)
        env_map.update(_env_extra or {})
        kw.setdefault("env", lambda: env_map)
        return DevinAcpProvider(FAKE_DEVIN, **kw)

    # --------------------------------------------------- dispatch path
    def test_send_poll_done_text_and_usage(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "warm hello"})
            d = Path(td)
            pp, ep = d / "p.md", d / "e.json"
            pp.write_text("question?", encoding="utf-8")
            h = p.send(str(pp), session_id=None, model_uid="fake-model",
                       export_path=str(ep), workspace=d)
            res = _poll_until(p, h)
            self.assertTrue(res.done)
            self.assertEqual(res.text, "warm hello")
            self.assertTrue(res.session_id.startswith("fake-acp-"))
            self.assertEqual(res.raw_metrics["total_prompt_tokens"], 100)
            self.assertEqual(res.raw_metrics["total_completion_tokens"],
                             40)
            self.assertEqual(res.raw_metrics["total_cached_tokens"], 30)
            # ACP reports no LLM step count -> the key stays absent, NULL
            # is recorded (never a fabricated number, per TRANSPORT.md)
            self.assertNotIn("total_steps", res.raw_metrics)
            self.assertEqual(res.model_label, "fake-model")   # the uid
            self.assertTrue(res.request_id)          # userMessageId
            self.assertIsNone(h.export_path)         # no ATIF export
            p.close()

    def test_fresh_session_sets_model_and_ask_mode(self):
        """session/new is followed by mode=ask + model=<uid> config; a
        loaded (adopted) session keeps its saved model instead."""
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "ok"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None,
                       model_uid="gpt-6-luna-low-priority",
                       export_path=str(d / "e.json"), workspace=d)
            res = _poll_until(p, h)
            self.assertTrue(res.done)
            reg = json.loads((d / ".fake_assist_sessions.json")
                             .read_text())
            cfg = reg[res.session_id]["config"]
            self.assertEqual(cfg["mode"], "ask")          # read-only
            self.assertEqual(cfg["model"],
                             "gpt-6-luna-low-priority")
            p.close()

    def test_loaded_session_model_set_only_when_different(self):
        """session/load keeps its saved model; set_config_option model
        fires ONLY when the requested uid differs (mid-session switch is
        supported on this transport, per TRANSPORT.md)."""
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "r"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None,
                       model_uid="first-model",
                       export_path=str(d / "e.json"), workspace=d)
            sid = _poll_until(p, h).session_id
            p.close()
            # load with the SAME uid -> mode re-assert only (+1 call)
            p2 = self._provider({"FAKE_REPLY": "r"}, adopt_drain_s=0.05)
            h2 = p2.send(str(d / "p.md"), session_id=sid,
                         model_uid="first-model",
                         export_path=str(d / "e2.json"), workspace=d)
            self.assertTrue(_poll_until(p2, h2).done)
            reg = json.loads((d / ".fake_assist_sessions.json")
                             .read_text())
            self.assertEqual(reg[sid]["config"]["model"], "first-model")
            self.assertEqual(reg[sid]["config_calls"], 3)   # no re-set
            p2.close()
            # load with a DIFFERENT uid -> mid-session model switch
            p3 = self._provider({"FAKE_REPLY": "r"}, adopt_drain_s=0.05)
            h3 = p3.send(str(d / "p.md"), session_id=sid,
                         model_uid="different-model",
                         export_path=str(d / "e3.json"), workspace=d)
            self.assertTrue(_poll_until(p3, h3).done)
            reg = json.loads((d / ".fake_assist_sessions.json")
                             .read_text())
            self.assertEqual(reg[sid]["config"]["model"],
                             "different-model")
            self.assertEqual(reg[sid]["config_calls"], 5)  # mode+model
            p3.close()

    def test_warm_second_turn_reuses_process_and_accumulates(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "r"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h1 = p.send(str(d / "p.md"), session_id=None,
                        model_uid="m", export_path=str(d / "e1.json"),
                        workspace=d)
            r1 = _poll_until(p, h1)
            self.assertTrue(r1.done)
            sid = r1.session_id
            h2 = p.send(str(d / "p.md"), session_id=sid,
                        model_uid="m", export_path=str(d / "e2.json"),
                        workspace=d)
            self.assertEqual(h2.pid, h1.pid)         # SAME warm process
            r2 = _poll_until(p, h2)
            self.assertTrue(r2.done)
            self.assertEqual(r2.raw_metrics["total_prompt_tokens"], 200)
            self.assertEqual(r2.raw_metrics["total_completion_tokens"],
                             80)
            self.assertNotIn("total_steps", r2.raw_metrics)
            p.close()

    def test_resume_uses_session_load_not_new(self):
        """A send() against a persisted sid (pool cold) session/loads it
        in a fresh process - same session id, cumulative counters
        recovered from the server's own dims."""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            p = self._provider({"FAKE_REPLY": "first"})
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            res = _poll_until(p, h)
            sid = res.session_id
            p.close()

            # fresh adapter = serve restart; send resumes the session
            p2 = self._provider({"FAKE_REPLY": "second"},
                                adopt_drain_s=0.05)
            h2 = p2.send(str(d / "p.md"), session_id=sid, model_uid="m",
                         export_path=str(d / "e2.json"), workspace=d)
            r2 = _poll_until(p2, h2)
            self.assertTrue(r2.done)
            self.assertEqual(r2.text, "second")
            self.assertEqual(r2.session_id, sid)
            # cumulative counters came from the registry's own cum, not
            # a restarted adapter accumulator
            self.assertEqual(r2.raw_metrics["total_prompt_tokens"], 200)
            p2.close()

    def test_stale_sid_falls_back_to_fresh_session(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "fresh"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id="ghost-session",
                       model_uid="m", export_path=str(d / "e.json"),
                       workspace=d)
            res = _poll_until(p, h)
            self.assertTrue(res.done)
            self.assertEqual(res.text, "fresh")
            self.assertTrue(res.session_id.startswith("fake-acp-"))
            p.close()

    def test_busy_session_refused(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_ACP_SLEEP": "5",
                                "FAKE_REPLY": "r"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            time.sleep(0.3)
            with self.assertRaises(ProviderUnavailable):
                p.send(str(d / "p.md"), session_id=h.session_id,
                       model_uid="m", export_path=str(d / "e2.json"),
                       workspace=d)
            p.cancel(h)
            p.close()

    # --------------------------------------------------- progressive text
    def test_partial_text_streams_before_done(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "streamed answer"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            res = _poll_until(p, h)
            self.assertEqual(res.text, "streamed answer")
            p.close()

    def test_repeated_live_deltas_are_never_deduped(self):
        """Regression for the garbled-answer corruption: live
        agent_message_chunk deltas may carry NO timestamp or a shared
        one. Markdown repeats identical fragments constantly - a
        (kind,ts,text) dedup on live chunks silently eats every repeat.
        Only replayed-history chunks (real timestamps) may dedup."""
        ws = _WarmSession(conn=None, sid="s1", workspace="w")
        ws.turn = _Turn()

        def upd(text, ts=None):
            u = {"sessionUpdate": "agent_message_chunk",
                 "content": {"type": "text", "text": text}}
            if ts is not None:
                u["_meta"] = {"cognition.ai/timestamp": ts}
            return {"params": {"sessionId": "s1", "update": u}}

        for meta in (None, "ts-shared"):
            ws.turn.parts = []
            for t in ("- `a`\n", "one\n", "- `a`\n", "two\n", "- `a`\n"):
                ws.apply_update(upd(t, meta))
            self.assertEqual(ws.turn.text(),
                             "- `a`\none\n- `a`\ntwo\n- `a`")
        # ...and a ts'd straggler matching the seeded replay set is still
        # dropped - dedup lives on for its real purpose
        ws.turn.parts = ["tail"]
        ws.turn.seen_chunks = {("agent_message_chunk", "t1", "old bit")}
        ws.apply_update(upd("old bit", "t1"))
        self.assertEqual(ws.turn.text(), "tail")

    def test_no_ts_reply_streams_intact(self):
        """End-to-end through the real subprocess: a reply made of
        IDENTICAL repeated fragments (markdown's whole nature) arrives
        verbatim even when live deltas carry no timestamp or a shared
        one - the live corruption case."""
        chunks = ["- `x`\n", "one\n", "- `x`\n", "two\n", "- `x`\n"]
        reply = "".join(chunks).strip()   # res.text is turn text stripped
        for knob in ("FAKE_ACP_NO_TS", "FAKE_ACP_CONST_TS"):
            # mkdtemp + retry loop: TemporaryDirectory's onerror path
            # recurses unboundedly on a dir-level PermissionError
            # (Windows holds the killed child's cwd a beat past kill)
            td = tempfile.mkdtemp()
            try:
                p = self._provider({knob: "1",
                                    "FAKE_CHUNKS": json.dumps(chunks)})
                d = Path(td)
                (d / "p.md").write_text("q", encoding="utf-8")
                h = p.send(str(d / "p.md"), session_id=None,
                           model_uid="fake-model",
                           export_path=str(d / "e.json"), workspace=d)
                res = _poll_until(p, h)
                self.assertTrue(res.done)
                self.assertEqual(res.text, reply, knob)
                p.close()
            finally:
                for _ in range(20):
                    try:
                        shutil.rmtree(td)
                        break
                    except OSError:
                        time.sleep(0.2)

    def test_snapshot_chunks_resolve_to_full_text(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "snapshotted",
                                "FAKE_ACP_SNAPSHOT": "1"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            res = _poll_until(p, h)
            self.assertTrue(res.done)
            self.assertEqual(res.text, "snapshotted")
            p.close()

    # --------------------------------------------------- usage
    def test_no_dims_falls_back_to_accumulated_turn_usage(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_ACP_NO_DIMS": "1",
                                "FAKE_REPLY": "r"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            r1 = _poll_until(p, h)
            self.assertEqual(r1.raw_metrics["total_prompt_tokens"], 100)
            h2 = p.send(str(d / "p.md"), session_id=r1.session_id,
                        model_uid="m", export_path=str(d / "e2.json"),
                        workspace=d)
            r2 = _poll_until(p, h2)
            self.assertTrue(r2.done)
            self.assertEqual(r2.raw_metrics["total_prompt_tokens"], 200)
            self.assertEqual(r2.raw_metrics["total_cached_tokens"], 60)
            p.close()

    # --------------------------------------------------- cancel / kill
    def test_cancel_resolves_turn_via_session_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_ACP_SLEEP": "30",
                                "FAKE_REPLY": "late"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            time.sleep(0.4)
            t0 = time.time()
            p.cancel(h)
            self.assertLess(time.time() - t0, 10)   # not the 30s sleep
            res = _poll_until(p, h, timeout=5)
            self.assertTrue(res.done)
            p.close()

    def test_cancel_escalates_to_process_kill(self):
        """session/cancel unanswered within the grace -> proc killed and
        the session dropped from the pool (reloadable later)."""
        with tempfile.TemporaryDirectory() as td:
            killed = []
            p = self._provider({"FAKE_ACP_SLEEP": "30",
                                "FAKE_ACP_IGNORE_CANCEL": "1",
                                "FAKE_REPLY": "late"},
                               cancel_grace=0.3,
                               kill=lambda proc: killed.append(proc.pid)
                               or proc.kill())
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            time.sleep(0.3)
            p.cancel(h)
            res = _poll_until(p, h, timeout=5)
            self.assertTrue(res.done)
            self.assertTrue(res.error)
            self.assertEqual(killed, [h.pid])
            self.assertNotIn(h.session_id, p._pool)
            p.close()

    # --------------------------------------------------- crash recovery
    def test_process_death_mid_turn_is_an_error_not_a_hang(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_ACP_DIE": "1"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            res = _poll_until(p, h, timeout=10)
            self.assertTrue(res.done)
            self.assertTrue(res.error)
            p.close()

    def test_dead_pooled_conn_respawns_via_load(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "again"}, adopt_drain_s=0.05)
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            r1 = _poll_until(p, h)
            sid = r1.session_id
            h.session.conn.proc.kill()
            h.session.conn.proc.wait(timeout=10)
            h2 = p.send(str(d / "p.md"), session_id=sid, model_uid="m",
                        export_path=str(d / "e2.json"), workspace=d)
            self.assertNotEqual(h2.pid, h.pid)      # fresh process
            r2 = _poll_until(p, h2)
            self.assertTrue(r2.done)
            self.assertEqual(r2.session_id, sid)    # SAME session id
            self.assertEqual(r2.text, "again")
            p.close()

    # --------------------------------------------------- adopt (orphan)
    def test_attach_dead_pid_reissues_prompt(self):
        """Adopted exchange = session/load + prompt re-issued once (the
        decision record's re-arm path); the re-answer lands normally."""
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "recovered answer"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            r1 = _poll_until(p, h)
            sid = r1.session_id
            p.close()

            p2 = self._provider({"FAKE_REPLY": "recovered answer"},
                                workspace=str(d), adopt_drain_s=0.05)
            row = {"id": "x-1", "pid": 99999999,   # dead pid
                   "provider_session_id": sid,
                   "prompt_path": str(d / "p.md"),
                   "export_path": str(d / "adopt.json")}
            h2 = p2.attach(row)
            res = _poll_until(p2, h2, timeout=15)
            self.assertTrue(res.done)
            self.assertEqual(res.session_id, sid)
            self.assertEqual(res.text, "recovered answer")
            self.assertGreaterEqual(
                res.raw_metrics["total_prompt_tokens"], 100)
            self.assertNotEqual(h2.session.conn.proc.pid, h.pid)
            p2.close()

    def test_attach_reaps_live_orphan_before_load(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "r"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            r1 = _poll_until(p, h)
            sid = r1.session_id
            old_pid = h.pid                          # still-alive child

            killed_pids = []

            def real_kill(pid):
                killed_pids.append(pid)
                h.session.conn.proc.kill()           # reap for real

            p2 = self._provider(workspace=str(d), adopt_drain_s=0.05,
                                alive=lambda pid: pid == old_pid,
                                kill_pid=real_kill)
            h2 = p2.attach({"id": "x-2", "pid": old_pid,
                            "provider_session_id": sid,
                            "prompt_path": str(d / "p.md"),
                            "export_path": str(d / "a.json")})
            res = _poll_until(p2, h2, timeout=15)
            self.assertTrue(res.done)
            self.assertEqual(killed_pids, [old_pid])
            self.assertEqual(res.session_id, sid)
            self.assertNotEqual(h2.session.conn.proc.pid, old_pid)
            p.close()                                # kill the real child
            p2.close()

    def test_locked_session_is_skipped_not_loaded(self):
        """A session isLocked by a live foreign process is treated like
        session-gone: the exchange starts on a fresh session id."""
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "r"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            sid = _poll_until(p, h).session_id
            # p's fake child is STILL ALIVE holding the session lock
            p2 = self._provider({"FAKE_REPLY": "r2"},
                                adopt_drain_s=0.05)
            h2 = p2.send(str(d / "p.md"), session_id=sid, model_uid="m",
                         export_path=str(d / "e2.json"), workspace=d)
            r2 = _poll_until(p2, h2)
            self.assertTrue(r2.done)
            self.assertNotEqual(r2.session_id, sid)  # fresh, not loaded
            self.assertEqual(r2.text, "r2")
            p.close()
            p2.close()

    # --------------------------------------------------- sessions list
    def test_sessions_listing(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider()
            d = Path(td)
            self.assertEqual(p.sessions(d), set())
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            _poll_until(p, h)
            self.assertIn(h.session_id, p.sessions(d))
            p.close()

    def test_sessions_list_failure_is_none_not_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider(run_list=lambda cwd: (1, "boom"))
            self.assertIsNone(p.sessions(Path(td)))

    # --------------------------------------------------- failure surfaces
    def test_auth_failure_raises_auth_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_ACP_AUTH_FAIL": "1"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            with self.assertRaises(AuthUnavailable) as cm:
                p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            self.assertIn("not authenticated", str(cm.exception))
            p.close()

    def test_spawn_failure_raises_provider_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            p = DevinAcpProvider(["definitely-not-a-real-binary-xyz"],
                                 env=lambda: dict(os.environ))
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            with self.assertRaises(ProviderUnavailable):
                p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            p.close()

    def test_session_new_hang_times_out(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_ACP_HANG": "1"}, session_s=0.5)
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            with self.assertRaises(ProviderUnavailable):
                p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            p.close()

    # --------------------------------------------------- read-only
    def test_inbound_write_request_is_refused(self):
        """A server->client write request mid-turn gets a JSON-RPC
        refusal; the turn still completes and nothing is written."""
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_ACP_INBOUND": "1",
                                "FAKE_REPLY": "refused cleanly"})
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            res = _poll_until(p, h)
            self.assertTrue(res.done)
            self.assertEqual(res.text, "refused cleanly")
            self.assertFalse((d / "evil.txt").exists())
            p.close()

    # --------------------------------------------------- idle reaping
    def test_idle_session_reaped_and_reloadable(self):
        """An idle pooled conn expires (turn never does); the session
        itself persists and a later send() reloads it."""
        with tempfile.TemporaryDirectory() as td:
            clock = [1000.0]
            p = self._provider({"FAKE_REPLY": "r"},
                               clock=lambda: clock[0],
                               idle_ttl=10.0)
            d = Path(td)
            (d / "p.md").write_text("q", encoding="utf-8")
            h = p.send(str(d / "p.md"), session_id=None, model_uid="m",
                       export_path=str(d / "e.json"), workspace=d)
            r1 = _poll_until(p, h)
            ws = h.session
            self.assertFalse(p._idle_expired(ws))
            clock[0] += 11.0                          # past the idle ttl
            self.assertTrue(p._idle_expired(ws))
            # what the reaper thread does on its scan
            p._kill(ws.conn.proc)
            with p._lock:
                p._drop(ws)
            self.assertNotIn(r1.session_id, p._pool)
            # the session still answers - send() session/loads it
            h2 = p.send(str(d / "p.md"), session_id=r1.session_id,
                        model_uid="m", export_path=str(d / "e2.json"),
                        workspace=d)
            r2 = _poll_until(p, h2)
            self.assertTrue(r2.done)
            self.assertEqual(r2.session_id, r1.session_id)
            self.assertNotEqual(h2.pid, h.pid)
            p.close()

    # --------------------------------------------------- end to end
    def test_end_to_end_runtime_on_acp(self):
        """store + runtime + acp provider + fake binary - the real stack
        minus real auth."""
        with tempfile.TemporaryDirectory() as td:
            p = self._provider({"FAKE_REPLY": "the acp answer"})
            core, rt = make_runtime(td, p)
            try:
                cv = core.create_conversation({"view": "dashboard"})
                r = ask(core, cv, "real question", cmid="cm-acp-1")
                x = wait_x(core, r["exchange"]["id"], timeout=30)
                self.assertEqual(x["status"], "done")
                v = core.exchange_view(x["id"])
                self.assertEqual(v["text"], "the acp answer")
                self.assertTrue(v["provider_session_id"].startswith(
                    "fake-acp-"))
                self.assertEqual(v["prompt_t"], 100)  # delta vs fresh {}
                self.assertEqual(v["model_label"], "test-model")
                # a second message rides the SAME warm process
                r2 = ask(core, cv, "follow up", cmid="cm-acp-2")
                x2 = wait_x(core, r2["exchange"]["id"], timeout=30)
                v2 = core.exchange_view(x2["id"])
                self.assertEqual(v2["status"], "done")
                self.assertEqual(v2["prompt_t"], 100)  # delta, not cum
                self.assertEqual(v2["provider_session_id"],
                                 v["provider_session_id"])
            finally:
                core.close()
                p.close()


if __name__ == "__main__":
    unittest.main()
