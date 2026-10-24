# Fact validity hardening — independent experiment (24 Oct 2026)

**Branch:** `feature/quiz-web-foundation`. **Generator:** `v1` remains the default.
**Critic:** `v3` remains audit-only and its score does not block publication.
No database schema, player-report endpoint, frontend or production master changes here.

## 1. Reviewer-independent literal leak filter

The backend validates a generated question with Pydantic, then calls
`local_leak_filter.check_local_leak` **before** the optional reviewer,
even if `AI_QUALITY_MODE=off` or `audit`.

`AI_LOCAL_LEAK_MODE` values:
- `enforce` (**default**): reject only an unambiguous *full answer phrase*
  repeated literally in the question stem. Retry with the existing shared
  budget and max 3 attempts. On exhaustion return HTTP 502; do not advance
  progress or consume the game's chosen category.
- `audit`: emit safe findings, do not block the question.
- `off`: skip this independent filter.

No key or question text is put into new diagnostic events. The filter tolerates
case, Unicode accents, punctuation and spacing. It does **not** attempt semantic
matching or Russian inflection; this intentionally avoids speculative
false positives. Long, quotable slogans remain valid if their words do not
literally spell the selected answer. The existing broader `lexical_findings`
in `quality_checker.py` remains separate and unchanged for v1/v2/v3 reviewer
diagnostics.

Examples caught: stem names **Эбла** and the exact keyed answer **Эбла**;
stem repeats **Миссис Дэллоуэй** and the keyed option is that same title.
Examples intentionally **not** caught: an iconic X-Files slogan, an
indirect link via etymology or a related word with different inflection.
This is a conservative gate, **not** a comprehensive leak-proof verifier.

## 2. Key-blind factual answer validity experiment

`backend/independent_answer_check.py` provides an independent four-option
contract. The input includes **only** language, stem and choices A/B/C/D.
It does not contain the generator-supplied answer key, requested difficulty,
expected answer count, critic output or test labels.

The experiment evaluates every answer option as `yes/no/uncertain` and
premise as `valid/invalid/uncertain`. Deterministic Python reduces the
assessment to `single_correct`, `multiple_correct`, `no_correct`,
`invalid_premise` or `uncertain`. If any option is uncertain, a
seemingly unique key cannot be confirmed. An LLM determination is still
**unverified**; it is not automatic publication approval or rejection.

The offline tests use mocked or synthetic review reports. The live script
`test_openai_answer_check.py` tests 14 editor-labeled (provisional) cases:
healthy questions, deliberate incorrect keys, questions with multiple
correct options, no correct option, direct textual leaks, and high-level
historical or cultural ambiguities. The borderline *Хованщина* case is
labeled `uncertain` and excluded from known-class accuracy metrics until
sourced adjudication; obscure examples are not eligible for training as-is.
The reporter computes a **hypothetical publication decision** separately from
the model's raw answer-count classification. The preview would block if:
the local literal gate finds a leak, the checker says anything other than
`single_correct` (including `uncertain`), or the uniquely supported answer
does not match the generator-supplied key. This preview is **not wired into
the live API**. The deliberately wrong-key Saturn example MUST count as a
rejected candidate even though the factual question has one correct answer.

The reporter prints missed defects and false rejections, and writes JSONL/CSV
to the project root. No test annotations are sent to the model.

Run from the **project root** with the local Python environment in PyCharm:

~~~powershell
git pull
python -m unittest discover -s tests -v
python test_openai_answer_check.py --limit 4
python test_openai_answer_check.py
~~~

Live testing requires `OPENAI_API_KEY` in the project-root `.env`, uses
`AI_QUALITY_MODEL_ID` (or `gpt-5.4-mini`) and may incur API charges.
One call per test case, with strict JSON Schema, `reasoning=low`, no
automatic retries. Logs/outputs are not persisted to the application's
database. Share the JSONL to evaluate **missed severe defects** and
**false rejection of sound questions** before any runtime integration.
The model cannot consult external sources through this experiment.

### First live result (24 Oct 2026)

The original 14-case run completed without model errors. With combined
local-literal + independent-option + key-agreement *offline preview*:

- 6/6 uncontested good fixtures would have been accepted.
- 6/7 known-defective fixtures would have been rejected.
- **Miss:** invented historical nickname `Длиннорукий` was rationalized as
  equivalent to the attested `Долгорукий`; the model accepted the false
  premise. This remains a priority regression and a case for sourced checking.
- **Under review:** `Хованщина` was accepted as uniquely Rimsky-Korsakov,
  despite performances using both Rimsky-Korsakov and Shostakovich versions
  during the 20th century. Conservatively excluded from the known-class
  ratio pending editorial adjudication. It would pass the preview.
- 9/9 `single_correct` class matches is **not** a publication-safety metric,
  because a single correct option can still conflict with the supplied
  answer key or be spelled out verbatim in the stem.

These are tiny, curated fixtures, **not** evidence of generalization or
an acceptable production false-negative rate. Continue in offline/audit
experiments; do NOT enable model-driven rejection yet. No need to rerun
the paid benchmark just to update score math.

## 3. Follow-up — persistent quality data and player reports

As specified by [issue #3](https://github.com/Subuday77/ai-quiz-game/issues/3)
and [issue #6](https://github.com/Subuday77/ai-quiz-game/issues/6), after
PostgreSQL/session migration create separate persistent records for:

1. **Immutable question snapshots**: generated text, four choices, designated
   key, requested level *separate from semantic topic tags*, locale, category,
   provider/model/prompt/schema versions, generation attempts, timestamp.
2. **Automated assessments**: local checks, independent-model reviews,
   unverified machine predictions and evidence/review versions. Preserve
   original predictions even if subsequently overturned.
3. **Delivery/response events**: display count, correctness, selected
   distractor, hints used, game-level and timestamps. Avoid identifying users
   unless necessary; retain rates and statistically meaningful sample sizes.
4. **Player reports**: user-reported reason (`incorrect_key`,
   `multiple_correct`, `answer_leak`, `ambiguous`, `bad_translation`,
   `difficulty_mismatch`, `other`), optional text, immutable question ID,
   question delivery ID, `pending/upheld/rejected/uncertain`, moderator
   adjudication and timestamps. A report is NOT a ground-truth label.
5. **Editorial outcomes**: separate human or evidence-backed verified labels,
   provenance and dates. Store accepted questions too to avoid training bias.

Use server-authoritative question references, idempotency keys and limits
for repeat reports. Preserve feedback ability **after submitting an answer**,
when the existing in-memory session clears the active question; do not rely on
the public question DTO containing `correct_answer`. Do not auto-restore
achievements without an explicit game policy. Build these features alongside
persistent PostgreSQL session history, not as an unprotected in-memory
endpoint pretending to be durable.

**Scoping rule from PRD:** tags identify semantic content for diversity.
A question's requested or empirically measured difficulty is NOT a
topic tag; the same tagged topic can appear at several nearby levels.

## 4. Gate for continuing

- Keep generator v1; do not switch prompts based on this experiment.
- Verify **all** old offline tests and new local false-positive regressions.
- Inspect the live benchmark error cases; never assert that JSON validity
  means factual validity.
- Only consider enabling model-based automatic blocking after a larger,
  adjudicated benchmark establishes acceptable false-positive and
  false-negative rates; use trusted source retrieval for obscure facts.
- Keep release decisions and player disputes separate from evaluator scores.
