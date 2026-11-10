# Community question votes and source priority

Feature branch: `feature/question-votes-pool-first-20261110`.

## Runtime policy

- During an **unanswered active question**, authenticated players may press 👍 or
  👎. The same button toggles the vote back to neutral. Opposite button changes
  the vote atomically (e.g. +1 -> -1 changes net by -2).
- The server stores **one vote per (question, user)** in `question_votes`.
  `questions.vote_score` is the row-locked aggregate, persisted in PostgreSQL.
- The browser receives **only `my_vote`**, not likes, dislikes, net score, or
  whether a community review is pending. Votes do not affect stars, hints,
  results or the answer key.
- At or below `QUIZ_COMMUNITY_VOTE_THRESHOLD` (default **-12**), a unique
  `question_vote_reviews` item is queued. Subsequent changes to the rating
  cannot create duplicate reviews.
- Low rating is **not evidence of factual error**: independent Caddy
  (`QUIZ_COMPLAINT_CADDY_MODEL_ID`) and Judge
  (`QUIZ_COMPLAINT_JUDGE_MODEL_ID`) examine it. The Judge model must be
  different from the question generator. Confirmation rejects the question
  permanently; rejection leaves it approved; inconclusive decisions are
  preserved for operator review and do **not** automatically block.
- Paid player appeal workflows are independent: no synthetic appeals, no
  deposits, no compensatory star transfers. This is *not* an overturn of
  previous Judge rulings. A pending community review temporarily excludes the
  question from the shared pool; a validated defect prevents reuse entirely.
- Worker uses the existing 60s in-FastAPI maintenance scan and a durable
  PostgreSQL queue. On model/DB failure it retries on the next day; after
  seven days it records `technical_unresolved` rather than silently banning
  the question. The initial trigger also schedules immediate best-effort
  background processing.
- Administrative/operator UI for community review history is not included in
  this phase; results are audit-logged in `question_reviews` and
  `question_vote_reviews`.

## Source order

Set `QUIZ_QUESTION_SOURCE_ORDER` in the backend environment:

| Value | Lookup |
| --- | --- |
| `generate_first` (default) | Existing behavior: ask AI, then use verified pool when generation fails |
| `pool_first` | First find reusable verified questions; if none, ask AI |

Both paths require exact **language, category and difficulty**, approved
status, independent v5 acceptance, complete structured tags/facts, and
**per-user semantic and tag deduplication**. Q16 still needs the stricter
Terra arbitration evidence; an old bonus approval is never enough.

Eligible shared questions are ordered by their net rating (descending), with
random ordering for ties; no rating influences whether a question is approved.
Neither the source order nor the net rating is exposed to the browser. A
question currently awaiting low-score review is not offered from the pool.

`QUIZ_QUESTION_POOL_SCAN_LIMIT` still caps candidate checks. In
`pool_first`, the explicit policy tries the pool even if the emergency
`QUIZ_QUESTION_POOL_FALLBACK_ENABLED` flag is false. In
`generate_first`, the emergency flag retains its old meaning.

## Deployment

1. Run `python -m alembic upgrade head` from `backend` **before** restarting
   FastAPI on an existing PostgreSQL database. Migration
   `0013_question_votes_pool_priority` adds the tables, index and net score.
2. Configure `QUIZ_QUESTION_SOURCE_ORDER=generate_first` initially.
3. Configure `QUIZ_COMMUNITY_VOTE_THRESHOLD=-12` (must be negative).
4. Ensure Caddy, independent Judge, and `OPENAI_API_KEY` are configured,
   just as for appeals. Do not run multiple scheduler instances on separate
   DBs.
5. Review net-score logs and moderation results before enabling pool-first in
   production. Dislike storms can incur review cost; one ballot per account is
   an abuse throttle, not Sybil protection.

## Regression

```powershell
python -m unittest tests.test_question_votes tests.test_question_pool -v
python -m unittest discover -s tests -v
```

GitHub Actions also runs Angular tests and production build plus PostgreSQL
migrations. No external AI calls are necessary in the offline suite.

Known limits: Subjective votes may be biased, malicious users may create
multiple accounts, and the Judge has no trusted live-web fact checker. A
threshold crossing requests inspection but does not itself prove an error.
