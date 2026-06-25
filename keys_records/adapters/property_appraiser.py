"""
Monroe County Property Appraiser adapter.

URL: https://qpublic.schneidercorp.com/Application.aspx?AppID=605

This is an ASP.NET WebForms application. The PoliteClient navigates it
through the natural path (home → disclaimer → search → result) rather
than deep-linking to avoid detection. schneidercorp.com is in
restricted_domains so the pacing multiplier applies.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
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

# ---------------------------------------------------------------------------
# Primary path: Florida DOR Statewide Cadastral (ArcGIS REST, JSON over HTTP).
#
# The qPublic appraiser site (schneidercorp.com / mcpafl.org) sits behind
# Cloudflare bot protection and routinely blocks automated browser sessions.
# The Florida Department of Revenue publishes the same property roll (owner,
# valuation, year built, living area, legal description, land use) as an open
# ArcGIS FeatureServer that answers plain GET requests with JSON -- no
# Cloudflare, no CAPTCHA, no browser. We query it FIRST and only fall back to
# the Playwright/qPublic navigation if it returns nothing.
#
# This is "routing around" a block via an alternative open dataset, NOT
# circumventing the protection on the blocked host: we never touch Cloudflare.
# ---------------------------------------------------------------------------
CADASTRAL_QUERY_URL = (
    "https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/"
    "Florida_Statewide_Cadastral/FeatureServer/0/query"
)
CADASTRAL_SOURCE = "Florida DOR Statewide Cadastral (FDOR property roll, ArcGIS REST)"

# Florida Keys bounding box. Used to confirm a suffix-matched parcel is actually
# in Monroe County before trusting it (parcel-number suffixes can collide across
# counties; geography cannot).
_KEYS_LAT_MIN, _KEYS_LAT_MAX = 24.30, 25.45
_KEYS_LON_MIN, _KEYS_LON_MAX = -82.30, -80.00

# The five incorporated municipalities in Monroe County. Everything else is
# unincorporated and routes to the county permit systems (MCeSearch/OPAL).
_INCORPORATED_CITIES = {
    "KEY WEST", "MARATHON", "ISLAMORADA", "KEY COLONY BEACH", "LAYTON",
}

# Common Florida DOR use codes (residential subset) for human-readable output.
_DOR_USE_CODES = {
    "000": "Vacant Residential",
    "001": "Single Family",
    "002": "Mobile Home",
    "003": "Multi-Family (10+ units)",
    "004": "Condominium",
    "005": "Cooperative",
    "006": "Retirement / Misc Residential",
    "007": "Misc Residential (migrant camp, boarding)",
    "008": "Multi-Family (fewer than 10 units)",
    "009": "Residential Common Element / Common Area",
}


def _ci(attrs: dict, *names):
    """Case-insensitive first-non-empty attribute getter for ArcGIS rows."""
    for n in names:
        v = attrs.get(n)
        if v not in (None, "", " "):
            return v
    return None


def _as_int(v) -> Optional[int]:
    try:
        if v in (None, "", " "):
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _ring_centroid(geometry: dict):
    """Return (lat, lon) centroid of an ArcGIS polygon's first ring, or None."""
    if not geometry:
        return None
    rings = geometry.get("rings")
    if not rings or not rings[0]:
        return None
    ring = rings[0]
    cx = sum(p[0] for p in ring) / len(ring)
    cy = sum(p[1] for p in ring) / len(ring)
    return (cy, cx)  # (lat, lon)


def _jurisdiction_from_city(city: Optional[str]) -> Optional[str]:
    """
    Derive a permit-routing jurisdiction from the physical city.

    NOTE: this is the postal/physical city from the DOR roll, not an
    authoritative municipal-boundary determination. For the five incorporated
    Keys cities it is reliable; an unrecognized city falls through to the
    unincorporated county default in the router. This limitation is recorded
    so an empty permit pull from the wrong jurisdiction is never read as
    "no permits exist".
    """
    if not city:
        return None
    c = city.upper().strip()
    for inc in _INCORPORATED_CITIES:
        if inc in c:
            return inc
    return c  # unrecognized -> router falls back to mcesearch/opal


def fetch_via_cadastral(
    parcel_id: str,
    cache_root: Path,
    cache_key: str,
    session: Optional[requests.Session] = None,
) -> Optional[AppraiserRecord]:
    """
    Resolve the appraiser record from the FDOR Statewide Cadastral by parcel ID.

    Returns an AppraiserRecord (with owner, valuation, year built, living area,
    legal description, land use, lot size, and parcel centroid) or None if the
    parcel cannot be found / confirmed to be in Monroe County.
    """
    digits = re.sub(r"\D", "", parcel_id or "")
    if len(digits) < 8:
        logger.info("Cadastral: parcel '%s' has too few digits to query", parcel_id)
        return None

    sess = session or requests
    # Suffix match: Monroe appraiser parcel "00341780-000000" maps to the
    # cadastral PARCEL_ID ending in "00341780000000".
    params = {
        "where": f"PARCEL_ID LIKE '%{digits}'",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }
    # The ArcGIS host occasionally stalls; retry with backoff before giving up,
    # because this is the primary (must-have) source for valuation and owner.
    data = None
    last_exc = None
    for attempt in range(4):
        try:
            resp = sess.get(CADASTRAL_QUERY_URL, params=params, timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"})
            data = resp.json()
            break
        except Exception as exc:
            last_exc = exc
            wait = 2 * (attempt + 1)
            logger.info("Cadastral query attempt %d failed (%s); retrying in %ss",
                        attempt + 1, exc, wait)
            time.sleep(wait)
    if data is None:
        logger.warning("Cadastral query failed after retries: %s", last_exc)
        return None

    feats = data.get("features", [])
    if not feats:
        logger.info("Cadastral: no parcel matched suffix %s", digits)
        return None

    # If several counties share the suffix, keep only the one inside the Keys.
    chosen = None
    for f in feats:
        latlon = _ring_centroid(f.get("geometry") or {})
        if latlon and (_KEYS_LAT_MIN <= latlon[0] <= _KEYS_LAT_MAX
                       and _KEYS_LON_MIN <= latlon[1] <= _KEYS_LON_MAX):
            chosen = f
            chosen["_latlon"] = latlon
            break
    if chosen is None:
        logger.info(
            "Cadastral: %d match(es) for %s but none inside the Keys bounding "
            "box; not trusting a cross-county collision.", len(feats), digits
        )
        return None

    return _cadastral_attrs_to_record(
        chosen["attributes"], chosen.get("_latlon"),
        Path(cache_root), cache_key,
        parcel_id=parcel_id,
        where_clause=f"PARCEL_ID LIKE '%{digits}'",
    )


def _derive_appraiser_parcel(attrs: dict) -> Optional[str]:
    """
    Derive the Monroe appraiser parcel format (NNNNNNNN-NNNNNN) from the
    cadastral PARCELNO/PARCEL_ID (e.g. '3366 28 00185410000000' -> '00185410-000000').
    """
    raw = _ci(attrs, "PARCELNO", "PARCEL_ID") or ""
    m = re.search(r"(\d{14})\s*$", str(raw).replace(" ", " ").strip())
    if not m:
        # Fall back to the longest digit run.
        runs = re.findall(r"\d{8,}", str(raw))
        if not runs:
            return None
        digits = max(runs, key=len)
    else:
        digits = m.group(1)
    digits = digits[-14:]
    if len(digits) == 14:
        return f"{digits[:8]}-{digits[8:]}"
    return digits


def _cadastral_attrs_to_record(
    attrs: dict,
    latlon,
    cache_root: Path,
    cache_key: str,
    parcel_id: Optional[str],
    where_clause: str,
) -> AppraiserRecord:
    """Map a single FDOR cadastral feature's attributes to an AppraiserRecord."""
    # Persist the raw JSON snapshot for provenance.
    snap_dir = Path(cache_root) / "cadastral"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_path = snap_dir / f"{cache_key}.json"
    try:
        snap_path.write_text(json.dumps(attrs, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass

    if not parcel_id:
        parcel_id = _derive_appraiser_parcel(attrs)

    # --- Owner ---
    own_name = _ci(attrs, "OWN_NAME")
    owner_names = [own_name.strip()] if own_name else []

    own_addr = _ci(attrs, "OWN_ADDR1")
    own_city = _ci(attrs, "OWN_CITY")
    own_state = _ci(attrs, "OWN_STATE")
    own_zip = _ci(attrs, "OWN_ZIPCD")
    mailing = None
    if own_addr:
        mailing = ", ".join(
            [str(p) for p in [own_addr, own_city, f"{own_state or ''} {own_zip or ''}".strip()] if p]
        )

    # --- Situs / physical address ---
    phy_addr = _ci(attrs, "PHY_ADDR1")
    phy_city = _ci(attrs, "PHY_CITY")
    phy_zip = _ci(attrs, "PHY_ZIPCD")
    situs = None
    if phy_addr:
        situs = ", ".join(
            [str(p) for p in [phy_addr, phy_city, f"FL {phy_zip or ''}".strip()] if p]
        )

    jurisdiction = _jurisdiction_from_city(phy_city)

    # --- Use code ---
    dor_uc = _ci(attrs, "DOR_UC")
    use_desc = None
    if dor_uc is not None:
        code = str(dor_uc).strip().zfill(3)
        use_desc = f"{code} - {_DOR_USE_CODES.get(code, 'see FL DOR use-code table')}"

    # --- Lot size ---
    lot_sqft = _as_int(_ci(attrs, "LND_SQFOOT"))
    lot_acres = round(lot_sqft / 43560.0, 4) if lot_sqft else None

    # --- Improvement (building) ---
    improvements = []
    act_yr = _as_int(_ci(attrs, "ACT_YR_BLT"))
    eff_yr = _as_int(_ci(attrs, "EFF_YR_BLT"))
    living = _as_int(_ci(attrs, "TOT_LVG_AR"))
    if (act_yr and act_yr > 1800) or living:
        improvements.append(ImprovementDetail(
            description=(use_desc or "Building"),
            year_built=act_yr if act_yr and act_yr > 1800 else None,
            effective_year=eff_yr if eff_yr and eff_yr > 1800 else None,
            living_area=living,
            gross_area=None,
            stories=None,
            construction_type=None,
            roof_type=None,
            beds=None,
            baths=None,
            assessed_value=None,
        ))

    # --- Valuation (current assessment year only; the roll is a single year) ---
    valuation_history = []
    asmnt_yr = _as_int(_ci(attrs, "ASMNT_YR"))
    jv = _as_int(_ci(attrs, "JV"))
    av_nsd = _as_int(_ci(attrs, "AV_NSD"))
    av_sd = _as_int(_ci(attrs, "AV_SD"))
    tv_nsd = _as_int(_ci(attrs, "TV_NSD"))
    tv_sd = _as_int(_ci(attrs, "TV_SD"))
    lnd_val = _as_int(_ci(attrs, "LND_VAL"))
    assessed = av_nsd if av_nsd is not None else av_sd
    county_taxable = tv_nsd if tv_nsd is not None else tv_sd
    bldg_val = (jv - lnd_val) if (jv is not None and lnd_val is not None) else None
    homestead = bool(assessed is not None and county_taxable is not None
                     and assessed > county_taxable)
    hmstd_amt = (assessed - county_taxable) if homestead else None
    soh = (jv - assessed) if (jv is not None and assessed is not None and jv > assessed) else None
    if asmnt_yr:
        valuation_history.append(ValuationYear(
            year=asmnt_yr,
            just_value=jv,
            assessed_value=assessed,
            taxable_value=county_taxable,
            school_taxable=tv_sd,
            land_value=lnd_val,
            building_value=bldg_val,
            homestead_exemption=homestead,
            homestead_amount=hmstd_amt,
            soh_cap_differential=soh,
        ))

    # --- Sales history (the roll carries up to two prior sales) ---
    sales_history = []
    for n in ("1", "2"):
        price = _as_int(_ci(attrs, f"SALE_PRC{n}"))
        yr = _as_int(_ci(attrs, f"SALE_YR{n}"))
        if not price and not yr:
            continue
        mo = _ci(attrs, f"SALE_MO{n}")
        date = None
        if yr:
            date = f"{yr}-{str(mo).strip().zfill(2)}" if mo and str(mo).strip() else str(yr)
        sales_history.append(SalesRecord(
            date=date,
            price=price,
            or_book=_ci(attrs, f"OR_BOOK{n}"),
            or_page=_ci(attrs, f"OR_PAGE{n}"),
            instrument_number=_ci(attrs, f"CLERK_NO{n}"),
            grantor=None,   # the roll does not carry grantor/grantee names
            grantee=None,
            sale_qualification=_ci(attrs, f"QUAL_CD{n}"),
            deed_type=None,
        ))

    legal = _ci(attrs, "S_LEGAL")
    alt_key = _ci(attrs, "ALT_KEY")

    from urllib.parse import quote
    record = AppraiserRecord(
        parcel_id=parcel_id,
        re_number=alt_key,
        alternate_key=alt_key,
        folio=parcel_id,
        owner_names=owner_names,
        mailing_address=mailing,
        situs_address=situs,
        jurisdiction=jurisdiction,
        legal_description=legal,
        subdivision=None,
        block=None,
        lot=None,
        property_use_code=use_desc,
        lot_size_sqft=float(lot_sqft) if lot_sqft else None,
        lot_size_acres=lot_acres,
        zoning=None,
        land_use=use_desc,
        improvements=improvements,
        valuation_history=valuation_history,
        sales_history=sales_history,
        provenance=Provenance(
            source_name=CADASTRAL_SOURCE,
            source_url=f"{CADASTRAL_QUERY_URL}?where={quote(where_clause)}",
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            cache_path=str(snap_path),
        ),
    )
    if latlon:
        record.parcel_centroid = {"lat": latlon[0], "lon": latlon[1]}
    logger.info(
        "Cadastral: resolved parcel %s -> owner=%s, JV=%s, yr_built=%s, living=%s",
        parcel_id, owner_names, jv, act_yr, living,
    )
    return record


def fetch_via_cadastral_by_address(
    address: str,
    cache_root: Path,
    cache_key: str,
    session: Optional[requests.Session] = None,
) -> Optional[AppraiserRecord]:
    """
    Resolve the appraiser record from the FDOR cadastral by street address when
    no parcel ID is available. Geocodes the address (US Census), then runs a
    spatially-indexed envelope query (the address field itself is not indexed),
    and picks the parcel whose physical address best matches.
    """
    if not address:
        return None
    sess = session or requests
    # Reuse the flood adapter's Census geocoder.
    try:
        from .flood import geocode as _geocode
        latlon = _geocode(address, cache_root)
    except Exception as exc:
        logger.info("Address geocode for cadastral failed: %s", exc)
        latlon = None
    if not latlon:
        return None
    lat, lon = latlon

    d = 0.0015  # ~150 m envelope
    params = {
        "geometry": f"{lon - d},{lat - d},{lon + d},{lat + d}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*", "returnGeometry": "true", "f": "json",
    }
    data = None
    for attempt in range(4):
        try:
            resp = sess.get(CADASTRAL_QUERY_URL, params=params, timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"})
            data = resp.json()
            break
        except Exception as exc:
            logger.info("Cadastral envelope attempt %d failed (%s)", attempt + 1, exc)
            time.sleep(2 * (attempt + 1))
    if not data:
        return None

    feats = data.get("features", [])
    if not feats:
        logger.info("Cadastral by-address: no parcels near %s", address)
        return None

    # Match the street number + first street-name token against PHY_ADDR1.
    m_num = re.match(r"\s*(\d+)", address)
    street_num = m_num.group(1) if m_num else None
    addr_upper = address.upper()
    best = None
    for f in feats:
        pa = (f["attributes"].get("PHY_ADDR1") or "").upper()
        if street_num and pa.strip().startswith(street_num + " "):
            best = f
            break
    if best is None:
        # Fall back to the geographically nearest feature centroid.
        def _dist(f):
            ll = _ring_centroid(f.get("geometry") or {})
            return ((ll[0] - lat) ** 2 + (ll[1] - lon) ** 2) if ll else 9e9
        best = min(feats, key=_dist)

    best["_latlon"] = _ring_centroid(best.get("geometry") or {}) or (lat, lon)
    return _cadastral_attrs_to_record(
        best["attributes"], best["_latlon"],
        Path(cache_root), cache_key,
        parcel_id=None,  # derived from the matched feature
        where_clause=f"envelope near {address}",
    )


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

    # ── Primary path: FDOR Statewide Cadastral over plain HTTP (no Cloudflare).
    # Only viable when we have a parcel ID (the roll is keyed by parcel number,
    # not address). This is tried first because it is far more reliable than the
    # Cloudflare-protected qPublic site and needs no browser.
    if parcel_id:
        try:
            cad = fetch_via_cadastral(parcel_id, Path(cache_root), cache_key)
            if cad and cad.owner_names:
                logger.info("Property appraiser resolved via FDOR Cadastral (by parcel).")
                return cad
            logger.info("Cadastral by-parcel returned nothing; trying by-address/qPublic.")
        except Exception as exc:
            logger.warning("Cadastral by-parcel errored (%s); trying by-address/qPublic.", exc)

    # No parcel ID (or parcel lookup empty): resolve by street address via the
    # spatial cadastral query. This lets `--mls <n> --address "..."` work end to
    # end even when the listing is an IDX page that never exposes a parcel number.
    if address:
        try:
            cad = fetch_via_cadastral_by_address(address, Path(cache_root), cache_key)
            if cad and cad.owner_names:
                logger.info("Property appraiser resolved via FDOR Cadastral (by address).")
                return cad
            logger.info("Cadastral by-address returned nothing; trying qPublic.")
        except Exception as exc:
            logger.warning("Cadastral by-address errored (%s); trying qPublic.", exc)

    # Cache check (qPublic browser path)
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
