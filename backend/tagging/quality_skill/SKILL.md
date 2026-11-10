# Question Quality Skill — shadow rollout

This is a **Python runtime component**, not an agent skill that automatically
executes from a Markdown file. The application calls
`tagging.quality_skill.engine.evaluate_shadow` after schema validation and
tag deduplication, before local leakage gates and the paid tagged reviewer.

## Contract and authority

- **Correctness tools:** `lexical_tool` (shared-root answer shortcuts),
  `taxonomy_tool` (option type/subtype relations), `fact_tool`
  (risky superlative, exclusive, chronology and compound award/year/rationale claims).
- **One decision owner:** the existing tagged reviewer is still the
  publication decision maker. Shadow findings **never** reject, change a
  question, modify critic scores or call an arbiter.
- **Arbitration plan:** `would_arbitrate` indicates that a future arbiter
  would be warranted; `arbiter_called` is always `false`. Only one
  combined request per candidate should ever be made if arbitration is
  subsequently implemented. "Unknown" is not a factual contradiction.
- **Safety:** no external calls, no source fabrications, no reader prompts
  or historical private user questions. Optional DEBUG includes generated
  candidate snippets and is unsuitable for public/shared logs.

## Curated short noun/adjective clues (2026-11-10)

Real English Q4 slipped through both primary reviewer and lexical shadow:
`Which substance turns blue litmus paper red because it is acidic?`
with the only `acid`-named option `Hydrochloric acid`. The existing
`QUIZ_QUALITY_LEXICAL_MIN_PREFIX=5` found no signal because `acid`
has four letters.

The lexical tool now recognizes **exact whole-token** curated pairs
`acid ↔ acidic`, `base ↔ basic`, and `magnet ↔ magnetic`.
The configured general minimum shared-root length is **unchanged**;
other short coincidences are not guessed. Only one matching option
is allowed to nominate a `unique_noun_adjective_shortcut`. Multiple
matching options suppress that signal, while an overlap in a wrong
option is recorded only as low-severity diagnostic evidence.
Findings remain shadow-only and never directly reject a question.
With optional `QUIZ_QUALITY_ARBITER_MODE=on`, a high-confidence keyed
signal sends the candidate to Terra, which must independently decide
whether the wording really leaks the answer. Both primary reviewer
and Terra prompts mention these specific families. If Terra confirms
an answer leak, the candidate is rejected through the existing bounded
quality regeneration path.

Regression tests cover the original English question, the paired
word families in both directions, uniqueness, irrelevant four-letter
overlaps and a primary-reviewer-pass / Terra-leak-veto scenario.

## Shadow configuration — independent weights

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `QUIZ_QUALITY_SKILL_MODE` | `shadow` | `off` or `shadow` only |
| `QUIZ_QUALITY_LEXICAL_PENALTY_WEIGHT` | `3.0` | Lexical risk-score weight |
| `QUIZ_QUALITY_TAXONOMY_PENALTY_WEIGHT` | `3.0` | Taxonomy risk-score weight |
| `QUIZ_QUALITY_FACT_PENALTY_WEIGHT` | `3.0` | Fact risk-score weight |
| `QUIZ_QUALITY_ARBITRATION_MIN_SEVERITY` | `2` | Shadow candidate threshold |
| `QUIZ_QUALITY_ARBITRATION_MIN_CONFIDENCE` | `0.55` | Shadow candidate threshold |
| `QUIZ_QUALITY_LEXICAL_MIN_PREFIX` | `5` | Shared Unicode letters |
| `QUIZ_QUALITY_LEXICAL_MIN_SHARED_FRACTION` | `0.8` | Of shorter word |

These scores express **heuristic risk**, not measured probabilities or
production editorial penalties. Each tool contributes only its highest
finding; overlapping signals do not stack per tool.

## Editorial configuration — active in tagged ENFORCE

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `QUIZ_QUALITY_SCORE_THRESHOLD` | `3.25` | Regeneration score threshold |
| `QUIZ_QUALITY_DISTRACTORS_WEIGHT` | `1.0` | Critic distractors |
| `QUIZ_QUALITY_SUITABILITY_WEIGHT` | `1.0` | Critic suitability |
| `QUIZ_QUALITY_WORDING_WEIGHT` | `1.0` | Critic wording |
| `QUIZ_QUALITY_KNOWLEDGE_UNIT` | `0.25` | Other knowledge issue, per unit |
| `QUIZ_QUALITY_KNOWLEDGE_CAP` | `0.5` | Other knowledge cap |
| `QUIZ_QUALITY_TOO_HARD_UNIT` | `0.25` | Too-hard cost, per unit |
| `QUIZ_QUALITY_TOO_HARD_CAP` | `0.5` | Too-hard cap |
| `QUIZ_QUALITY_TOO_EASY_UNIT_START` | `0.25` | Too-easy cost at level 0 |
| `QUIZ_QUALITY_TOO_EASY_UNIT_END` | `1.5` | Too-easy cost at level 15 |
| `QUIZ_QUALITY_TOO_EASY_CAP_START` | `0.5` | Too-easy cap at level 0 |
| `QUIZ_QUALITY_TOO_EASY_CAP_END` | `3.0` | Too-easy cap at level 15 |
| `QUIZ_QUALITY_TOO_EASY_CURVE` | `1.5` | Progress curve exponent |

Progress = `(clamped_difficulty/15)**curve`. The `too_easy` per-unit
weight and cap interpolate independently between start and end. The
`too_hard` weight stays constant. V3 model field ranges do not change.

## Logs and testing

Set `AI_GENERATION_DEBUG=true` locally to see four `DEBUG:` lines per
candidate: three `quality_skill_shadow_tool` entries, then one
`quality_skill_shadow` summary. This includes individual signals, risk
scores, their basis, proposed arbitration, and explicit
`decision=observe_only`. The existing `tagged_review_score` INFO event
shows the active editor score, threshold and `difficulty_progress`.

Run in backend:
```bash
python -m unittest tests.test_quality_skill_shadow tests.test_tagged_dynamic_scoring -v
python -m unittest discover -s tests -v
```

Current limits: morphological prefix matching is heuristic, the subtype
knowledge base is deliberately small, and the fact tool does **not**
verify external facts. False positives and missed facts are to be measured
from real game logs before turning on arbitration or authority.


## Source-backed false-fact guard and incident quarantine

## Spatial comparison safety gate (2026-11-10)

A confirmed player appeal found an apparently valid structured fact triple
`ural-river / flows-into / caspian-sea` that dropped the critical literal
restriction `южнее Волги`. The primary critic also evaluated only the weaker
question and mistakenly approved the candidate.

The Fact Tool now detects explicitly comparative directional phrases in
Russian, English, German, French and Spanish, including `южнее Волги`,
`к западу от Берлина`, `south of the Volga`. This is an *unknown-truth
signal*, not a geography lookup or automatic verdict. In tagged
`AI_QUALITY_MODE=enforce`, these candidates require independent Terra
arbitration **even if** the generic `QUIZ_QUALITY_ARBITER_MODE=off` or
`QUIZ_QUALITY_SKILL_MODE=off`. Other lexical/taxonomy/fact heuristics
retain the usual shadow and optional arbitration behavior.

Terra receives the exact extracted directional phrases and must inspect each
as a distinct question condition. Its output must anchor every requirement
in `stem_claims`; returning only a true endpoint/country fact without the
spatial qualifier is a **quality rejection** (regenerate candidate), not an
immediate HTTP 503. False or uncertain comparisons fail the same existing
independent-review acceptance gate. Genuine model/provider outages retain
normal fail-closed behavior. A correct comparative question can still be
accepted when all qualifiers have explicit evidence.

This is a second-model consistency check, **not independent geospatial source
verification**. A hallucinated agreement by both models remains possible.
Exact already-confirmed player-appeal wording is separately held by the
existing known QA incident registry. Verified shared-pool questions still
bypass new model calls as before.

Regression command:

```powershell
python -m unittest tests.test_spatial_relation_guard tests.test_quality_arbitration_live tests.test_quality_skill_shadow -v
```

The shadow Fact Tool now nominates award + year (+ stated rationale) claims
for independent evidence using `needs_source_verification:award_year_attribution`
or `needs_source_verification:compound_award_year_reason`. Such a signal
is **not** evidence that the claim is false and does not block a question
or make a paid arbiter call.

A separate **narrow deterministic pre-review hard gate** recognizes only
explicitly source-backed *false structured fact triples*. Initial incident:
`grace-hopper / turing-award-year / 1972`, contradicted by the ACM
1972 laureate listing (Edsger W. Dijkstra). This specific false triple
is rejected before the paid reviewer in ALL tagged quality modes. Other
awards, people and years remain subject to the regular critic;
the current guard is not general-purpose fact verification.

ACM: https://amturing.acm.org/laureates.cfm

To quarantine an already-persisted incident **without rewriting history**,
stop FastAPI and run from the backend using the same `DATABASE_URL`:

```powershell
python -m persistence.quarantine_known_bad
python -m persistence.quarantine_known_bad --apply
```

The first command only previews. The second changes only the exact logged
question (matching stem, correct answer and structured fact) from
`approved` to `quarantined`, appends a source-backed audit review, and
preserves the original critic review, question attempts, answers and stars.
It is idempotent and does not change other questions. On a different DB,
a zero-match result is a warning to verify `DATABASE_URL`.
The approved global pool excludes quarantined questions by status.

The source-backed hard gate and its tests do **not** establish general
accuracy of GPT-5.4-mini or of the Fact Tool. Future arbitrary compound
claims still require external verification or a separate arbiter.
