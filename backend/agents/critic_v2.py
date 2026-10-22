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

ANSWER-LEAK EDITORIAL TEST (apply specifically to the leakage field):
Imagine a player who does NOT know the underlying answer. Can that player
identify the keyed option from clues in the QUESTION'S WORDING alone, by
comparing it to the available options? Check ALL four options, not just the key.
Look for repeated distinctive words, shared word stems or inflections,
transparent name translations, paraphrases, and descriptions that spell out
an answer's distinctive title or name. Consider Russian morphology and
other supported languages, not only exact string matches.

Concrete leaks:
- "Какая кость образует лоб?" -> "Лобная кость": лоб/лобная gives the answer
  through a shared root without requiring anatomy knowledge.
- "В какой игре нужно собрать четыре в ряд?" -> "Соедини четыре":
  the wording closely paraphrases the game's name, so a player can pick it
  from the options without recognizing the game.

NOT leaks:
- "Какой закон связывает напряжение, ток и сопротивление?" -> "Закон Ома":
  the stem describes the concept, but does not reveal the name "Ом".
- "Кто написал роман «Педро Парамо»?" -> "Хуан Рульфо":
  naming the work is necessary identification context, not a lexical clue
  to the author's name.

Do NOT treat ordinary subject vocabulary, necessary definitions, correct
scientific descriptions, or overlap shared by multiple options as leakage
unless it disproportionately points to one answer without topic knowledge.
For a definite clue set leakage=fail and include answer_leak in issue_codes;
otherwise set leakage=pass. In the rationale, identify the specific clue
if and only if a leak was found. Do not invent leakage just because the
correct option sounds familiar or one distractor is weak (use distractors
for that separate problem).
"""

QUALITY_REVIEW_V2_PROMPT += "\n\n" + SHARED_DIFFICULTY_INSTRUCTIONS + "\n"
