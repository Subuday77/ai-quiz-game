"""Question thumbs endpoints. Never return aggregate score or other ballots."""
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, StrictInt
from sqlalchemy.orm import Session

from persistence.auth import authenticated_user, validate_origin
from persistence.database import get_db
from persistence.gameplay import GameConflict, GameNotFound
from persistence.models import User
from persistence.question_votes import submit_vote
from persistence.vote_worker import process_vote_review

router = APIRouter(prefix="/api/v2/games", tags=["Question feedback"])


class QuestionVoteInput(BaseModel):
    value: StrictInt


@router.put("/{run_id}/questions/{question_id}/vote")
def vote_question(run_id: UUID, question_id: UUID, payload: QuestionVoteInput,
                  request: Request, background_tasks: BackgroundTasks,
                  user: User = Depends(authenticated_user),
                  db: Session = Depends(get_db)):
    validate_origin(request)
    try:
        value, queued = submit_vote(
            db, run_id=run_id, question_id=question_id,
            user_id=user.id, value=payload.value,
        )
    except GameNotFound:
        raise HTTPException(status_code=404, detail="Game not found") from None
    except GameConflict:
        raise HTTPException(status_code=409, detail="Question cannot be rated now") from None
    except ValueError:
        raise HTTPException(status_code=422, detail="Vote must be -1, 0 or 1") from None
    if queued:
        # Queue is already committed; a crash before this task starts cannot
        # lose the review, since the built-in scheduler scans pending rows.
        background_tasks.add_task(process_vote_review, question_id)
    return {"my_vote": value}
