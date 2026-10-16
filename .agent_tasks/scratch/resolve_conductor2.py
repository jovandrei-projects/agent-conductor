"""Resolve remaining union conflicts in conductor.py (cumulative-elapsed merge)."""
from pathlib import Path

p = Path("conductor.py")
src = p.read_text(encoding="utf-8")

pairs = [
    # run close: ended_at via end_iso + _run_iv_close + _bump_completion
    ("""<<<<<<< HEAD
            state["ended_at"] = now_iso()
            _bump_completion(state)
=======
            end_iso = now_iso()
            _run_iv_close(state, end_iso)
            state["ended_at"] = end_iso
>>>>>>> conductor/20261016-100852-run-review-ux/cumulative-elapsed-time""",
     """            end_iso = now_iso()
            _run_iv_close(state, end_iso)
            state["ended_at"] = end_iso
            _bump_completion(state)"""),
    ("""<<<<<<< HEAD
                state["ended_at"] = now_iso()
                _bump_completion(state)
=======
                end_iso = now_iso()
                _run_iv_close(state, end_iso)
                state["ended_at"] = end_iso
>>>>>>> conductor/20261016-100852-run-review-ux/cumulative-elapsed-time""",
     """                end_iso = now_iso()
                _run_iv_close(state, end_iso)
                state["ended_at"] = end_iso
                _bump_completion(state)"""),
    ("""<<<<<<< HEAD
    state["ended_at"] = now_iso()
    _bump_completion(state)
=======
    end_iso = now_iso()
    _run_iv_close(state, end_iso)
    state["ended_at"] = end_iso
>>>>>>> conductor/20261016-100852-run-review-ux/cumulative-elapsed-time""",
     """    end_iso = now_iso()
    _run_iv_close(state, end_iso)
    state["ended_at"] = end_iso
    _bump_completion(state)"""),
    ("""<<<<<<< HEAD
                st["ended_at"] = now_iso()
                _bump_completion(st)
=======
                end_iso = now_iso()
                _run_iv_close(st, end_iso, run_dir=sj.parent,
                              witness_cap=True)
                st["ended_at"] = end_iso
>>>>>>> conductor/20261016-100852-run-review-ux/cumulative-elapsed-time""",
     """                end_iso = now_iso()
                _run_iv_close(st, end_iso, run_dir=sj.parent,
                              witness_cap=True)
                st["ended_at"] = end_iso
                _bump_completion(st)"""),
    # run dict: keep both run-level review mark block AND elapsed split
    ("""<<<<<<< HEAD
        # Run-level review mark, same sentinel/folded layering as the
        # steps'. A tombstone shows at once; a seq mismatch only reads as
        # 'new completion to review' once the run actually ended (a
        # resumed run keeps its old seq but pops ended_at - flagging it
        # mid-run would resurrect an ack the in-flight work superseded).
        run_mark = _review_mark(sj.parent, st.get("review"))
        run_needs = bool(
            (isinstance(run_mark, dict)
             and run_mark.get("reviewed") is False)
            or (st.get("ended_at")
                and _needs_review(st.get("completion_seq"), run_mark)))
=======
        # closed-intervals driver cumulative; exec_start carries the
        # live interval's start so the runs table ticks base+now-start
        # without a payload refresh
        run_els, run_exec_start = _run_elapsed_split(st, sj.parent)
>>>>>>> conductor/20261016-100852-run-review-ux/cumulative-elapsed-time""",
     """        # Run-level review mark, same sentinel/folded layering as the
        # steps'. A tombstone shows at once; a seq mismatch only reads as
        # 'new completion to review' once the run actually ended (a
        # resumed run keeps its old seq but pops ended_at - flagging it
        # mid-run would resurrect an ack the in-flight work superseded).
        run_mark = _review_mark(sj.parent, st.get("review"))
        run_needs = bool(
            (isinstance(run_mark, dict)
             and run_mark.get("reviewed") is False)
            or (st.get("ended_at")
                and _needs_review(st.get("completion_seq"), run_mark)))
        # closed-intervals driver cumulative; exec_start carries the
        # live interval's start so the runs table ticks base+now-start
        # without a payload refresh
        run_els, run_exec_start = _run_elapsed_split(st, sj.parent)"""),
    # run modal kv: keep both the review row and the elapsed row
    ("""<<<<<<< HEAD
    // review marks live on run records only - a proposal/planner row is
    // preparation history, never a completion to ack
    ...(r.stage?[]:[['review',reviewLine(r)]]),
=======
    ['elapsed',runElapsed(r, Date.now())],
>>>>>>> conductor/20261016-100852-run-review-ux/cumulative-elapsed-time""",
     """    // review marks live on run records only - a proposal/planner row is
    // preparation history, never a completion to ack
    ...(r.stage?[]:[['review',reviewLine(r)]]),
    ['elapsed',runElapsed(r, Date.now())],"""),
]

for old, new in pairs:
    assert old in src, "MISSING: " + old[:90]
    src = src.replace(old, new)

p.write_text(src, encoding="utf-8", newline="")
print("resolved", len(pairs), "conflicts")
