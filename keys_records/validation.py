"""
Validation harness — smoke-tests the full pipeline against known live parcels
and builds a coverage matrix showing which sources returned data.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("keys_records.validation")

# Known test parcels:
#   Marathon parcel  — MLS 619378, 6501 Oceanview Ave, Marathon FL 33050
#     Monroe County parcel IDs use format XXXXX-XXXXX-XXXXXX
#   Cudjoe Key parcel — MLS 612427, unincorporated Monroe County
TEST_PARCELS = [
    {
        "label": "Marathon — 6501 Oceanview Ave",
        "mls_number": "619378",
        "parcel_id": None,
        "address": "6501 Oceanview Ave, Marathon, FL 33050",
    },
    {
        "label": "Cudjoe Key — unincorporated Monroe",
        "mls_number": "612427",
        "parcel_id": None,
        "address": None,
    },
]

# Sources that should return data vs. those known to be manual-only
EXPECTED_SOURCES = [
    "property_appraiser",
    "tax_collector",
    "clerk_official",
    "clerk_civil",
    "permits",
    "flood",
]

MANUAL_ONLY_SOURCES = {
    "key_colony",
    "layton",
}


def run_validation(config_path: Optional[str] = None) -> dict:
    """
    Run the full pipeline against TEST_PARCELS and return a coverage report.
    """
    from .orchestrator import run as orchestrate

    results = []
    for spec in TEST_PARCELS:
        label = spec["label"]
        log.info("=== Validating: %s ===", label)
        start = time.time()
        try:
            record = orchestrate(
                mls_number=spec.get("mls_number"),
                parcel_id=spec.get("parcel_id"),
                address=spec.get("address"),
                config_path=config_path,
            )
            elapsed = time.time() - start
            coverage = _build_coverage(record)
            results.append({
                "label": label,
                "elapsed_s": round(elapsed, 1),
                "status": "ok",
                "coverage": coverage,
                "manual_notes": record.manual_retrieval_notes,
                "run_log_summary": _summarize_run_log(record.run_log),
            })
        except Exception as e:
            elapsed = time.time() - start
            log.error("Validation failed for %s: %s", label, e)
            results.append({
                "label": label,
                "elapsed_s": round(elapsed, 1),
                "status": "error",
                "error": str(e),
            })

    matrix = _build_matrix(results)
    report = {
        "validation_ts": time.time(),
        "parcels_tested": len(TEST_PARCELS),
        "results": results,
        "coverage_matrix": matrix,
    }
    _print_report(report)
    return report


def _build_coverage(record) -> dict:
    """Return a dict mapping source name → coverage status."""
    coverage = {}

    # Property Appraiser
    if record.appraiser is not None:
        pa = record.appraiser
        populated = [
            f for f in ("parcel_id", "owner_names", "situs_address", "just_value",
                        "assessed_value", "sales", "improvements", "valuations")
            if getattr(pa, f, None)
        ]
        coverage["property_appraiser"] = {
            "status": "populated" if populated else "empty",
            "fields": populated,
        }
    else:
        coverage["property_appraiser"] = {"status": "unreachable", "fields": []}

    # Tax Collector
    if record.tax_collector is not None:
        tc = record.tax_collector
        populated = [
            f for f in ("tax_years", "delinquent", "certificates")
            if getattr(tc, f, None)
        ]
        coverage["tax_collector"] = {
            "status": "populated" if populated else "empty",
            "fields": populated,
        }
    else:
        coverage["tax_collector"] = {"status": "unreachable", "fields": []}

    # Official Records
    n = len(record.official_records or [])
    coverage["clerk_official"] = {
        "status": "populated" if n > 0 else "empty",
        "count": n,
    }

    # Civil Cases
    n = len(record.court_cases or [])
    coverage["clerk_civil"] = {
        "status": "populated" if n > 0 else "empty",
        "count": n,
    }

    # Permits
    n = len(record.permits or [])
    coverage["permits"] = {
        "status": "populated" if n > 0 else "empty",
        "count": n,
    }

    # Flood
    if record.flood is not None:
        coverage["flood"] = {
            "status": "populated",
            "zone": record.flood.flood_zone,
            "panel": record.flood.firm_panel,
        }
    else:
        coverage["flood"] = {"status": "empty"}

    # Manual-only flags from notes
    for note in (record.manual_retrieval_notes or []):
        for src in MANUAL_ONLY_SOURCES:
            if src.replace("_", " ") in note.lower() or src in note.lower():
                coverage[src] = {"status": "manual_required"}

    return coverage


def _summarize_run_log(run_log: list) -> dict:
    if not run_log:
        return {}
    summary: dict[str, str] = {}
    for entry in run_log:
        src = entry.get("source", "unknown")
        status = entry.get("status", "?")
        summary[src] = status
    return summary


def _build_matrix(results: list) -> dict:
    """Build a cross-tab: source → parcel → status."""
    matrix: dict[str, dict] = {}
    for result in results:
        label = result["label"]
        coverage = result.get("coverage", {})
        for src, info in coverage.items():
            if src not in matrix:
                matrix[src] = {}
            matrix[src][label] = info.get("status", "unknown")
    return matrix


def _print_report(report: dict):
    print("\n" + "=" * 70)
    print(f"  keys_records Validation Report")
    print(f"  Parcels tested: {report['parcels_tested']}")
    print("=" * 70)

    for result in report["results"]:
        print(f"\n  [{result['status'].upper()}] {result['label']}  ({result['elapsed_s']}s)")
        if result["status"] == "error":
            print(f"    ERROR: {result.get('error')}")
            continue

        cov = result.get("coverage", {})
        log_sum = result.get("run_log_summary", {})
        for src in EXPECTED_SOURCES:
            info = cov.get(src, {})
            status = info.get("status", "not_attempted")
            count = info.get("count", "")
            fields = info.get("fields", [])
            extra = f" ({count})" if count != "" else ""
            extra += f" {fields}" if fields else ""
            print(f"    {src:<22} {status}{extra}")

        notes = result.get("manual_notes", [])
        if notes:
            print(f"\n    Manual retrieval required ({len(notes)}):")
            for n in notes:
                print(f"      • {n}")

    print("\n  Coverage Matrix:")
    matrix = report.get("coverage_matrix", {})
    labels = [r["label"] for r in report["results"]]
    col_w = max(len(l) for l in labels) if labels else 20
    header = "  " + f"{'Source':<24}" + "".join(f"  {l[:col_w]:<{col_w}}" for l in labels)
    print(header)
    print("  " + "-" * len(header))
    for src, by_label in sorted(matrix.items()):
        row = f"  {src:<24}" + "".join(f"  {by_label.get(l, '---'):<{col_w}}" for l in labels)
        print(row)

    print("=" * 70 + "\n")
