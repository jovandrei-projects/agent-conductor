# Task: whats-new-split

Redesign the dashboard "What's new" walkthrough into two sections -
**Tool updates** (change/curated cards) and **Work digest** (record
cards) - with folded request/run/follow-up work cards and asynchronous
feed refresh. Manifest run `20261019-031845-whats-new-split`
(`manifests/20261019-031223-58671699-whats-new-split.toml`), four steps;
each step lands on its own `data/worktrees/20261019-031845-*/<step>`
branch.

**Status:** in progress   **Started:** 2026-10-19   **Last touched:** 2026-10-19

## Objective

The tour distinguishes tool changes from conductor work, tells each
work event once (folding related request/run/follow-up records into one
card), shows review metadata on run cards, keeps a single persistent
acknowledgment model, and refreshes the feed without blocking the deck.

## Not in scope

- `tour_state.py` persistence shape - the single
  `{watermark, viewed[], snoozed[]}` file stays; no migrations, no
  per-section acknowledgment state.
- tour_polish internals.
- Server/API rewrites beyond what a step's manifest asks.

## Decisions already made

- **Folded-card identity is the RUN event** (`run:<dir>:<ended>`,
  `kind:"pipeline"`): the completion is the newest event, so the folded
  card is genuinely new info even when the request card was already
  seen. The absorbed standalone ids ride a `covers` list -
  `_rematerialize` resolves a `request:<rid>`/`followup:<rid>`
  descriptor to the folded card, so viewed-history reconstruction
  survives the fold.
- **Folding matches recorded links only** (`request_id`,
  `source_request`, `source_run` -> dir -> `request_id`), never
  display names. Both events must sit inside the SAME feed window for
  request+run fold; the associated request must be in-window for
  follow-up fold (transitive chains resolve to the root card).
- **Record tags are event-derived**, deliberately disjoint from
  change-style `new`/`improved`/`fixed`: proposals `ready`;
  run/pipeline `outcome` (`done`/`failed`/`partial`/`stopped`/`paused`/
  `drained`/`ended`/recorded `aborted` reason). `TOUR_TAG_STYLE` gained
  the event colors so today's renderer still paints real tag text.
- **Deck = unread.** `entries` excludes `state=='viewed'` cards the
  moment they are acknowledged - the watermark does not need to advance
  first. Viewed cards remain reachable via `history`.
- **Per-build range memoization**: `build_feed` collects each distinct
  `{base,tip}` exactly once (live window + descriptor windows share the
  `collected` dict keyed by `_range_key`); entries are `dict(e)`-copied
  before annotation so shared collections are never mutated; nothing
  persists across builds.
- **Review counts are read-only**: `unreviewed` = count of steps where
  `_needs_review(completion_seq, newest-mark)` - sentinel flags in the
  shared flags dir (`flags_dir` param, default
  `%TEMP%/agent-conductor-flags`) layered over `srec.review` marks,
  mirroring conductor.py's layering. Feed construction never writes a
  mark.

## Phases

### Step feed-build - DONE

- [x] `section`/`section_title` on every card (`tool` = change/curated,
  `work` = run/request/followup/pipeline; `unknown` stubs fall back to
  their id prefix).
- [x] Request+in-window-run -> `pipeline` card (`covers`, `proposed_at`,
  `completed_at`, `outcome`, `steps_done`, `steps_total`, `unreviewed`).
- [x] Follow-up -> folds into associated request's card when that
  request is in-window (`followups` list + `covers`); standalone when
  not. Cross-window request/run counterparts stay standalone.
- [x] Record-event tags (`ready` for proposals; outcome word for
  run/pipeline); `TOUR_TAG_STYLE` extended for current renderer.
- [x] `_collect_window` memoized per exact `(base,tip)` inside each
  `build_feed` incl. descriptor windows; no cross-build cache; cached
  results never mutated by assembly.
- [x] `state=='viewed'` excluded from live `entries` on feed load;
  `history` keeps re-materialized viewed cards; snoozed/unviewed
  descriptors re-inject, skipping ids a live folded card covers.

### Step walkthrough-ui - pending

Vanilla-JS two-section walkthrough in `_DASH_HTML`. Feed contract it
consumes (all present now):

- `e.section` = `"tool"|"work"`, `e.section_title` = display name.
- `e.kind`: `change|curated` (tool), `run|request|followup|pipeline`
  (work), `unknown` (history stub).
- Run/pipeline: `outcome`, `completed_at`, `proposed_at`,
  `steps_done`/`steps_total`, `unreviewed` (task count to surface as a
  review nudge), `tag` = outcome word.
- Pipeline: `covers` (absorbed standalone ids), `followups[]` =
  `{id,label,at,requests,runs}`.
- Request/followup: `tag="ready"`, `proposed_at`; `runs[]` links a
  launched run when one exists.
- `history` entries share the same fields (`state=='viewed'`).
- Card order inside `entries`: `at` ascending; `unseen_count` counts
  state `new` only.

### Step deferred-transition - pending

Asynchronous feed refresh / transition behavior per manifest.

### Step tour-tests - pending

Comprehensive regression expansion per manifest (this step only made
the minimal adjustments: folded-card expectations in
test_tour_feeds/api/e2e, viewed-in-history assertions, new
SplitFeedTests class, and a pre-existing 48h auto-archive time-bomb fix
in test_conductor's cumulative-elapsed payload test).

### Step follow-up-plan - pending

Closing review + next manifest only.

## Where it stopped

feed-build merged to master via merge dd1df59 (conflict resolution:
5 files, all semantic-equivalent both-changed regions - kept the
branch's section stamping plus HEAD's stronger viewed->history
assertions and `include_archived` archive-sweep isolation; 74 tour
tests + the cumulative-elapsed test green). Next: `walkthrough-ui`
step of run 20261019-031845-whats-new-split - split `_DASH_HTML`'s
walkthrough into 'Tool updates' vs 'Work digest' sections using the
feed contract above (cards carry `section`; render `unreviewed`,
dates/outcome/steps on work cards; keep spotlight anchors on tool
cards). Feed side is committed on this step's branch; only renderer
work remains in scope there.

## Verification

- `python -m unittest tests.test_tour_feeds` - 39/39 (28 prior + 11
  new/changed SplitFeedTests + 2 adjusted).
- `python -m unittest tests.test_tour_state tests.test_tour_api
  tests.test_tour_dash` - 54/54.
- `python -m unittest tests.test_tour_polish tests.test_tour_e2e` -
  32/32.
- `python run_tests.py --jobs 8` - 992 tests, 8/8 shards ok.

Pre-existing defect fixed en route (not tour scope but it gated the
verify command): `test_conductor.CumulativeElapsedTests.
test_dashboard_emits_cumulative_step_and_run_elapsed` had a stale
`ended_at` fixture that `_auto_archive`'s 48h sweep started swallowing
once wall-clock crossed 2026-10-18 - now isolates the sweep so the
payload contract is what gets asserted.

## Deferred or blocked

None known.
