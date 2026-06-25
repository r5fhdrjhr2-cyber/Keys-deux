"""
Report writer — JSON stub and Word (.docx) due-diligence report.
Reads from listing dict, ParcelRecord, and verdict dict; does zero I/O
beyond writing the two output files.
"""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

# ── Helpers ───────────────────────────────────────────────────────────────────

def _g(obj: Any, key: str, default=None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _money(n: Any) -> str:
    try:
        return f"${int(n):,}"
    except (TypeError, ValueError):
        return str(n) if n is not None else "N/A"


def _or(v: Any, fallback: str = "N/A") -> str:
    if v is None or v == "" or v == []:
        return fallback
    return str(v)


def _safe_dict(obj: Any) -> dict:
    """Convert a dataclass or dict to a plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        return dataclasses.asdict(obj)
    except TypeError:
        return obj.__dict__


def _label(path: str, mls: str, addr: str) -> str:
    """Build a filename-safe label."""
    raw = mls or re.sub(r"[^\w\s]", "", addr or "")[:30]
    return re.sub(r"\s+", "_", raw.strip())


# ── JSON output ───────────────────────────────────────────────────────────────

def write_json(verdict: dict, output_dir: str | Path, label: str) -> Path:
    """Write the verdict dict (plus run date) to JSON. Returns the path."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    fname = out / f"{label}_{today}.json"

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "verdict": verdict,
    }

    def _default(o: Any):
        try:
            return dataclasses.asdict(o)
        except TypeError:
            return str(o)

    fname.write_text(json.dumps(payload, indent=2, default=_default), encoding="utf-8")
    return fname


# ── Word report ───────────────────────────────────────────────────────────────

def write_docx(
    listing: dict,
    parcel_record: Any,
    verdict: dict,
    output_dir: str | Path,
    label: str,
) -> Path:
    """Build and write the .docx report. Returns the path."""
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    fname = out / f"{label}_{today}.docx"

    doc = Document()
    _set_margins(doc)

    pr = parcel_record
    address_full = _g(listing, "address", {})
    if isinstance(address_full, dict):
        address_str = address_full.get("full") or address_full.get("street") or ""
    else:
        address_str = str(address_full or "")

    mls = _g(listing, "mls_number") or ""
    parcel_id = (_g(pr, "parcel_id") or _g(listing, "parcel", {}).get("parcel_id") or "N/A")
    jurisdiction = _g(pr, "jurisdiction") or _g(listing, "jurisdiction") or "N/A"
    list_price = _g(listing, "list_price")
    price_ceiling = _g(verdict, "gate_results", {}).get("price_ceiling", 1_800_000)
    verdict_status = _g(verdict, "status") or "N/A"
    verdict_summary = _g(verdict, "summary") or ""

    # ── Cover block ────────────────────────────────────────────────────────────
    h = doc.add_heading("Florida Keys Property Due Diligence", level=1)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER

    cover = [
        ("Address", address_str or "N/A"),
        ("MLS Number", mls or "N/A"),
        ("Parcel ID", parcel_id),
        ("Jurisdiction", jurisdiction),
        ("Report Date", today),
        ("Verdict", verdict_status),
        ("List Price", _money(list_price) if list_price else "N/A"),
        ("Price Ceiling", _money(price_ceiling)),
    ]
    t = doc.add_table(rows=len(cover), cols=2)
    t.style = "Table Grid"
    for i, (k, v) in enumerate(cover):
        t.cell(i, 0).text = k
        cell = t.cell(i, 1)
        cell.text = v
        if k == "Verdict":
            _color_cell(cell, verdict_status)
    doc.add_paragraph()

    _bold_para(doc, verdict_summary)
    doc.add_paragraph()

    # ── PART ONE: DATA PULLED ─────────────────────────────────────────────────
    doc.add_heading("Part One: Data Pulled", level=1)
    doc.add_paragraph(
        "All data below is retrieved directly from government sources with provenance noted. "
        "Nothing in this section is interpreted or derived."
    )

    _section_property_snapshot(doc, listing, pr)
    _section_sales_history(doc, pr)
    _section_valuation_tax(doc, pr)
    _section_permits(doc, pr)
    _section_official_records(doc, pr)
    _section_court_cases(doc, pr)
    _section_flood(doc, pr, listing)
    _section_sources_reached(doc, pr, verdict)
    _section_manual_todo(doc, pr)

    # ── PART TWO: SYNTHESIS ───────────────────────────────────────────────────
    doc.add_heading("Part Two: Synthesis", level=1)
    doc.add_paragraph(
        "This section leads with reasons not to buy and open unknowns. "
        "Every claim names the field it rests on. Estimates are labeled as estimates."
    )

    _section_reasons_not_to_buy(doc, verdict)
    _section_estimates(doc, verdict)
    _section_unknowns(doc, verdict)
    _section_verdict_next_step(doc, verdict)

    doc.save(str(fname))
    return fname


# ── Section builders ──────────────────────────────────────────────────────────

def _section_property_snapshot(doc, listing, pr):
    doc.add_heading("Property Snapshot", level=2)
    appraiser = _g(pr, "appraiser")
    imps = _g(appraiser, "improvements") or []
    main_imp = imps[0] if imps else None

    # When the appraiser is unreachable, an empty appraiser field is unknown, not
    # absent. Label it so it never reads as a confirmed blank.
    appr_gap = "Appraiser unreachable - see Sources Reached" if appraiser is None else "N/A"

    def appr(value):
        """Appraiser-sourced value, or an honest unreachable/N/A marker."""
        return _or(value, appr_gap)

    # Parcel ID can come from the listing (tax number) even when appraiser fails.
    listing_parcel = _g(listing, "parcel") or {}
    parcel_id = (
        _g(pr, "parcel_id")
        or _g(appraiser, "parcel_id")
        or (listing_parcel.get("parcel_id") if isinstance(listing_parcel, dict) else None)
    )

    # Subdivision: prefer appraiser, fall back to listing.
    subdiv = (
        " / ".join(filter(None, [
            _g(appraiser, "subdivision"),
            _g(appraiser, "block"),
            _g(appraiser, "lot"),
        ]))
        or _g(listing, "subdivision")
        or appr_gap
    )

    rows = [
        ("List Price",
         _money(_g(listing, "list_price")) if _g(listing, "list_price") else "N/A",
         "listing"),
        ("Parcel / Tax ID",
         _or(parcel_id),
         "listing / appraiser"),
        ("Beds / Baths",
         f"{_or(_g(listing,'beds'))} / {_or(_g(listing,'baths'))}",
         "listing"),
        ("Living Area (sqft)",
         _or(_g(listing, "living_area_sqft")),
         "listing"),
        ("Lot Size (sqft)",
         _or(_g(listing, "lot_size_sqft")),
         "listing"),
        ("Year Built",
         _or(_g(listing, "year_built") or (_g(main_imp, "year_built") if main_imp else None)),
         "appraiser / listing"),
        ("Effective Year Built",
         appr(_g(main_imp, "effective_year") if main_imp else None),
         "appraiser"),
        ("Property Type",
         _or(_g(listing, "property_type")),
         "listing"),
        ("Waterfront",
         _or(_g(listing, "waterfront")),
         "listing"),
        ("Water / Sewer",
         _or(_g(listing, "water_sewer")),
         "listing"),
        ("Roof",
         _or(_g(listing, "roof")),
         "listing"),
        ("Use Code",
         appr(_g(appraiser, "property_use_code")),
         "appraiser"),
        ("Zoning",
         appr(_g(appraiser, "zoning")),
         "appraiser"),
        ("Jurisdiction",
         _or(_g(appraiser, "jurisdiction") or _g(pr, "jurisdiction"), appr_gap),
         "appraiser"),
        ("Owner(s)",
         "; ".join(_g(pr, "owner_names") or []) or appr_gap,
         "appraiser"),
        ("Legal Description",
         (appr(_g(appraiser, "legal_description")))[:120],
         "appraiser"),
        ("Subdivision / Block / Lot",
         subdiv,
         "appraiser / listing"),
        ("HOA Fee (monthly)",
         _money(_g(listing, "hoa_fee")) if _g(listing, "hoa_fee") else "Not published",
         "listing"),
        ("Annual Taxes (listing)",
         _money(_g(listing, "annual_taxes")) if _g(listing, "annual_taxes") else "N/A",
         "listing"),
    ]

    _key_value_table(doc, rows, source_col=True)
    doc.add_paragraph()


def _section_sales_history(doc, pr):
    doc.add_heading("Sales History", level=2)
    appraiser = _g(pr, "appraiser")
    sales = _g(appraiser, "sales_history") or []

    if not sales:
        if appraiser is None:
            doc.add_paragraph(
                "Unknown. The property appraiser was unreachable this run, so sales "
                "history could not be retrieved. This is not a confirmed absence of "
                "sales. See Sources Reached."
            )
        else:
            doc.add_paragraph("No sales history returned from property appraiser.")
        return

    hdrs = ["Date", "Price", "Grantor", "Grantee", "OR Book/Page", "Type", "Qualification"]
    rows = []
    for s in sales:
        book = _g(s, "or_book") or ""
        page = _g(s, "or_page") or ""
        bp = f"{book}/{page}" if book or page else _or(_g(s, "instrument_number"))
        rows.append([
            _or(_g(s, "date")),
            _money(_g(s, "price")) if _g(s, "price") else "N/A",
            _or(_g(s, "grantor")),
            _or(_g(s, "grantee")),
            bp,
            _or(_g(s, "deed_type")),
            _or(_g(s, "sale_qualification")),
        ])
    _table(doc, hdrs, rows)
    _source_note(doc, "Monroe County Property Appraiser (qPublic AppID 605)")
    doc.add_paragraph()


def _section_valuation_tax(doc, pr):
    doc.add_heading("Valuation and Tax History", level=2)
    appraiser = _g(pr, "appraiser")
    val_hist = _g(appraiser, "valuation_history") or []
    tax_collector = _g(pr, "tax_collector")
    tax_years = _g(tax_collector, "tax_years") or []

    # Index tax years for merge
    tax_by_year: dict = {}
    for ty in tax_years:
        yr = _g(ty, "year")
        if yr:
            tax_by_year[str(yr)] = ty

    if not val_hist and not tax_years:
        if appraiser is None and tax_collector is None:
            doc.add_paragraph(
                "Unknown. Neither the property appraiser nor the tax collector "
                "returned data this run, so valuation and tax history could not be "
                "retrieved. This is not a confirmed absence. See Sources Reached."
            )
        else:
            doc.add_paragraph("No valuation or tax history returned.")
        return

    all_years = sorted(
        set(
            [str(_g(v, "year")) for v in val_hist if _g(v, "year")]
            + list(tax_by_year.keys())
        ),
        reverse=True,
    )

    hdrs = [
        "Year", "Market Value", "Assessed Value", "Taxable Value",
        "Homestead", "SOH Cap Diff",
        "Gross Tax", "Millage", "Paid", "Date Paid", "Status",
        "Non-Ad Valorem",
    ]
    rows = []
    for yr_s in all_years:
        val = next((v for v in val_hist if str(_g(v, "year")) == yr_s), None)
        ty = tax_by_year.get(yr_s)
        hs = "Yes" if _g(val, "homestead_exemption") else "No"
        navs = "; ".join(str(n) for n in (_g(ty, "non_ad_valorem") or []))
        rows.append([
            yr_s,
            _money(_g(val, "just_value")) if _g(val, "just_value") else "N/A",
            _money(_g(val, "assessed_value")) if _g(val, "assessed_value") else "N/A",
            _money(_g(val, "taxable_value")) if _g(val, "taxable_value") else "N/A",
            hs,
            _money(_g(val, "soh_cap_differential")) if _g(val, "soh_cap_differential") else "N/A",
            _money(_g(ty, "gross_tax")) if _g(ty, "gross_tax") else "N/A",
            _or(_g(ty, "millage")),
            _money(_g(ty, "amount_paid")) if _g(ty, "amount_paid") else "N/A",
            _or(_g(ty, "date_paid")),
            _or(_g(ty, "status")),
            navs or "None",
        ])
    _table(doc, hdrs, rows)

    # Delinquency / certificate note
    delinquent = _g(tax_collector, "delinquent")
    delinquent_years = _g(tax_collector, "delinquent_years") or []
    certs = _g(tax_collector, "tax_certificates") or []
    if delinquent:
        _warn_para(doc, f"DELINQUENT: Years {', '.join(str(y) for y in delinquent_years)}.")
    if certs:
        _warn_para(doc, f"Tax certificates sold: {len(certs)} certificate(s) recorded.")

    _source_note(doc,
                 "Monroe County Property Appraiser (qPublic); "
                 "Monroe County Tax Collector")
    doc.add_paragraph()


def _section_permits(doc, pr):
    doc.add_heading("Permit History", level=2)
    permits = _g(pr, "permits") or []
    manual = _g(pr, "manual_retrievals_required") or []
    parcel_id = _g(pr, "parcel_id")
    jurisdiction = _g(pr, "jurisdiction")

    permit_manual = [
        m for m in manual
        if any(k in ((_g(m, "source") or "").lower())
               for k in ("opal", "mcesearch", "viewpoint", "etrakit", "cityview",
                         "permit", "key colony", "layton"))
    ]

    if not permits:
        if not parcel_id:
            doc.add_paragraph(
                "Unknown. No parcel ID was resolved, so the parcel-keyed permit "
                "search could not run reliably (the county states parcel search is "
                "more complete than address). This is not a confirmed absence of "
                "permits. Supply --parcel to enable the search."
            )
        elif permit_manual:
            doc.add_paragraph(
                f"Unknown. The permit system for jurisdiction "
                f"'{jurisdiction or 'unknown'}' could not be reached this run (see "
                "the manual-retrieval notice below). This is NOT a confirmed "
                "absence of permits and NOT confirmation that all work was "
                "permitted — retrieve permit history directly from the jurisdiction."
            )
        else:
            doc.add_paragraph(
                f"No permits returned for parcel {parcel_id} in jurisdiction "
                f"'{jurisdiction or 'unknown'}'. Confirm jurisdiction routing was "
                "correct before concluding all work is permitted."
            )
        # Still surface any manual-retrieval notices below.
        if not manual:
            doc.add_paragraph()
            return

    OPEN_STATUSES = {"applied", "issued", "active", "expired"}

    hdrs = [
        "Permit Number", "Jurisdiction", "System", "Type",
        "Status", "Applied", "Issued", "Finaled", "Value", "Contractor",
    ]
    rows = []
    for p in permits:
        status = str(_g(p, "status") or "")
        finaled = _g(p, "finaled_date")
        flag = " [OPEN/EXPIRED]" if status in OPEN_STATUSES and not finaled else ""
        rows.append([
            _or(_g(p, "permit_number")) + flag,
            _or(_g(p, "jurisdiction")),
            _or(_g(p, "source_system")),
            _or(_g(p, "permit_type")),
            status,
            _or(_g(p, "applied_date")),
            _or(_g(p, "issued_date")),
            _or(finaled),
            _money(_g(p, "declared_value")) if _g(p, "declared_value") else "N/A",
            _or(_g(p, "contractor_name")),
        ])
    if rows:
        _table(doc, hdrs, rows)

    for item in manual:
        source = _g(item, "source") or str(item)
        reason = _g(item, "reason") or ""
        contact = _g(item, "contact") or ""
        doc.add_paragraph(
            f"Manual retrieval required — {source}: {reason} Contact: {contact}"
        )

    _source_note(doc,
                 "MCeSearch (Monroe County legacy); OPAL (Monroe County current); "
                 "eTRAKiT (Key West); ViewPointCloud (Marathon); CityView (Islamorada)")
    doc.add_paragraph()


def _clerk_unreached(pr) -> bool:
    """True if a Clerk source flagged a manual retrieval (could not be reached)."""
    for m in (_g(pr, "manual_retrievals_required") or []):
        src = (_g(m, "source") or "").lower()
        if "clerk" in src:
            return True
    return False


def _section_official_records(doc, pr):
    doc.add_heading("Recorded Instruments (Official Records)", level=2)
    recs = _g(pr, "official_records") or []
    owners = _g(pr, "owner_names") or []

    if not recs:
        if not owners:
            doc.add_paragraph(
                "Unknown. Owner names were not resolved (the property appraiser was "
                "unreachable), so the name-based Official Records search could not "
                "run. This is not a confirmed absence of liens, mortgages, or "
                "non-conversion agreements. Resolve the owner name (via --parcel or "
                "manual appraiser lookup) to enable this search."
            )
        elif _clerk_unreached(pr):
            doc.add_paragraph(
                "Unknown. The Clerk Official Records index could not be reached this "
                f"run for owner name(s) {', '.join(owners)} (see the manual-retrieval "
                "notice in Sources Reached). This is NOT a confirmed absence of "
                "liens, mortgages, or non-conversion agreements — search the Clerk "
                "Official Records directly by owner name."
            )
        else:
            doc.add_paragraph(
                "No official records returned from Monroe County Clerk for the "
                f"searched owner name(s): {', '.join(owners)}."
            )
        return

    ALERT_TAGS = {
        "lis_pendens", "lien", "construction_lien", "claim_of_lien",
        "code_enforcement_lien", "judgment", "non_conversion_agreement",
    }

    hdrs = [
        "Recording Date", "Type", "Book/Page or Instrument",
        "Grantor", "Grantee", "Consideration", "Tags",
    ]
    rows = []
    for rec in recs:
        book = _g(rec, "book") or ""
        page = _g(rec, "page") or ""
        inst = _g(rec, "instrument_number") or ""
        ref = f"{book}/{page}" if book else inst
        tags = _g(rec, "tags") or []
        tag_str = ", ".join(tags)
        flag = " [ALERT]" if any(t in ALERT_TAGS for t in tags) else ""
        rows.append([
            _or(_g(rec, "recording_date")),
            _or(_g(rec, "instrument_type")),
            ref,
            _or(_g(rec, "grantor")),
            _or(_g(rec, "grantee")),
            _money(_g(rec, "consideration")) if _g(rec, "consideration") else "N/A",
            tag_str + flag,
        ])
    _table(doc, hdrs, rows)
    _source_note(doc, "Monroe County Clerk of Courts — Official Records")
    doc.add_paragraph()


def _section_court_cases(doc, pr):
    doc.add_heading("Court and Foreclosure Cases", level=2)
    cases = _g(pr, "court_cases") or []
    owners = _g(pr, "owner_names") or []

    if not cases:
        if not owners:
            doc.add_paragraph(
                "Unknown. Owner names were not resolved (the property appraiser was "
                "unreachable), so the name-based civil and foreclosure docket search "
                "could not run. This is not a confirmed absence of litigation or "
                "foreclosure. Resolve the owner name to enable this search."
            )
        elif _clerk_unreached(pr):
            doc.add_paragraph(
                "Unknown. The Clerk civil/foreclosure docket could not be reached this "
                f"run for owner name(s) {', '.join(owners)} (see the manual-retrieval "
                "notice in Sources Reached). This is NOT a confirmed absence of "
                "litigation or foreclosure — search the Clerk civil docket directly."
            )
        else:
            doc.add_paragraph(
                "No civil or foreclosure cases returned from Monroe County Clerk for "
                f"the searched owner name(s): {', '.join(owners)}."
            )
        return

    hdrs = ["Case Number", "Type", "Filing Date", "Status", "Parties"]
    rows = []
    for c in cases:
        parties = "; ".join(str(p) for p in (_g(c, "parties") or []))
        rows.append([
            _or(_g(c, "case_number")),
            _or(_g(c, "case_type")),
            _or(_g(c, "filing_date")),
            _or(_g(c, "status")),
            parties[:80],
        ])
    _table(doc, hdrs, rows)
    _source_note(doc, "Monroe County Clerk of Courts — Civil Case Records")
    doc.add_paragraph()


def _section_flood(doc, pr, listing):
    doc.add_heading("Flood Zone (FEMA NFHL)", level=2)
    flood = _g(pr, "flood")
    # Fall back to keys_mls flood data if no Playwright flood record
    if flood is None:
        flood = _g(listing, "flood")

    if flood is None:
        doc.add_paragraph(
            "Flood data not returned. Provide a geocoded address or coordinates to query "
            "the FEMA National Flood Hazard Layer."
        )
        return

    rows = [
        ("Flood Zone", _or(_g(flood, "zone")), "FEMA NFHL"),
        ("Base Flood Elevation", _or(_g(flood, "base_flood_elevation")), "FEMA NFHL"),
        ("FIRM Panel", _or(_g(flood, "firm_panel")), "FEMA NFHL"),
        ("Pre-FIRM", str(_g(flood, "pre_firm", False)), "FEMA NFHL"),
        ("SFHA (Special Flood Hazard Area)",
         str(_g(flood, "sfha")) if _g(flood, "sfha") is not None else "N/A",
         "FEMA NFHL"),
        ("Vertical Datum", _or(_g(flood, "v_datum")), "FEMA NFHL"),
    ]
    _key_value_table(doc, rows, source_col=True)
    _source_note(doc, "FEMA NFHL ArcGIS REST API (Layer 28)")
    doc.add_paragraph()


def _section_sources_reached(doc, pr, verdict):
    doc.add_heading("Sources Reached", level=2)
    appraiser = _g(pr, "appraiser")
    tax_col = _g(pr, "tax_collector")
    off_recs = _g(pr, "official_records")
    court = _g(pr, "court_cases")
    permits = _g(pr, "permits")
    flood = _g(pr, "flood")
    manual = _g(pr, "manual_retrievals_required") or []
    errors = _g(pr, "run_errors") or []

    # Which sources flagged a manual retrieval (could not be reached/parsed)?
    # An unreached source must NOT read as "0 found" — that is a false all-clear.
    manual_src = " ".join(
        ((_g(m, "source") or "") if not isinstance(m, str) else m).lower()
        for m in manual
    )
    clerk_unreached = "clerk" in manual_src
    permits_unreached = any(k in manual_src for k in (
        "opal", "mcesearch", "viewpoint", "etrakit", "cityview", "permit",
        "key colony", "layton"))

    def _count_or_unreached(items, noun, unreached):
        n = len(items or [])
        if n == 0 and unreached:
            return f"not retrieved this run — manual retrieval required (NOT a confirmed zero {noun})"
        return f"{n} {noun}(s)"

    status_lines = [
        f"Property Appraiser: {'returned data' if appraiser else 'no data returned'}",
        f"Tax Collector: {'returned data' if tax_col else 'not retrieved — manual retrieval required' if 'tax' in manual_src else 'no data returned'}",
        f"Clerk Official Records: {_count_or_unreached(off_recs, 'instrument', clerk_unreached)}",
        f"Clerk Civil Cases: {_count_or_unreached(court, 'case', clerk_unreached)}",
        f"Permits: {_count_or_unreached(permits, 'permit', permits_unreached)} across all systems",
        f"Flood (FEMA NFHL): {'returned data' if flood else 'no data returned'}",
    ]
    for line in status_lines:
        doc.add_paragraph(line, style="List Bullet")

    if manual:
        doc.add_paragraph("Manual retrieval required:")
        for item in manual:
            src = _g(item, "source") or str(item)
            contact = _g(item, "contact") or ""
            doc.add_paragraph(f"  {src} -- {contact}", style="List Bullet")

    if errors:
        doc.add_paragraph(f"Run errors encountered: {len(errors)} source(s) failed. See JSON output for details.")

    doc.add_paragraph()


_MANUAL_TODO_WORKFLOWS = {
    "tax": {
        "title": "Tax Collector — Bill, Payment History, Delinquency",
        "url": "https://www.monroecounty-fl.gov/1210/Tax-Collector",
        "steps": [
            "Go to https://www.monroecounty-fl.gov/1210/Tax-Collector or "
            "https://monroetaxcollector.com/",
            "Click 'Property Tax Search' or 'Search by Real Estate Number'.",
            "Enter the parcel/folio ID (shown on this report).",
            "Confirm current year and prior year tax status (paid / unpaid / delinquent).",
            "Download the current tax bill PDF; note any non-ad-valorem assessments "
            "(sewer, stormwater, solid waste) — these are special assessments that "
            "transfer with title.",
            "Check for any outstanding tax certificates or tax deed proceedings.",
        ],
    },
    "clerk_official": {
        "title": "Clerk Official Records — Liens, Mortgages, Lis Pendens, Non-Conversion",
        "url": "https://www.monroe-clerk.com/",
        "steps": [
            "Go to https://www.monroe-clerk.com/",
            "Click 'Official Records' in the navigation.",
            "Search by grantor/grantee name for each owner listed on this report.",
            "Also search the parcel's legal description (subdivision + lot) if available.",
            "Look specifically for: open mortgages, claims of lien, construction liens, "
            "lis pendens, judgments, and non-conversion agreements.",
            "Open each result and download the document image if relevant.",
            "A non-conversion agreement (restricting STR use) will appear as an instrument "
            "recorded against the parcel — search both current and prior owner names.",
        ],
    },
    "clerk_civil": {
        "title": "Clerk Civil / Foreclosure Docket — Active Litigation",
        "url": "https://www.monroe-clerk.com/",
        "steps": [
            "Go to https://www.monroe-clerk.com/",
            "Click 'Civil' or 'Case Search' in the navigation.",
            "Search by party name for each owner listed on this report.",
            "Look for: active foreclosure cases, pending judgments, or HOA actions.",
            "Check case status — active vs. dismissed vs. closed.",
        ],
    },
    "mcesearch": {
        "title": "MCeSearch Permits — Unincorporated Monroe County (before Oct 2022)",
        "url": "https://mcesearch.monroecounty-fl.gov/search/permits",
        "steps": [
            "Go to https://mcesearch.monroecounty-fl.gov/search/permits",
            "Enter the parcel ID in the search field and submit.",
            "Review ALL permits — note any with status 'Open', 'Expired', or 'Void'.",
            "Open/expired permits without a final inspection indicate unfinalized work "
            "that may be unpermitted from the buyer's standpoint.",
            "Download the permit list and individual permit details for your records.",
            "Applies to: unincorporated Monroe County properties only (not Key West, "
            "Marathon, Islamorada, Key Colony Beach, or Layton).",
        ],
    },
    "opal": {
        "title": "OPAL Permits — Unincorporated Monroe County (Oct 2022 – present)",
        "url": "https://opal.monroecounty-fl.gov/",
        "steps": [
            "Go to https://opal.monroecounty-fl.gov/",
            "Search by address or parcel ID.",
            "Review permits issued after October 2022.",
            "Note any open or expired permits.",
            "Applies to: unincorporated Monroe County properties only.",
        ],
    },
    "key_west": {
        "title": "Key West Permits — eTRAKiT",
        "url": "https://www.cityofkeywest-fl.gov/",
        "steps": [
            "Go to the City of Key West eTRAKiT portal.",
            "Search by address or parcel number.",
            "Review all permit history, open permits, and expired permits.",
        ],
    },
    "marathon": {
        "title": "Marathon Permits — ViewPointCloud",
        "url": "https://www.ci.marathon.fl.us/",
        "steps": [
            "Go to the City of Marathon ViewPointCloud permit portal.",
            "Search by address.",
            "Review all permit history and note open or expired permits.",
        ],
    },
    "islamorada": {
        "title": "Islamorada Permits — CityView",
        "url": "https://www.islamorada.fl.us/",
        "steps": [
            "Go to the Village of Islamorada CityView permit portal.",
            "Search by address or parcel number.",
            "Review all permit history.",
        ],
    },
    "key colony": {
        "title": "Key Colony Beach Permits — Manual (no online portal)",
        "url": "",
        "steps": [
            "Key Colony Beach does not have an online permit portal.",
            "Contact the Key Colony Beach Building Department directly:",
            "  Phone: (305) 289-1212",
            "  Address: 600 W Ocean Dr, Key Colony Beach, FL 33051",
            "Request the complete permit history for the property address.",
        ],
    },
    "layton": {
        "title": "Layton Permits — Manual (no online portal)",
        "url": "",
        "steps": [
            "City of Layton does not have an online permit portal.",
            "Contact the City of Layton directly:",
            "  Phone: (305) 852-9099",
            "  Address: 68260 Overseas Hwy, Long Key, FL 33001",
            "Request the complete permit history for the property address.",
        ],
    },
}


def _section_manual_todo(doc, pr):
    """
    Write a Manual Retrieval To-Do List section for every source that could
    not be reached this run. Each item includes the direct URL and step-by-step
    workflow instructions for a human researcher.
    """
    manual = _g(pr, "manual_retrievals_required") or []
    if not manual:
        return

    doc.add_heading("Manual Retrieval To-Do List", level=2)
    doc.add_paragraph(
        "The following sources could not be retrieved automatically this run. "
        "Each item below provides direct links and step-by-step workflow instructions. "
        "Complete every item before concluding due diligence — an unreached source is "
        "NOT a confirmed all-clear."
    )

    for item in manual:
        source = (_g(item, "source") or str(item)).lower()
        source_display = _g(item, "source") or str(item)
        reason = _g(item, "reason") or ""
        contact_url = _g(item, "url") or _g(item, "contact") or ""

        # Match to workflow template by keyword
        workflow = None
        for kw, wf in _MANUAL_TODO_WORKFLOWS.items():
            if kw in source:
                workflow = wf
                break

        # Section heading for this item
        title = (workflow["title"] if workflow else source_display)
        p_title = doc.add_paragraph()
        run = p_title.add_run(title)
        run.bold = True

        # Why it matters
        if reason:
            doc.add_paragraph(reason[:300])

        if workflow:
            url = workflow["url"] or contact_url
            if url:
                doc.add_paragraph(f"URL: {url}")
            doc.add_paragraph("Steps:")
            for step in workflow["steps"]:
                doc.add_paragraph(step, style="List Number")
        else:
            # Fallback: just show the contact info
            if contact_url:
                doc.add_paragraph(f"Contact / URL: {contact_url}")

        doc.add_paragraph()


# ── Part Two sections ─────────────────────────────────────────────────────────

def _section_reasons_not_to_buy(doc, verdict):
    doc.add_heading("Reasons to Walk or Negotiate", level=2)
    doc.add_paragraph(
        "Items below are ordered by severity. Reds require resolution before proceeding; "
        "yellows require acknowledgment. Every item names the field it rests on."
    )

    diagnostics = _g(verdict, "diagnostics") or []
    reds = [d for d in diagnostics if d.get("severity") == "red"]
    yellows = [d for d in diagnostics if d.get("severity") == "yellow"]

    if not reds and not yellows:
        doc.add_paragraph("No red or yellow flags found in pulled records.")
    else:
        for d in reds + yellows:
            sev = d.get("severity", "").upper()
            title = d.get("title", "")
            finding = d.get("finding", "")
            fields = ", ".join(d.get("fields_used") or [])
            p = doc.add_paragraph()
            run = p.add_run(f"[{sev}] {d['id']} {title}: ")
            run.bold = True
            if sev == "RED":
                run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
            else:
                run.font.color.rgb = RGBColor(0xC0, 0x80, 0x00)
            p.add_run(finding)
            if fields:
                f_run = p.add_run(f" [Fields: {fields}]")
                f_run.font.size = Pt(8)
                f_run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    doc.add_paragraph()


def _section_estimates(doc, verdict):
    doc.add_heading("Estimates (not facts)", level=2)
    doc.add_paragraph(
        "These are estimates only. They are labeled as estimates because the inputs "
        "required for precision (Elevation Certificate, live insurance quote, final "
        "millage) are not in the pulled records."
    )

    diagnostics = _g(verdict, "diagnostics") or []
    estimate_ids = {"2.1", "2.5", "2.10"}
    for d in diagnostics:
        if d.get("id") in estimate_ids:
            p = doc.add_paragraph()
            p.add_run(f"{d['id']} {d['title']}: ").bold = True
            p.add_run(d.get("finding", ""))

    doc.add_paragraph()


def _section_unknowns(doc, verdict):
    doc.add_heading("Open Unknowns", level=2)
    doc.add_paragraph(
        "Each item below could not be determined from pulled records. "
        "The input that would resolve each gap is listed."
    )

    diag_unknowns = _g(verdict, "unknowns_from_diagnostics") or []
    for u in diag_unknowns:
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(f"{u.get('id')} {u.get('title')}: ").bold = True
        p.add_run(u.get("finding", ""))

    doc.add_paragraph("Structural unknowns (always present regardless of data returned):")
    for u in (_g(verdict, "structural_unknowns") or []):
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(f"{u.get('item')}: ").bold = True
        p.add_run(f"Resolve with: {u.get('resolve_with')}")

    doc.add_paragraph()


def _section_verdict_next_step(doc, verdict):
    doc.add_heading("Verdict and Next Step", level=2)
    status = _g(verdict, "status") or "N/A"
    summary = _g(verdict, "summary") or ""

    p = doc.add_paragraph()
    run = p.add_run(f"Verdict: {status}. ")
    run.bold = True
    _color_run(run, status)
    p.add_run(summary)

    reds = _g(verdict, "reds") or []
    unknowns = _g(verdict, "unknowns_from_diagnostics") or []

    doc.add_paragraph()
    if status == "CAUTION" and reds:
        doc.add_paragraph(
            "Next step: do not proceed without resolving each red flag. "
            "Each red item above names the exact field and the input needed. "
            "This document is for individual due diligence; it is not legal, "
            "insurance, or financial advice."
        )
    elif unknowns:
        doc.add_paragraph(
            "Next step: resolve the open unknowns listed above before making an offer. "
            "Particularly: obtain the Elevation Certificate and a live insurance quote "
            "before underwriting the deal. This document is not legal, insurance, or "
            "financial advice."
        )
    else:
        doc.add_paragraph(
            "Next step: obtain the Elevation Certificate and a live insurance quote. "
            "This document is not legal, insurance, or financial advice."
        )

    doc.add_paragraph()
    doc.add_paragraph(
        "DISCLAIMER: This report contains estimates, not facts. Insurance band figures "
        "are illustrative ranges only; actual premiums depend on the Elevation Certificate, "
        "wind mitigation inspection, and coverage terms. Tax estimates are based on "
        "appraiser values and may differ from actual bills. This is not legal, insurance, "
        "or financial advice. Verify all material facts with licensed professionals."
    ).italic = True


# ── Low-level docx helpers ────────────────────────────────────────────────────

def _set_margins(doc):
    from docx.shared import Inches
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)


def _table(doc, headers: list, rows: list):
    from docx.shared import Pt
    t = doc.add_table(rows=1 + len(rows), cols=len(headers))
    t.style = "Table Grid"
    hdr_row = t.rows[0]
    for i, h in enumerate(headers):
        cell = hdr_row.cells[i]
        cell.text = h
        for para in cell.paragraphs:
            for run in para.runs:
                run.bold = True
                run.font.size = Pt(9)
    for ri, row in enumerate(rows):
        tr = t.rows[ri + 1]
        for ci, val in enumerate(row):
            cell = tr.cells[ci]
            cell.text = str(val)
            for para in cell.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(9)


def _key_value_table(doc, rows: list, source_col: bool = False):
    from docx.shared import Pt
    cols = 3 if source_col else 2
    t = doc.add_table(rows=len(rows), cols=cols)
    t.style = "Table Grid"
    for i, row_data in enumerate(rows):
        tr = t.rows[i]
        tr.cells[0].text = row_data[0]
        tr.cells[1].text = str(row_data[1])
        for run in tr.cells[0].paragraphs[0].runs:
            run.bold = True
            run.font.size = Pt(9)
        for run in tr.cells[1].paragraphs[0].runs:
            run.font.size = Pt(9)
        if source_col and len(row_data) > 2:
            tr.cells[2].text = str(row_data[2])
            for run in tr.cells[2].paragraphs[0].runs:
                run.font.size = Pt(8)
                run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)


def _bold_para(doc, text: str):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = True


def _warn_para(doc, text: str):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = True
    run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)


def _source_note(doc, source: str):
    from docx.shared import Pt
    p = doc.add_paragraph(f"Source: {source}")
    for run in p.runs:
        run.font.size = Pt(8)
        run.italic = True
        run.font.color.rgb = RGBColor(0x60, 0x60, 0x60)


def _color_cell(cell, verdict: str):
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    colors = {"CAUTION": "FFC000", "REJECTED": "FF0000", "CHASE": "70AD47"}
    color = colors.get(verdict, "FFFFFF")
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), color)
    shd.set(qn("w:val"), "clear")
    tcPr.append(shd)


def _color_run(run, verdict: str):
    colors = {
        "CAUTION": RGBColor(0xC0, 0x80, 0x00),
        "REJECTED": RGBColor(0xC0, 0x00, 0x00),
        "CHASE": RGBColor(0x38, 0x86, 0x38),
    }
    if verdict in colors:
        run.font.color.rgb = colors[verdict]


# Keep these accessible even if docx not installed yet
try:
    from docx.shared import RGBColor, Pt
except ImportError:
    class RGBColor:
        def __init__(self, r, g, b):
            pass
    class Pt:
        def __new__(cls, v):
            return v
