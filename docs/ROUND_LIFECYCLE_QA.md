# Saved rounds: lifecycle, Play Again and legacy cleanup

## Contract

- PostgreSQL is the authority for each authenticated user's active game and for
  each purchased helper. Browser storage is a cache, never an entitlement.
- Exactly **one** `main` or `bonus` game may exist per user. Revision
  `0008_single_active_round` creates a PostgreSQL partial UNIQUE index over
  `game_runs(user_id)` for those statuses.
- F5 / sign-in reads `GET /api/v2/games/active`. The saved game language,
  helper identity, consultation count and purchases are restored from that run.
- **Play Again** from a completed/lost result calls
  `POST /api/v2/games/{terminal_run_id}/replay`. It atomically creates **one
  new, empty setup** (no Q1, no AI call, no helper purchase); it is idempotent
  using a server-derived successor UUID. Any obsolete, *unpaid* unfinished
  runs for that user are marked `abandoned`; their questions and history remain.
- If any older unfinished run has a helper purchase or any debited star perk,
  Play Again returns HTTP 409 `saved_paid_round` and does **not** change that
  run or the star ledger. The UI presents the saved round with Resume and the
  existing confirmation before abandoning it to start a new one.
- Explicit **New Game** confirmation uses
  `POST /api/v2/games/{run_id}/replace`, which keeps old stars/history but
  does not refund user-abandoned paid perks. Normal `POST /api/v2/games`
  refuses to create over an already-active round.
- A lost/completed round is terminal; a late or duplicate answer cannot reopen
  it. Retries of the same Replay/Replace action do not produce extra rounds.
- `QUIZ_HELPER_DEBUG=true` reports `helper_active_round_lookup`,
  `helper_profile_purchased`, and consultation events. Helper profiles and
  correct keys are emitted **only to local backend logs**, never HTTP clients.

## Safe rollout for an existing local PostgreSQL database

1. **Back up the database** and stop the local FastAPI server before modifying
   the schema or cleaning old rounds.
2. Pull the branch and from `backend/` run
   `python -m alembic upgrade head` (revision `0008_single_active_round`).
   It keeps the newest active game per user and automatically archives only
   old duplicate games **without** any paid perks. If an older duplicate has a
   helper purchase or debit, the migration aborts transactionally with that
   game ID. Review it first; do **not** delete rows or blindly bypass the guard.
3. To archive **remaining old unpaid** active games (including the sole active
   one), preview with:
   `python -m persistence.cleanup_stale_rounds --username YOUR_USERNAME --older-than-days 2`
   The default is DRY RUN; only active games older than the specified age are
   inspected. Paid games are listed under `protected`.
4. After reviewing the IDs, run the same command with `--apply` to archive
   only `archivable` games. No questions, awards, purchases or wallet
   transactions are deleted, and no paid games are touched.
5. Restart the FastAPI backend in PyCharm, reload Angular and verify that
   `/api/v2/games/active` contains the expected run (or JSON `null`).

## Verification SQL

```sql
SELECT u.username, COUNT(*) AS open_games
FROM game_runs g JOIN users u ON u.id = g.user_id
WHERE g.status IN ('main', 'bonus')
GROUP BY u.username
HAVING COUNT(*) > 1;
```

The result must be **empty** after migration. To check purchases, use the
`platinum_helper_purchases` table and ledger reason
`star/platinum/helper-purchase` scoped by `run_id`. A restored paid helper
should keep its original purchase row and ID; there is no new debit on reload.

## QA regression

1. A player loses Q1, clicks Play Again, gets a fresh setup and no paid helper.
2. F5 restores the **new** empty setup; only Start issues Q1.
3. Repeating the same Replay HTTP request returns the same successor ID.
4. A previous paid game is never silently abandoned; Resume and explicit New
   preserve choice and warning.
5. User A's old round never appears for User B.
6. Legacy unpaid duplicates are archived without data deletion and the
   partial unique index prevents new duplicate active games.
