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
