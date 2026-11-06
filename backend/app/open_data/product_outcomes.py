"""Explicit collection non-success states; never turn absence into fabricated data."""
from __future__ import annotations

import time


class NotDisclosedError(ValueError):
    """A verified official source has not disclosed this product for the subject."""

    status = 'not_disclosed'

    def __init__(self, reason, source, evidence, *, observed_at=None, valid_until=None):
        super().__init__(reason)
        self.reason = reason
        self.source = source
        if not evidence:
            raise ValueError("Verified non-disclosure requires source evidence")
        self.evidence = evidence
        self.observed_at = time.time() if observed_at is None else observed_at
        self.valid_until = self.observed_at+86400 if valid_until is None else valid_until


class NotApplicableError(NotDisclosedError):
    """Canonical source evidence proves that the requested report period cannot exist."""

    status = 'not_applicable'
