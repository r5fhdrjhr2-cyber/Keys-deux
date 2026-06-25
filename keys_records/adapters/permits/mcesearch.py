"""
Monroe County eSearch (MCeSearch) permits adapter.

URL: https://mcesearch.monroecounty-fl.gov/search/permits
Covers permit applications before 2022-10-01.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from ...polite_client import PoliteClient, ManualRetrievalRequired
from ...schemas import PermitRecord, Provenance

logger = logging.getLogger("adapter.permits.mcesearch")

BASE_URL = "https://mcesearch.monroecounty-fl.gov/search/permits"
SOURCE_NAME = "Monroe County eSearch (MCeSearch)"
JURISDICTION = "Monroe County (Unincorporated)"


def _parse_permit_list(soup: BeautifulSoup, page_url: str) -> list:
    permits = []
    keywords = ["PERMIT", "STATUS", "APPLIED", "ISSUED"]
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
            permit_num = _gc(cells, "PERMIT") or _gc(cells, "PERMIT NUMBER") or _gc(cells, "PERMIT #")
            if not permit_num:
                permit_num = cells[0] if cells else "UNKNOWN"

            permits.append(PermitRecord(
                jurisdiction=JURISDICTION,
                source_system=SOURCE_NAME,
                permit_number=permit_num,
                permit_type=_gc(cells, "TYPE") or _gc(cells, "PERMIT TYPE"),
                subtype=_gc(cells, "SUBTYPE") or _gc(cells, "SUB TYPE"),
                description=_gc(cells, "DESCRIPTION") or _gc(cells, "DESC") or _gc(cells, "WORK DESC"),
                status=_gc(cells, "STATUS"),
                applied_date=_gc(cells, "APPLIED") or _gc(cells, "APPLICATION DATE"),
                issued_date=_gc(cells, "ISSUED") or _gc(cells, "ISSUE DATE"),
                finaled_date=_gc(cells, "FINAL") or _gc(cells, "FINALED") or _gc(cells, "CO DATE"),
                expiration_date=_gc(cells, "EXPIR") or _gc(cells, "EXPIRES"),
                contractor_name=_gc(cells, "CONTRACTOR"),
                contractor_license=_gc(cells, "LICENSE") or _gc(cells, "LIC"),
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


def _get_permit_detail(client: PoliteClient, page, permit: PermitRecord, cache_root: Path):
    """Click into permit detail to get inspections and contractor info."""
    try:
        # Find link matching permit number
        link = page.get_by_role("link", name=re.compile(re.escape(permit.permit_number), re.IGNORECASE))
        if link.count() == 0:
            return
        client.click(link.first)
        page.wait_for_load_state("domcontentloaded", timeout=20000)

        html = page.content()
        snap_path = client.save_snapshot("mcesearch", permit.permit_number, html, "html")
        permit.provenance = Provenance(
            source_name=SOURCE_NAME,
            source_url=page.url,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            cache_path=str(snap_path),
        )

        soup = BeautifulSoup(html, "lxml")
        text_upper = soup.get_text(" ", strip=True).upper()

        # Declared value
        m = re.search(r"DECLARED\s*VALUE[\s:$]*([\d,]+(?:\.\d{2})?)", text_upper)
        if m:
            permit.declared_value = float(m.group(1).replace(",", ""))

        # Contractor
        if not permit.contractor_name:
            m2 = re.search(r"CONTRACTOR[\s:]+([A-Z &',.\-]+?)(?:\n|LIC|\||$)", text_upper)
            if m2:
                permit.contractor_name = m2.group(1).strip().title()

        # Inspections table
        insp_keywords = ["INSPECTION", "RESULT", "INSPECTOR"]
        for table in soup.find_all("table"):
            hdrs = [th.get_text(strip=True).upper() for th in table.find_all("th")]
            if any(k in " ".join(hdrs) for k in insp_keywords):
                for row in table.find_all("tr")[1:]:
                    cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
                    if cells:
                        permit.inspections.append({"raw": " | ".join(cells)})

        page.go_back()
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception as e:
        logger.debug("Permit detail fetch failed for %s: %s", permit.permit_number, e)
        try:
            page.go_back()
        except Exception:
            pass


def fetch(
    client: PoliteClient,
    parcel_id: str,
    cache_root: Path,
) -> list:
    """Fetch permit records from MCeSearch for a parcel ID."""
    cached_html = client.load_snapshot("mcesearch", parcel_id)
    if cached_html:
        logger.info("Cache hit for mcesearch/%s", parcel_id)
        soup = BeautifulSoup(cached_html, "lxml")
        return _parse_permit_list(soup, BASE_URL)

    permits = []
    try:
        page = client.navigate(BASE_URL)

        # Find parcel/folio input
        input_locators = [
            page.locator("input[name*='parcel' i]"),
            page.locator("input[name*='folio' i]"),
            page.locator("input[id*='parcel' i]"),
            page.locator("input[placeholder*='parcel' i]"),
            page.locator("input[placeholder*='folio' i]"),
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
            logger.warning("No parcel input found on MCeSearch page")
            return permits

        search_input.clear()
        client.type_text(search_input, parcel_id)

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
        snap_path = client.save_snapshot("mcesearch", parcel_id, html, "html")
        soup = BeautifulSoup(html, "lxml")
        permits = _parse_permit_list(soup, page.url)

        logger.info("MCeSearch found %d permits for %s", len(permits), parcel_id)

        # Get detail for each permit
        for permit in permits:
            _get_permit_detail(client, page, permit, cache_root)

    except ManualRetrievalRequired:
        raise
    except Exception as exc:
        logger.error("mcesearch.fetch failed: %s", exc, exc_info=True)
        raise ManualRetrievalRequired(
            source=SOURCE_NAME,
            reason=(f"Automated retrieval failed ({type(exc).__name__}): the "
                    "permit system could not be reached or parsed this run. This "
                    "is NOT a confirmed absence of permits — retrieve manually."),
            contact=BASE_URL,
            url=BASE_URL,
        )

    return permits
