# RAM Phase 4 — eliminating converged work

Written 2026-09-14 from a cloud session with no Docker. Nothing here was
measured against a container; every memory claim in this document is a
prediction to be checked, not a result.

The premise: the runtime now has memory ceilings that return high-water
processes to the OS. The point of this phase is to create fewer high-water
states in the first place, by not doing work whose answer is already known.

Production baseline this was written against: ~738-739 MiB raw cgroup, ~632 MiB
total process PSS, ~608 MiB anonymous PSS, roughly 15 minutes after start.

---

## 1. What changed

### 1.1 IMDB game credits — three loops with no memory of an empty answer

`Refresh IMDB game credits from datasets` held a background worker for 698
seconds, searched 1,966 people, matched 1,048, and updated nothing. Three
separate loops re-asked questions that already had answers:

| Loop | Was | Now |
|---|---|---|
| TMDB person profile lookup | every IMDB person still missing an image or gender, every run | once per person per `PERSON_PROFILE_BACKFILL_VERSION`; transient provider failures keep an exponential retry |
| IGDB studio backfill | every game with no studio credits, every run | `MetadataBackfillState` pending/retry, like every other backfill |
| IMDB title match | every unmatched game, every run - and one candidate pulls the whole `title.basics` dataset into the worker | unmatched games back off for a week; the in-memory title index is built only for the title keys a candidate asks for |

A note on the retry horizon, because the first version of this change got it
wrong. The shared backoff caps at one day and these tasks run nightly, so a
miss recorded on the default schedule is due again on the very next run - the
backoff defers nothing at all. Callers whose retry is expensive now pass
`min_delay_seconds` to set a floor that clears their own beat interval. The
tests advance across a week of simulated nightly runs rather than re-running
immediately, which is what the original tests did and why they missed it.

`count_people_missing_profiles()`, which gates the startup sweep, now counts
outstanding lookups rather than missing images. A converged library stops
queueing the task at all.

Invalidation: bump `PERSON_PROFILE_BACKFILL_VERSION` to re-open everyone; a
changed `Person.name` re-opens that person, because the name is the whole
lookup key.

**Docker validation.** Background Celery worker.
- The startup sweep should not queue `Refresh IMDB game credits from datasets`
  at all once converged. Confirm by its absence from the task log after a
  restart, not by task duration.
- When it does run (nightly, or after a manual game metadata sync), the
  `imdb_game_credits: starting TMDB person profile backfill for N people` line
  should report a small N, not ~1,966. Expect the run to drop from ~698s to
  seconds.
- Expect roughly 1,966 fewer TMDB calls and one fewer `title.basics` download
  per run. The download is the interesting one for memory: watch the
  background child's RSS across the task rather than only its settled value.

### 1.2 Metadata backfill — terminal failures re-queued forever

One startup pass processed 150 items in 144 seconds; 148 failed, mostly
MusicBrainz 400/404. The release and status queues were the only backfill
queues with no state filtering at all, and both order by oldest
`metadata_fetched_at`, so dead ids sorted straight back to the front.

Attempts are now classified:

| Outcome | Treatment |
|---|---|
| provider 400/404/410/422 | terminal — the id is wrong, not the provider |
| an item whose own identity is unusable (`MalformedItemIdentityError`) | terminal — a season row with no season number can never be fetched |
| everything else: 5xx, 429, unreachable host, missing API key (401/403), an unconfigured provider, anything unanticipated | transient — keeps its exponential retry |
| fetch succeeded, field still blank | pending — the provider may fill it in later, but not on the next cycle |

The classification is a deliberate allowlist rather than a heuristic. An
earlier version treated any `ValueError`/`TypeError`/`KeyError` as terminal,
which would have retired every TVDB item permanently the moment TVDB
credentials lapsed, since `tvdb._request` raises a bare `ValueError` for that.
Wrongly retrying costs one request; wrongly retiring loses the item silently
and forever, so anything unrecognised stays retryable.

Invalidation: `RELEASE_BACKFILL_VERSION` / `STATUS_BACKFILL_VERSION`
(`_apply_backfill_state_filters` is now version-aware), and an identity change
— the TVDB migration rewrites `media_id` in place and clears the state for the
rows it re-points, because a verdict is about a provider id, not a row.

**Docker validation.** Background Celery worker, `Backfill item metadata`.
- `metadata_backfill_error` count per pass should fall toward zero over a few
  cycles instead of sitting near 148/150.
- `remaining_release` and `remaining_status` in the task result should shrink
  and stay shrunk, rather than plateauing.
- Confirm no legitimate item was silently dropped: an item that gains a
  release date upstream must still pick it up. Check `MetadataBackfillState`
  rows with `field='release'` and `give_up=True` and spot-check a few ids
  against MusicBrainz by hand.

### 1.3 Pocket Casts — re-walking an unchanged catalog

Four runs of ~1,019 seconds each, ~30% of nine hours of background worker
time, walked ~11,000 episodes across 12 shows and imported nothing. Each
episode cost a SELECT by UUID, a hydrated model, and a comparison.

Each show's stored catalog is now read once as plain rows; an episode whose
stored values already match what the sync would write is skipped with no
query and no hydration. The comparison is driven from one field table shared
with the write path, and a consistency test walks every writable field in both
directions.

**This does not avoid fetching the catalog.** See §2.1.

**Docker validation.** Background Celery worker, the recurring Pocket Casts
import.
- New log line: `pocketcasts_catalog_sync user=… shows=… synced=… skipped=…`.
  On a settled library, `skipped` should dominate and `synced` should be near
  zero.
- Run duration should fall well below ~1,019s. The remaining time is the
  provider fetch, not local work.
- Watch the background child's RSS across a full run: ~11,000 hydrated model
  instances were the peak, and they are gone.

### 1.4 List endpoints — hydrating a whole list to return a page

Both list API endpoints hydrated every item — one media lookup per item —
and then sliced. Returning twenty rows from a 4,683-item list cost 4,683
queries and 4,683 hydrated objects. The detail endpoint additionally
prefetched every item a second time for a count `COUNT(*)` answers.

The page is now taken at the database layer whenever no aggregated sort is
requested. An aggregated sort still has to rank every item before it knows
which are on the page, so that path is unchanged.

Regressions assert cost follows the page, not the library, and fail on the
previous code (50 and 46 queries for a 40-item list).

**Docker validation.** Gunicorn web worker, `GET /api/v1/lists/<id>` and
`/items`. Time the request against the real 4,683-item list and watch the
serving worker's RSS during it. This is a per-request allocation spike, so
look at peak RSS during the request, not the settled value.

### 1.5 TVDB migration — re-asking, and starving the queue

The nightly migration pins a show it will never migrate, but a show whose
TVDB id simply does not resolve is deliberately not pinned, and nothing
recorded the attempt. Those shows were re-resolved against the providers every
night forever. Worse, the batch is taken in id order, so a backlog of
unresolvable shows filled the batch and a newly tracked show never got a turn.

Unresolvable shows and crashed attempts now use the same backoff as every
other backfill.

**Docker validation.** Background Celery worker, `Migrate TV shows to
preferred metadata provider`. The `skipped` count should fall to near zero
after a couple of nights while `migrated` continues to advance, and a
newly tracked TVDB-preferring show should migrate on the next run rather than
waiting behind the backlog.

---

## 2. Deliberately not done, and why

### 2.1 Skipping the Pocket Casts catalog fetch entirely

The remaining cost is the provider round-trip: `GET /podcast/full/<uuid>` per
show per poll, paginated up to 10 pages. Avoiding it needs a freshness token
that is safe to trust.

`/user/podcast/list` returns per-podcast fields that look like candidates
(`lastEpisodePublished`, `lastEpisodeUuid`), but "looks like a watermark" is
not "is a watermark". Trusting the wrong one silently stops importing
episodes, which is a correctness bug the user would not notice for weeks.

**What a Docker/credentialed session needs to establish, in order:**
1. Capture a raw `/user/podcast/list` response for the real account. Record
   which fields are present on every podcast, not just some.
2. Establish whether `lastEpisodePublished` changes when an episode is *edited*
   (title, duration, audio URL) rather than added. If it does not, it is a
   watermark for additions only, and a metadata correction would be missed.
3. Establish behaviour for a deleted or unlisted episode.
4. Only then: skip the full-metadata fetch for a show whose token matches the
   stored one, and keep a periodic full reconcile (weekly is likely enough) as
   the backstop against a token that lies.

Until (1)-(3) are answered from real responses, guessing is worse than the
current cost. The `synced=…/skipped=…` log line from §1.3 is the instrument:
it tells you what a token would have saved before you implement it.

### 2.2 Making `refresh_statistics_cache_task` interruptible

The instruction was to implement only with strong tests, and otherwise leave
routing intact and produce a design. Leaving routing intact is the right call
here; the task is not close to chunkable as written.

Why it resists decomposition (`app/statistics_refresh.py`,
`refresh_statistics_cache`):
- One `prefetch` object is built for the entire day range up front and shared
  by every day's build. Chunking either rebuilds it per chunk — turning one
  bulk query per media model into one per chunk — or persists it, which is the
  large object you are trying not to retain.
- `backfill_collector` accumulates across all days and is flushed once at the
  end.
- `cache_write_fallback_days` carries days whose cache write failed into the
  final aggregate, so the aggregate is not a pure function of what is in the
  cache.
- The dirty-day set is cleared only after the aggregate succeeds, so a partial
  run must not clear it.
- The refresh lock is held by one task for the whole run and deleted in
  `finally`. A chain would need explicit lock ownership across links, including
  the case where a link dies.

A concrete design, if this is picked up:
1. Persist a per-run record (user, range, ordered day list, cursor, collected
   backfill ids, failed-write days) keyed by `history_version`, so a run is
   resumable and a history change invalidates it.
2. Make the lock refer to the run record rather than the task, with a
   heartbeat, so a dead link's lock expires rather than blocking forever.
3. Chain: `start` (resolve days, take the lock, write the record) → N ×
   `build_chunk` (rebuild prefetch for that chunk only, advance the cursor) →
   `finish` (aggregate, cache, clear dirty days, release the lock).
4. Accept the per-chunk prefetch cost explicitly and size the chunk so it is
   amortised — a chunk of 1 day is strictly worse than today.
5. Tests must cover: a link dying mid-run, a history version changing
   mid-run, a forced refresh arriving mid-run, and the cache semantics being
   identical to a single-shot run for the same input.

That is a piece of work in its own right, not a side change, and it should be
done when someone can measure interactive-queue latency before and after.

### 2.3 Process-lifetime state audit — no unbounded state found

Audited: module-level dicts/sets/lists, `lru_cache`/`cache` decorators, client
and session registries, provider metadata caches, singletons, task-local state.

| What | Classification |
|---|---|
| `integrations.anime_mapping._IN_MEMORY_SNAPSHOT` | Bounded and replaced, not accumulated. One snapshot, capped by `MAX_MAPPING_BYTES` (8 MiB) and `MAX_MAPPING_ENTRIES` (50,000). Keyed by revision+digest, so a new revision replaces rather than adds. |
| `app.services.trakt_popularity.load_calibration_fixture`, `_calibration_reference_scores` | `maxsize=1`, fixture-sized. |
| `config.sqlite_recovery_server` `lru_cache(maxsize=4)` | Bounded. |
| `integrations.state.outbound._ADAPTER_BUILDERS` | Registry of adapter types, bounded by code. |
| `users.home_screen.STATUS_FILTER_ALIASES` | Built at import from `Status` choices. Bounded. |
| `app.providers.musicbrainz._last_request_time` | A float. |
| `SNAPSHOT_BUILD_COUNT`, `CLASSIFIER_CALL_COUNT` | Integer audit counters. |

Nothing grows with users, items, or requests. No fix was made, because
inventing one here would have been a change with no defect behind it.

The one item worth a Docker note: `_IN_MEMORY_SNAPSHOT` is bounded but
*permanently resident*, up to 8 MiB in every process that touches anime
mapping. That is a fixed floor, not aging. If a later sample shows the
background and interactive children both carrying it, dropping it to a
cache-only read is a known, safe lever — at the cost of a Redis round-trip and
a re-freeze per call, which is exactly what the memo was added to avoid.

### 2.4 Home screen custom-list rows — found, not fixed

`users/home_screen.py::_custom_list_entries` has the same shape as §1.4:
it hydrates every item in a custom list, builds a media lookup for all of
them, sorts in Python, and only then slices `entries[batch_start:batch_end]`
for ~10 cards. On the real 4,683-item list that is 4,683 hydrated items per
cache miss, in a Gunicorn worker.

It was not fixed because, unlike the API endpoint, the sort is applied in
Python across the whole row (`sort_home_entries`) and `total` is
`len(entries)`, so pushing the page into SQL requires establishing that each
supported `row.sort_by` has an equivalent database ordering. That is a real
piece of work with real regression surface, and it deserves its own change
rather than being bolted onto this one.

Suggested approach: handle the non-smart custom list with the default
date-added ordering first — that case maps directly to the existing
`customlistitem__date_added, id` queryset — take `total` from `COUNT(*)`, and
leave every other sort on the current path. Require a query-count regression
like the one in `api/tests/test_fork_list_pagination_cost.py`.

### 2.5 RSS ceilings and `WEB_CONCURRENCY`

Untouched, deliberately. Tuning either needs aging evidence from a container.

---

## 3. What none of this proves

Every change here reduces work or retained objects. None of it has been shown
to reduce settled RSS, because that cannot be shown from this session. The
honest summary is:

- The per-run allocation peaks in the background worker should be smaller
  (~11,000 podcast episodes, ~1,966 person lookups, one `title.basics` parse).
- The number of runs that allocate at all should be much lower.
- Whether settled PSS at 3, 8 and 24 hours is lower is an open question, and
  the aging capture is still the only thing that answers it.
