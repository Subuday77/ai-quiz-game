import os
from types import SimpleNamespace
from typing import Any

import boto3
from openai import OpenAI

from consts import (LLM_PROVIDER, OPENAI, OPENAI_API_KEY, NVIDIA, BEDROCK, TEXT, ROLE, USER, SYSTEM, CONTENT,
                    OPENAI_MODEL_ID, NVIDIA_API_KEY, NVIDIA_MODEL_ID, BEDROCK_RUNTIME, AWS_REGION, BEDROCK_MODEL_ID,
                    UNKNOWN_PROVIDER, MAX_TOKENS, TEMPERATURE, OUTPUT, MESSAGE, MAX_RETRIES)

PROVIDER = os.getenv(LLM_PROVIDER, OPENAI).lower()


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

        if provider == OPENAI:
            self.client = OpenAI(
                api_key=os.environ[OPENAI_API_KEY]
            )
            self.model = os.environ[OPENAI_MODEL_ID]

        elif provider == NVIDIA:
            self.client = OpenAI(
                base_url="https://integrate.api.nvidia.com/v1",
                api_key=os.environ[NVIDIA_API_KEY],
                max_retries=MAX_RETRIES
            )
            self.model = os.environ[NVIDIA_MODEL_ID]

        elif provider == BEDROCK:
            self.client = boto3.client(BEDROCK_RUNTIME, region_name=os.environ[AWS_REGION])
            self.model = os.environ[BEDROCK_MODEL_ID]

        else:
            raise ValueError(f"{UNKNOWN_PROVIDER}: {provider}")

    def create(
            self,
            *,
            model=None,
            instructions,
            input,
            max_output_tokens=500,
            reasoning=None,
            temperature=None,
    ) -> Any:
        """
        Create an LLM response through the configured provider.
        :param model: Optional model argument kept for API compatibility.
        :param instructions: System instructions for the model.
        :param input: User input sent to the model.
        :param max_output_tokens: Maximum number of output tokens to request.
        :param reasoning: Optional reasoning configuration for providers that support it.
        :param temperature: Optional sampling temperature.
        :return: Response object with an output_text attribute or provider-native response.
        """
        if self.provider == OPENAI:
            return self.client.responses.create(
                model=self.model,
                instructions=instructions,
                input=input,
                max_output_tokens=max_output_tokens,
                reasoning=reasoning,
                temperature=temperature,
            )

        if self.provider == NVIDIA:
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
                extra_body={
                    "chat_template_kwargs": {
                        "enable_thinking": False
                    }
                }
            )

            return SimpleNamespace(
                output_text=normalize_output(
                    response.choices[0].message.content
                )
            )

        if self.provider == BEDROCK:
            inference_config = {
                MAX_TOKENS: max_output_tokens
            }

            if temperature is not None:
                # Bedrock Converse accepts temperature only from 0 to 1.
                inference_config[TEMPERATURE] = min(temperature, 1.0)

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
            )

            text = response[OUTPUT][MESSAGE][CONTENT][0][TEXT]

            return SimpleNamespace(
                output_text=normalize_output(text)
            )


def normalize_output(text: str) -> str:
    """
    Normalize model output by removing optional Markdown code fences.
    :param text: Raw text returned by the model.
    :return: Cleaned text content.
    """
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()

        # Remove opening ```json / ```
        lines = lines[1:]

        # Remove closing ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        text = "\n".join(lines).strip()

    return text


def get_client() -> LLMClient:
    """
    Build an LLM client from the configured provider environment value.
    :return: Configured LLMClient instance.
    """
    return LLMClient(provider=PROVIDER)
