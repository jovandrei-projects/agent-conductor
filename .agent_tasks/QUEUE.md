# Task queue

The master ordered list. One line per task. This answers "what next"; the
task's own `STATE.md` answers "where in it".

Status is one of `next`, `in progress`, `blocked`, `paused`, `done`. Keep the
order meaningful, but see rule 3 in `README.md`: finishing something early is
fine, it just has to be ticked where it lives.

Conventions for `STATE.md` are in `README.md`; the template is in `_template/`.

| # | Task | Status | Folder | Waits on |
|---|---|---|---|---|
| 1 | Bootstrap: conductor.py v1 + mock tests + docs | done | bootstrap/ | trap 5 root-caused + fixed (no workspace writes while a worker is alive); auth solved; live smoke green |

## Not yet filed as tasks

- First real overnight manifest on a real project (Phase 2 in ROADMAP.md; live smoke already green)
- Phone access to the :8773 dashboard via lan-bridge proxy (needs mobile-apps change; tasks-ui rows already give coarse progress)
