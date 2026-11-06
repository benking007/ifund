"""Strict source financial number parsing with finite-value checks."""
from __future__ import annotations

from decimal import Decimal
import math


def numeric(value):
    """Parse documented comma, currency and percent renderings; preserve units."""
    if value is None or str(value).strip() in ("", "-", "--", "---", "—"):
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean financial observation")
    text = str(value).strip().replace(",", "").removeprefix("￥").removesuffix("%")
    number = Decimal(text)
    if not number.is_finite() or not math.isfinite(float(number)):
        raise ValueError("Nonfinite financial observation")
    return float(number)
