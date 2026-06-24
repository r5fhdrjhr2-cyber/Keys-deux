"""
Monroe County Property Appraiser adapter.

URL: https://qpublic.schneidercorp.com/Application.aspx?AppID=605

This is an ASP.NET WebForms application. The PoliteClient navigates it
through the natural path (home → disclaimer → search → result) rather
than deep-linking to avoid detection. schneidercorp.com is in
restricted_domains so the pacing multiplier applies.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..polite_client import PoliteClient
from ..schemas import (
    AppraiserRecord,
    ImprovementDetail,
    Provenance,
    SalesRecord,
    ValuationYear,
)

logger = logging.getLogger("adapter.property_appraiser")

BASE_URL = "https://qpublic.schneidercorp.com/Application.aspx?AppID=605"
SOURCE_NAME = "Monroe County Property Appraiser (qPublic)"


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


def _parse_year(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"\b(19|20)\d{2}\b", s)
    return int(m.group(0)) if m else None


def extract_labeled_table(soup: BeautifulSoup) -> dict:
    """
    qPublic renders data in labeled <tr> rows where the first <td> is the
    label and the second is the value. Returns a dict mapping
    normalized_label -> value_text.
    """
    result = {}
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            label = cells[0].get_text(separator=" ", strip=True).rstrip(":").strip()
            value = cells[1].get_text(separator=" ", strip=True)
            if label:
                result[label.upper()] = value
    return result


def _handle_disclaimer(page) -> bool:
    """Click Accept/Agree if a disclaimer gate is present. Returns True if clicked."""
    for text in ("Agree", "Accept", "I Agree", "Continue", "I Accept"):
        try:
            btn = page.get_by_role("button", name=re.compile(text, re.IGNORECASE))
            if btn.count() > 0:
                btn.first.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                logger.info("Clicked disclaimer button: %s", text)
                return True
        except Exception:
            pass
        try:
            link = page.get_by_role("link", name=re.compile(text, re.IGNORECASE))
            if link.count() > 0:
                link.first.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                logger.info("Clicked disclaimer link: %s", text)
                return True
        except Exception:
            pass
    return False


def _parse_detail_page(html: str, parcel_id: str, page_url: str, cache_path: str) -> AppraiserRecord:
    """Parse the full qPublic property detail HTML."""
    soup = BeautifulSoup(html, "lxml")
    labeled = extract_labeled_table(soup)

    owner_names = []
    raw_owners = labeled.get("OWNER NAME", labeled.get("OWNER", ""))
    if raw_owners:
        owner_names = [n.strip() for n in raw_owners.split("\n") if n.strip()]
        if not owner_names:
            owner_names = [raw_owners]

    parcel_out = labeled.get("PARCEL ID", labeled.get("PARCEL NUMBER", parcel_id))
    re_number = labeled.get("RE NUMBER", labeled.get("RE#", None))
    alt_key = labeled.get("ALTERNATE KEY", labeled.get("ALT KEY", None))
    folio = labeled.get("FOLIO", labeled.get("FOLIO NUMBER", parcel_id))

    situs = labeled.get("SITUS ADDRESS", labeled.get("PHYSICAL ADDRESS", labeled.get("LOCATION ADDRESS", None)))
    mailing = labeled.get("MAILING ADDRESS", None)
    jurisdiction = labeled.get("JURISDICTION", labeled.get("MUNICIPALITY", labeled.get("CITY", None)))
    legal = labeled.get("LEGAL DESCRIPTION", labeled.get("LEGAL DESC", None))
    subdivision = labeled.get("SUBDIVISION", None)
    block = labeled.get("BLOCK", None)
    lot = labeled.get("LOT", None)
    use_code = labeled.get("USE CODE", labeled.get("PROPERTY USE CODE", labeled.get("DOR USE CODE", None)))
    zoning = labeled.get("ZONING", None)
    land_use = labeled.get("LAND USE", None)

    lot_sqft = None
    lot_acres = None
    lot_raw = labeled.get("LOT SIZE", labeled.get("LAND AREA", labeled.get("LOT AREA", None)))
    if lot_raw:
        # Try to extract acres and sqft
        m_ac = re.search(r"([\d,.]+)\s*ac", lot_raw, re.IGNORECASE)
        m_sf = re.search(r"([\d,.]+)\s*s[qf]", lot_raw, re.IGNORECASE)
        if m_ac:
            lot_acres = float(m_ac.group(1).replace(",", ""))
        if m_sf:
            lot_sqft = float(m_sf.group(1).replace(",", ""))
        if not m_ac and not m_sf:
            v = _parse_money(lot_raw)
            if v:
                lot_sqft = v

    # --- Sketch / Photo ---
    sketch_url = None
    photo_url = None
    for img in soup.find_all("img"):
        src = img.get("src", "")
        alt = img.get("alt", "").lower()
        if "sketch" in src.lower() or "sketch" in alt:
            sketch_url = urljoin(page_url, src)
        elif "photo" in src.lower() or "photo" in alt or "image" in alt:
            photo_url = urljoin(page_url, src)

    # --- Improvements ---
    improvements = []
    impr_headers = ["YEAR BUILT", "YR BUILT", "LIVING AREA", "EFFECTIVE YEAR"]
    for table in soup.find_all("table"):
        header_texts = [th.get_text(strip=True).upper() for th in table.find_all("th")]
        if not any(h in " ".join(header_texts) for h in impr_headers):
            continue
        rows = table.find_all("tr")[1:]  # skip header
        for row in rows:
            cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
            if len(cells) < 2:
                continue
            desc = cells[0] if cells else "Improvement"
            year_built = None
            eff_year = None
            living_area = None
            gross_area = None
            stories = None
            construction = None
            roof = None
            beds = None
            baths = None
            assessed = None

            col_map = {h.upper(): i for i, h in enumerate(header_texts)}
            def _gcell(key):
                idx = col_map.get(key)
                if idx is not None and idx < len(cells):
                    return cells[idx]
                return None

            if col_map:
                desc = _gcell("DESCRIPTION") or _gcell("IMPROVEMENT TYPE") or desc
                year_built = _parse_year(_gcell("YEAR BUILT") or _gcell("YR BUILT"))
                eff_year = _parse_year(_gcell("EFFECTIVE YEAR") or _gcell("EFF YR"))
                living_area = _parse_int(_gcell("LIVING AREA") or _gcell("LIVING SQ FT"))
                gross_area = _parse_int(_gcell("GROSS AREA") or _gcell("GROSS SQ FT"))
                stories_raw = _gcell("STORIES") or _gcell("FLOORS")
                stories = float(stories_raw) if stories_raw and re.match(r"[\d.]+", stories_raw) else None
                construction = _gcell("CONSTRUCTION") or _gcell("CONST TYPE")
                roof = _gcell("ROOF") or _gcell("ROOF TYPE")
                beds_raw = _gcell("BEDS") or _gcell("BEDROOMS")
                baths_raw = _gcell("BATHS") or _gcell("BATHROOMS")
                beds = float(beds_raw) if beds_raw and re.match(r"[\d.]+", beds_raw) else None
                baths = float(baths_raw) if baths_raw and re.match(r"[\d.]+", baths_raw) else None
                assessed = _parse_int(_gcell("ASSESSED VALUE") or _gcell("BLDG VALUE"))
            else:
                # Positional fallback
                if len(cells) > 1:
                    year_built = _parse_year(cells[1])
                if len(cells) > 2:
                    living_area = _parse_int(cells[2])

            improvements.append(ImprovementDetail(
                description=str(desc) if desc else "Improvement",
                year_built=year_built,
                effective_year=eff_year,
                living_area=living_area,
                gross_area=gross_area,
                stories=stories,
                construction_type=construction,
                roof_type=roof,
                beds=beds,
                baths=baths,
                assessed_value=assessed,
            ))

    # --- Valuation history ---
    valuation_history = []
    val_keywords = ["JUST", "ASSESSED", "TAXABLE"]
    for table in soup.find_all("table"):
        header_texts = [th.get_text(strip=True).upper() for th in table.find_all("th")]
        if not any(k in " ".join(header_texts) for k in val_keywords):
            continue
        col_map = {h: i for i, h in enumerate(header_texts)}
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
            if not cells:
                continue
            def _get(key):
                for k in col_map:
                    if key in k:
                        idx = col_map[k]
                        if idx < len(cells):
                            return cells[idx]
                return None
            year_raw = _get("YEAR") or (cells[0] if cells else None)
            year_val = _parse_year(year_raw) or _parse_int(year_raw)
            if not year_val:
                continue
            hmstd = bool(_get("HOMESTEAD") or _get("HX"))
            hmstd_amt_raw = _get("HOMESTEAD AMT") or _get("HX AMT")
            valuation_history.append(ValuationYear(
                year=year_val,
                just_value=_parse_int(_get("JUST") or _get("MARKET")),
                assessed_value=_parse_int(_get("ASSESSED")),
                taxable_value=_parse_int(_get("TAXABLE") or _get("COUNTY TAXABLE")),
                school_taxable=_parse_int(_get("SCHOOL")),
                land_value=_parse_int(_get("LAND")),
                building_value=_parse_int(_get("BUILDING") or _get("BLDG")),
                homestead_exemption=hmstd,
                homestead_amount=_parse_int(hmstd_amt_raw),
                soh_cap_differential=_parse_int(_get("SOH") or _get("CAP DIFF")),
            ))

    # --- Sales history ---
    sales_history = []
    sale_keywords = ["SALE DATE", "PRICE", "OR BOOK", "OR PAGE", "GRANTOR", "GRANTEE"]
    for table in soup.find_all("table"):
        header_texts = [th.get_text(strip=True).upper() for th in table.find_all("th")]
        if not any(k in " ".join(header_texts) for k in sale_keywords):
            continue
        col_map = {h: i for i, h in enumerate(header_texts)}
        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
            if not cells:
                continue
            def _gs(key):
                for k in col_map:
                    if key in k:
                        idx = col_map[k]
                        if idx < len(cells):
                            return cells[idx]
                return None
            price_raw = _gs("PRICE") or _gs("SALE PRICE") or _gs("AMOUNT")
            price = _parse_int(price_raw) if price_raw else None
            sales_history.append(SalesRecord(
                date=_gs("DATE") or _gs("SALE DATE"),
                price=price,
                or_book=_gs("OR BOOK") or _gs("BOOK"),
                or_page=_gs("OR PAGE") or _gs("PAGE"),
                instrument_number=_gs("INSTRUMENT") or _gs("INSTR"),
                grantor=_gs("GRANTOR"),
                grantee=_gs("GRANTEE"),
                sale_qualification=_gs("QUALIFICATION") or _gs("QUAL"),
                deed_type=_gs("DEED TYPE") or _gs("DEED"),
            ))

    return AppraiserRecord(
        parcel_id=parcel_out,
        re_number=re_number,
        alternate_key=alt_key,
        folio=folio,
        owner_names=owner_names,
        mailing_address=mailing,
        situs_address=situs,
        jurisdiction=jurisdiction,
        legal_description=legal,
        subdivision=subdivision,
        block=block,
        lot=lot,
        property_use_code=use_code,
        lot_size_sqft=lot_sqft,
        lot_size_acres=lot_acres,
        zoning=zoning,
        land_use=land_use,
        sketch_url=sketch_url,
        photo_url=photo_url,
        improvements=improvements,
        valuation_history=valuation_history,
        sales_history=sales_history,
        provenance=Provenance(
            source_name=SOURCE_NAME,
            source_url=page_url,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            cache_path=cache_path,
        ),
    )


def fetch(
    client: PoliteClient,
    parcel_id: Optional[str] = None,
    address: Optional[str] = None,
    cache_root: Optional[Path] = None,
) -> Optional[AppraiserRecord]:
    """
    Fetch property appraiser record for a parcel.

    Navigates qPublic naturally: home → disclaimer → search tab → search → detail.
    Returns AppraiserRecord or None on failure.
    """
    if cache_root is None:
        cache_root = client.cache_root

    cache_key = parcel_id or address or "unknown"

    # Cache check
    cached_html = client.load_snapshot("qpublic", cache_key)
    if cached_html:
        logger.info("Cache hit for qpublic/%s", cache_key)
        snap_path = str(cache_root / "qpublic" / f"{cache_key}.html")
        return _parse_detail_page(cached_html, parcel_id or "", BASE_URL, snap_path)

    try:
        # Step 1: Navigate to the home page (natural entry point)
        page = client.navigate(BASE_URL)

        # Step 2: Handle disclaimer if present
        _handle_disclaimer(page)
        # Save session after disclaimer acceptance so next run skips it
        domain = "qpublic.schneidercorp.com"
        client.save_domain_session(domain)

        # Step 3: Find and click parcel search tab/link
        search_tab_texts = ["Search by Parcel", "Parcel Search", "Parcel ID", "RE Number"]
        clicked_tab = False
        for text in search_tab_texts:
            try:
                link = page.get_by_role("link", name=re.compile(text, re.IGNORECASE))
                if link.count() > 0:
                    client.click(link.first)
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                    clicked_tab = True
                    logger.info("Clicked search tab: %s", text)
                    break
            except Exception:
                pass

        if not clicked_tab:
            # Try looking for a Search nav item
            try:
                nav_search = page.get_by_role("link", name=re.compile(r"search", re.IGNORECASE))
                if nav_search.count() > 0:
                    client.click(nav_search.first)
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass

        # Step 4: Find parcel input and enter the search term
        search_value = parcel_id or address or ""
        input_locators = [
            page.locator("input[name*='parcel' i]"),
            page.locator("input[id*='parcel' i]"),
            page.locator("input[name*='re_number' i]"),
            page.locator("input[name*='alternate' i]"),
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

        if search_input is None:
            logger.warning("Could not find parcel search input on qPublic page")
            return None

        search_input.clear()
        client.type_text(search_input, search_value)

        # Step 5: Submit
        try:
            submit = page.get_by_role("button", name=re.compile(r"search|submit|go", re.IGNORECASE))
            if submit.count() > 0:
                client.click(submit.first)
            else:
                search_input.press("Enter")
        except Exception:
            search_input.press("Enter")

        page.wait_for_load_state("domcontentloaded", timeout=30000)

        # Step 6: If multiple results, click the first matching row
        try:
            result_rows = page.locator("table tr a")
            if result_rows.count() > 1:
                client.click(result_rows.first)
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            elif result_rows.count() == 1:
                client.click(result_rows.first)
                page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception as exc:
            logger.debug("Result click: %s", exc)

        # Step 7: Save snapshot
        html = page.content()
        snap_path = client.save_snapshot("qpublic", cache_key, html, "html")

        # Step 8: Try to download sketch and photo
        record = _parse_detail_page(html, parcel_id or "", page.url, str(snap_path))

        if record.sketch_url:
            try:
                sketch_dest = cache_root / "qpublic" / f"{cache_key}_sketch.jpg"
                client.download_file(record.sketch_url, sketch_dest)
                record.sketch_cache_path = str(sketch_dest)
            except Exception as e:
                logger.debug("Sketch download failed: %s", e)

        if record.photo_url:
            try:
                photo_dest = cache_root / "qpublic" / f"{cache_key}_photo.jpg"
                client.download_file(record.photo_url, photo_dest)
                record.photo_cache_path = str(photo_dest)
            except Exception as e:
                logger.debug("Photo download failed: %s", e)

        return record

    except Exception as exc:
        logger.error("property_appraiser.fetch failed: %s", exc, exc_info=True)
        return None
