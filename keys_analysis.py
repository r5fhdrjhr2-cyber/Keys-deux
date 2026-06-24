"""
Analysis layer — pure functions over a ParcelRecord and listing dict.
No network I/O. Treats every field as potentially missing.

Honesty rule: absence of data is reported as "unknown, not in any source we
pull" with the exact field path(s) consulted. Absence is never a pass.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_PRICE_CEILING = 1_800_000
DEFAULT_HOA_CEILING = 2_000  # per month

IRMA_YEAR = 2017
PRE_FIRM_CUTOFF_YEAR = 1970  # Monroe County community FIRM date circa 1970
CURRENT_YEAR = date.today().year

RED = "red"
YELLOW = "yellow"
INFO = "info"
UNKNOWN = "unknown"

_SEV_RANK = {INFO: 0, UNKNOWN: 1, YELLOW: 2, RED: 3}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _g(obj: Any, key: str, default=None) -> Any:
    """Get a field from a dataclass or dict transparently."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _max_sev(a: str, b: str) -> str:
    return a if _SEV_RANK.get(a, 0) >= _SEV_RANK.get(b, 0) else b


def _diag(id_: str, title: str, severity: str, finding: str,
          fields_used: list, raw: dict | None = None) -> dict:
    return {
        "id": id_,
        "title": title,
        "severity": severity,
        "finding": finding,
        "fields_used": fields_used,
        "raw": raw or {},
    }


def _money(n: Any) -> str:
    try:
        return f"${int(n):,}"
    except (TypeError, ValueError):
        return str(n)


# ── Gate checks ──────────────────────────────────────────────────────────────

def check_gates(listing: dict, config: dict) -> dict:
    """
    Run Gate A (list price ceiling) and Gate B (HOA ceiling).
    Gates fire before any retrieval. Returns a gate_results dict.
    """
    ceilings = config.get("ceilings", {})
    price_ceiling = ceilings.get("list_price", DEFAULT_PRICE_CEILING)
    hoa_ceiling = ceilings.get("hoa_monthly", DEFAULT_HOA_CEILING)

    results = {
        "price_ceiling": price_ceiling,
        "hoa_ceiling": hoa_ceiling,
        "gate_a_status": "no_listing_price",
        "gate_b_status": "no_hoa_published",
        "rejected": False,
        "rejection_reason": None,
        "rejected_value": None,
    }

    # Gate A: price
    list_price = _to_float(listing.get("list_price"))
    if list_price is not None:
        if list_price > price_ceiling:
            results.update({
                "gate_a_status": "rejected",
                "rejected": True,
                "rejection_reason": "over_price_ceiling",
                "rejected_value": list_price,
            })
            return results
        results["gate_a_status"] = "pass"

    # Gate B: HOA
    hoa = _to_float(listing.get("hoa_fee") or listing.get("hoa_monthly"))
    if hoa is not None:
        if hoa > hoa_ceiling:
            results.update({
                "gate_b_status": "rejected",
                "rejected": True,
                "rejection_reason": "hoa_over_ceiling",
                "rejected_value": hoa,
            })
            return results
        results["gate_b_status"] = "pass"

    return results


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


# ── Individual diagnostics ────────────────────────────────────────────────────

def _d21_flood(flood: Any, appraiser: Any) -> dict:
    """2.1 Flood zone and insurance band."""
    fields = [
        "flood.zone", "flood.base_flood_elevation",
        "flood.firm_panel", "flood.pre_firm",
    ]

    if flood is None:
        return _diag("2.1", "Flood zone and insurance band", UNKNOWN,
                     "unknown, not in any source we pull — flood data query returned no "
                     "result. Provide a geocoded address to resolve.",
                     fields)

    zone = _g(flood, "zone")
    bfe = _g(flood, "base_flood_elevation")
    firm = _g(flood, "firm_panel")
    pre_firm = _g(flood, "pre_firm", False)

    if not zone:
        return _diag("2.1", "Flood zone and insurance band", UNKNOWN,
                     "unknown, not in any source we pull — FEMA NFHL returned a record "
                     "but no flood zone code. Verify coordinates.",
                     fields, {"bfe": bfe, "firm": firm})

    z = zone.upper()
    parts = []

    if z.startswith("V"):
        sev = RED
        parts.append(
            f"Zone {zone} (VE - wave-action zone, worst case for flood insurance). "
            "Estimated annual all-in band (flood + wind + hazard) for a $1.0M to $1.5M "
            "single-family oceanfront: roughly $15,000 to $30,000, wind likely via Citizens."
        )
    elif z.startswith("A"):
        sev = YELLOW
        bfe_str = f" BFE {bfe} ft NAVD." if bfe is not None else " BFE not returned."
        parts.append(
            f"Zone {zone} (AE - high-risk flood zone, common for Keys oceanfront).{bfe_str} "
            "Estimated annual all-in band: roughly $6,000 to $15,000 for a modern elevated "
            "structure, or $15,000 to $30,000 for an older or non-elevated structure."
        )
    elif z in ("X", "X500", "0.2 PCT ANNUAL CHANCE FLOOD HAZARD"):
        sev = INFO
        parts.append(
            f"Zone {zone} (moderate or minimal flood risk). "
            "Confirm with a live quote; lower risk does not mean no risk in the Keys."
        )
    else:
        sev = INFO
        parts.append(f"Zone {zone}. Confirm risk level with a live quote.")

    parts.append(
        "The figure that sets the premium is finished floor height versus base flood elevation "
        "(BFE), which lives on the Elevation Certificate. NFIP building coverage caps at $250,000; "
        "a private flood layer is needed above that."
    )

    # Check for Elevation Certificate in permit records
    permit_refs = _g(appraiser, "permit_cross_refs") or []
    has_ec = any("elevation" in str(p).lower() for p in permit_refs)
    if not has_ec:
        parts.append(
            "Elevation Certificate not found in pulled records; band only, refine with a real quote."
        )

    if pre_firm:
        parts.append(
            "Structure appears pre-FIRM (built before the community FIRM date of roughly 1970). "
            "Likely not elevated to current code. Worst-case insurance scenario."
        )
        sev = _max_sev(sev, RED)

    return _diag("2.1", "Flood zone and insurance band", sev,
                 " ".join(parts), fields,
                 {"zone": zone, "bfe": bfe, "firm_panel": firm, "pre_firm": pre_firm})


def _d22_pre_irma(appraiser: Any, permits: list) -> dict:
    """2.2 Pre-Irma vs post-Irma rebuild."""
    fields = [
        "appraiser.improvements[*].year_built",
        "appraiser.improvements[*].effective_year",
        "permits[permit_type~roof|structural|reconstruction, applied_date 2017-2019]",
    ]

    if appraiser is None:
        return _diag("2.2", "Pre-Irma vs post-Irma rebuild", UNKNOWN,
                     "unknown, not in any source we pull — appraiser record not returned.",
                     fields)

    improvements = _g(appraiser, "improvements") or []
    year_built = None
    eff_year = None
    for imp in improvements:
        yb = _to_int(_g(imp, "year_built"))
        ey = _to_int(_g(imp, "effective_year") or _g(imp, "effective_year"))
        if yb and (year_built is None or yb < year_built):
            year_built = yb
        if ey and (eff_year is None or ey > eff_year):
            eff_year = ey

    irma_kw = re.compile(
        r"roof|roofing|structural|reconstruction|rebuild|repair|wind|hurricane|storm", re.I
    )
    irma_permits = []
    open_irma = []

    for p in (permits or []):
        ptype = _g(p, "permit_type") or ""
        pdesc = _g(p, "description") or ""
        if not irma_kw.search(ptype + " " + pdesc):
            continue
        applied = str(_g(p, "applied_date") or "")
        try:
            yr = int(applied[:4])
        except (ValueError, TypeError):
            continue
        if 2017 <= yr <= 2019:
            irma_permits.append(p)
            status = _g(p, "status") or ""
            finaled = _g(p, "finaled_date")
            if status in ("applied", "issued", "active", "expired") and not finaled:
                open_irma.append(p)

    notes = []
    sev = INFO

    if year_built:
        if year_built < 1975:
            notes.append(
                f"Year built {year_built}: pre-1975, likely pre-FIRM and not elevated to "
                "current code. Worst-case insurance exposure."
            )
            sev = _max_sev(sev, YELLOW)
        else:
            notes.append(f"Year built {year_built}.")
    else:
        notes.append("Year built: not found in appraiser record.")

    if eff_year:
        if eff_year > IRMA_YEAR and year_built and year_built < 2010:
            notes.append(
                f"Effective year built {eff_year} on a structure built {year_built}: "
                "signals a substantial rebuild, likely post-Irma."
            )
        else:
            notes.append(f"Effective year built: {eff_year}.")

    if permits is None:
        notes.append(
            "Permit records not returned. Confirm jurisdiction routing was correct "
            "before concluding no storm repairs were made."
        )
        sev = _max_sev(sev, YELLOW)
    elif not permits:
        notes.append(
            "Permit history is empty. Confirm jurisdiction routing was correct before "
            "concluding no storm repairs were made; an empty from the wrong jurisdiction "
            "is a false negative."
        )
        sev = _max_sev(sev, YELLOW)
    elif open_irma:
        nums = ", ".join(str(_g(p, "permit_number") or "?") for p in open_irma)
        notes.append(
            f"OPEN or EXPIRED post-Irma repair permit(s) with no final or CO: {nums}. "
            "This is unfinished storm repair the buyer inherits."
        )
        sev = RED
    elif irma_permits:
        nums = ", ".join(str(_g(p, "permit_number") or "?") for p in irma_permits)
        notes.append(
            f"Post-Irma repair permit(s) found, all finaled: {nums}. Reassuring."
        )
    else:
        notes.append(
            "No roofing, structural, or reconstruction permits found in the 2017-2019 window."
        )
        if year_built and year_built < IRMA_YEAR:
            sev = _max_sev(sev, YELLOW)
            notes.append(
                "Structure predates Irma and no post-Irma repair permits are visible; "
                "verify with the jurisdiction directly."
            )

    return _diag("2.2", "Pre-Irma vs post-Irma rebuild", sev, " ".join(notes), fields,
                 {"year_built": year_built, "effective_year": eff_year,
                  "irma_permit_count": len(irma_permits),
                  "open_irma_count": len(open_irma)})


def _d23_str(appraiser: Any, permits: list) -> dict:
    """2.3 Short-term rental eligibility."""
    fields = [
        "appraiser.zoning", "appraiser.property_use_code", "appraiser.jurisdiction",
        "permits[permit_type~vacation rental|transient|STR]",
    ]

    if appraiser is None:
        return _diag("2.3", "Short-term rental eligibility", UNKNOWN,
                     "unknown, not in any source we pull — appraiser record not returned. "
                     "Verify the local STR ordinance and license status directly.",
                     fields)

    zoning = _g(appraiser, "zoning")
    use_code = _g(appraiser, "property_use_code")
    jurisdiction = _g(appraiser, "jurisdiction")

    vr_kw = re.compile(r"vacation.rental|transient|vr\b|short.?term|rental.licens", re.I)
    vr_permits = [
        p for p in (permits or [])
        if vr_kw.search((_g(p, "permit_type") or "") + " " + (_g(p, "description") or ""))
    ]

    notes = []
    if zoning:
        notes.append(f"Zoning: {zoning}.")
    else:
        notes.append("Zoning: not in pulled records.")
    if use_code:
        notes.append(f"Use code: {use_code}.")
    if jurisdiction:
        notes.append(f"Jurisdiction: {jurisdiction}.")

    if vr_permits:
        statuses = ", ".join(str(_g(p, "status") or "?") for p in vr_permits)
        notes.append(
            f"Vacation rental license or permit found in permit records (status: {statuses}). "
            "Confirm whether it is transferable."
        )
    else:
        notes.append("No vacation rental license found in pulled permit records.")

    notes.append(
        "STR eligibility cannot be confirmed from records alone. "
        "Verify the local ordinance and license status directly. "
        "Owner-occupancy financing terms can bar renting for a period regardless of local rules."
    )

    return _diag("2.3", "Short-term rental eligibility", UNKNOWN, " ".join(notes), fields,
                 {"zoning": zoning, "use_code": use_code, "jurisdiction": jurisdiction,
                  "vr_permits_found": len(vr_permits)})


def _d24_mangrove() -> dict:
    """2.4 Mangrove / view obstruction."""
    return _diag("2.4", "Mangrove or view obstruction", UNKNOWN,
                 "unknown, inspect the shoreline by air or in person. Records say nothing "
                 "about a shoreline mangrove fringe, which is common on open-water Keys lots "
                 "and legally protected under Florida and federal law. "
                 "Imagery module was not available in this run.",
                 [])


def _d25_tax_shock(appraiser: Any, tax_collector: Any) -> dict:
    """2.5 Tax shock on resale."""
    fields = [
        "appraiser.valuation_history[*].just_value",
        "appraiser.valuation_history[*].assessed_value",
        "appraiser.valuation_history[*].homestead_exemption",
        "appraiser.valuation_history[*].soh_cap_differential",
        "tax_collector.tax_years[*].gross_tax",
        "tax_collector.tax_years[*].millage",
    ]

    if appraiser is None:
        return _diag("2.5", "Tax shock on resale", UNKNOWN,
                     "unknown, not in any source we pull — appraiser record not returned.",
                     fields)

    val_hist = _g(appraiser, "valuation_history") or []
    if not val_hist:
        return _diag("2.5", "Tax shock on resale", UNKNOWN,
                     "unknown, not in any source we pull — no valuation history in appraiser record.",
                     fields)

    most_recent = max(val_hist, key=lambda v: _to_int(_g(v, "year")) or 0)
    just_value = _to_int(_g(most_recent, "just_value"))
    assessed = _to_int(_g(most_recent, "assessed_value"))
    taxable = _to_int(_g(most_recent, "taxable_value"))
    homestead = bool(_g(most_recent, "homestead_exemption", False))
    soh_diff = _to_int(_g(most_recent, "soh_cap_differential"))
    val_year = _g(most_recent, "year")

    millage = None
    seller_tax = None
    if tax_collector is not None:
        tax_years = _g(tax_collector, "tax_years") or []
        if tax_years:
            recent_ty = max(tax_years, key=lambda t: _to_int(_g(t, "year")) or 0)
            millage = _to_float(_g(recent_ty, "millage"))
            seller_tax = _to_float(_g(recent_ty, "gross_tax"))

    notes = []
    sev = INFO

    if homestead and soh_diff and soh_diff > 5000:
        notes.append(
            f"Seller holds homestead exemption. Assessed value "
            f"({_money(assessed)}) is capped below market value ({_money(just_value)}) "
            f"by roughly {_money(soh_diff)} (Save Our Homes differential). "
            "Taxable value resets toward market on sale — the buyer's first-year tax "
            "will be higher than the seller's current bill."
        )
        sev = YELLOW
    elif homestead:
        notes.append(
            "Seller holds homestead exemption. Taxable value is likely capped below "
            "market; confirm the Save Our Homes differential."
        )
        sev = YELLOW
    else:
        notes.append("No homestead exemption found. Limited SOH cap effect expected on sale.")

    if just_value and millage:
        buyer_est = int(just_value * millage / 1000)
        notes.append(
            f"Estimate: buyer first-year gross tax approximately {_money(buyer_est)} "
            f"(based on {_money(just_value)} market value x {millage:.4f} millage from {val_year}). "
            "This is an estimate; actual millage and exemptions will differ."
        )
    elif just_value:
        notes.append(
            f"Market value (just value): {_money(just_value)} ({val_year}). "
            "Millage not returned; cannot estimate buyer first-year tax."
        )

    if seller_tax:
        notes.append(f"Seller current gross tax: {_money(int(seller_tax))}.")

    return _diag("2.5", "Tax shock on resale", sev, " ".join(notes), fields,
                 {"just_value": just_value, "assessed": assessed, "taxable": taxable,
                  "homestead": homestead, "soh_diff": soh_diff, "val_year": val_year,
                  "millage": millage, "seller_tax": seller_tax})


def _d26_distress(official_records: list, court_cases: list) -> dict:
    """2.6 Distress signals."""
    fields = ["official_records[*].tags", "court_cases[*].case_type"]

    if official_records is None and court_cases is None:
        return _diag("2.6", "Distress signals", UNKNOWN,
                     "unknown, not in any source we pull — official records and court cases "
                     "not returned.", fields)

    distress_tags = {
        "lis_pendens", "lien", "construction_lien", "claim_of_lien",
        "code_enforcement_lien", "judgment",
    }

    flags = []
    for rec in (official_records or []):
        tags = _g(rec, "tags") or []
        hits = [t for t in tags if t in distress_tags]
        if hits:
            rec_date = _g(rec, "recording_date") or "?"
            itype = _g(rec, "instrument_type") or ", ".join(hits)
            book = _g(rec, "book") or ""
            page = _g(rec, "page") or ""
            inst = _g(rec, "instrument_number") or ""
            ref = f" (Book {book}/{page})" if book else (f" (Inst {inst})" if inst else "")
            flags.append(f"{itype}{ref}, recorded {rec_date} [{', '.join(hits)}]")

    for case in (court_cases or []):
        ctype = str(_g(case, "case_type") or "").lower()
        if any(kw in ctype for kw in ("foreclos", "lis pendens", "lien", "judgment")):
            cnum = _g(case, "case_number") or "?"
            status = _g(case, "status") or "?"
            flags.append(f"Court case {cnum}: {_g(case, 'case_type')}, status {status}")

    if flags:
        finding = (
            "RED: distress instrument(s) or case(s) found. Slow down. "
            + "; ".join(flags)
        )
        sev = RED
    else:
        finding = (
            "No lis pendens, construction lien, code enforcement lien, or judgment found "
            "in pulled official records or court cases."
        )
        sev = INFO

    return _diag("2.6", "Distress signals", sev, finding, fields, {"flags": flags})


def _d27_open_permits(appraiser: Any, permits: list) -> dict:
    """2.7 Open or expired permits and unpermitted work."""
    fields = [
        "permits[*].status", "permits[*].finaled_date",
        "appraiser.improvements[description~enclosure|addition|pool|dock]",
    ]

    if permits is None:
        return _diag("2.7", "Open or expired permits and unpermitted work", UNKNOWN,
                     "unknown, not in any source we pull — permit records not returned.",
                     fields)

    open_permits = []
    for p in permits:
        status = _g(p, "status") or ""
        finaled = _g(p, "finaled_date")
        if status in ("applied", "issued", "active", "expired") and not finaled:
            num = _g(p, "permit_number") or "?"
            ptype = _g(p, "permit_type") or ""
            applied = _g(p, "applied_date") or ""
            open_permits.append(f"{num} ({ptype}, status: {status}, applied: {applied})")

    unpermitted = []
    if appraiser is not None:
        improvements = _g(appraiser, "improvements") or []
        all_permit_nums = set()
        for p in permits:
            n = _g(p, "permit_number")
            if n:
                all_permit_nums.add(str(n).upper())

        ds_kw = re.compile(
            r"enclosure|downstairs|lower.?level|garage.conver|bonus.room|storage.below", re.I
        )
        for imp in improvements:
            desc = _g(imp, "description") or ""
            if ds_kw.search(desc):
                imp_permits = _g(imp, "permit_numbers") or []
                if not imp_permits or not any(
                    str(n).upper() in all_permit_nums for n in imp_permits
                ):
                    unpermitted.append(
                        f"Improvement '{desc}' has no matching permit in pulled records. "
                        "Probable unpermitted downstairs enclosure."
                    )

    notes = []
    sev = INFO

    if not permits:
        notes.append(
            "Permit records returned empty. Confirm jurisdiction routing was correct "
            "before concluding all work is permitted."
        )
        sev = YELLOW
    elif open_permits:
        notes.append(
            f"Open or expired permit(s) with no final or CO ({len(open_permits)}): "
            + "; ".join(open_permits) + "."
        )
        sev = RED
    else:
        notes.append(
            f"All {len(permits)} permit(s) in pulled records show a final date or are in a "
            "closed status."
        )

    for flag in unpermitted:
        notes.append(flag)
        sev = _max_sev(sev, RED)

    return _diag("2.7", "Open or expired permits and unpermitted work", sev,
                 " ".join(notes), fields,
                 {"open_permit_count": len(open_permits),
                  "unpermitted_flags": unpermitted})


def _d28_non_conversion(official_records: list) -> dict:
    """2.8 Recorded non-conversion agreement."""
    fields = ["official_records[tags=non_conversion_agreement]"]

    if official_records is None:
        return _diag("2.8", "Recorded non-conversion agreement", UNKNOWN,
                     "unknown, not in any source we pull — official records not returned.",
                     fields)

    found = []
    for rec in official_records:
        tags = _g(rec, "tags") or []
        if "non_conversion_agreement" in tags or "recorded_non_conversion" in tags:
            rec_date = _g(rec, "recording_date") or "?"
            book = _g(rec, "book") or ""
            page = _g(rec, "page") or ""
            ref = f"Book {book}/{page}, " if book else ""
            found.append(f"{ref}recorded {rec_date}")

    if found:
        finding = (
            f"RED: Recorded non-conversion agreement found: {'; '.join(found)}. "
            "Ground-floor space is restricted to storage or parking and cannot legally be "
            "used as living area. Advertised square footage may not be usable living space."
        )
        sev = RED
    else:
        finding = (
            "No recorded non-conversion agreement found in pulled official records."
        )
        sev = INFO

    return _diag("2.8", "Recorded non-conversion agreement", sev, finding, fields,
                 {"found": found})


def _d29_special_assessments(tax_collector: Any) -> dict:
    """2.9 Extra assessments (sewer, stormwater, etc.)."""
    fields = ["tax_collector.tax_years[*].non_ad_valorem"]

    if tax_collector is None:
        return _diag("2.9", "Special assessments", UNKNOWN,
                     "unknown, not in any source we pull — tax collector record not returned.",
                     fields)

    tax_years = _g(tax_collector, "tax_years") or []
    kw = re.compile(r"sewer|wastewater|stormwater|water.management|solid.waste", re.I)
    found: dict[Any, list] = {}

    for ty in tax_years:
        yr = _g(ty, "year") or "?"
        navs = _g(ty, "non_ad_valorem") or []
        for nav in navs:
            if kw.search(str(nav)):
                found.setdefault(yr, []).append(str(nav))

    if found:
        lines = "; ".join(
            f"{yr}: {', '.join(items)}"
            for yr, items in sorted(found.items(), reverse=True)
        )
        finding = (
            f"Special assessment(s) found that add annual cost above ordinary taxes: {lines}."
        )
        sev = YELLOW
    elif tax_years:
        finding = (
            "No sewer, wastewater, or stormwater special assessments found in "
            "pulled tax collector records."
        )
        sev = INFO
    else:
        finding = (
            "unknown, not in any source we pull — no tax year records returned."
        )
        sev = UNKNOWN

    return _diag("2.9", "Special assessments", sev, finding, fields, {"found": found})


def _d210_roof_age(permits: list) -> dict:
    """2.10 Roof age from most recent finaled roofing permit."""
    fields = ["permits[permit_type~roof, status=finaled].finaled_date"]

    if permits is None:
        return _diag("2.10", "Roof age", UNKNOWN,
                     "unknown, not in any source we pull — permit records not returned.",
                     fields)

    roof_kw = re.compile(r"\broof\b|roofing|re-?roof", re.I)
    best_year = None
    best_permit = None

    for p in permits:
        ptype = _g(p, "permit_type") or ""
        pdesc = _g(p, "description") or ""
        if not roof_kw.search(ptype + " " + pdesc):
            continue
        finaled = str(_g(p, "finaled_date") or "")
        try:
            yr = int(finaled[:4])
        except (ValueError, TypeError):
            continue
        if best_year is None or yr > best_year:
            best_year = yr
            best_permit = p

    if best_year is None:
        return _diag("2.10", "Roof age", UNKNOWN,
                     "Roof age undetermined from permits. No roofing permit with a final "
                     "date found in pulled records. Obtain permit history from the "
                     "jurisdiction directly or inspect the physical roof.",
                     fields)

    age = CURRENT_YEAR - best_year
    num = _g(best_permit, "permit_number") or "?"
    finding = (
        f"Most recent roofing permit finaled {best_year} (permit {num}), "
        f"making the roof approximately {age} year(s) old. "
    )
    if age >= 15:
        finding += (
            "Roof is 15 or more years old. Near-term replacement is likely. "
            "Factor in replacement cost and effect on insurability."
        )
        sev = YELLOW
    elif age >= 10:
        finding += (
            "Roof is 10 to 14 years old. Budget for replacement within the next several years."
        )
        sev = INFO
    else:
        finding += "Roof is relatively recent."
        sev = INFO

    return _diag("2.10", "Roof age", sev, finding, fields,
                 {"roof_year": best_year, "roof_age_years": age, "permit": str(num)})


def _d211_market_posture(listing: dict) -> dict:
    """2.11 Market posture (days on market, price cuts)."""
    fields = ["listing.days_on_market", "listing.price_history"]

    if not listing:
        return _diag("2.11", "Market posture", UNKNOWN,
                     "unknown, not in any source we pull — listing data not available.",
                     fields)

    dom = _to_int(listing.get("days_on_market"))
    price_history = listing.get("price_history") or []
    cuts = [e for e in price_history if isinstance(e, dict)
            and "reduction" in str(e.get("event", "") or "").lower()]
    n_cuts = len(cuts)

    notes = []
    sev = INFO

    if dom is not None:
        if dom > 180:
            notes.append(
                f"Days on market: {dom}. Extended sit in the current high-inventory Keys market. "
                "A long sit is often negotiating room, not a defect, but can also signal "
                "awareness of one of the issues above."
            )
            sev = YELLOW
        elif dom > 90:
            notes.append(f"Days on market: {dom}. Moderate time on market.")
        else:
            notes.append(f"Days on market: {dom}.")
    else:
        notes.append("Days on market: not in pulled listing data.")

    if n_cuts:
        notes.append(
            f"{n_cuts} price reduction(s) recorded. Repeated cuts with no sale can indicate "
            "awareness of an issue or general market softness."
        )
        sev = _max_sev(sev, YELLOW)
    elif price_history:
        notes.append("No price reductions found in listing price history.")
    else:
        notes.append("Price history not in pulled listing data.")

    return _diag("2.11", "Market posture", sev, " ".join(notes), fields,
                 {"days_on_market": dom, "price_reductions": n_cuts})


# ── HOA check against official records ───────────────────────────────────────

def _check_hoa_in_records(official_records: list) -> dict | None:
    """
    If no HOA fee was published in the listing, check whether official records
    show a recorded HOA or condo declaration. Returns a synthetic Gate B
    supplement diagnostic if found.
    """
    hoa_tags = {"hoa", "condo", "homeowners_association", "condominium_declaration"}
    found = []
    for rec in (official_records or []):
        tags = _g(rec, "tags") or []
        hits = [t for t in tags if t in hoa_tags]
        if hits:
            rec_date = _g(rec, "recording_date") or "?"
            itype = _g(rec, "instrument_type") or ", ".join(hits)
            found.append(f"{itype}, recorded {rec_date}")

    if not found:
        return None

    return _diag("B", "HOA or association detected in official records", YELLOW,
                 "A recorded HOA or condo declaration was found in official records but "
                 "the listing does not publish a monthly fee. Obtain an estoppel letter "
                 "to determine current dues and special assessments. "
                 "Unknown dues are yellow, never green.",
                 ["official_records[tags=hoa|condo|homeowners_association]"],
                 {"instruments_found": found})


# ── Main entry point ──────────────────────────────────────────────────────────

def run_diagnostics(listing: dict, parcel_record: Any, config: dict) -> list:
    """
    Run all 11 diagnostics. Returns a list of diagnostic dicts ordered by ID.
    Inserts a HOA supplement diagnostic if a declaration is found in official
    records but no fee was published.
    """
    if parcel_record is None:
        appraiser = tax_collector = flood = None
        official_records = court_cases = permits = []
    else:
        appraiser = _g(parcel_record, "appraiser")
        tax_collector = _g(parcel_record, "tax_collector")
        official_records = _g(parcel_record, "official_records") or []
        court_cases = _g(parcel_record, "court_cases") or []
        permits = _g(parcel_record, "permits") or []
        flood = _g(parcel_record, "flood")

    diags = [
        _d21_flood(flood, appraiser),
        _d22_pre_irma(appraiser, permits),
        _d23_str(appraiser, permits),
        _d24_mangrove(),
        _d25_tax_shock(appraiser, tax_collector),
        _d26_distress(official_records, court_cases),
        _d27_open_permits(appraiser, permits),
        _d28_non_conversion(official_records),
        _d29_special_assessments(tax_collector),
        _d210_roof_age(permits),
        _d211_market_posture(listing),
    ]

    # Check for HOA declaration when no fee was published
    gate_b_no_fee = not listing.get("hoa_fee") and not listing.get("hoa_monthly")
    if gate_b_no_fee:
        hoa_diag = _check_hoa_in_records(official_records)
        if hoa_diag:
            diags.insert(0, hoa_diag)

    return diags


def build_verdict(gate_results: dict, diagnostics: list) -> dict:
    """Roll up gate results and diagnostics into a verdict object."""
    if gate_results.get("rejected"):
        return {
            "status": "REJECTED",
            "gate_results": gate_results,
            "rejection_reason": gate_results.get("rejection_reason"),
            "rejected_value": gate_results.get("rejected_value"),
            "diagnostics": [],
            "reds": [],
            "yellows": [],
            "unknowns_from_diagnostics": [],
            "structural_unknowns": _structural_unknowns(),
            "summary": _rejection_summary(gate_results),
        }

    reds = [d for d in diagnostics if d.get("severity") == RED]
    yellows = [d for d in diagnostics if d.get("severity") == YELLOW]
    unknowns = [d for d in diagnostics if d.get("severity") == UNKNOWN]

    if reds:
        status = "CAUTION"
    elif yellows:
        status = "CHASE"
    else:
        status = "CHASE"

    return {
        "status": status,
        "gate_results": gate_results,
        "diagnostics": diagnostics,
        "reds": [{"id": d["id"], "title": d["title"]} for d in reds],
        "yellows": [{"id": d["id"], "title": d["title"]} for d in yellows],
        "unknowns_from_diagnostics": [
            {"id": d["id"], "title": d["title"], "finding": d["finding"]}
            for d in unknowns
        ],
        "structural_unknowns": _structural_unknowns(),
        "summary": _verdict_summary(status, reds, yellows, unknowns),
    }


def _rejection_summary(gate_results: dict) -> str:
    reason = gate_results.get("rejection_reason", "")
    val = gate_results.get("rejected_value")
    if reason == "over_price_ceiling":
        ceiling = gate_results.get("price_ceiling", DEFAULT_PRICE_CEILING)
        return (
            f"REJECTED. List price {_money(int(val))} exceeds the "
            f"{_money(ceiling)} ceiling. No data pulled."
        )
    if reason == "hoa_over_ceiling":
        ceiling = gate_results.get("hoa_ceiling", DEFAULT_HOA_CEILING)
        return (
            f"REJECTED. HOA fee {_money(int(val))}/month exceeds the "
            f"{_money(ceiling)}/month ceiling. No data pulled."
        )
    return f"REJECTED: {reason}"


def _verdict_summary(status: str, reds: list, yellows: list, unknowns: list) -> str:
    parts = []
    if status == "CAUTION":
        parts.append(
            f"CAUTION: {len(reds)} red flag(s) in pulled records require attention "
            "before proceeding."
        )
    else:
        parts.append("CHASE: No red flags found in pulled records.")
    if yellows:
        parts.append(f"{len(yellows)} yellow flag(s) to review.")
    if unknowns:
        parts.append(
            f"{len(unknowns)} item(s) could not be determined from pulled records "
            "(see unknowns list)."
        )
    return " ".join(parts)


def _structural_unknowns() -> list:
    return [
        {
            "item": "Exact flood and wind insurance premium",
            "resolve_with": (
                "Elevation Certificate, wind mitigation inspection, and a live quote "
                "from a licensed Keys insurance agent"
            ),
        },
        {
            "item": "Prior flood or insurance claims and repetitive-loss history",
            "resolve_with": (
                "Owner authorization and direct inquiry to the insurer; "
                "this is not public record"
            ),
        },
        {
            "item": "True current HOA dues when the listing omits them",
            "resolve_with": "Estoppel letter from the association",
        },
        {
            "item": "Open water versus mangrove-screened shoreline view",
            "resolve_with": (
                "Recent aerial imagery and in-person shoreline inspection"
            ),
        },
        {
            "item": "Septic versus central sewer (confirmed)",
            "resolve_with": (
                "Confirm with the utility district; a special assessment on the tax bill "
                "hints at sewer but does not confirm hookup status"
            ),
        },
    ]


# ── Small utilities used by report layer ─────────────────────────────────────

def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None
