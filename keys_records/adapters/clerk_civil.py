"""
Monroe County Clerk of Courts — Civil Case Records adapter.

URL: https://www.monroe-clerk.com (Court Records section)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from ..polite_client import PoliteClient, ManualRetrievalRequired
from ..schemas import CourtCase, Provenance

logger = logging.getLogger("adapter.clerk_civil")

CLERK_URL = "https://www.monroe-clerk.com"
SOURCE_NAME = "Monroe County Clerk of Courts — Civil Records"


def _handle_disclaimer(page) -> bool:
    for text in ("Agree", "Accept", "I Agree", "Continue", "I Accept"):
        try:
            btn = page.get_by_role("button", name=re.compile(text, re.IGNORECASE))
            if btn.count() > 0:
                btn.first.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                return True
        except Exception:
            pass
        try:
            link = page.get_by_role("link", name=re.compile(text, re.IGNORECASE))
            if link.count() > 0:
                link.first.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                return True
        except Exception:
            pass
    return False


def _parse_case_list(soup: BeautifulSoup, page_url: str) -> list:
    """Parse court cases from a results table or list."""
    cases = []
    case_keywords = ["CASE NUMBER", "CASE NO", "FILING DATE", "CASE TYPE"]
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).upper() for th in table.find_all("th")]
        if not any(k in " ".join(headers) for k in case_keywords):
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
            case_num = _gc(cells, "CASE") or (cells[0] if cells else None)
            if not case_num:
                continue
            # Parse parties from a combined field or separate fields
            grantor = _gc(cells, "PLAINTIFF") or _gc(cells, "PARTY")
            grantee = _gc(cells, "DEFENDANT")
            parties = []
            if grantor:
                parties.append(grantor)
            if grantee:
                parties.append(grantee)

            cases.append(CourtCase(
                case_number=case_num,
                filing_date=_gc(cells, "DATE") or _gc(cells, "FILING"),
                case_type=_gc(cells, "TYPE") or _gc(cells, "CASE TYPE"),
                parties=parties,
                status=_gc(cells, "STATUS"),
                docket_events=[],
            ))
    return cases


def _search_name(client: PoliteClient, page, name: str, cache_root: Path) -> list:
    """Search civil records for a single name and return CourtCase list."""
    cases = []
    try:
        name_inputs = [
            page.locator("input[name*='name' i]"),
            page.locator("input[id*='name' i]"),
            page.locator("input[placeholder*='name' i]"),
            page.locator("input[type='text']").first,
        ]
        name_input = None
        for loc in name_inputs:
            try:
                if loc.count() > 0:
                    name_input = loc.first
                    break
            except Exception:
                pass

        if not name_input:
            return cases

        name_input.clear()
        client.type_text(name_input, name)

        try:
            submit = page.get_by_role("button", name=re.compile(r"search|submit|go|find", re.IGNORECASE))
            if submit.count() > 0:
                client.click(submit.first)
            else:
                name_input.press("Enter")
        except Exception:
            name_input.press("Enter")

        page.wait_for_load_state("domcontentloaded", timeout=30000)

        html = page.content()
        snap_path = client.save_snapshot("clerk_civil", name, html, "html")
        soup = BeautifulSoup(html, "lxml")
        found_cases = _parse_case_list(soup, page.url)

        # Try to get docket events for each case
        for case in found_cases:
            # Look for a link matching the case number to get detail
            try:
                case_link = page.get_by_role("link", name=re.compile(re.escape(case.case_number), re.IGNORECASE))
                if case_link.count() > 0:
                    client.click(case_link.first)
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    detail_html = page.content()
                    detail_soup = BeautifulSoup(detail_html, "lxml")
                    # Extract docket events from detail page
                    docket_events = []
                    for table in detail_soup.find_all("table"):
                        hdrs = [th.get_text(strip=True).upper() for th in table.find_all("th")]
                        if "DOCKET" in " ".join(hdrs) or "EVENT" in " ".join(hdrs):
                            for row in table.find_all("tr")[1:]:
                                cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
                                if cells:
                                    docket_events.append({"raw": " | ".join(cells)})
                    case.docket_events = docket_events
                    # Go back
                    page.go_back()
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception as e:
                logger.debug("Docket detail fetch failed: %s", e)

            case.provenance = Provenance(
                source_name=SOURCE_NAME,
                source_url=page.url,
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                cache_path=str(snap_path),
            )

        cases.extend(found_cases)
    except Exception as exc:
        logger.warning("Clerk civil name search for %s failed: %s", name, exc)
    return cases


def fetch(
    client: PoliteClient,
    owner_names: list,
    address: str,
    cache_root: Path,
) -> list:
    """Fetch civil court cases by searching owner names and address."""
    all_cases = []
    seen_case_numbers = set()

    try:
        page = client.navigate(CLERK_URL)
        _handle_disclaimer(page)
        client.save_domain_session("www.monroe-clerk.com")

        # Navigate to Court Records section
        nav_attempts = [
            "Court Records",
            "Civil Case",
            r"Case\s*Search",
            "Civil Search",
        ]
        navigated = False
        for text in nav_attempts:
            try:
                el = page.get_by_role("link", name=re.compile(text, re.IGNORECASE))
                if el.count() > 0:
                    client.click(el.first)
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    _handle_disclaimer(page)
                    navigated = True
                    break
            except Exception:
                pass

        if not navigated:
            logger.warning("Could not navigate to Court Records on clerk site")

        # Search each owner name
        search_terms = list(owner_names)
        if address:
            search_terms.append(address)

        for term in search_terms:
            if not term:
                continue
            cases = _search_name(client, page, term, cache_root)
            for case in cases:
                if case.case_number not in seen_case_numbers:
                    seen_case_numbers.add(case.case_number)
                    all_cases.append(case)

    except ManualRetrievalRequired:
        raise
    except Exception as exc:
        logger.error("clerk_civil.fetch failed: %s", exc, exc_info=True)
        raise ManualRetrievalRequired(
            source=SOURCE_NAME,
            reason=(f"Automated retrieval failed ({type(exc).__name__}): the "
                    "Clerk civil/foreclosure docket could not be reached or "
                    "parsed this run. This is NOT a confirmed absence of "
                    "litigation or foreclosure — search manually."),
            contact=CLERK_URL,
            url=CLERK_URL,
        )

    return all_cases
