"""
FEMA NFHL flood zone adapter.

Uses the ArcGIS REST API directly (requests, not Playwright).
Key quirks of the NFHL layer 28:
  - geometry must be compact JSON (no spaces) with spatialReference included
  - where=1=1 must accompany the spatial filter
  - outFields=* required; comma-separated specific fields return HTTP 400
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from ..schemas import FloodRecord, Provenance

log = logging.getLogger("adapter.flood")

NFHL_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)
GEOCODE_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
MONROE_INITIAL_FIRM_YEAR = 1970


def geocode(address: str, cache_root: Path) -> Optional[tuple[float, float]]:
    """Geocode address via US Census (no key needed). Returns (lat, lon) or None."""
    cache_key = f"geocode_{address}"
    safe = cache_key.replace(" ", "_")[:80]
    cache_dir = cache_root / "geocode"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{safe}.json"

    if cache_file.exists():
        try:
            d = json.loads(cache_file.read_text())
            return d["lat"], d["lon"]
        except Exception:
            pass

    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"address": address, "benchmark": "2020", "format": "json"},
            timeout=20,
        )
        resp.raise_for_status()
        matches = resp.json().get("result", {}).get("addressMatches", [])
        if matches:
            c = matches[0]["coordinates"]
            lat, lon = float(c["y"]), float(c["x"])
            cache_file.write_text(json.dumps({"lat": lat, "lon": lon}))
            return lat, lon
    except Exception as e:
        log.warning("Geocode failed for %s: %s", address, e)
    return None


def fetch(
    lat: float,
    lon: float,
    year_built: Optional[int],
    cache_root: Path,
    cache_key: str = "",
) -> Optional[FloodRecord]:
    """Query FEMA NFHL for flood zone at (lat, lon)."""
    key = cache_key or f"fema_{lat:.5f}_{lon:.5f}"
    safe = key.replace("-", "m").replace(".", "d")[:60]
    cache_dir = cache_root / "fema"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{safe}.json"

    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            return FloodRecord(**data)
        except Exception:
            pass

    geom = f'{{"x":{lon},"y":{lat},"spatialReference":{{"wkid":4326}}}}'
    params = {
        "where": "1=1",
        "geometry": geom,
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }

    try:
        resp = requests.get(NFHL_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            log.warning("FEMA error: %s", data["error"])
            return None
        features = data.get("features", [])
        if not features:
            log.warning("FEMA: no data at %.5f, %.5f", lat, lon)
            return None

        a = features[0]["attributes"]
        zone = a.get("FLD_ZONE", "")
        subtype = a.get("ZONE_SUBTY") or ""
        bfe = a.get("STATIC_BFE")
        if bfe is None or bfe == -9999.0:
            bfe = a.get("BFE_REVERT")
        if bfe == -9999.0:
            bfe = None
        firm_panel = a.get("FLD_AR_ID") or a.get("DFIRM_ID", "")
        full_zone = f"{zone} {subtype}".strip() if subtype else zone
        pre_firm = bool(year_built and year_built < MONROE_INITIAL_FIRM_YEAR)

        rec = FloodRecord(
            zone=full_zone,
            base_flood_elevation=float(bfe) if bfe is not None else None,
            firm_panel=firm_panel,
            pre_firm=pre_firm,
            sfha=a.get("SFHA_TF") == "T",
            v_datum=a.get("V_DATUM"),
            lat=lat,
            lon=lon,
            provenance=Provenance(
                source_name="FEMA NFHL ArcGIS",
                source_url=NFHL_URL,
                retrieved_at=datetime.now(timezone.utc).isoformat(),
                cache_path=str(cache_file),
            ),
        )
        # Cache as dict (provenance serialized manually)
        import dataclasses
        cache_file.write_text(json.dumps(dataclasses.asdict(rec), default=str))
        return rec

    except Exception as e:
        log.warning("FEMA lookup failed: %s", e)
        return None
