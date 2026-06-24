"""
Monroe County Tax Collector adapter.

Primary URL: https://www.monroecounty-fl.gov/etc/rp/search.php
Fallback:    https://monroetaxcollector.com
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from ..polite_client import PoliteClient
from ..schemas import Provenance, TaxCollectorRecord, TaxYear

logger = logging.getLogger("adapter.tax_collector")

PRIMARY_URL = "https://www.monroecounty-fl.gov/etc/rp/search.php"
FALLBACK_URL = "https://monroetaxcollector.com"
SOURCE_NAME = "Monroe County Tax Collector"


def _parse_money(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    cleaned = re.sub(r"[^0-9.]", "", s.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_int(s: Optional[str]) -> Optional[int]:
    v = _parse_money(s)
    return int(v) if v is not None else None


def _parse_tax_years(soup: BeautifulSoup) -> list:
    """Parse tax year rows from a table with year, gross tax, taxable value, etc."""
    tax_years = []
    year_keywords = ["YEAR", "TAX YEAR"]
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).upper() for th in table.find_all("th")]
        if not any(k in " ".join(headers) for k in year_keywords):
            continue
        col_map = {h: i for i, h in enumerate(headers)}

        def _gc(row_cells, key):
            for k in col_map:
                if key in k:
                    idx = col_map[k]
                    if idx < len(row_cells):
                        return row_cells[idx]
            return None

        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
            if not cells:
                continue
            year_raw = _gc(cells, "YEAR") or (cells[0] if cells else None)
            year_val = None
            if year_raw:
                m = re.search(r"\b(20\d{2}|19\d{2})\b", year_raw)
                if m:
                    year_val = int(m.group(1))
            if not year_val:
                continue
            status_raw = _gc(cells, "STATUS") or _gc(cells, "PAID") or ""
            status = status_raw.lower().strip() if status_raw else None
            if status and "paid" in status:
                status = "paid"
            elif status and "unpaid" in status:
                status = "unpaid"
            elif status and "partial" in status:
                status = "partial"

            noadval = []
            noadval_raw = _gc(cells, "NON-AD") or _gc(cells, "NON AD")
            if noadval_raw:
                noadval = [{"description": "Non-Ad Valorem", "amount": _parse_money(noadval_raw)}]

            tax_years.append(TaxYear(
                year=year_val,
                bill_number=_gc(cells, "BILL") or _gc(cells, "BILL NUMBER"),
                gross_tax=_parse_money(_gc(cells, "GROSS TAX") or _gc(cells, "GROSS") or _gc(cells, "TAX")),
                taxable_value=_parse_int(_gc(cells, "TAXABLE VALUE") or _gc(cells, "TAXABLE")),
                millage=_parse_money(_gc(cells, "MILLAGE") or _gc(cells, "RATE")),
                amount_paid=_parse_money(_gc(cells, "AMOUNT PAID") or _gc(cells, "PAID AMT")),
                date_paid=_gc(cells, "DATE PAID") or _gc(cells, "PAID DATE"),
                discount_taken=_parse_money(_gc(cells, "DISCOUNT") or _gc(cells, "DISC")),
                status=status,
                non_ad_valorem=noadval,
            ))
    return tax_years


def _extract_record(html: str, parcel_id: str, source_url: str, cache_path: str) -> TaxCollectorRecord:
    soup = BeautifulSoup(html, "lxml")
    text_upper = soup.get_text(" ", strip=True).upper()

    # Account number
    acct = None
    m = re.search(r"ACCOUNT\s*(?:NUMBER|#|NO\.?)[\s:]*([A-Z0-9\-]+)", text_upper)
    if m:
        acct = m.group(1).strip()

    tax_years = _parse_tax_years(soup)

    # Delinquency
    delinquent = "DELINQUENT" in text_upper or "PAST DUE" in text_upper
    delinquent_years = []
    for yr in tax_years:
        if yr.status and "unpaid" in yr.status:
            delinquent_years.append(yr.year)

    # Tax certificates
    tax_certs = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).upper() for th in table.find_all("th")]
        if "CERTIFICATE" in " ".join(headers):
            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
                if cells:
                    tax_certs.append({"raw": " | ".join(cells)})

    # Tax deed status
    deed_status = None
    if "TAX DEED" in text_upper:
        deed_status = "tax_deed_pending"

    # Escrow code
    escrow = None
    m2 = re.search(r"ESCROW\s*(?:CODE|#)?[\s:]*([A-Z0-9]+)", text_upper)
    if m2:
        escrow = m2.group(1).strip()

    return TaxCollectorRecord(
        account_number=acct,
        tax_years=tax_years,
        delinquent=delinquent,
        delinquent_years=delinquent_years,
        tax_certificates=tax_certs,
        tax_deed_status=deed_status,
        escrow_code=escrow,
        provenance=Provenance(
            source_name=SOURCE_NAME,
            source_url=source_url,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            cache_path=cache_path,
        ),
    )


def _try_search(client: PoliteClient, url: str, parcel_id: str, cache_root: Path) -> Optional[TaxCollectorRecord]:
    """Attempt tax search at a given URL. Returns record or None."""
    try:
        page = client.navigate(url)

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

        # Find parcel/account input
        input_locators = [
            page.locator("input[name*='parcel' i]"),
            page.locator("input[name*='account' i]"),
            page.locator("input[id*='parcel' i]"),
            page.locator("input[placeholder*='parcel' i]"),
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
            logger.warning("Could not find search input at %s", url)
            return None

        search_input.clear()
        client.type_text(search_input, parcel_id)

        # Submit
        try:
            submit = page.get_by_role("button", name=re.compile(r"search|submit|go|find", re.IGNORECASE))
            if submit.count() > 0:
                client.click(submit.first)
            else:
                search_input.press("Enter")
        except Exception:
            search_input.press("Enter")

        page.wait_for_load_state("domcontentloaded", timeout=30000)

        # Click through to detail if results table present
        try:
            result_link = page.locator("table tr td a").first
            if result_link.count() > 0:
                client.click(result_link)
                page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass

        html = page.content()
        snap_path = client.save_snapshot("tax_collector", parcel_id, html, "html")
        return _extract_record(html, parcel_id, page.url, str(snap_path))

    except Exception as exc:
        logger.warning("Tax collector search at %s failed: %s", url, exc)
        return None


def fetch(
    client: PoliteClient,
    parcel_id: str,
    cache_root: Path,
) -> Optional[TaxCollectorRecord]:
    """Fetch tax collector record for a parcel_id."""
    cached_html = client.load_snapshot("tax_collector", parcel_id)
    if cached_html:
        logger.info("Cache hit for tax_collector/%s", parcel_id)
        return _extract_record(cached_html, parcel_id, PRIMARY_URL, "")

    # Try primary URL first
    record = _try_search(client, PRIMARY_URL, parcel_id, cache_root)
    if record:
        return record

    # Fallback
    logger.info("Primary tax collector URL failed, trying fallback")
    record = _try_search(client, FALLBACK_URL, parcel_id, cache_root)
    return record
