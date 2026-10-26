#!/usr/bin/env python3
"""
satellite_plume_dataset.py - clean, merge, and deduplicate coal-sector methane
plume detections from Carbon Mapper, Kayrros Methane Watch, and UNEP IMEO
MARS, and summarise them per country.

This is the standalone, updated equivalent of five cmm-data-tool notebooks,
run in this order:
    eda_carbon_mapper.ipynb
    eda_kayrros_methane_watch.ipynb
    eda_mars.ipynb
    merge_after_cleaning_satellite_cmm_data_tool.ipynb
    duplicates.ipynb              (re-applies the same cross-platform dedup to
                                    already-deduplicated data - a no-op given
                                    the current inputs, so not reproduced here)

Each source is cleaned to a shared column format - sensor, sector, country,
date, emission rate (kg/h), uncertainty (kg/h), latitude, longitude, comments,
platform - concatenated, filtered to sector == 'Coal', and deduplicated: once
on an exact match across all shared fields, then again across platforms that
reported the same sensor/date/lat/lon pair (keeping whichever record has both
an emission rate and an uncertainty; otherwise the first seen).

Three fixes relative to the notebooks (methodologically meaningful, called
out explicitly rather than silently inherited):

  1. Country-name harmonisation is now applied once, globally, after all
     three sources are combined (COUNTRY_ALIASES below), instead of per-source.
     The notebooks only harmonised 'United States'/'United States of America'
     -> 'USA' inside the Kayrros and MARS notebooks - but Carbon Mapper's
     country comes from the shapefile lookup, which returns the shapefile's
     own NAME value ('United States of America'), never harmonised, and no
     source harmonised 'Russian Federation' vs 'Russia' at all. The result,
     confirmed in the notebooks' current output, was the USA and Russia split
     across two country rows each in the stats file. This version harmonises
     to GEM's own country naming ('United States', 'Russia' - see
     ../production and ../satellite/gem_coal_production_per_country.csv) so
     the output here joins cleanly to the GEM-derived satellite-favourability
     tables in this same folder. This is not an exhaustive country-name audit
     against every GEM country spelling - only the two confirmed collisions.

  2. Carbon Mapper rows are now filtered to gas == 'CH4' before anything else.
     The notebook never filtered on gas, and Carbon Mapper's raw export mixes
     CH4 and CO2 plumes; a CO2 detection with a quantified emission rate (the
     coal sector has exactly one across the full history to date) would
     otherwise be counted as a methane detection.

  3. Airborne (aircraft-mounted) Carbon Mapper sensors - AVIRIS-3, AVIRIS-NG,
     Global Airborne Observatory - are excluded at merge time. The notebooks
     only excluded these two steps later, in processing_satellite.ipynb, when
     building the Flourish time series; the per-country statistics file
     (satellite_country_emission_statistics_*.csv, "the data for the cmm data
     tool") still included them. Since this folder is specifically about
     satellite monitoring, the exclusion is moved up front here.

Also changed: `date` is written as an ISO date, not a dd/mm/yyyy string - the
notebooks' format does not sort correctly as a string and is locale-ambiguous.

NOT fixed (left as an open, inherited limitation - see the docstring in
geo_country_lookup.py and the module-level note below):
  * Country assignment for Carbon Mapper (always) and Kayrros (EMIT rows with
    no reported country) still comes from the Natural Earth 110m shapefile, a
    coarse point-in-polygon lookup that can miss coastal/near-border plumes.
  * Cross-platform dedup still matches on an *exact* sensor/date/lat/lon key;
    the same plume reported with slightly different coordinates or timestamps
    by two platforms would not be caught.
  * Kayrros and MARS/Carbon Mapper are not guaranteed to be the same download
    vintage (see DOWNLOAD_DATE_KAYRROS vs DOWNLOAD_DATE_CM_MARS below) - this
    script uses whatever the most recent local download of each source is,
    same as the notebooks did, and logs the vintage of each at run time.
  * No normalisation by coal production/mine count is computed here - see
    gem_coal_satellite_detection.py in this folder for the production-
    weighted "currently detected" counterpart to this dataset.

Inputs (unchanged raw downloads, copied from cmm-data-tool into data/ alongside
this script so the 2026 Data Tool Updates folder doesn't depend on
cmm-data-tool being checked out next to it - these are frozen snapshots of
that download, not re-fetched or re-derived here):
  * data/carbonmapper/downloaded_<date>/export_*.csv
  * data/kayrros/Methane_Watch_asset_data_<date>.csv
  * data/mars/unep_methanedata_detected_plumes_<date>.csv
  * data/naturalearth_country_shapefiles/ne_110m_admin_0_countries.geojson
    (via geo_country_lookup.py)

Outputs (written to --outdir, default: this folder):
  * merged_satellite_plumes_coal.csv
        one row per deduplicated coal-sector plume detection, the standalone
        equivalent of final_data.csv but with the fixes above applied.
  * satellite_country_emission_statistics_coal.csv
        one row per country: number of observations, median/min/max emission
        rate (kg/h), and detection counts broken down by sensor and by
        platform - the standalone equivalent of
        satellite_country_emission_statistics_<date>_coal.csv.

Usage:
    python satellite_plume_dataset.py
    python satellite_plume_dataset.py --carbonmapper-date 11122025 --mars-date 11122025 --kayrros-date 11032025
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from geo_country_lookup import load_lookup

DATA_ROOT = Path("data")

DOWNLOAD_DATE_CM_MARS = "11122025"
DOWNLOAD_DATE_KAYRROS = "11032025"

CARBONMAPPER_URL = "https://data.carbonmapper.org/#1.81/30.8/50.5"
KAYRROS_URL = "https://methanewatch.kayrros.com/map"
MARS_URL = "https://methanedata.unep.org/map?mars=&company=&country=&cc=&sat=&limit=1000&pub=#mcoord=0/30/0"

SHARED_COLUMNS = [
    "sensor",
    "sector",
    "country",
    "date",
    "emission rate (kg/h)",
    "uncertainty (kg/h)",
    "latitude",
    "longitude",
    "comments",
    "platform",
]

COAL_SECTOR = "Coal"

# Confirmed collisions only (see fix #1 above) - canonicalised to GEM's spelling.
COUNTRY_ALIASES = {
    "United States of America": "United States",
    "USA": "United States",
    "Russian Federation": "Russia",
}

AIRBORNE_SENSORS = ["AVIRIS-3", "AVIRIS-NG", "Global Airborne Observatory"]

EXACT_DUP_KEYS = ["latitude", "longitude", "date", "emission rate (kg/h)", "sector", "country", "sensor"]
PLATFORM_DUP_KEYS = ["sensor", "date", "latitude", "longitude"]


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def clean_carbon_mapper(directory: Path, download_date: str) -> pl.DataFrame:
    """export_*.csv (one per year) -> cleaned coal+other-sector CH4 plumes.

    Country is unknown in the raw export and is always filled via the
    point-in-polygon shapefile lookup (geo_country_lookup).
    """
    files = sorted(directory.glob("export_*.csv"))
    if not files:
        raise SystemExit(f"no export_*.csv files found in {directory}")
    raw = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)

    raw = raw[raw["gas"] == "CH4"]  # fix #2: drop CO2 plumes

    sector = raw["ipcc_sector"].replace(
        {
            "Solid Waste (6A)": "Waste",
            "Waste Water (6B)": "Waste",
            "Oil & Gas (1B2)": "Oil and Gas",
            "Coal Mining (1B1a)": "Coal",
        }
    )
    sensor = raw["platform"].replace({"ISS": "EMIT - NASA"})
    date = pd.to_datetime(raw["datetime"], format="mixed").dt.date

    df = pl.DataFrame(
        {
            "sensor": sensor.astype("string"),
            "sector": sector.astype("string"),
            "date": date,
            "emission rate (kg/h)": pd.to_numeric(raw["emission_auto"], errors="coerce"),
            "uncertainty (kg/h)": pd.to_numeric(raw["emission_uncertainty_auto"], errors="coerce"),
            "latitude": raw["plume_latitude"].astype(float),
            "longitude": raw["plume_longitude"].astype(float),
        }
    ).drop_nulls(subset=["emission rate (kg/h)"])

    lookup = load_lookup()
    country = lookup.assign(df["latitude"].to_numpy(), df["longitude"].to_numpy())

    return df.with_columns(
        pl.Series("country", country, dtype=pl.Utf8),
        pl.lit(f"Downloaded from {CARBONMAPPER_URL} on the {download_date}. Cleaned using satellite_plume_dataset.py").alias(
            "comments"
        ),
        pl.lit("Carbon Mapper").alias("platform"),
    ).select(SHARED_COLUMNS)


def clean_kayrros(path: Path, download_date: str) -> pl.DataFrame:
    """Methane_Watch_asset_data_<date>.csv -> cleaned coal+other-sector plumes.

    Country is kept where Kayrros supplied one; the shapefile lookup only
    fills rows where it's blank (documented in the notebook as EMIT rows,
    which carry lat/lon but no country).
    """
    tonnes2kg = 1000.0
    raw = pd.read_csv(path, encoding="utf-8-sig")
    raw.columns = raw.columns.str.strip()

    sector = raw["Category"].replace({"og": "Oil and Gas", "coal": "Coal", "human": "Waste"})
    sensor = raw["Satellite"].replace({"Sentinel - 5P": "Sentinel-5P/TROPOMI - ESA", "EMIT": "EMIT - NASA"})
    date = pd.to_datetime(raw["Date"], format="mixed").dt.date

    df = pl.DataFrame(
        {
            "sensor": sensor.astype("string"),
            "sector": sector.astype("string"),
            "country": raw["Country"].astype("string"),
            "date": date,
            "emission rate (kg/h)": pd.to_numeric(raw["Emission Rate (tons/hour)"], errors="coerce") * tonnes2kg,
            "uncertainty (kg/h)": pd.to_numeric(raw["Uncertainty (tons/hour)"], errors="coerce") * tonnes2kg,
            "latitude": raw["Source Latitude Rough"].astype(float),
            "longitude": raw["Source Longitude Rough"].astype(float),
        }
    ).drop_nulls(subset=["emission rate (kg/h)"])

    blank_country = df["country"].is_null() | (df["country"] == "")
    if blank_country.any():
        lookup = load_lookup()
        idx = np.where(blank_country.to_numpy())[0]
        filled = lookup.assign(
            df["latitude"].to_numpy()[idx], df["longitude"].to_numpy()[idx]
        )
        country = df["country"].to_list()
        for i, name in zip(idx, filled):
            country[i] = name
        df = df.with_columns(pl.Series("country", country, dtype=pl.Utf8))

    return df.with_columns(
        pl.lit(f"Downloaded from {KAYRROS_URL} on the {download_date}. Cleaned using satellite_plume_dataset.py").alias(
            "comments"
        ),
        pl.lit("Kayrros Methane Watch").alias("platform"),
    ).select(SHARED_COLUMNS)


def clean_mars(path: Path, download_date: str) -> pl.DataFrame:
    """unep_methanedata_detected_plumes_<date>.csv -> cleaned coal+other-sector plumes.

    Country is MARS's own reported value - no spatial lookup, unlike the
    other two sources.
    """
    raw = pd.read_csv(path)
    raw.columns = raw.columns.str.strip()
    date = pd.to_datetime(raw["tile_date"], format="mixed").dt.date

    df = pl.DataFrame(
        {
            "sensor": raw["satellite"].astype("string"),
            "sector": raw["sector"].astype("string"),
            "country": raw["country"].astype("string"),
            "date": date,
            "emission rate (kg/h)": pd.to_numeric(raw["ch4_fluxrate"], errors="coerce"),
            "uncertainty (kg/h)": pd.to_numeric(raw["ch4_fluxrate_std"], errors="coerce"),
            "latitude": raw["lat"].astype(float),
            "longitude": raw["lon"].astype(float),
        }
    )

    return df.with_columns(
        pl.lit(f"Downloaded from {MARS_URL} on the {download_date}. Cleaned using satellite_plume_dataset.py").alias(
            "comments"
        ),
        pl.lit("IMEO MARS").alias("platform"),
    ).select(SHARED_COLUMNS)


def harmonise_countries(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(pl.col("country").replace(COUNTRY_ALIASES))


def deduplicate(df: pl.DataFrame) -> pl.DataFrame:
    """Exact-duplicate drop, then cross-platform same-plume collapse.

    Mirrors merge_after_cleaning_satellite_cmm_data_tool.ipynb's two-step
    dedup: an exact match across all shared fields, then (within groups that
    share sensor/date/lat/lon across *different* platforms) keep whichever
    row has both an emission rate and an uncertainty, or the first row seen
    if none do.
    """
    df = df.unique(subset=EXACT_DUP_KEYS, keep="first", maintain_order=True)

    platform_count = pl.col("platform").n_unique().over(PLATFORM_DUP_KEYS)
    df = df.with_columns(platform_count.alias("_platform_count"))

    singles = df.filter(pl.col("_platform_count") == 1)
    dupes = df.filter(pl.col("_platform_count") > 1)

    has_both = pl.col("emission rate (kg/h)").is_not_null() & pl.col("uncertainty (kg/h)").is_not_null()
    resolved_dupes = (
        dupes.with_columns(has_both.alias("_has_both"))
        .sort("_has_both", descending=True, maintain_order=True)
        .group_by(PLATFORM_DUP_KEYS, maintain_order=True)
        .first()
        .drop("_has_both")
    )

    return pl.concat([singles, resolved_dupes], how="diagonal").drop("_platform_count").sort(["country", "date"])


def build_merged_plumes(cm: pl.DataFrame, kayrros: pl.DataFrame, mars: pl.DataFrame) -> pl.DataFrame:
    merged = pl.concat([kayrros, mars, cm], how="diagonal")
    merged = merged.filter(pl.col("sector") == COAL_SECTOR)
    merged = harmonise_countries(merged)
    merged = merged.filter(~pl.col("sensor").is_in(AIRBORNE_SENSORS))  # fix #3
    return deduplicate(merged)


def build_country_statistics(merged: pl.DataFrame) -> pl.DataFrame:
    summary = merged.group_by("country").agg(
        pl.len().alias("number observations"),
        pl.col("emission rate (kg/h)").median().alias("median (kg/h)"),
        pl.col("emission rate (kg/h)").min().alias("min (kg/h)"),
        pl.col("emission rate (kg/h)").max().alias("max (kg/h)"),
    )

    sensor_counts = (
        merged.group_by(["country", "sensor"])
        .agg(pl.len().alias("n"))
        .pivot(on="sensor", index="country", values="n")
        .fill_null(0)
    )
    platform_counts = (
        merged.group_by(["country", "platform"])
        .agg(pl.len().alias("n"))
        .pivot(on="platform", index="country", values="n")
        .fill_null(0)
    )

    return summary.join(sensor_counts, on="country", how="left").join(
        platform_counts, on="country", how="left"
    ).sort("number observations", descending=True)


def main() -> None:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", type=Path, default=here / DATA_ROOT)
    ap.add_argument("--carbonmapper-date", default=DOWNLOAD_DATE_CM_MARS)
    ap.add_argument("--mars-date", default=DOWNLOAD_DATE_CM_MARS)
    ap.add_argument("--kayrros-date", default=DOWNLOAD_DATE_KAYRROS)
    ap.add_argument("--outdir", type=Path, default=here)
    args = ap.parse_args()

    if args.carbonmapper_date != args.kayrros_date or args.mars_date != args.kayrros_date:
        log(
            f"NOTE: source download dates differ (Carbon Mapper {args.carbonmapper_date}, "
            f"MARS {args.mars_date}, Kayrros {args.kayrros_date}) - Kayrros has not been "
            f"refreshed as recently as the other two sources. Not an error, just worth knowing."
        )

    cm_dir = args.data_root / "carbonmapper" / f"downloaded_{args.carbonmapper_date}"
    kayrros_path = args.data_root / "kayrros" / f"Methane_Watch_asset_data_{args.kayrros_date}.csv"
    mars_path = args.data_root / "mars" / f"unep_methanedata_detected_plumes_{args.mars_date}.csv"

    log(f"cleaning Carbon Mapper   {cm_dir}")
    cm = clean_carbon_mapper(cm_dir, args.carbonmapper_date)
    log(f"  {cm.height} CH4 plumes with a quantified emission rate")

    log(f"cleaning Kayrros         {kayrros_path}")
    kayrros = clean_kayrros(kayrros_path, args.kayrros_date)
    log(f"  {kayrros.height} plumes with a quantified emission rate")

    log(f"cleaning IMEO MARS       {mars_path}")
    mars = clean_mars(mars_path, args.mars_date)
    log(f"  {mars.height} plumes")

    merged = build_merged_plumes(cm, kayrros, mars)
    log(f"merged, coal-only, deduplicated: {merged.height} plumes")

    country_stats = build_country_statistics(merged)

    args.outdir.mkdir(parents=True, exist_ok=True)
    merged_out = args.outdir / "merged_satellite_plumes_coal.csv"
    stats_out = args.outdir / "satellite_country_emission_statistics_coal.csv"

    merged.write_csv(merged_out)
    country_stats.write_csv(stats_out)

    log("")
    log(f"wrote {merged.height} rows -> {merged_out}")
    log(f"wrote {country_stats.height} rows -> {stats_out}")


if __name__ == "__main__":
    main()
