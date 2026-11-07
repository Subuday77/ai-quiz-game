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

## Comparing a new responder version with the original 30-case baseline

The initial 30 GPT-5.4 consultations belong to the previous prompt version.
**Do not resume that directory after code changes**, because a mixed report
would not be a valid regression. The runner now fingerprints the actual helper
code and refuses to reuse a plan from another version, including legacy plans
without a fingerprint.

To start a fresh comparable run with the same seed, run these commands from
`backend/` after `git pull`:

    python -m scripts.helper_live_regression --limit 30 --output-dir qa_reports/platinum_helper_live_v2
    python -m scripts.helper_live_regression --execute --limit 30 --output-dir qa_reports/platinum_helper_live_v2

The new ZIP will appear in `backend/qa_reports/platinum_helper_live_v2/`.
Keep the original `platinum_helper_live/` directory for the before/after QA.

## Third speech iteration: multilingual naturalness (V3)

The second 30-case run eliminated wrong-script responses and the four
self-disqualifying factual explanations, but exposed repetitive formulas and
ungrammatical insertion of nominative answer labels after a verb.

V3 uses a **language-neutral** rule: construct a natural sentence *around*
an unchanged option label instead of trying to decline the label. It does
not implement a case table, automatic declension or per-language rewriting.
Russian deterministic fallbacks now use an option-label construction.

Start a new 30-case comparison after updating from GitHub:

    python -m scripts.helper_live_regression --limit 30 --output-dir qa_reports/platinum_helper_live_v3
    python -m scripts.helper_live_regression --execute --limit 30 --output-dir qa_reports/platinum_helper_live_v3

Keep the existing `platinum_helper_live/` (V1) and
`platinum_helper_live_v2/` directories for comparison. The runner rejects
plans generated against a different code fingerprint. Evaluate V3 speech
naturalness in **all languages tested** before expanding to 150; the current
30-case plan is mostly Russian, so two English examples cannot validate
every supported language.

## V4 — SMALL cross-language speech test (9 cases, not 30)

V4 removes technical expressions from the model-facing instructions that
caused *"answer label"* and repeated *"вариант"*. It preserves equal
KNOW/MISTAKEN instructions, the ban on factual justifications, the selected
answer's exact wording, and UI-language speech even when questions are in
another language. No morphological engine or per-language case table.

This is a separate, focused smoke test using **nine different approved
PostgreSQL questions**, with the real production answer/distractor selection,
speech model, validator, retry and fallback. The 3 × 3 planned matrix is:

| Question language | UI language for KNOW | MISTAKEN | UNSURE |
| --- | --- | --- | --- |
| German | Russian | English | German |
| Russian | German | English | Russian |
| English | Russian | German | English |

Run from the PyCharm terminal in \`backend/\`. First preview and freeze
real scenarios for FREE; then explicitly authorize at most **nine model
consultations and 18 total attempts**:

    python -m scripts.helper_speech_smoke_v4
    python -m scripts.helper_speech_smoke_v4 --execute

No Angular or running uvicorn is needed. A populated PostgreSQL pool must
contain at least three eligible questions in each language, including a
plausible wrong choice for each MISTAKEN slot. If not, preview fails without
inventing test questions; add more verified questions and rerun.

The paid test has an estimated USD 0.10 ceiling, **not a guarantee of
billing**, and records every draft and response; it won't modify stars,
questions or game history. Keep the original V1–V3 run directories untouched.
Output ZIP:

    backend/qa_reports/platinum_helper_speech_v4_small/platinum_helper_live_results.zip

Review naturalness and correctness manually: avoid stock formulas, accidental
technical jargon, wrong-language replies and self-disqualifying explanations.
The script reports token usage and guardrail rejection/fallback counts but
does **not** certify semantic correctness from deterministic checks alone.

## V5 — frozen speaking-frame diversity and exact answer text (9 cases)

Why: in V4, all 6 recommendation samples independently started with
"I would", "Я бы" or "Ich würde" despite generic naturalness instructions.

The backend now picks one of three *language-neutral speaking approaches*,
entirely from the saved random call UUID: **DIRECT**, **PERSONAL**, **CASUAL**.
This style is independent of KNOW/MISTAKEN and the 50/50
CONFIDENT/TENTATIVE confidence style; a retry reuses exactly the same frame.
No user-facing "label", "variant", or inflection templates. The model still
must speak in the current **UI language** and preserve the chosen option text.

The mandatory validator now requires the COMPLETE selected option text in
a recommendation (Unicode-normalized, case-insensitive), beyond merely
matching the hidden KEY. If a model omits it, the usual bounded retry and
deterministic fallback apply. No extra model or critic call was added.

Small test: 9 real, distinct approved questions from PostgreSQL:
3 each in German/Russian/English; 3 KNOW, 3 MISTAKEN, 3 UNSURE;
each style is covered once per question language AND once per outcome.
For the six recommendations, every style appears in one confident and one
tentative scenario. The tester overrides only the *test's presentation styles*
to ensure coverage; production still hashes the stored UUID.

In PyCharm from \`backend/\`:

    git pull
    python -m scripts.helper_speech_smoke_v5
    python -m scripts.helper_speech_smoke_v5 --execute

Default preview makes **ZERO API calls**. Explicit \`--execute\` allows
9 GPT-5.4 consultations, at most 18 requests with retries and an estimated
USD 0.10 guard (not a hard billing guarantee). Repeated runs in the same
version-frozen directory skip completed calls. The script does NOT modify
the game, question pool, stars, outcomes, or database records.

V5 results will appear in:

    backend/qa_reports/platinum_helper_speech_v5_frames/platinum_helper_live_results.zip

Do not reuse V4's directory; V4's nine replies are the baseline. Examine
the new ZIP for repetition, naturalness, literal option preservation,
wrong-language speech and implicit factual self-disclosure. The model may
still independently choose identical natural words even with new frames;
the smoke run measures whether the variation actually helps.

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
