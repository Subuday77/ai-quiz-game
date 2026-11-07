# Platinum Helper — question-level competence area

A helper's **profession** and twenty skill levels stay fixed at purchase. The
question's *game category* is still used for selecting questions, but it no
longer unconditionally dictates the helper's knowledge area.

## Current behavior

- For **new tagged questions**, the mandatory quality critic returns one
  `competence_area` from the same twenty areas used by helper profiles.
  The choice is based on the knowledge actually tested, not the broad category
  or the profession of an answer choice. **No additional AI request**.
- The accepted question's area is stored privately on `questions.competence_area`
  in PostgreSQL; the browser does not receive it.
- During consultation, the server reads that field before rolling the helper's
  HIGH/MEDIUM/LOW probabilities. The frozen area and rolled answer are saved
  in `platinum_helper_calls.topic`, so retries do not reroll.
- Canonical fact/concept cues (e.g., `electromagnetic-induction`) resolve
  **existing questions** for free even when the question stem is in German.
  Conflicting/unclear cues are deliberately ignored.
- For historical questions without reviewed area or sufficiently clear tags,
  the legacy category mapping remains an **explicit last resort**.
  `HELPER_DEBUG` includes `competence_source`: `question`,
  `legacy_metadata`, `legacy_category`, or `frozen_call`.
- No old attempts, answers, earned stars, wallet transactions, or already
  recorded consultations are changed.

The reviewed competence field is not a ground-truth guarantee: model
classifications can be wrong. High-confidence canonical subject evidence
takes precedence when it conflicts with the critic's area; the disagreement
is logged as `question_competence_area_reconciled`. Do not derive an area from
a scientist's name alone (a biographical question may test History).

## Installation

From the **PyCharm backend terminal**, after pulling the branch:

```powershell
python -m alembic upgrade head
```

Revision `0009_question_competence_area` adds a nullable column. It
**intentionally leaves historical rows NULL**, avoiding fabricated assignments.

## Existing questions — free preview and controlled backfill

To inspect the historical Faraday question supplied in live QA:

```powershell
python -m scripts.backfill_question_competence --question-id 2372b48d-784d-4116-a253-a275a1e0ca55
```

It displays `legacy_metadata Physics` if canonical metadata is sufficient,
or `legacy_category Technology` if it is not. In that order:

1. If it says `legacy_metadata Physics`, persist the free verified assignment:

   ```powershell
   python -m scripts.backfill_question_competence --question-id 2372b48d-784d-4116-a253-a275a1e0ca55 --apply
   ```

2. If it says `legacy_category Technology`, the question does not have
   enough canonical evidence. Classify its actual German question text once:

   ```powershell
   python -m scripts.backfill_question_competence --question-id 2372b48d-784d-4116-a253-a275a1e0ca55 --apply --ai
   ```

   This is an **explicit billable OpenAI request**. It uses the existing
   `OPENAI_API_KEY` and `AI_QUALITY_MODEL_ID` from the root `.env`.
   Override the model only with an explicit `--model`. There are no extra
   environment variables for this migration tool.

For more existing questions:

```powershell
python -m scripts.backfill_question_competence --limit 100
python -m scripts.backfill_question_competence --limit 100 --apply
# OPTIONAL, PAID, for unresolved legacy questions only:
python -m scripts.backfill_question_competence --limit 100 --apply --ai
```

The script limits model batch size to 10 and total AI requests to 10 by
default. Change these explicitly via `--batch-size` and
`--max-api-requests`. It never contacts the model in preview or ordinary
`--apply` mode, and it never overwrites an existing saved area.

**Important:** existing `platinum_helper_calls` remain frozen even if the
question is later reclassified. Test using a *new* consultation against the
saved question, not by replaying an old idempotency key.

## Expected examples

| Game category | Question subject | Competence area |
| --- | --- | --- |
| Inventions and discoveries | Electromagnetic induction | Physics |
| Inventions and discoveries | Penicillin | Medicine |
| Inventions and discoveries | Printing press | Technology |

The result affects the HIGH/MEDIUM/LOW probability draw; a HIGH helper still
has an 8% chance to be MISTAKEN by the existing game rules.
