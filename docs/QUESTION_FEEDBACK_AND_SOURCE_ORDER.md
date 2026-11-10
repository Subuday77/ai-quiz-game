# Question rating and DB First — implementation contract (10 Nov 2026)

Based on the agreed Russian specification `ai_quiz_question_rating_db_first_spec_2026-11-10.md`.
Premium is intentionally not enabled in this release.

## Provisional client votes

- 👍, 👎 are shown **only on the unanswered question card**. Counts and
  accumulated rating never appear to players.
- Buttons implement `none -> like -> none`, `none -> dislike -> none`,
  `like <-> dislike`.
- All clicks are **local-only drafts** in browser storage, scoped by account ID,
  game ID and question ID. No vote is sent to the server until the user submits
  their answer. Refresh/Resume may recover the local choice.
- `POST /api/v2/games/{run_id}/answer` optionally includes
  `vote: -1|0|1`; an accepted answer and final vote are persisted together
  in one transaction, then the draft is deleted from local storage. If answer
  submission fails, the draft remains. No post-answer changes are possible.
- `question_votes` enforces one **immutable lifetime** ballot for each
  `(question_id,user_id)`. Re-answering, multiple tabs, retries, replay and
  reissuing after any acquittal cannot add another rating contribution.
- The answer response contains no aggregate score. Votes never cost stars,
  affect hints, pay compensation, or reveal the correct answer.

## Historical and working reputation

- `questions.vote_score` is the **historical net**, lifetime sum of all
  finalized votes, never reset.
- `questions.working_vote_score` is the **working net**, used in DB First
  probability and the community review threshold; updated ±1 only for new
  final ballots, reset after Judge acquittals.
- `questions.community_referrals` counts threshold crossings.
  `question_vote_reviews` stores immutable **numbered** referrals
  `(question_id,referral_number)` and their outcomes.
- At or below `QUIZ_QUESTION_RATING_REVIEW_THRESHOLD=-15`, the answer
  transaction immediately quarantines the question and queues one referral.
  It cannot be issued again until Judge acquittal.
- First Judge acquittal: restore approved status and reset working net to
  `-5`. Second acquittal: reset working net to `-10`.
- Any Judge finding of a real defect: permanent ban.
- **Third** threshold crossing: automatic permanent ban, **no model call**.
- Historical score, ballots and all prior verdicts survive every reset and ban.
  Technical failure/inconclusive review leaves the question quarantined,
  not restored by accident. A player appeal cannot release a community hold.
- Models for community review reuse Caddy and independent Judge. The Judge
  model ID must differ from the original question generator ID; no automatic
  star/appeal ledger changes. Existing FastAPI maintenance scans durable
  review queue; transient errors retry, 7-day expiry is recorded.
- Rejected Judge referral means the question was **acquitted**, not banned;
  `upheld` means the suspected error was confirmed and the question banned.

## DB First source selection

`QUIZ_DB_FIRST_GENERATION=false` is **Model First**; original ordering
is preserved: AI generation, emergency approved pool on recoverable failure.

When `QUIZ_DB_FIRST_GENERATION=true` and tagged gameplay is enabled,
the **main questions Q1–Q15** follow this priority:

1. Search the assigned category's verified approved pool.
2. If no candidate is admitted, search **each other not-yet-answered category**
   in the saved per-game allowlist (never any category excluded before the
   game for silver). The search order is deterministic for the game; it neither
   spends stars nor records a category change/hint.
3. Only after every eligible category is exhausted, call the existing AI
   generator, keeping its retry/quality/fallback behavior.

A played/answered category is consumed; a skipped or replaced question's
category can return later, according to existing `available_categories`
rules. The existing emergency reset when all allowed categories are consumed
is preserved (normally not reached within Q1–Q15). The offered question
must match the current difficulty and player language regardless of category.

**Paid manual selection (2 silver):** an explicit category choice overrides
the cross-category search. First try only that chosen category's eligible
approved pool; if none is admitted, generate within **that same category**.
Do not auto-substitute a different topic. A successful pooled replacement
atomically supersedes the old attempt and charges silver; an unsuccessful
lookup or generation charges nothing.

**Bonus Q16** retains its separate persisted topic rotation and strict Terra
v2 reuse requirements; the new all-category search does not apply.

Existing strict v5 quality reviews, complete fact/tag metadata, cross-language
per-user deduplication and bonus Q16 Terra v2 evidence are still mandatory.
Exclude quarantined, banned and pending-review questions.

Each fully qualified candidate undergoes ONE independent admission draw:

```
p(admit | r) = 1 / (1 + exp(-(r-center)/scale))
```

where `r` is the **working** score. Defaults:
`QUIZ_DB_FIRST_RATING_CENTER=0`,
`QUIZ_DB_FIRST_RATING_SCALE=10`.
At `r=-10, -5, 0, +5, +10`, admission is approximately
`26.9%, 37.8%, 50%, 62.2%, 73.1%` respectively. This is a filter,
**not** the probability of final selection.

Choose **uniformly at random** among admitted verified candidates. If no
candidate passes, call the normal AI generator once along the existing retry,
quality and emergency fallback path. Never reroll the filter indefinitely.
Weakly negative ratings (-1 to -14) are not excluded from the sample outright.
No new model call is necessary after a successful DB hit.

The pool scan is capped by `QUIZ_QUESTION_POOL_SCAN_LIMIT` to limit
latency. Future Premium may shift `center` right, but Premium logic,
subscription pricing and premium toggles are out of scope.

## Configuration

```dotenv
QUIZ_DB_FIRST_GENERATION=false
QUIZ_QUESTION_RATING_REVIEW_THRESHOLD=-15
QUIZ_QUESTION_FIRST_ACQUITTAL_SCORE=-5
QUIZ_QUESTION_SECOND_ACQUITTAL_SCORE=-10
QUIZ_QUESTION_MAX_REFERRALS=3
QUIZ_DB_FIRST_RATING_CENTER=0
QUIZ_DB_FIRST_RATING_SCALE=10
```

## Included shadow morphology regression

Merged `fix/shadow-prefixed-morphology-20261110` into this feature branch
through PR #25. The lexical tool now recognizes the uniquely prefixed Russian
root case (e.g. `коленн-` inside `надколенник`) in addition to existing long
shared-prefix detection. The signal `unique_prefixed_root_shortcut` remains
**shadow-only**: it is diagnostic evidence, not a new automatic question
rejection rule. Regression tests in
`backend/tests/test_quality_skill_shadow.py` verify the prefix case, short
roots and nonunique options. The unified branch's CI must check these tests
together with DB-first and vote regressions before local QA.

## Deployment and QA

1. Review the Draft PR and CI; do **not** deploy to live data until successful.
2. Apply Alembic migration `0013_votes_pool` with
   `python -m alembic upgrade head` on the correct backend environment.
3. Keep DB First off for first smoke test; enable true only after regression.
4. Test draft click/undo/change/F5/logout, successful answer commit, same-account
   retry and two-tab race, HTTP answer validation and immutable ballot.
5. Drive -15 -> acquit -> -5 -> -15 -> acquit -> -10 -> -15 -> automatic
   permanent ban. Check original historical score, queued review count, star
   ledger and zero Judge calls for the final strike.
6. Validate DB First sampling with seeded draws, exact schema, empty-pool AI
   fallback, Q16 refusal without modern Terra approval and no answer leakage.
7. Observe `db_pool_candidates`, `db_admitted`, `db_first_hit`,
   `ai_fallback`, `rating_quarantine`, `judge_referral`, `judge_ban`.

Known limits: malicious accounts can vote strategically; one vote per account
does not prevent coordinated abuse. Judge does not have trusted live-web fact
checking. Dislikes trigger review, not proof. Interrupted in-flight reviews
are retried based on durable leases; operator intervention is required if
review ultimately becomes inconclusive or technically unresolved.
