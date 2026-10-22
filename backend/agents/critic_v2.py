"""Optional diagnostic critic v2 instruction layer.

Keep the existing skill and add an explicit status for each independent check.
No tool access and no autonomous decision-making.
"""
from agents.critic import QUALITY_REVIEW_PROMPT as BASE_PROMPT
from consts import SHARED_DIFFICULTY_INSTRUCTIONS

QUALITY_REVIEW_V2_PROMPT = BASE_PROMPT + """

V2 OUTPUT OVERRIDE. The old four-field v1 JSON format above is superseded for
this request. Return ONLY a JSON object with these exact fields:
facts, options, language, distractors, leakage: pass | fail | uncertain
difficulty: appropriate | too_easy | too_hard | uncertain
independent_answer: A | B | C | D | unknown
issue_codes: array using the existing issue code vocabulary
rationale: brief concrete explanation (1-600 characters)
No decision field: Python applies the publication policy.
Treat difficulty as an advisory judgement separate from factual validity.
For each status, fail only for an identifiable material flaw; uncertain
means a claim cannot confidently be established without external checking.
If you cannot identify a specific factual mistake, do not claim one.
An item can have a correct key and still fail its premise or distractors.
"""

QUALITY_REVIEW_V2_PROMPT += "\n\n" + SHARED_DIFFICULTY_INSTRUCTIONS + "\n"
