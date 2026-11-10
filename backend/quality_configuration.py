"""Resolve reviewer-only settings without environment or generator mutation."""

import os
from dataclasses import dataclass

from define_ai_provider import PROVIDER
from quality_errors import QualityConfigurationError
from structured_outputs import StructuredOutputSettings


@dataclass(frozen=True)
class ReviewerSettings:
    provider: str
    model_id: str | None
    structured_output_mode: str

    @classmethod
    def from_env(cls):
        provider = os.getenv("AI_QUALITY_PROVIDER")
        model = os.getenv("AI_QUALITY_MODEL_ID")
        # An explicitly empty setting is invalid too; remove both to inherit.
        if (provider is None) != (model is None):
            raise QualityConfigurationError("Reviewer provider and model must be paired")
        if provider is not None:
            if (provider not in {"openai", "nvidia", "bedrock"}
                    or not model or not model.strip() or model != model.strip()):
                raise QualityConfigurationError("Invalid reviewer configuration")
        else:
            provider = PROVIDER
        mode = os.getenv("AI_QUALITY_STRUCTURED_OUTPUT_MODE")
        settings = StructuredOutputSettings.from_env(provider, mode=mode)
        return cls(provider, model, settings.mode)
