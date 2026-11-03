"""Safe quality failures shared with retry classification; never retain verdict text."""


class QualityConfigurationError(Exception):
    pass


class QualityRegenerationError(Exception):
    pass


class QualityEscalationError(Exception):
    pass


class QualityReviewFailure(Exception):
    def __init__(self, classification: str, *, retryable=False, retry_after=None):
        self.classification = classification
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__("Quality review unavailable")


class QualityArbitrationUnavailable(Exception):
    """No publishable verdict from Terra. Abort paid regeneration immediately.

    Distinct from QualityRegenerationError (a *valid* reject verdict). The
    game route may still offer a previously vetted, eligible pool question.
    """

    def __init__(self, reason: str = "unavailable"):
        # Only allow internally authored codes; never propagate provider text.
        self.reason = reason if reason in {
            "unavailable", "budget_exhausted", "invalid_stem_anchor"
        } else "unavailable"
        super().__init__("Independent question arbitration unavailable")
