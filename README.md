# Florida Keys Property Due Diligence Tool

A two-layer Python tool for buyers researching single-family properties in the
Florida Keys. Given an MLS number or street address, it retrieves raw public
records from every applicable Monroe County government system, then analyzes
the data and writes a Word report with a verdict.

This tool is for individual due diligence on properties you are actively
researching. It is not a bulk crawling tool and will not let you run more
than five parcels per session (configurable).

---

## Two-layer design

**Retrieval layer** (`keys_records/`) fetches, caches, and stores raw public
records with full provenance. It interprets nothing. Every record includes the
source name, URL, retrieval timestamp, and path to the raw snapshot on disk.

**Analysis layer** (`keys_analysis.py`, `keys_report.py`) reads the cached
records, runs eleven diagnostics, rolls up a verdict, and writes a Word report.
It makes no network calls except through the retrieval layer.

---

## Sources and coverage

| Source | System | Jurisdiction |
|--------|--------|-------------|
| Property Appraiser (primary) | Florida DOR Statewide Cadastral, ArcGIS REST (JSON over HTTP) | All Monroe County parcels |
| Property Appraiser (fallback) | qPublic AppID 605 (schneidercorp.com) | All Monroe County parcels |
| Tax Collector | monroetaxcollector.com, monroecounty-fl.gov | All Monroe County parcels |
| Clerk -- Official Records | monroe-clerk.com | All Monroe County |
| Clerk -- Civil Cases | monroe-clerk.com | All Monroe County |
| Permits -- legacy (before 2022-10-01) | MCeSearch | Unincorporated Monroe County |
| Permits -- current (after 2022-10-01) | Oracle OPAL | Unincorporated Monroe County |
| Permits | eTRAKiT | City of Key West |
| Permits | ViewPointCloud | City of Marathon |
| Permits | CityView (discovered dynamically) | Village of Islamorada |
| Permits | Manual retrieval required | City of Key Colony Beach |
| Permits | Manual retrieval required | City of Layton |
| Flood zone | FEMA NFHL ArcGIS REST API | All parcels |

Key Colony Beach and Layton have no online permit portal. The tool emits a
manual retrieval notice with contact information rather than silently returning
empty.

---

## Polite client behavior

All network access goes through a single `PoliteClient` class that behaves like
one careful human researcher, not a crawler.

- Concurrency is exactly 1. One request in flight at any moment.
- Between navigations to the same domain: 4 to 9 seconds, jittered.
- After page load: dwell 2 to 5 seconds before reading or clicking.
- Search box input: character by character, 80 to 220 ms per key. No paste.
- Hard per-domain cap: no more than 6 page loads per minute.
- Every 8 to 12 actions: rest 20 to 45 seconds.
- All delays are drawn from a jittered distribution, never fixed constants.

On CAPTCHA or block: stop that domain, record the event, emit a manual
retrieval notice. Do not attempt to solve CAPTCHAs. Do not use a solving
service.

After 3 consecutive failures on a domain: open the circuit for the rest of the
run and record it.

The tool fetches and respects robots.txt per domain. qPublic (schneidercorp.com)
restricts automated access; the tool respects this by using the slowest pacing
tier, enforcing the per-run parcel cap, and recording the restriction in the log.

Session state (cookies, storage) is persisted per domain so that disclaimer
gates (qPublic, Clerk) are only accepted once and reused on subsequent runs.

Every navigation is logged to `audit.jsonl` with timestamp, domain, URL, status,
delay used, and cache hit or miss.

---

## Setup and install

Requirements: Python 3.11 or later.

```
pip install -r requirements.txt
pip install -r requirements_records.txt
playwright install chromium
```

For the virtual display (recommended on Linux; allows headful Chromium that
avoids headless detection):

```
sudo apt-get install xvfb
```

Optional environment variables:

```
FLKEYS_RESO_TOKEN   -- RESO Web API bearer token (Tier 1 MLS data, optional)
FLKEYS_RESO_URL     -- RESO Web API base URL (Tier 1 MLS data, optional)
SEARCH_API_KEY      -- Serper, Brave, or Bing search API key (Tier 2 listing data)
SEARCH_PROVIDER     -- serper | brave | bing (default: serper)
```

Without a `SEARCH_API_KEY`, Tier 2 web search is disabled and listing data
comes from RESO only. The retrieval and analysis layers do not require a search
key.

---

## Configuration

All configurable values live in `keys_records/config.yaml`.

```yaml
ceilings:
  list_price: 1800000     # Gate A: reject if list price exceeds this
  hoa_monthly: 2000       # Gate B: reject if HOA exceeds this per month

pacing:
  nav_delay_min: 4.0      # seconds between same-domain navigations (lower bound)
  nav_delay_max: 9.0      # seconds between same-domain navigations (upper bound)
  dwell_min: 2.0          # dwell after page load before reading or clicking
  dwell_max: 5.0
  key_delay_min: 0.08     # per-key delay when typing into search boxes
  key_delay_max: 0.22
  rate_cap_per_minute: 6  # hard cap: page loads per minute per domain
  long_rest_every_min: 8  # rest every N to M actions
  long_rest_every_max: 12
  long_rest_min: 20.0     # rest duration (seconds)
  long_rest_max: 45.0

backoff:
  initial_seconds: 120    # first backoff on block or 429
  multiplier: 2.0         # exponential multiplier
  max_seconds: 480        # cap
  circuit_break_after: 3  # consecutive failures before circuit opens

cache:
  root: ./cache
  freshness_days: 7       # serve from cache if snapshot is younger than this

sessions:
  root: ./sessions        # persisted browser session state per domain

run:
  parcel_cap: 5           # max parcels per run session

robots:
  restricted_domains:
    - schneidercorp.com
  restricted_pacing_multiplier: 2.0
```

---

## How to run

With a street address:

```
python keys_dd.py --address "6501 Oceanview Ave, Marathon, FL 33050"
```

With an MLS number:

```
python keys_dd.py --mls 619378
```

With both (address improves resolution if MLS data is thin):

```
python keys_dd.py --mls 619378 --address "6501 Oceanview Ave, Marathon, FL 33050"
```

With a parcel ID (skips the appraiser search step):

```
python keys_dd.py --parcel 00123456-000100 --address "6501 Oceanview Ave, Marathon, FL 33050"
```

Run the validation harness:

```
python keys_dd.py --validate
```

---

## Outputs

All outputs land in `./output/` (override with `--output-dir`).

```
output/
  619378_2025-01-15.json    -- verdict + all diagnostic data
  619378_2025-01-15.docx    -- Word report (requires python-docx)
```

Raw snapshots, downloaded PDFs, and cached HTML are stored under `./cache/`.

The JSON file contains the full verdict object including gate results, all 11
diagnostics with severity and supporting fields, unknowns, and estimates.

The Word report has two parts:

**Part One -- Data Pulled**: property snapshot, sales history, valuation and
tax history, permit table (all jurisdictions), recorded instruments table,
court cases, flood zone, and a sources-reached summary.

**Part Two -- Synthesis**: leads with reasons not to buy and open unknowns.
Every red and yellow flag names the exact field it rests on. Estimates are
labeled as estimates. Verdict and next step at the end.

---

## The two hard gates

Gate A -- list price: if the listing price exceeds $1,800,000, the tool stops
immediately with a rejection notice. It does not pull any records and does not
build a Word report. The threshold is in `config.yaml` under `ceilings.list_price`.

Gate B -- HOA fee: if the listing publishes a recurring HOA or condo fee over
$2,000 per month, the tool stops with a rejection notice. If no fee is published
but official records contain a recorded HOA or condo declaration, the tool
continues but flags YELLOW "association exists, dues unknown, obtain estoppel."
Unknown dues are always yellow, never green. The threshold is in `config.yaml`
under `ceilings.hoa_monthly`.

If either gate fires, a small JSON stub is written and the tool prints one clear
line. No Word report is built.

---

## Diagnostics

The analysis layer runs eleven diagnostics. Each returns a severity (red, yellow,
info, or unknown) and names the exact fields consulted.

| ID | Diagnostic |
|----|-----------|
| 2.1 | Flood zone and insurance band |
| 2.2 | Pre-Irma vs post-Irma rebuild |
| 2.3 | Short-term rental eligibility |
| 2.4 | Mangrove or view obstruction |
| 2.5 | Tax shock on resale |
| 2.6 | Distress signals (lis pendens, liens, judgments) |
| 2.7 | Open or expired permits and unpermitted work |
| 2.8 | Recorded non-conversion agreement |
| 2.9 | Special assessments (sewer, stormwater) |
| 2.10 | Roof age |
| 2.11 | Market posture (days on market, price cuts) |

Any red diagnostic produces a CAUTION verdict. No reds produces a CHASE verdict.
The tool does not produce a numeric score.

---

## The honesty rule

Absence of data is reported as absence, never as a pass. If a field is not
returned from any source, the diagnostic says "unknown, not in any source we
pull" and names the exact field path and the input that would resolve it.

A diagnostic that says "no liens found" means no liens were found in the records
that were successfully retrieved. It does not mean there are no liens.

---

## Known limitations and structural unknowns

The following items cannot be determined from public records alone and are always
listed in the unknowns section of every report:

- **Exact flood and wind insurance premium**: requires the Elevation Certificate,
  a wind mitigation inspection, and a live quote from a licensed Keys agent.
  NFIP building coverage caps at $250,000; a private flood layer is needed above
  that.
- **Prior flood claims and repetitive-loss history**: not public record. Requires
  owner authorization and direct inquiry to the insurer.
- **True HOA dues when listing omits them**: requires an estoppel letter from the
  association.
- **Open water versus mangrove-screened shoreline view**: requires aerial imagery
  and in-person inspection. The mangrove fringe is common on Keys oceanfront lots
  and legally protected.
- **Septic versus central sewer confirmed**: a special assessment on the tax bill
  hints at sewer but does not confirm hookup status. Confirm with the utility
  district.

Monroe County Property Appraiser (mcpafl.org / qPublic) uses Cloudflare
protection that blocks automated browser sessions. Rather than fight it, the
tool reads the same property roll (owner, just/assessed/taxable value, year
built, living area, legal description, land use, lot size, homestead) from the
Florida Department of Revenue Statewide Cadastral, an open ArcGIS REST service
that answers plain JSON requests with no browser and no CAPTCHA. It resolves by
parcel ID, or by street address via a spatial query when no parcel ID is known
(so `--mls N --address "..."` works even for listings that never expose a tax
number). The Cloudflare-protected qPublic site is used only as a fallback. The
tax *collector* bill/payment/delinquency portal remains behind Cloudflare; when
it cannot be reached the tool emits a manual-retrieval notice (the assessed and
taxable *values* still come from the cadastral above).

---

## Disclaimer

This tool is for individual due diligence on properties you are actively
considering. It is not designed for bulk data collection and enforces a per-run
parcel cap of 5 to reinforce that limit.

Flood band figures are illustrative ranges only. Actual premiums depend on the
Elevation Certificate, wind mitigation inspection, insurer, and coverage terms.
First-year tax estimates are based on appraiser values and may differ from actual
bills. Nothing in this tool or its output is legal, insurance, or financial
advice. Verify all material facts with licensed professionals before making any
decision.
