#!/usr/bin/env python3
"""
keys_dd.py -- Florida Keys property due-diligence tool.

Given an MLS number or street address, this tool resolves the Monroe County
parcel, pulls raw public records from every applicable Keys government system,
analyzes the data, and writes a Word report plus a JSON verdict file.

Usage:
    python keys_dd.py --address "6501 Oceanview Ave, Marathon, FL 33050"
    python keys_dd.py --mls 619378
    python keys_dd.py --validate
    python keys_dd.py --help

Environment variables:
    FLKEYS_RESO_TOKEN   -- RESO Web API bearer token (optional)
    FLKEYS_RESO_URL     -- RESO Web API base URL (optional)
    SEARCH_API_KEY      -- Serper / Brave / Bing API key (optional)
    SEARCH_PROVIDER     -- serper | brave | bing (default: serper)

Outputs land in ./output/ unless overridden with --output-dir.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import yaml

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stderr,
)
log = logging.getLogger("keys_dd")

# ── Default paths ─────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent
DEFAULT_CONFIG = str(_HERE / "keys_records" / "config.yaml")
DEFAULT_OUTPUT_DIR = "./output"


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


# ── Address resolution ────────────────────────────────────────────────────────

def _resolve_listing(mls_number: Optional[str], address: Optional[str]) -> dict:
    """
    Attempt to resolve listing data via keys_mls. Returns a listing dict
    (possibly sparse). Never raises -- errors become warnings.
    """
    if mls_number:
        try:
            import keys_mls
            log.info("Resolving listing data for MLS %s", mls_number)
            result = keys_mls.resolve(mls_number, address_hint=address)
            result["mls_number"] = mls_number
            return result
        except Exception as exc:
            log.warning("keys_mls.resolve failed: %s", exc)

    # Minimal listing dict from address only
    listing: dict = {"mls_number": mls_number or "", "address": {}}
    if address:
        parts = address.split(",")
        listing["address"] = {
            "full": address,
            "street": parts[0].strip() if parts else address,
            "city": parts[1].strip() if len(parts) > 1 else "",
            "state": "FL",
            "zip": parts[-1].strip() if len(parts) > 2 else "",
        }
    return listing


# ── Retrieval ─────────────────────────────────────────────────────────────────

def _run_retrieval(
    mls_number: Optional[str],
    parcel_id: Optional[str],
    address: Optional[str],
    config_path: str,
):
    """Run the keys_records retrieval pipeline. Returns a ParcelRecord or None."""
    try:
        from keys_records.orchestrator import run as retrieve
        log.info("Starting public-records retrieval")
        return retrieve(
            mls_number=mls_number,
            parcel_id=parcel_id,
            address=address,
            config_path=config_path,
        )
    except Exception as exc:
        log.error("Retrieval pipeline failed: %s", exc, exc_info=True)
        return None


# ── Main workflow ─────────────────────────────────────────────────────────────

def main(
    mls_number: Optional[str] = None,
    address: Optional[str] = None,
    parcel_id: Optional[str] = None,
    config_path: str = DEFAULT_CONFIG,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    validate: bool = False,
) -> int:
    """
    Returns 0 on success, 1 on hard error.
    """
    # ── Validation harness ─────────────────────────────────────────────────
    if validate:
        from keys_records.validation import run_validation
        report = run_validation(config_path)
        print(json.dumps(report, indent=2, default=str))
        return 0

    if not any([mls_number, address, parcel_id]):
        _print_help()
        return 1

    config = _load_config(config_path)

    # ── Step 1: Resolve listing data (fast, no Playwright) ─────────────────
    listing = _resolve_listing(mls_number, address)
    log.info("Listing resolved: price=%s, hoa=%s",
             listing.get("list_price"), listing.get("hoa_fee"))

    # ── Step 2: Hard gates (run before any expensive retrieval) ────────────
    from keys_analysis import check_gates, run_diagnostics, build_verdict
    gate_results = check_gates(listing, config)

    if gate_results.get("rejected"):
        from keys_report import write_json, _label
        label = _label(
            "", mls_number or "",
            listing.get("address", {}).get("full") or address or ""
        )
        reason = gate_results.get("rejection_reason", "")
        val = gate_results.get("rejected_value")
        ceiling_key = "list_price" if reason == "over_price_ceiling" else "hoa_monthly"
        ceiling = config.get("ceilings", {}).get(ceiling_key, "?")

        if reason == "over_price_ceiling":
            line = (
                f"REJECTED. List price ${int(val):,} exceeds the "
                f"${int(ceiling):,} ceiling. No data pulled."
            )
        else:
            line = (
                f"REJECTED. HOA fee ${int(val):,}/month exceeds the "
                f"${int(ceiling):,}/month ceiling. No data pulled."
            )

        print(line)
        stub_verdict = build_verdict(gate_results, [])
        json_path = write_json(stub_verdict, output_dir, label)
        print(f"Rejection record written to: {json_path}")
        _print_run_instructions(listing, stub_verdict, json_path, None, config)
        return 0

    # ── Step 3: Retrieval ──────────────────────────────────────────────────
    addr_str = (
        listing.get("address", {}).get("full")
        or address
        or ""
    )
    parcel_record = _run_retrieval(
        mls_number=mls_number,
        parcel_id=parcel_id or listing.get("parcel", {}).get("parcel_id"),
        address=addr_str,
        config_path=config_path,
    )

    if parcel_record is None:
        log.warning(
            "Retrieval returned no ParcelRecord. "
            "Analysis will run against empty data; all diagnostics will be UNKNOWN."
        )

    # ── Step 4: Analysis ───────────────────────────────────────────────────
    diagnostics = run_diagnostics(listing, parcel_record, config)
    verdict = build_verdict(gate_results, diagnostics)

    # ── Step 5: Write outputs ──────────────────────────────────────────────
    from keys_report import write_json, write_docx, _label

    label = _label(
        "", mls_number or "",
        (listing.get("address") or {}).get("full") or address or ""
    )

    json_path = write_json(verdict, output_dir, label)
    log.info("Verdict JSON written to %s", json_path)

    try:
        docx_path = write_docx(listing, parcel_record, verdict, output_dir, label)
        log.info("Word report written to %s", docx_path)
    except ImportError:
        log.warning(
            "python-docx not installed. Word report skipped. "
            "Run: pip install python-docx"
        )
        docx_path = None
    except Exception as exc:
        log.error("Word report failed: %s", exc, exc_info=True)
        docx_path = None

    # ── Step 6: Print run instructions ────────────────────────────────────
    _print_run_instructions(listing, verdict, json_path, docx_path, config)

    return 0


# ── Run instructions ──────────────────────────────────────────────────────────

def _print_run_instructions(listing, verdict, json_path, docx_path, config):
    price_ceil = config.get("ceilings", {}).get("list_price", 1_800_000)
    hoa_ceil = config.get("ceilings", {}).get("hoa_monthly", 2_000)
    status = verdict.get("status", "N/A") if isinstance(verdict, dict) else "N/A"
    summary = verdict.get("summary", "") if isinstance(verdict, dict) else ""

    addr = (listing.get("address") or {}).get("full") or ""
    mls = listing.get("mls_number") or ""

    print()
    print("=" * 70)
    print("  keys_dd.py -- run complete")
    print("=" * 70)
    print(f"  Verdict:     {status}")
    if summary:
        print(f"  {summary}")
    print()
    print("  Outputs:")
    print(f"    JSON:  {json_path}")
    if docx_path:
        print(f"    Word:  {docx_path}")
    else:
        print("    Word:  not written (install python-docx to enable)")
    print()
    print("  To run again:")
    if mls:
        print(f"    python keys_dd.py --mls {mls}")
    if addr:
        print(f'    python keys_dd.py --address "{addr}"')
    print()
    print("  Ceilings in force:")
    print(f"    List price:     ${price_ceil:,}")
    print(f"    HOA (monthly):  ${hoa_ceil:,}/month")
    print("=" * 70)
    print()


def _print_help():
    print(__doc__)
    print()
    print("  Examples:")
    print('    python keys_dd.py --address "1113 De Lussan Lane, Cudjoe Key, FL 33042"')
    print("    python keys_dd.py --mls 619378")
    print('    python keys_dd.py --mls 619378 --address "6501 Oceanview Ave, Marathon, FL 33050"')
    print("    python keys_dd.py --validate")
    print()
    print("  Ceilings (edit keys_records/config.yaml to change):")
    print("    List price ceiling:  $1,800,000")
    print("    HOA monthly ceiling: $2,000/month")
    print()


# ── CLI entry point ───────────────────────────────────────────────────────────

def _cli():
    parser = argparse.ArgumentParser(
        description=(
            "Florida Keys property due-diligence tool. "
            "Retrieves public records from all Keys government systems, "
            "analyzes for buyer risk factors, and writes a Word report."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python keys_dd.py --mls 619378\n'
            '  python keys_dd.py --address "6501 Oceanview Ave, Marathon, FL 33050"\n'
            '  python keys_dd.py --mls 612427 --address "1113 De Lussan Lane, Cudjoe Key, FL 33042"\n'
            "  python keys_dd.py --validate\n"
        ),
    )
    parser.add_argument("--mls", metavar="NUMBER",
                        help="Keys MLS listing number (e.g. 619378)")
    parser.add_argument("--address", metavar="ADDR",
                        help='Full street address (e.g. "6501 Oceanview Ave, Marathon, FL 33050")')
    parser.add_argument("--parcel", metavar="ID",
                        help="Monroe County parcel/folio ID (bypasses appraiser search)")
    parser.add_argument("--config", default=DEFAULT_CONFIG, metavar="PATH",
                        help=f"Path to YAML config (default: {DEFAULT_CONFIG})")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, metavar="DIR",
                        help=f"Directory for output files (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--validate", action="store_true",
                        help="Run the validation harness against known test parcels")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable DEBUG logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.validate and not any([args.mls, args.address, args.parcel]):
        _print_help()
        parser.print_usage()
        sys.exit(1)

    sys.exit(
        main(
            mls_number=args.mls,
            address=args.address,
            parcel_id=args.parcel,
            config_path=args.config,
            output_dir=args.output_dir,
            validate=args.validate,
        )
    )


if __name__ == "__main__":
    _cli()
