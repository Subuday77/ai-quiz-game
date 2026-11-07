"""Speech-only history for a single purchased Platinum Helper.

Past replies live in PostgreSQL. The next model sees style patterns but never
previous selected answers or other question choices. This module does not
modify stored speech or require a database migration.
"""
from __future__ import annotations

import re

HISTORY_LIMIT = 2
MASK = "[ANSWER]"


def anonymize_reply(
    speech: str | None,
    choices: dict | None,
    selected_key: str | None,
) -> str | None:
    """Conservatively strip old option text; discard unmaskable history.

    Avoid passing previous answers (including incorrect suggestions) to the
    model. Replace all four option strings, not just the selected one. Match
    literal text case-insensitively and fail closed if the chosen label cannot
    be found or a normalized option still remains.
    """
    if not isinstance(speech, str) or not speech.strip() or len(speech) > 450:
        return None
    if not isinstance(choices, dict) or set(choices) != set("ABCD"):
        return None
    options = list(choices.values())
    if any(not isinstance(value, str) or not value.strip() for value in options):
        return None
    if selected_key is not None and selected_key not in choices:
        return None

    sanitized = speech
    selected_masked = selected_key is None
    # Longest first: if two option labels overlap, avoid leaving a tail.
    for option in sorted(options, key=len, reverse=True):
        sanitized, count = re.subn(re.escape(option), MASK, sanitized, flags=re.I)
        if selected_key is not None and option == choices[selected_key] and count:
            selected_masked = True
    if not selected_masked:
        # Unicode-normalized but nonliteral matches would require guessing
        # character offsets. Drop this row instead of leaking its answer.
        return None

    # Reuse the production matching policy to guard Unicode/CJK corner cases.
    # Local import keeps the helper's pure responder independent of SQLAlchemy.
    from platinum_helper.responder import _mentions

    if any(_mentions(sanitized, value) for value in options):
        return None
    sanitized = re.sub(r"[\r\n\t]+", " ", sanitized).strip()
    if not sanitized or len(sanitized) > 450:
        return None
    return sanitized
