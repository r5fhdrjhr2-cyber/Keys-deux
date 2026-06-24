from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Provenance:
    source_name: str
    source_url: str
    retrieved_at: str  # ISO 8601
    cache_path: str


@dataclass
class SalesRecord:
    date: Optional[str]
    price: Optional[int]
    or_book: Optional[str]
    or_page: Optional[str]
    instrument_number: Optional[str]
    grantor: Optional[str]
    grantee: Optional[str]
    sale_qualification: Optional[str]
    deed_type: Optional[str]
    provenance: Optional[Provenance] = None


@dataclass
class ValuationYear:
    year: int
    just_value: Optional[int]
    assessed_value: Optional[int]
    taxable_value: Optional[int]
    school_taxable: Optional[int]
    land_value: Optional[int]
    building_value: Optional[int]
    homestead_exemption: bool
    homestead_amount: Optional[int]
    soh_cap_differential: Optional[int]


@dataclass
class ImprovementDetail:
    description: str
    year_built: Optional[int]
    effective_year: Optional[int]
    living_area: Optional[int]
    gross_area: Optional[int]
    stories: Optional[float]
    construction_type: Optional[str]
    roof_type: Optional[str]
    beds: Optional[float]
    baths: Optional[float]
    assessed_value: Optional[int]
    permit_numbers: list = field(default_factory=list)


@dataclass
class AppraiserRecord:
    parcel_id: Optional[str]
    re_number: Optional[str]
    alternate_key: Optional[str]
    folio: Optional[str]
    owner_names: list = field(default_factory=list)
    mailing_address: Optional[str] = None
    situs_address: Optional[str] = None
    jurisdiction: Optional[str] = None
    legal_description: Optional[str] = None
    subdivision: Optional[str] = None
    block: Optional[str] = None
    lot: Optional[str] = None
    property_use_code: Optional[str] = None
    lot_size_sqft: Optional[float] = None
    lot_size_acres: Optional[float] = None
    zoning: Optional[str] = None
    land_use: Optional[str] = None
    sketch_url: Optional[str] = None
    photo_url: Optional[str] = None
    sketch_cache_path: Optional[str] = None
    photo_cache_path: Optional[str] = None
    improvements: list = field(default_factory=list)
    valuation_history: list = field(default_factory=list)
    sales_history: list = field(default_factory=list)
    permit_cross_refs: list = field(default_factory=list)
    trim_estimate: Optional[float] = None
    provenance: Optional[Provenance] = None


@dataclass
class TaxYear:
    year: int
    bill_number: Optional[str]
    gross_tax: Optional[float]
    taxable_value: Optional[int]
    millage: Optional[float]
    amount_paid: Optional[float]
    date_paid: Optional[str]
    discount_taken: Optional[float]
    status: Optional[str]  # paid, unpaid, partial
    non_ad_valorem: list = field(default_factory=list)


@dataclass
class TaxCollectorRecord:
    account_number: Optional[str]
    tax_years: list = field(default_factory=list)
    delinquent: bool = False
    delinquent_years: list = field(default_factory=list)
    tax_certificates: list = field(default_factory=list)
    tax_deed_status: Optional[str] = None
    escrow_code: Optional[str] = None
    provenance: Optional[Provenance] = None


@dataclass
class OfficialRecordInstrument:
    recording_date: Optional[str]
    instrument_type: Optional[str]
    book: Optional[str]
    page: Optional[str]
    instrument_number: Optional[str]
    grantor: Optional[str]
    grantee: Optional[str]
    consideration: Optional[float]
    doc_stamps: Optional[float]
    legal_description: Optional[str]
    document_url: Optional[str]
    document_cache_path: Optional[str]
    tags: list = field(default_factory=list)  # e.g. "open_mortgage", "lis_pendens", "lien"
    provenance: Optional[Provenance] = None


@dataclass
class CourtCase:
    case_number: str
    filing_date: Optional[str]
    case_type: Optional[str]
    parties: list = field(default_factory=list)
    status: Optional[str] = None
    docket_events: list = field(default_factory=list)
    provenance: Optional[Provenance] = None


@dataclass
class PermitRecord:
    jurisdiction: str
    source_system: str
    permit_number: str
    permit_type: Optional[str]
    subtype: Optional[str]
    description: Optional[str]
    status: Optional[str]  # applied, issued, active, expired, finaled, void, withdrawn
    applied_date: Optional[str]
    issued_date: Optional[str]
    finaled_date: Optional[str]
    expiration_date: Optional[str]
    contractor_name: Optional[str]
    contractor_license: Optional[str]
    declared_value: Optional[float]
    inspections: list = field(default_factory=list)
    provenance: Optional[Provenance] = None


@dataclass
class FloodRecord:
    zone: Optional[str]
    base_flood_elevation: Optional[float]
    firm_panel: Optional[str]
    pre_firm: bool
    sfha: Optional[bool]
    v_datum: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    provenance: Optional[Provenance] = None


@dataclass
class ManualRetrievalRequired:
    source: str
    reason: str
    contact: str
    url: Optional[str] = None


@dataclass
class ParcelRecord:
    parcel_id: str
    re_number: Optional[str]
    address: dict  # full, street, city, state, zip
    jurisdiction: Optional[str]
    owner_names: list
    prior_owner_names: list
    appraiser: Optional[AppraiserRecord] = None
    tax_collector: Optional[TaxCollectorRecord] = None
    official_records: list = field(default_factory=list)
    court_cases: list = field(default_factory=list)
    permits: list = field(default_factory=list)
    flood: Optional[FloodRecord] = None
    manual_retrievals_required: list = field(default_factory=list)
    run_errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
