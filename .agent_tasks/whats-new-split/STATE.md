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

### Step walkthrough-ui - DONE

Vanilla-JS two-section walkthrough in `_DASH_HTML`, landed on this
step's branch. Render contract (all client-side, feed fields consumed
as documented below):

- `tourLoad` partitions `entries` by `e.section` into Tool updates
  (first) then Work digest, keeping feed `at` order inside each;
  `tourSec` falls back to the id prefix for cards lacking `section`.
  Empty section = no cards, nav never blanks; all-empty stays on the
  'All caught up' card.
- Card header: `.tsecname` section title + per-section `card i of n`
  (was global `Update i of N`) + `stop_title`; `#tourcard` gets
  `sec-tool`/`sec-work` top-edge accent following the current card
  (cleared on close/loading/error/caught-up). `hist` mode keeps
  `viewed i of N` with the same section prefix; `tourHistHtml` rows
  label their section too.
- Work-card `tag` renders FILLED (`.ttag.ev`, `background:<hex>26`)
  with `run outcome - a record event`/`record event` titles; tool tags
  stay outline `change type`. run/pipeline cards use verbatim
  `e.outcome` over `e.tag`; unknown outcome words still print (color
  defaults #8b949e) - the chip never lies about the event.
- `.tmeta` line: `proposed`/`finished`/`k/n steps done` for work cards,
  `loc(e.at)` for tool cards.
- `e.unreviewed` on run/pipeline renders as a `.trev` chip wired to
  `data-run-details=<runs[0]>` (the existing run modal; `0` = muted
  'all reviewed'). Mark viewed stays notification-ack only.
- Chips: run/pipeline = run chip only; request/followup/tool =
  named requests + recorded runs only. Both kinds coexisting get
  explicit `Request`/`Run` `.relk` prefixes + `rel req`/`rel run`
  tints. Request chip = the request's own identity (stage row's `run`
  label else bare rid - never the launched run's name); run chips
  prefer the non-stage row sharing the key. `followups[]` render as
  `.tfu` rows (request chip + recorded run links + stamp).
- `tourAcked` (page-life Map id -> marked card): marked ids are
  filtered out of re-pulled feeds (a pre-mark cache can still list
  them) and reattached to `history` when absent - client ack is
  authoritative for the deck, unview deletes the id. Transition
  responses repaint the badge ONLY when `unseen_count` is numeric -
  deferred/absent leaves the last-known pill to the `/api/state` poll.

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

walkthrough-ui implemented + verified on branch
`conductor/20261019-031845-whats-new-split/walkthrough-ui` (feed-build
had already merged to master via dd1df59). Next: `deferred-transition`
step of run 20261019-031845-whats-new-split - move POST
/api/tour/<id>/{viewed,snooze,unview} off the synchronous `_tour_build`
(tour_transition, conductor.py ~line 14047): persist first, rebuild on
a daemon, return a last-known/deferred `unseen_count` - the renderer
already tolerates both shapes (numeric-only badge repaint + the
`tourAcked` stale-feed filter documented above).

## Verification

- `python -m unittest tests.test_tour_dash` - 12/12 (10 prior updated
  to the per-section `card i of n` position text + 2 new:
  split-section order/chrome/chips and empty-section + deferred-count
  + stale-feed no-reinsert).
- `python -m unittest tests.test_tour_state tests.test_tour_feeds
  tests.test_tour_api tests.test_tour_dash tests.test_tour_polish
  tests.test_tour_e2e` - 127/127.
- `python run_tests.py --jobs 8` - 999 tests, 8/8 shards ok.

Earlier (feed-build): 39/39 test_tour_feeds, 54/54 state+api+dash,
32/32 polish+e2e, 992-test suite green; cumulative-elapsed auto-archive
flake fixed en route.

## Deferred or blocked

None known.
