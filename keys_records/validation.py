"""
Validation harness — runs the pipeline on known test parcels and produces
a coverage matrix and operational metrics report.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("validation")

_DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"

# Known test parcels
TEST_CASES = [
    {
        "mls": "619378",
        "address": "6501 Oceanview Ave, Marathon, FL 33050",
        "expected_jurisdiction": "marathon",
        "label": "Marathon (city permit system)",
    },
    {
        "mls": "612427",
        "address": "Cudjoe Key, FL",
        "expected_jurisdiction": "unincorporated",
        "label": "Cudjoe Key — unincorporated Monroe County",
    },
]

# Portal connectivity checks (address doesn't need to match a real parcel)
CONNECTIVITY_CHECKS = [
    {
        "name": "Monroe County Property Appraiser (qPublic)",
        "url": "https://qpublic.schneidercorp.com/Application.aspx?AppID=605",
    },
    {
        "name": "Monroe County Tax Collector",
        "url": "https://www.monroecounty-fl.gov/etc/rp/search.php",
    },
    {
        "name": "Monroe County Clerk",
        "url": "https://www.monroe-clerk.com",
    },
    {
        "name": "MCeSearch",
        "url": "https://mcesearch.monroecounty-fl.gov/search/permits",
    },
    {
        "name": "OPAL Discovery",
        "url": "https://www.monroecounty-fl.gov/1278/Online-Permitting-Services",
    },
    {
        "name": "Key West eTRAKiT",
        "url": "https://etrakit.cityofkeywest-fl.gov/eTRAKiT",
    },
    {
        "name": "Marathon ViewPointCloud",
        "url": "https://marathonfl.viewpointcloud.com",
    },
    {
        "name": "Islamorada",
        "url": "https://www.islamorada.fl.us",
    },
    {
        "name": "FEMA NFHL API",
        "url": "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query",
    },
]

# Fields to check for coverage
COVERAGE_FIELDS = {
    "appraiser": [
        "parcel_id", "owner_names", "situs_address", "jurisdiction",
        "legal_description", "valuation_history", "sales_history", "improvements",
    ],
    "tax_collector": [
        "account_number", "tax_years", "delinquent",
    ],
    "official_records": ["__count__"],
    "court_cases": ["__count__"],
    "permits": ["__count__"],
    "flood": [
        "zone", "base_flood_elevation", "firm_panel", "sfha",
    ],
}


def _load_config(config_path: Optional[str]) -> dict:
    path = Path(config_path) if config_path else _DEFAULT_CONFIG
    with open(path) as fh:
        return yaml.safe_load(fh)


def _check_field(record_dict: dict, section: str, field: str) -> str:
    """Return 'populated', 'empty', or 'missing'."""
    section_data = record_dict.get(section)
    if section_data is None:
        return "missing"
    if field == "__count__":
        if isinstance(section_data, list):
            return "populated" if len(section_data) > 0 else "empty"
        return "empty"
    if isinstance(section_data, dict):
        val = section_data.get(field)
        if val is None:
            return "empty"
        if isinstance(val, (list,)) and len(val) == 0:
            return "empty"
        return "populated"
    return "missing"


def _read_audit_log(cache_root: Path) -> list:
    audit_path = cache_root / "audit.jsonl"
    entries = []
    if audit_path.exists():
        for line in audit_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    return entries


def _compute_metrics(audit_entries: list, run_start: float) -> dict:
    """Compute operational metrics from audit log entries."""
    from collections import defaultdict
    domain_counts = defaultdict(int)
    delays = []
    cache_hits = 0
    backoffs = 0
    captchas = 0
    circuit_trips = 0

    for entry in audit_entries:
        domain_counts[entry.get("domain", "unknown")] += 1
        delay = entry.get("delay_used")
        if delay is not None:
            delays.append(delay)
        if entry.get("cache_hit"):
            cache_hits += 1
        error = entry.get("error", "")
        if error and "blocked" in error:
            backoffs += 1
        if error == "captcha":
            captchas += 1

    total_requests = len(audit_entries)
    run_duration = time.monotonic() - run_start

    return {
        "total_requests": total_requests,
        "requests_per_domain": dict(domain_counts),
        "run_duration_seconds": round(run_duration, 1),
        "average_delay_seconds": round(sum(delays) / len(delays), 2) if delays else 0,
        "min_delay_seconds": round(min(delays), 2) if delays else 0,
        "cache_hits": cache_hits,
        "cache_hit_rate": round(cache_hits / total_requests, 3) if total_requests else 0,
        "backoffs": backoffs,
        "circuit_breaker_trips": circuit_trips,
        "captchas_detected": captchas,
    }


def _check_portal_reachability(config: dict) -> dict:
    """Check each portal is reachable (plain HTTP request, not browser)."""
    import requests as req_lib
    results = {}
    for check in CONNECTIVITY_CHECKS:
        name = check["name"]
        url = check["url"]
        try:
            resp = req_lib.get(url, timeout=15, allow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (compatible; keys-records-validator/1.0)"
            })
            results[name] = {
                "url": url,
                "status": resp.status_code,
                "reachable": resp.status_code < 500,
            }
        except Exception as exc:
            results[name] = {
                "url": url,
                "status": None,
                "reachable": False,
                "error": str(exc),
            }
    return results


def run_validation(config_path: Optional[str] = None) -> dict:
    """
    Run validation harness.

    1. Check portal reachability for all sources
    2. Run full pipeline on two known test parcels
    3. Build coverage matrix
    4. Compute operational metrics from audit log
    5. Write validation_report.json
    6. Print readable summary
    7. Return validation dict
    """
    run_start = time.monotonic()
    config = _load_config(config_path)
    cache_root = Path(config.get("cache", {}).get("root", "./cache")).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "portal_reachability": {},
        "pipeline_runs": [],
        "coverage_matrix": {},
        "per_source_counts": {},
        "operational_metrics": {},
        "warnings": [],
    }

    # Step 1: Portal reachability
    logger.info("Checking portal reachability...")
    try:
        report["portal_reachability"] = _check_portal_reachability(config)
    except Exception as exc:
        logger.warning("Portal reachability check failed: %s", exc)
        report["warnings"].append(f"Portal reachability check error: {exc}")

    # Step 2: Pipeline runs
    from .orchestrator import run as run_pipeline

    for test_case in TEST_CASES:
        logger.info("Running pipeline for %s", test_case["label"])
        try:
            # Override parcel_cap for validation by temporarily patching run state
            run_result = {
                "label": test_case["label"],
                "mls": test_case["mls"],
                "address": test_case["address"],
                "status": "ok",
                "errors": [],
                "manual_retrievals": [],
                "record_summary": {},
            }

            record = run_pipeline(
                mls_number=test_case["mls"],
                address=test_case["address"],
                config_path=config_path,
            )

            record_dict = record.to_dict()
            run_result["record_summary"] = {
                "parcel_id": record_dict.get("parcel_id"),
                "jurisdiction": record_dict.get("jurisdiction"),
                "owner_count": len(record_dict.get("owner_names", [])),
                "permit_count": len(record_dict.get("permits", [])),
                "official_record_count": len(record_dict.get("official_records", [])),
                "court_case_count": len(record_dict.get("court_cases", [])),
                "has_appraiser": record_dict.get("appraiser") is not None,
                "has_tax_collector": record_dict.get("tax_collector") is not None,
                "has_flood": record_dict.get("flood") is not None,
            }
            run_result["errors"] = record_dict.get("run_errors", [])
            run_result["manual_retrievals"] = [
                {"source": m["source"], "reason": m["reason"]}
                for m in record_dict.get("manual_retrievals_required", [])
            ]

            # --- Coverage matrix ---
            coverage = {}
            for section, fields in COVERAGE_FIELDS.items():
                coverage[section] = {}
                for field in fields:
                    coverage[section][field] = _check_field(record_dict, section, field)

            # Check permit routing
            jur = record_dict.get("jurisdiction") or ""
            permit_count = len(record_dict.get("permits", []))
            if permit_count == 0:
                from .jurisdiction_router import route
                expected_adapters = route(jur)
                errors = record_dict.get("run_errors", [])
                permit_errors = [e for e in errors if "permit" in e.get("adapter", "")]
                coverage["permits"]["routing_note"] = (
                    f"0 permits found; expected adapters: {expected_adapters}; "
                    f"permit adapter errors: {len(permit_errors)}"
                )

            report["coverage_matrix"][test_case["label"]] = coverage

            # --- Per-source counts ---
            counts = {
                "sales_rows": 0,
                "valuation_rows": 0,
                "permits": permit_count,
                "official_records": len(record_dict.get("official_records", [])),
                "court_cases": len(record_dict.get("court_cases", [])),
                "tax_years": 0,
            }
            if record_dict.get("appraiser"):
                a = record_dict["appraiser"]
                counts["sales_rows"] = len(a.get("sales_history", []))
                counts["valuation_rows"] = len(a.get("valuation_history", []))
            if record_dict.get("tax_collector"):
                counts["tax_years"] = len(record_dict["tax_collector"].get("tax_years", []))

            report["per_source_counts"][test_case["label"]] = counts

        except Exception as exc:
            logger.error("Pipeline run failed for %s: %s", test_case["label"], exc)
            run_result = {
                "label": test_case["label"],
                "mls": test_case["mls"],
                "status": "error",
                "error": str(exc),
            }
            report["warnings"].append(f"Pipeline failed for {test_case['label']}: {exc}")

        report["pipeline_runs"].append(run_result)

    # Step 3: Operational metrics from audit log
    audit_entries = _read_audit_log(cache_root)
    report["operational_metrics"] = _compute_metrics(audit_entries, run_start)

    # Write report
    report_path = cache_root / "validation_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info("Validation report written to %s", report_path)

    # Print readable summary
    print("\n" + "=" * 70)
    print("VALIDATION REPORT SUMMARY")
    print("=" * 70)
    print(f"Generated: {report['generated_at']}")
    print()

    print("PORTAL REACHABILITY:")
    for name, result in report["portal_reachability"].items():
        status = "OK" if result.get("reachable") else "UNREACHABLE"
        code = result.get("status", "N/A")
        print(f"  [{status:12s}] {name} (HTTP {code})")

    print()
    print("PIPELINE RUNS:")
    for run in report["pipeline_runs"]:
        print(f"  {run['label']}: {run.get('status', 'unknown').upper()}")
        summary = run.get("record_summary", {})
        if summary:
            print(f"    Parcel ID:  {summary.get('parcel_id', 'N/A')}")
            print(f"    Permits:    {summary.get('permit_count', 0)}")
            print(f"    OR Instr:   {summary.get('official_record_count', 0)}")
            print(f"    Appraiser:  {'yes' if summary.get('has_appraiser') else 'NO'}")
            print(f"    Tax:        {'yes' if summary.get('has_tax_collector') else 'NO'}")
            print(f"    Flood:      {'yes' if summary.get('has_flood') else 'NO'}")
        errors = run.get("errors", [])
        if errors:
            print(f"    Errors: {len(errors)}")
        manual = run.get("manual_retrievals", [])
        if manual:
            for m in manual:
                print(f"    Manual required: {m['source']} — {m['reason']}")

    print()
    metrics = report["operational_metrics"]
    print("OPERATIONAL METRICS:")
    print(f"  Total requests:    {metrics.get('total_requests', 0)}")
    print(f"  Run duration:      {metrics.get('run_duration_seconds', 0):.1f}s")
    print(f"  Avg delay:         {metrics.get('average_delay_seconds', 0):.2f}s")
    print(f"  Cache hit rate:    {metrics.get('cache_hit_rate', 0):.1%}")
    print(f"  Backoffs:          {metrics.get('backoffs', 0)}")
    print(f"  CAPTCHAs detected: {metrics.get('captchas_detected', 0)}")
    print("=" * 70)

    return report
