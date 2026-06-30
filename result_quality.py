"""
Lifecycle result quality — decide if extracted fields are useful enough to return.
"""

import os
from typing import Optional

from models import ProductLifecycle


def lifecycle_fields_present(data: ProductLifecycle) -> int:
    """Count populated lifecycle parameters the user cares about."""
    n = 0
    if data.end_of_support:
        n += 1
    if data.end_of_extended_support:
        n += 1
    if data.end_of_life:
        n += 1
    if data.support_policy_relative:
        n += 1
    if data.estimated_end_of_support:
        n += 1
    if data.release_date:
        n += 1
    if data.licensing_model:
        n += 1
    if data.chargeability_type:
        n += 1
    return n


def has_minimum_lifecycle_data(data: ProductLifecycle) -> bool:
    """At least one core lifecycle signal (not licensing-only)."""
    return bool(
        data.end_of_support
        or data.end_of_extended_support
        or data.end_of_life
        or data.support_policy_relative
        or data.estimated_end_of_support
    )


def partial_confidence_threshold() -> float:
    try:
        return float(os.getenv("PARTIAL_CONFIDENCE_THRESHOLD", "0.42"))
    except ValueError:
        return 0.42


def accept_as_found(data: ProductLifecycle, strict_threshold: float) -> bool:
    """
    Strict: confidence >= threshold (default 0.55).
    Partial: lower threshold if at least one lifecycle date/policy field exists.
    """
    if data.confidence >= strict_threshold:
        return True
    if not has_minimum_lifecycle_data(data):
        return False
    min_fields = max(1, int(os.getenv("PARTIAL_MIN_FIELDS", "1")))
    if lifecycle_fields_present(data) < min_fields:
        return False
    return data.confidence >= partial_confidence_threshold()


def annotate_partial(data: ProductLifecycle) -> ProductLifecycle:
    note = "[partial: some lifecycle fields found below full confidence threshold]"
    if data.lifecycle_notes:
        if note not in data.lifecycle_notes:
            data.lifecycle_notes = f"{data.lifecycle_notes} {note}"
    else:
        data.lifecycle_notes = note
    return data
