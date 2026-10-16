# Task: full-trajectory-view

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

The task modal's trajectory arc showed only ~110-char `head` lines. Make
expanded entries expose the complete text stored in the ATIF export
(message / reasoning_content / tool-call args / tool results), with
per-entry expansion + expand-all/collapse-all, wrapped selectable text,
a copy affordance, and no UI freeze on large trajectories. Compact
collapsed view stays the default. Text never retained must say so, not
fabricate.

## What landed

- `trajectory_detail(export)` + `cached_arc_detail` (mtime-keyed,
  16-entry bound): serves every step's full `message`,
  `reasoning_content`, tool calls as `{name, args, result}` (args
  rendered `key: value` so multiline edit payloads stay literal),
  unmatched `observation.results` as `extra_obs`, plus `src`/`model`.
  Fields over `ARC_DETAIL_CAP` (200k) ship their head with the real
  length in `trunc` - labeled in the UI, never silently dropped.
- Endpoints (same guards as `/summary`): `GET
  /api/runs/<r>/steps/<s>/trajectory` and
  `/api/requests/<id>/trajectory` (planner proposal session). Missing
  export / step-less mock export / escaped-path record all 404 with a
  truthful reason. `/api/state` stays compact - only `arc.em` (mtime)
  and `arc.groups_total` were added to the polled payload.
- UI: entries are nested `<details class="arcent">` under each arcgrp;
  Expand all / Collapse all buttons; `trajOpen` (agent-keyed Sets)
  persists open state across the 2s modal rebuild (fixes the old
  collapse-on-tick too); `trajDetail` caches fetched bodies by
  export-path+mtime so a resume-refreshed export refetches cleanly.
  Bodies render only when open and carry `content-visibility:auto`.
- Copy: per-entry Copy button -> `arcPlainText` (verbatim, section-
  labeled, never HTML) via clipboard API + execCommand fallback.
- `.arcpre` blocks: `pre-wrap` + `overflow-wrap:anywhere` + selectable;
  `.arc:has(details.arcent[open])` releases the compact 280px cap to
  70vh. An in-modal text selection pauses the tick rebuild
  (`modalSelActive`) so mid-read/mid-copy isn't destroyed.
- Groups beyond the 40-group cap now announce their count instead of
  vanishing silently.

## Honest-absence handling

Mock exports (`{"mock": true}`) and step-less/corrupt exports return
404 'no readable trajectory'; an entry with no stored text renders
'no detailed text was recorded'; steps with no export linked say so.
Nothing is synthesized from the 110-char `head`.

## Verification

- 272/272 `python run_tests.py` green (8 new `TrajectoryDetailTests`:
  extraction incl. multiline/whitespace/result-pairing/orphan results,
  honest cap, junk tolerance, endpoint 404s incl. traversal + mock
  export, request endpoint; 2 node-harness tests covering expand/
  collapse transitions, open-state persistence + run isolation,
  XSS-escaped rendering, verbatim copy payload).
- `python conductor.py demo` green.
- HTTP smoke on a staged real export: 200 with full step-4 system
  message (637 chars), step-8 reasoning 7.7k + paired tool results;
  bad names / missing step / bad request id all 404; `/api/state` head
  stays 110 chars.

## Where it stopped

Done - committed and recorded.
