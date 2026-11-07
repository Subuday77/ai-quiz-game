# Platinum Helper — live regression QA (GPT-5.4)

This **offline-from-the-game** script exercises the real production
platinum_helper.responder.talk() with actual OpenAI API calls.

Run in the **PyCharm backend terminal**, after pulling the branch
feature/platinum-helper-54-guardrails. The Angular frontend and uvicorn
do not need to be started.

## Prerequisites

- Backend dependencies installed from backend/requirements.txt.
- The single **repository-root** `.env` (`../.env` from `backend/`) is copied
  from `.env.example`. Fill `DATABASE_URL` and `OPENAI_API_KEY` there.
  The script, FastAPI and Alembic use the same file; no `backend/.env`.
  Neither secret is printed, included in results, or uploaded to GitHub.
- Approved questions with original generator plausibility reviews.

## Commands (working directory: backend)

1. Free preview, saves frozen 150-case plan without calling the provider:

       python -m scripts.helper_live_regression --limit 30

2. First 30 consultations (REAL paid GPT-5.4 calls):

       python -m scripts.helper_live_regression --execute --limit 30

3. If the first run is satisfactory, continue **the same frozen plan** up to
   150 total (already completed cases are skipped):

       python -m scripts.helper_live_regression --execute --limit 150

The default model, reasoning effort, token cap, timeout, attempt count and
plausibility thresholds all come from the **root .env**, just like gameplay.
Only an EXPLICIT `--model` argument overrides the configured model for that
run. The saved test plan records the effective settings; changed settings
require a new `--output-dir` (historical plans without metadata are readable).

Default spending guard: estimated USD 3 total across the run and a maximum
of 350 actual API attempts including retries. Override using --max-usd or
--max-api-requests. These are guards; estimates are not billing data.
The script stores every API attempt immediately, even invalid replies.
A crash during an in-flight request may still cause one duplicate request
on resume. Stop with Ctrl+C to get a partial ZIP.

## What is tested

- Real approved questions + original plausibility metadata from PostgreSQL,
  including all four actual answer texts.
- The production selection policy best_visible_wrong() and simulated
  0/1/2 bronze-hidden answers using choose_to_hide().
- Forced KNOW, MISTAKEN, UNSURE and MISTAKEN-to-UNSURE cases to cover
  guardrails efficiently (not a statistical simulation of skill weights).
- Production confidence styles, real GPT-5.4 prompt, validation, one retry,
  and fallback. No changes to database state, game wallets, or users.

## Result files

The script creates backend/qa_reports/platinum_helper_live/ (ignored by Git):
- plan.json — immutable plan for resume
- calls.jsonl — final consultation per scenario
- attempts.jsonl — raw model outputs and usage, including retries
- results.csv — joined question/answers/selection/outcome/final speech
- blind_review.csv — human evaluation sheet without truth labels
- summary.json — counts and estimated total cost
- platinum_helper_live_results.zip — **send this file back for analysis**

The automatic guardrail is NOT a full semantic fact checker. Review naturalness,
incorrect facts, hidden hints, and subtle self-disclosure manually.

To retry previously failed (non-completed) scenarios, explicitly add
--retry-failed. To start a completely independent test use --output-dir
with a different folder; do not delete a partially completed run by accident.

If an existing plan was created with a different seed, target or model, the
runner stops rather than silently changing already-frozen cases.
