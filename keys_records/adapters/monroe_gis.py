"""
Monroe County GIS — Parcels and Condos adapter.

Source: Monroe County's own ArcGIS Online organization (MonroeCountyGIS),
the public "Current Parcels" feature service:

  https://services.arcgis.com/D7K7hj5GW1YIVRiA/arcgis/rest/services/
      Current_Parcels/FeatureServer/9/query

Why this exists
---------------
qPublic / mcpafl.org (the Monroe County Property Appraiser web front end) sits
behind Cloudflare and blocks automated access from any server environment
(plain HTTP gets a 403 challenge; a real headless browser gets the connection
closed at the egress proxy). The Florida DOR Statewide Cadastral works but
carries only the *current* assessment year.

Monroe County publishes the same appraiser roll as an open ArcGIS feature
service that answers plain JSON over HTTPS with no Cloudflare and no browser.
It carries, per parcel:
  - owner name + mailing address
  - physical/situs address, legal description, subdivision, millage group
  - year built, finished living area, land use, lot area
  - the most recent qualified sale (price, month/year, OR book/page, qual code)
  - TWO assessment years of valuation (current + prior): land, building,
    just/market, assessed, taxable, exempt
  - the parcel centroid (lat/lon)
  - the exact qPublic property-record-card deep link (MCPA_URL)

This is strictly richer than the FDOR cadastral and is reachable everywhere, so
it is the primary appraiser source. qPublic (deep link) remains the enrichment
source for the FULL multi-year history (7 years), the FULL sales history, and
permit history — none of which any open dataset carries.

Provenance is attached to every record.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from ..schemas import (
    AppraiserRecord,
    ImprovementDetail,
    Provenance,
    SalesRecord,
    ValuationYear,
)

logger = logging.getLogger("adapter.monroe_gis")

GIS_QUERY_URL = (
    "https://services.arcgis.com/D7K7hj5GW1YIVRiA/arcgis/rest/services/"
    "Current_Parcels/FeatureServer/9/query"
)
SOURCE_NAME = "Monroe County GIS (Parcels & Condos, ArcGIS REST)"

# The five incorporated municipalities; everything else routes to the
# unincorporated county permit systems.
_INCORPORATED_CITIES = {
    "KEY WEST", "MARATHON", "ISLAMORADA", "KEY COLONY BEACH", "LAYTON",
}

# Florida DOR use codes (residential subset) for human-readable output.
_DOR_USE_CODES = {
    "0000": "Vacant Residential", "0100": "Single Family", "0200": "Mobile Home",
    "0300": "Multi-Family (10+ units)", "0400": "Condominium", "0500": "Cooperative",
    "0800": "Multi-Family (<10 units)",
}


def _as_int(v) -> Optional[int]:
    try:
        if v in (None, "", " "):
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _jurisdiction(keyname: Optional[str], city: Optional[str]) -> Optional[str]:
    """Derive a permit-routing jurisdiction from KEYNAME / city."""
    for cand in (keyname, city):
        if not cand:
            continue
        c = cand.upper().strip()
        for inc in _INCORPORATED_CITIES:
            if inc in c:
                return inc
    # Unrecognized -> the router falls back to unincorporated county systems.
    return (keyname or city or "").upper().strip() or None


def _use_desc(pc: Optional[str]) -> Optional[str]:
    if pc in (None, ""):
        return None
    code = str(pc).strip()
    return f"{code} - {_DOR_USE_CODES.get(code, 'see FL DOR use-code table')}"


def _valuation_years(a: dict) -> list:
    """Build ValuationYear entries from the PYEAR2/PYEAR3 columns (2 years)."""
    out = []
    for suffix in ("2", "3"):
        yr = _as_int(a.get(f"PYEAR{suffix}"))
        if not yr:
            continue
        land = _as_int(a.get(f"PLAND{suffix}"))
        bldg = _as_int(a.get(f"PBLDG{suffix}"))
        misc = _as_int(a.get(f"PMISC{suffix}"))
        just = _as_int(a.get(f"PJUST{suffix}"))
        assd = _as_int(a.get(f"PASSD{suffix}"))
        taxbl = _as_int(a.get(f"PTAX{suffix}"))
        exem = _as_int(a.get(f"PEXEM{suffix}"))
        homestead = bool(exem)
        soh = (just - assd) if (just is not None and assd is not None and just > assd) else None
        out.append(ValuationYear(
            year=yr,
            just_value=just,
            assessed_value=assd,
            taxable_value=taxbl,
            school_taxable=taxbl,
            land_value=land,
            building_value=(bldg + misc) if (bldg is not None and misc is not None) else bldg,
            homestead_exemption=homestead,
            homestead_amount=exem if homestead else None,
            soh_cap_differential=soh,
        ))
    # Most recent year first.
    out.sort(key=lambda v: v.year, reverse=True)
    return out


def _sales(a: dict) -> list:
    """Build SalesRecord entries from SALE1/SALE2 (deduped)."""
    out = []
    seen = set()
    for suffix in ("1", "2"):
        price = _as_int(a.get(f"SALE{suffix}"))
        yr = a.get(f"Y{suffix}")
        mo = a.get(f"M{suffix}")
        book = a.get(f"ORBOOK{suffix}")
        page = a.get(f"ORPAGE{suffix}")
        qual = a.get(f"C{suffix}")
        if not price and not (book and page):
            continue
        key = (price, str(yr), str(book), str(page))
        if key in seen:
            continue
        seen.add(key)
        date = None
        if yr:
            date = f"{str(mo).zfill(2)}/{yr}" if mo else str(yr)
        out.append(SalesRecord(
            date=date,
            price=price,
            or_book=str(book) if book else None,
            or_page=str(page) if page else None,
            instrument_number=None,
            grantor=None,
            grantee=None,
            sale_qualification=str(qual) if qual else None,
            deed_type=None,
        ))
    return out


def _attrs_to_record(a: dict, cache_root: Path, cache_key: str) -> AppraiserRecord:
    snap_dir = cache_root / "monroe_gis"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_path = snap_dir / f"{cache_key}.json"
    try:
        snap_path.write_text(json.dumps(a, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass

    parcelno = (a.get("PARCELNO") or "").strip()
    digits = _digits(parcelno)[-14:]
    appraiser_parcel = f"{digits[:8]}-{digits[8:]}" if len(digits) == 14 else parcelno

    owner = (a.get("NAME") or "").strip()
    owner_names = [owner] if owner else []

    add1 = (a.get("ADD1") or "").strip()
    city = (a.get("CITY") or "").strip()
    state = (a.get("STATE") or "").strip()
    zipc = (a.get("ZIP") or "").strip()
    mailing = ", ".join([p for p in [add1, city, f"{state} {zipc}".strip()] if p]) or None

    situs = (a.get("LOCATION") or "").strip() or None

    legal = " ".join(
        p.strip() for p in [a.get("LEGAL1"), a.get("LEGAL2"), a.get("LEGAL3")] if p and p.strip()
    ) or None

    pc = a.get("PC")
    use_desc = _use_desc(pc)

    lot_sqft = _as_int(a.get("AREA1"))
    lot_acres = round(lot_sqft / 43560.0, 4) if lot_sqft else None

    yrblt = _as_int(a.get("YRBLT"))
    fla = _as_int(a.get("FLA"))
    improvements = []
    if (yrblt and yrblt > 1800) or fla:
        improvements.append(ImprovementDetail(
            description=use_desc or "Building",
            year_built=yrblt if yrblt and yrblt > 1800 else None,
            effective_year=None,   # not in this layer; qPublic carries it
            living_area=fla,
            gross_area=None,
            stories=None,
            construction_type=None,
            roof_type=None,
            beds=None,
            baths=None,
            assessed_value=None,
        ))

    valuation_history = _valuation_years(a)
    sales_history = _sales(a)

    lat = a.get("CENTROID_LAT")
    lon = a.get("CENTROID_LONG")
    centroid = None
    try:
        if lat is not None and lon is not None:
            centroid = {"lat": float(lat), "lon": float(lon)}
    except (TypeError, ValueError):
        centroid = None

    rec = AppraiserRecord(
        parcel_id=appraiser_parcel,
        re_number=str(a.get("AK")) if a.get("AK") else None,
        alternate_key=str(a.get("AK")) if a.get("AK") else None,
        folio=appraiser_parcel,
        owner_names=owner_names,
        mailing_address=mailing,
        situs_address=situs,
        jurisdiction=_jurisdiction(a.get("KEYNAME"), city),
        legal_description=legal,
        subdivision=(a.get("SUBDIVISION") or "").strip() or None,
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
            source_name=SOURCE_NAME,
            source_url=f"{GIS_QUERY_URL}?where=PARCELNO+LIKE+%27%25{digits}%27",
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            cache_path=str(snap_path),
        ),
    )
    rec.parcel_centroid = centroid
    # Stash the authoritative qPublic deep link (used by the qPublic enricher and
    # by the manual-retrieval to-do for permits / full history).
    mcpa_url = (a.get("MCPA_URL") or "").strip() or None
    if mcpa_url:
        rec.mcpa_url = mcpa_url  # dynamic attribute; read defensively downstream
    logger.info(
        "Monroe GIS resolved parcel %s: owner=%s, val_years=%s, sales=%d, centroid=%s",
        appraiser_parcel, owner_names, [v.year for v in valuation_history],
        len(sales_history), centroid,
    )
    return rec


def _query(params: dict, session: Optional[requests.Session]) -> Optional[dict]:
    sess = session or requests
    last = None
    for attempt in range(4):
        try:
            resp = sess.get(GIS_QUERY_URL, params=params, timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"})
            return resp.json()
        except Exception as exc:
            last = exc
            wait = 2 * (attempt + 1)
            logger.info("Monroe GIS query attempt %d failed (%s); retry in %ss",
                        attempt + 1, exc, wait)
            time.sleep(wait)
    logger.warning("Monroe GIS query failed after retries: %s", last)
    return None


def fetch_by_parcel(
    parcel_id: str,
    cache_root: Path,
    cache_key: str,
    session: Optional[requests.Session] = None,
) -> Optional[AppraiserRecord]:
    """Resolve a parcel by its appraiser parcel/folio number via suffix match."""
    digits = _digits(parcel_id)
    if len(digits) < 8:
        return None
    data = _query({
        "where": f"PARCELNO LIKE '%{digits}'",
        "outFields": "*", "returnGeometry": "false", "f": "json",
    }, session)
    if not data:
        return None
    feats = data.get("features", [])
    if not feats:
        logger.info("Monroe GIS: no parcel matched suffix %s", digits)
        return None
    return _attrs_to_record(feats[0]["attributes"], cache_root, cache_key)


def fetch_by_address(
    address: str,
    cache_root: Path,
    cache_key: str,
    session: Optional[requests.Session] = None,
) -> Optional[AppraiserRecord]:
    """
    Resolve a parcel by street address. Matches the street number + street-name
    token against the physical address columns (ADD1 / LOCATION).
    """
    if not address:
        return None
    # Extract street number and the first alphabetic street-name token.
    m_num = re.match(r"\s*(\d+)", address)
    street_num = m_num.group(1) if m_num else None
    # Street name tokens (drop the number, stop at the first comma).
    head = address.split(",")[0]
    tokens = [t for t in re.findall(r"[A-Za-z]+", head) if len(t) >= 3]
    name_tok = tokens[0].upper() if tokens else None
    if not name_tok:
        return None

    # ArcGIS SQL LIKE on the situs/location field.
    where = f"UPPER(LOCATION) LIKE '%{name_tok}%'"
    data = _query({
        "where": where, "outFields": "*", "returnGeometry": "false", "f": "json",
    }, session)
    if not data:
        return None
    feats = data.get("features", [])
    if not feats:
        logger.info("Monroe GIS: no parcel matched address token %s", name_tok)
        return None

    # Pick the feature whose LOCATION/ADD1 starts with the street number.
    chosen = None
    if street_num:
        for f in feats:
            a = f["attributes"]
            loc = (a.get("LOCATION") or "").upper().strip()
            add1 = (a.get("ADD1") or "").upper().strip()
            if loc.startswith(street_num + " ") or add1.startswith(street_num + " "):
                chosen = f
                break
    if chosen is None:
        # No exact street-number hit. Do NOT guess — an arbitrary neighbor is worse
        # than honestly returning nothing (the caller will flag manual retrieval).
        logger.info(
            "Monroe GIS: %d candidates for '%s' but none start with street number %s; "
            "not guessing.", len(feats), name_tok, street_num,
        )
        return None
    return _attrs_to_record(chosen["attributes"], cache_root, cache_key)
