#!/usr/bin/env python3
"""
methane_targets.py - attach an absolute CO2e value (20-yr and 100-yr GWP) to
each country's methane reduction target, using Ember's coal-mining CH4
(1.B.1.a) series as the baseline.

SOURCES
    1. "Methane Reduction Targets by Country" Google Sheet (hand-maintained):
       https://docs.google.com/spreadsheets/d/1KdFKrbCsKYi2i5tYDZ31EjacfS-cGiXBoDO1fsqoGxk
       Columns: Country, Methane Target Type, Value (%), From (baseline),
       To (target year), Notes, Source. Fetched unauthenticated via Sheets'
       CSV export, so the sheet must be link-viewable.
    2. ../emissions/crt_methane_ch4_gapfilled.csv (crt_methane_gapfill.py's
       output) - coal-mining CH4 only, one row per COUNTRY_CODE x YEAR, with
       EMISSIONS_CO2E_20YR_KT / EMISSIONS_CO2E_100YR_KT (IPCC AR6 fossil-CH4
       GWP already applied).

METHOD
    target_<x> = baseline_year_EMISSIONS_<X> * (1 - Value), for x in
    {CH4_KT, CO2E_20YR_KT, CO2E_100YR_KT}, where the baseline comes from the
    gapfilled series at COUNTRY_CODE = the sheet's Country (name -> ISO3 via
    pycountry) and YEAR = the sheet's "From (baseline)". Left blank when the
    sheet gives no baseline year or the gapfilled series has no row for it.

OUTPUT (methane_targets.csv):
    country, target_year, target_ch4_kt, target_co2e_20yr_kt,
    target_co2e_100yr_kt, note, source
    target_year - the sheet's "To (target year)", verbatim.
    note        - the sheet's own Notes column, verbatim.
    source      - the sheet's Source URL, verbatim.

USAGE
    pip install pandas requests pycountry
    python3 methane_targets.py
    python3 methane_targets.py --sheet <url|id> --gapfilled <path> --out <path>
"""
from __future__ import annotations
import argparse
import io
import re
import sys

import pandas as pd
import requests

DEFAULT_SHEET = "1KdFKrbCsKYi2i5tYDZ31EjacfS-cGiXBoDO1fsqoGxk"
DEFAULT_GAPFILLED = "../emissions/crt_methane_ch4_gapfilled.csv"
DEFAULT_OUT = "methane_targets.csv"

MANUAL_NAME_TO_ISO3 = {
    "Iran (Islamic Republic of)": "IRN",
    "Republic of Korea": "KOR",
    "Korea": "KOR",
    "South Korea": "KOR",
    "Brunei": "BRN",
    "Russia": "RUS",
    "State of Palestine": "PSE",
    "Kosovo": "XKX",
    "Viet Nam": "VNM",
    "Vietnam": "VNM",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def country_name_to_code(country: str) -> str | None:
    if country in MANUAL_NAME_TO_ISO3:
        return MANUAL_NAME_TO_ISO3[country]
    try:
        import pycountry

        return pycountry.countries.lookup(country).alpha_3
    except LookupError:
        return None


def _resolve_sheet_id(sheet: str) -> str:
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", sheet)
    return m.group(1) if m else sheet.strip()


def fetch_targets_sheet(sheet: str) -> pd.DataFrame:
    """First/only tab of the targets sheet, fetched via the unauthenticated
    CSV export endpoint (same approach crt_methane.py uses for its
    hand-maintained non-Annex sheet, via gviz instead)."""
    doc = _resolve_sheet_id(sheet)
    url = f"https://docs.google.com/spreadsheets/d/{doc}/export?format=csv"
    log("fetching the methane targets sheet ...")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    if resp.text.lstrip().lower().startswith(("<!doctype", "<html")):
        sys.exit(
            f"the sheet returned HTML, not CSV - {doc} is probably not shared "
            '"anyone with the link can view".'
        )
    return pd.read_csv(io.StringIO(resp.text))


def load_gapfilled(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    return df[
        [
            "COUNTRY_CODE",
            "YEAR",
            "EMISSIONS_CH4_KT",
            "EMISSIONS_CO2E_20YR_KT",
            "EMISSIONS_CO2E_100YR_KT",
        ]
    ]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--sheet", default=DEFAULT_SHEET)
    ap.add_argument("--gapfilled", default=DEFAULT_GAPFILLED)
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args()

    targets = fetch_targets_sheet(a.sheet)
    gapfilled = load_gapfilled(a.gapfilled)
    by_country_year = {
        (r.COUNTRY_CODE, int(r.YEAR)): (
            r.EMISSIONS_CH4_KT,
            r.EMISSIONS_CO2E_20YR_KT,
            r.EMISSIONS_CO2E_100YR_KT,
        )
        for r in gapfilled.itertuples()
    }

    rows = []
    for r in targets.itertuples():
        country = r.Country
        code = country_name_to_code(country)
        pct = float(str(getattr(r, "Value")).rstrip("%")) / 100
        baseline_year = getattr(r, "_4")  # "From (baseline)"
        baseline_year = None if pd.isna(baseline_year) else int(baseline_year)
        target_year = getattr(r, "_5")  # "To (target year)"
        target_year = None if pd.isna(target_year) else int(target_year)

        baseline = None
        if code and baseline_year is not None:
            baseline = by_country_year.get((code, baseline_year))
            if baseline is None:
                log(f"  no {code} {baseline_year} row in the gapfilled series - "
                    f"{country} left blank")
        elif baseline_year is None:
            log(f"  no baseline year given for {country} - left blank")
        elif code is None:
            log(f"  could not resolve '{country}' to an ISO3 code - left blank")

        target_ch4, target_20, target_100 = (None, None, None)
        if baseline is not None:
            base_ch4, base_20, base_100 = baseline
            target_ch4 = round(base_ch4 * (1 - pct), 1)
            target_20 = round(base_20 * (1 - pct), 1)
            target_100 = round(base_100 * (1 - pct), 1)

        rows.append(
            {
                "country": country,
                "target_year": target_year,
                "target_ch4_kt": target_ch4,
                "target_co2e_20yr_kt": target_20,
                "target_co2e_100yr_kt": target_100,
                "note": getattr(r, "Notes"),
                "source": getattr(r, "Source"),
            }
        )

    out = pd.DataFrame(
        rows,
        columns=[
            "country",
            "target_year",
            "target_ch4_kt",
            "target_co2e_20yr_kt",
            "target_co2e_100yr_kt",
            "note",
            "source",
        ],
    )
    out.to_csv(a.out, index=False, encoding="utf-8")
    log(f"-> {a.out} ({len(out)} rows, "
        f"{out['target_ch4_kt'].notna().sum()} with a computed value)")


if __name__ == "__main__":
    main()
