"""
Monroe County OPAL (Oracle Community Development) permits adapter.

Covers permit applications from 2022-10-01 onwards.
Discovery page: https://www.monroecounty-fl.gov/1278/Online-Permitting-Services
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ...polite_client import PoliteClient, ManualRetrievalRequired
from ...schemas import PermitRecord, Provenance

logger = logging.getLogger("adapter.permits.opal")

DISCOVERY_URL = "https://www.monroecounty-fl.gov/1278/Online-Permitting-Services"
SOURCE_NAME = "Monroe County OPAL"
JURISDICTION = "Monroe County (Unincorporated)"


def _find_opal_url(page) -> Optional[str]:
    """Scan the discovery page for a link to the OPAL portal."""
    try:
        html = page.content()
        soup = BeautifulSoup(html, "lxml")
        opal_patterns = [
            re.compile(r"opal", re.IGNORECASE),
            re.compile(r"community\s*development", re.IGNORECASE),
            re.compile(r"permit\s*portal", re.IGNORECASE),
            re.compile(r"online\s*permit", re.IGNORECASE),
        ]
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            for pat in opal_patterns:
                if pat.search(href) or pat.search(text):
                    if href.startswith("http"):
                        return href
                    elif href.startswith("/"):
                        from urllib.parse import urlparse
                        parsed = urlparse(page.url)
                        return f"{parsed.scheme}://{parsed.netloc}{href}"
    except Exception as e:
        logger.debug("OPAL URL discovery error: %s", e)
    return None


def _parse_permit_list(soup: BeautifulSoup, page_url: str) -> list:
    permits = []
    keywords = ["PERMIT", "STATUS", "APPLIED", "ISSUED", "RECORD"]
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
            permit_num = (
                _gc(cells, "RECORD") or
                _gc(cells, "PERMIT") or
                _gc(cells, "PERMIT NUMBER") or
                (cells[0] if cells else "UNKNOWN")
            )
            permits.append(PermitRecord(
                jurisdiction=JURISDICTION,
                source_system=SOURCE_NAME,
                permit_number=permit_num,
                permit_type=_gc(cells, "TYPE") or _gc(cells, "RECORD TYPE"),
                subtype=_gc(cells, "SUBTYPE"),
                description=_gc(cells, "DESCRIPTION") or _gc(cells, "DESC"),
                status=_gc(cells, "STATUS"),
                applied_date=_gc(cells, "APPLIED") or _gc(cells, "APPLICATION DATE"),
                issued_date=_gc(cells, "ISSUED") or _gc(cells, "ISSUE DATE"),
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


def fetch(
    client: PoliteClient,
    parcel_id: str,
    address: str,
    cache_root: Path,
) -> list:
    """Fetch permit records from OPAL for a parcel."""
    cached_html = client.load_snapshot("opal", parcel_id)
    if cached_html:
        logger.info("Cache hit for opal/%s", parcel_id)
        soup = BeautifulSoup(cached_html, "lxml")
        return _parse_permit_list(soup, DISCOVERY_URL)

    permits = []
    try:
        # Step 1: Discover OPAL URL
        page = client.navigate(DISCOVERY_URL)
        opal_url = _find_opal_url(page)

        if not opal_url:
            logger.warning("Could not find OPAL portal URL from discovery page")
            raise ManualRetrievalRequired(
                source=SOURCE_NAME,
                reason="OPAL portal URL not found on discovery page",
                contact="Monroe County Building Dept: https://www.monroecounty-fl.gov/1278/Online-Permitting-Services",
                url=DISCOVERY_URL,
            )

        logger.info("Found OPAL URL: %s", opal_url)

        # Step 2: Navigate to OPAL portal
        page = client.navigate(opal_url)

        # Handle any disclaimer
        for text in ("Agree", "Accept", "I Agree", "Continue"):
            try:
                btn = page.get_by_role("button", name=re.compile(text, re.IGNORECASE))
                if btn.count() > 0:
                    client.click(btn.first)
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    break
            except Exception:
                pass

        # Step 3: Try searching by parcel ID first, then address
        search_terms = [parcel_id]
        if address:
            search_terms.append(address)

        for term in search_terms:
            if not term:
                continue
            input_locators = [
                page.locator("input[name*='parcel' i]"),
                page.locator("input[name*='address' i]"),
                page.locator("input[id*='search' i]"),
                page.locator("input[placeholder*='search' i]"),
                page.locator("input[type='search']"),
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
                continue

            search_input.clear()
            client.type_text(search_input, term)

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
            snap_path = client.save_snapshot("opal", parcel_id, html, "html")
            soup = BeautifulSoup(html, "lxml")
            found = _parse_permit_list(soup, page.url)

            if found:
                logger.info("OPAL found %d permits for %s", len(found), term)
                permits.extend(found)
                break

    except ManualRetrievalRequired:
        raise
    except Exception as exc:
        logger.error("opal.fetch failed: %s", exc, exc_info=True)
        raise ManualRetrievalRequired(
            source=SOURCE_NAME,
            reason=(f"Automated retrieval failed ({type(exc).__name__}): the "
                    "permit system could not be reached or parsed this run. This "
                    "is NOT a confirmed absence of permits — retrieve manually."),
            contact=DISCOVERY_URL,
            url=DISCOVERY_URL,
        )

    return permits
