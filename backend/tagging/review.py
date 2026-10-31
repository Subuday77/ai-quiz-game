"""Tag-aware reviewer v4: one independent critic call for quality + semantics.

Even AI_QUALITY_MODE=off cannot skip mandatory tags/fact/duplicate checks
when the tagged workflow is enabled. Python's deterministic rejection is
always earlier and cannot be overridden by this reviewer.
"""
import json
from time import perf_counter

from pydantic import ValidationError

from config import MAX_OUTPUT_TOKENS, TEMPERATURE
from diagnostics import event, timed_stage
from define_ai_provider import LLMClient
from generation_policy import is_transient, provider_timeout, retry_after_seconds
from output_contracts import OutputContract
from provider_result import ProviderCompletionError, completed_text
from quality_configuration import ReviewerSettings
from quality_errors import QualityConfigurationError, QualityRegenerationError, QualityReviewFailure
from quality_schema import QualityMode
from quality_schema_v2 import CheckStatus
from quality_schema_v3 import QualityReviewV3, audit_score
from question_generator import normalize_output
from tagging.contract import TaggedQuestion

from agents.critic_v3 import QUALITY_REVIEW_V3_PROMPT

TAGGED_REVIEW_PROMPT = QUALITY_REVIEW_V3_PROMPT + """
FINAL V4 CONTRACT OVERRIDE:
Keep all fields required in the v3 JSON review schema. ALSO return
tags_valid: pass|fail|uncertain
fact_valid: pass|fail|uncertain
semantic_duplicate: boolean
duplicate_match: string (previous question_id, or empty if no duplication).

Review ALL generated entity, concept and keyword tags against the question
and the actual independently determined correct answer. They must be in
canonical English kebab-case and accurately describe the question, not
generic filler. Check the fact.subject, fact.relation, fact.object triple:
- The relation MUST uniquely name the specific fact/property tested, e.g.
  'birth-year', not generic 'date' or 'identity'.
- The subject must be the same real-world entity across languages; do not
  accept made-up or ambiguous subject/relation labels.
- The object must correctly describe the true answer.
- If any metadata is incorrect, unsupported or ambiguous, mark fail/uncertain.
- Compare the candidate with the related *prior user questions*. Different
  questions about the SAME ENTITY are allowed if they test different facts.
  Different phrasing/languages of the SAME KNOWLEDGE are duplicates.
- Set semantic_duplicate=true only when the requested knowledge is the same,
  and specify the matching question_id as duplicate_match.
- Do not conceal uncertainty or claim unrelated facts are duplicates merely
  because a topic/entity appears in both.
- Never expose these tags, facts or reviews to the player.
The Python verifier's deterministic rejection takes precedence over this
review, even if you think a question is new.
Return only the strict v4 JSON object. Do not add a model-computed total.
"""


class TaggedReview(QualityReviewV3):
    tags_valid: CheckStatus
    fact_valid: CheckStatus
    semantic_duplicate: bool
    duplicate_match: str


def check_tagged_quality(question: TaggedQuestion, *, language: str,
                         requested_difficulty: int, category: str,
                         neighbors: list[dict], mode: QualityMode,
                         deadline: float, timeout_seconds: float,
                         clock=perf_counter) -> None:
    """Raises to trigger the shared bounded generation retry loop.

    Metadata validation and semantic duplicates are hard failures in EVERY
    quality mode. Non-tag editorial checks honor the existing mode.
    """
    remaining = deadline - clock()
    if remaining <= 0:
        raise QualityReviewFailure("tag_review_budget_exhausted", retryable=False)
    settings = ReviewerSettings.from_env()
    token = provider_timeout.set(min(timeout_seconds, remaining))
    try:
        reviewer = LLMClient(settings.provider, model_id=settings.model_id,
                             structured_output_mode=settings.structured_output_mode,
                             role="reviewer")
        with timed_stage("tagged_quality_review"):
            response = reviewer.responses.create(
                instructions=TAGGED_REVIEW_PROMPT,
                input=json.dumps({
                    "requested_difficulty": requested_difficulty,
                    "language": language, "category": category,
                    "candidate": question.model_dump(),
                    "prior_user_questions": neighbors,
                }, ensure_ascii=False),
                max_output_tokens=max(MAX_OUTPUT_TOKENS, 1600),
                reasoning={"effort": "low"}, temperature=TEMPERATURE,
                output_contract=OutputContract.TAGGED_REVIEW,
                request_timeout=lambda: min(timeout_seconds, deadline - clock()),
            )
            raw = json.loads(normalize_output(completed_text(response)))
            verdict = TaggedReview.model_validate(raw)
    except Exception as exc:
        if isinstance(exc, (json.JSONDecodeError, ValidationError, ProviderCompletionError)):
            raise QualityReviewFailure("invalid_tag_review", retryable=False) from None
        if isinstance(exc, QualityConfigurationError):
            raise
        raise QualityReviewFailure("tag_review_unavailable", retryable=is_transient(exc),
                                   retry_after=retry_after_seconds(exc)) from None
    finally:
        provider_timeout.reset(token)

    event("tagged_review_completed", tags=verdict.tags_valid.value,
          fact=verdict.fact_valid.value, semantic_duplicate=verdict.semantic_duplicate,
          general_mode=mode.value)
    if (verdict.tags_valid != CheckStatus.PASS
            or verdict.fact_valid != CheckStatus.PASS
            or verdict.semantic_duplicate):
        raise QualityRegenerationError()

    # The v3 editorial numeric score remains an AUDIT/diagnostic signal.
    assessment = audit_score(verdict, question.correct_answer)
    event("tagged_review_score", score=assessment["total"],
          critical=assessment["critical"], mode=mode.value)
    if mode == QualityMode.ENFORCE and assessment["critical"] != "none":
        raise QualityRegenerationError()
