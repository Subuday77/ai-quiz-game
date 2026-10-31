# Quiz — cross-language fact and tag duplicate protection

Branch: feature/quiz-fact-dedup-tags (based on feature/quiz-ui-localization).
The feature is OFF until migration + historical backfill are completed.

## Agreed invariants

- One user history across ALL languages and rounds.
- A previously displayed fact is NEVER returned to the same user again,
  regardless of wording, answer order, difficulty, or whether it was answered.
- Different facts about one entity ARE allowed.
- Python controls deterministic rejection; the critic cannot override it.
- Retries exhausted -> a safe error, never a duplicated question.

## Tagged question and reviewer pipeline

The legacy question_v1 generator and v1/v2/v3 critic contracts are unchanged.
With tagging enabled, the server uses a separate tagged_question_v2 contract:
five normal question fields plus tags (entity, concept, keyword) and fact
(subject, relation, object), all canonical lower-case English kebab-case.

1. Load all previously displayed user facts from PostgreSQL. An untagged
   historical question causes a fail-closed 503 response.
2. Send up to 35 RECENT facts from the current topic as an exclusion list in
   the generator prompt. Python still checks against ALL user history.
3. Pydantic validates the tagged JSON. Python computes the canonical fact
   signature from subject + relation; object is intentionally excluded so an
   altered answer cannot evade detection.
4. Exact fact signature -> hard reject. Otherwise score each prior question
   individually and take the maximum: same entity 20, entity+relation 25,
   any shared concept 20, entity+answer 20, nearly identical wording 35.
   Same broad category adds zero. Default reject threshold: 70.
5. Remaining candidates are checked by ONE mandatory critic v4 call,
   including ordinary v3 quality diagnostics and tag/fact correctness,
   semantic duplicate detection, and matching prior question if any. This
   REPLACES the older critic call, rather than adding a second one when
   quality review was already enabled. Tag checks are mandatory even if
   AI_QUALITY_MODE=off. Ordinary editorial enforcement continues to depend on
   AI_QUALITY_MODE. Python hard rejects cannot be overruled by the critic.
6. Rejected candidates use the existing generation retry/budget policy.
   At commit, a PostgreSQL user-row lock serializes all concurrent rounds
   for that user, followed by a fresh full-history strict recheck.
7. On acceptance, question, fact signature, global tags, question-tag links,
   and user attempt commit together.
8. Only the established PublicQuestion fields reach Angular. No fact,
   correct answer, tag, or personal history leaks to the browser.

### Limitations to evaluate in smoke tests

English canonical labels can still vary for synonyms and entity aliases.
The critic checks that the metadata is meaningful, but AI semantics are not
mathematically perfect. For time-dependent facts, the relation must name the
exact tested property, including the reference year or other essential
context. Distinct facts about the same person must remain available.

## Activation (from backend/ in PyCharm)

1. Pull this branch. Keep AI_TAG_DEDUP_ENABLED=false initially.
2. Apply the additive migration:

    python -m alembic -c alembic.ini upgrade head

3. Count old questions at zero API cost and with no DB writes:

    python -m tagging.backfill --dry-run

4. Process an intentionally small, paid batch:

    python -m tagging.backfill --apply --limit 20

   Each question allows at most 2 validation attempts by default. Repeat the
   dry-run and controlled batches until none remain. Use --retries 1 or
   --pause 1 if necessary. Backfill is resumable and never starts on server
   boot. Existing player/game data is preserved.

5. Review representative fact metadata in PostgreSQL, especially for
   questions in multiple languages. Check canonical subjects and relation
   specificity. Verify that no old displayed question lacks a fact row.
6. ONLY after migration and historical backfill, set in root .env:

    AI_TAG_DEDUP_ENABLED=true
    QUIZ_TAG_DUPLICATE_SCORE_THRESHOLD=70

   Restart FastAPI. Because the tagged critic is mandatory, the feature
   costs an extra critic request when AI_QUALITY_MODE=off. The existing
   AI_GENERATION_MAX_ATTEMPTS (default 3) and
   AI_GENERATION_BUDGET_SECONDS (default 30) remain active. Consider increasing
   the generation budget to 60s for testing and aligning frontend timeouts.

Rollback: AI_TAG_DEDUP_ENABLED=false and restart backend. Existing tags remain,
no Alembic downgrade necessary. No frontend changes needed.

## SQL checks

    SELECT count(*) FROM questions;
    SELECT count(*) FROM question_facts;
    SELECT count(*) FROM tags;
    SELECT count(*) FROM question_tags;

    SELECT q.language, q.text, f.subject, f.relation, f.object, f.fact_key
    FROM questions q JOIN question_facts f ON f.question_id = q.id
    ORDER BY q.created_at DESC LIMIT 20;

    SELECT count(DISTINCT q.id)
    FROM question_attempts qa
    JOIN questions q ON q.id = qa.question_id
    LEFT JOIN question_facts f ON f.question_id = q.id
    WHERE f.question_id IS NULL;

## Tests

From backend/:

    python -m unittest discover -s tests -v

The offline unit tests cover stable JSON contracts, Python strict duplicate
scores, per-player all-language history, different-fact acceptance, metadata
atomicity, missing-history fail-closed, and mandatory critic behavior.
Before deployment, test PostgreSQL migrations and concurrent requests against
real PostgreSQL. Nothing is silently deployed or backfilled.
