"""The conductor-side ActionAdapter - the assistant's guarded write path
(DESIGN.md 6.8, manifest 'Guarded dashboard actions').

Every whitelisted kind delegates to the SAME Python function the
dashboard's own POST endpoint calls, on the SAME injected dirs. All the
hard guarantees - run/step name regexes, request stage gates, watched-
workspace refusals, liveness checks, merge claims, proposal validation,
retry-safety - are inherited from those functions, never re-implemented
here. What this file adds on top:

- a FINITE whitelist (spec_for): unknown kinds can never even persist
  as proposed rows;
- param validation at PROPOSE time (target records must exist and be
  well-formed) so junk dies in the card instead of at execution;
- the launch-tier `expected_ack` string the client must echo - it names
  the exact target the second confirmation step displays;
- `describe()` - the prompt-facing catalog so the model only ever
  proposes real kinds.

Deliberately NOT in the whitelist (the manifest's exclusions): arbitrary
shell commands, unrestricted code evaluation, direct repository edits,
file writes outside the managed dirs, raw HTTP to other endpoints.
"""
import json
import re
from pathlib import Path

import conductor
from assistant.actions import (ActionAdapter, ActionSpec,
                               LAUNCH_TIER_KINDS)
from conductor import RequestError

_STEP_RE = conductor.RUN_DIR_RE            # step names share the run regex
_REQ_ID = conductor.REQUEST_ID_RE
_TRASH_ID = re.compile(
    r"\d{8}-\d{6}-[0-9a-f]{6}\Z|legacy-\d{8}-\d{6}-[0-9a-f]{8}\Z")

# The request-form field whitelist - mirrors _request_fields' own set so a
# proposal can never smuggle an unknown key into create/save/apply.
_REQUEST_FIELDS = frozenset({
    "raw", "label", "project", "model", "effort", "manifest",
    "followup", "on_failure", "max_attempts", "plan_model",
    "plan_effort", "merge_target", "worktrees", "merge_resolver",
    "merge", "summarize", "ask", "attachments"})


def _extra(p: dict, *keys: str) -> None:
    extra = set(p) - set(keys)
    if extra:
        raise RequestError(f"Unknown params: {sorted(extra)}")


def _s(p: dict, key: str, *, required: bool = True,
       mx: int = 2000) -> str:
    v = p.get(key)
    if v is None:
        if required:
            raise RequestError(f"{key} is required")
        return ""
    if not isinstance(v, str):
        raise RequestError(f"{key} must be text")
    v = v.strip()
    if required and not v:
        raise RequestError(f"{key} must be non-empty")
    return v[:mx]


def _b(p: dict, key: str, default: bool = True) -> bool:
    v = p.get(key)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise RequestError(f"{key} must be true or false")
    return v


def _i(p: dict, key: str) -> int | None:
    v = p.get(key)
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool):
        raise RequestError(f"{key} must be an integer")
    return v


def _str_list(p: dict, key: str, *, required: bool = True,
              mx: int = 60) -> list[str]:
    v = p.get(key)
    if v is None:
        if required:
            raise RequestError(f"{key} is required")
        return []
    if not isinstance(v, list) or not v or len(v) > mx or any(
            not isinstance(x, str) or not x.strip() for x in v):
        raise RequestError(f"{key} must be a list of 1-{mx} names")
    return [x.strip() for x in v]


class ConductorActions(ActionAdapter):
    """Whitelist -> the dashboard's own Python functions.

    `runs_dir` anchors the served data tree; requests/manifests/trash
    default to their serve-time siblings (tests inject fixtures).
    `devin_argv` is the worker binary override the serve path carries.
    `power` is the optional serve_power seam - None keeps the kind
    completely invisible to the model (spec_for returns None).
    """

    def __init__(self, *, runs_dir, requests_dir=None, manifests_dir=None,
                 trash_dir=None, devin_argv=None, power=None):
        self.runs_dir = Path(runs_dir)
        self.requests_dir = Path(requests_dir) if requests_dir \
            else self.runs_dir.parent / "requests"
        self.manifests_dir = Path(manifests_dir) if manifests_dir \
            else Path(conductor.MANIFESTS_DIR)
        self.trash_dir = Path(trash_dir) if trash_dir \
            else conductor._trash_root(self.runs_dir)
        self.devin_argv = devin_argv
        self._power = power

    # ------------------------------------------------- adapter surface
    def spec_for(self, kind: str) -> ActionSpec | None:
        ent = _KINDS.get(kind)
        if ent is None:
            return None
        if kind == "serve_power" and self._power is None:
            return None
        return ActionSpec(kind, requires_confirm=True,
                          confirm_text=ent["text"])

    def validate(self, kind: str, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise RequestError("params must be a JSON object")
        ent = _KINDS.get(kind)
        if ent is None:
            raise RequestError(f"Unknown action kind {kind!r}", 403)
        return ent["validate"](self, payload)

    def execute(self, action: dict) -> dict:
        kind = action.get("kind")
        ent = _KINDS.get(kind)
        if ent is None:
            raise RequestError(f"Unknown action kind {kind!r}", 403)
        try:
            p = json.loads(action.get("payload") or "{}")
        except ValueError:
            raise RequestError("Stored action payload is unreadable", 500)
        # The stored payload was validated at propose time; targets are
        # re-checked here anyway by the underlying function - the record
        # can have moved/been deleted between confirm and click.
        out = ent["call"](self, p)
        return out if isinstance(out, dict) else {"result": out}

    def expected_ack(self, action: dict) -> str | None:
        kind = action.get("kind")
        ent = _KINDS.get(kind)
        if ent is None or kind not in LAUNCH_TIER_KINDS:
            return None
        try:
            p = json.loads(action.get("payload") or "{}")
        except ValueError:
            p = {}
        tgt = ent["target"](self, p) if ent.get("target") else ""
        return f"{kind}:{tgt}"

    def describe(self) -> str:
        lines = []
        for kind, ent in _KINDS.items():
            if kind == "serve_power" and self._power is None:
                continue
            tier = (" [two-step confirm: exact target shown]"
                    if kind in LAUNCH_TIER_KINDS else "")
            lines.append(f"- {kind} - {ent['help']}{tier}")
        return "\n".join(lines)

    # ------------------------------------------------- shared validators
    def _run(self, p: dict) -> tuple[str, dict]:
        """run dir name -> (name, state); 404 on malformed/missing."""
        run = _s(p, "run", mx=200)
        return run, conductor._run_dir_checked(run, self.runs_dir)[1]

    def _step(self, st: dict, p: dict, *, manifest_ok: bool) -> str:
        """step name -> exists in state.json, optionally in the manifest
        (for controls a never-dispatched step legitimately takes)."""
        step = _s(p, "step", mx=200)
        if not _STEP_RE.fullmatch(step):
            raise RequestError("Invalid step name", 404)
        if step not in (st.get("steps") or {}) and not (
                manifest_ok
                and step in conductor._manifest_step_names(st)):
            raise RequestError("Step not in this run", 404)
        return step

    def _request(self, p: dict) -> str:
        rid = _s(p, "request_id", mx=64)
        if not _REQ_ID.fullmatch(rid):
            raise RequestError("Invalid request id", 404)
        conductor._read_request(rid, self.requests_dir)      # 404s if gone
        return rid

    def _request_fields(self, p: dict, rec: dict | None,
                        allowed: frozenset, ignore=()) -> dict:
        patch = {k: v for k, v in p.items() if k in allowed}
        if not patch:
            raise RequestError("Nothing to change")
        extra = set(p) - set(allowed) - {"request_id"} - set(ignore)
        if extra:
            raise RequestError(f"Unknown params: {sorted(extra)}")
        # The full shape gate runs NOW - a bad value fails at propose
        # time, not after the human confirms.
        conductor._request_fields(patch, rec)
        return patch

    def _review_body(self, p: dict) -> dict:
        body = {"reviewed": _b(p, "reviewed")}
        seq = _i(p, "seq")
        if seq is not None:
            body["seq"] = seq
        return body

    # ------------------------------------------------------ validate impls
    def _v_create_request(self, p: dict) -> dict:
        _extra(p, *_REQUEST_FIELDS)
        conductor._request_fields(dict(p))         # full create-time gate
        return dict(p)

    def _v_edit_request(self, p: dict) -> dict:
        rid = self._request(p)
        rec = conductor._read_request(rid, self.requests_dir)
        patch = self._request_fields(p, rec, _REQUEST_FIELDS - {"manifest"},
                                     ignore={"manifest"})
        out = {"request_id": rid, **patch}
        if "manifest" in p:
            text = _s(p, "manifest", mx=120_000)
            # publish-path validation at propose time - same gate the
            # endpoint's Save runs before the TOML touches disk.
            conductor.validate_proposal(text, self.manifests_dir)
            out["manifest"] = text
        return out

    def _v_apply_request(self, p: dict) -> dict:
        rid = self._request(p)
        rec = conductor._read_request(rid, self.requests_dir)
        patch = self._request_fields(p, rec, _REQUEST_FIELDS - {"manifest"})
        return {"request_id": rid, **patch}

    def _v_request_id(self, p: dict) -> dict:
        _extra(p, "request_id")
        return {"request_id": self._request(p)}

    def _v_run(self, p: dict) -> dict:
        _extra(p, "run")
        run, _st = self._run(p)
        return {"run": run}

    def _v_answer_request(self, p: dict) -> dict:
        _extra(p, "request_id", "qid", "selected", "text", "skipped",
               "skip_all")
        out = {"request_id": self._request(p)}
        body = {}
        if p.get("skip_all") is True:
            body["skip_all"] = True
        else:
            body["qid"] = _s(p, "qid", mx=64)
            if "selected" in p:
                sel = _str_list(p, "selected", required=False, mx=60)
                if sel:
                    body["selected"] = sel
            text = _s(p, "text", required=False, mx=2000)
            if text:
                body["text"] = text
            if "skipped" in p:
                body["skipped"] = _b(p, "skipped", default=False)
        out.update(body)
        return out

    def _v_import_followup(self, p: dict) -> dict:
        _extra(p, "path")
        return {"path": _s(p, "path", mx=600)}

    def _v_review_run(self, p: dict) -> dict:
        _extra(p, "run", "reviewed", "seq")
        run, _st = self._run(p)
        return {"run": run, **self._review_body(p)}

    def _v_review_step(self, p: dict) -> dict:
        _extra(p, "run", "step", "reviewed", "seq")
        run, st = self._run(p)
        step = self._step(st, p, manifest_ok=True)
        return {"run": run, "step": step, **self._review_body(p)}

    def _v_batch_run_review(self, p: dict) -> dict:
        _extra(p, "runs", "reviewed", "seq")
        runs = _str_list(p, "runs")
        for r in runs:
            if not _STEP_RE.fullmatch(r):
                raise RequestError(f"Invalid run dir {r!r}", 404)
            conductor._run_dir_checked(r, self.runs_dir)
        return {"runs": sorted(set(runs)), **self._review_body(p)}

    def _v_batch_step_review(self, p: dict) -> dict:
        _extra(p, "run", "steps", "reviewed")
        run, st = self._run(p)
        steps = _str_list(p, "steps")
        names = set(st.get("steps") or {}) | conductor._manifest_step_names(st)
        for s in steps:
            if s not in names:
                raise RequestError(f"Step {s!r} is not in this run", 404)
        return {"run": run, "steps": sorted(set(steps)),
                "reviewed": _b(p, "reviewed")}

    def _v_archive_run(self, p: dict) -> dict:
        _extra(p, "run", "archived")
        run, _st = self._run(p)
        return {"run": run, "archived": _b(p, "archived")}

    def _v_batch_archive(self, p: dict) -> dict:
        _extra(p, "runs", "archived")
        runs = _str_list(p, "runs")
        for r in runs:
            if not _STEP_RE.fullmatch(r):
                raise RequestError(f"Invalid run dir {r!r}", 404)
            conductor._run_dir_checked(r, self.runs_dir)
        return {"runs": sorted(set(runs)),
                "archived": _b(p, "archived")}

    def _v_skip_step(self, p: dict) -> dict:
        _extra(p, "run", "step", "skip")
        run, st = self._run(p)
        step = self._step(st, p, manifest_ok=True)
        return {"run": run, "step": step, "skip": _b(p, "skip")}

    def _v_batch_skip(self, p: dict) -> dict:
        _extra(p, "run", "steps", "skip")
        run, st = self._run(p)
        steps = _str_list(p, "steps")
        names = set(st.get("steps") or {}) | conductor._manifest_step_names(st)
        for s in steps:
            if s not in names:
                raise RequestError(f"Step {s!r} is not in this run", 404)
        return {"run": run, "steps": sorted(set(steps)),
                "skip": _b(p, "skip")}

    def _v_step_notes(self, p: dict) -> dict:
        _extra(p, "run", "step", "notes")
        run, st = self._run(p)
        step = self._step(st, p, manifest_ok=True)
        notes = p.get("notes")
        if notes is None:
            notes = ""
        if not isinstance(notes, str) or len(notes) > 20_000:
            raise RequestError("notes must be text (<= 20,000 chars)")
        return {"run": run, "step": step, "notes": notes}

    def _v_step_prompt(self, p: dict) -> dict:
        _extra(p, "run", "step", "prompt")
        run, st = self._run(p)
        step = self._step(st, p, manifest_ok=True)
        prompt = p.get("prompt")
        if prompt is not None and (
                not isinstance(prompt, str) or not 1 <= len(prompt)
                <= 100_000):
            raise RequestError("prompt must be 1-100,000 chars or null")
        return {"run": run, "step": step, "prompt": prompt}

    def _v_step_timeout(self, p: dict) -> dict:
        _extra(p, "run", "step", "minutes")
        run, st = self._run(p)
        step = self._step(st, p, manifest_ok=False)
        try:
            minutes = float(p.get("minutes"))
        except (TypeError, ValueError):
            raise RequestError("minutes must be a number") from None
        if not 1 <= minutes <= 1440:
            raise RequestError("minutes must be 1-1440")
        return {"run": run, "step": step, "minutes": minutes}

    def _v_restore_trash(self, p: dict) -> dict:
        _extra(p, "entry_id")
        eid = _s(p, "entry_id", mx=64)
        conductor._trash_entry(self.trash_dir, eid)      # shape + exists
        return {"entry_id": eid}

    def _v_merge_run(self, p: dict) -> dict:
        _extra(p, "run", "steps")
        run, st = self._run(p)
        out = {"run": run}
        if "steps" in p:
            steps = _str_list(p, "steps", required=False)
            known = set(st.get("steps") or {})
            for s in steps:
                if s not in known:
                    raise RequestError(
                        f"Step {s!r} is not in this run", 404)
            if steps:
                out["steps"] = sorted(set(steps))
        return out

    def _v_summarize_run(self, p: dict) -> dict:
        _extra(p, "run", "model")
        run, _st = self._run(p)
        out = {"run": run}
        model = _s(p, "model", required=False, mx=64)
        if model:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", model):
                raise RequestError("Invalid model")
            out["model"] = model
        return out

    def _v_stop_run(self, p: dict) -> dict:
        _extra(p, "run", "mode")
        run, _st = self._run(p)
        mode = _s(p, "mode", required=False) or "stop"
        if mode not in ("stop", "drain", "pause"):
            raise RequestError("mode must be stop, drain or pause")
        return {"run": run, "mode": mode}

    def _v_stop_step(self, p: dict) -> dict:
        _extra(p, "run", "step")
        run, st = self._run(p)
        step = self._step(st, p, manifest_ok=False)
        return {"run": run, "step": step}

    def _v_batch_step_stop(self, p: dict) -> dict:
        _extra(p, "run", "steps")
        run, st = self._run(p)
        steps = _str_list(p, "steps")
        known = set(st.get("steps") or {})
        for s in steps:
            if s not in known:
                raise RequestError(f"Step {s!r} is not in this run", 404)
        return {"run": run, "steps": sorted(set(steps))}

    def _v_interrupt_step(self, p: dict) -> dict:
        _extra(p, "run", "step", "instructions", "attachments")
        run, st = self._run(p)
        step = self._step(st, p, manifest_ok=False)
        text = _s(p, "instructions", mx=20_000)
        out = {"run": run, "step": step, "instructions": text}
        if "attachments" in p:
            out["attachments"] = conductor._attachment_fields(
                p.get("attachments"))
        return out

    def _v_serve_power(self, p: dict) -> dict:
        _extra(p, "action")
        act = _s(p, "action", mx=16)
        if act not in ("stop", "restart"):
            raise RequestError("action must be 'stop' or 'restart'")
        return {"action": act}

    # ------------------------------------------------------- execute impls
    def _batch(self, items, fn):
        """Run one host call per item; per-item errors land in the result
        instead of aborting the batch (a batch is only 'failed' when
        NOTHING succeeded)."""
        done, errors = {}, {}
        for it in items:
            try:
                done[it] = fn(it)
            except RequestError as exc:
                errors[it] = str(exc)
        if not done and errors:
            raise RequestError(next(iter(errors.values())))
        return {"done": done, "errors": errors}

    def _x_create_request(self, p: dict) -> dict:
        return conductor.create_request(
            dict(p), self.requests_dir, self.runs_dir)

    def _x_edit_request(self, p: dict) -> dict:
        rid = p["request_id"]
        body = {k: v for k, v in p.items() if k != "request_id"}
        return conductor.save_request(rid, body, self.requests_dir,
                                      self.manifests_dir, self.runs_dir)

    def _x_apply_request(self, p: dict) -> dict:
        rid = p["request_id"]
        body = {k: v for k, v in p.items() if k != "request_id"}
        return conductor.apply_request(rid, body, self.requests_dir,
                                       self.manifests_dir, self.runs_dir)

    def _x_launch_request(self, p: dict) -> dict:
        return conductor.launch_request(
            p["request_id"], self.requests_dir, self.manifests_dir,
            self.runs_dir, self.devin_argv)

    def _x_generate_request(self, p: dict) -> dict:
        return conductor.start_generation(
            p["request_id"], self.requests_dir, self.manifests_dir,
            self.runs_dir, self.devin_argv)

    def _x_stop_generation(self, p: dict) -> dict:
        return conductor.stop_generation(
            p["request_id"], self.requests_dir, self.runs_dir)

    def _x_answer_request(self, p: dict) -> dict:
        rid = p["request_id"]
        body = {k: v for k, v in p.items() if k != "request_id"}
        return conductor.answer_request(
            rid, body, self.requests_dir, self.manifests_dir,
            self.runs_dir, self.devin_argv)

    def _x_discard_request(self, p: dict) -> dict:
        return conductor.discard_request(
            p["request_id"], self.requests_dir, self.manifests_dir,
            self.runs_dir, self.trash_dir)

    def _x_import_followup(self, p: dict) -> dict:
        return conductor.import_followup_file(
            p["path"], self.requests_dir, self.manifests_dir,
            self.runs_dir)

    def _x_review_run(self, p: dict) -> dict:
        return conductor.set_run_review(
            p["run"], {k: v for k, v in p.items() if k != "run"},
            self.runs_dir)

    def _x_review_step(self, p: dict) -> dict:
        return conductor.set_step_review(
            p["run"], p["step"],
            {k: v for k, v in p.items()
             if k not in ("run", "step")}, self.runs_dir)

    def _x_batch_run_review(self, p: dict) -> dict:
        body = {k: v for k, v in p.items() if k != "runs"}
        return self._batch(
            p["runs"], lambda r: conductor.set_run_review(
                r, dict(body), self.runs_dir))

    def _x_batch_step_review(self, p: dict) -> dict:
        return self._batch(
            p["steps"], lambda s: conductor.set_step_review(
                p["run"], s, {"reviewed": p["reviewed"]}, self.runs_dir))

    def _x_archive_run(self, p: dict) -> dict:
        return conductor.set_run_archived(
            p["run"], {"archived": p["archived"]}, self.runs_dir)

    def _x_batch_archive(self, p: dict) -> dict:
        return self._batch(
            p["runs"], lambda r: conductor.set_run_archived(
                r, {"archived": p["archived"]}, self.runs_dir))

    def _x_skip_step(self, p: dict) -> dict:
        return conductor.skip_step(
            p["run"], p["step"], {"skip": p["skip"]}, self.runs_dir)

    def _x_batch_skip(self, p: dict) -> dict:
        return self._batch(
            p["steps"], lambda s: conductor.skip_step(
                p["run"], s, {"skip": p["skip"]}, self.runs_dir))

    def _x_step_notes(self, p: dict) -> dict:
        return conductor.set_step_notes(
            p["run"], p["step"], {"notes": p["notes"]}, self.runs_dir)

    def _x_step_prompt(self, p: dict) -> dict:
        return conductor.set_step_prompt(
            p["run"], p["step"], {"prompt": p["prompt"]}, self.runs_dir)

    def _x_step_timeout(self, p: dict) -> dict:
        return conductor.set_step_timeout(
            p["run"], p["step"], {"minutes": p["minutes"]}, self.runs_dir)

    def _x_restore_trash(self, p: dict) -> dict:
        return conductor.restore_trash(
            p["entry_id"], self.trash_dir, self.runs_dir,
            self.requests_dir, self.manifests_dir)

    def _x_resume_run(self, p: dict) -> dict:
        return conductor.resume_run(
            p["run"], self.runs_dir, self.devin_argv)

    def _x_merge_run(self, p: dict) -> dict:
        return conductor.merge_run_start(
            p["run"], self.runs_dir, self.devin_argv,
            only=p.get("steps") or None)

    def _x_summarize_run(self, p: dict) -> dict:
        return conductor.summarize_run_start(
            p["run"], self.runs_dir, self.devin_argv,
            model=p.get("model") or None)

    def _x_stop_run(self, p: dict) -> dict:
        mode = p.get("mode") or "stop"
        return conductor.stop_run(
            p["run"], {"pause": mode == "pause",
                       "drain": mode == "drain"}, self.runs_dir)

    def _x_stop_step(self, p: dict) -> dict:
        return conductor.stop_step(p["run"], p["step"], self.runs_dir)

    def _x_batch_step_stop(self, p: dict) -> dict:
        return self._batch(
            p["steps"], lambda s: conductor.stop_step(
                p["run"], s, self.runs_dir))

    def _x_interrupt_step(self, p: dict) -> dict:
        body = {"instructions": p["instructions"]}
        if "attachments" in p:
            body["attachments"] = p["attachments"]
        return conductor.interrupt_step(
            p["run"], p["step"], body, self.runs_dir)

    def _x_delete_run(self, p: dict) -> dict:
        return conductor.delete_run(
            p["run"], {"confirm": True}, self.runs_dir, self.trash_dir)

    def _x_empty_trash(self, p: dict) -> dict:
        return conductor.empty_trash(
            {"confirm": True}, self.trash_dir, self.requests_dir,
            self.runs_dir)

    def _x_serve_power(self, p: dict) -> dict:
        if self._power is None:
            raise RequestError("serve_power is not wired on this build",
                               503)
        return self._power(p["action"])


# ---------------------------------------------------------------- kinds
# kind -> {text (card's effect line), help (prompt catalog line),
#          validate (propose-time gate), call (execute), target (ack)}.
_KINDS = {
    "create_request": {
        "text": "Create a proposal request draft from this text",
        "help": "create_request {raw, label?, project?, model?, effort?, "
                "plan_model?, plan_effort?, merge?, merge_target?, "
                "worktrees?, merge_resolver?, summarize?, followup?, ask?, "
                "on_failure?, max_attempts?} - draft a bug-fix/proposal "
                "request; launching it is a separate action",
        "validate": ConductorActions._v_create_request,
        "call": ConductorActions._x_create_request},
    "edit_request": {
        "text": "Edit this proposal request (fields, or republish the "
                "proposal TOML when 'manifest' is given)",
        "help": "edit_request {request_id, <request fields>, manifest?} - "
                "edit an existing proposal's fields or republish its TOML",
        "validate": ConductorActions._v_edit_request,
        "call": ConductorActions._x_edit_request,
        "target": lambda self, p: p.get("request_id", "")},
    "apply_request": {
        "text": "Apply these option fields to the proposal TOML "
                "(deterministic patch - no regeneration)",
        "help": "apply_request {request_id, <option fields>} - apply "
                "model/effort/merge/worktree options onto the proposal "
                "TOML without regenerating",
        "validate": ConductorActions._v_apply_request,
        "call": ConductorActions._x_apply_request,
        "target": lambda self, p: p.get("request_id", "")},
    "launch_request": {
        "text": "Launch this proposal as a conductor run - real agents "
                "start working",
        "help": "launch_request {request_id} - start the ready proposal "
                "as a run (spawns real worker sessions)",
        "validate": ConductorActions._v_request_id,
        "call": ConductorActions._x_launch_request,
        "target": lambda self, p: p.get("request_id", "")},
    "generate_request": {
        "text": "Generate (or regenerate) this request's proposal - "
                "spawns a planner session",
        "help": "generate_request {request_id} - run the proposal "
                "planner for this request",
        "validate": ConductorActions._v_request_id,
        "call": ConductorActions._x_generate_request,
        "target": lambda self, p: p.get("request_id", "")},
    "stop_generation": {
        "text": "Stop this request's in-flight proposal generation",
        "help": "stop_generation {request_id} - cancel the planner pass "
                "and hand the request back to editing",
        "validate": ConductorActions._v_request_id,
        "call": ConductorActions._x_stop_generation,
        "target": lambda self, p: p.get("request_id", "")},
    "answer_request": {
        "text": "Answer (or skip) a parked planner question for this "
                "request - the final answer restarts generation",
        "help": "answer_request {request_id, qid?, selected?, text?, "
                "skipped?, skip_all?} - answer the planner questionnaire",
        "validate": ConductorActions._v_answer_request,
        "call": ConductorActions._x_answer_request,
        "target": lambda self, p: p.get("request_id", "")},
    "discard_request": {
        "text": "Discard this proposal request - its record and proposal "
                "file move to recoverable trash",
        "help": "discard_request {request_id} - move the proposal bundle "
                "to trash (recoverable for the retention window)",
        "validate": ConductorActions._v_request_id,
        "call": ConductorActions._x_discard_request,
        "target": lambda self, p: p.get("request_id", "")},
    "import_followup": {
        "text": "Import a proposal TOML file as a request record",
        "help": "import_followup {path} - register an existing proposal "
                "file as a request (validates like a generated one)",
        "validate": ConductorActions._v_import_followup,
        "call": ConductorActions._x_import_followup},
    "set_run_review": {
        "text": "Mark this run reviewed / needing review",
        "help": "set_run_review {run, reviewed, seq?} - toggle the run's "
                "review mark",
        "validate": ConductorActions._v_review_run,
        "call": ConductorActions._x_review_run},
    "set_step_review": {
        "text": "Mark this task reviewed / needing review",
        "help": "set_step_review {run, step, reviewed, seq?} - toggle one "
                "task's review mark",
        "validate": ConductorActions._v_review_step,
        "call": ConductorActions._x_review_step},
    "batch_run_review": {
        "text": "Mark these runs reviewed / needing review",
        "help": "batch_run_review {runs[], reviewed, seq?} - same review "
                "mark on several runs",
        "validate": ConductorActions._v_batch_run_review,
        "call": ConductorActions._x_batch_run_review},
    "batch_step_review": {
        "text": "Mark these tasks reviewed / needing review",
        "help": "batch_step_review {run, steps[], reviewed} - same review "
                "mark on several tasks",
        "validate": ConductorActions._v_batch_step_review,
        "call": ConductorActions._x_batch_step_review},
    "set_run_archived": {
        "text": "Archive / unarchive this run (leaves the default list "
                "but stays on disk)",
        "help": "set_run_archived {run, archived} - archive or restore "
                "the run row",
        "validate": ConductorActions._v_archive_run,
        "call": ConductorActions._x_archive_run},
    "batch_run_archived": {
        "text": "Archive / unarchive these runs",
        "help": "batch_run_archived {runs[], archived} - archive or "
                "restore several run rows",
        "validate": ConductorActions._v_batch_archive,
        "call": ConductorActions._x_batch_archive},
    "skip_step": {
        "text": "Skip / unskip this task at its next dispatch",
        "help": "skip_step {run, step, skip} - skip the task (its turn "
                "records 'skipped') or undo the mark",
        "validate": ConductorActions._v_skip_step,
        "call": ConductorActions._x_skip_step},
    "batch_step_skip": {
        "text": "Skip / unskip these tasks at their next dispatch",
        "help": "batch_step_skip {run, steps[], skip} - same skip mark "
                "on several tasks",
        "validate": ConductorActions._v_batch_skip,
        "call": ConductorActions._x_batch_skip},
    "set_step_notes": {
        "text": "Set resume notes / additional instructions for this "
                "task's next dispatch (empty clears)",
        "help": "set_step_notes {run, step, notes} - operator text "
                "appended to the task's next prompt",
        "validate": ConductorActions._v_step_notes,
        "call": ConductorActions._x_step_notes},
    "set_step_prompt": {
        "text": "Override this task's not-yet-dispatched prompt (null "
                "reverts to the manifest text)",
        "help": "set_step_prompt {run, step, prompt} - replace the task "
                "body a pending step will dispatch with",
        "validate": ConductorActions._v_step_prompt,
        "call": ConductorActions._x_step_prompt},
    "set_step_timeout": {
        "text": "Move this live task's timeout (1-1440 minutes)",
        "help": "set_step_timeout {run, step, minutes} - change the "
                "running/queued task's timeout floor",
        "validate": ConductorActions._v_step_timeout,
        "call": ConductorActions._x_step_timeout},
    "restore_trash": {
        "text": "Restore this trash entry back to its recorded "
                "destination",
        "help": "restore_trash {entry_id} - undo a delete/discard while "
                "it is still recoverable",
        "validate": ConductorActions._v_restore_trash,
        "call": ConductorActions._x_restore_trash},
    "resume_run": {
        "text": "Resume this ended run - re-dispatches unfinished tasks "
                "in fresh sessions",
        "help": "resume_run {run} - re-enter the run, skipping done "
                "tasks (spawns real worker sessions)",
        "validate": ConductorActions._v_run,
        "call": ConductorActions._x_resume_run,
        "target": lambda self, p: p.get("run", "")},
    "merge_run": {
        "text": "Merge this run's pending/deferred branches now - "
                "spawns the merge (and resolver sessions if needed)",
        "help": "merge_run {run, steps?} - land pending worktree "
                "branches (optionally a subset)",
        "validate": ConductorActions._v_merge_run,
        "call": ConductorActions._x_merge_run,
        "target": lambda self, p: p.get("run", "")},
    "summarize_run": {
        "text": "Generate this run's LLM executive summary - spawns a "
                "summary session",
        "help": "summarize_run {run, model?} - write run.summary.md "
                "with an optional model pin",
        "validate": ConductorActions._v_summarize_run,
        "call": ConductorActions._x_summarize_run,
        "target": lambda self, p: p.get("run", "")},
    "stop_run": {
        "text": "Stop / drain / pause this run - live tasks' process "
                "trees die (drain/pause finish in-flight first)",
        "help": "stop_run {run, mode?} - mode stop (kill now), drain "
                "(graceful halt) or pause (resumable hold)",
        "validate": ConductorActions._v_stop_run,
        "call": ConductorActions._x_stop_run,
        "target": lambda self, p: p.get("run", "")},
    "stop_step": {
        "text": "Stop this task now - its process tree dies; the run "
                "continues to the next queued task",
        "help": "stop_step {run, step} - kill one running/queued task",
        "validate": ConductorActions._v_stop_step,
        "call": ConductorActions._x_stop_step,
        "target": lambda self, p:
                  f"{p.get('run', '')}:{p.get('step', '')}"},
    "batch_step_stop": {
        "text": "Stop these tasks now - their process trees die",
        "help": "batch_step_stop {run, steps[]} - kill several "
                "running/queued tasks",
        "validate": ConductorActions._v_batch_step_stop,
        "call": ConductorActions._x_batch_step_stop,
        "target": lambda self, p:
                  f"{p.get('run', '')}:{','.join(p.get('steps') or [])}"},
    "interrupt_step": {
        "text": "Interrupt this task and restart it with your "
                "instructions - kills the worker, respawns the same task",
        "help": "interrupt_step {run, step, instructions, attachments?} - "
                "stop the live task and respawn it with supplemental "
                "instructions",
        "validate": ConductorActions._v_interrupt_step,
        "call": ConductorActions._x_interrupt_step,
        "target": lambda self, p:
                  f"{p.get('run', '')}:{p.get('step', '')}"},
    "delete_run": {
        "text": "Delete this run - its whole record moves to recoverable "
                "trash",
        "help": "delete_run {run} - move the run dir to trash "
                "(recoverable for the retention window)",
        "validate": ConductorActions._v_run,
        "call": ConductorActions._x_delete_run,
        "target": lambda self, p: p.get("run", "")},
    "empty_trash": {
        "text": "Empty the trash - permanently and irreversibly deletes "
                "every entry",
        "help": "empty_trash {} - permanently delete all trashed "
                "entries (irreversible)",
        "validate": lambda self, p: (_extra(p), {})[1],
        "call": ConductorActions._x_empty_trash,
        "target": lambda self, p: "all trashed entries"},
    "serve_power": {
        "text": "Stop or restart the dashboard itself (workers survive)",
        "help": "serve_power {action} - stop or restart the dashboard "
                "process",
        "validate": ConductorActions._v_serve_power,
        "call": ConductorActions._x_serve_power,
        "target": lambda self, p: p.get("action", "")},
}
