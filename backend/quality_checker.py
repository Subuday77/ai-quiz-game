"""One independent review context per candidate; retries belong to the API loop."""

import json
import os
import re
import unicodedata
from difflib import SequenceMatcher
from time import perf_counter

from pydantic import ValidationError

from config import MAX_OUTPUT_TOKENS, TEMPERATURE
from diagnostics import event, timed_stage
from define_ai_provider import LLMClient, ProviderTimeoutBudgetError
from generation_policy import is_transient, provider_timeout, retry_after_seconds
from output_contracts import OutputContract
from provider_result import ProviderCompletionError, completed_text
from quality_configuration import ReviewerSettings
from quality_errors import (QualityConfigurationError, QualityEscalationError,
                            QualityRegenerationError, QualityReviewFailure)
from quality_schema import IssueCode, QualityMode, QualityReview, ReviewDecision
from structured_outputs import StructuredOutputConfigurationError
from question_generator import normalize_output
from question_schema import QuizQuestion

from agents.critic import QUALITY_REVIEW_PROMPT
from agents.critic_v2 import QUALITY_REVIEW_V2_PROMPT
from quality_schema_v2 import QualityReviewV2, decide_review


def _provider() -> str:
    from define_ai_provider import PROVIDER
    name = PROVIDER
    return name if name in {"openai", "nvidia", "bedrock"} else "unknown"


def quality_mode_from_env() -> QualityMode:
    try:
        return QualityMode(os.getenv("AI_QUALITY_MODE", "off"))
    except ValueError:
        event("quality_review_failed", mode="invalid", provider=_provider(),
              classification="invalid_configuration")
        raise QualityConfigurationError("Invalid quality mode") from None


def lexical_findings(question: QuizQuestion) -> list[IssueCode]:
    """Unique full-answer phrases, plus conservative long quoted near-copies."""
    def tokens(text):
        normalized = unicodedata.normalize("NFKD", text).casefold()
        normalized = "".join(char for char in normalized
                             if not unicodedata.category(char).startswith("M")
                             and unicodedata.category(char) != "Cf")
        return re.findall(r"[^\W_]+", normalized)

    def contains(haystack, needle):
        return bool(needle) and any(haystack[i:i + len(needle)] == needle
                                   for i in range(len(haystack) - len(needle) + 1))

    quoted_text = re.findall(r'"([^"\n]+)"|«([^»\n]+)»|“([^”\n]+)”', question.question)
    question_tokens = tokens(question.question)
    answer_tokens = {key: tokens(answer) for key, answer in question.answers.items()}
    correct = answer_tokens[question.correct_answer]
    quoted_answer = any(tokens(" ".join(groups)) == correct for groups in quoted_text)
    # Avoid tiny/common standalone tokens; short quoted expressions are stronger.
    substantial = (len(correct) >= 2 and sum(map(len, correct)) >= 5
                   or len(correct) == 1 and len(correct[0]) >= 4 and correct[0].isalpha()
                   or quoted_answer and sum(map(len, correct)) >= 2)
    if substantial and contains(question_tokens, correct) and not any(
            contains(question_tokens, phrase) or contains(phrase, correct)
            for key, phrase in answer_tokens.items()
            if key != question.correct_answer):
        return [IssueCode.ANSWER_LEAK]

    def words(text):
        text = unicodedata.normalize("NFKC", text).casefold()
        return [w for w in re.findall(r"[^\W\d_]+", text) if 10 <= len(w) <= 64]

    def similar(left, right):
        return abs(len(left) - len(right)) <= 2 and SequenceMatcher(
            None, left, right, autojunk=False).ratio() >= 0.9

    # Paired quotation marks only, avoiding ordinary shared topic words.
    key_words = words(question.answers[question.correct_answer])
    distractors = [word for key, text in question.answers.items()
                   if key != question.correct_answer for word in words(text)]
    for groups in quoted_text:
        for word in words(" ".join(groups)):
            if any(similar(word, answer) for answer in key_words) and not any(
                    similar(word, answer) for answer in distractors):
                return [IssueCode.ANSWER_LEAK]
    return []


def check_quality(question: QuizQuestion, requested_difficulty: int, language: str,
                  mode: QualityMode, deadline: float, timeout_seconds: float,
                  clock=perf_counter) -> None:
    fields = {"mode": mode.value, "provider": _provider(), "role": "reviewer"}
    if mode == QualityMode.OFF:
        event("quality_review_skipped", **fields, reason="disabled")
        return
    local = [code.value for code in lexical_findings(question)]
    if local and mode == QualityMode.ENFORCE:
        event("quality_review_skipped", **fields, reason="local_rejection")
        event("quality_question_rejected", **fields, decision="regenerate",
              issue_codes=local, source="local_precheck")
        raise QualityRegenerationError()
    remaining = deadline - clock()
    if remaining <= 0:
        event("quality_review_skipped", **fields, reason="budget_exhausted")
        if mode == QualityMode.AUDIT:
            return
        event("quality_review_failed", **fields, classification="budget_exhausted")
        raise QualityReviewFailure("budget_exhausted")
    start = clock()
    token = provider_timeout.set(min(timeout_seconds, remaining))
    try:
        settings = ReviewerSettings.from_env()
        fields["provider"] = settings.provider
        event("quality_review_started", **fields, local_issue_codes=local)
        with timed_stage("quality_provider_client_setup"):
            reviewer = LLMClient(settings.provider, model_id=settings.model_id,
                                 structured_output_mode=settings.structured_output_mode, role="reviewer")
        remaining = deadline - clock()
        if remaining <= 0:
            raise QualityReviewFailure("budget_exhausted")
        contract_version = os.getenv("AI_QUALITY_CONTRACT", "v1")
        if contract_version not in {"v1", "v2"}:
            raise QualityConfigurationError("Invalid quality contract version")
        is_v2 = contract_version == "v2"
        response = reviewer.responses.create(
            instructions=QUALITY_REVIEW_V2_PROMPT if is_v2 else QUALITY_REVIEW_PROMPT,
            input=json.dumps({"requested_difficulty": requested_difficulty,
                              "language": language, "candidate": question.model_dump()},
                             ensure_ascii=False),
            max_output_tokens=max(MAX_OUTPUT_TOKENS, 800) if is_v2 else MAX_OUTPUT_TOKENS,
            reasoning={"effort": "low" if is_v2 else "none"},
            temperature=TEMPERATURE,
            output_contract=(OutputContract.QUALITY_REVIEW_V2 if is_v2
                             else OutputContract.QUALITY_REVIEW),
            request_timeout=lambda: min(timeout_seconds, deadline - clock()))
        text = completed_text(response)
        with timed_stage("quality_json_parse"):
            raw = json.loads(normalize_output(text))
        with timed_stage("quality_validation"):
            verdict = (QualityReviewV2.model_validate(raw) if is_v2
                       else QualityReview.model_validate(raw))
    except Exception as exc:
        if isinstance(exc, (QualityConfigurationError, StructuredOutputConfigurationError)):
            classification = "configuration_error"
        elif isinstance(exc, (json.JSONDecodeError, ValidationError)):
            classification = "invalid_verdict"
        elif isinstance(exc, ProviderCompletionError):
            classification = "completion_" + exc.completion.value
        elif isinstance(exc, ProviderTimeoutBudgetError):
            classification = exc.reason_code
        elif isinstance(exc, QualityReviewFailure):
            classification = exc.classification
        else:
            classification = "transient_provider_failure" if is_transient(exc) else "permanent_review_failure"
        retryable = is_transient(exc)
        event("quality_review_failed", **fields, classification=classification,
              error_type=type(exc).__name__, retryable=retryable,
              duration_ms=round((clock() - start) * 1000, 3))
        if mode == QualityMode.AUDIT:
            return
        raise QualityReviewFailure(classification, retryable=retryable,
                                   retry_after=retry_after_seconds(exc)) from None
    finally:
        provider_timeout.reset(token)
    if isinstance(verdict, QualityReviewV2):
        decision, issues = decide_review(verdict, question.correct_answer)
        event("quality_review_checks", **fields, facts=verdict.facts.value,
              options=verdict.options.value, language=verdict.language.value,
              distractors=verdict.distractors.value, leakage=verdict.leakage.value,
              difficulty=verdict.difficulty.value)
    else:
        decision = verdict.decision
        issues = [code.value for code in verdict.issue_codes]
        if decision == ReviewDecision.ACCEPT and verdict.independent_answer != question.correct_answer:
            decision, issues = ReviewDecision.ESCALATE, [IssueCode.NEEDS_VERIFICATION.value]
    event("quality_review_completed", **fields, decision=decision.value, issue_codes=issues,
          local_issue_codes=local, duration_ms=round((clock() - start) * 1000, 3))
    if mode == QualityMode.AUDIT or decision == ReviewDecision.ACCEPT:
        return
    event("quality_question_rejected", **fields, decision=decision.value,
          issue_codes=issues, source="model_review")
    if decision == ReviewDecision.REGENERATE:
        raise QualityRegenerationError()
    raise QualityEscalationError()
