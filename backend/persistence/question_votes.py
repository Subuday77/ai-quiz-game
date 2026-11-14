"""Atomic, immutable votes recorded only after a successful answer.

This module never commits: the caller owns the answer + vote + history
transaction, and must already have locked the player, game and question.
An unanswered preference is browser-local only, not a SQL vote.
"""
from uuid import UUID

from sqlalchemy.orm import Session

from diagnostics import event
from persistence.models import (
    Question, QuestionModeration, QuestionVote, QuestionVoteReview,
)
from persistence.question_source_policy import (
    community_vote_threshold, max_referrals,
)


def record_final_vote(db: Session, *, question: Question, user_id: UUID,
                      run_id: UUID, value: int) -> bool:
    """Update both nets once; queue Judge or third-strike ban atomically.

    Returns True only when a new permanent vote was inserted.
    Call *inside* the same transaction as the accepted answer; do not commit.
    """
    if type(value) is not int or value not in (-1, 0, 1):
        raise ValueError("Final vote must be -1, 0 or 1")
    if value == 0:
        return False
    # (user, game, question) row locks were obtained by record_answer.
    # Existing historical votes are immutable, even after two acquittals.
    if db.get(QuestionVote, (question.id, user_id)) is not None:
        return False
    db.add(QuestionVote(
        question_id=question.id, user_id=user_id, run_id=run_id, value=value,
    ))
    question.vote_score += value
    question.working_vote_score += value
    if (question.status != "approved" or
            question.working_vote_score > community_vote_threshold()):
        return True

    next_referral = question.community_referrals + 1
    maximum = max_referrals()
    question.community_referrals = next_referral
    auto_ban = next_referral >= maximum
    review = QuestionVoteReview(
        question_id=question.id, referral_number=next_referral,
        trigger_user_id=user_id,
        score_at_trigger=question.working_vote_score,
        status="auto_banned" if auto_ban else "pending",
    )
    db.add(review)
    event("rating_quarantine", question_id=str(question.id),
          working_score=question.working_vote_score,
          referral_number=next_referral)
    if auto_ban:
        question.status = "rejected"
        moderation = db.get(QuestionModeration, question.id)
        if moderation is None:
            moderation = QuestionModeration(question_id=question.id)
            db.add(moderation)
        moderation.permanently_blocked = True
        moderation.block_reason = moderation.block_reason or "community_third_referral"
        review.verdict = "Third threshold crossing: automatic permanent ban."
        from datetime import datetime, timezone
        review.reviewed_at = datetime.now(timezone.utc)
        event("judge_ban", question_id=str(question.id),
              reason="community_third_referral")
    else:
        question.status = "quarantined"
        event("judge_referral", question_id=str(question.id),
              referral_number=next_referral)
    return True
