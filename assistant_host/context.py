"""Conductor view-context adapter - the server-side `enriched` half of the
ContextEnvelope (DESIGN.md section 6.5).

The browser reports a lightweight ``client_ctx`` (view slug + refs +
filters + text selection); this adapter re-resolves those refs against the
REAL run/request records and returns a bounded ``enriched`` dict that
``AssistantCore.post_message`` freezes onto the turn's ``ctx_ref``. Client
text is never authoritative: ids are verified here, record fields come from
state.json - not from the page - and anything unresolved is reported
honestly (``not_found``/``unknown``) instead of guessed.

View providers (shared + custom modal coverage)
------------------------------------------------
The dashboard renders every modal into ONE shared ``#detail`` shell, each
with its own context needs - so providers are per-view callables living in
``_PROVIDERS``. Custom surfaces (or a future host view) register theirs via
``register_provider()``; an unregistered view falls back to the generic
provider which reports the client-supplied refs without inventing data.

Each enriched dict carries ``title`` (the view's headline), ``fields``
(concise meanings for the record fields actually present), ``workflows``
(the actions that view legitimately offers - descriptive, NOT a
capability grant; the read-only gate is enforced elsewhere), plus the
view's bounded record data.
"""
import json
import time
from pathlib import Path

import conductor
from assistant import context as ctxmod

TASK_CHARS = 1536            # like _summary_prompt's task budget
EXCERPT_CHARS = 6144         # ctxmod.ENRICHED_LEAF_MAX-aligned excerpt cap
RUN_LIST_MAX = 12
STEP_LIST_MAX = 24

# Step-record fields safe to surface as context (whitelist - env blobs,
# flag internals and credential-shaped keys never appear; the envelope
# sanitizer is a second layer, not the only one).
_STEP_FIELDS = (
    "status", "started_at", "first_started_at", "ended_at", "elapsed_s",
    "model", "model_actual", "effort", "mode", "attempts", "max_attempts",
    "session_id", "exit_code", "verify_ok", "timeout_min",
    "timeout_extended_s", "merged", "merged_at", "merge_target", "branch",
    "deferred", "cloud", "wt", "device", "device_spec", "device_booted",
    "slug", "purpose", "outcome", "note", "next", "status_line",
    "completion_seq", "pid", "prompt_edited")

# Concise field glossary - what the dashboard's own terminology means.
# Emitted per-view as `fields` covering only the keys actually present.
FIELD_MEANINGS = {
    "status": "record status (done/failed/blocked/partial/timed_out/"
              "stopped/lost/skipped/waiting/queued/running) - 'lost' means "
              "the record outlived its process",
    "effective_status": "status corrected for reality - a dead 'running' "
                        "record displays as 'lost'",
    "verify_ok": "the manifest's post-step verify gate (true=pass, "
                 "false=fail, null=none ran) - NOT the step's own tests",
    "elapsed_s": "cumulative work seconds across every dispatch/attempt; "
                 "idle gaps between attempts never count",
    "outcome": "the worker's own 1-2 sentence result report (OUTCOME: "
               "contract line) - self-reported, not verified",
    "next": "the worker's suggested follow-up (NEXT: line) - a handoff "
            "note, not a fact",
    "note": "conductor-side annotation (e.g. kept worktree, marked lost)",
    "tokens": "per-step LLM sums: prompt/completion/cached/llm_steps - "
              "cached is a SHARE of prompt, never additive",
    "merged": "worktree branch landed on the merge target",
    "session_id": "the devin CLI session that ran the step",
    "counts": "per-status step tally for the whole run",
    "archived": "run hidden from the default dashboard view (kept on disk)",
    "stage": "request lifecycle stage (draft/generating/questions/ready/"
             "stale/launched/discarded)",
    "pending_merges": "deferred worktree branches still waiting on a "
                      "deliberate merge - done work, not landed yet",
    "proposed_steps": "the generated plan's step snapshot - what would "
                      "run, in order",
    "pending_questions": "planner questions still awaiting an answer",
    "exec_intervals": "per-dispatch {start,end} spans elapsed_s sums over",
}

_VIEW_WORKFLOWS = {
    "dashboard": ["filter rows (text/facets/project/run)", "open a run or "
                  "task modal", "New request", "Compact/Full density",
                  "row context menus", "power dialog (restart/stop)",
                  "trash view + restore"],
    "agent": ["Session summary (report view)", "Mark as reviewed/"
              "needing review", "Stop this step (live)", "Skip/Unskip step",
              "Edit prompt (queued only)", "Interrupt + instruct (live)",
              "timeout editor", "resume notes (finished steps)",
              "open its run modal", "prev/next task nav, Back stack"],
    "run": ["Resume unfinished steps", "Pause run", "Stop run now",
            "Merge pending branches", "Generate LLM summary",
            "Archive/Unarchive", "Delete run", "Mark reviewed",
            "open task modals", "prev/next run nav"],
    "summary": ["prev/next summary nav", "Back to task", "open as page",
                "Mark as reviewed", "open its run modal"],
    "run-summary": ["Back to run", "open as page"],
    "request": ["Save", "Regenerate", "Apply options", "Start run",
                "Discard", "answer planner questions", "attach images",
                "edit fields/model/effort/merge policy"],
    "questions": ["answer option cards or free text", "skip a question"],
    "power": ["Restart dashboard", "Stop dashboard (workers keep "
              "running)"],
}


def _clip(text: str | None, cap: int) -> str | None:
    if text is None:
        return None
    text = str(text)
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n[...{len(text) - cap} chars truncated...]"


def _field_notes(mapping: dict) -> dict:
    """Glossary entries for the field names actually present."""
    return {k: FIELD_MEANINGS[k] for k in mapping if k in FIELD_MEANINGS}


class ConductorContext:
    """enricher callable for ``AssistantCore(enricher=...)``.

    ``enrich(env)`` takes the sanitized client envelope and returns the
    ``enriched`` dict. Bounded reads only: state.json + persisted
    ``<step>.summary.md`` (rebuilt via step_summary_md when absent) +
    request records; it never streams whole logs into the envelope.
    """

    def __init__(self, *, runs_dir, requests_dir=None, projects=None,
                 now=None):
        self._runs = Path(runs_dir)
        self._requests = (Path(requests_dir) if requests_dir
                          else self._runs.parent / "requests")
        self._projects = projects  # None -> conductor.load_projects() lazily
        self._now = now or time.time
        self._providers = {
            "dashboard": self._v_dashboard,
            "agent": self._v_agent,
            "run": self._v_run,
            "summary": self._v_summary,
            "run-summary": self._v_run_summary,
            "request": self._v_request,
            "questions": self._v_questions,
            "power": self._v_power,
        }

    def register_provider(self, view: str, fn) -> None:
        """Host extension point for a custom modal/surface: ``fn(refs,
        filters, selection) -> dict``. The view slug obeys the envelope's
        own validation (ctxmod.VIEW_RE)."""
        if not ctxmod.VIEW_RE.fullmatch(str(view or "")):
            raise ValueError(f"invalid view slug {view!r}")
        self._providers[view] = fn

    def enrich(self, env: dict) -> dict:
        view = (env or {}).get("view") or "dashboard"
        refs = env.get("refs") or {}
        filters = env.get("filters") or {}
        selection = env.get("selection") or ""
        provider = self._providers.get(view)
        if provider is None:
            out = {"title": f"view '{view}' (custom/unregistered)",
                   "note": "no server-side provider for this view - only "
                           "the client-reported refs/filters/selection are "
                           "available; nothing was re-resolved",
                   "refs_echo": refs, "filters_echo": filters}
        else:
            out = dict(provider(refs, filters, selection) or {})
        out.setdefault("view", view)
        out.setdefault("fields", _field_notes(out.get("record") or out))
        wf = _VIEW_WORKFLOWS.get(view)
        if wf and "workflows" not in out:
            out["workflows"] = wf
        return out

    # ---------------------------------------------------------- helpers
    def _projects_map(self) -> dict:
        if self._projects is not None:
            return self._projects
        try:
            return conductor.load_projects()
        except Exception:                  # noqa: BLE001 - never 500 on ctx
            return {}

    @staticmethod
    def run_key_of(st: dict, run_dir: Path) -> str:
        """The dashboard's runKey for a state.json: 'request:<id>' when the
        run came from a request, else the run dir's absolute path - the
        exact key agentKey/runKey build citations on."""
        return (f"request:{st['request_id']}" if st.get("request_id")
                else str(run_dir))

    def _find_run_by_request(self, request_id: str) -> tuple | None:
        for sj in sorted(self._runs.glob("*/state.json")):
            try:
                st = json.loads(sj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if st.get("request_id") == request_id:
                return sj.parent, st
        return None

    def _resolve_run(self, refs: dict) -> tuple | None:
        """refs -> (run_dir Path, state dict) or None.

        run_key may be 'request:<id>', an absolute run dir path, or the
        bare run dir name; run_dir may be a path or name. Resolution is
        strict - a guessed dir is never returned."""
        key = str(refs.get("run_key") or refs.get("run_dir") or "")
        if not key:
            sk = str(refs.get("summary_key") or "")
            if sk.startswith("run|"):
                key = sk[4:]                 # aux row's run summary
            elif "|" in sk:
                key = sk.rsplit("|", 1)[0]   # agentKey | 's|<dir>' cache key
                if key.startswith("s|"):
                    key = key[2:]
        if not key:
            return None
        if key.startswith("request:"):
            return self._find_run_by_request(key.split(":", 1)[1])
        name = Path(key).name if (":" in key or "/" in key or "\\" in key) \
            else key
        if not conductor.RUN_DIR_RE.fullmatch(name):
            return None
        d = self._runs / name
        sj = d / "state.json"
        if not sj.is_file():
            return None
        try:
            return d, json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _split_step(refs: dict, st: dict | None) -> str | None:
        step = refs.get("step")
        if not step:
            sk = str(refs.get("summary_key") or "")
            if sk and not sk.startswith("run|") and "|" in sk:
                step = sk.rsplit("|", 1)[1]
        if step and st is not None and conductor.RUN_DIR_RE.fullmatch(
                str(step)) and step in (st.get("steps") or {}):
            return str(step)
        return str(step) if step else None

    def _step_record(self, st: dict, step: str) -> tuple | None:
        rec = (st.get("steps") or {}).get(step)
        if rec is None:
            return None
        eff = conductor.effective_statuses(st)
        drv = conductor.driver_alive(st)
        fields = {k: rec.get(k) for k in _STEP_FIELDS
                  if rec.get(k) is not None}
        fields["effective_status"] = eff.get(step, rec.get("status"))
        fields["alive"] = conductor.step_live(rec, drv)
        tokens = rec.get("tokens")
        if isinstance(tokens, dict):
            fields["tokens"] = {k: tokens.get(k) for k in
                                ("prompt", "completion", "cached",
                                 "llm_steps") if tokens.get(k) is not None}
        return rec, fields

    def _summary_text(self, d: Path, st: dict, step: str,
                      rec: dict) -> tuple:
        """(markdown, origin) - the persisted <step>.summary.md first; a
        live step or a missing file gets a fresh step_summary_md render
        (bounded compute, no writes)."""
        p = d / f"{step}.summary.md"
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8",
                                   errors="replace"), str(p)
        except OSError:
            pass
        try:
            return conductor.step_summary_md(
                {**rec, "name": rec.get("name") or step}, st,
                st.get("run") or d.name), "computed"
        except Exception:                  # noqa: BLE001
            return "", "unavailable"

    def _request(self, request_id: str) -> tuple | None:
        if not request_id or not conductor.REQUEST_ID_RE.fullmatch(
                str(request_id)):
            return None
        try:
            rec = conductor._read_request(str(request_id), self._requests)
        except Exception:                  # noqa: BLE001
            return None
        return rec if isinstance(rec, dict) else None

    # --------------------------------------------------------- providers
    def _v_dashboard(self, refs, filters, selection) -> dict:
        runs = []
        total = 0
        running = 0
        archived = 0
        status_counts: dict[str, int] = {}
        for sj in sorted(self._runs.glob("*/state.json"), reverse=True):
            try:
                st = json.loads(sj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            total += 1
            if st.get("archived"):
                archived += 1
            if conductor.driver_alive(st):
                running += 1
            eff = conductor.effective_statuses(st)
            for s in eff.values():
                status_counts[s] = status_counts.get(s, 0) + 1
            if len(runs) < RUN_LIST_MAX:
                runs.append({"run": st.get("run") or sj.parent.name,
                             "dir": sj.parent.name,
                             "ended": bool(st.get("ended_at")),
                             "archived": bool(st.get("archived")),
                             "steps": len(st.get("steps") or {})})
        projs = sorted(self._projects_map())
        return {
            "title": "conductor dashboard",
            "record": {
                "runs_total": total, "runs_live": running,
                "runs_archived": archived, "step_statuses": status_counts,
                "recent_runs": runs, "registered_projects": projs},
            "note": "counts cover all recorded runs incl. archived ones; "
                    "the page's own row filter may hide some - active "
                    "filters are echoed under envelope.filters"}

    def _v_agent(self, refs, filters, selection) -> dict:
        found = self._resolve_run(refs)
        if found is None:
            return {"title": "task detail (run not resolved)",
                    "not_found": "run_key/run_dir did not resolve to a "
                                 "recorded run"}
        d, st = found
        step = self._split_step(refs, st)
        if not step or step not in (st.get("steps") or {}):
            return {"title": f"run {st.get('run') or d.name}",
                    "not_found": f"step {step or '?'} is not in this run",
                    "record": {"run": st.get("run"), "dir": d.name,
                               "steps": sorted(st.get("steps") or {})}}
        rec, fields = self._step_record(st, step)
        tail = conductor.stdout_tail(rec.get("stdout_log"), 12)
        out = {
            "title": f"task {step}",
            "run": st.get("run") or d.name,
            "run_dir_name": d.name,
            "record": fields,
            "task": _clip(conductor.task_display(
                conductor.task_label(rec, full=True)), TASK_CHARS),
        }
        if tail:
            out["log_tail"] = _clip(tail, 2048)
        return out

    def _v_run(self, refs, filters, selection) -> dict:
        found = self._resolve_run(refs)
        if found is None:
            return {"title": "run details (run not resolved)",
                    "not_found": "run_key/run_dir did not resolve"}
        d, st = found
        eff = conductor.effective_statuses(st)
        steps = st.get("steps") or {}
        step_rows = []
        for n, r in list(steps.items())[:STEP_LIST_MAX]:
            step_rows.append({
                "step": n, "status": eff.get(n, r.get("status")),
                "slug": r.get("slug"),
                "started": r.get("first_started_at") or r.get("started_at"),
                "ended": r.get("ended_at"),
                "elapsed_s": conductor.rec_elapsed_s(r),
                "merged": bool(r.get("merged")),
                "outcome": _clip(r.get("outcome"), 200)})
        pm_groups, pm_legacy = conductor._pending_merge_view(st, eff)
        pend = sum(len(g.get("entries") or []) for g in pm_groups or []) \
            + len(pm_legacy or [])
        return {
            "title": f"run {st.get('run') or d.name}",
            "run_dir_name": d.name,
            "record": {
                "run": st.get("run"), "request_id": st.get("request_id"),
                "description": _clip(st.get("description"), 400),
                "started_at": st.get("started_at"),
                "ended_at": st.get("ended_at"),
                "elapsed_s": conductor.run_elapsed_s(st),
                "archived": bool(st.get("archived")),
                "aborted": st.get("aborted"),
                "counts": {s: sum(1 for v in eff.values() if v == s)
                           for s in set(eff.values())},
                "steps": step_rows,
                "steps_omitted": max(0, len(steps) - STEP_LIST_MAX),
                "pending_merges": pend,
                "driver_alive": conductor.driver_alive(st),
                "merge_target_hint": next(
                    (r.get("merge_target") for r in steps.values()
                     if r.get("merge_target")), None)}}

    def _v_summary(self, refs, filters, selection) -> dict:
        found = self._resolve_run(refs)
        if found is None:
            return {"title": "session summary (run not resolved)",
                    "not_found": "run_key/run_dir did not resolve"}
        d, st = found
        step = self._split_step(refs, st)
        rec = (st.get("steps") or {}).get(step or "")
        if rec is None:
            return {"title": f"run {st.get('run') or d.name}",
                    "not_found": f"step {step or '?'} is not in this run"}
        md, origin = self._summary_text(d, st, step, rec)
        _, fields = self._step_record(st, step)
        return {
            "title": f"session summary: {step}",
            "run": st.get("run") or d.name,
            "record": fields,
            "excerpt": _clip(md, EXCERPT_CHARS),
            "excerpt_origin": origin,   # 'persisted <path>' vs 'computed'
        }

    def _v_run_summary(self, refs, filters, selection) -> dict:
        found = self._resolve_run(refs)
        if found is None:
            return {"title": "run summary (run not resolved)",
                    "not_found": "run_key/run_dir did not resolve"}
        d, st = found
        p = d / "run.summary.md"
        try:
            if p.is_file():
                return {"title": f"run summary: {st.get('run') or d.name}",
                        "excerpt": _clip(
                            p.read_text(encoding="utf-8", errors="replace"),
                            EXCERPT_CHARS),
                        "excerpt_origin": str(p)}
        except OSError:
            pass
        return {"title": f"run summary: {st.get('run') or d.name}",
                "excerpt": _clip(conductor.run_report_md(st),
                                 EXCERPT_CHARS),
                "excerpt_origin": "deterministic roll-up (run_report_md) "
                                  "- no LLM run.summary.md exists"}

    def _v_request(self, refs, filters, selection) -> dict:
        rid = str(refs.get("request_id") or "")
        rec = self._request(rid)
        if rec is None:
            return {"title": "request/proposal (not found)",
                    "not_found": f"request {rid or '?'} is not recorded"}
        stage = None
        try:
            stage = conductor._request_status(rec, self._runs)
        except Exception:                  # noqa: BLE001
            pass
        steps = rec.get("proposed_steps") or []
        return {
            "title": f"request {rid}"
                     + (f": {rec.get('label')}" if rec.get("label") else ""),
            "record": {
                "id": rid, "label": rec.get("label"),
                "stage": stage, "status": rec.get("status"),
                "project": rec.get("project"),
                "model": rec.get("model"), "effort": rec.get("effort"),
                "created_at": rec.get("created_at"),
                "updated_at": rec.get("updated_at"),
                "stale": bool(rec.get("stale")),
                "pending_questions": len(rec.get("pending_questions") or []),
                "proposed_steps": [
                    {"step": s.get("step"), "slug": s.get("slug"),
                     "task": _clip(s.get("task"), 160)}
                    for s in steps[:STEP_LIST_MAX]],
                "followup": rec.get("followup"),
                "source_run": rec.get("source_run")},
            "request_text": _clip(rec.get("raw"), TASK_CHARS)}

    def _v_questions(self, refs, filters, selection) -> dict:
        rid = str(refs.get("request_id") or "")
        rec = self._request(rid)
        if rec is None:
            return {"title": "planner questions (request not found)",
                    "not_found": f"request {rid or '?'} is not recorded"}
        qs = rec.get("pending_questions") or rec.get("qa") or []
        cur = None
        if isinstance(qs, list) and qs:
            q0 = qs[0]
            cur = _clip((q0.get("question") or q0.get("q")
                         if isinstance(q0, dict) else str(q0)), 512)
        return {"title": f"planner questions: {rid}",
                "record": {"id": rid, "questions_pending":
                           len(qs) if isinstance(qs, list) else None},
                "current_question": cur}

    def _v_power(self, refs, filters, selection) -> dict:
        return {"title": "dashboard power dialog",
                "note": "transient dialog over the dashboard - restart "
                        "respawns serve (workers keep running), Stop shuts "
                        "only the dashboard down"}
