"""
City of Marathon permits adapter.

URL: https://marathonfl.viewpointcloud.com
Platform: ViewPointCloud
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from ...polite_client import PoliteClient
from ...schemas import PermitRecord, Provenance

logger = logging.getLogger("adapter.permits.marathon")

BASE_URL = "https://marathonfl.viewpointcloud.com"
SOURCE_NAME = "ViewPointCloud"
JURISDICTION = "City of Marathon"


def _parse_permit_list(soup: BeautifulSoup, page_url: str) -> list:
    permits = []
    # ViewPointCloud renders records in divs/cards or tables
    keywords = ["PERMIT", "STATUS", "APPLIED", "RECORD TYPE"]
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).upper() for th in table.find_all("th")]
        if not any(k in " ".join(headers) for k in keywords):
            continue
        col_map = {h: i for i, h in enumerate(headers)}

        def _gc(cells, key):
            for k in col_map:
                if key in k:
                    idx = col_map[k]
                    if idx < len(cells):
                        return cells[idx]
            return None

        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
            if not cells:
                continue
            permit_num = _gc(cells, "RECORD") or _gc(cells, "PERMIT") or (cells[0] if cells else "UNKNOWN")
            permits.append(PermitRecord(
                jurisdiction=JURISDICTION,
                source_system=SOURCE_NAME,
                permit_number=permit_num,
                permit_type=_gc(cells, "TYPE") or _gc(cells, "RECORD TYPE"),
                subtype=_gc(cells, "SUBTYPE"),
                description=_gc(cells, "DESCRIPTION"),
                status=_gc(cells, "STATUS"),
                applied_date=_gc(cells, "APPLIED") or _gc(cells, "SUBMITTED"),
                issued_date=_gc(cells, "ISSUED"),
                finaled_date=_gc(cells, "FINAL"),
                expiration_date=_gc(cells, "EXPIR"),
                contractor_name=_gc(cells, "CONTRACTOR"),
                contractor_license=_gc(cells, "LICENSE"),
                declared_value=None,
                inspections=[],
                provenance=Provenance(
                    source_name=SOURCE_NAME,
                    source_url=page_url,
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    cache_path="",
                ),
            ))

    # Also try to parse card-style layouts
    if not permits:
        for card in soup.find_all(class_=re.compile(r"record|permit|card", re.IGNORECASE)):
            text = card.get_text(separator="\n", strip=True)
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if not lines:
                continue
            permit_num = lines[0]
            desc = lines[1] if len(lines) > 1 else None
            permits.append(PermitRecord(
                jurisdiction=JURISDICTION,
                source_system=SOURCE_NAME,
                permit_number=permit_num,
                permit_type=None,
                subtype=None,
                description=desc,
                status=None,
                applied_date=None,
                issued_date=None,
                finaled_date=None,
                expiration_date=None,
                contractor_name=None,
                contractor_license=None,
                declared_value=None,
                inspections=[],
                provenance=Provenance(
                    source_name=SOURCE_NAME,
                    source_url=page_url,
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    cache_path="",
                ),
            ))
    return permits


def fetch(
    client: PoliteClient,
    address: str,
    parcel_id: str,
    cache_root: Path,
) -> list:
    """Fetch permits from City of Marathon ViewPointCloud."""
    cache_key = parcel_id or address
    cached_html = client.load_snapshot("marathon_permits", cache_key)
    if cached_html:
        logger.info("Cache hit for marathon_permits/%s", cache_key)
        soup = BeautifulSoup(cached_html, "lxml")
        return _parse_permit_list(soup, BASE_URL)

    permits = []
    try:
        page = client.navigate(BASE_URL)

        # ViewPointCloud: look for a search bar
        search_term = address or parcel_id
        input_locators = [
            page.locator("input[type='search']"),
            page.locator("input[placeholder*='search' i]"),
            page.locator("input[placeholder*='address' i]"),
            page.locator("input[name*='search' i]"),
            page.locator("input[type='text']").first,
        ]
        search_input = None
        for loc in input_locators:
            try:
                if loc.count() > 0:
                    search_input = loc.first
                    break
            except Exception:
                pass

        if not search_input:
            logger.warning("No search input found on Marathon ViewPointCloud")
            return permits

        search_input.clear()
        client.type_text(search_input, search_term)

        try:
            submit = page.get_by_role("button", name=re.compile(r"search|submit|go|find", re.IGNORECASE))
            if submit.count() > 0:
                client.click(submit.first)
            else:
                search_input.press("Enter")
        except Exception:
            search_input.press("Enter")

        page.wait_for_load_state("domcontentloaded", timeout=30000)

        html = page.content()
        snap_path = client.save_snapshot("marathon_permits", cache_key, html, "html")
        soup = BeautifulSoup(html, "lxml")
        permits = _parse_permit_list(soup, page.url)
        logger.info("Marathon ViewPointCloud found %d permits for %s", len(permits), search_term)

    except Exception as exc:
        logger.error("marathon.fetch failed: %s", exc, exc_info=True)

    return permits
