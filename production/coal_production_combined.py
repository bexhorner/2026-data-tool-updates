"""Combine reported EIA coal production with the IEA Coal 2025 forecast.

Reads:
  * eia_coal_production.csv            (from eia_coal_production.py)
  * iea_coal_production_forecast.csv   (from iea_coal_production_forecast.py)

Writes a single stacked table, coal_production_combined.csv (+ .parquet), in the
same schema Ember's methane pipeline uses for `methane.mart_coal_production`:

    YEAR  COUNTRY_CODE  PRODUCTION_METALLURGICAL_MT  PRODUCTION_TOTAL_MT
          PRODUCTION_OTHER_MT  FORECAST_FLAG  SOURCE

The IEA rows are transformed exactly like
`src/pipelines/methane/transformations_sources/iea.py :: transform_iea_production`:
  PRODUCTION_TOTAL_MT      = Production (kt) / 1000
  PRODUCTION_METALLURGICAL_MT = null   (IEA forecast is total coal only)
  PRODUCTION_OTHER_MT        = null
  FORECAST_FLAG           = Year >= IEA_FORECAST_START_YEAR  (2025)
  SOURCE                  = "IEA"

The EIA rows already carry FORECAST_FLAG = False and SOURCE = "EIA".
Both sources are kept (not de-duplicated) and told apart by SOURCE /
FORECAST_FLAG, matching how the pipeline concatenates them.

Also appends a COUNTRY_CODE="WLD" world-total row per YEAR. SOURCE is "EIA"
or "IEA" depending on which source actually populates that year - EIA covers
1990-2024 and IEA covers 2025-2030 with zero country-year overlap between
them, so this is unambiguous (no new "WORLD" source value is invented).
PRODUCTION_METALLURGICAL_MT / PRODUCTION_OTHER_MT / PRODUCTION_TOTAL_MT are
each the sum across every country that year, skipping nulls. Because IEA
forecast rows carry a total but no metallurgical/other split, the world
met+other sum is null for 2025+ (every country that year is IEA-sourced) -
same "unspecified" gap as Ember's public chart. FORECAST_FLAG is true if any
country that year is on the IEA forecast. Pass --no-world to skip these rows.

Usage:
    python coal_production_combined.py
    python coal_production_combined.py --indir . --outdir .
    python coal_production_combined.py --no-world
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

IEA_FORECAST_START_YEAR = 2025

# Canonical column order (matches eia_coal_production.csv / mart_coal_production).
COLUMNS = [
    "YEAR",
    "COUNTRY_CODE",
    "PRODUCTION_METALLURGICAL_MT",
    "PRODUCTION_TOTAL_MT",
    "PRODUCTION_OTHER_MT",
    "FORECAST_FLAG",
    "SOURCE",
]


def load_eia(path: Path) -> pl.DataFrame:
    df = pl.read_csv(path)
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise SystemExit(f"{path.name}: missing columns {sorted(missing)}")
    return df.select(COLUMNS)


def load_iea_forecast(path: Path) -> pl.DataFrame:
    raw = pl.read_csv(path)  # Code, Year, Production (kt)
    return raw.select(
        pl.col("Year").cast(pl.Int64).alias("YEAR"),
        pl.col("Code").alias("COUNTRY_CODE"),
        pl.lit(None, dtype=pl.Float64).alias("PRODUCTION_METALLURGICAL_MT"),
        (
            pl.col("Production").cast(pl.Utf8).str.replace_all(",", "").cast(pl.Float64)
            / 1000
        ).alias("PRODUCTION_TOTAL_MT"),
        pl.lit(None, dtype=pl.Float64).alias("PRODUCTION_OTHER_MT"),
        (pl.col("Year").cast(pl.Int64) >= IEA_FORECAST_START_YEAR).alias("FORECAST_FLAG"),
        pl.lit("IEA").alias("SOURCE"),
    ).select(COLUMNS)


def _null_safe_sum(col: str) -> pl.Expr:
    """sum() that stays null (not 0) when every contributing value that year
    is null - e.g. 2026-2030, where every country is on an IEA forecast row
    with no metallurgical/other split at all."""
    return (
        pl.when(pl.col(col).count() == 0)
        .then(None)
        .otherwise(pl.col(col).sum())
        .alias(col)
    )


def add_world_rows(combined: pl.DataFrame) -> pl.DataFrame:
    """Append one COUNTRY_CODE="WLD" row per YEAR summing every country's
    production (nulls skipped - see module docstring for the met/other
    undercount this implies on IEA-forecast years). SOURCE is "IEA" for a
    year where any contributing row is IEA-sourced, else "EIA" - EIA (1990-
    2024) and IEA (2025-2030) never share a country-year in this file, so
    this exactly reflects which source actually built the row rather than
    inventing a third "WORLD" label."""
    world = (
        combined.group_by("YEAR")
        .agg(
            _null_safe_sum("PRODUCTION_METALLURGICAL_MT"),
            _null_safe_sum("PRODUCTION_TOTAL_MT"),
            _null_safe_sum("PRODUCTION_OTHER_MT"),
            pl.col("FORECAST_FLAG").any(),
            (pl.col("SOURCE") == "IEA").any().alias("_has_iea"),
        )
        .with_columns(
            pl.lit("WLD").alias("COUNTRY_CODE"),
            pl.when(pl.col("_has_iea")).then(pl.lit("IEA")).otherwise(pl.lit("EIA")).alias("SOURCE"),
        )
        .select(combined.columns)
    )
    return pl.concat([combined, world], how="vertical").sort(["COUNTRY_CODE", "YEAR", "SOURCE"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).parent
    ap.add_argument("--indir", default=str(here))
    ap.add_argument("--outdir", default=str(here))
    ap.add_argument("--no-world", action="store_true", help="skip the COUNTRY_CODE=WLD world-total rows")
    args = ap.parse_args()

    indir, outdir = Path(args.indir), Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    eia = load_eia(indir / "eia_coal_production.csv")
    iea = load_iea_forecast(indir / "iea_coal_production_forecast.csv")

    combined = pl.concat([eia, iea], how="vertical").sort(
        ["COUNTRY_CODE", "YEAR", "SOURCE"]
    )

    if not args.no_world:
        combined = add_world_rows(combined)

    out_csv = outdir / "coal_production_combined.csv"
    combined.write_csv(out_csv)
    combined.write_parquet(outdir / "coal_production_combined.parquet")

    by_src = combined.group_by("SOURCE").agg(
        pl.len().alias("rows"),
        pl.col("YEAR").min().alias("year_min"),
        pl.col("YEAR").max().alias("year_max"),
        pl.col("FORECAST_FLAG").sum().alias("forecast_rows"),
    )
    overlap = (
        eia.select("COUNTRY_CODE", "YEAR")
        .join(iea.select("COUNTRY_CODE", "YEAR"), on=["COUNTRY_CODE", "YEAR"], how="inner")
        .height
    )
    print(f"wrote {combined.height} rows -> {out_csv}", file=sys.stderr)
    print(by_src.sort("SOURCE"), file=sys.stderr)
    print(f"country-year pairs present in both sources: {overlap}", file=sys.stderr)


if __name__ == "__main__":
    main()
