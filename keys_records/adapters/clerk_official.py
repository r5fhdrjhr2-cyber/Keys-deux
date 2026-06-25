"""
Monroe County Clerk of Courts — Official Records adapter.

URL: https://www.monroe-clerk.com
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from ..polite_client import PoliteClient, ManualRetrievalRequired
from ..schemas import OfficialRecordInstrument, Provenance

logger = logging.getLogger("adapter.clerk_official")

CLERK_URL = "https://www.monroe-clerk.com"
SOURCE_NAME = "Monroe County Clerk of Courts — Official Records"

# Tag classification rules
_TAG_RULES = [
    (re.compile(r"\b(MTG|MOR|MORTGAGE)\b", re.IGNORECASE), "open_mortgage"),
    (re.compile(r"\b(SATISF|SAT MTG|RELEASE OF MORTGAGE)\b", re.IGNORECASE), "satisfied_mortgage"),
    (re.compile(r"\b(LIS PENDENS|LP)\b", re.IGNORECASE), "lis_pendens"),
    (re.compile(r"\b(LIEN|CLAIM OF LIEN|CONSTRUCTION LIEN|MECHANIC)\b", re.IGNORECASE), "lien"),
    (re.compile(r"\b(NOTICE OF COMMENCEMENT|NOC)\b", re.IGNORECASE), "notice_of_commencement"),
    (re.compile(r"\bJUDGMENT\b", re.IGNORECASE), "judgment"),
    (re.compile(r"\bEASEMENT\b", re.IGNORECASE), "easement"),
    (re.compile(r"\b(SATISFACTION|RELEASE)\b", re.IGNORECASE), "satisfaction"),
    (re.compile(r"\b(WARRANTY DEED|DEED|WD)\b", re.IGNORECASE), "deed"),
]


def _classify_instrument(instrument_type: str) -> list:
    tags = []
    if not instrument_type:
        return tags
    for pattern, tag in _TAG_RULES:
        if pattern.search(instrument_type):
            tags.append(tag)
    return tags


def _parse_money(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    cleaned = re.sub(r"[^0-9.]", "", s.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


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


def _parse_results_table(soup: BeautifulSoup, page_url: str) -> list:
    """Extract OfficialRecordInstrument entries from a clerk results table."""
    instruments = []
    keywords = ["RECORDING DATE", "INSTRUMENT TYPE", "GRANTOR", "GRANTEE", "BOOK", "PAGE"]
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
            instr_type = _gc(cells, "INSTRUMENT TYPE") or _gc(cells, "TYPE") or ""
            tags = _classify_instrument(instr_type)
            # Look for document URL in this row
            doc_url = None
            for td in row.find_all("td"):
                for a in td.find_all("a", href=True):
                    href = a["href"]
                    if any(ext in href.lower() for ext in (".pdf", "document", "view", "image")):
                        from urllib.parse import urljoin
                        doc_url = urljoin(page_url, href)
                        break

            instruments.append(OfficialRecordInstrument(
                recording_date=_gc(cells, "DATE") or _gc(cells, "RECORDING"),
                instrument_type=instr_type or None,
                book=_gc(cells, "BOOK") or _gc(cells, "OR BOOK"),
                page=_gc(cells, "PAGE") or _gc(cells, "OR PAGE"),
                instrument_number=_gc(cells, "INSTRUMENT") or _gc(cells, "INSTRUMENT #") or _gc(cells, "INSTR"),
                grantor=_gc(cells, "GRANTOR"),
                grantee=_gc(cells, "GRANTEE"),
                consideration=_parse_money(_gc(cells, "CONSIDERATION") or _gc(cells, "AMOUNT")),
                doc_stamps=_parse_money(_gc(cells, "DOC STAMP") or _gc(cells, "STAMPS")),
                legal_description=_gc(cells, "LEGAL") or _gc(cells, "LEGAL DESCRIPTION"),
                document_url=doc_url,
                document_cache_path=None,
                tags=tags,
            ))
    return instruments


def _search_name(client: PoliteClient, page, name: str, cache_root: Path) -> list:
    """Search official records for a single name and return instruments found."""
    results = []
    try:
        # Find name input (grantor/grantee)
        name_inputs = [
            page.locator("input[name*='grantor' i]"),
            page.locator("input[name*='grantee' i]"),
            page.locator("input[name*='name' i]"),
            page.locator("input[id*='name' i]"),
            page.locator("input[placeholder*='name' i]"),
        ]
        name_input = None
        for loc in name_inputs:
            try:
                if loc.count() > 0:
                    name_input = loc.first
                    break
            except Exception:
                pass

        if name_input is None:
            logger.warning("Could not find name input on clerk official records page")
            return results

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
        snap_path = client.save_snapshot("clerk_official", name, html, "html")
        soup = BeautifulSoup(html, "lxml")
        instruments = _parse_results_table(soup, page.url)

        # Try to download PDFs for each instrument
        for inst in instruments:
            if inst.document_url:
                try:
                    safe_name = re.sub(r"[^a-z0-9]", "_", name.lower())[:40]
                    fn = f"{safe_name}_{inst.book}_{inst.page}.pdf"
                    dest = cache_root / "clerk_official" / fn
                    client.download_file(inst.document_url, dest)
                    inst.document_cache_path = str(dest)
                except Exception as e:
                    logger.debug("PDF download failed: %s", e)

            inst.provenance = Provenance(
                source_name=SOURCE_NAME,
                source_url=page.url,
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                cache_path=str(snap_path),
            )

        results.extend(instruments)
    except Exception as exc:
        logger.warning("Clerk official name search for %s failed: %s", name, exc)
    return results


def fetch(
    client: PoliteClient,
    owner_names: list,
    parcel_id: str,
    cache_root: Path,
) -> list:
    """Fetch official record instruments by searching owner names."""
    all_instruments = []
    seen_keys = set()

    try:
        page = client.navigate(CLERK_URL)
        _handle_disclaimer(page)
        client.save_domain_session("www.monroe-clerk.com")

        # Navigate to Official Records section
        nav_attempts = [
            ("link", "Official Records"),
            ("link", "Official Record"),
            ("link", "OR Search"),
            ("link", r"Record\s*Search"),
        ]
        navigated = False
        for role, text in nav_attempts:
            try:
                el = page.get_by_role(role, name=re.compile(text, re.IGNORECASE))
                if el.count() > 0:
                    client.click(el.first)
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    _handle_disclaimer(page)
                    navigated = True
                    break
            except Exception:
                pass

        if not navigated:
            logger.warning("Could not navigate to Official Records section on clerk site")

        # Search each owner name
        for name in owner_names:
            if not name:
                continue
            cache_key = f"or_{parcel_id}_{name}"
            cached = client.load_snapshot("clerk_official", cache_key)
            if cached:
                soup = BeautifulSoup(cached, "lxml")
                instruments = _parse_results_table(soup, CLERK_URL)
            else:
                instruments = _search_name(client, page, name, cache_root)

            for inst in instruments:
                key = (inst.book, inst.page, inst.instrument_number)
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_instruments.append(inst)

    except ManualRetrievalRequired:
        raise
    except Exception as exc:
        logger.error("clerk_official.fetch failed: %s", exc, exc_info=True)
        raise ManualRetrievalRequired(
            source=SOURCE_NAME,
            reason=(f"Automated retrieval failed ({type(exc).__name__}): the "
                    "Clerk official-records index could not be reached or parsed "
                    "this run. This is NOT a confirmed absence of liens, "
                    "mortgages, or other recorded instruments — search manually."),
            contact=CLERK_URL,
            url=CLERK_URL,
        )

    return all_instruments
