# Question Quality Skill — shadow rollout

This is a **Python runtime component**, not an agent skill that automatically
executes from a Markdown file. The application calls
`tagging.quality_skill.engine.evaluate_shadow` after schema validation and
tag deduplication, before local leakage gates and the paid tagged reviewer.

## Contract and authority

- **Correctness tools:** `lexical_tool` (shared-root answer shortcuts),
  `taxonomy_tool` (option type/subtype relations), `fact_tool`
  (risky superlative, exclusive and chronology claims).
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
