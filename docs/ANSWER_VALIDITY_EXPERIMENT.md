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
`test_openai_answer_check.py` now tests 22 editor-labeled (provisional) cases:
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

### 2 Nov 2026 — replay actual game incidents

Eight additional frozen cases from the 31 Oct–1 Nov playtest cover:

- `jam_only_number_one`: "the ONLY #1" false premise; all four offered songs
  topped the UK singles chart. Expected `invalid_premise` (or flag as
  `multiple_correct` if the model interprets the options before the premise).
- `jam_four_number_ones`: same four songs in a deliberately plural question,
  expected `multiple_correct`, all A–D supported.
- `beatles_1967_drums` and `kate_bush_2022_debut`: incorrect chronology /
  "first" premises. Expected `invalid_premise`.
- `ginkgo_motile_sperm`, `fenrir_odin`, `riemann_trivial_zeros`:
  factually sound high-level questions, expected exactly one correct answer.
- `vice_city_title_leak`: a factual single-correct question but an obvious
  shortcut from the English game title to the Russian answer. This
  independent factual examiner does NOT check leakage. The conservative
  literal filter may also miss this cross-script translation: a
  `MISSED KNOWN DEFECT` for it does not mean the four-option factual
  checker made a factual error. The tagged v4 critic previously caught
  this as `answer_leak`; in `enforce` it would reject it.

The source labels are **editorial expectations**, not facts secretly sent to
the model or automated claims of current model accuracy. The script can now
select a small, inexpensive subset:

~~~powershell
# In PyCharm terminal / PowerShell from the repository ROOT.
python -m unittest discover -s tests -v
cd backend
python -m unittest discover -s tests -v
cd ..

# No API calls; inspect case names:
python test_openai_answer_check.py --list

# FOUR real AI calls; pay attention to A-D yes/no/uncertain and premise:
python test_openai_answer_check.py --case jam_only_number_one --case jam_four_number_ones --case ginkgo_motile_sperm --case vice_city_title_leak

# Optional: repeat the hardest case three times (3 billable calls):
python test_openai_answer_check.py --case jam_only_number_one --repeats 3
~~~

`--case` can be repeated to choose multiple IDs; `--repeats` is bounded
to 1..5. No case labels, generator keys, difficulty or expected answer counts
are passed to the model. The per-option statuses and explanations are written
to the output JSONL/CSV (plus concise status summaries to the terminal).
The preview is **experimental only**: it changes neither the actual
tagged critic nor `AI_QUALITY_MODE=audit/enforce`, and costs no AI calls
during ordinary gameplay.

### 2 Nov 2026 — positive regression from the 31 Oct full game log

The 31 October playthrough contained 23 accepted candidates. We selected
**12 factually sound, single-answer examples**, preserving the exact
question text, order of A–D and generator key from their DEBUG records.
The goal is to measure the independent reviewer's **false rejections**,
not to teach it to reject more questions.

The `playthrough-positive` group covers lava, Mayakovsky's poem,
tape-measure hook slack, the 1924 Winter Olympics (Chamonix),
lassi, egg-laying echidna, Sudoku, Guatemala's two coasts,
Sagrada Família (Barcelona), the 1938 Munich Agreement,
the unarticulated hyoid bone and cannoli's ricotta filling.
These include factual questions that the in-game critic rated
`too_easy` (Chamonix, Sagrada Família and cannoli): such subjective
difficulty should **not** become a factual failure.

Run from the **repository root** in the Python virtual environment
(PyCharm terminal or PowerShell):

~~~powershell
# Offline tests; no AI calls:
python -m unittest discover -s tests -v

# See every case name without any AI calls:
python test_openai_answer_check.py --list

# First run just 4 selected GOOD cases (4 billable calls):
python test_openai_answer_check.py --case good_20261031_tape_measure_hook --case good_20261031_guatemala_two_coasts --case good_20261031_hyoid_unarticulated --case good_20261031_cannoli_ricotta

# Then, if desired, all 12 positives (12 billable calls):
python test_openai_answer_check.py --group playthrough-positive
~~~

`--group` cannot be combined with `--case` or `--limit`. The
`--repeats N` option also works with the group and multiplies cost;
default is one call per selected case. Results go to timestamped
`answer_check_*.jsonl` and `.csv` in the root. Look for
`FALSE REJECTION`, `observed=uncertain` and cases incorrectly
classified as `no_correct` / `multiple_correct`.

The 12 questions are provisional **positive editorial labels** based
on the historical game transcript, not evidence-backed source
citations for every fact and not proof the model will agree. Offline
mock tests establish that the evaluation and scoring code WOULD accept
a correct four-option review, not that the model produces one.
Do not interpret a slow or incomplete model response as a false factual
rejection. No production API changes, new model calls in gameplay,
database migration or `audit/enforce` policy changes are involved.

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
