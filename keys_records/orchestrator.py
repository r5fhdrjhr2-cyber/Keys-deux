"""
Orchestrator — runs all adapters for a given parcel and assembles ParcelRecord.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .polite_client import PoliteClient, ManualRetrievalRequired
from .schemas import (
    ManualRetrievalRequired as ManualRetrievalRequiredSchema,
    ParcelRecord,
)
from .jurisdiction_router import route

logger = logging.getLogger("orchestrator")

_DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"
_RUN_STATE_FILE = "run_state.json"


def _load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path) if config_path else _DEFAULT_CONFIG
    with open(path) as fh:
        return yaml.safe_load(fh)


def _load_run_state(cache_root: Path) -> dict:
    path = cache_root / _RUN_STATE_FILE
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"parcel_count": 0, "parcels": []}


def _save_run_state(cache_root: Path, state: dict):
    path = cache_root / _RUN_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _geocode_address(address: str, cache_root: Path) -> tuple:
    """Geocode via US Census geocoder. Returns (lat, lon) or (None, None)."""
    from .adapters.flood import geocode
    result = geocode(address, cache_root)
    if result:
        return result
    return None, None


def run(
    mls_number: Optional[str] = None,
    parcel_id: Optional[str] = None,
    address: Optional[str] = None,
    config_path: Optional[str] = None,
) -> ParcelRecord:
    """
    Run the full public-records pipeline for a Florida Keys parcel.

    At least one of mls_number, parcel_id, or address must be provided.
    Returns a ParcelRecord with all available data.
    """
    config = _load_config(config_path)
    cache_root = Path(config.get("cache", {}).get("root", "./cache")).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)

    parcel_cap = config.get("run", {}).get("parcel_cap", 5)

    # Enforce parcel cap
    run_state = _load_run_state(cache_root)
    if run_state["parcel_count"] >= parcel_cap:
        raise RuntimeError(
            f"parcel_cap of {parcel_cap} reached. Reset {cache_root / _RUN_STATE_FILE} to continue."
        )

    run_errors = []
    manual_retrievals = []

    with PoliteClient(config) as client:
        # --- Step 1: Resolution ---
        resolved_parcel_id = parcel_id
        resolved_address = address

        # Step 2: Property Appraiser
        appraiser_record = None
        try:
            from .adapters.property_appraiser import fetch as fetch_appraiser
            logger.info("Step 2: Fetching property appraiser record")
            appraiser_record = fetch_appraiser(
                client=client,
                parcel_id=resolved_parcel_id,
                address=resolved_address,
                cache_root=cache_root,
            )
        except Exception as exc:
            logger.error("Property appraiser failed: %s", exc)
            run_errors.append({
                "adapter": "property_appraiser",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })

        # Extract jurisdiction and owner names from appraiser record
        jurisdiction = None
        owner_names = []
        prior_owner_names = []
        situs_address = resolved_address or ""
        re_number = None

        if appraiser_record:
            jurisdiction = appraiser_record.jurisdiction
            owner_names = appraiser_record.owner_names or []
            re_number = appraiser_record.re_number
            if appraiser_record.situs_address:
                situs_address = appraiser_record.situs_address
            if not resolved_parcel_id:
                resolved_parcel_id = appraiser_record.parcel_id or appraiser_record.folio
            # Prior owners from sales history
            for sale in (appraiser_record.sales_history or []):
                if sale.grantor and sale.grantor not in owner_names:
                    prior_owner_names.append(sale.grantor)

        resolved_parcel_id = resolved_parcel_id or mls_number or "unknown"
        search_names = list(owner_names) + list(prior_owner_names)

        # --- Step 3: Tax Collector ---
        tax_record = None
        try:
            from .adapters.tax_collector import fetch as fetch_tax
            logger.info("Step 3: Fetching tax collector record")
            tax_record = fetch_tax(
                client=client,
                parcel_id=resolved_parcel_id,
                cache_root=cache_root,
            )
        except Exception as exc:
            logger.error("Tax collector failed: %s", exc)
            run_errors.append({
                "adapter": "tax_collector",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })

        # --- Step 4: Clerk Official Records ---
        official_records = []
        try:
            from .adapters.clerk_official import fetch as fetch_clerk_official
            logger.info("Step 4: Fetching clerk official records")
            official_records = fetch_clerk_official(
                client=client,
                owner_names=search_names,
                parcel_id=resolved_parcel_id,
                cache_root=cache_root,
            )
        except Exception as exc:
            logger.error("Clerk official failed: %s", exc)
            run_errors.append({
                "adapter": "clerk_official",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })

        # --- Step 5: Clerk Civil ---
        court_cases = []
        try:
            from .adapters.clerk_civil import fetch as fetch_clerk_civil
            logger.info("Step 5: Fetching clerk civil records")
            court_cases = fetch_clerk_civil(
                client=client,
                owner_names=search_names,
                address=situs_address,
                cache_root=cache_root,
            )
        except Exception as exc:
            logger.error("Clerk civil failed: %s", exc)
            run_errors.append({
                "adapter": "clerk_civil",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })

        # --- Step 6: Permits via JurisdictionRouter ---
        all_permits = []
        permit_adapter_names = route(jurisdiction or "")
        logger.info(
            "Step 6: Running permit adapters %s for jurisdiction '%s'",
            permit_adapter_names,
            jurisdiction,
        )

        permit_adapter_map = {
            "mcesearch": "adapters.permits.mcesearch",
            "opal": "adapters.permits.opal",
            "key_west": "adapters.permits.key_west",
            "marathon": "adapters.permits.marathon",
            "islamorada": "adapters.permits.islamorada",
            "key_colony": "adapters.permits.key_colony",
            "layton": "adapters.permits.layton",
        }

        for adapter_name in permit_adapter_names:
            module_path = permit_adapter_map.get(adapter_name)
            if not module_path:
                logger.warning("Unknown permit adapter: %s", adapter_name)
                continue
            try:
                import importlib
                mod = importlib.import_module(f".{module_path}", package="keys_records")
                permits = mod.fetch(
                    client=client,
                    parcel_id=resolved_parcel_id,
                    address=situs_address,
                    cache_root=cache_root,
                )
                all_permits.extend(permits)
                logger.info("Permit adapter %s returned %d records", adapter_name, len(permits))
            except ManualRetrievalRequired as exc:
                logger.warning("Manual retrieval required: %s", exc.reason)
                manual_retrievals.append(ManualRetrievalRequiredSchema(
                    source=exc.source,
                    reason=exc.reason,
                    contact=exc.contact,
                    url=exc.url,
                ))
            except Exception as exc:
                logger.error("Permit adapter %s failed: %s", adapter_name, exc)
                run_errors.append({
                    "adapter": f"permits.{adapter_name}",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                })

        # --- Step 7: Geocode + FEMA Flood ---
        flood_record = None
        try:
            from .adapters.flood import fetch as fetch_flood, geocode
            logger.info("Step 7: Geocoding address and fetching flood data")
            lat, lon = _geocode_address(situs_address, cache_root)
            if lat and lon:
                year_built = None
                if appraiser_record and appraiser_record.improvements:
                    for imp in appraiser_record.improvements:
                        if imp.year_built:
                            year_built = imp.year_built
                            break
                flood_record = fetch_flood(
                    lat=lat,
                    lon=lon,
                    year_built=year_built,
                    cache_root=cache_root,
                    cache_key=f"flood_{resolved_parcel_id}",
                )
            else:
                logger.warning("Geocoding failed for address: %s", situs_address)
        except Exception as exc:
            logger.error("Flood adapter failed: %s", exc)
            run_errors.append({
                "adapter": "flood",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })

        # --- Assemble ParcelRecord ---
        address_dict = {
            "full": situs_address,
            "street": situs_address,
            "city": "",
            "state": "FL",
            "zip": "",
        }
        # Try to parse address parts
        if situs_address:
            # Simple parsing: last token might be zip, second-to-last state
            parts = situs_address.split(",")
            if len(parts) >= 2:
                address_dict["street"] = parts[0].strip()
                city_state_zip = parts[-1].strip()
                m = __import__("re").search(r"(\d{5}(?:-\d{4})?)", city_state_zip)
                if m:
                    address_dict["zip"] = m.group(1)
                if len(parts) >= 3:
                    address_dict["city"] = parts[1].strip()

        record = ParcelRecord(
            parcel_id=resolved_parcel_id,
            re_number=re_number,
            address=address_dict,
            jurisdiction=jurisdiction,
            owner_names=owner_names,
            prior_owner_names=prior_owner_names,
            appraiser=appraiser_record,
            tax_collector=tax_record,
            official_records=official_records,
            court_cases=court_cases,
            permits=all_permits,
            flood=flood_record,
            manual_retrievals_required=manual_retrievals,
            run_errors=run_errors,
        )

        # Write final JSON
        parcel_dir = cache_root / resolved_parcel_id
        parcel_dir.mkdir(parents=True, exist_ok=True)
        output_path = parcel_dir / "record.json"
        output_path.write_text(
            json.dumps(dataclasses.asdict(record), indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("Wrote record to %s", output_path)

        # Update run state
        run_state["parcel_count"] += 1
        if resolved_parcel_id not in run_state["parcels"]:
            run_state["parcels"].append(resolved_parcel_id)
        _save_run_state(cache_root, run_state)

        return record
