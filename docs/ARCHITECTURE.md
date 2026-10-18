# How the first web version works

This page is part of the learning track. It describes only what the current branch actually implements.

## First request

~~~text
Angular (localhost:4200)
    |
    | POST /api/games  {"language": "English"}
    v
Angular development proxy
    |
    v
FastAPI (localhost:8000)
    |
    | creates GameState and a random list of unused categories
    | keeps them in a Python dictionary keyed by a random UUID
    v
201 {"game_id": "...", "phase": "main", ...}
~~~

The game ID identifies a temporary session, *not* an authenticated user. This must be replaced before publication.

## Getting a question

~~~text
Angular -> GET /api/games/{id}/question
FastAPI -> current GameState difficulty and unused category
        -> question_generator -> LLM provider
        <- generated JSON with the correct answer
FastAPI -> Pydantic QuizQuestion validation
        -> store the question and its correct answer IN MEMORY
        -> return PublicQuestion (question ID, number, text, four choices)
Angular <- public question; correct_answer is not included
~~~

Validation checks JSON shape, four unique non-empty answers, an A–D key, and the requested difficulty. Invalid model output is retried three times; an API/network exception stops without replaying an invalid or unauthorized request. **This validates structure, not historical truth**. Factual verification and smarter retry policies are future work.

The current GET returns the same question ID until it is answered. Browser refreshes therefore do not spend an additional AI call for that unanswered question (as long as the backend process is running).

## Submitting an answer

~~~text
Angular -> POST /api/games/{id}/answer
           {"question_id": "...", "answer": "B"}
FastAPI -> verify question ID belongs to active session
        -> compare the chosen letter with the saved correct_answer
        -> GameState.submit_answer(correct)
        -> award star at the milestone, or end the game
        -> erase the answered question's pending ID
Angular <- feedback including the correct answer AFTER the answer is submitted
~~~

The question ID is single-use: sending an old answer twice returns HTTP 409, not a second reward. No second AI request is needed to decide correctness.

## Why the game engine is separate

game_logic.py contains no Angular, FastAPI, database or AI code. Its job is to enforce rules: 15 levels, awards at 5/10/15, optional secret 16th on no-hint runs, gold retained if bonus missed. Tests can run this logic without an API key.

question_schema.py sets contracts for the AI and browser. The answer key exists in QuizQuestion but not PublicQuestion. This boundary matters more than Base64 or hiding data in HTTP headers.

## Known current gaps

- No authentication or persistent storage.
- No rate limits, spending quotas or AI verification beyond shape validation.
- Hints are not yet implemented, so all perfect runs qualify for the 16th.
- Achievement records exist only in memory until database support.
- Backend does not currently serve the Angular production build; use the local dev server and proxy.
- Native model-specific structured-output and stop-reason handling remain for the next AI Engine lessons.
