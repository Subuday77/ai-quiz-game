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
4. Exact fact signature OR identical normalized question text -> hard reject
   (100 points), even when the generator reverses subject and object or
   changes the answer order. Exact text normalization handles typography,
   quote styles, case and whitespace, but preserves numerical operators.
   Otherwise score against each prior question: shared entity 20, same
   fact subject+relation 25, shared concept 20, same fact subject+answer 20,
   very similar wording 35. Different works by the same author must not gain
   relation/answer points merely for sharing that author. Same broad
   category adds zero. Default threshold: 70.
5. Remaining candidates are checked by ONE mandatory critic v5 call,
   including ordinary v3 quality diagnostics and tag/fact correctness,
   semantic duplicate detection, and matching prior question if any. This
   REPLACES the older critic call, rather than adding a second one when
   quality review was already enabled. Tag checks are mandatory even if
   AI_QUALITY_MODE=off. Ordinary editorial enforcement continues to depend on
   AI_QUALITY_MODE. Critic-only findings (facts/options/leakage/language,
   including answer_leak) are logged but NOT rejected in audit/off; they
   are enforceable only in enforce mode. The critic is instructed to test
   A-D independently,
   especially when a claim says "only", "first", "all" or "never". Python
   hard rejects cannot be overruled by the critic.
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

## Duplicate-exhaustion player recovery

When EVERY failed candidate in the bounded tagged generation attempt is
rejected as a duplicate (either Python's deterministic similarity check or
the tagged critic's semantic-duplicate verdict), the backend returns:

    HTTP 409
    {"detail":{"code":"duplicate_exhausted"}}

Any mixed validation/provider/ordinary-quality failure retains the existing
generic HTTP error; it is not mislabeled as a duplicate. Existing answer
conflicts and concurrent-save conflicts are also distinct.

Angular detects the structured code and shows a localized explanation with
**Try again** and **Back to menu** buttons. These labels and the message are
keys in backend/localization/en.json and are translated by the UI localizer.
Try again requests a new candidate for the SAME game and current level.
Back to menu navigates to setup but **preserves** the existing game ID,
progress, and saved resume state; the user can resume from that same round.
No accepted question, answer, game run, or star award is deleted.

The generic Retry question button remains for all other errors and is hidden
while the specialized panel is present. UI regression tests cover recovery,
translation keys, no extra game POST, and unaffected error handling.


## Critic scoring and database audit

The tagged v5 reviewer retains the full v3 editorial assessment:

- distractors penalty: 0..3;
- knowledge penalty: 0..2;
- suitability penalty: 0..2;
- wording penalty: 0..1.

The sum is a PENALTY (0 = no penalty assigned; higher = worse).
These fields, the reported difficulty (appropriate/too_easy/too_hard/
uncertain), category checks, issue codes, threshold previews and critical
recommendations are emitted in the structured tagged_review_score log event.
The deterministic Python duplicate scoring is logged separately as
tag_similarity_score with score, rejection threshold, reason and rejected flag.

The accepted tagged candidate carries a PRIVATE reviewer payload to the
same PostgreSQL transaction that creates the question, fact, tags and attempt.
The existing question_reviews table stores checker_type=ai_tagged_critic,
contract_version=tagged_review_v5 (older rows remain v4), decision, issue codes, provider/model,
review duration, and details with:

- verdict: complete typed reviewer output, including rationale;
- assessment: computed total and four individual penalties;
- option_diagnostics: Python-derived premise, A–D yes/no/uncertain statuses,
  supported/uncertain choices, outcome and consistency flags (nonblocking);
- python_duplicate_check: similarity score, threshold, reason, and matching
  prior question ID (private developer diagnostics only);
- general_mode, requested_difficulty, category.

The rationale, correct answer and question body are NOT included in ordinary
content-free diagnostic logs. The full verdict is safely inspectable by
developers in PostgreSQL. This does not change what is sent to Angular.

IMPORTANT: review rows start appearing only for NEW questions accepted after
this revision is deployed. Prior approved questions and past historical
tag-extraction backfills have no saved reviewer output; no verdict is invented.
Rejected candidates that were never inserted as questions have no
question_reviews row because that table has a mandatory question_id foreign
key. Their score breakdown is still logged for debugging.

With AI_QUALITY_MODE=audit, all critic-only quality judgments
(facts/options/language/leakage, issue codes, critical status, difficulty,
and editorial penalties) are diagnostic, even when critical=regenerate.
They do NOT reject an otherwise valid tagged question. The established
metadata and history guards remain mandatory in all modes: tags_valid and
fact_valid must pass, and semantic_duplicate must be false. In enforce,
critical quality findings reject; in tagged v5 a weighted editorial score
of at least 3.25 also triggers regeneration.
For non-tagged v3 the numeric thresholds are still diagnostic only.

Developer SQL:

    SELECT q.id, q.requested_difficulty, q.text,
           r.created_at, r.decision,
           r.details->'verdict'->>'difficulty' AS critic_difficulty,
           r.details->'assessment'->>'total' AS penalty_total,
           r.details->'assessment'->'penalties' AS penalties,
           r.details->'python_duplicate_check' AS duplicate_similarity,
           r.details->'verdict'->>'rationale' AS rationale
    FROM public.questions q
    LEFT JOIN public.question_reviews r ON r.question_id = q.id
    WHERE q.prompt_version = 'tagged-v2'
    ORDER BY q.created_at DESC
    LIMIT 50;

## Weighted difficulty in tagged reviewer (v4)

The tagged v5 reviewer extends the strict tagged JSON contract with
diagnostic option evidence. The existing numeric fields remain unchanged:
`knowledge_penalty` stays an integer 0..2 and `difficulty` stays an enum.
The **server-side TAGGED score** is unchanged; legacy/v3 scoring is untouched.

Difficulty is subjective, so its contribution is deliberately tiny:
- No difficulty mismatch and raw knowledge penalty 0: **0**.
- `too_easy`, `too_hard`, `difficulty_mismatch`, or raw knowledge penalty 1:
  **0.25**.
- Raw knowledge penalty 2: **0.5** maximum.

This replaces, rather than adds to, the old 0..2-point knowledge contribution.
The other categories retain their scoring: distractors 0..3, suitability
0..2, wording 0..1. `assessment.penalties.knowledge` reports the weighted
quarter-point value, `assessment.raw_penalties.knowledge` the model's
original 0..2 answer. Scores are computed in Python, not by the LLM.

An editorial score of **3.25 or more** is sufficient to regenerate a
tagged question **only when `AI_QUALITY_MODE=enforce`**. For example, 3
other penalty points + 0.25 difficulty = 3.25, whereas 3 other points by
themselves are still accepted unless a separate critical fault applies.
Difficulty alone cannot exceed 0.5 and can NEVER cause rejection.

With `AI_QUALITY_MODE=audit`, all critic-only quality results are
diagnostic, including factual/options flags, leakage, critical recommendation
and threshold preview. Tagged fact/tag METADATA correctness
(`tags_valid`/`fact_valid`) and semantic duplicates still reject in all
modes. In `enforce`, critical quality failures reject independently of
numerical penalties. The AI tagged-review schema is now **v5**, but no
database migration is required; existing v4 reviewer rows remain readable.

## DEBUG: all generated candidate traces

Default: `AI_GENERATION_DEBUG=false`. Set
`AI_GENERATION_DEBUG=true` in the backend's loaded `.env` and restart FastAPI
only when diagnosing AI-generated questions.

The normal `quiz.diagnostics` logger stays INFO-level and remains
content-free. A separate opt-in DEBUG-level diagnostic logger emits one
single-line UTF-8 JSON record for each accepted or rejected candidate. **Every record
starts literally with `DEBUG: `**, e.g.:

    DEBUG: {"event":"question_candidate_rejected","outcome":"rejected","request_id":"...","attempt":1,"rejection_reason":"quality_rejected","question":"...","answers":{"A":"...","B":"...","C":"...","D":"..."},"correct_answer":"B","tags":{"entity":["..."],"concept":["..."],"keyword":[]},"fact":{"subject":"...","relation":"...","object":"..."},"critic":{"verdict":{"rationale":"..."},"assessment":{"total":3}}}
    DEBUG: {"event":"question_candidate_accepted","outcome":"accepted","request_id":"...","attempt":2,"rejection_reason":null,"question":"...","answers":{"A":"...","B":"...","C":"...","D":"..."},"correct_answer":"B","tags":{"entity":["..."],"concept":["..."],"keyword":[]},"fact":{"subject":"...","relation":"...","object":"..."},"critic":{"verdict":{"rationale":"..."},"assessment":{"total":0}}}

Every accepted/rejected record uses the same payload format. An accepted
candidate has passed Python/model checks but is NOT necessarily committed
to PostgreSQL yet. The actual records include the timestamp, fact_key (when available),
Python duplication assessment (including matched_question_id when known),
error type, reviewer contract version, full reviewer verdict and structured
A–D option diagnostics when the critic completed a valid review. Candidate content and the correct
answer are absent from INFO logs. A malformed JSON completion produces
`DEBUG: {"event":"question_candidate_invalid_json",...}` with up to 3,000
characters of unparseable model output.

For validation failures, the candidate is recorded if the provider
successfully returned a JSON object. Model output that never arrived has
no candidate text to log. Question stems are limited to 2,500 characters,
individual answer choices to 500, and slug fields to 140. No prompts,
prior-question histories, passwords, API keys, request cookies, or arbitrary
extra model fields are logged. Reviewer rationales may quote candidate
text: treat DEBUG logs as sensitive, use local/private log sinks, and disable
the flag when troubleshooting is over. In production, restrict access and
retention and keep this off by default.

There are no new tables, migrations or database writes for this feature. Existing
`question_reviews` rows for accepted questions remain unaffected. Rejected
candidates are never inserted into `questions` or `question_reviews`
through this debug feature. No candidate DEBUG logs are persisted by the application itself;
any external log capture remains the operator's responsibility.

Tests: `python -m unittest discover -s tests -v` from `backend/`.

## 2026-11-01 deterministic and factual review fixes

- Python: exact normalized question text is an unconditional duplicate even
  when fact triples vary. Subject-specific relation/answer weights prevent
  false 85/70 collisions for different works sharing one author.
- Python: the matching prior question ID is available only in private
  `_duplicate_assessment` / opt-in DEBUG and persisted reviewer details;
  INFO duplicate events remain content-free and ID-free.
- Critic: the final v4 prompt explicitly tests A, B, C, D against the literal
  stem, including exclusivity/quantifier words; it must not assume the
  generator's selected answer is uniquely correct.
- Policy: `facts=fail` or `options=fail` are recorded in audit/off,
  not hard-rejected. In enforce, they trigger regeneration through
  `critical=regenerate`. The difficulty weight (0–0.5) and score
  threshold (3.25) remain unchanged and mode-gated.
- **Important limitation:** the model can still wrongly report all four
  options as checked and valid, as in the historical The Jam case. A stronger
  prompt and consistent rejection handling do NOT guarantee factual truth;
  external fact verification remains a separate potential next phase.
- Tests cover the literal 1984 repeat, the Mona Lisa/Last Supper distinction,
  numeric notation safety and the audit-vs-enforce policy for facts/options.
  Run tests locally in PyCharm; no database migration was introduced.

## 2026-11-01: Vice City regression — audit vs enforce

Real level-4 question: "Как называется вымышленный город, в котором
происходит действие игры Grand Theft Auto: Vice City?" The options were:
A = Либерти-Сити, B = Вайс-Сити, C = Сан-Андреас, D = Лос-Сантос.
The critic reported `leakage=fail`, `answer_leak`,
`critical=regenerate`, `score=1.25`. This is a GOOD detection.

**Intended policy:** tagged `AI_QUALITY_MODE=audit` logs and accepts this
candidate, allowing measurement of reviewer quality. `enforce` regenerates
because `critical=regenerate`, independent of score. A test stores the full
candidate and checks both modes. Never introduce another mandatory quality
rejection from a log without first checking how enforce would behave.
The mandatory `tags_valid`/`fact_valid`/semantic-duplicate guards are
intentionally separate, because they protect database dedup metadata.

## 2026-11-02: tagged critic v5 — A–D evidence in one call

The same mandatory tagged reviewer call now returns two additional strict
fields: `premise` (`valid|invalid|uncertain`) and `option_assessments` with
**exactly A, B, C, D**, each containing `status` (`yes|no|uncertain`)
and a short, specific `rationale`. All previous reviewer fields remain.
`OutputContract.TAGGED_REVIEW` now uses schema `quiz_tagged_review_v5`.
The private persisted reviewer row has `contract_version=tagged_review_v5`.
Older rows remain unchanged. No new PostgreSQL column or table is needed.

One content-free INFO event is added per successfully parsed reviewer result:

    {"event":"tagged_option_audit","premise":"valid","statuses":{"A":"no","B":"yes","C":"no","D":"no"},"supported_choices":["B"],"uncertain_choices":[],"outcome":"single_correct","consistency_flags":[],"mode":"audit",...}

`outcome` can be `single_correct`, `multiple_correct`,
`no_correct`, `invalid_premise` or `uncertain`.
`consistency_flags` highlight disagreements between A–D evidence and
legacy fields, such as `invalid_premise_but_facts_pass`,
`nonunique_options_but_options_pass`,
`independent_answer_disagrees_with_options` and
`generator_key_disagrees_with_options`.
The accepted `question_reviews.details.verdict` and the private opt-in
`DEBUG: question_candidate_accepted/rejected` records retain the full
per-option rationales.

**No new rejection gates.** The new premise, A–D results and consistency
flags are OBSERVATION ONLY. `AI_QUALITY_MODE=audit` still accepts
critic-only quality findings, while `enforce` uses exactly the previous
`critical` and score >= 3.25 rules. The existing mandatory
`tags_valid` / `fact_valid` / semantic-duplicate guards still apply in
both modes. The increased tagged-review output cap (1600 → 2400 tokens)
accommodates four concise reasons; this may increase latency/cost but is
still one critic call per candidate, not two.

### Recommended full local audit run

1. Update the feature branch and run backend offline regressions:

       git switch feature/quiz-fact-dedup-tags
       git pull
       cd backend
       python -m unittest discover -s tests -v

2. In the loaded **backend/.env**, set `AI_TAG_DEDUP_ENABLED=true`,
   `AI_QUALITY_MODE=audit` and `AI_GENERATION_DEBUG=true`. Keep the
   existing reviewer/provider/structured-output configuration. Restart FastAPI.
3. Play a full game with varied levels. Preserve the **complete** console
   log, including INFO `tagged_option_audit`, `tagged_review_score`, and
   the opt-in `DEBUG:` candidate records (accepted and rejected).
   Compare by `request_id` and `attempt`. Check false premise and
   multi-correct warnings, but also high-quality questions accepted
   without warnings. A failed or truncated reviewer response will still
   use the existing generation retry/error policy.
4. After collecting the private log, turn `AI_GENERATION_DEBUG=false`
   again. Never post credentials or private player data. Do not enable
   new enforce gates based on a single log.

If a valid reviewer reply cannot fit the schema/token/time budget, diagnose
completion status and review latency before altering scoring or rejection
policy. No cloud deployment or merge to master is part of this change.
