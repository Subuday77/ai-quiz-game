import math
import os
from importlib.metadata import version
from typing import Any, Callable

import boto3
from openai import APIStatusError, OpenAI
from botocore.config import Config
from botocore.exceptions import ClientError, ParamValidationError

from generation_policy import GenerationPolicy, is_transient, provider_timeout

from diagnostics import event, timed_stage
from provider_result import Completion, ProviderResult
from output_contracts import OutputContract
from structured_outputs import StructuredOutputConfigurationError, StructuredOutputSettings

from consts import (LLM_PROVIDER, OPENAI, OPENAI_API_KEY, NVIDIA, BEDROCK, TEXT, ROLE, USER, SYSTEM, CONTENT,
                    OPENAI_MODEL_ID, NVIDIA_API_KEY, NVIDIA_MODEL_ID, BEDROCK_RUNTIME, AWS_REGION, BEDROCK_MODEL_ID,
                    UNKNOWN_PROVIDER, MAX_TOKENS, TEMPERATURE)

PROVIDER = os.getenv(LLM_PROVIDER, OPENAI).lower()


class ProviderTimeoutBudgetError(Exception):
    """No request may start with socket limits exceeding its current allocation."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__("Provider timeout allocation unavailable")


class LLMClient:
    def __init__(self, provider: str) -> None:
        """
        Initialize a provider-specific LLM client.
        :param provider: Provider identifier to configure: openai, nvidia, or bedrock.
        :return: None.
        Raise:
            ValueError: If the provider is not supported.
            KeyError: If a required environment variable is missing.
        """
        self.provider = provider
        self.responses = self
        timeout = provider_timeout.get()
        if timeout is None:
            timeout = GenerationPolicy.from_env().provider_timeout_seconds

        if provider == OPENAI:
            self.client = OpenAI(
                api_key=os.environ[OPENAI_API_KEY],
                timeout=timeout, max_retries=0
            )
            self.model = os.environ[OPENAI_MODEL_ID]

        elif provider == NVIDIA:
            self.client = OpenAI(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key=os.environ[NVIDIA_API_KEY],
                max_retries=0, timeout=timeout
            )
            self.model = os.environ[NVIDIA_MODEL_ID]

        elif provider == BEDROCK:
            self.client = boto3.client(
                BEDROCK_RUNTIME, region_name=os.environ[AWS_REGION],
                config=Config(connect_timeout=min(3.0, timeout), read_timeout=timeout,
                              retries={"mode": "standard", "total_max_attempts": 1}),
            )
            self.model = os.environ[BEDROCK_MODEL_ID]

        else:
            raise ValueError(f"{UNKNOWN_PROVIDER}: {provider}")

        # Log the resolved policy without credentials or request contents.
        if provider in (OPENAI, NVIDIA):
            timeout = self.client.timeout
            # SDK versions may retain a scalar rather than a Timeout object.
            # Normalize only diagnostic fields; leave the transport policy intact.
            socket_names = ("connect", "read", "write", "pool")
            timeout_seconds = (
                dict.fromkeys(socket_names, timeout)
                if isinstance(timeout, (int, float)) or timeout is None
                else {name: getattr(timeout, name) for name in socket_names}
            )
            event("provider_configuration", provider=provider,
                  sdk_version=version("openai"),
                  sdk_max_retries=self.client.max_retries,
                  timeout_seconds=timeout_seconds)
        elif provider == BEDROCK:
            config = self.client.meta.config
            retries = config.retries or {}
            event("provider_configuration", provider=provider,
                  sdk_version=version("boto3"), botocore_version=version("botocore"),
                  connect_timeout_seconds=config.connect_timeout,
                  read_timeout_seconds=config.read_timeout,
                  retry_mode=retries.get("mode"),
                  sdk_total_max_attempts=retries.get("total_max_attempts"),
                  sdk_max_attempts=retries.get("max_attempts"))

    def create(
            self, *, model=None, instructions, input, max_output_tokens=500,
            reasoning=None, temperature=None, output_contract: OutputContract = OutputContract.QUESTION,
            request_timeout: Callable[[], float] | None = None,
    ) -> ProviderResult:
        """Request optional native schema output, preserving completion normalization."""
        settings = None
        try:
            settings = StructuredOutputSettings.from_env(self.provider)
            parameters = settings.request_parameters(self.provider, self.client, output_contract)
        except StructuredOutputConfigurationError as exc:
            event("structured_output_failed", provider=self.provider,
                  native_requested=os.getenv("AI_STRUCTURED_OUTPUT_MODE", "off") == "strict",
                  output_format=settings.output_format if settings else None,
                  classification="configuration_error", reason_code=exc.reason_code)
            raise
        fields = {"provider": self.provider, "native_requested": settings.requested,
                  "output_format": settings.output_format,
                  "output_contract": OutputContract(output_contract).value}
        event("structured_output_request", **fields)
        try:
            result = self._create(
                model=model, instructions=instructions, input=input,
                max_output_tokens=max_output_tokens, reasoning=reasoning,
                temperature=temperature, structured_parameters=parameters, request_timeout=request_timeout,
            )
        except Exception as exc:
            status = exc.status_code if isinstance(exc, APIStatusError) else None
            rejected = status in (400, 422) or (
                isinstance(exc, ClientError) and
                exc.response.get("Error", {}).get("Code") == "ValidationException")
            classification = ("provider_request_rejected" if rejected else
                              "sdk_request_rejected" if isinstance(exc, (TypeError, ParamValidationError)) else
                              "transient_failure" if is_transient(exc) else "permanent_failure")
            event("structured_output_failed", **fields, classification=classification,
                  error_type=type(exc).__name__, status_code=status)
            raise
        event("structured_output_result", **fields, classification=result.completion.value)
        return result

    def _create(
            self,
            *,
            model=None,
            instructions,
            input,
            max_output_tokens=500,
            reasoning=None,
            temperature=None,
            structured_parameters: dict,
            request_timeout: Callable[[], float] | None = None,
    ) -> ProviderResult:
        """
        Create an LLM response through the configured provider.
        :param model: Optional model argument kept for API compatibility.
        :param instructions: System instructions for the model.
        :param input: User input sent to the model.
        :param max_output_tokens: Maximum number of output tokens to request.
        :param reasoning: Optional reasoning configuration for providers that support it.
        :param temperature: Optional sampling temperature.
        :return: Provider-independent text and completion metadata.
        """
        if self.provider == OPENAI:
            with timed_stage("provider_request", provider=self.provider):
                timeout_parameters = self._timeout_parameters(request_timeout)
                response = self.client.responses.create(
                    model=self.model,
                    instructions=instructions,
                    input=input,
                    max_output_tokens=max_output_tokens,
                    reasoning=reasoning,
                    temperature=temperature,
                    **structured_parameters,
                    **timeout_parameters,
                )
            return self._record_completion(normalize_openai_response(response))

        if self.provider == NVIDIA:
            with timed_stage("provider_request", provider=self.provider):
                timeout_parameters = self._timeout_parameters(request_timeout)
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            ROLE: SYSTEM,
                            CONTENT: instructions
                        },
                        {
                            ROLE: USER,
                            CONTENT: input
                        }
                    ],
                    max_completion_tokens=max_output_tokens,
                    temperature=temperature,
                    **structured_parameters,
                    **timeout_parameters,
                )

            return self._record_completion(normalize_nvidia_response(response))

        if self.provider == BEDROCK:
            inference_config = {
                MAX_TOKENS: max_output_tokens
            }

            if temperature is not None:
                # Bedrock Converse accepts temperature only from 0 to 1.
                inference_config[TEMPERATURE] = min(temperature, 1.0)

            with timed_stage("provider_request", provider=self.provider):
                timeout_parameters = self._timeout_parameters(request_timeout)
                response = self.client.converse(
                    modelId=self.model,
                    system=[
                        {
                            TEXT: instructions
                        }
                    ],
                    messages=[
                        {
                            ROLE: USER,
                            CONTENT: [
                                {
                                    TEXT: input
                                }
                            ]
                        }
                    ],
                    inferenceConfig=inference_config,
                    **structured_parameters,
                    **timeout_parameters,
                )

            return self._record_completion(normalize_bedrock_response(response))

    def _timeout_parameters(self, allocation: Callable[[], float] | None) -> dict:
        """Evaluate at dispatch, after client/schema setup. Never mutate SDK internals."""
        if allocation is None:
            return {}
        seconds = allocation()
        if not math.isfinite(seconds) or seconds <= 0:
            raise ProviderTimeoutBudgetError("budget_exhausted")
        if self.provider in (OPENAI, NVIDIA):
            # Both SDK resource methods pass this override to the HTTP transport.
            event("provider_timeout_allocated", provider=self.provider,
                  timeout_seconds=seconds, application="request_override")
            return {"timeout": seconds}
        # Converse has no request-level socket override. Config alone must not be
        # mutated: Botocore copied these limits into its transport at construction.
        config = self.client.meta.config
        limits = (config.connect_timeout, config.read_timeout)
        if any(not isinstance(value, (int, float)) or not math.isfinite(value)
               or value <= 0 or value > seconds for value in limits):
            event("provider_timeout_unavailable", provider=self.provider,
                  allocation_seconds=seconds, reason="configured_timeout_exceeds_budget")
            raise ProviderTimeoutBudgetError("timeout_allocation_unavailable")
        event("provider_timeout_allocated", provider=self.provider,
              allocation_seconds=seconds, connect_timeout_seconds=config.connect_timeout,
              read_timeout_seconds=config.read_timeout, application="existing_socket_limits")
        return {}

    def _record_completion(self, result: ProviderResult) -> ProviderResult:
        event("provider_completion", provider=self.provider,
              completion=result.completion.value, provider_reason=result.provider_reason)
        return result


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _items(value: Any) -> list | tuple:
    return value if isinstance(value, (list, tuple)) else []


def _reason(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def normalize_openai_response(response: Any) -> ProviderResult:
    status = _reason(_field(response, "status"))
    incomplete = _reason(_field(_field(response, "incomplete_details"), "reason"))
    reason = incomplete if incomplete is not None else status
    texts = []
    refused = tool_call = unexpected = unfinished_message = False
    # Output discriminators from the Responses API, not invented finish reasons.
    tool_types = {
        "function_call", "custom_tool_call", "computer_call", "web_search_call",
        "file_search_call", "code_interpreter_call", "image_generation_call",
        "local_shell_call", "shell_call", "apply_patch_call", "mcp_call",
        "tool_search_call", "mcp_approval_request",
        "function_call_output", "custom_tool_call_output", "computer_call_output",
        "tool_search_output", "local_shell_call_output", "shell_call_output",
        "apply_patch_call_output", "mcp_list_tools", "mcp_approval_response",
        "program", "program_output", "additional_tools",
    }
    for item in _items(_field(response, "output")):
        kind = _field(item, "type")
        if kind == "message":
            message_status = _field(item, "status")
            if message_status != "completed":
                unfinished_message = True
            for part in _items(_field(item, "content")):
                part_type = _field(part, "type")
                if part_type == "refusal":
                    refused = True
                elif part_type == "output_text":
                    text = _field(part, "text")
                    if isinstance(text, str):
                        texts.append(text)
                else:
                    unexpected = True
        elif kind in tool_types:
            tool_call = True
        elif kind != "reasoning":
            unexpected = True
    if incomplete == "content_filter":
        completion = Completion.BLOCKED
    elif refused:
        completion = Completion.REFUSED
    elif tool_call:
        completion = Completion.TOOL_CALL
    elif incomplete == "max_output_tokens":
        completion = Completion.TRUNCATED
    elif status == "completed" and incomplete is None and not (unexpected or unfinished_message):
        completion = Completion.COMPLETE
    else:
        completion = Completion.UNKNOWN
    return ProviderResult("".join(texts) if texts else None, completion, reason)


def normalize_nvidia_response(response: Any) -> ProviderResult:
    choices = _items(_field(response, "choices"))
    choice = choices[0] if choices else None
    reason = _reason(_field(choice, "finish_reason"))
    message = _field(choice, "message")
    text = _field(message, "content")
    completion = {
        "stop": Completion.COMPLETE,
        "length": Completion.TRUNCATED,
        "content_filter": Completion.BLOCKED,
        "tool_calls": Completion.TOOL_CALL,
        "function_call": Completion.TOOL_CALL,
    }.get(reason, Completion.UNKNOWN)
    if completion != Completion.BLOCKED:
        if isinstance(_field(message, "refusal"), str):
            completion = Completion.REFUSED
        elif _field(message, "tool_calls") or _field(message, "function_call") is not None:
            completion = Completion.TOOL_CALL
    return ProviderResult(text if isinstance(text, str) else None, completion, reason)


def normalize_bedrock_response(response: Any) -> ProviderResult:
    reason = _reason(_field(response, "stopReason"))
    completion = {
        "end_turn": Completion.COMPLETE,
        "stop_sequence": Completion.COMPLETE,
        "max_tokens": Completion.TRUNCATED,
        "model_context_window_exceeded": Completion.TRUNCATED,
        "guardrail_intervened": Completion.BLOCKED,
        "content_filtered": Completion.BLOCKED,
        "tool_use": Completion.TOOL_CALL,
        "malformed_tool_use": Completion.TOOL_CALL,
    }.get(reason, Completion.UNKNOWN)
    message = _field(_field(response, "output"), "message")
    texts = []
    for block in _items(_field(message, "content")):
        text = _field(block, "text")
        if isinstance(text, str):
            texts.append(text)
        if isinstance(block, dict) and "toolUse" in block and completion != Completion.BLOCKED:
            completion = Completion.TOOL_CALL
    return ProviderResult("".join(texts) if texts else None, completion, reason)


def get_client() -> LLMClient:
    """
    Build an LLM client from the configured provider environment value.
    :return: Configured LLMClient instance.
    """
    return LLMClient(provider=PROVIDER)
