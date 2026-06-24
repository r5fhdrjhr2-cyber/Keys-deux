"""
City of Layton permits adapter.

No online portal available — raises ManualRetrievalRequired immediately.
"""
from __future__ import annotations

from pathlib import Path

from ...polite_client import PoliteClient, ManualRetrievalRequired
from ...schemas import PermitRecord


def fetch(
    client: PoliteClient,
    address: str,
    parcel_id: str,
    cache_root: Path,
) -> list:
    """No online portal — raises ManualRetrievalRequired."""
    raise ManualRetrievalRequired(
        source="City of Layton",
        reason="No public online permit portal for City of Layton",
        contact="City Hall 305-664-4667",
        url=None,
    )
