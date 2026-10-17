"""Tests for the assistant chat core (assistant/ + assistant_host/).

Everything runs on tempdir sqlite stores and injected fakes - no auth, no
provider, no server. Convention: C:\\Projects\\TESTING.md
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from assistant import context as ctxmod          # noqa: E402
from assistant import http_api                   # noqa: E402
from assistant.actions import ActionAdapter, ActionSpec  # noqa: E402
from assistant.core import AssistantCore, AssistantError  # noqa: E402
from assistant.providers import usage_delta      # noqa: E402
from assistant.store import SCHEMA_VERSION, Store, new_id  # noqa: E402


class Clock:
    """Deterministic epoch clock - same-second ids stay unique via
    token_hex; ordering is proven through seq/pagination, not stamps."""

    def __init__(self, t=1_760_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s=1.0):
        self.t += s
        return self.t


def make_core(td, *, clock=None, **kw):
    root = Path(td)
    clk = clock or Clock()
    store = Store(root / "assistant" / "assistant.db", now=clk)
    kw.setdefault("attachments_dir", root / "attachments")
    return AssistantCore(store, now=clk, **kw)


def conv(core, view="dashboard", **kw):
    return core.create_conversation(
        {"view": view, "refs": kw.pop("refs", {"run_key": "r1"}),
         "window": "win-1"}, **kw)


def ask(core, cid, text="q", cmid=None, cctx=None, **kw):
    return core.post_message(cid, text,
                             client_msg_id=cmid or new_id("c"),
                             client_ctx=cctx, **kw)


def finish(core, xid, text="a", **kw):
    return core.complete_exchange(
        xid, text, usage={"prompt_t": 100, "completion_t": 40,
                          "cached_t": 30, "llm_steps": 2},
        model={"uid": "gpt-6-luna-low-priority",
               "label": "GPT-6 Luna Low Thinking Fast"},
        price={"in": 0.2, "cached": 0.02, "out": 1.0,
               "fetched_at": 1_760_000_000.0, "source": "docs"},
        **kw)


class StoreMigrationTests(unittest.TestCase):
    def test_fresh_db_gets_v1_schema(self):
        with tempfile.TemporaryDirectory() as td:
            st = Store(Path(td) / "assistant.db")
            self.assertEqual(st.user_version(), SCHEMA_VERSION)
            names = {r["name"] for r in st.query(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"conversations", "messages", "exchanges",
                             "actions", "attachments", "settings"}
                            <= names)
            st.close()

    def test_migration_is_additive_and_preserves_rows(self):
        """A later migration adds columns without touching stored data -
        the path future schema changes must take."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "a.db"
            st = Store(db)
            st.execute("INSERT INTO conversations(id, title, created_at,"
                       " updated_at, last_active_at) VALUES('conv-1','t',"
                       " 'x','x','x')")
            st.close()
            st = Store(db, migrations=[
                (1, []),                 # baseline already applied
                (2, []),                 # exchanges.sources (applied too)
                (3, []),                 # exchanges.requests (applied too)
                (4, []),                 # actions.explain (applied too)
                (5, ["ALTER TABLE conversations ADD COLUMN note TEXT"]),
            ])
            self.assertEqual(st.user_version(), 5)
            row = st.query_one("SELECT * FROM conversations")
            self.assertEqual(row["title"], "t")
            self.assertIn("note", row)   # new column, old row intact
            st.close()

    def test_persistence_across_reopen(self):
        """Close + reopen = the 'serve restart' case: everything the client
        wrote is still there."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "assistant" / "assistant.db"
            c1 = make_core(td)
            c = conv(c1)
            r = ask(c1, c["id"], "hello", cmid="cm-000001")
            finish(c1, r["exchange"]["id"])
            c1.close()
            c2 = make_core(td)
            got = c2.get_conversation(c["id"])
            self.assertEqual(got["id"], c["id"])
            msgs = c2.list_messages(c["id"])["items"]
            self.assertEqual([m["role"] for m in msgs],
                             ["user", "assistant"])
            self.assertEqual(msgs[1]["text"], "a")
            x = c2.list_exchanges(c["id"])
            self.assertEqual(x[0]["prompt_t"], 100)
            c2.close()


class ConversationTests(unittest.TestCase):
    def test_create_origin_and_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c, view="agent",
                      refs={"run_key": "r", "step": "s1"}, title="Hi")
            self.assertTrue(cv["id"].startswith("conv-"))
            self.assertTrue(cv["readonly"])            # default ON
            self.assertEqual(cv["origin_view"]["view"], "agent")
            self.assertEqual(cv["origin_view"]["refs"]["step"], "s1")
            self.assertEqual(cv["origin_view"]["window"], "win-1")
            c.close()

    def test_client_ref_dedupes_create(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            a = conv(c, client_ref="web-uuid-1")
            b = conv(c, client_ref="web-uuid-1")
            self.assertEqual(a["id"], b["id"])
            self.assertTrue(b["deduped"])
            self.assertEqual(len(c.list_conversations()["items"]), 1)
            c.close()

    def test_reopen_and_last_active(self):
        with tempfile.TemporaryDirectory() as td:
            clock = Clock()
            c = make_core(td, clock=clock)
            a = conv(c, title="first")
            clock.tick(60)
            b = conv(c, title="second")
            self.assertEqual(c.last_active_conversation()["id"], b["id"])
            c.open_conversation(a["id"])
            self.assertEqual(c.last_active_conversation()["id"], a["id"])
            # and it is what a fresh process restores:
            c.close()
            c2 = make_core(td, clock=clock)
            self.assertEqual(c2.last_active_conversation()["id"], a["id"])
            c2.close()

    def test_bad_ids_are_404_not_errors(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            for bad in ("", "../x", "conv-!", "x" * 200, None):
                with self.assertRaises(AssistantError) as e:
                    c.get_conversation(bad)
                self.assertEqual(e.exception.status, 404)
            c.close()

    def test_conversation_pagination(self):
        with tempfile.TemporaryDirectory() as td:
            clock = Clock()
            c = make_core(td, clock=clock)
            ids = []
            for i in range(7):
                ids.append(conv(c, title=f"c{i}")["id"])
                clock.tick(2)
            p1 = c.list_conversations(limit=3)
            self.assertEqual([r["title"] for r in p1["items"]],
                             ["c6", "c5", "c4"])        # newest first
            p2 = c.list_conversations(limit=3, before=p1["next_before"])
            self.assertEqual([r["title"] for r in p2["items"]],
                             ["c3", "c2", "c1"])
            p3 = c.list_conversations(limit=3, before=p2["next_before"])
            self.assertEqual([r["title"] for r in p3["items"]], ["c0"])
            self.assertIsNone(p3["next_before"])
            c.close()

    def test_message_pagination(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            for i in range(5):
                ask(c, cv["id"], f"q{i}")
            msgs = c.list_messages(cv["id"], limit=100)["items"]
            self.assertEqual(len(msgs), 10)            # 5 user + 5 pending
            p1 = c.list_messages(cv["id"], limit=4)
            self.assertEqual(len(p1["items"]), 4)      # newest page last
            p2 = c.list_messages(cv["id"], limit=4,
                                 before=p1["next_before"])
            self.assertEqual(len(p2["items"]), 4)
            s1 = [m["seq"] for m in p1["items"]]
            s2 = [m["seq"] for m in p2["items"]]
            self.assertEqual(s1, sorted(s1))           # each page chrono
            self.assertEqual(s2, sorted(s2))
            self.assertFalse(set(s1) & set(s2))        # no dup rows
            self.assertGreater(min(s1), max(s2))       # p2 is older
            rest = c.list_messages(cv["id"], limit=4,
                                   before=p2["next_before"])
            self.assertEqual(len(rest["items"]), 2)
            self.assertIsNone(rest["next_before"])
            c.close()


class ExchangeTests(unittest.TestCase):
    def test_post_creates_exchange_and_pending_reply(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            r = ask(c, cv["id"], "hello")
            self.assertFalse(r["deduped"])
            self.assertEqual(r["exchange"]["status"], "queued")
            self.assertEqual(r["assistant_message"]["status"], "pending")
            self.assertEqual(r["message"]["role"], "user")
            c.close()

    def test_resend_same_client_msg_id_is_not_a_duplicate(self):
        """The retry-safe write contract: a resumed POST lands once."""
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            r1 = ask(c, cv["id"], "hello", cmid="cm-fixed-1")
            r2 = ask(c, cv["id"], "hello", cmid="cm-fixed-1")
            self.assertTrue(r2["deduped"])
            self.assertEqual(r1["exchange"]["id"], r2["exchange"]["id"])
            self.assertEqual(
                [m["role"] for m in
                 c.list_messages(cv["id"])["items"]],
                ["user", "assistant"])
            self.assertEqual(len(c.list_exchanges(cv["id"])), 1)
            c.close()

    def test_resend_after_error_creates_fresh_exchange_same_message(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            r1 = ask(c, cv["id"], "hello", cmid="cm-000001")
            c.fail_exchange(r1["exchange"]["id"], "provider died")
            r2 = ask(c, cv["id"], "hello", cmid="cm-000001")
            self.assertFalse(r2["deduped"])
            self.assertNotEqual(r1["exchange"]["id"], r2["exchange"]["id"])
            self.assertEqual(r2["exchange"]["user_msg_id"],
                             r1["exchange"]["user_msg_id"])
            self.assertEqual(c.store.count_messages(cv["id"]), 3)
            c.close()

    def test_complete_is_terminal_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            r = ask(c, cv["id"], "hi")
            xid = r["exchange"]["id"]
            done = finish(c, xid)
            self.assertEqual(done["status"], "done")
            self.assertEqual(done["prompt_t"], 100)
            again = c.complete_exchange(
                xid, "different", usage={"prompt_t": 999})
            self.assertEqual(again["prompt_t"], 100)   # no double-land
            self.assertEqual(again["text"], "a")
            c.close()

    def test_provider_request_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            r1 = ask(c, cv["id"], "one")
            r2 = ask(c, cv["id"], "two")
            c.begin_exchange(r1["exchange"]["id"],
                             provider_session_id="sess-1")
            c.begin_exchange(r2["exchange"]["id"],
                             provider_session_id="sess-1")
            own = c.claim_request(r1["exchange"]["id"], "sess-1", "req-9")
            self.assertFalse(own["dup"])
            dup = c.claim_request(r2["exchange"]["id"], "sess-1", "req-9")
            self.assertTrue(dup["dup"])                # replayed callback
            self.assertEqual(dup["id"], r1["exchange"]["id"])
            c.close()

    def test_cancelled_and_failed_keep_partial_usage(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            r = ask(c, cv["id"], "work")
            xid = r["exchange"]["id"]
            c.begin_exchange(xid)
            c.update_partial(xid, "half an answer",
                             usage={"prompt_t": 50})
            done = c.fail_exchange(xid, "boom")
            self.assertEqual(done["status"], "error")
            self.assertEqual(done["prompt_t"], 50)     # usage kept
            self.assertEqual(done["text"], "half an answer")
            r2 = ask(c, cv["id"], "again")
            c.begin_exchange(r2["exchange"]["id"])
            cx = c.cancel_exchange(r2["exchange"]["id"])
            self.assertEqual(cx["status"], "cancelled")
            c.close()

    def test_usage_rollup_counts_unpriced(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            finish(c, ask(c, cv["id"], "a")["exchange"]["id"])
            r2 = ask(c, cv["id"], "b")
            c.complete_exchange(r2["exchange"]["id"], "b-answer",
                                usage={"prompt_t": 10, "completion_t": 5},
                                usd=None)              # unpriced: unknown
            u = c.usage(cv["id"])
            self.assertEqual(u["totals"]["prompt_t"], 110)
            self.assertEqual(u["totals"]["exchanges"], 2)
            self.assertEqual(u["totals"]["unpriced"], 1)
            self.assertAlmostEqual(
                u["totals"]["usd"],
                ((100 - 30) * 0.2 + 30 * 0.02 + 40 * 1.0) / 1e6)
            c.close()


class ContextTests(unittest.TestCase):
    def test_ctx_frozen_per_turn(self):
        """Each question keeps the view it was asked from; the next one
        picks up the now-current view."""
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            r1 = ask(c, cv["id"], "from modal", cctx={
                "view": "agent", "refs": {"run_key": "r1", "step": "s1"}})
            r2 = ask(c, cv["id"], "from dashboard", cctx={
                "view": "dashboard", "filters": {"only": "running"}})
            m1 = ctxmod.thaw(r1["message"]["ctx_ref"])
            m2 = ctxmod.thaw(r2["message"]["ctx_ref"])
            self.assertEqual(m1["view"], "agent")
            self.assertEqual(m1["refs"]["step"], "s1")
            self.assertEqual(m2["view"], "dashboard")
            # the assistant reply inherits the same frozen envelope
            self.assertEqual(m1, ctxmod.thaw(
                r1["assistant_message"]["ctx_ref"]))
            c.close()

    def test_sensitive_keys_never_persist(self):
        env = ctxmod.envelope("agent", refs={
            "run_key": "r", "api_token": "sk-1",
            "nested": {"client_secret": "x", "ok": 1}})
        frozen = ctxmod.freeze(env)
        self.assertNotIn("sk-1", frozen)
        self.assertNotIn("client_secret", frozen)
        self.assertIn("run_key", frozen)

    def test_selection_capped_and_bad_view_rejected(self):
        env = ctxmod.envelope("dashboard", selection="x" * 5000)
        self.assertEqual(len(env["selection"]), ctxmod.SELECTION_MAX)
        with self.assertRaises(ctxmod.ContextError):
            ctxmod.envelope("DROP TABLE")
        with self.assertRaises(ctxmod.ContextError):
            ctxmod.new_envelope_from_client("nope")


class SettingsTests(unittest.TestCase):
    def test_snapshot_per_message_survives_later_changes(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c, settings={"model": "gpt-6-luna",
                                   "effort": "low", "fast": True})
            r1 = ask(c, cv["id"], "first")
            c.update_settings(cv["id"], {"model": "swe-2",
                                         "effort": "max", "fast": False})
            r2 = ask(c, cv["id"], "second")
            s1 = json.loads(r1["message"]["settings_json"])
            s2 = json.loads(r2["message"]["settings_json"])
            self.assertEqual((s1["model"], s1["effort"], s1["fast"]),
                             ("gpt-6-luna", "low", True))
            self.assertEqual((s2["model"], s2["effort"], s2["fast"]),
                             ("swe-2", "max", False))
            c.close()

    def test_defaults_inherited_by_new_conversations_only(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            c.set_defaults({"model": "gpt-6-luna", "effort": "low",
                            "fast": True, "readonly": True})
            a = conv(c)
            self.assertEqual(a["model"], "gpt-6-luna")
            self.assertTrue(a["fast"])
            c.set_defaults({"model": "swe-2"})
            self.assertEqual(c.get_conversation(a["id"])["model"],
                             "gpt-6-luna")            # existing untouched
            b = conv(c)
            self.assertEqual(b["model"], "swe-2")
            c.close()

    def test_readonly_defaults_on(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            self.assertEqual(cv["readonly"], 1)
            self.assertTrue(
                json.loads(ask(c, cv["id"], "x")["message"]
                           ["settings_json"])["readonly"])
            c.close()


class ActionAuditTests(unittest.TestCase):
    class FakeActions(ActionAdapter):
        def spec_for(self, kind):
            specs = {"add_numbers": ActionSpec(
                "add_numbers", requires_confirm=True,
                confirm_text="Add these?")}
            return specs.get(kind)

        def validate(self, kind, payload):
            return {"a": int(payload.get("a") or 0),
                    "b": int(payload.get("b") or 0)}

        def execute(self, action):
            p = json.loads(action["payload"])
            return {"sum": p["a"] + p["b"]}

    def test_proposal_requires_whitelisted_kind(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, actions=self.FakeActions())
            cv = conv(c)
            a = c.propose_action(cv["id"], "add_numbers",
                                 {"a": "2", "b": 3})
            self.assertEqual(a["state"], "proposed")
            self.assertTrue(a["requires_confirm"])
            with self.assertRaises(AssistantError) as e:
                c.propose_action(cv["id"], "shell_exec", {})
            self.assertEqual(e.exception.status, 403)
            c.close()

    def test_no_adapter_no_proposals(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            with self.assertRaises(AssistantError):
                c.propose_action(cv["id"], "add_numbers", {})
            c.close()

    def test_readonly_rechecked_at_execute_time(self):
        """Confirm -> flip read-only ON -> execute refuses and stays
        proposed (the pending write is invalidated, not destroyed)."""
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, actions=self.FakeActions())
            cv = conv(c, settings={"readonly": False})
            a = c.propose_action(cv["id"], "add_numbers", {"a": 1, "b": 2})
            c.decide_action(a["id"], True)
            c.update_settings(cv["id"], {"readonly": True})
            with self.assertRaises(AssistantError) as e:
                c.execute_action(a["id"])
            self.assertEqual(e.exception.status, 409)
            a = c.store.get_action(a["id"])
            self.assertEqual(a["state"], "confirmed")  # decision kept
            self.assertIn("read-only", a["result"])
            c.update_settings(cv["id"], {"readonly": False})
            a = c.execute_action(a["id"])
            self.assertEqual(a["state"], "executed")
            self.assertEqual(json.loads(a["result"])["sum"], 3)
            self.assertIsNotNone(a["executed_at"])
            c.close()

    def test_denied_never_executes(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, actions=self.FakeActions())
            cv = conv(c, settings={"readonly": False})
            a = c.propose_action(cv["id"], "add_numbers", {"a": 1, "b": 1})
            c.decide_action(a["id"], False)
            a = c.execute_action(a["id"])
            self.assertEqual(a["state"], "denied")
            c.close()

    def test_execution_failure_is_audited(self):
        class Boom(self.FakeActions):
            def execute(self, action):
                raise RuntimeError("kaboom")
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, actions=Boom())
            cv = conv(c, settings={"readonly": False})
            a = c.propose_action(cv["id"], "add_numbers", {"a": 1, "b": 1})
            c.decide_action(a["id"], True)
            with self.assertRaises(AssistantError):
                c.execute_action(a["id"])
            a = c.store.get_action(a["id"])
            self.assertEqual(a["state"], "failed")
            self.assertIn("kaboom", a["result"])
            c.close()


class AttachmentTests(unittest.TestCase):
    def _saver(self, td):
        root = Path(td) / "attachments"

        def save(data):
            root.mkdir(exist_ok=True)
            p = root / f"{new_id('img')}.png"
            p.write_bytes(data)
            return {"name": p.name, "path": str(p)}
        return save

    def test_upload_dedupe_and_bind(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, attachment_saver=self._saver(td))
            cv = conv(c)
            a1 = c.save_upload(cv["id"], b"\x89PNG fake", client_ref="u1")
            a2 = c.save_upload(cv["id"], b"\x89PNG fake", client_ref="u1")
            self.assertEqual(a1["id"], a2["id"])
            self.assertTrue(a2["deduped"])
            r = ask(c, cv["id"], "look", attachments=[a1["id"]])
            stored = json.loads(r["message"]["attachments"])
            self.assertEqual(stored[0]["name"], a1["name"])
            bound = c.store.list_attachments(message_id=r["message"]["id"])
            self.assertEqual(bound[0]["id"], a1["id"])
            c.close()

    def test_foreign_conversation_attachment_refused(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, attachment_saver=self._saver(td))
            a = conv(c)
            b = conv(c)
            att = c.save_upload(a["id"], b"\x89PNG fake")
            with self.assertRaises(AssistantError) as e:
                ask(c, b["id"], "x", attachments=[att["id"]])
            self.assertEqual(e.exception.status, 404)
            c.close()

    def test_path_outside_attachments_dir_refused(self):
        with tempfile.TemporaryDirectory() as td:
            def bad_saver(data):
                return {"name": "evil.png",
                        "path": str(Path(td) / "elsewhere" / "e.png")}
            c = make_core(td, attachment_saver=bad_saver)
            cv = conv(c)
            with self.assertRaises(AssistantError):
                c.save_upload(cv["id"], b"\x89PNG fake")
            c.close()

    def _real_saver(self, td):
        """The seam the host wires (conductor.save_attachment) confined
        to this test's tempdir - real magic-byte/size validation."""
        import conductor
        adir = Path(td) / "attachments"
        return lambda data: conductor.save_attachment(data, adir)

    def test_real_saver_validates_content_and_records_ctype(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, attachment_saver=self._real_saver(td))
            cv = conv(c)
            png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
            a = c.save_upload(cv["id"], png)
            self.assertEqual(a["ctype"], "image/png")
            self.assertTrue(Path(a["path"]).is_file())
            # non-image bytes are refused by CONTENT (magic bytes), and
            # an empty upload is a 400 - never by filename or header
            with self.assertRaises(AssistantError) as e:
                c.save_upload(cv["id"], b"MZ" + b"x" * 64)
            self.assertEqual(e.exception.status, 415)
            with self.assertRaises(AssistantError) as e:
                c.save_upload(cv["id"], b"")
            self.assertEqual(e.exception.status, 400)
            c.close()

    def test_oversized_upload_is_413(self):
        import conductor
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, attachment_saver=self._real_saver(td))
            cv = conv(c)
            big = b"\x89PNG\r\n\x1a\n" + \
                b"x" * conductor.ATTACH_MAX_BYTES
            with self.assertRaises(AssistantError) as e:
                c.save_upload(cv["id"], big)
            self.assertEqual(e.exception.status, 413)
            c.close()

    def test_saver_errors_keep_their_http_status(self):
        """The host's RequestError is translated to AssistantError with
        the SAME status instead of escaping dispatch as a 500."""
        with tempfile.TemporaryDirectory() as td:
            import conductor

            def saver(data):
                raise conductor.RequestError("way too big", 413)
            c = make_core(td, attachment_saver=saver)
            cv = conv(c)
            with self.assertRaises(AssistantError) as e:
                c.save_upload(cv["id"], b"\x89PNG fake")
            self.assertEqual(e.exception.status, 413)
            self.assertIn("way too big", str(e.exception))
            c.close()

    def test_per_conversation_upload_cap(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, attachment_saver=self._saver(td))
            cv = conv(c)
            with mock.patch("assistant.core.ATTACH_PER_CONV_MAX", 3):
                for _ in range(3):
                    c.save_upload(cv["id"], b"\x89PNG fake")
                with self.assertRaises(AssistantError) as e:
                    c.save_upload(cv["id"], b"\x89PNG fake")
                self.assertEqual(e.exception.status, 409)
            c.close()

    def test_message_attachment_count_cap(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, attachment_saver=self._saver(td))
            cv = conv(c)
            ids = [c.save_upload(cv["id"], b"\x89PNG fake")["id"]
                   for _ in range(21)]
            with self.assertRaises(AssistantError) as e:
                ask(c, cv["id"], "x", attachments=ids)
            self.assertEqual(e.exception.status, 400)
            c.close()

    def test_bound_attachment_cannot_be_rebound(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, attachment_saver=self._saver(td))
            cv = conv(c)
            a = c.save_upload(cv["id"], b"\x89PNG fake")
            ask(c, cv["id"], "one", attachments=[a["id"]])
            with self.assertRaises(AssistantError) as e:
                ask(c, cv["id"], "two", attachments=[a["id"]])
            self.assertEqual(e.exception.status, 409)
            c.close()

    def test_read_upload_round_trip_and_gates(self):
        """The preview route's backing call: bytes come back by row id
        with the recorded image type; bad ids, foreign paths, missing
        files and non-image ctype rows are all refused."""
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, attachment_saver=self._real_saver(td))
            cv = conv(c)
            png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
            a = c.save_upload(cv["id"], png)
            data, ctype = c.read_upload(a["id"])
            self.assertEqual(data, png)
            self.assertEqual(ctype, "image/png")
            for bad in ("att-nope", "att-../../x", "", "x" * 200, None):
                with self.assertRaises(AssistantError) as e:
                    c.read_upload(bad)
                self.assertEqual(e.exception.status, 404)
            # a tampered ctype can never turn the route into an
            # html/script-serving endpoint
            c.store.execute(
                "UPDATE attachments SET ctype='text/html' WHERE id=?",
                (a["id"],))
            _, ctype = c.read_upload(a["id"])
            self.assertEqual(ctype, "application/octet-stream")
            # a row pointing outside the attachments dir is refused
            outside = Path(td) / "secret.txt"
            outside.write_text("nope")
            c.store.execute(
                "UPDATE attachments SET path=? WHERE id=?",
                (str(outside), a["id"]))
            with self.assertRaises(AssistantError):
                c.read_upload(a["id"])
            # a missing file is a clean 404, not a 500
            c.store.execute(
                "UPDATE attachments SET path=? WHERE id=?",
                (a["path"], a["id"]))
            Path(a["path"]).unlink()
            with self.assertRaises(AssistantError) as e:
                c.read_upload(a["id"])
            self.assertEqual(e.exception.status, 404)
            c.close()


class ModelHistoryTests(unittest.TestCase):
    def test_bounded_model_view_full_store_kept(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            cv = conv(c)
            for i in range(10):
                r = ask(c, cv["id"], "q" * 40 + str(i))
                finish(c, r["exchange"]["id"], "a" * 40 + str(i))
            h = c.model_history(cv["id"], max_chars=300)
            self.assertTrue(h["truncated"])
            self.assertLess(len(h["items"]), h["total_messages"])
            self.assertEqual(h["total_messages"], 20)
            # the bounded view ends at the newest exchange, chronological
            self.assertEqual(h["items"][-1]["role"], "assistant")
            self.assertEqual(h["items"][-1]["text"], "a" * 40 + "9")
            self.assertEqual(h["items"][-2]["text"], "q" * 40 + "9")
            # the store still has everything
            self.assertEqual(
                len(c.list_messages(cv["id"], limit=200)["items"]), 20)
            c.close()


class ProviderContractTests(unittest.TestCase):
    def test_usage_delta_subtracts_cumulative_counters(self):
        d = usage_delta({"total_prompt_tokens": 100,
                         "total_completion_tokens": 40,
                         "total_cached_tokens": 30, "total_steps": 2},
                        {"total_prompt_tokens": 250,
                         "total_completion_tokens": 90,
                         "total_cached_tokens": 80, "total_steps": 5})
        self.assertEqual((d.prompt_t, d.completion_t, d.cached_t,
                          d.llm_steps), (150, 50, 50, 3))
        fresh = usage_delta(None, {"total_prompt_tokens": 10})
        self.assertEqual(fresh.prompt_t, 10)
        neg = usage_delta({"total_prompt_tokens": 99}, {})  # reset/garbage
        self.assertEqual(neg.prompt_t, 0)


class HttpApiTests(unittest.TestCase):
    def post(self, core, path, body=None):
        return http_api.dispatch(
            core, "POST", path,
            body=json.dumps(body or {}).encode())

    def test_full_flow_over_http(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            code, cv = self.post(c, "/api/assistant/conversations",
                                 {"origin": {"view": "agent",
                                             "refs": {"run_key": "r"}},
                                  "title": "T"})
            self.assertEqual(code, 201)
            cid = cv["id"]
            code, res = self.post(
                c, f"/api/assistant/conversations/{cid}/messages",
                {"text": "hi", "client_msg_id": "cm-http-1",
                 "client_ctx": {"view": "agent"}})
            self.assertEqual(code, 202)
            xid = res["exchange"]["id"]
            # identical retry -> same exchange, no duplicate
            code, res2 = self.post(
                c, f"/api/assistant/conversations/{cid}/messages",
                {"text": "hi", "client_msg_id": "cm-http-1"})
            self.assertEqual(code, 200)
            self.assertEqual(res2["exchange"]["id"], xid)
            code, x = http_api.dispatch(
                c, "GET", f"/api/assistant/exchanges/{xid}")
            self.assertEqual(x["status"], "queued")
            finish(c, xid, "answer text")
            code, x = http_api.dispatch(
                c, "GET", f"/api/assistant/exchanges/{xid}")
            self.assertEqual(x["status"], "done")
            self.assertEqual(x["text"], "answer text")
            code, page = http_api.dispatch(
                c, "GET", f"/api/assistant/conversations/{cid}")
            self.assertEqual(len(page["items"]), 2)
            code, u = http_api.dispatch(
                c, "GET", f"/api/assistant/conversations/{cid}/usage")
            self.assertEqual(u["totals"]["prompt_t"], 100)
            code, last = http_api.dispatch(c, "GET", "/api/assistant/last")
            self.assertEqual(last["conversation"]["id"], cid)
            c.close()

    def test_dispatch_404s_and_errors(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td)
            code, body = http_api.dispatch(c, "GET", "/api/nope")
            self.assertEqual((code, body["error"]), (404, "not found"))
            code, body = http_api.dispatch(
                c, "GET", "/api/assistant/conversations/conv-bad")
            self.assertEqual(code, 404)
            code, body = self.post(c, "/api/assistant/conversations",
                                   {"origin": {"view": "BAD VIEW!!"}})
            self.assertEqual(code, 400)
            code, body = http_api.dispatch(
                c, "POST", "/api/assistant/conversations",
                body=b"{not json")
            self.assertEqual(code, 400)
            c.close()

    def test_settings_and_models_routes(self):
        with tempfile.TemporaryDirectory() as td:
            c = make_core(td, catalog=lambda: [
                {"uid": "gpt-6-luna-low-priority", "in": 0.2}])
            code, s = http_api.dispatch(c, "GET", "/api/assistant/settings")
            self.assertTrue(s["defaults"]["readonly"])
            code, s = self.post(c, "/api/assistant/settings",
                                {"model": "gpt-6-luna", "effort": "low"})
            self.assertEqual(s["defaults"]["model"], "gpt-6-luna")
            code, m = http_api.dispatch(c, "GET", "/api/assistant/models")
            self.assertEqual(m["models"][0]["uid"],
                             "gpt-6-luna-low-priority")
            c.close()

    def test_attachment_upload_route(self):
        """POST /api/assistant/attachments takes RAW image bytes (the
        JSON decoder is skipped for it), records the row against the
        ?conversation= id, dedupes on ?ref=, and validates content."""
        with tempfile.TemporaryDirectory() as td:
            import conductor
            adir = Path(td) / "attachments"
            c = make_core(
                td, attachment_saver=lambda d:
                    conductor.save_attachment(d, adir))
            code, cv = self.post(c, "/api/assistant/conversations",
                                 {"origin": {"view": "dashboard"}})
            cid = cv["id"]
            png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
            code, a = http_api.dispatch(
                c, "POST", "/api/assistant/attachments", body=png,
                query={"conversation": [cid], "ref": ["r1"]})
            self.assertEqual(code, 201)
            self.assertEqual(a["ctype"], "image/png")
            self.assertEqual(a["conversation_id"], cid)
            self.assertIsNone(a["message_id"])       # bound at send
            code, a2 = http_api.dispatch(
                c, "POST", "/api/assistant/attachments", body=png,
                query={"conversation": [cid], "ref": ["r1"]})
            self.assertTrue(a2["deduped"])
            code, b = http_api.dispatch(
                c, "POST", "/api/assistant/attachments", body=png)
            self.assertEqual(code, 400)            # no conversation
            code, b = http_api.dispatch(
                c, "POST", "/api/assistant/attachments", body=png,
                query={"conversation": ["conv-nope"]})
            self.assertEqual(code, 404)
            code, b = http_api.dispatch(
                c, "POST", "/api/assistant/attachments",
                body=b"MZ" + b"x" * 64,
                query={"conversation": [cid]})
            self.assertEqual(code, 415)
            code, b = http_api.dispatch(
                c, "POST", "/api/assistant/attachments", body=b"",
                query={"conversation": [cid]})
            self.assertEqual(code, 400)
            c.close()

    def test_cancel_route(self):
        class Disp:
            def __init__(self):
                self.launched, self.cancelled = [], []

            def launch(self, x):
                self.launched.append(x["id"])

            def cancel(self, x):
                self.cancelled.append(x["id"])
        with tempfile.TemporaryDirectory() as td:
            d = Disp()
            c = make_core(td, dispatcher=d)
            cv = conv(c)
            code, res = self.post(
                c, f"/api/assistant/conversations/{cv['id']}/messages",
                {"text": "hi", "client_msg_id": "cm-0000x"})
            self.assertEqual(d.launched, [res["exchange"]["id"]])
            c.begin_exchange(res["exchange"]["id"])
            code, x = self.post(
                c, f"/api/assistant/exchanges/{res['exchange']['id']}"
                   "/cancel")
            self.assertEqual(x["status"], "cancelled")
            self.assertEqual(d.cancelled, [res["exchange"]["id"]])
            c.close()


class HostAdapterTests(unittest.TestCase):
    def test_open_core_uses_data_dir_and_conductor_seams(self):
        import conductor  # noqa: F401 - imported for attr patching
        import assistant_host.conductor_adapters as host
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data" / "assistant"
            core = host.open_core(data_dir=data,
                                  runs_dir=Path(td) / "data" / "runs")
            self.assertTrue((data / "assistant.db").is_file())
            self.assertTrue((data / "workspace").is_dir())
            self.assertTrue((data / "spool").is_dir())
            self.assertIs(core._save_attach, conductor.save_attachment)
            conv_rec = core.create_conversation({"view": "dashboard"})
            self.assertTrue(conv_rec["id"].startswith("conv-"))
            core.close()


if __name__ == "__main__":
    unittest.main()
