"""V3 audit scoring instructions; reuse all v2 editorial guidance and shared scale.

V3 does not alter the generator and cannot force a retry in audit mode.
"""
from agents.critic_v2 import QUALITY_REVIEW_V2_PROMPT


QUALITY_REVIEW_V3_PROMPT = QUALITY_REVIEW_V2_PROMPT + """

FINAL V3 OUTPUT CONTRACT OVERRIDE.
Ignore the earlier v1/v2 JSON field lists. Return ONLY a JSON object with
these exact fields (strict schema is authoritative):
facts, options, language, leakage: pass | fail | uncertain
difficulty: appropriate | too_easy | too_hard | uncertain
independent_answer: A | B | C | D | unknown
issue_codes: list of existing issue codes
distractor_penalty: integer 0..3
knowledge_penalty: integer 0..2
suitability_penalty: integer 0..2
wording_penalty: integer 0..1
rationale: specific reasons for nonzero penalties or critical flags, <=600 chars
Do not include distractors status, decision, totals or threshold decisions.
Python owns the policy; the model supplies only observations.

CRITICAL FAULTS (not scored):
- Factually false premise, wrong keyed answer, multiple/no correct options,
  ambiguous or missing decisive context -> facts/options fail as appropriate.
- Decisive answer leakage -> leakage fail (use the reasoning-versus-leakage
  guidance above).
- Language so broken that the meaning cannot be reliably understood ->
  language fail. Minor awkward wording is NOT critical.
- Uncertain factual validity -> uncertain, not fabricated certainty.
Do not hide critical faults as a numerical penalty or falsely invent them.

NONCRITICAL EDITORIAL PENALTIES (calibrate to requested difficulty 0..15):

DISTRACTORS, 0..3 (judge effectiveness relative to the REQUESTED level):
0: Well suited. At levels 0–3, trivially eliminated answers can be GOOD.
   At high levels, distractors should be close, realistic alternatives.
1: One clearly weak or out-of-family distractor; the question still tests
   the intended knowledge at its level.
2: Several weak alternatives make elimination noticeably easier than intended.
3: Almost every wrong answer is absurd or easily eliminated at a mid/high
   level; a player can find the key without the intended knowledge.
Do not penalize easy elimination simply because the requested level is low.
Check whether each distractor is historically/scientifically plausible.
A distractor that is ALSO truly correct is a CRITICAL options failure.

KNOWLEDGE / INTRINSIC DIFFICULTY, 0..2:
0: Reasonable within a broad range for the requested level.
1: Noticeable but tolerable mismatch in the knowledge needed.
2: Large discrepancy in prerequisite knowledge even when ALL four choices
   are plausible; the question belongs far above/below its requested level.
Do NOT charge a knowledge penalty a second time just because weak
distractors made the SAME otherwise suitable question easy.
Judge against an educated GENERAL player, NOT an expert or yourself.
A literary attribution such as «Педро Парамо» -> Juan Rulfo is not
automatically easy merely because it is a straightforward recall question.
Set 'difficulty' to describe direction when relevant; it is advisory.

TRIVIA SUITABILITY, 0..2:
0: Interesting, fair general-interest trivia; inference/association welcome.
1: Somewhat dry or narrow, but still defensible as trivia at its level.
2: Specialist examination or arbitrary memorization of technical data
   (e.g., enormous group orders) rather than enjoyable erudition, even
   if entirely factual and difficult. At high levels favor deep CULTURAL
   or cross-disciplinary knowledge over specialist professional exams.

WORDING/PRESENTATION, 0..1:
0: Clear and readable in the requested language.
1: Needlessly verbose, clunky, or mildly unidiomatic, but interpretable.
Do not double-penalize factual falsehood or answer leakage as wording.

Scoring is independent of the critical checks. NEVER reject a question solely
because of a score: the Python audit pipeline will log score distributions
without enforcing any threshold. Explain nonzero components concretely and
avoid inventing minor flaws just to assign points.
"""
