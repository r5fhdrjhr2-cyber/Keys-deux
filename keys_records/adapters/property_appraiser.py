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
    PermitRecord,
    Provenance,
    SalesRecord,
    ValuationYear,
)

logger = logging.getLogger("adapter.property_appraiser")

BASE_URL = "https://qpublic.schneidercorp.com/Application.aspx?AppID=605"
SOURCE_NAME = "Monroe County Property Appraiser (qPublic)"

# Direct deep-link bypasses the search form. Format with parcel_id.
# Pattern confirmed from user ground-truth PDF for parcel 00341780-000000.
QPUBLIC_DIRECT_TEMPLATE = (
    "https://qpublic.schneidercorp.com/Application.aspx"
    "?AppID=605&LayerID=9946&PageTypeID=4&PageID=7635&KeyValue={parcel_id}"
)
QPUBLIC_SOURCE = "Monroe County Property Appraiser (qPublic, direct property record)"

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


# ---------------------------------------------------------------------------
# qPublic direct deep-link scraper
# ---------------------------------------------------------------------------

def _find_col(col_map: dict, cells: list, *keys: str) -> Optional[str]:
    """Return the first matching cell value for any key substring in col_map."""
    for k in keys:
        k_up = k.upper()
        for col, idx in col_map.items():
            if k_up in col and idx < len(cells):
                return cells[idx]
    return None


def _qpub_money(s: Optional[str]) -> Optional[int]:
    """Parse '$763,425' or '763425' to int, return None on failure."""
    if not s:
        return None
    cleaned = re.sub(r"[^0-9]", "", s)
    return int(cleaned) if cleaned else None


def _section_heading_table(soup: BeautifulSoup, *keywords: str) -> Optional[BeautifulSoup]:
    """Find the first <table> that follows a heading containing any keyword."""
    kw_up = [k.upper() for k in keywords]
    for h in soup.find_all(["h2", "h3", "h4", "h5", "strong"]):
        title = h.get_text(strip=True).upper()
        if any(kw in title for kw in kw_up):
            tbl = h.find_next("table")
            if tbl:
                return tbl
    return None


def _parse_qpublic_owners(soup: BeautifulSoup) -> list:
    """Extract owner names from the Owner section of a qPublic page."""
    owners = []
    for h in soup.find_all(["h2", "h3", "h4", "h5"]):
        if h.get_text(strip=True).upper() in ("OWNER", "OWNER(S)", "OWNERS"):
            # Collect text from siblings until next heading
            for sib in h.next_siblings:
                if sib.name and re.match(r"^h[2-5]$", sib.name):
                    break
                if not hasattr(sib, "get_text"):
                    continue
                for line in sib.get_text(separator="\n").split("\n"):
                    name = line.strip()
                    if (name and len(name) >= 3
                            and not re.match(r"^\d", name)
                            and name.upper() not in ("OWNER", "FL", "N/A", "NONE")):
                        owners.append(name)
            break
    # De-dup address lines (contain city/state/zip patterns)
    cleaned = []
    for n in owners:
        if re.search(r"\bFL\s+\d{5}\b", n):
            continue
        if re.search(r"^\d+\s+\w", n):  # street number address lines
            continue
        cleaned.append(n)
    return cleaned


def _parse_qpublic_historical(soup: BeautifulSoup) -> list:
    """Parse Historical Assessments table → list of ValuationYear."""
    valuation_history = []
    tbl = _section_heading_table(soup, "Historical Assessments")
    if not tbl:
        # Fallback: table with Year + Land Value + Just + Assessed headers
        for t in soup.find_all("table"):
            hdrs = [th.get_text(strip=True).upper() for th in t.find_all("th")]
            hdr_str = " ".join(hdrs)
            if "YEAR" in hdrs and "LAND VALUE" in hdr_str and "ASSESSED VALUE" in hdr_str:
                tbl = t
                break
    if not tbl:
        return valuation_history

    hdrs_raw = [th.get_text(strip=True) for th in tbl.find_all("th")]
    col_map = {h.upper(): i for i, h in enumerate(hdrs_raw)}

    for row in tbl.find_all("tr")[1:]:
        cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
        if not cells:
            continue
        yr_raw = _find_col(col_map, cells, "YEAR") or (cells[0] if cells else None)
        yr = _parse_int(yr_raw)
        if not yr or yr < 1990 or yr > 2035:
            continue
        exempt_raw = _find_col(col_map, cells, "EXEMPT VALUE", "EXEMPT")
        valuation_history.append(ValuationYear(
            year=yr,
            just_value=_qpub_money(_find_col(col_map, cells, "JUST (MARKET)", "JUST")),
            assessed_value=_qpub_money(_find_col(col_map, cells, "ASSESSED VALUE")),
            taxable_value=_qpub_money(_find_col(col_map, cells, "TAXABLE VALUE")),
            school_taxable=None,
            land_value=_qpub_money(_find_col(col_map, cells, "LAND VALUE")),
            building_value=_qpub_money(_find_col(col_map, cells, "BUILDING VALUE")),
            homestead_exemption=bool(_qpub_money(exempt_raw)),
            homestead_amount=_qpub_money(exempt_raw),
            soh_cap_differential=None,
        ))

    return valuation_history


def _parse_qpublic_land(soup: BeautifulSoup) -> dict:
    """Parse the Land section → {'lot_sqft': int|None, 'land_use': str|None, ...}."""
    result = {"lot_sqft": None, "land_use": None, "frontage": None, "depth": None}
    tbl = _section_heading_table(soup, "Land")
    if not tbl:
        for t in soup.find_all("table"):
            hdr_str = " ".join(th.get_text(strip=True).upper() for th in t.find_all("th"))
            if "LAND USE" in hdr_str or ("FRONTAGE" in hdr_str and "DEPTH" in hdr_str):
                tbl = t
                break
    if not tbl:
        return result

    hdrs_raw = [th.get_text(strip=True) for th in tbl.find_all("th")]
    col_map = {h.upper(): i for i, h in enumerate(hdrs_raw)}
    for row in tbl.find_all("tr")[1:]:
        cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
        if not cells:
            continue
        result["land_use"] = _find_col(col_map, cells, "LAND USE") or result["land_use"]
        result["frontage"] = _parse_int(_find_col(col_map, cells, "FRONTAGE")) or result["frontage"]
        result["depth"] = _parse_int(_find_col(col_map, cells, "DEPTH")) or result["depth"]
        # Number of units + "Square Foot" unit type → lot sqft
        units_raw = _find_col(col_map, cells, "NUMBER OF UNITS", "UNITS")
        unit_type = _find_col(col_map, cells, "UNIT TYPE", "TYPE")
        if units_raw and unit_type and "SQUARE" in unit_type.upper():
            result["lot_sqft"] = _parse_int(units_raw.replace(",", ""))

    # Fallback: frontage × depth
    if not result["lot_sqft"] and result["frontage"] and result["depth"]:
        result["lot_sqft"] = result["frontage"] * result["depth"]

    return result


def _parse_qpublic_buildings(soup: BeautifulSoup) -> list:
    """Parse the Buildings section → list of ImprovementDetail."""
    improvements = []
    tbl = _section_heading_table(soup, "Buildings")
    if not tbl:
        return improvements

    # qPublic buildings tables use label-value pairs. Common layouts:
    # 2-col (label | value) or 4-col (label | val | label | val) side by side.
    labeled: dict = {}
    for row in tbl.find_all("tr"):
        cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
        if not cells:
            continue
        if len(cells) == 2:
            k = cells[0].rstrip(":").strip().upper()
            if k and len(k) < 50:
                labeled[k] = cells[1]
        elif len(cells) >= 4:
            for i in range(0, len(cells) - 1, 2):
                k = cells[i].rstrip(":").strip().upper()
                if k and len(k) < 50:
                    labeled[k] = cells[i + 1] if i + 1 < len(cells) else ""

    if not labeled:
        return improvements

    def _lget(*keys):
        for k in keys:
            if k in labeled and labeled[k]:
                return labeled[k]
        return None

    eff_yr_raw = (_lget("EFFECTIVEYEARBUILT", "EFFECTIVE YEAR BUILT", "EFF YEAR", "EFF YR BUILT")
                  or "")
    stories_raw = _lget("STORIES") or ""
    stories_val = None
    m_st = re.search(r"(\d+(?:\.\d)?)", stories_raw)
    if m_st:
        try:
            stories_val = float(m_st.group(1))
        except ValueError:
            pass

    beds_raw = _lget("BEDROOMS", "BEDS") or ""
    baths_raw = _lget("FULL BATHROOMS", "FULL BATHS", "BATHROOMS") or ""

    improvements.append(ImprovementDetail(
        description=str(_lget("BUILDING TYPE", "STYLE") or "Building"),
        year_built=_parse_year(_lget("YEAR BUILT")),
        effective_year=_parse_year(eff_yr_raw) if eff_yr_raw else None,
        living_area=_parse_int(_lget("FINISHED SQ FT", "FINISHED AREA", "LIVING AREA")),
        gross_area=_parse_int(_lget("GROSS SQ FT", "GROSS AREA")),
        stories=stories_val,
        construction_type=_lget("EXTERIOR WALLS"),
        roof_type=_lget("ROOF COVERAGE", "ROOF TYPE"),
        beds=float(beds_raw) if re.match(r"^\d", beds_raw) else None,
        baths=float(baths_raw) if re.match(r"^\d", baths_raw) else None,
        assessed_value=None,
    ))
    return improvements


def _parse_qpublic_sales(soup: BeautifulSoup) -> list:
    """Parse the Sales table → list of SalesRecord."""
    sales: list = []
    tbl = _section_heading_table(soup, "Sales")
    if not tbl:
        for t in soup.find_all("table"):
            hdr_str = " ".join(th.get_text(strip=True).upper() for th in t.find_all("th"))
            if "SALE DATE" in hdr_str and "SALE PRICE" in hdr_str:
                tbl = t
                break
    if not tbl:
        return sales

    hdrs_raw = [th.get_text(strip=True) for th in tbl.find_all("th")]
    col_map = {h.upper(): i for i, h in enumerate(hdrs_raw)}

    for row in tbl.find_all("tr")[1:]:
        cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
        if not cells or not any(cells):
            continue
        price_raw = _find_col(col_map, cells, "SALE PRICE", "PRICE")
        instr_num = _find_col(col_map, cells, "INSTRUMENT NUMBER")
        instr_type = _find_col(col_map, cells, "INSTRUMENT")
        # Avoid using the full "INSTRUMENT NUMBER" column as deed_type
        deed_type = instr_type if (instr_type and not instr_num) else _find_col(col_map, cells, "DEED TYPE")
        sales.append(SalesRecord(
            date=_find_col(col_map, cells, "SALE DATE", "DATE"),
            price=_qpub_money(price_raw),
            or_book=_find_col(col_map, cells, "DEED BOOK", "OR BOOK", "BOOK"),
            or_page=_find_col(col_map, cells, "DEED PAGE", "OR PAGE", "PAGE"),
            instrument_number=instr_num,
            grantor=_find_col(col_map, cells, "GRANTOR"),
            grantee=_find_col(col_map, cells, "GRANTEE"),
            sale_qualification=_find_col(col_map, cells, "SALE QUALIFICATION", "QUALIFICATION", "QUAL"),
            deed_type=deed_type,
        ))
    return sales


def _parse_qpublic_permits(
    soup: BeautifulSoup,
    page_url: str,
    cache_path: str,
) -> list:
    """Parse the Permits table → list of PermitRecord."""
    permits: list = []
    tbl = _section_heading_table(soup, "Permits")
    if not tbl:
        for t in soup.find_all("table"):
            hdr_str = " ".join(th.get_text(strip=True).upper() for th in t.find_all("th"))
            if "DATE ISSUED" in hdr_str and "STATUS" in hdr_str:
                tbl = t
                break
    if not tbl:
        return permits

    hdrs_raw = [th.get_text(strip=True) for th in tbl.find_all("th")]
    col_map = {h.upper(): i for i, h in enumerate(hdrs_raw)}
    now = datetime.now(timezone.utc).isoformat()

    for row in tbl.find_all("tr")[1:]:
        cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
        if not cells or not any(cells):
            continue
        pnum = _find_col(col_map, cells, "NUMBER", "PERMIT NUMBER", "PERMIT #") or cells[0] or "UNKNOWN"
        amount_raw = _find_col(col_map, cells, "AMOUNT")
        declared_value = None
        if amount_raw:
            c = re.sub(r"[^0-9.]", "", amount_raw.replace(",", ""))
            try:
                declared_value = float(c) if c else None
            except ValueError:
                pass
        permits.append(PermitRecord(
            jurisdiction="Monroe County (MCPA qPublic)",
            source_system=QPUBLIC_SOURCE,
            permit_number=pnum,
            permit_type=_find_col(col_map, cells, "PERMIT TYPE", "TYPE"),
            subtype=None,
            description=_find_col(col_map, cells, "NOTES", "DESCRIPTION"),
            status=_find_col(col_map, cells, "STATUS"),
            applied_date=None,
            issued_date=_find_col(col_map, cells, "DATE ISSUED", "DATE"),
            finaled_date=None,
            expiration_date=None,
            contractor_name=None,
            contractor_license=None,
            declared_value=declared_value,
            inspections=[],
            provenance=Provenance(
                source_name=QPUBLIC_SOURCE,
                source_url=page_url,
                retrieved_at=now,
                cache_path=cache_path,
            ),
        ))
    return permits


def _parse_qpublic_page(
    html: str,
    parcel_id: str,
    page_url: str,
    cache_path: str,
) -> AppraiserRecord:
    """
    Parse a qPublic direct-URL property record card.

    Extracts: summary fields, owners, multi-year valuation history (Historical
    Assessments table), land, buildings, sales, and permit history.
    All values are taken literally from the page — nothing is inferred.
    """
    soup = BeautifulSoup(html, "lxml")

    # ── Summary (key-value labeled table rows) ────────────────────────────────
    summary: dict = {}
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) == 2:
            k = cells[0].get_text(separator=" ", strip=True).rstrip(":").upper().strip()
            v = cells[1].get_text(separator=" ", strip=True)
            if k and len(k) < 60 and k != v.upper():
                summary[k] = v

    parcel_out = summary.get("PARCEL ID") or parcel_id
    account = summary.get("ACCOUNT#") or summary.get("ACCOUNT")
    millage = summary.get("MILLAGE GROUP")
    location = summary.get("LOCATION ADDRESS") or summary.get("LOCATION")
    neighborhood = summary.get("NEIGHBORHOOD")
    prop_class = summary.get("PROPERTY CLASS")
    subdivision = summary.get("SUBDIVISION")
    sec_twp_rng = summary.get("SEC/TWP/RNG")
    legal = summary.get("LEGAL DESCRIPTION")

    # ── Owners ────────────────────────────────────────────────────────────────
    owner_names = _parse_qpublic_owners(soup)
    if not owner_names:
        raw = summary.get("OWNER NAME") or summary.get("OWNER") or ""
        if raw:
            owner_names = [n.strip() for n in raw.split("\n") if n.strip()]

    # ── Valuation history (Historical Assessments table) ──────────────────────
    valuation_history = _parse_qpublic_historical(soup)

    # ── Land ─────────────────────────────────────────────────────────────────
    land = _parse_qpublic_land(soup)
    lot_sqft = land.get("lot_sqft")
    land_use_str = land.get("land_use")
    lot_acres = round(lot_sqft / 43560.0, 4) if lot_sqft else None

    # ── Buildings ────────────────────────────────────────────────────────────
    improvements = _parse_qpublic_buildings(soup)

    # ── Sales ────────────────────────────────────────────────────────────────
    sales_history = _parse_qpublic_sales(soup)

    # ── Permits ──────────────────────────────────────────────────────────────
    mcpa_permits = _parse_qpublic_permits(soup, page_url, cache_path)

    # ── Situs address ─────────────────────────────────────────────────────────
    situs = location or summary.get("ADDRESS") or None

    logger.info(
        "qPublic parsed: parcel=%s owners=%s val_years=%d improvements=%d sales=%d permits=%d",
        parcel_out, owner_names, len(valuation_history), len(improvements),
        len(sales_history), len(mcpa_permits),
    )

    return AppraiserRecord(
        parcel_id=parcel_out or parcel_id,
        re_number=account,
        alternate_key=account,
        folio=parcel_out or parcel_id,
        owner_names=owner_names,
        mailing_address=None,
        situs_address=situs,
        jurisdiction=None,  # set downstream from cadastral or city detection
        legal_description=legal,
        subdivision=subdivision,
        block=None,
        lot=None,
        property_use_code=prop_class,
        lot_size_sqft=float(lot_sqft) if lot_sqft else None,
        lot_size_acres=lot_acres,
        zoning=None,
        land_use=land_use_str or prop_class,
        improvements=improvements,
        valuation_history=valuation_history,
        sales_history=sales_history,
        mcpa_permits=mcpa_permits,
        provenance=Provenance(
            source_name=QPUBLIC_SOURCE,
            source_url=page_url,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            cache_path=cache_path,
        ),
    )


def fetch_via_qpublic_direct(
    parcel_id: str,
    client: PoliteClient,
    cache_root: Path,
    cache_key: str,
) -> Optional[AppraiserRecord]:
    """
    Navigate to the qPublic direct deep-link for parcel_id and extract the
    full property record card, including multi-year valuation history and
    MCPA permit history.

    Uses the URL pattern:
      https://qpublic.schneidercorp.com/Application.aspx
        ?AppID=605&LayerID=9946&PageTypeID=4&PageID=7635&KeyValue={parcel_id}

    On any block, CAPTCHA, or challenge page: stops immediately, logs, and
    returns None. Never attempts to solve CAPTCHAs.
    """
    url = QPUBLIC_DIRECT_TEMPLATE.format(parcel_id=parcel_id)

    # Check cache first (avoids re-hitting the site on repeated runs)
    snap_dir = cache_root / "qpublic_direct"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_path_str = str(snap_dir / f"{cache_key}.html")

    cached = client.load_snapshot("qpublic_direct", cache_key)
    if cached:
        logger.info("Cache hit for qpublic_direct/%s", cache_key)
        return _parse_qpublic_page(cached, parcel_id, url, snap_path_str)

    try:
        page = client.navigate(url)
        html_pre = page.content()

        # Hard stop on any Cloudflare/CAPTCHA/block signal.
        # Policy: stop and flag; never push through.
        lower = html_pre.lower()
        block_signals = (
            "just a moment",
            "checking your browser",
            "cloudflare",
            "enable javascript",
            "captcha",
            "access denied",
            "403 forbidden",
            "you have been blocked",
        )
        if any(sig in lower for sig in block_signals):
            logger.warning(
                "qPublic direct URL blocked (Cloudflare/CAPTCHA) for parcel %s. "
                "Stopping per no-CAPTCHA-solving policy. Use the manual-retrieval "
                "link: %s", parcel_id, url
            )
            return None

        # Accept disclaimer if present (session persistence caches this)
        _handle_disclaimer(page)
        client.save_domain_session("qpublic.schneidercorp.com")

        html = page.content()
        snap_path = client.save_snapshot("qpublic_direct", cache_key, html, "html")

        return _parse_qpublic_page(html, parcel_id, page.url, str(snap_path))

    except Exception as exc:
        logger.error("qPublic direct fetch for parcel %s failed: %s", parcel_id, exc)
        return None


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

    Two-stage approach:
    1. FDOR Statewide Cadastral (ArcGIS REST, plain JSON, no Cloudflare):
       resolves owner, valuation, jurisdiction, parcel centroid. Fast and reliable.
    2. qPublic direct deep-link (Playwright, behind PoliteClient):
       enriches with multi-year valuation history, building detail, sales history,
       and MCPA permit history — data not available in the cadastral.

    If qPublic is blocked or fails, the cadastral-only record is returned.
    On block/CAPTCHA: stops immediately, never tries to solve it.
    """
    if cache_root is None:
        cache_root = client.cache_root
    cache_root = Path(cache_root)

    cache_key = re.sub(r"[^\w\-]", "_", parcel_id or address or "unknown")[:80]

    # ── Stage 1: FDOR Statewide Cadastral (primary identifier) ───────────────
    cad_record: Optional[AppraiserRecord] = None

    if parcel_id:
        try:
            cad_record = fetch_via_cadastral(parcel_id, cache_root, cache_key)
            if cad_record and cad_record.owner_names:
                logger.info("Cadastral resolved by parcel: owner=%s", cad_record.owner_names)
            else:
                logger.info("Cadastral by-parcel returned no data.")
        except Exception as exc:
            logger.warning("Cadastral by-parcel errored: %s", exc)

    if not cad_record and address:
        try:
            cad_record = fetch_via_cadastral_by_address(address, cache_root, cache_key)
            if cad_record and cad_record.owner_names:
                logger.info("Cadastral resolved by address: owner=%s", cad_record.owner_names)
            else:
                logger.info("Cadastral by-address returned no data.")
        except Exception as exc:
            logger.warning("Cadastral by-address errored: %s", exc)

    # Resolve actual parcel ID (supplied arg takes precedence over derived)
    actual_parcel_id = parcel_id or (
        cad_record and (cad_record.parcel_id or cad_record.folio)
    )

    # ── Stage 2: qPublic direct deep-link (enrichment) ────────────────────────
    # Requires a parcel ID. Adds multi-year valuations, building detail, MCPA permits.
    qpub_record: Optional[AppraiserRecord] = None
    if actual_parcel_id:
        logger.info("Stage 2: qPublic direct deep-link for parcel %s", actual_parcel_id)
        qpub_record = fetch_via_qpublic_direct(
            actual_parcel_id, client, cache_root, cache_key
        )

    # ── Merge ─────────────────────────────────────────────────────────────────
    if qpub_record:
        # qPublic has richer data; use it as primary with cadastral supplementing.
        # Centroid from cadastral (has geometry; qPublic does not)
        if cad_record and cad_record.parcel_centroid:
            qpub_record.parcel_centroid = cad_record.parcel_centroid
        # Jurisdiction from cadastral (city-to-permit-router) if qPublic didn't resolve
        if not qpub_record.jurisdiction and cad_record:
            qpub_record.jurisdiction = cad_record.jurisdiction
        # Owner names: qPublic is authoritative if it has them; fall back to cadastral
        if not qpub_record.owner_names and cad_record:
            qpub_record.owner_names = cad_record.owner_names
        # Situs address from cadastral if qPublic page omitted it
        if not qpub_record.situs_address and cad_record:
            qpub_record.situs_address = cad_record.situs_address
        # If valuation history is missing from qPublic but present in cadastral, use cadastral
        if not qpub_record.valuation_history and cad_record:
            qpub_record.valuation_history = cad_record.valuation_history
        logger.info(
            "qPublic merged: val_years=%d, permits=%d, improvements=%d",
            len(qpub_record.valuation_history),
            len(qpub_record.mcpa_permits),
            len(qpub_record.improvements),
        )
        return qpub_record

    if cad_record:
        logger.info("qPublic direct unavailable; returning FDOR Cadastral record only.")
        return cad_record

    logger.warning("Both FDOR Cadastral and qPublic returned no data for this parcel/address.")
    return None
