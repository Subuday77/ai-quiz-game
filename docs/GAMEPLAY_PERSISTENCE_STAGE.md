# DB gameplay persistence — stage 1 (work branch)

Branch: `feature/quiz-db-gameplay-persistence` (based on `feature/quiz-web-foundation`).

## Included

- `backend/persistence/gameplay.py`: transactional create/get/offer/answer
  functions, owned by an authenticated user's UUID.
- `backend/tests/test_gameplay_repository.py`: SQLite contract tests for
  reload, ownership, cached pending questions, replay rejection, milestones,
  missing topics and wrong difficulty.
- Existing Alembic revision `0001_initial` and current SQLAlchemy models are
  reused. **No migration changes.**
- The API and Angular are deliberately not switched over yet.

## Transaction and security boundaries

1. The caller authenticates the player and validates language.
2. The application generates and quality-checks a `QuizQuestion` before
   calling `offer_question()`, outside a row lock.
3. `offer_question()` uses PostgreSQL `SELECT ... FOR UPDATE` to lock that
   player's game round, reuse an existing unanswered question, or atomically
   save a question and attempt.
4. `record_answer()` locks the same row, verifies ownership and question ID,
   records the answer, updates phase/progress and inserts milestone stars in
   **one transaction**. Replays fail before modifications.
5. The correct key stays in `questions.correct_key`; no key is returned from
   the offer function. The answer function returns the key only for the
   existing post-answer feedback contract.
6. No reward currency is minted, and `round_score` remains zero until the
   gameplay score policy is decided. Statistics derive from attempts/awards.
7. `topics` must be initialized via `python -m persistence.seed_topics`.

## Local validation (PyCharm backend terminal)

```powershell
cd backend
python -m unittest discover -s tests -p test_gameplay_repository.py -v
python -m unittest discover -s tests -q
```

The new repository tests use SQLite as an offline test replacement.
They do not prove PostgreSQL concurrent-request behavior; run live
PostgreSQL integration tests before production route cutover.

## Next implementation increment

- Extract the existing bounded question-generation logic from
  `backend/api.py` without changing its retry/quality/leak behavior.
- Wire authenticated `/api/games` routes to this DB repository.
- Preserve cached pending questions across restart and limit simultaneous
  question creation through a PostgreSQL transactional check.
- Apply `validate_origin` to mutations; test cross-user access,
  concurrent answers, and privacy of the public question response.
- Update Angular to present login/registration before starting a game.
- Add live PostgreSQL concurrency tests and only then retire the legacy
  in-memory `sessions` dictionary.
