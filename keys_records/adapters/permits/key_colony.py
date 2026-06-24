"""
City of Key Colony Beach permits adapter.

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
        source="Key Colony Beach Building Dept",
        reason="No public online permit portal available for City of Key Colony Beach",
        contact="600 W Ocean Drive, Key Colony Beach, FL 33051 | 305-289-0247",
        url=None,
    )
