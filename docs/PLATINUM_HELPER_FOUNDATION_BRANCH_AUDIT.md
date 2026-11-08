# Audit: legacy Platinum Helper foundation branch (2026-11-08)

## Branch ancestry and handling

- Legacy: `feature/platinum-helper-foundation`.
- Last tested working base: `fix/late-question-recovery-20261108`.
- New changes: `feature/platinum-helper-skill-calibration-20261108` (PR #14 targeting the working base).
- GitHub compare reports a **diverged** history: 54 commits on foundation not reachable from working base, 350 commits on working base not reachable from foundation. This is an ancestry difference, **not** proof of 54 missing features.
- **Do not merge, rebase, reset or delete** the legacy branch automatically. A mechanical merge can restore obsolete logic.

## Functional comparison

| Feature originally in foundation | In working branch | Evidence / change |
| --- | --- | --- |
| Private generated helper profile; 20 topics and profession skills | Present, expanded | `backend/platinum_helper/core.py` |
| Purchase, 3 consultations, no double charge, refund after Q15 if unused | Present, expanded | `backend/platinum_helper/service.py`, `backend/persistence/star_economy.py` |
| HTTP helper routes | Present, extended with UI language and identity generation | `backend/platinum_helper/routes.py` |
| Model responder | Present, more robust validation, speech style, fallbacks, multilingual guardrails | `backend/platinum_helper/responder.py` |
| Foundation `0005_platinum_helper` migration | **Superseded**, not dropped | New `0005_star_categories` then `0006_platinum_helper`; purchase and call tables preserved |
| Separate `platinum_star_movements` and `gold_star_movements` | **Deliberately replaced** | Shared `wallet_transactions` ledger; bonus wager has separate later revision |
| Initial helper CI | Present, expanded | `.github/workflows/platinum-helper-tests.yml` with backend, frontend and PostgreSQL migration jobs |
| Basic backend/frontend helper regression | Present and expanded | `backend/tests/test_platinum_helper*.py`, `frontend/tests/app.component.test.mjs` |

## Migration finding

The old file `backend/migrations/versions/0005_platinum_helper.py` was **not missing functionality**. Its revision number conflicted with the category-shop migration that now owns revision `0005_star_categories`. The live sequence is:

```
0004_bonus_topic_rotation
  -> 0005_star_categories
  -> 0006_platinum_helper
  -> 0007_bonus_wager
  -> 0008_single_active_round
  -> 0009_question_competence_area
```

The newer `0006_platinum_helper` creates the purchase and consultation tables. The legacy additional star-movement tables were intentionally replaced by the common wallet ledger. **Do not copy the old 0005 migration into the new branch:** that would produce conflicting Alembic heads, table duplication or transaction-model regressions.

## Risk and validation limitations

The review compares GitHub branch file contents and per-file change lists; it is not an exhaustive proof that every old commit has an exact successor. No legacy-only functionality worth cherry-picking was identified in the core helper, routes, purchase/call persistence, responder, migration and tests. The latest working code has been exercised in manual browser testing, but this audit did **not** execute the Python/Angular suites locally or run a PostgreSQL migration.

The CI workflow in PR #14 now includes the current PR base and feature branch so a pushed commit can trigger checks. Inspect the resulting workflow run; do not treat these tests as passing until GitHub reports a successful conclusion.

## Cleanup decision

Keep the legacy branch available as an archive until PR #14 checks and a final `git diff` review are complete. No branch was deleted, rebased or merged as part of this audit. A safe eventual cleanup is to preserve a tag or archive reference, then remove the legacy branch in a separate owner-approved operation.
