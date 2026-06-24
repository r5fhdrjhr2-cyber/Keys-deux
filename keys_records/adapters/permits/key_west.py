"""
City of Key West permits adapter.

URL: https://etrakit.cityofkeywest-fl.gov/eTRAKiT (ASP.NET eTRAKiT)
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

logger = logging.getLogger("adapter.permits.key_west")

BASE_URL = "https://etrakit.cityofkeywest-fl.gov/eTRAKiT"
SOURCE_NAME = "eTRAKiT"
JURISDICTION = "City of Key West"


def _parse_permit_list(soup: BeautifulSoup, page_url: str) -> list:
    permits = []
    keywords = ["PERMIT", "STATUS", "APPLIED", "TYPE"]
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
            permit_num = _gc(cells, "PERMIT") or _gc(cells, "PERMIT NUMBER") or (cells[0] if cells else "UNKNOWN")
            permits.append(PermitRecord(
                jurisdiction=JURISDICTION,
                source_system=SOURCE_NAME,
                permit_number=permit_num,
                permit_type=_gc(cells, "TYPE") or _gc(cells, "PERMIT TYPE"),
                subtype=_gc(cells, "SUBTYPE"),
                description=_gc(cells, "DESCRIPTION") or _gc(cells, "WORK DESCRIPTION"),
                status=_gc(cells, "STATUS"),
                applied_date=_gc(cells, "APPLIED") or _gc(cells, "APPLICATION DATE"),
                issued_date=_gc(cells, "ISSUED"),
                finaled_date=_gc(cells, "FINAL") or _gc(cells, "CO DATE"),
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
    return permits


def _get_detail(client: PoliteClient, page, permit: PermitRecord, cache_root: Path):
    """Navigate to permit detail page to get inspections."""
    try:
        link = page.get_by_role("link", name=re.compile(re.escape(permit.permit_number), re.IGNORECASE))
        if link.count() == 0:
            return
        client.click(link.first)
        page.wait_for_load_state("domcontentloaded", timeout=20000)

        html = page.content()
        snap = client.save_snapshot("key_west_permits", permit.permit_number, html, "html")
        permit.provenance = Provenance(
            source_name=SOURCE_NAME,
            source_url=page.url,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            cache_path=str(snap),
        )

        soup = BeautifulSoup(html, "lxml")
        for table in soup.find_all("table"):
            hdrs = [th.get_text(strip=True).upper() for th in table.find_all("th")]
            if "INSPECTION" in " ".join(hdrs):
                for row in table.find_all("tr")[1:]:
                    cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
                    if cells:
                        permit.inspections.append({"raw": " | ".join(cells)})

        # Get declared value
        text_upper = soup.get_text(" ", strip=True).upper()
        m = re.search(r"(?:DECLARED|ESTIMATED|JOB)\s*VALUE[\s:$]*([\d,]+(?:\.\d{2})?)", text_upper)
        if m:
            permit.declared_value = float(m.group(1).replace(",", ""))

        page.go_back()
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception as e:
        logger.debug("Key West permit detail failed: %s", e)
        try:
            page.go_back()
        except Exception:
            pass


def fetch(
    client: PoliteClient,
    address: str,
    parcel_id: str,
    cache_root: Path,
) -> list:
    """Fetch permits from City of Key West eTRAKiT."""
    cache_key = parcel_id or address
    cached_html = client.load_snapshot("key_west_permits", cache_key)
    if cached_html:
        logger.info("Cache hit for key_west_permits/%s", cache_key)
        soup = BeautifulSoup(cached_html, "lxml")
        return _parse_permit_list(soup, BASE_URL)

    permits = []
    try:
        page = client.navigate(BASE_URL)

        # Navigate to Permits tab
        for text in ("Permits", "Permit Search", "Search Permits"):
            try:
                el = page.get_by_role("link", name=re.compile(text, re.IGNORECASE))
                if el.count() > 0:
                    client.click(el.first)
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    break
            except Exception:
                pass

        # Search by address
        search_term = address or parcel_id
        input_locators = [
            page.locator("input[name*='address' i]"),
            page.locator("input[id*='address' i]"),
            page.locator("input[placeholder*='address' i]"),
            page.locator("input[name*='parcel' i]"),
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
            logger.warning("No search input found on eTRAKiT page")
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
        snap_path = client.save_snapshot("key_west_permits", cache_key, html, "html")
        soup = BeautifulSoup(html, "lxml")
        permits = _parse_permit_list(soup, page.url)
        logger.info("eTRAKiT found %d permits for %s", len(permits), search_term)

        for permit in permits:
            _get_detail(client, page, permit, cache_root)

    except Exception as exc:
        logger.error("key_west.fetch failed: %s", exc, exc_info=True)

    return permits
