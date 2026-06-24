from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..polite_client import PoliteClient
from ..schemas import Provenance


class BaseAdapter:
    source_name: str = "unknown"
    source_url: str = ""

    def __init__(self, client: PoliteClient, cache_root: Path):
        self.client = client
        self.cache_root = Path(cache_root)

    def _provenance(self, url: str = "", cache_path: str = "") -> Provenance:
        return Provenance(
            source_name=self.source_name,
            source_url=url or self.source_url,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            cache_path=cache_path,
        )

    def _snap(self, page, source: str, query: str) -> Path:
        html = page.content()
        return self.client.save_snapshot(source, query, html, "html")

    def _safe_text(self, el) -> Optional[str]:
        if el is None:
            return None
        if hasattr(el, "inner_text"):
            try:
                return el.inner_text().strip() or None
            except Exception:
                return None
        text = el.get_text(separator=" ", strip=True)
        return text or None

    def _parse_money(self, s: Optional[str]) -> Optional[float]:
        if not s:
            return None
        cleaned = re.sub(r"[^0-9.]", "", s.replace(",", ""))
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _parse_int(self, s: Optional[str]) -> Optional[int]:
        v = self._parse_money(s)
        return int(v) if v is not None else None

    def _parse_year(self, s: Optional[str]) -> Optional[int]:
        if not s:
            return None
        m = re.search(r"\b(19|20)\d{2}\b", s)
        return int(m.group(0)) if m else None
