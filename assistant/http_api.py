"""HTTP route table -> AssistantCore calls (DESIGN.md section 6.2).

`dispatch` is deliberately framework-free: the host handler (conductor's
make_dashboard_handler) calls it with the method, path, raw body bytes and
parsed query, and gets back (status, payload). All POSTs are idempotent
through client_msg_id / client_ref / provider request dedupe in core.

Route summary (all under /api/assistant/):
  GET  conversations?limit&before           - history list (newest first)
  POST conversations                        - create {origin,title,client_ref,settings}
  GET  conversations/<id>                   - record + newest message page
  GET  conversations/<id>/messages?limit&before - older pages
  POST conversations/<id>/messages          - queue exchange {text,
       client_msg_id, client_ctx, attachments, settings} -> 202
  GET  conversations/<id>/usage             - per-exchange rows + rollup
  GET  conversations/<id>/exchanges         - exchange rows incl. thawed
       citation sources (chips survive reload)
  GET  conversations/<id>/actions           - audit rows for the conv
  POST conversations/<id>/settings          - per-conv settings update
  POST conversations/<cid>/actions/<aid>/confirm {approve}
  GET  last                                 - last-active conversation
  GET  exchanges/<xid>                      - poll state/text/usage
  POST exchanges/<xid>/cancel
  POST attachments?ref=                     - raw image bytes -> row
  GET  settings | POST settings             - global defaults
  GET  models                               - catalog for settings modal
"""
import json
import re
import sqlite3

from .core import AssistantCore, AssistantError

_ID = r"([a-z]{2,5}-\d{8}-\d{6}-[0-9a-f]{4,16})"


def _int(q, key, default, lo=1, hi=500):
    try:
        return max(lo, min(int((q or {}).get(key) or default), hi))
    except (TypeError, ValueError):
        return default


def _before_cursor(q):
    raw = (q or {}).get("before")
    if not raw:
        return None
    if isinstance(raw, list):
        raw = raw[0]
    raw = str(raw)
    if "|" in raw:                          # 'last_active_at|id'
        t, i = raw.split("|", 1)
        return (t, i)
    try:                                    # messages: plain seq cursor
        return int(raw)
    except ValueError:
        return None


# Each handler: (core, match_groups, body_dict_or_None, raw_body, query)
def _h_list_convs(core, g, body, raw, q):
    return 200, core.list_conversations(
        limit=_int(q, "limit", 50, 1, 100), before=_before_cursor(q))


def _h_create_conv(core, g, body, raw, q):
    conv = core.create_conversation(
        (body or {}).get("origin") or (body or {}).get("origin_view"),
        title=(body or {}).get("title"),
        client_ref=(body or {}).get("client_ref"),
        settings=(body or {}).get("settings"))
    return (200 if conv.get("deduped") else 201), conv


def _h_get_conv(core, g, body, raw, q):
    out = core.open_conversation(
        g[0], limit=_int(q, "limit", 50, 1, 200),
        before=_before_cursor(q))
    return 200, out


def _h_list_msgs(core, g, body, raw, q):
    return 200, core.list_messages(
        g[0], limit=_int(q, "limit", 50, 1, 200),
        before=_before_cursor(q))


def _h_post_msg(core, g, body, raw, q):
    body = body or {}
    res = core.post_message(
        g[0], body.get("text"), client_msg_id=body.get("client_msg_id"),
        client_ctx=body.get("client_ctx"),
        attachments=body.get("attachments"), settings=body.get("settings"))
    code = 200 if res.get("deduped") else 202
    return code, res


def _h_usage(core, g, body, raw, q):
    return 200, core.usage(g[0])


def _h_exchanges(core, g, body, raw, q):
    return 200, {"items": core.list_exchanges(g[0])}


def _h_actions(core, g, body, raw, q):
    return 200, {"items": core.list_actions(g[0])}


def _h_conv_settings(core, g, body, raw, q):
    return 200, core.update_settings(g[0], body or {})


def _h_confirm(core, g, body, raw, q):
    a = core.decide_action(g[1], bool((body or {}).get("approve")))
    if (body or {}).get("approve") and (body or {}).get("execute"):
        a = core.execute_action(a["id"])
    return 200, a


def _h_last(core, g, body, raw, q):
    conv = core.last_active_conversation()
    return 200, {"conversation": conv}


def _h_exchange(core, g, body, raw, q):
    return 200, core.exchange_view(g[0])


def _h_cancel(core, g, body, raw, q):
    return 200, core.cancel_exchange(g[0])


def _h_upload(core, g, body, raw, q):
    q = q or {}
    conv = q.get("conversation") or q.get("conv")
    if not conv:
        raise AssistantError("conversation id required")
    att = core.save_upload(str(conv), raw or b"",
                           client_ref=q.get("ref"))
    return (200 if att.get("deduped") else 201), att


def _h_get_settings(core, g, body, raw, q):
    return 200, {"defaults": core.defaults()}


def _h_put_settings(core, g, body, raw, q):
    return 200, {"defaults": core.set_defaults(body or {})}


def _h_models(core, g, body, raw, q):
    return 200, core.catalog()


ROUTES = [
    ("GET",    re.compile(r"/api/assistant/conversations\Z"),
     _h_list_convs),
    ("POST",   re.compile(r"/api/assistant/conversations\Z"),
     _h_create_conv),
    ("GET",    re.compile(rf"/api/assistant/conversations/{_ID}\Z"),
     _h_get_conv),
    ("GET",    re.compile(rf"/api/assistant/conversations/{_ID}/messages\Z"),
     _h_list_msgs),
    ("POST",   re.compile(rf"/api/assistant/conversations/{_ID}/messages\Z"),
     _h_post_msg),
    ("GET",    re.compile(rf"/api/assistant/conversations/{_ID}/usage\Z"),
     _h_usage),
    ("GET",    re.compile(rf"/api/assistant/conversations/{_ID}/exchanges\Z"),
     _h_exchanges),
    ("GET",    re.compile(rf"/api/assistant/conversations/{_ID}/actions\Z"),
     _h_actions),
    ("POST",   re.compile(rf"/api/assistant/conversations/{_ID}/settings\Z"),
     _h_conv_settings),
    ("PUT",    re.compile(rf"/api/assistant/conversations/{_ID}/settings\Z"),
     _h_conv_settings),
    ("POST",   re.compile(
        rf"/api/assistant/conversations/{_ID}/actions/{_ID}/confirm"),
     _h_confirm),
    ("GET",    re.compile(r"/api/assistant/last\Z"), _h_last),
    ("GET",    re.compile(rf"/api/assistant/exchanges/{_ID}\Z"), _h_exchange),
    ("POST",   re.compile(rf"/api/assistant/exchanges/{_ID}/cancel\Z"),
     _h_cancel),
    ("POST",   re.compile(r"/api/assistant/attachments\Z"), _h_upload),
    ("GET",    re.compile(r"/api/assistant/settings\Z"), _h_get_settings),
    ("POST",   re.compile(r"/api/assistant/settings\Z"), _h_put_settings),
    ("PUT",    re.compile(r"/api/assistant/settings\Z"), _h_put_settings),
    ("GET",    re.compile(r"/api/assistant/models\Z"), _h_models),
]


def dispatch(core: AssistantCore, method: str, path: str, *,
             body: bytes | None = None, query: dict | None = None
             ) -> tuple[int, dict]:
    """(status, payload) for one /api/assistant/* call. Raises nothing for
    expected client errors - AssistantError maps to its status."""
    method = method.upper()
    q = {}
    for k, v in (query or {}).items():      # accept parse_qs lists or scalars
        q[k] = v[0] if isinstance(v, (list, tuple)) and v else v
    try:
        for m, rx, fn in ROUTES:
            if m != method:
                continue
            match = rx.fullmatch(path)
            if not match:
                continue
            if method in ("POST", "PUT") and not rx.pattern.endswith(
                    "attachments\\Z"):
                parsed = json.loads(body.decode("utf-8")
                                    if body else "{}")
                if not isinstance(parsed, dict):
                    raise AssistantError("body must be a JSON object")
            else:
                parsed = None
            return fn(core, match.groups(), parsed, body, q)
        return 404, {"error": "not found"}
    except AssistantError as exc:
        return exc.status, {"error": str(exc)}
    except json.JSONDecodeError:
        return 400, {"error": "invalid JSON body"}
    except sqlite3.IntegrityError:
        return 409, {"error": "duplicate write refused by constraint"}
