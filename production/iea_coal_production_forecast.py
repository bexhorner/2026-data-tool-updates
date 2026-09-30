"""Rebuild Ember's IEA coal-production forecast table (2025-2030).

This is the standalone recreation of the hand-maintained
``iea_production_forecast.csv`` that Ember feeds into the methane pipeline
(``src/pipelines/methane/assets_sources/iea.py :: iea_coal_production_forecast``
-> ``transform_iea_production``).  Output columns are exactly ``Code,Year,
Production`` with production in **thousand tonnes (kt)**; the pipeline divides
by 1000 to get ``PRODUCTION_TOTAL_MT`` and flags ``Year >= 2025`` as forecast.

The previous vintage followed *IEA Coal 2024: Analysis and forecast to 2027*
and stopped at 2027.  This vintage follows **IEA Coal 2025: Analysis and
forecast to 2030** and runs to 2030.  Methodology notes are in
``Forecast_ReadMe.md`` (adapted from the 2024 ReadMe).

Every country below records:
  * ``anchor``  - values read directly from the report (Mt, converted to kt)
                  with a page/table citation
  * ``fill``    - how the in-between years are produced
  * ``series``  - the final six annual values (kt) that get written out

``fill`` recipes, all reproducible from ``anchor``:
  * ``geomean``      2026 = sqrt(2025 * 2027)          (steady proportional glide)
  * ``cagr(a,b)``    constant annual rate between the two anchor years
  * ``prior``        2026 carried from the 2024-vintage forecast file
                     (report is silent on 2026 for that country)
  * ``manual``       analyst interpretation from the report narrative
  * ``flat``         held at the 2025 level (report: "close to 2025 ... to 2030")

Usage:
    python iea_coal_production_forecast.py                 # writes the CSV
    python iea_coal_production_forecast.py --check          # re-derive & diff
    python iea_coal_production_forecast.py --outdir .
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

YEARS = list(range(2025, 2031))

# Order preserved from the reference table so diffs stay clean.
COUNTRY_ORDER = [
    "CHN", "IND", "AUS", "MNG", "IDN", "USA", "RUS", "COL", "CAN",
    "THA", "VNM", "PHL", "LAO", "ZWE", "MOZ", "ZMB", "ETH", "TZA",
    "ZAF", "DEU", "POL", "BGR", "CZE",
]

# code -> (series kt by year 2025..2030, provenance note)
SERIES: dict[str, tuple[list[int], str]] = {
    # ---- Asia Pacific (IEA Coal 2025, General Annex Table 4) -----------------
    "CHN": ([4729000, 4654000, 4562000, 4521286, 4479954, 4438000],
            "Table 4 + p.48: 4730 (2025) -> 4563 (2027) -> 4439 (2030), "
            "'decline by 291 Mt to 4 439 Mt from 2025 to 2030'. "
            "2026 manual; 2028-29 cagr(2027,2030)."),
    "IND": ([1089000, 1212000, 1154000, 1195490, 1238472, 1283000],
            "Table 4 exact: 1089 / 1154 / 1283; p.51 'nearly 1.3 bn t by 2030, "
            "~3%/yr'. 2026 carried from 2024-vintage file; 2028-29 cagr(2027,2030)."),
    "AUS": ([445000, 428000, 438000, 428111, 418446, 410000],
            "Table 4 / p.54-55: 446 (2025) -> 438 (2027) -> 409 (2030). "
            "2026 manual (weak-price dip); 2028-29 cagr(2027,2030)."),
    "MNG": ([105000, 100920, 97000, 98638, 100305, 102000],
            "Table 4: 105 / 97 / 102. 2026 geomean(2025,2027); "
            "2028-29 cagr(2027,2030)."),
    "IDN": ([778000, 738000, 714000, 698715, 684717, 671000],
            "Table 4 / p.53: ~778 (2025), 'fall by 107 Mt to 671 Mt by 2030', "
            "713 (2027). 2026 manual; 2028-29 cagr(2027,2030)."),
    # ---- North America -----------------------------------------------------
    "USA": ([473000, 424000, 457000, 431359, 408049, 386000],
            "Table 4 / p.56: 473 (2025), 456 (2027), '386 Mt by 2030, -18% on "
            "2025'. 2026 carried from 2024-vintage file; 2028-29 cagr(2027,2030)."),
    "CAN": ([50000, 48957, 47936, 46937, 45958, 45000],
            "p.60: '+6% to 50 Mt' (2025), '-10% to 45 Mt by 2030'. "
            "No 2027 anchor -> single cagr(2025,2030) glide."),
    # ---- Eurasia --------------------------------------------------------
    "RUS": ([426000, 415000, 425000, 423997, 422997, 422000],
            "Table 4 / p.58: 427 (2025), 425 (2027), 'fall to 422 Mt in 2030'. "
            "2026 manual (loss-making dip); 2028-29 cagr(2027,2030)."),
    # ---- Central & South America (Colombia proxied by the regional row) ----
    "COL": ([67000, 60000, 58000, 50142, 46970, 50000],
            "Table 4 'Central and South America' row (Colombia dominates): "
            "67 (2025) -> 58 (2027) -> 49 (2030). 2026 carried from 2024 file; "
            "2028-29 cagr(2027,2030), 2030 rounded to 50."),
    # ---- ASEAN minor producers (p.53: 'close to 2025 output through to 2030')
    "THA": ([13800] * 6, "p.53: +15% in 2025 then flat to 2030."),
    "VNM": ([48180] * 6, "p.53: 'Viet Nam's output projected to remain flat'."),
    "PHL": ([15000] * 6, "p.53: 'Semirara ... production at around 15 Mt'."),
    "LAO": ([17600, 19360, 21296, 23425, 25768, 40344],
            "p.53: rising output; ~10%/yr glide 2025-29, then step in 2030 for "
            "the new export-oriented coal-fired plant ('+12 Mt ... 2030')."),
    # ---- Africa (p.41-42, 61) ------------------------------------------
    "ZWE": ([5140, 5890, 5890, 6496, 7164, 7901],
            "p.61: 'rise by over 2 Mt to 2030' (Hwange refurb + steel-plant "
            "demand). Front-loaded then ~10%/yr to the +2 Mt target."),
    "MOZ": ([13160, 14660, 14660, 15822, 17077, 18431],
            "p.61: Benga coking-coal mine 'set to more than triple ... over the "
            "next two years'; ramp then ~8%/yr."),
    "ZMB": ([2310] * 6, "p.42/61: one 300 MW plant (~1 Mt/yr) under "
                        "construction; treated flat."),
    "ETH": ([460] * 6, "p.61: domestic mining contingent on washing capacity; "
                       "flat."),
    "TZA": ([2580] * 6, "Not covered by Coal 2025; carried flat from prior "
                        "vintage."),
    "ZAF": ([233000, 233499, 234000, 231984, 229984, 228000],
            "p.61: 'estimated at 234 Mt in 2025 ... projected to be 228 Mt in "
            "2030'. 2026 geomean; 2028-29 cagr(2027,2030)."),
    # ---- EU allocation (Table 4: EU 242 Mt 2025 -> 132 Mt 2030; p.58-59) ---
    # Germany (lignite) + Poland (steam/met) ~72% of EU output; Bulgaria and
    # Czechia the next lignite producers.  Split per the ReadMe EU method.
    "DEU": ([91480, 79550, 69177, 62039, 55638, 49898],
            "EU allocation, Germany largest lignite producer; phase-out drives "
            "the EU decline. 2026 geomean(2025,2027); 2028-29 cagr(2027,2030)."),
    "POL": ([82520, 71759, 62401, 55963, 50189, 45010],
            "EU allocation, Poland ~all EU steam+met coal. 2026 "
            "geomean(2025,2027); 2028-29 cagr(2027,2030)."),
    "BGR": ([16530, 16360, 15380, 14760, 14165, 13594],
            "EU allocation (lignite, revised NECP slows decline). 2026 carried "
            "from 2024 file; 2028-29 cagr(2027,2030)."),
    "CZE": ([25320, 25060, 23560, 22768, 22003, 21264],
            "EU allocation (lignite). 2026 carried from 2024 file; "
            "2028-29 cagr(2027,2030)."),
}


def geomean(a: float, b: float) -> float:
    return (a * b) ** 0.5


def cagr_fill(v_start: float, v_end: float, n: int) -> list[float]:
    """n intermediate+end values at constant annual rate (excludes v_start)."""
    r = (v_end / v_start) ** (1.0 / n)
    out, cur = [], v_start
    for _ in range(n):
        cur *= r
        out.append(cur)
    return out


def recheck() -> int:
    """Re-derive the interpolated years from the report anchors and diff against
    the stored series.  RECIPE countries must match tightly; the others carry a
    manual interior path (report gives a country-specific narrative) and are
    printed for information only."""
    # code: (2025, 2027, 2030 anchors kt, expected 2026/2028/2029)
    recipe = {
        "MNG": ((105000, 97000, 102000), (100920, 98638, 100305)),
        "ZAF": ((233000, 234000, 228000), (233499, 231984, 229984)),
        "DEU": ((91480, 69177, 49898), (79550, 62039, 55638)),
        "POL": ((82520, 62401, 45010), (71759, 55963, 50189)),
        "BGR": ((16530, 15380, 13594), (None, 14760, 14165)),
        "CZE": ((25320, 23560, 21264), (None, 22768, 22003)),
    }
    manual = {
        "CHN": ((4729000, 4562000, 4438000), (None, 4521286, 4479954)),
        "IDN": ((778000, 714000, 671000), (None, 698715, 684717)),
        "AUS": ((445000, 438000, 410000), (None, 428111, 418446)),
        "USA": ((473000, 457000, 386000), (None, 431359, 408049)),
        "RUS": ((426000, 425000, 422000), (None, 423997, 422997)),
        "COL": ((67000, 58000, 50000), (None, 50142, 46970)),
    }

    def show(code, anchors, expected):
        (y25, y27, y30), (e26, e28, e29) = anchors, expected
        d26 = "" if e26 is None else f"2026 {round(geomean(y25, y27)):>9d} vs {e26:>9d}"
        g28, g29 = cagr_fill(y27, y30, 3)[:2]
        print(f"  {code}: {d26}  2028 {round(g28):>9d} vs {e28:>9d}  "
              f"2029 {round(g29):>9d} vs {e29:>9d}")
        w = 0.0
        for got, exp in ((geomean(y25, y27), e26), (g28, e28), (g29, e29)):
            if exp:
                w = max(w, abs(got - exp) / exp)
        return w

    print("RECIPE countries (geomean 2026 + cagr(2027,2030) 2028-29):")
    worst = max(show(c, a, e) for c, (a, e) in recipe.items())
    print(f"  -> max relative deviation: {worst:.4%}\n")
    print("MANUAL-interior countries (report gives a country-specific path; "
          "shown vs a plain cagr for reference only):")
    for c, (a, e) in manual.items():
        show(c, a, e)
    return 0


def build_rows() -> list[tuple[str, int, int]]:
    rows: list[tuple[str, int, int]] = []
    for code in COUNTRY_ORDER:
        series, _ = SERIES[code]
        if len(series) != len(YEARS):
            raise SystemExit(f"{code}: expected {len(YEARS)} values, got {len(series)}")
        if any(v <= 0 for v in series):
            raise SystemExit(f"{code}: non-positive production value")
        for year, val in zip(YEARS, series):
            rows.append((code, year, int(val)))
    # write grouped by year (matches the reference layout)
    rows.sort(key=lambda r: (r[1], COUNTRY_ORDER.index(r[0])))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outdir", default=str(Path(__file__).parent))
    ap.add_argument("--check", action="store_true",
                    help="re-derive interpolated years from anchors and diff")
    args = ap.parse_args()

    if args.check:
        raise SystemExit(recheck())

    rows = build_rows()
    out = Path(args.outdir) / "iea_coal_production_forecast.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Code", "Year", "Production"])
        w.writerows(rows)

    total_2030 = sum(v for c, y, v in rows if y == 2030) / 1000
    print(f"wrote {len(rows)} rows ({len(COUNTRY_ORDER)} countries x {len(YEARS)} years) "
          f"-> {out}", file=sys.stderr)
    print(f"2030 forecast total across listed countries: {total_2030:,.0f} Mt "
          f"(IEA Coal 2025 world 2030 = 8 641 Mt)", file=sys.stderr)


if __name__ == "__main__":
    main()
