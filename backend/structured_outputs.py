"""Explicit, default-off native schema requests. No provider/model fallback."""

import json
import os
from dataclasses import dataclass
from typing import Literal

from output_contracts import OutputContract, transport_contract


class StructuredOutputConfigurationError(Exception):
    """A permanent configuration error with a safe diagnostic code."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__("Unsupported structured-output configuration")


@dataclass(frozen=True)
class StructuredOutputSettings:
    mode: Literal["off", "strict"]
    output_format: str

    @property
    def requested(self) -> bool:
        return self.mode == "strict"

    @classmethod
    def from_env(cls, provider: str, *, mode: str | None = None):
        if mode is None:
            mode = os.getenv("AI_STRUCTURED_OUTPUT_MODE", "off")
        if mode not in ("off", "strict"):
            raise StructuredOutputConfigurationError("invalid_mode")
        if mode == "off":
            return cls("off", "text")
        formats = {"openai": "text.format.json_schema", "bedrock": "outputConfig.textFormat.json_schema"}
        if provider == "nvidia":
            selected = os.getenv("NVIDIA_STRUCTURED_OUTPUT_FORMAT", "guided_json")
            if selected not in ("guided_json", "json_schema"):
                raise StructuredOutputConfigurationError("invalid_nvidia_format")
            return cls("strict", selected)
        if provider not in formats:
            raise StructuredOutputConfigurationError("unsupported_provider")
        return cls("strict", formats[provider])

    def request_parameters(self, provider: str, client,
                           output_contract: OutputContract = OutputContract.QUESTION) -> dict:
        try:
            contract = OutputContract(output_contract)
        except ValueError:
            raise StructuredOutputConfigurationError("invalid_output_contract") from None
        # Keep NVIDIA's working chat-template extension in both modes.
        params = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}} if provider == "nvidia" else {}
        if not self.requested:
            return params
        schema_name, schema = transport_contract(contract)
        if provider == "openai":
            return {"text": {"format": {"type": "json_schema", "name": schema_name,
                                        "strict": True, "schema": schema}}}
        if provider == "nvidia":
            if self.output_format == "guided_json":
                params["extra_body"]["guided_json"] = schema
            else:
                # NVIDIA's documented JSON Schema form does not require an
                # additional OpenAI strict flag; model/backend support varies.
                params["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": schema_name, "schema": schema}}
            return params
        if provider == "bedrock":
            ensure_bedrock_schema_support(client)
            return {"outputConfig": {"textFormat": {"type": "json_schema", "structure": {
                "jsonSchema": {"name": schema_name, "schema": json.dumps(schema, sort_keys=True, separators=(",", ":"))}}}}}
        raise StructuredOutputConfigurationError("unsupported_provider")


def ensure_bedrock_schema_support(client) -> None:
    """Inspect SDK service metadata only; never query a remote model for support."""
    try:
        shape = client.meta.service_model.operation_model("Converse").input_shape
        for name in ("outputConfig", "textFormat", "structure", "jsonSchema"):
            shape = shape.members[name]
        if shape.members["schema"].type_name != "string" or "name" not in shape.members:
            raise KeyError("schema")
    except (AttributeError, KeyError, TypeError):
        raise StructuredOutputConfigurationError("bedrock_sdk_unsupported") from None
