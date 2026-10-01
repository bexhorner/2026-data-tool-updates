#!/usr/bin/env python3
"""
crt_methane_gapfill.py - gap-fill the coal-mining CH4 (1.B.1.a) series from
crt_methane.py using a coal-production emission factor.

This is the standalone equivalent of ember-data-processing's

    src/pipelines/methane/transformations_curated/production_emissions.py
        :: transform_coal_emissions_unfccc

restricted to the UNFCCC half (no IEA / GEM branches). The method, verbatim
from that function:

  1. join reported emissions (crt_methane_ch4.csv) to coal production
     (coal_production_combined.csv) on (COUNTRY_CODE, YEAR);
  2. per country-year with both values, an emission factor
        INTENSITY_CH4_KT_PER_MT = EMISSIONS_CH4_KT / PRODUCTION_MT
     (null where production == 0);
  3. forward-fill then backward-fill that factor within each country, ordered
     by year, so every year gets a factor;
  4. EMISSIONS_CH4_KT_ESTIMATED = PRODUCTION_MT * factor;
  5. where a year has no reported emission, use the estimate. Label it
     "Estimate" if the year is <= the country's last reported production year,
     else "Forecast"; reported years stay "Report".

  Where coal production is 0, emissions are 0 - but the cell is left blank
  (null), not written as a fabricated 0. Those country-years carry no reported
  figure, so they simply fall out of the gap-filled series. This departs from
  the pipeline's ZERO_PRODUCTION_FLAG branch, which writes an explicit 0.
  Genuine reported zeros (EMISSIONS_CH4_KT == 0 in the input) are kept as-is.

PRE-CLEAN (applied to the reported emissions BEFORE the factor is computed, so
these values neither survive in the output nor distort a country's factor):

  * Nigeria     1994-2016  removed   (see REMOVALS below)
  * Afghanistan 1991-2004  removed

Input schema (crt_methane_ch4.csv, from crt_methane.py --non-annex-sheet):
    COUNTRY_CODE, YEAR, EMISSIONS_CH4_KT, SOURCE, FILENAME, ANNEX_FLAG
Input schema (coal_production_combined.csv, from coal_production_combined.py):
    YEAR, COUNTRY_CODE, PRODUCTION_METALLURGICAL_MT, PRODUCTION_TOTAL_MT,
    PRODUCTION_OTHER_MT, FORECAST_FLAG, SOURCE

Output (crt_methane_ch4_gapfilled.csv), one row per country-year on the
gap-filled series:
    COUNTRY_CODE, YEAR, EMISSIONS_CH4_KT, EMISSIONS_TYPE,
    EMISSIONS_ESTIMATED_FLAG, SOURCE_EMISSIONS, SOURCE_PRODUCTION,
    SOURCE_INTENSITY, SOURCE_ALL, ANNEX_FLAG

SOURCE_ALL follows transform_mart_coal_emissions_all's SOURCE_ALL:
    "UNFCCC"      raw UNFCCC submission (reported figure passed through)
    "EIA-UNFCCC"  gap-filled: EIA reported coal production x UNFCCC factor
    "IEA-UNFCCC"  gap-filled: IEA coal production forecast x UNFCCC factor

EXTRA 2025 SOURCES (one aggregated row per country, appended alongside the
above - a country-year can now carry several rows, told apart by SOURCE):
    "GEM"        Global Coal Mine Tracker: per-mine CH4 estimate for operating
                 mines, summed by country, Mt -> kt. (transform_gem_country)
    "IEA"        IEA 2026 Google Sheet, 'Data' tab (fetched live via gviz CSV):
                 coal segments (steam / coking / other from coal) summed by
                 country. "Other from coal" is a residual bucket and is dropped
                 for any country with no steam- or coking-coal emissions of
                 its own.

EXTRA-SOURCE FORECAST (2026-2030, see forecast_extra_source) - ported from
ember-data-processing's transform_coal_emissions_iea / _gem, restricted to
their forecast branch:
    for each extra source, derive a constant per-country CH4-per-tonne
    intensity (EMISSIONS_CH4_KT / PRODUCTION_MT), then scale the coal
    production mart's forecast years (FORECAST_FLAG=true, i.e. 2026-2030 -
    IEA is the only production SOURCE with a forecast) by that factor:
      "IEA"  intensity benchmarked at EXTRA_SOURCE_YEAR (2025) production.
      "GEM"  intensity benchmarked at each country's latest *reported*
             (non-forecast) production year, same as the UNFCCC gap-fill's
             own emission factor.
    SOURCE_ALL stays "IEA"/"GEM" (no new label); EMISSIONS_TYPE is "Forecast"
    and SOURCE_PRODUCTION/SOURCE_INTENSITY record how the figure was derived.
    Only countries with usable benchmark production get a forecast -
    production.csv's IEA forecast covers 24 countries, so a GEM country
    outside that set keeps just its single EXTRA_SOURCE_YEAR row.

WORLD ROLLUP (COUNTRY_CODE="WLD", one set of rows per YEAR, see add_world_rows):
    SOURCE_ALL="EIA-UNFCCC"  sum of EMISSIONS_CH4_KT across every country for
                        that year, EXCLUDING the standalone GEM/IEA extras
                        below - i.e. the same "reported + estimated" series
                        Ember's public chart sums (UNFCCC / EIA-UNFCCC /
                        IEA-UNFCCC together, labelled by the dominant method).
    SOURCE_ALL="GEM"    sum of that year's GEM extra rows (EXTRA_SOURCE_YEAR only).
    SOURCE_ALL="IEA"    sum of that year's IEA extra rows (EXTRA_SOURCE_YEAR only).
    Pass --no-world to skip these.

Usage:
    python crt_methane_gapfill.py
    python crt_methane_gapfill.py --emissions crt_methane_ch4.csv \
        --production coal_production_combined.csv --out crt_methane_ch4_gapfilled.csv
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

import polars as pl

# --- extra 2025 emission sources (aggregated to one row per country) ----------
GEM_SHEET = "Non-closed mines"
GEM_STATUS = "Operating"  # transform_gem_country keeps only operating mines
GEM_EST_COL = "GEM Coal Mine Methane Emissions Estimate (M tonnes/yr)"
GEM_COUNTRY_COL = "Country / Area"
GEM_FILENAME = "GEM Global Coal Mine Tracker (May 2026)"

# IEA 2026 (Greenhouse gas emissions from energy) - hand-maintained Google
# Sheet, "Data" tab. Fetched live (unauthenticated gviz CSV endpoint, so the
# sheet must be link-viewable), same pattern as crt_methane.py's
# load_non_annex_sheet.
IEA_SHEET_ID = "1F5JFoJxPxDOZy4oRC7mdbF1IvthtJRZ6zOw5oBly6js"
IEA_TAB = "Data"
IEA_FILENAME = "IEA 2026 (Greenhouse gas emissions from energy)"
IEA_CORE_COAL_SEGMENTS = ("steam coal", "coking coal")
IEA_OTHER_COAL_SEGMENT = "other from coal"
EXTRA_SOURCE_YEAR = 2025
FORECAST_END_YEAR = 2030

# (COUNTRY_CODE, first_year, last_year) - reported emissions inside the closed
# interval are dropped before the emission factor is derived. Both ranges are
# known-bad stretches of the hand-maintained non-Annex sheet: Nigeria's 1994
# figure (136 kt) is ~40x the 2017+ level and is the country's only pre-2017
# point; Afghanistan's 1991-2004 stretch predates any usable production-linked
# report. Downstream, these years are re-estimated from production x factor.
REMOVALS: list[tuple[str, int, int]] = [
    ("NGA", 1994, 2016),
    ("AFG", 1991, 2004),
]


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def load_emissions(path: Path) -> pl.DataFrame:
    df = pl.read_csv(path)
    need = {"COUNTRY_CODE", "YEAR", "EMISSIONS_CH4_KT"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"{path.name}: missing columns {sorted(missing)}")
    if "ANNEX_FLAG" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Boolean).alias("ANNEX_FLAG"))
    if "FILENAME" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias("FILENAME"))
    if "SOURCE" not in df.columns:
        df = df.with_columns(pl.lit("1.B.1.a Coal mining and handling").alias("SOURCE"))

    df = df.select(
        pl.col("COUNTRY_CODE").cast(pl.Utf8),
        pl.col("YEAR").cast(pl.Int64),
        pl.col("EMISSIONS_CH4_KT").cast(pl.Float64),
        pl.col("SOURCE").cast(pl.Utf8),
        pl.col("FILENAME").cast(pl.Utf8),
        pl.col("ANNEX_FLAG").cast(pl.Boolean),
    )

    # crt_methane.py can emit two rows for one country-year when a country
    # appears in both a CRT round and the non-Annex manual sheet (e.g. BRA/ARG/
    # MEX/GEO/SRB/TJK 2022). transform_coal_emissions_unfccc joins on
    # (YEAR, COUNTRY_CODE), so collapse first. Keep the non-Annex-sheet row when
    # present (ANNEX_FLAG False sorts first) - its ANNEX_FLAG is the correct one
    # for those parties; values differ by <0.1%.
    before = df.height
    df = df.sort(["COUNTRY_CODE", "YEAR", "ANNEX_FLAG", "FILENAME"]).unique(
        subset=["COUNTRY_CODE", "YEAR"], keep="first", maintain_order=True
    )
    if df.height != before:
        log(f"  collapsed {before - df.height} duplicate (country, year) emission row(s)")
    return df


def load_production(path: Path) -> pl.DataFrame:
    df = pl.read_csv(path)
    need = {"COUNTRY_CODE", "YEAR", "PRODUCTION_TOTAL_MT", "FORECAST_FLAG", "SOURCE"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"{path.name}: missing columns {sorted(missing)}")
    # coal_production_combined.py's own COUNTRY_CODE="WLD"/SOURCE="WORLD" rollup
    # isn't a real country - this script derives its own WLD rows (add_world_rows)
    # from the per-country factor estimates, so drop the input one before the join.
    df = df.filter(pl.col("SOURCE") != "WORLD")
    df = df.select(
        pl.col("COUNTRY_CODE").cast(pl.Utf8),
        pl.col("YEAR").cast(pl.Int64),
        pl.col("PRODUCTION_TOTAL_MT").cast(pl.Float64).alias("PRODUCTION_MT"),
        pl.col("FORECAST_FLAG").cast(pl.Boolean),
        pl.col("SOURCE").cast(pl.Utf8).alias("PRODUCTION_SOURCE"),
    )
    before = df.height
    df = df.sort(["COUNTRY_CODE", "YEAR", "FORECAST_FLAG"]).unique(
        subset=["COUNTRY_CODE", "YEAR"], keep="first", maintain_order=True
    )
    if df.height != before:
        log(f"  collapsed {before - df.height} duplicate (country, year) production row(s)")
    return df


def apply_removals(emiss: pl.DataFrame, removals: list[tuple[str, int, int]]) -> pl.DataFrame:
    keep = pl.lit(True)
    for code, lo, hi in removals:
        n = emiss.filter(
            (pl.col("COUNTRY_CODE") == code) & pl.col("YEAR").is_between(lo, hi)
        ).height
        log(f"  removing {n} reported {code} row(s) in {lo}-{hi}")
        keep = keep & ~((pl.col("COUNTRY_CODE") == code) & pl.col("YEAR").is_between(lo, hi))
    return emiss.filter(keep)


def _name_to_code_map(names: list[str]) -> dict[str, str | None]:
    """country_name_to_code (crt_methane.py, vendored from
    transformations_sources/pycountry.py) applied over a list of unique names."""
    import pycountry

    manual = {
        "Iran (Islamic Republic of)": "IRN",
        "Republic of Korea": "KOR",
        "Korea": "KOR",
        "Brunei": "BRN",
        "Russia": "RUS",
        "State of Palestine": "PSE",
        "Saint Vincent and the Gredis": "VCT",
        "Kosovo": "XKX",
    }

    def one(country: str) -> str | None:
        try:
            return pycountry.countries.lookup(country).alpha_3
        except LookupError:
            return manual.get(country)

    return {n: one(n) for n in names}


def load_gem_emissions(path: Path, year: int = EXTRA_SOURCE_YEAR) -> pl.DataFrame:
    """GEM Global Coal Mine Tracker -> one CH4 row per country for `year`.

    Mirrors transform_gem_country: operating mines only, sum the per-mine
    "GEM Coal Mine Methane Emissions Estimate (M tonnes/yr)" by country and
    convert Mt -> kt (x1000). SOURCE = "GEM".
    """
    import pandas as pd

    raw = pd.read_excel(path, sheet_name=GEM_SHEET)
    for col in (GEM_COUNTRY_COL, "Status", GEM_EST_COL):
        if col not in raw.columns:
            raise SystemExit(f"{path.name}: missing column {col!r} on sheet {GEM_SHEET!r}")

    df = pl.DataFrame(
        {
            "_name": raw[GEM_COUNTRY_COL].astype("string"),
            "_status": raw["Status"].astype("string"),
            "_est_mt": pd.to_numeric(raw[GEM_EST_COL], errors="coerce"),
        }
    ).filter((pl.col("_status") == GEM_STATUS) & pl.col("_est_mt").is_not_null())

    code_map = _name_to_code_map(df["_name"].drop_nulls().unique().to_list())
    unresolved = sorted(n for n, c in code_map.items() if c is None)
    if unresolved:
        log(f"  ! GEM: {len(unresolved)} country name(s) not matched, dropped: {unresolved}")

    out = (
        df.with_columns(
            pl.col("_name").replace_strict(code_map, default=None).alias("COUNTRY_CODE")
        )
        .drop_nulls("COUNTRY_CODE")
        .group_by("COUNTRY_CODE")
        .agg((pl.col("_est_mt").sum() * 1000).alias("EMISSIONS_CH4_KT"))
        .with_columns(
            pl.lit(year, dtype=pl.Int64).alias("YEAR"),
            pl.lit("GEM").alias("SOURCE"),
            pl.lit(GEM_FILENAME).alias("FILENAME"),
        )
        .select("COUNTRY_CODE", "YEAR", "EMISSIONS_CH4_KT", "SOURCE", "FILENAME")
    )
    log(f"  GEM {year}: {out.height} countries, {out['EMISSIONS_CH4_KT'].sum():.0f} kt")
    return out


def _resolve_sheet_id(sheet: str) -> str:
    """Accept a full Sheets URL or a bare doc id, return the doc id."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", sheet)
    return m.group(1) if m else sheet.strip()


def _fetch_sheet_csv(sheet: str, tab: str) -> str:
    """Fetch one tab of a Google Sheet as CSV via the unauthenticated gviz
    endpoint (mirrors crt_methane.py's load_non_annex_sheet). The sheet must
    be shared "anyone with the link can view"."""
    import requests

    doc = _resolve_sheet_id(sheet)
    url = (
        f"https://docs.google.com/spreadsheets/d/{doc}/gviz/tq"
        f"?tqx=out:csv&sheet={requests.utils.quote(tab)}"
    )
    log(f"  fetching the '{tab}' tab (gviz CSV) ...")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    if resp.text.lstrip().lower().startswith(("<!doctype", "<html")):
        raise SystemExit(
            f"the sheet returned HTML, not CSV - {doc} is probably not shared "
            '"anyone with the link can view", or the tab name is wrong.'
        )
    return resp.text


def load_iea_emissions(
    sheet: str = IEA_SHEET_ID, tab: str = IEA_TAB, year: int = EXTRA_SOURCE_YEAR
) -> pl.DataFrame:
    """IEA 2026 Google Sheet, 'Data' tab -> one CH4 row per country for `year`.

    Keeps only coal segments (segment contains "coal": steam / coking / other
    from coal). Per the brief: "other from coal" is a residual bucket, so it is
    dropped for any country that has no steam- or coking-coal emissions of its
    own (i.e. no genuine coal-mining series to attach it to). Remaining coal
    segments are summed per country. SOURCE = "IEA".
    """
    import pandas as pd

    raw = pd.read_csv(io.StringIO(_fetch_sheet_csv(sheet, tab)))
    cols = {c.strip().lower(): c for c in map(str, raw.columns)}
    try:
        c_country = cols["country"]
        c_segment = cols["segment"]
        c_emiss = cols["emissions (kt)"]
    except KeyError as exc:
        raise SystemExit(f"'{tab}' tab: missing column {exc}")

    df = pl.DataFrame(
        {
            "_name": raw[c_country].astype("string"),
            "_segment": raw[c_segment].astype("string"),
            "_kt": pd.to_numeric(raw[c_emiss], errors="coerce"),
        }
    ).filter(
        pl.col("_name").is_not_null()
        & pl.col("_kt").is_not_null()
        & pl.col("_segment").str.to_lowercase().str.contains("coal")
    )

    code_map = _name_to_code_map(df["_name"].drop_nulls().unique().to_list())
    df = df.with_columns(
        pl.col("_name").replace_strict(code_map, default=None).alias("COUNTRY_CODE"),
        pl.col("_segment").str.to_lowercase().str.strip_chars().alias("_seg"),
    ).drop_nulls("COUNTRY_CODE")

    core = set(
        df.filter(pl.col("_seg").is_in(list(IEA_CORE_COAL_SEGMENTS)))["COUNTRY_CODE"].to_list()
    )
    n_other = df.filter(
        (pl.col("_seg") == IEA_OTHER_COAL_SEGMENT) & ~pl.col("COUNTRY_CODE").is_in(list(core))
    ).height
    kept = df.filter(
        (pl.col("_seg") != IEA_OTHER_COAL_SEGMENT) | pl.col("COUNTRY_CODE").is_in(list(core))
    )
    log(
        f"  IEA: {len(core)} countries with steam/coking coal; "
        f"dropped {n_other} 'other from coal'-only country row(s)"
    )

    out = (
        kept.group_by("COUNTRY_CODE")
        .agg(pl.col("_kt").sum().alias("EMISSIONS_CH4_KT"))
        .with_columns(
            pl.lit(year, dtype=pl.Int64).alias("YEAR"),
            pl.lit("IEA").alias("SOURCE"),
            pl.lit(IEA_FILENAME).alias("FILENAME"),
        )
        .select("COUNTRY_CODE", "YEAR", "EMISSIONS_CH4_KT", "SOURCE", "FILENAME")
    )
    log(f"  IEA {year}: {out.height} countries, {out['EMISSIONS_CH4_KT'].sum():.0f} kt")
    return out


def forecast_extra_source(
    base: pl.DataFrame, benchmark_production: pl.DataFrame, prod: pl.DataFrame
) -> pl.DataFrame:
    """Extend a one-year (EXTRA_SOURCE_YEAR) extra-source emissions df (GEM or
    IEA, as produced by load_gem_emissions / load_iea_emissions) out to
    FORECAST_END_YEAR.

    Ported from ember-data-processing's transform_coal_emissions_iea /
    transform_coal_emissions_gem, restricted to their forecast branch: derive
    each country's implied CH4-per-tonne-of-coal intensity (EMISSIONS_CH4_KT
    / PRODUCTION_MT) against `benchmark_production` (one row per country -
    the caller picks the benchmark year), then scale the production mart's
    forecast years (FORECAST_FLAG=true, i.e. 2026-2030 - IEA is the only
    production SOURCE with a forecast) by that constant factor. A country
    with no or zero benchmark production (no intensity) is left out of the
    result - it keeps only its EXTRA_SOURCE_YEAR row.
    """
    source_name = base["SOURCE"][0] if base.height else "?"

    intensity = (
        base.join(benchmark_production, how="inner", on="COUNTRY_CODE")
        .filter(pl.col("PRODUCTION_MT") > 0)
        .select(
            "COUNTRY_CODE",
            (pl.col("EMISSIONS_CH4_KT") / pl.col("PRODUCTION_MT")).alias(
                "INTENSITY_CH4_KT_PER_MT"
            ),
        )
    )

    future_prod = prod.filter(
        pl.col("FORECAST_FLAG") & (pl.col("YEAR") > EXTRA_SOURCE_YEAR) & (pl.col("YEAR") <= FORECAST_END_YEAR)
    ).select("COUNTRY_CODE", "YEAR", "PRODUCTION_MT", "PRODUCTION_SOURCE")

    out = (
        intensity.join(future_prod, how="inner", on="COUNTRY_CODE")
        .with_columns(
            (pl.col("INTENSITY_CH4_KT_PER_MT") * pl.col("PRODUCTION_MT")).alias("EMISSIONS_CH4_KT"),
            pl.lit(source_name).alias("SOURCE_INTENSITY"),
        )
        .rename({"PRODUCTION_SOURCE": "SOURCE_PRODUCTION"})
        .select("COUNTRY_CODE", "YEAR", "EMISSIONS_CH4_KT", "SOURCE_INTENSITY", "SOURCE_PRODUCTION")
    )
    log(
        f"  {source_name} forecast {EXTRA_SOURCE_YEAR + 1}-{FORECAST_END_YEAR}: "
        f"{out.height} country-year rows, {out['COUNTRY_CODE'].n_unique()} countries "
        f"(of {base['COUNTRY_CODE'].n_unique()} with a {EXTRA_SOURCE_YEAR} estimate)"
    )
    return out


def append_extra_sources(
    out: pl.DataFrame, extras: list[pl.DataFrame], forecasts: list[pl.DataFrame]
) -> pl.DataFrame:
    """Add GEM / IEA per-country rows to the gap-filled frame as extra source
    rows (a country-year can now hold several rows, told apart by SOURCE_ALL):
    the EXTRA_SOURCE_YEAR base estimate from `extras`, plus any
    FORECAST_END_YEAR-bound rows from `forecasts` (see forecast_extra_source)."""
    if not extras and not forecasts:
        return out

    annex = (
        out.select("COUNTRY_CODE", "ANNEX_FLAG")
        .drop_nulls("ANNEX_FLAG")
        .unique(subset=["COUNTRY_CODE"])
    )

    parts = []
    if extras:
        parts.append(
            pl.concat(extras, how="vertical")
            .join(annex, how="left", on="COUNTRY_CODE")
            .with_columns(
                pl.lit("Estimate").alias("EMISSIONS_TYPE"),
                pl.lit(True).alias("EMISSIONS_ESTIMATED_FLAG"),
                pl.col("SOURCE").alias("SOURCE_EMISSIONS"),
                pl.col("SOURCE").alias("SOURCE_ALL"),
                pl.lit(None, dtype=pl.Utf8).alias("SOURCE_PRODUCTION"),
                pl.lit(None, dtype=pl.Utf8).alias("SOURCE_INTENSITY"),
            )
            .select(out.columns)
        )
    if forecasts:
        parts.append(
            pl.concat(forecasts, how="vertical")
            .join(annex, how="left", on="COUNTRY_CODE")
            .with_columns(
                pl.lit("Forecast").alias("EMISSIONS_TYPE"),
                pl.lit(True).alias("EMISSIONS_ESTIMATED_FLAG"),
                pl.lit(None, dtype=pl.Utf8).alias("SOURCE_EMISSIONS"),
                pl.col("SOURCE_INTENSITY").alias("SOURCE_ALL"),
            )
            .select(out.columns)
        )

    return pl.concat([out, *parts], how="vertical_relaxed").sort(
        ["COUNTRY_CODE", "YEAR", "SOURCE_ALL"]
    )


def add_world_rows(out: pl.DataFrame) -> pl.DataFrame:
    """Append COUNTRY_CODE="WLD" rollup rows, one set per YEAR:

      SOURCE_ALL="EIA-UNFCCC"  sum of EMISSIONS_CH4_KT over every row EXCEPT
                          the standalone GEM/IEA extras (i.e. the UNFCCC /
                          EIA-UNFCCC / IEA-UNFCCC "reported + estimated"
                          series - matches Ember's public chart's main line).
                          Labelled "EIA-UNFCCC" rather than a made-up value
                          since that's this bottom-up series' dominant
                          underlying method; no collision with the per-country
                          EIA-UNFCCC/IEA-UNFCCC/UNFCCC rows since COUNTRY_CODE
                          differs ("WLD" vs a real ISO3). EMISSIONS_TYPE is
                          "Forecast" if any contributing row that year is a
                          Forecast, else "Estimate" if any is an Estimate,
                          else "Report".
      SOURCE_ALL="GEM"    sum of the GEM extra rows for that year (EXTRA_SOURCE_YEAR,
                          plus FORECAST_END_YEAR-bound years where forecast - see
                          forecast_extra_source).
      SOURCE_ALL="IEA"    sum of the IEA extra rows for that year (same).

    Per-row fields that don't aggregate sensibly (SOURCE_EMISSIONS,
    SOURCE_PRODUCTION, SOURCE_INTENSITY, ANNEX_FLAG) are left null on the
    world rows.
    """
    null_extras = [
        pl.lit(None, dtype=pl.String).alias("SOURCE_EMISSIONS"),
        pl.lit(None, dtype=pl.String).alias("SOURCE_PRODUCTION"),
        pl.lit(None, dtype=pl.String).alias("SOURCE_INTENSITY"),
        pl.lit(None, dtype=pl.Boolean).alias("ANNEX_FLAG"),
    ]

    main = out.filter(~pl.col("SOURCE_ALL").is_in(["GEM", "IEA"]))
    world_main = (
        main.group_by("YEAR")
        .agg(
            pl.col("EMISSIONS_CH4_KT").sum(),
            (pl.col("EMISSIONS_TYPE") == "Forecast").any().alias("_has_forecast"),
            (pl.col("EMISSIONS_TYPE") == "Estimate").any().alias("_has_estimate"),
        )
        .with_columns(
            pl.when(pl.col("_has_forecast"))
            .then(pl.lit("Forecast"))
            .when(pl.col("_has_estimate"))
            .then(pl.lit("Estimate"))
            .otherwise(pl.lit("Report"))
            .alias("EMISSIONS_TYPE"),
            pl.lit("WLD").alias("COUNTRY_CODE"),
            pl.lit("EIA-UNFCCC").alias("SOURCE_ALL"),
            *null_extras,
        )
        .with_columns((pl.col("EMISSIONS_TYPE") != "Report").alias("EMISSIONS_ESTIMATED_FLAG"))
        .select(out.columns)
    )

    world_extras = [
        out.filter(pl.col("SOURCE_ALL") == src)
        .group_by("YEAR")
        .agg(pl.col("EMISSIONS_CH4_KT").sum())
        .with_columns(
            pl.lit("WLD").alias("COUNTRY_CODE"),
            pl.lit(src).alias("SOURCE_ALL"),
            pl.when(pl.col("YEAR") == EXTRA_SOURCE_YEAR)
            .then(pl.lit("Estimate"))
            .otherwise(pl.lit("Forecast"))
            .alias("EMISSIONS_TYPE"),
            pl.lit(True).alias("EMISSIONS_ESTIMATED_FLAG"),
            *null_extras,
        )
        .select(out.columns)
        for src in ("GEM", "IEA")
    ]

    return pl.concat([out, world_main, *world_extras], how="vertical_relaxed").sort(
        ["COUNTRY_CODE", "YEAR", "SOURCE_ALL"]
    )


def gap_fill(emiss: pl.DataFrame, prod: pl.DataFrame) -> pl.DataFrame:
    """transform_coal_emissions_unfccc, UNFCCC branch only."""
    # ANNEX_FLAG is country-constant; carry it to rows that come only from the
    # production side so estimated years keep the right flag.
    annex_by_country = (
        emiss.drop_nulls("ANNEX_FLAG")
        .group_by("COUNTRY_CODE")
        .agg(pl.col("ANNEX_FLAG").first())
    )

    prod_and_emiss = prod.join(emiss, how="full", on=["YEAR", "COUNTRY_CODE"], coalesce=True)

    intensity = prod_and_emiss.drop_nulls(
        subset=["PRODUCTION_MT", "EMISSIONS_CH4_KT"]
    ).select(
        "COUNTRY_CODE",
        "YEAR",
        pl.when(pl.col("PRODUCTION_MT") == 0)
        .then(None)
        .otherwise(pl.col("EMISSIONS_CH4_KT") / pl.col("PRODUCTION_MT"))
        .alias("INTENSITY_CH4_KT_PER_MT"),
    )

    latest_reported_production = (
        prod.filter(~pl.col("FORECAST_FLAG"))
        .group_by("COUNTRY_CODE")
        .agg(pl.col("YEAR").max().alias("LATEST_REPORTED_PRODUCTION_YEAR"))
    )

    w = {"partition_by": ["COUNTRY_CODE"], "order_by": ["YEAR"]}
    out = (
        prod_and_emiss.join(intensity, how="left", on=["COUNTRY_CODE", "YEAR"])
        .with_columns(
            pl.col("INTENSITY_CH4_KT_PER_MT")
            .forward_fill()
            .over(**w)
            .alias("INTENSITY_CH4_KT_PER_MT_FILLED")
        )
        .with_columns(
            pl.col("INTENSITY_CH4_KT_PER_MT_FILLED")
            .backward_fill()
            .over(**w)
            .alias("INTENSITY_CH4_KT_PER_MT_FILLED")
        )
        .with_columns(
            # zero coal production -> emissions are zero, but we leave the cell
            # blank (null) rather than writing a fabricated 0. Such rows carry no
            # reported figure and drop out of the final gap-filled series.
            pl.when(pl.col("PRODUCTION_MT") == 0)
            .then(None)
            .otherwise(pl.col("PRODUCTION_MT") * pl.col("INTENSITY_CH4_KT_PER_MT_FILLED"))
            .alias("EMISSIONS_CH4_KT_ESTIMATED")
        )
        .with_columns(
            (
                pl.col("EMISSIONS_CH4_KT").is_null()
                & pl.col("EMISSIONS_CH4_KT_ESTIMATED").is_not_null()
            ).alias("EMISSIONS_ESTIMATED_FLAG")
        )
        .join(latest_reported_production, how="left", on=["COUNTRY_CODE"])
        .with_columns(
            pl.coalesce("EMISSIONS_CH4_KT", "EMISSIONS_CH4_KT_ESTIMATED").alias(
                "EMISSIONS_CH4_KT"
            ),
            pl.when(
                "EMISSIONS_ESTIMATED_FLAG",
                pl.col("YEAR") <= pl.col("LATEST_REPORTED_PRODUCTION_YEAR"),
            )
            .then(pl.lit("Estimate"))
            .when(
                "EMISSIONS_ESTIMATED_FLAG",
                pl.col("YEAR") > pl.col("LATEST_REPORTED_PRODUCTION_YEAR"),
            )
            .then(pl.lit("Forecast"))
            .when(pl.col("EMISSIONS_CH4_KT").is_not_null())
            .then(pl.lit("Report"))
            .otherwise(None)
            .alias("EMISSIONS_TYPE"),
            pl.when("EMISSIONS_ESTIMATED_FLAG")
            .then("PRODUCTION_SOURCE")
            .otherwise(None)
            .alias("SOURCE_PRODUCTION"),
            pl.when("EMISSIONS_ESTIMATED_FLAG")
            .then(pl.lit("UNFCCC"))
            .otherwise(None)
            .alias("SOURCE_INTENSITY"),
            # non-null only for a genuine reported figure (pre-coalesce value)
            pl.when(pl.col("EMISSIONS_CH4_KT").is_not_null())
            .then(pl.lit("UNFCCC"))
            .otherwise(None)
            .alias("SOURCE_EMISSIONS"),
        )
        .with_columns(
            pl.when(pl.col("EMISSIONS_CH4_KT").is_null())
            .then(None)
            .otherwise(pl.col("EMISSIONS_ESTIMATED_FLAG"))
            .alias("EMISSIONS_ESTIMATED_FLAG")
        )
        # SOURCE_ALL, following transform_mart_coal_emissions_all's SOURCE_ALL:
        #   reported figure            -> "UNFCCC"
        #   gap-filled from production -> "<PRODUCTION_SOURCE>-<SOURCE_INTENSITY>",
        #                                 i.e. "EIA-UNFCCC" or "IEA-UNFCCC"
        .with_columns(
            pl.when(pl.col("SOURCE_EMISSIONS").is_not_null())
            .then(pl.col("SOURCE_EMISSIONS"))
            .when(pl.col("SOURCE_PRODUCTION").is_not_null())
            .then(pl.concat_str(["SOURCE_PRODUCTION", "SOURCE_INTENSITY"], separator="-"))
            .otherwise(None)
            .alias("SOURCE_ALL")
        )
    )

    out = out.drop("ANNEX_FLAG").join(annex_by_country, how="left", on="COUNTRY_CODE")

    return (
        out.filter(pl.col("EMISSIONS_CH4_KT").is_not_null())
        .select(
            "COUNTRY_CODE",
            "YEAR",
            "EMISSIONS_CH4_KT",
            "EMISSIONS_TYPE",
            "EMISSIONS_ESTIMATED_FLAG",
            "SOURCE_EMISSIONS",
            "SOURCE_PRODUCTION",
            "SOURCE_INTENSITY",
            "SOURCE_ALL",
            "ANNEX_FLAG",
        )
        .sort(["COUNTRY_CODE", "YEAR"])
    )


def main() -> None:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--emissions", type=Path, default=here / "crt_methane_ch4.csv")
    ap.add_argument(
        "--production",
        type=Path,
        default=here.parent / "production" / "coal_production_combined.csv",
    )
    ap.add_argument(
        "--gem",
        type=Path,
        default=here / "Global Coal Mine Tracker, May 2026__.xlsx",
        help="GEM Global Coal Mine Tracker .xlsx; adds one GEM 2025 row per country",
    )
    ap.add_argument(
        "--iea",
        default=IEA_SHEET_ID,
        help="IEA 2026 Google Sheet URL or doc id (must be link-viewable); "
        "adds one IEA 2025 row per country, fetched live from --iea-tab",
    )
    ap.add_argument(
        "--iea-tab",
        default=IEA_TAB,
        help="tab name within --iea (default 'Data')",
    )
    ap.add_argument("--no-gem", action="store_true", help="skip the GEM 2025 rows")
    ap.add_argument("--no-iea", action="store_true", help="skip the IEA 2025 rows")
    ap.add_argument(
        "--no-world",
        action="store_true",
        help="skip the COUNTRY_CODE=WLD world-total rows (SOURCE_ALL=EIA-UNFCCC/GEM/IEA)",
    )
    ap.add_argument("--out", type=Path, default=here / "crt_methane_ch4_gapfilled.csv")
    args = ap.parse_args()

    log(f"reading emissions   {args.emissions}")
    emiss = load_emissions(args.emissions)
    log(f"reading production  {args.production}")
    prod = load_production(args.production)

    emiss = apply_removals(emiss, REMOVALS)

    out = gap_fill(emiss, prod)

    extras: list[pl.DataFrame] = []
    forecasts: list[pl.DataFrame] = []

    # IEA forecast benchmark: production at EXTRA_SOURCE_YEAR itself (already
    # an IEA-forecast-flagged year in the mart).
    iea_benchmark = prod.filter(pl.col("YEAR") == EXTRA_SOURCE_YEAR).select(
        "COUNTRY_CODE", "PRODUCTION_MT"
    )
    # GEM forecast benchmark: each country's latest *reported* (non-forecast)
    # production year - same baseline the UNFCCC gap-fill factor uses.
    gem_benchmark = (
        prod.filter(~pl.col("FORECAST_FLAG"))
        .group_by("COUNTRY_CODE")
        .agg(pl.col("YEAR").max().alias("YEAR"))
        .join(prod, how="left", on=["COUNTRY_CODE", "YEAR"])
        .select("COUNTRY_CODE", "PRODUCTION_MT")
    )

    if not args.no_gem:
        if args.gem.exists():
            log(f"reading GEM          {args.gem}")
            gem = load_gem_emissions(args.gem)
            extras.append(gem)
            forecasts.append(forecast_extra_source(gem, gem_benchmark, prod))
        else:
            log(f"  ! GEM file not found, skipping: {args.gem}")
    if not args.no_iea:
        log(f"reading IEA          {args.iea} [{args.iea_tab}]")
        iea = load_iea_emissions(args.iea, args.iea_tab)
        extras.append(iea)
        forecasts.append(forecast_extra_source(iea, iea_benchmark, prod))
    out = append_extra_sources(out, extras, forecasts)

    if not args.no_world:
        out = add_world_rows(out)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.write_csv(args.out)

    log("")
    log(f"wrote {out.height} rows -> {args.out}")
    log(f"  countries : {out['COUNTRY_CODE'].n_unique()}")
    log(f"  years     : {out['YEAR'].min()}-{out['YEAR'].max()}")
    by_src = (
        out.group_by("SOURCE_ALL")
        .agg(pl.len().alias("rows"), pl.col("YEAR").min().alias("from"), pl.col("YEAR").max().alias("to"))
        .sort("SOURCE_ALL")
    )
    for row in by_src.iter_rows(named=True):
        log(f"  {str(row['SOURCE_ALL']):<11} {row['rows']:>6} rows  {row['from']}-{row['to']}")


if __name__ == "__main__":
    main()
