from __future__ import annotations

from datetime import date


def format_internal_reference(sequence: int, *, year: int | None = None) -> str:
    """Format the readable, immutable reference assigned at publication.

    The future publisher can safely use a persisted draft ID as ``sequence``.
    """
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise ValueError("sequence must be a positive integer")
    reference_year = date.today().year if year is None else year
    if not isinstance(reference_year, int) or not 1000 <= reference_year <= 9999:
        raise ValueError("year must be a four-digit integer")
    return f"BL-{reference_year}-{sequence:06d}"
