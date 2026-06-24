"""
keys_mls.py — Florida Keys MLS lookup tool
Resolves a Keys MLS number to a fully normalized property record.

Usage:
    python keys_mls.py 619378
    python keys_mls.py 619378 --address "6501 Oceanview Ave, Marathon, FL 33050"

Entry point:
    resolve(mls_number, address_hint=None) -> dict

Environment variables:
    FLKEYS_RESO_TOKEN   — RESO Web API bearer token (Tier 1, optional)
    FLKEYS_RESO_URL     — RESO Web API base URL (Tier 1, optional)
    SEARCH_API_KEY      — Serper / Brave / Bing search API key (Tier 2)
    SEARCH_PROVIDER     — "serper" | "brave" | "bing" (default: serper)
    POLITE_DELAY        — seconds between HTTP requests (default: 2.5)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CACHE_ROOT = Path("./cache")
POLITE_DELAY = float(os.getenv("POLITE_DELAY", "2.5"))

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
SESSION_TIMEOUT = 20  # seconds

logging.basicConfig(
    format="%(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
log = logging.getLogger("keys_mls")

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

_session: requests.Session | None = None


def _http() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": BROWSER_UA})
    return _session


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_dir(mls: str) -> Path:
    d = CACHE_ROOT / mls
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(mls: str, key: str, ext: str = "txt") -> Path:
    safe = re.sub(r"[^\w\-]", "_", key)
    return _cache_dir(mls) / f"{safe}.{ext}"


def _cache_read(path: Path) -> str | None:
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    return None


def _cache_write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# robots.txt gating
# ---------------------------------------------------------------------------

_robots: dict[str, urllib.robotparser.RobotFileParser] = {}


def _robots_allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.set_url(f"{origin}/robots.txt")
            rp.read()
        except Exception:
            rp = urllib.robotparser.RobotFileParser()
        _robots[origin] = rp
    return _robots[origin].can_fetch(BROWSER_UA, url)


# ---------------------------------------------------------------------------
# Tolerant HTTP fetch with caching
# ---------------------------------------------------------------------------

def _fetch(url: str, mls: str, cache_key: str, *, raw: bool = False) -> str | None:
    path = _cache_path(mls, cache_key)
    cached = _cache_read(path)
    if cached is not None:
        log.debug("Cache hit: %s", cache_key)
        return cached

    if not _robots_allowed(url):
        log.warning("robots.txt disallows: %s", url)
        return None

    try:
        time.sleep(POLITE_DELAY)
        resp = _http().get(url, timeout=SESSION_TIMEOUT, allow_redirects=True)
        if resp.status_code == 403:
            log.warning(
                "403 Forbidden from %s (likely Cloudflare bot protection). "
                "This source requires a real browser session.",
                url,
            )
            return None
        resp.raise_for_status()
        text = resp.text if not raw else resp.content.decode("utf-8", errors="replace")
        _cache_write(path, text)
        log.info("Fetched %s (%d bytes)", url, len(text))
        return text
    except Exception as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Tier 1 — RESO Web API
# ---------------------------------------------------------------------------

def fetch_reso(mls_number: str) -> dict | None:
    token = os.getenv("FLKEYS_RESO_TOKEN", "")
    base_url = os.getenv("FLKEYS_RESO_URL", "")
    if not token or not base_url:
        log.info("RESO credentials not configured, skipping Tier 1")
        return None

    odata_filter = urllib.parse.quote(f"ListingId eq '{mls_number}'")
    url = f"{base_url.rstrip('/')}/Property?$filter={odata_filter}&$top=1"
    log.info("Querying RESO API: %s", url)

    cache_key = f"reso_{mls_number}"
    path = _cache_path(mls_number, cache_key, "json")
    cached = _cache_read(path)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    try:
        time.sleep(POLITE_DELAY)
        resp = _http().get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=SESSION_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        _cache_write(path, json.dumps(data, indent=2))
        records = data.get("value", [])
        if records:
            return records[0]
        log.warning("RESO API returned no results for %s", mls_number)
        return None
    except Exception as exc:
        log.warning("RESO API error: %s", exc)
        return None


def _normalize_reso(raw: dict, mls_number: str) -> dict:
    """Map a RESO standard property record to our output schema."""
    address_parts = {
        "street": raw.get("UnparsedAddress", ""),
        "city": raw.get("City", ""),
        "state": raw.get("StateOrProvince", "FL"),
        "zip": raw.get("PostalCode", ""),
    }
    address_parts["full"] = (
        f"{address_parts['street']}, {address_parts['city']}, "
        f"{address_parts['state']} {address_parts['zip']}"
    ).strip(", ")
    return {
        "address": address_parts,
        "geo": {"lat": raw.get("Latitude"), "lon": raw.get("Longitude")},
        "status": raw.get("StandardStatus", raw.get("MlsStatus")),
        "list_price": _int_or_none(raw.get("ListPrice")),
        "beds": raw.get("BedroomsTotal"),
        "baths": raw.get("BathroomsTotalInteger"),
        "living_area_sqft": _int_or_none(raw.get("LivingArea")),
        "lot_size_sqft": _int_or_none(raw.get("LotSizeSquareFeet")),
        "year_built": _int_or_none(raw.get("YearBuilt")),
        "property_type": raw.get("PropertyType", raw.get("PropertySubType")),
        "hoa_fee": _int_or_none(raw.get("AssociationFee")),
        "annual_taxes": _int_or_none(raw.get("TaxAnnualAmount")),
        "listing_office": raw.get("ListOfficeName", ""),
        "listing_agent": raw.get("ListAgentFullName", ""),
        "public_remarks": raw.get("PublicRemarks", ""),
        "photo_urls": raw.get("Media", []),
        "days_on_market": _int_or_none(raw.get("DaysOnMarket")),
    }


# ---------------------------------------------------------------------------
# Tier 2 — Web search
# ---------------------------------------------------------------------------

def _search_serper(query: str, api_key: str) -> list[dict]:
    url = "https://google.serper.dev/search"
    try:
        time.sleep(POLITE_DELAY)
        resp = _http().post(
            url,
            json={"q": query, "num": 10},
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            timeout=SESSION_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("organic", [])
    except Exception as exc:
        log.warning("Serper search failed: %s", exc)
        return []


def _search_brave(query: str, api_key: str) -> list[dict]:
    url = "https://api.search.brave.com/res/v1/web/search"
    try:
        time.sleep(POLITE_DELAY)
        resp = _http().get(
            url,
            params={"q": query, "count": 10},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            timeout=SESSION_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        return [
            {"title": r.get("title"), "link": r.get("url"), "snippet": r.get("description")}
            for r in results
        ]
    except Exception as exc:
        log.warning("Brave search failed: %s", exc)
        return []


def _search_bing(query: str, api_key: str) -> list[dict]:
    url = "https://api.bing.microsoft.com/v7.0/search"
    try:
        time.sleep(POLITE_DELAY)
        resp = _http().get(
            url,
            params={"q": query, "count": 10},
            headers={"Ocp-Apim-Subscription-Key": api_key},
            timeout=SESSION_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("webPages", {}).get("value", [])
        return [
            {"title": i.get("name"), "link": i.get("url"), "snippet": i.get("snippet")}
            for i in items
        ]
    except Exception as exc:
        log.warning("Bing search failed: %s", exc)
        return []


def search_listings(mls_number: str) -> list[dict]:
    """Run a web search and return raw result dicts (title, link, snippet)."""
    api_key = os.getenv("SEARCH_API_KEY", "")
    provider = os.getenv("SEARCH_PROVIDER", "serper").lower()
    query = f'"{mls_number}" Florida Keys Marathon "Monroe County" site:realestate OR site:homesnapshots OR site:movoto OR site:estately OR MLS'

    # Check cache
    cache_key = f"search_{mls_number}"
    path = _cache_path(mls_number, cache_key, "json")
    cached = _cache_read(path)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    if not api_key:
        log.warning(
            "No SEARCH_API_KEY set. Tier 2 web search disabled. "
            "Set SEARCH_API_KEY and SEARCH_PROVIDER (serper|brave|bing) to enable."
        )
        return []

    if provider == "brave":
        results = _search_brave(query, api_key)
    elif provider == "bing":
        results = _search_bing(query, api_key)
    else:
        results = _search_serper(query, api_key)

    if results:
        _cache_write(path, json.dumps(results, indent=2))
    return results


# ---------------------------------------------------------------------------
# Scrape-tolerant listing sites for Tier 2
# ---------------------------------------------------------------------------

# Domains we'll attempt to fetch from. Ordered by reliability.
TOLERANT_DOMAINS = [
    "movoto.com",
    "estately.com",
    "homesnap.com",
    "redfin.com",
    "coldwellbankerhomes.com",
    "marathonfloridakeysrealestate.com",
    "keysrealestate.com",
    "islandsothebysrealty.com",
]

BLOCKED_DOMAINS = {
    "zillow.com",
    "realtor.com",
    "remax.com",
    "trulia.com",
}


def _is_scrape_tolerant(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
    if any(host.endswith(b) for b in BLOCKED_DOMAINS):
        return False
    return True


def fetch_listing_pages(mls_number: str, search_results: list[dict]) -> list[dict]:
    """
    Fetch scrape-tolerant listing pages from search results.
    Returns list of {url, html, retrieved_at}.
    """
    fetched = []
    seen_urls: set[str] = set()

    for result in search_results:
        url = result.get("link", "")
        if not url or url in seen_urls:
            continue
        if not _is_scrape_tolerant(url):
            log.debug("Skipping blocked domain: %s", url)
            continue
        seen_urls.add(url)

        cache_key = f"page_{urllib.parse.urlparse(url).netloc}"
        html = _fetch(url, mls_number, cache_key)
        if html:
            fetched.append(
                {"url": url, "html": html, "retrieved_at": _now_iso()}
            )
        if len(fetched) >= 4:
            break

    return fetched


# ---------------------------------------------------------------------------
# JSON-LD parser
# ---------------------------------------------------------------------------

def extract_json_ld(html: str) -> list[dict]:
    """Return all parsed JSON-LD objects from an HTML page."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)
        except (json.JSONDecodeError, TypeError):
            pass
    return results


def _find_listing_in_json_ld(blocks: list[dict]) -> dict | None:
    """
    Look for a schema.org RealEstateListing, Product, or Residence object.
    """
    target_types = {
        "RealEstateListing", "Product", "Residence", "House",
        "SingleFamilyResidence", "Apartment", "Accommodation",
    }
    for block in blocks:
        raw_type = block.get("@type", "")
        types = raw_type if isinstance(raw_type, list) else [raw_type]
        if any(t in target_types for t in types):
            return block
    return None


def _parse_schema_org_listing(block: dict) -> dict:
    """Extract our fields from a schema.org listing block."""
    geo = block.get("geo", {})
    address = block.get("address", {})
    offers = block.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    street = (
        address.get("streetAddress", "")
        or block.get("name", "")
    )
    city = address.get("addressLocality", "")
    state = address.get("addressRegion", "FL")
    zipcode = address.get("postalCode", "")

    return {
        "address": {
            "full": f"{street}, {city}, {state} {zipcode}".strip(", "),
            "street": street,
            "city": city,
            "state": state,
            "zip": zipcode,
        },
        "geo": {
            "lat": _float_or_none(geo.get("latitude")),
            "lon": _float_or_none(geo.get("longitude")),
        },
        "list_price": _price_from_offer(offers),
        "status": offers.get("availability", ""),
        "beds": _int_or_none(
            block.get("numberOfRooms") or block.get("numberOfBedrooms")
        ),
        "baths": _int_or_none(block.get("numberOfBathroomsTotal")),
        "living_area_sqft": _sqft_from_block(block),
        "year_built": _int_or_none(block.get("yearBuilt")),
        "public_remarks": block.get("description", ""),
        "photo_urls": _photos_from_block(block),
    }


def _price_from_offer(offers: dict) -> int | None:
    price = offers.get("price") or offers.get("lowPrice")
    return _int_or_none(price)


def _sqft_from_block(block: dict) -> int | None:
    for key in ("floorSize", "floorSizeValue", "livingArea"):
        val = block.get(key)
        if isinstance(val, dict):
            val = val.get("value")
        if val is not None:
            return _int_or_none(val)
    return None


def _photos_from_block(block: dict) -> list[str]:
    imgs = block.get("image", [])
    if isinstance(imgs, str):
        return [imgs]
    if isinstance(imgs, dict):
        url = imgs.get("url", imgs.get("contentUrl", ""))
        return [url] if url else []
    result = []
    for img in imgs:
        if isinstance(img, str):
            result.append(img)
        elif isinstance(img, dict):
            url = img.get("url", img.get("contentUrl", ""))
            if url:
                result.append(url)
    return result


# ---------------------------------------------------------------------------
# Embedded-state JSON parser (Next.js / preloaded state)
# ---------------------------------------------------------------------------

def extract_embedded_state(html: str) -> dict | None:
    """
    Look for __NEXT_DATA__, window.__PRELOADED_STATE__, or similar.
    Returns the first parseable large JSON blob found.
    """
    patterns = [
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'window\.__NEXT_DATA__\s*=\s*(\{.*?\});\s*(?:window|</script>)',
        r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});',
        r'window\.initialState\s*=\s*(\{.*?\});',
        r'window\.__STATE__\s*=\s*(\{.*?\});',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except (json.JSONDecodeError, IndexError):
                pass
    return None


def _dig(obj: Any, *keys: str, default=None) -> Any:
    """Safely traverse nested dicts/lists with key sequence."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int):
            try:
                cur = cur[k]
            except IndexError:
                return default
        else:
            return default
        if cur is None:
            return default
    return cur


def extract_from_state(state: dict) -> dict:
    """
    Best-effort extraction from a Next.js / preloaded state blob.
    Keys vary by site; we look for common patterns.
    """
    listing: dict = {}

    # Try common state paths
    candidates = [
        _dig(state, "props", "pageProps", "listing"),
        _dig(state, "props", "pageProps", "property"),
        _dig(state, "listing"),
        _dig(state, "property"),
        _dig(state, "listingData"),
    ]
    for cand in candidates:
        if cand and isinstance(cand, dict):
            listing = cand
            break

    if not listing:
        return {}

    # Try to pull common fields regardless of exact key names
    price = (
        listing.get("listPrice")
        or listing.get("price")
        or listing.get("Price")
        or listing.get("list_price")
    )
    beds = (
        listing.get("bedrooms")
        or listing.get("beds")
        or listing.get("BedroomsTotal")
    )
    baths = (
        listing.get("bathrooms")
        or listing.get("baths")
        or listing.get("BathroomsTotalInteger")
    )
    sqft = (
        listing.get("livingArea")
        or listing.get("squareFeet")
        or listing.get("LivingArea")
    )
    addr = listing.get("address", {})
    if isinstance(addr, str):
        full_addr = addr
        street = addr
        city = state_ = zipcode = ""
    else:
        street = addr.get("streetAddress", addr.get("street", ""))
        city = addr.get("city", addr.get("addressLocality", ""))
        state_ = addr.get("state", addr.get("addressRegion", "FL"))
        zipcode = addr.get("zip", addr.get("postalCode", ""))
        full_addr = f"{street}, {city}, {state_} {zipcode}".strip(", ")

    geo_raw = listing.get("geo", listing.get("location", {}))
    lat = _float_or_none(
        geo_raw.get("lat") or geo_raw.get("latitude") if isinstance(geo_raw, dict) else None
    )
    lon = _float_or_none(
        geo_raw.get("lng") or geo_raw.get("longitude") if isinstance(geo_raw, dict) else None
    )

    result: dict = {}
    if full_addr:
        result["address"] = {
            "full": full_addr, "street": street,
            "city": city, "state": state_, "zip": zipcode
        }
    if lat or lon:
        result["geo"] = {"lat": lat, "lon": lon}
    if price:
        result["list_price"] = _int_or_none(price)
    if beds:
        result["beds"] = _int_or_none(beds)
    if baths:
        result["baths"] = _int_or_none(baths)
    if sqft:
        result["living_area_sqft"] = _int_or_none(sqft)
    return result


# ---------------------------------------------------------------------------
# DOM-level visible-text fallback
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(r"\$\s?([\d,]+)", re.IGNORECASE)
_BEDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bed(?:room)?s?|br\b)", re.IGNORECASE)
_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bath(?:room)?s?|ba\b)", re.IGNORECASE)
_SQFT_RE = re.compile(r"([\d,]+)\s*(?:sq\.?\s?ft\.?|square\s+feet)", re.IGNORECASE)
_STATUS_RE = re.compile(
    r"\b(Active|Pending|Sold|Closed|Under Contract|Back on Market|Contingent)\b",
    re.IGNORECASE,
)
_OFFICE_RE = re.compile(r"(?:listed by|listing office|brokerage)[\s:]+([^\n\|<]{5,60})", re.IGNORECASE)
_AGENT_RE = re.compile(r"(?:listing agent|listed by agent|agent)[\s:]+([^\n\|<]{5,60})", re.IGNORECASE)
_YEAR_BUILT_RE = re.compile(r"(?:year built|built in|built:)\s*(\d{4})", re.IGNORECASE)
_LOT_RE = re.compile(r"lot\s*(?:size)?[\s:]*([0-9,]+)\s*(?:sq\.?\s?ft\.?|sf\b)", re.IGNORECASE)


def parse_dom(html: str) -> dict:
    """Last-resort: extract key fields from visible page text."""
    soup = BeautifulSoup(html, "lxml")
    # Remove nav, footer, script, style noise
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)

    result: dict = {}

    m = _PRICE_RE.search(text)
    if m:
        result["list_price"] = _int_or_none(m.group(1).replace(",", ""))

    m = _BEDS_RE.search(text)
    if m:
        result["beds"] = _float_or_none(m.group(1))

    m = _BATHS_RE.search(text)
    if m:
        result["baths"] = _float_or_none(m.group(1))

    m = _SQFT_RE.search(text)
    if m:
        result["living_area_sqft"] = _int_or_none(m.group(1).replace(",", ""))

    m = _STATUS_RE.search(text)
    if m:
        result["status"] = m.group(1).title()

    m = _YEAR_BUILT_RE.search(text)
    if m:
        result["year_built"] = int(m.group(1))

    m = _OFFICE_RE.search(text)
    if m:
        result["listing_office"] = m.group(1).strip()

    m = _AGENT_RE.search(text)
    if m:
        result["listing_agent"] = m.group(1).strip()

    m = _LOT_RE.search(text)
    if m:
        result["lot_size_sqft"] = _int_or_none(m.group(1).replace(",", ""))

    # Grab meta description for remarks fallback
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        result.setdefault("public_remarks", meta_desc["content"])

    return result


def parse_page(page: dict) -> tuple[dict, str]:
    """
    Parse a fetched page using JSON-LD → embedded state → DOM fallback.
    Returns (fields_dict, method_used).
    """
    html = page["html"]

    # 1. JSON-LD
    blocks = extract_json_ld(html)
    listing_block = _find_listing_in_json_ld(blocks)
    if listing_block:
        return _parse_schema_org_listing(listing_block), "json-ld"

    # 2. Embedded state
    state = extract_embedded_state(html)
    if state:
        fields = extract_from_state(state)
        if fields:
            return fields, "embedded-state"

    # 3. DOM
    return parse_dom(html), "dom"


# ---------------------------------------------------------------------------
# Tier 3 — Snippet fallback
# ---------------------------------------------------------------------------

def parse_snippets(search_results: list[dict]) -> dict:
    """Extract core fields from search result snippets alone."""
    combined = " ".join(
        (r.get("title", "") + " " + r.get("snippet", ""))
        for r in search_results
    )
    result: dict = {}

    m = _PRICE_RE.search(combined)
    if m:
        result["list_price"] = _int_or_none(m.group(1).replace(",", ""))
    m = _BEDS_RE.search(combined)
    if m:
        result["beds"] = _float_or_none(m.group(1))
    m = _BATHS_RE.search(combined)
    if m:
        result["baths"] = _float_or_none(m.group(1))
    m = _SQFT_RE.search(combined)
    if m:
        result["living_area_sqft"] = _int_or_none(m.group(1).replace(",", ""))
    m = _STATUS_RE.search(combined)
    if m:
        result["status"] = m.group(1).title()
    m = _OFFICE_RE.search(combined)
    if m:
        result["listing_office"] = m.group(1).strip()

    # Try to extract remarks from the richest snippet
    best = max(
        search_results, key=lambda r: len(r.get("snippet", "")), default={}
    )
    if best.get("snippet"):
        result.setdefault("public_remarks", best["snippet"])

    return result


# ---------------------------------------------------------------------------
# Monroe County Property Appraiser
# ---------------------------------------------------------------------------

MCPA_SEARCH_URL = "https://www.mcpafl.org/PropSearch.aspx"
MCPA_DETAIL_URL = "https://www.mcpafl.org/PropDetail.aspx"
# MCPA is behind Cloudflare which blocks automated requests.
# We attempt the fetch anyway (some environments succeed) but degrade gracefully on 403.
MCPA_CLOUDFLARE_NOTE = (
    "MCPA (mcpafl.org) is Cloudflare-protected and may return 403. "
    "To get parcel data, look up the address manually at https://www.mcpafl.org/PropSearch.aspx "
    "or provide a MCPA_PARCEL_ID environment variable."
)


def fetch_mcpa(mls_number: str, address: str | None = None) -> dict | None:
    """
    Query Monroe County Property Appraiser for parcel details.

    Note: mcpafl.org is Cloudflare-protected.  We attempt the fetch anyway
    since some network environments succeed, but degrade gracefully on 403/CAPTCHA.
    Callers can bypass by setting MCPA_PARCEL_ID env var with a known parcel ID.
    """
    # Allow caller to short-circuit with a known parcel ID
    manual_parcel = os.getenv("MCPA_PARCEL_ID", "")
    if manual_parcel:
        return {"parcel_id": manual_parcel, "_source": "env"}

    if not address:
        log.info("No address provided; skipping MCPA lookup")
        return None

    # Parse street number and name from address
    addr_parts = address.strip().split(",")
    street_raw = addr_parts[0].strip() if addr_parts else ""
    m = re.match(r"(\d+)\s+(.*)", street_raw)
    if not m:
        log.warning("Cannot parse street number from: %s", street_raw)
        return None
    street_num = m.group(1)
    street_name = m.group(2).strip()

    cache_key = f"mcpa_{mls_number}"
    path = _cache_path(mls_number, cache_key, "json")
    cached = _cache_read(path)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    # MCPA exposes a search form; we simulate a GET with query params.
    # The site's search also works via direct URL with StreetNumber and StreetName.
    search_params = {
        "StreetNumber": street_num,
        "StreetName": street_name.upper(),
        "StreetType": "",
        "City": "",
        "Zip": "",
        "Subdivision": "",
        "SearchType": "Address",
    }
    search_url = f"{MCPA_SEARCH_URL}?" + urllib.parse.urlencode(search_params)
    html = _fetch(search_url, mls_number, "mcpa_search")
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")

    # MCPA search results table — look for a link to PropDetail.aspx
    detail_link = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "PropDetail" in href or "RE=" in href or "Parcel" in href:
            detail_link = urllib.parse.urljoin(MCPA_SEARCH_URL, href)
            break

    # Fallback: look for any table row with our street number
    if not detail_link:
        for row in soup.find_all("tr"):
            row_text = row.get_text()
            if street_num in row_text:
                a_tag = row.find("a", href=True)
                if a_tag:
                    detail_link = urllib.parse.urljoin(MCPA_SEARCH_URL, a_tag["href"])
                    break

    if not detail_link:
        log.warning("MCPA: could not find detail link for %s", street_raw)
        # Try to extract any parcel info directly from the search page
        return _parse_mcpa_page(html, mls_number)

    detail_html = _fetch(detail_link, mls_number, "mcpa_detail")
    if not detail_html:
        return None

    result = _parse_mcpa_page(detail_html, mls_number)
    if result:
        _cache_write(path, json.dumps(result, indent=2))
    return result


def _parse_mcpa_page(html: str, mls_number: str) -> dict | None:
    """Extract parcel data from MCPA HTML."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    result: dict = {}

    # Parcel / RE number
    for pat in [
        r"(?:Parcel\s*(?:ID|Number)|RE\s*Number|Alternate Key)[\s:#]*([0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{6})",
        r"(?:Parcel\s*(?:ID|Number)|RE\s*Number|Alternate Key)[\s:#]*(\d{7,})",
        r"\bRE\s*#\s*([0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{6})",
        r"([0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{6})",  # bare pattern
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            result["parcel_id"] = m.group(1).strip()
            break

    # Values
    for label, key in [
        (r"Just\s*(?:Market\s*)?Value", "just_value"),
        (r"Assessed\s*Value", "assessed_value"),
        (r"Taxable\s*Value", "taxable_value"),
    ]:
        m = re.search(rf"{label}[\s:$]*([0-9,]+)", text, re.IGNORECASE)
        if m:
            result[key] = _int_or_none(m.group(1).replace(",", ""))

    # Year built
    m = re.search(r"(?:Year\s*Built|Effective\s*Year)[\s:]*(\d{4})", text, re.IGNORECASE)
    if m:
        result["year_built"] = int(m.group(1))

    # Living area from appraiser (sq ft)
    m = re.search(r"(?:Living\s*Area|Heated\s*Area|Building\s*Area)[\s:]*([0-9,]+)", text, re.IGNORECASE)
    if m:
        result["living_area_sqft"] = _int_or_none(m.group(1).replace(",", ""))

    # Ownership
    m = re.search(r"(?:Owner|Taxpayer)[\s:]+([A-Z][^\n\r<]{3,60})", text, re.IGNORECASE)
    if m:
        result["owner"] = m.group(1).strip()

    # Sales history — look for last sale
    sale_date_m = re.search(
        r"(?:Sale Date|Last\s*Sale|Date\s*of\s*Sale)[\s:]*([\d/\-]+)", text, re.IGNORECASE
    )
    sale_price_m = re.search(
        r"(?:Sale Price|Last\s*Sale\s*Price|Sale\s*Amount)[\s:$]*([0-9,]+)", text, re.IGNORECASE
    )
    if sale_date_m:
        result["last_sold_date"] = sale_date_m.group(1).strip()
    if sale_price_m:
        result["last_sold_price"] = _int_or_none(sale_price_m.group(1).replace(",", ""))

    # Annual taxes
    m = re.search(r"(?:Total\s*Tax|Annual\s*Tax|Ad\s*Valorem)[\s:$]*([0-9,]+)", text, re.IGNORECASE)
    if m:
        result["annual_taxes"] = _int_or_none(m.group(1).replace(",", ""))

    if not result:
        return None
    return result


# ---------------------------------------------------------------------------
# FEMA Flood Zone — National Flood Hazard Layer ArcGIS REST API
# ---------------------------------------------------------------------------

FEMA_NFHL_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)
GEOCODE_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

# Monroe County initial FIRM date (used to flag pre-FIRM construction).
# Monroe County adopted its first FIRM in November 1970.
MONROE_INITIAL_FIRM_DATE = 1970

# The NFHL ArcGIS service only accepts point queries when:
# 1. The geometry is a compact JSON string (no spaces) with spatialReference included.
# 2. A where=1=1 clause is present.
# 3. outFields=* is used (comma-separated specific fields cause 400 errors).
# Spatial reference must be omitted from params since server uses its own CRS.
def _fema_geometry_json(lon: float, lat: float) -> str:
    return f'{{"x":{lon},"y":{lat},"spatialReference":{{"wkid":4326}}}}'


def geocode_address(address: str, mls_number: str) -> tuple[float, float] | None:
    """Return (lat, lon) using the US Census geocoder (no API key needed)."""
    cache_key = f"geocode_{mls_number}"
    path = _cache_path(mls_number, cache_key, "json")
    cached = _cache_read(path)
    if cached:
        try:
            d = json.loads(cached)
            return d["lat"], d["lon"]
        except (json.JSONDecodeError, KeyError):
            pass

    params = {
        "address": address,
        "benchmark": "2020",
        "format": "json",
    }
    try:
        time.sleep(POLITE_DELAY)
        resp = _http().get(GEOCODE_URL, params=params, timeout=SESSION_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        matches = data.get("result", {}).get("addressMatches", [])
        if matches:
            coords = matches[0].get("coordinates", {})
            lat = float(coords.get("y", 0))
            lon = float(coords.get("x", 0))
            _cache_write(path, json.dumps({"lat": lat, "lon": lon}))
            return lat, lon
    except Exception as exc:
        log.warning("Geocoding failed for %s: %s", address, exc)
    return None


def fetch_flood_zone(mls_number: str, lat: float, lon: float, year_built: int | None = None) -> dict | None:
    """
    Query FEMA NFHL ArcGIS REST API for flood zone data at (lat, lon).

    The NFHL layer requires:
    - geometry as compact JSON with spatialReference (no spaces in the string)
    - where=1=1 alongside the spatial filter
    - outFields=* (comma-separated specific fields return 400)
    """
    cache_key = f"fema_{mls_number}"
    path = _cache_path(mls_number, cache_key, "json")
    cached = _cache_read(path)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    params = {
        "where": "1=1",
        "geometry": _fema_geometry_json(lon, lat),
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        time.sleep(POLITE_DELAY)
        resp = _http().get(FEMA_NFHL_URL, params=params, timeout=SESSION_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            log.warning("FEMA NFHL API error: %s", data["error"].get("message"))
            return None

        features = data.get("features", [])
        if not features:
            log.warning("FEMA NFHL: no flood zone data at %.6f, %.6f", lat, lon)
            return None

        attrs = features[0].get("attributes", {})
        zone = attrs.get("FLD_ZONE", "")
        subtype = attrs.get("ZONE_SUBTY") or ""
        # STATIC_BFE holds the BFE for AE zones; BFE_REVERT is for A/AR reversion zones
        bfe_raw = attrs.get("STATIC_BFE")
        if bfe_raw is None or bfe_raw == -9999.0:
            bfe_raw = attrs.get("BFE_REVERT")
        if bfe_raw == -9999.0:
            bfe_raw = None

        # FIRM panel ID from DFIRM_ID (6-char) + panel suffix
        dfirm = attrs.get("DFIRM_ID", "")
        fld_ar_id = attrs.get("FLD_AR_ID", "")
        firm_panel = fld_ar_id or dfirm

        full_zone = f"{zone} {subtype}".strip() if subtype else zone
        pre_firm = bool(year_built and year_built < MONROE_INITIAL_FIRM_DATE)

        result = {
            "zone": full_zone,
            "base_flood_elevation": _float_or_none(bfe_raw),
            "firm_panel": firm_panel,
            "pre_firm": pre_firm,
            "sfha": attrs.get("SFHA_TF") == "T",
            "v_datum": attrs.get("V_DATUM"),
        }
        _cache_write(path, json.dumps(result, indent=2))
        return result
    except Exception as exc:
        log.warning("FEMA NFHL lookup failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

_NUMERIC_FIELDS = {
    "list_price", "beds", "baths", "living_area_sqft", "lot_size_sqft",
    "year_built", "hoa_fee", "annual_taxes", "days_on_market",
}


def reconcile(sources: list[tuple[str, dict]]) -> tuple[dict, list[dict]]:
    """
    Merge fields from multiple (source_name, fields) tuples.
    Returns (merged_record, conflicts_list).
    Priority: reso > mcpa (physical facts) > most-recently-added listing source.
    """
    merged: dict = {}
    all_values: dict[str, dict[str, Any]] = {}  # field -> {source: value}

    priority_order = ["reso", "mcpa", "json-ld", "embedded-state", "dom", "snippet"]

    for source_name, fields in sources:
        for field, value in fields.items():
            if value is None or value == "" or value == []:
                continue
            all_values.setdefault(field, {})[source_name] = value

    for field, by_source in all_values.items():
        if not by_source:
            continue
        # Pick by priority
        chosen = None
        for prio in priority_order:
            if prio in by_source:
                chosen = by_source[prio]
                break
        if chosen is None:
            # Take the first available
            chosen = next(iter(by_source.values()))
        merged[field] = chosen

    # Detect conflicts
    conflicts = []
    for field, by_source in all_values.items():
        if len(by_source) < 2:
            continue
        values_list = list(by_source.values())
        # For numeric fields compare with 5% tolerance
        if field in _NUMERIC_FIELDS:
            nums = [v for v in values_list if isinstance(v, (int, float))]
            if nums and (max(nums) - min(nums)) / max(max(nums), 1) > 0.05:
                conflicts.append({"field": field, "values_by_source": by_source})
        else:
            # String fields: conflict if not all equal (case-insensitive for strings)
            strs = [str(v).lower().strip() for v in values_list if v]
            if len(set(strs)) > 1:
                conflicts.append({"field": field, "values_by_source": by_source})

    return merged, conflicts


# ---------------------------------------------------------------------------
# Output schema builder
# ---------------------------------------------------------------------------

EMPTY_RECORD: dict = {
    "mls_number": "",
    "address": {"full": "", "street": "", "city": "", "state": "FL", "zip": ""},
    "geo": {"lat": None, "lon": None},
    "status": None,
    "list_price": None,
    "price_history": [],
    "days_on_market": None,
    "beds": None,
    "baths": None,
    "living_area_sqft": None,
    "lot_size_sqft": None,
    "year_built": None,
    "property_type": None,
    "hoa_fee": None,
    "annual_taxes": None,
    "listing_office": None,
    "listing_agent": None,
    "public_remarks": None,
    "photo_urls": [],
    "parcel": {
        "parcel_id": None,
        "just_value": None,
        "assessed_value": None,
        "taxable_value": None,
        "last_sold_date": None,
        "last_sold_price": None,
    },
    "flood": {
        "zone": None,
        "base_flood_elevation": None,
        "firm_panel": None,
        "pre_firm": False,
        "sfha": None,
        "v_datum": None,
    },
    "sources": [],
    "conflicts": [],
}


def _build_record(
    mls_number: str,
    merged: dict,
    mcpa_data: dict | None,
    flood_data: dict | None,
    sources: list[dict],
    conflicts: list[dict],
) -> dict:
    import copy
    rec = copy.deepcopy(EMPTY_RECORD)
    rec["mls_number"] = mls_number

    for field in [
        "address", "geo", "status", "list_price", "price_history",
        "days_on_market", "beds", "baths", "living_area_sqft", "lot_size_sqft",
        "year_built", "property_type", "hoa_fee", "annual_taxes",
        "listing_office", "listing_agent", "public_remarks", "photo_urls",
    ]:
        if field in merged:
            rec[field] = merged[field]

    if mcpa_data:
        for parcel_field in ["parcel_id", "just_value", "assessed_value",
                              "taxable_value", "last_sold_date", "last_sold_price"]:
            if parcel_field in mcpa_data:
                rec["parcel"][parcel_field] = mcpa_data[parcel_field]
        # MCPA may have authoritative living area and year built
        for passthrough in ["living_area_sqft", "year_built", "annual_taxes"]:
            if passthrough in mcpa_data and rec[passthrough] is None:
                rec[passthrough] = mcpa_data[passthrough]

    if flood_data:
        rec["flood"] = flood_data

    rec["sources"] = sources
    rec["conflicts"] = conflicts
    return rec


# ---------------------------------------------------------------------------
# Plain-text summary
# ---------------------------------------------------------------------------

def print_summary(rec: dict) -> None:
    addr = rec["address"].get("full", "Unknown address")
    price = f"${rec['list_price']:,}" if rec.get("list_price") else "Price unknown"
    status = rec.get("status") or "Status unknown"
    beds = rec.get("beds")
    baths = rec.get("baths")
    sqft = rec.get("living_area_sqft")
    yr = rec.get("year_built")
    parcel = rec.get("parcel", {})
    flood = rec.get("flood", {})

    bed_bath = f"{beds}bd/{baths}ba" if beds and baths else ""
    sqft_str = f"{sqft:,} sqft" if sqft else ""
    yr_str = f"built {yr}" if yr else ""
    details = ", ".join(filter(None, [bed_bath, sqft_str, yr_str]))

    print("\n" + "=" * 60)
    print(f"MLS {rec['mls_number']}  |  {status}")
    print(f"{addr}")
    print(f"{price}" + (f"  ({details})" if details else ""))

    if parcel.get("parcel_id"):
        pv = parcel.get("just_value")
        print(f"Parcel: {parcel['parcel_id']}" + (f"  Just value: ${pv:,}" if pv else ""))
    if flood.get("zone"):
        bfe = flood.get("base_flood_elevation")
        bfe_str = f"  BFE {bfe}ft" if bfe else ""
        pre = "  [PRE-FIRM]" if flood.get("pre_firm") else ""
        print(f"Flood zone: {flood['zone']}{bfe_str}{pre}")

    sources = rec.get("sources", [])
    if sources:
        print(f"Sources: {', '.join(s['source_name'] for s in sources)}")

    conflicts = rec.get("conflicts", [])
    if conflicts:
        print(f"Conflicts detected ({len(conflicts)} field(s)):")
        for c in conflicts:
            print(f"  {c['field']}: {c['values_by_source']}")

    if rec.get("public_remarks"):
        print(f"\n{rec['public_remarks'][:300]}" + ("..." if len(rec.get("public_remarks", "")) > 300 else ""))
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _int_or_none(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(float(str(val).replace(",", "").replace("$", "").strip()))
    except (ValueError, TypeError):
        return None


def _float_or_none(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def resolve(mls_number: str, address_hint: str | None = None) -> dict:
    """
    Resolve an MLS number to a full property record.

    Args:
        mls_number: Florida Keys MLS listing number (string).
        address_hint: Optional street address to seed public-records lookups.

    Returns:
        Normalized property record dict matching the output schema.
    """
    log.info("Resolving MLS %s", mls_number)
    raw_sources: list[tuple[str, dict]] = []  # (source_name, fields)
    source_meta: list[dict] = []

    # ------------------------------------------------------------------
    # Tier 1 — RESO Web API
    # ------------------------------------------------------------------
    reso_raw = fetch_reso(mls_number)
    if reso_raw:
        fields = _normalize_reso(reso_raw, mls_number)
        raw_sources.append(("reso", fields))
        source_meta.append({
            "source_name": "RESO Web API",
            "url": os.getenv("FLKEYS_RESO_URL", ""),
            "fields_contributed": list(fields.keys()),
            "retrieved_at": _now_iso(),
        })
        # Use address from RESO if none provided
        if not address_hint and fields.get("address", {}).get("full"):
            address_hint = fields["address"]["full"]
        log.info("Tier 1 RESO: %d fields", len(fields))
    else:
        log.info("Tier 1 RESO: skipped or unavailable")

    # ------------------------------------------------------------------
    # Tier 2 — Web search + page fetch
    # ------------------------------------------------------------------
    search_results = search_listings(mls_number)

    if search_results:
        log.info("Tier 2: %d search results", len(search_results))

        # Try to extract address hint from snippets if still missing
        if not address_hint:
            for r in search_results:
                snippet = r.get("snippet", "") + " " + r.get("title", "")
                addr_m = re.search(
                    r"\d+\s+\w[\w\s]+(?:Ave|Blvd|Dr|St|Ln|Rd|Way|Ct|Ter)[,.]?\s+\w[\w\s]+,\s*FL",
                    snippet,
                    re.IGNORECASE,
                )
                if addr_m:
                    address_hint = addr_m.group(0)
                    log.info("Address extracted from snippet: %s", address_hint)
                    break

        pages = fetch_listing_pages(mls_number, search_results)
        for page in pages:
            fields, method = parse_page(page)
            if fields:
                source_name = f"{method}:{urllib.parse.urlparse(page['url']).netloc}"
                raw_sources.append((method, fields))
                source_meta.append({
                    "source_name": source_name,
                    "url": page["url"],
                    "fields_contributed": list(fields.keys()),
                    "retrieved_at": page["retrieved_at"],
                })
                log.info("Tier 2 %s (%s): %d fields", page["url"], method, len(fields))

                # Grab address from first successful page parse if still missing
                if not address_hint and fields.get("address", {}).get("full"):
                    address_hint = fields["address"]["full"]

        # Tier 3 snippet fallback if no pages or no fields
        if not any(fields for _, fields in raw_sources if _ != "reso"):
            log.info("Tier 3: falling back to snippet extraction")
            snippet_fields = parse_snippets(search_results)
            if snippet_fields:
                raw_sources.append(("snippet", snippet_fields))
                source_meta.append({
                    "source_name": "search-snippets",
                    "url": "",
                    "fields_contributed": list(snippet_fields.keys()),
                    "retrieved_at": _now_iso(),
                })
    else:
        log.info("Tier 2: no search results (SEARCH_API_KEY not set or no results)")

    # ------------------------------------------------------------------
    # Reconcile listing sources
    # ------------------------------------------------------------------
    merged, conflicts = reconcile(raw_sources)

    # Resolve address for downstream lookups
    full_address = (
        address_hint
        or merged.get("address", {}).get("full")
        or ""
    )

    # ------------------------------------------------------------------
    # Seed address from hint when no listing source populated it
    # ------------------------------------------------------------------
    if full_address and not merged.get("address", {}).get("full"):
        parts = [p.strip() for p in full_address.split(",")]
        street = parts[0] if parts else full_address
        city = parts[1] if len(parts) > 1 else ""
        state_zip = parts[2].strip() if len(parts) > 2 else "FL"
        state_zip_parts = state_zip.split()
        state = state_zip_parts[0] if state_zip_parts else "FL"
        zipcode = state_zip_parts[1] if len(state_zip_parts) > 1 else ""
        merged["address"] = {
            "full": full_address,
            "street": street,
            "city": city,
            "state": state,
            "zip": zipcode,
        }

    # ------------------------------------------------------------------
    # Geocode (needed for FEMA; MCPA uses street address)
    # ------------------------------------------------------------------
    lat = merged.get("geo", {}).get("lat")
    lon = merged.get("geo", {}).get("lon")

    if full_address and (not lat or not lon):
        coords = geocode_address(full_address, mls_number)
        if coords:
            lat, lon = coords
            if "geo" not in merged:
                merged["geo"] = {}
            merged["geo"]["lat"] = lat
            merged["geo"]["lon"] = lon
            log.info("Geocoded to %.6f, %.6f", lat, lon)

    # ------------------------------------------------------------------
    # Monroe County Property Appraiser
    # ------------------------------------------------------------------
    mcpa_data = fetch_mcpa(mls_number, full_address) if full_address else None
    if mcpa_data:
        source_meta.append({
            "source_name": "Monroe County Property Appraiser",
            "url": MCPA_SEARCH_URL,
            "fields_contributed": list(mcpa_data.keys()),
            "retrieved_at": _now_iso(),
        })
        log.info("MCPA: %d fields", len(mcpa_data))
        # Add MCPA fields as a source for reconciliation
        raw_sources.append(("mcpa", mcpa_data))
        merged, conflicts = reconcile(raw_sources)

    # ------------------------------------------------------------------
    # FEMA Flood Zone
    # ------------------------------------------------------------------
    flood_data = None
    if lat and lon:
        year_built = merged.get("year_built") or (mcpa_data or {}).get("year_built")
        flood_data = fetch_flood_zone(mls_number, lat, lon, year_built)
        if flood_data:
            source_meta.append({
                "source_name": "FEMA NFHL ArcGIS",
                "url": FEMA_NFHL_URL,
                "fields_contributed": list(flood_data.keys()),
                "retrieved_at": _now_iso(),
            })
            log.info("FEMA: zone=%s", flood_data.get("zone"))
    else:
        log.warning("No coordinates available; skipping FEMA flood lookup")

    # ------------------------------------------------------------------
    # Build final record
    # ------------------------------------------------------------------
    record = _build_record(
        mls_number=mls_number,
        merged=merged,
        mcpa_data=mcpa_data,
        flood_data=flood_data,
        sources=source_meta,
        conflicts=conflicts,
    )

    return record


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Look up a Florida Keys MLS listing and return a normalized record."
    )
    parser.add_argument("mls_number", help="Keys MLS listing number (e.g. 619378)")
    parser.add_argument(
        "--address",
        default=None,
        help="Known street address to seed public-records lookups",
    )
    parser.add_argument(
        "--json-only", action="store_true", help="Print JSON record only (no summary)"
    )
    args = parser.parse_args()

    record = resolve(args.mls_number, address_hint=args.address)

    if not args.json_only:
        print_summary(record)

    print(json.dumps(record, indent=2, default=str))


if __name__ == "__main__":
    main()
