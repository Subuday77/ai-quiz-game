# Follow-up proposal: recover a lost answer response (CR-2)

Status: proposed; not implemented in the Issue #2 timeout diagnostics fix.

## Confirmed failure

Angular applies its finite HTTP timeout to `POST /api/games/{game_id}/answer`.
FastAPI can finish `submit_answer` after the browser stops waiting. The backend
advances the game and clears the unanswered question ID, while Angular retains
the question screen without feedback. Clicking an answer again then receives 409
(`No matching unanswered question`). The existing game-info endpoint reports
progress/phase but cannot restore the accepted answer, correctness, or achievement.
Question Retry does not resolve this case: requesting the next question could skip
feedback, and a wrong answer or final bonus answer may already have ended the game.

This needs a separate change because it introduces an answer receipt API and a
frontend recovery state with reward reconciliation. No answer behavior, API
contract, or reward handling is changed in the CR-1 patch. The limitation remains
until that follow-up ships.

## Proposed server contract

Keep answer submission single-use and keep existing duplicate POST behavior (409).
Add a read-only recovery endpoint, provisionally:
`GET /api/games/{game_id}/answers/{question_id}`.

Under the existing session lock, record an immutable receipt for every accepted
answer as part of the same critical section as progression and clearing the
unanswered question. Store the question ID, accepted choice, the original
`AnswerFeedback` snapshot, and a stable achievement event ID (for example, the
question ID when an achievement is awarded). Retain receipts through the session's
lifetime; the current rules bound this to 16 accepted answers per game. Do not
recompute feedback from the game's later state or store only the latest answer.

The recovery endpoint must acquire the same lock and return:

- **200, answered:** the stored receipt, including original feedback and event ID.
  Reading it must not call `submit_answer`, change progression, allocate a reward,
  generate a question, or clear any current unanswered question.
- **202, unanswered:** only when the requested ID still matches the current
  unanswered question. Do not reveal correctness before an answer is accepted.
- **404, unknown:** an unknown game/question or a session lost after restart.
  Explain that recovery is unavailable; do not guess whether the answer succeeded.

The lock makes recovery wait for a POST already processing in its critical section.
A recovery GET can still arrive before the original POST reaches the backend, so a
202 response must not be treated as proof that the original request was canceled.
Conflicting submissions remain rejected; an old receipt must never be applied to a
different question or mistaken for the latest game state.

## Proposed browser flow

1. Before sending an answer, retain its game ID, question ID, and selected choice.
   Freeze answer choices until submission/recovery resolves. Do not automatically
   replay POSTs after a timeout or network error.
2. If the POST result is uncertain, show **Recover answer result**. Read the receipt
   for that exact pending question. This GET is safe to retry with a finite timeout.
3. On 200, show the accepted choice and original feedback, reconcile progress and
   rewards, and clear the pending submission. Only the normal Continue action may
   request the next question. A completed/lost game goes through its existing
   feedback/results flow; it must not request another playable question.
4. On 202, retain the pending choice and offer another recovery check. If offering
   an explicit resend, resend only the original question ID and original choice.
   Single-use server processing ensures at most one acceptance; after a resend's
   409, fetch the receipt rather than displaying the old question for another try.
5. On 404, show a clear session/recovery error and offer a new game explicitly.

If refresh recovery is included, persist the pending identifiers and choice in
browser storage before POST submission. Persist no provider secrets or hidden
correct-answer key.

## Reward reconciliation

The server issues each achievement once during the single accepted submission.
Receipt reads and duplicate/conflicting POSTs must not issue achievement events.
Angular must apply a stable achievement event ID at most once regardless of whether
feedback arrived through POST, recovery GET, duplicate recovery clicks, or reload.
For the current display-only browser totals, persist processed event IDs together
with the updated totals in one storage record, and update current-game rewards by
receipt identity instead of incrementing on each response. If storage is blocked,
keep an in-memory deduplication set and document that cross-reload totals are not
durable. A future authoritative reward balance requires server-side persistence;
local browser totals must not be treated as authoritative.

## Acceptance tests for the follow-up

- Process an answer but drop/time out its response; recover identical feedback
  without another answer submission, question generation, or category consumption.
- Concurrent original/duplicate/conflicting submissions and recovery GETs advance
  the game once; duplicate POSTs still return 409 and receipt reads are read-only.
- Exercise recovery GET both before and after the original POST reaches the lock.
  Preserve the original choice across pending recovery and any explicit resend.
- Recover wrong answers, milestone rewards, and the final bonus answer, then show
  the correct feedback/end state without replaying an answered question.
- Re-read an old receipt after later gameplay without rolling progress backward
  or replacing a newer pending question in the UI.
- Repeated recovery and refresh allocate/display each achievement once. Cover
  unavailable browser storage and expired/missing in-memory sessions.
- No correct answer is exposed for an unanswered question; all tests are offline.
