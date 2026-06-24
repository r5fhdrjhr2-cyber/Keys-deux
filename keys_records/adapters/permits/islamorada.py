"""
Village of Islamorada permits adapter.

Platform: CityView
Discovery: https://www.islamorada.fl.us (building/permits section)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

from ...polite_client import PoliteClient, ManualRetrievalRequired
from ...schemas import PermitRecord, Provenance

logger = logging.getLogger("adapter.permits.islamorada")

DISCOVERY_URL = "https://www.islamorada.fl.us"
SOURCE_NAME = "CityView"
JURISDICTION = "Village of Islamorada"


def _find_cityview_url(page) -> Optional[str]:
    """Scan the Islamorada site for a CityView portal link."""
    try:
        html = page.content()
        soup = BeautifulSoup(html, "lxml")
        patterns = [
            re.compile(r"cityview", re.IGNORECASE),
            re.compile(r"permit\s*portal", re.IGNORECASE),
            re.compile(r"online\s*permit", re.IGNORECASE),
            re.compile(r"building\s*portal", re.IGNORECASE),
        ]
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            for pat in patterns:
                if pat.search(href) or pat.search(text):
                    if href.startswith("http"):
                        return href
                    elif href.startswith("/"):
                        parsed = urlparse(page.url)
                        return f"{parsed.scheme}://{parsed.netloc}{href}"
    except Exception as e:
        logger.debug("CityView URL discovery error: %s", e)
    return None


def _parse_permit_list(soup: BeautifulSoup, page_url: str) -> list:
    permits = []
    keywords = ["PERMIT", "STATUS", "APPLIED", "TYPE", "RECORD"]
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
            permit_num = _gc(cells, "PERMIT") or _gc(cells, "RECORD") or (cells[0] if cells else "UNKNOWN")
            permits.append(PermitRecord(
                jurisdiction=JURISDICTION,
                source_system=SOURCE_NAME,
                permit_number=permit_num,
                permit_type=_gc(cells, "TYPE"),
                subtype=_gc(cells, "SUBTYPE"),
                description=_gc(cells, "DESCRIPTION"),
                status=_gc(cells, "STATUS"),
                applied_date=_gc(cells, "APPLIED") or _gc(cells, "DATE"),
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
    return permits


def fetch(
    client: PoliteClient,
    address: str,
    parcel_id: str,
    cache_root: Path,
) -> list:
    """Fetch permits from Village of Islamorada CityView portal."""
    cache_key = parcel_id or address
    cached_html = client.load_snapshot("islamorada_permits", cache_key)
    if cached_html:
        logger.info("Cache hit for islamorada_permits/%s", cache_key)
        soup = BeautifulSoup(cached_html, "lxml")
        return _parse_permit_list(soup, DISCOVERY_URL)

    permits = []
    try:
        page = client.navigate(DISCOVERY_URL)

        # Look for building/permits navigation first
        for text in ("Building", "Permits", "Building Department", "Building Services"):
            try:
                el = page.get_by_role("link", name=re.compile(text, re.IGNORECASE))
                if el.count() > 0:
                    client.click(el.first)
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    break
            except Exception:
                pass

        cityview_url = _find_cityview_url(page)
        if not cityview_url:
            logger.warning("CityView portal not found for Islamorada")
            raise ManualRetrievalRequired(
                source="Village of Islamorada Building Department",
                reason="CityView permit portal URL not found",
                contact="Village of Islamorada: https://www.islamorada.fl.us | 305-664-6400",
                url=DISCOVERY_URL,
            )

        page = client.navigate(cityview_url)

        # Handle disclaimer
        for text in ("Agree", "Accept", "I Agree", "Continue"):
            try:
                btn = page.get_by_role("button", name=re.compile(text, re.IGNORECASE))
                if btn.count() > 0:
                    client.click(btn.first)
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    break
            except Exception:
                pass

        # Search
        search_term = address or parcel_id
        input_locators = [
            page.locator("input[name*='address' i]"),
            page.locator("input[name*='parcel' i]"),
            page.locator("input[placeholder*='address' i]"),
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
            logger.warning("No search input found on Islamorada CityView")
            return permits

        search_input.clear()
        client.type_text(search_input, search_term)

        try:
            submit = page.get_by_role("button", name=re.compile(r"search|submit|go", re.IGNORECASE))
            if submit.count() > 0:
                client.click(submit.first)
            else:
                search_input.press("Enter")
        except Exception:
            search_input.press("Enter")

        page.wait_for_load_state("domcontentloaded", timeout=30000)

        html = page.content()
        client.save_snapshot("islamorada_permits", cache_key, html, "html")
        soup = BeautifulSoup(html, "lxml")
        permits = _parse_permit_list(soup, page.url)
        logger.info("Islamorada CityView found %d permits for %s", len(permits), search_term)

    except ManualRetrievalRequired:
        raise
    except Exception as exc:
        logger.error("islamorada.fetch failed: %s", exc, exc_info=True)

    return permits
