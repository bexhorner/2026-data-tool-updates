#!/usr/bin/env python3
"""
gem_coal_satellite_favourability.py - % of a country's coal production that
sits under favourable satellite methane-monitoring conditions.

This is the standalone, updated equivalent of cmm-data-tool's
    year_frac_gem_coal_production_per_country.ipynb
rebuilt to read mine identity, coal type, and historical production straight
from the single GEM workbook (Global Coal Mine Tracker, May 2026__.xlsx)
instead of the old three-source setup (a metabase `gem_mines_raw` export plus
separate China / non-China production supplement CSVs).

That old setup had a real bug the notebook version of this script fixed by
hand: mines present in the metabase export but absent from the production
supplement got appended with no GEM Mine ID at all (a name-matching fallback
that never carried the ID over), so they could never join to their satellite
score later even when one existed. Sourcing everything from the May 2026
workbook's "Historic Production (2018-2025)" sheet sidesteps this entirely -
every row there already carries its own GEM Mine ID (0 missing, 0 duplicate
across 3,740 mines), so no name-matching fallback is needed at all.

Method (unchanged from the notebook):
  1. Per operating coal mine, take the satellite-viewing-condition class
     (0 = difficult, 1 = moderate, 2 = favourable) for each of the 12 months
     in the pre-computed score file, and turn that into the fraction of the
     year spent in each class (year_frac_0 / 1 / 2).
  2. Multiply each fraction by the mine's most recent annual coal output to
     get production-months in each class.
  3. Sum by country. % favourable = year_frac_2_prod / (sum of all three).
  4. Append a World row (sum across all countries) before computing %.

Non-lignite, Operating mines only - lignite is excluded because it's
unsuited to the satellite retrieval; Operating-only mirrors the notebook's
filter on the score file's Status column. Only "Historic Production" rows
that also have a coal-type match (excluding Lignite) and a score-file match
are counted, same drop conditions as the notebook (dropna on the score /
dropna on production), just without the ID-loss bug.

Inputs:
  * Global Coal Mine Tracker, May 2026__.xlsx
        "Non-closed mines" + "Closed mines"   -> GEM Mine ID -> Coal Type
        "Historic Production (2018-2025)"     -> GEM Mine ID, Country,
                                                  Mine Name, Status, yearly
                                                  Coal Output (Annual, Mt)
  * combined_classes score file (unchanged source, from cmm-data-tool's
    satellite-viewing-condition pipeline): one row per mine with Unit ID,
    Country, Fuel type, Status, and Month_1..Month_12 condition classes.

Outputs (written to --outdir, default: this folder):
  * gem_coal_mine_production_most_recent.csv
        one row per non-lignite mine: Unit ID, Country, Mine Name, Status,
        Most Recent Coal Output (Annual, Mt)
  * gem_coal_production_per_country.csv
        one row per country: year_frac_0_prod, year_frac_1_prod,
        year_frac_2_prod, production_year_frac_0_1_sum
  * gem_coal_production_per_country_favourable.csv
        one row per country: % Coal Production with Favourable Conditions
        for Satellite Methane Monitoring

Usage:
    python gem_coal_satellite_favourability.py
    python gem_coal_satellite_favourability.py \
        --gem "../emissions/Global Coal Mine Tracker, May 2026__.xlsx" \
        --scores "../../Data Tool/cmm-data-tool/data_historical/combined_score/diff7_med1_sza70_75_elev80_100_csThres0.2_0.3_albedoSwir0.02_0.06_wind4_10combined_classes.csv"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

GEM_NONCLOSED_SHEET = "Non-closed mines"
GEM_CLOSED_SHEET = "Closed mines"
GEM_PROD_SHEET = "Historic Production (2018-2025)"

# Newest -> oldest, so pl.coalesce picks the most recent non-null figure.
PROD_YEARS = list(range(2025, 2016, -1))
PROD_YEAR_COL = "Coal Output (Annual, Mt) {year}"

LIGNITE_TYPE = "lignite"

SCORE_MONTH_COLS = [f"Month_{i}" for i in range(1, 13)]
SCORE_STATUS = "Operating"
SCORE_FUEL_TYPE = "coal"

DEFAULT_SCORE_FILE = (
    Path("..")
    / ".."
    / "Data Tool"
    / "cmm-data-tool"
    / "data_historical"
    / "combined_score"
    / (
        "diff7_med1_sza70_75_elev80_100_csThres0.2_0.3_"
        "albedoSwir0.02_0.06_wind4_10combined_classes.csv"
    )
)


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def load_coal_types(gem_path: Path) -> pl.DataFrame:
    """GEM Mine ID -> Coal Type, deduped across the Non-closed + Closed sheets.

    Both sheets carry duplicate GEM Mine ID rows (a pre-existing artifact of
    how GEM's export is put together, not introduced here) - drop exact
    duplicate rows first, then collapse any remaining same-ID rows to the
    first non-null Coal Type.
    """
    import pandas as pd

    frames = []
    for sheet in (GEM_NONCLOSED_SHEET, GEM_CLOSED_SHEET):
        raw = pd.read_excel(gem_path, sheet_name=sheet, usecols=["GEM Mine ID", "Coal Type"])
        frames.append(raw.drop_duplicates())
    raw = pd.concat(frames, ignore_index=True).dropna(subset=["GEM Mine ID"])
    raw = raw.groupby("GEM Mine ID", as_index=False).first()

    return pl.DataFrame(
        {
            "Unit ID": raw["GEM Mine ID"].astype("string"),
            "Coal Type": raw["Coal Type"].astype("string"),
        }
    )


def load_gem_production(gem_path: Path) -> pl.DataFrame:
    """Historic Production sheet -> one row per mine with its most recent
    non-null annual coal output (Mt), newest year wins."""
    import pandas as pd

    year_cols = [PROD_YEAR_COL.format(year=y) for y in PROD_YEARS]
    usecols = ["GEM Mine ID", "Country", "Mine Name", "Status", *year_cols]
    raw = pd.read_excel(gem_path, sheet_name=GEM_PROD_SHEET, usecols=usecols)
    missing = set(usecols) - set(raw.columns)
    if missing:
        raise SystemExit(f"{gem_path.name}: missing columns on {GEM_PROD_SHEET!r}: {sorted(missing)}")

    df = pl.DataFrame(
        {
            "Unit ID": raw["GEM Mine ID"].astype("string"),
            "Country": raw["Country"].astype("string"),
            "Mine Name": raw["Mine Name"].astype("string"),
            "Status": raw["Status"].astype("string"),
            **{
                col: pd.to_numeric(raw[col], errors="coerce")
                for col in year_cols
            },
        }
    )
    return df.with_columns(
        pl.coalesce([pl.col(c) for c in year_cols]).alias("Most Recent Coal Output (Annual, Mt)")
    ).select("Unit ID", "Country", "Mine Name", "Status", "Most Recent Coal Output (Annual, Mt)")


def load_satellite_scores(score_path: Path) -> pl.DataFrame:
    """Score file -> one row per operating coal mine with the fraction of the
    year spent in each viewing-condition class (0 difficult / 1 moderate /
    2 favourable)."""
    import csv

    need = ["Unit ID", "Country", "Fuel type", "Status", *SCORE_MONTH_COLS]
    with score_path.open(newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    missing = set(need) - set(header)
    if missing:
        raise SystemExit(f"{score_path.name}: missing columns {sorted(missing)}")

    # Read only the columns this pipeline needs: the file carries other,
    # unused fields (e.g. Production (Mtpa)) with stray non-numeric junk
    # values that would otherwise fail dtype inference.
    df = pl.read_csv(score_path, columns=need, null_values="--")

    df = df.filter(
        (pl.col("Status") == SCORE_STATUS) & (pl.col("Fuel type") == SCORE_FUEL_TYPE)
    ).with_columns(pl.col("Unit ID").cast(pl.Utf8).str.strip_chars())

    n_months = len(SCORE_MONTH_COLS)
    return df.with_columns(
        *(
            (
                pl.sum_horizontal([(pl.col(c) == cls) for c in SCORE_MONTH_COLS]) / n_months
            ).alias(f"year_frac_{cls}")
            for cls in (0, 1, 2)
        )
    ).select("Unit ID", "year_frac_0", "year_frac_1", "year_frac_2")


def build_mine_production(gem_path: Path) -> pl.DataFrame:
    production = load_gem_production(gem_path)
    coal_types = load_coal_types(gem_path)

    merged = production.join(coal_types, on="Unit ID", how="left")
    is_lignite = pl.col("Coal Type").str.to_lowercase().str.strip_chars() == LIGNITE_TYPE
    return merged.filter(~is_lignite.fill_null(False)).drop("Coal Type")


def build_country_tables(mine_production: pl.DataFrame, scores: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    # Right-join semantics of the notebook: keep every production mine, attach
    # a score where one exists. A mine with no score match (Unit ID absent
    # from `scores`) has all year_frac_* null and is dropped, same as the
    # notebook's dropna(subset=['Mode']).
    merged = mine_production.join(scores, on="Unit ID", how="left").filter(
        pl.col("year_frac_0").is_not_null()
        & pl.col("Most Recent Coal Output (Annual, Mt)").is_not_null()
    )

    weighted = merged.with_columns(
        *(
            (pl.col(f"year_frac_{cls}") * pl.col("Most Recent Coal Output (Annual, Mt)")).alias(
                f"year_frac_{cls}_prod"
            )
            for cls in (0, 1, 2)
        )
    )

    by_country = (
        weighted.group_by("Country")
        .agg(
            pl.col("year_frac_0_prod").sum(),
            pl.col("year_frac_1_prod").sum(),
            pl.col("year_frac_2_prod").sum(),
        )
        .with_columns(
            (pl.col("year_frac_0_prod") + pl.col("year_frac_1_prod")).alias(
                "production_year_frac_0_1_sum"
            )
        )
        .sort("production_year_frac_0_1_sum", descending=True)
    )

    world = by_country.select(
        pl.lit("World").alias("Country"),
        pl.col("year_frac_0_prod").sum(),
        pl.col("year_frac_1_prod").sum(),
        pl.col("year_frac_2_prod").sum(),
        pl.col("production_year_frac_0_1_sum").sum(),
    )
    by_country = pl.concat([by_country, world], how="vertical")

    favourable = by_country.select(
        "Country",
        (
            100
            * pl.col("year_frac_2_prod")
            / (pl.col("year_frac_0_prod") + pl.col("year_frac_1_prod") + pl.col("year_frac_2_prod"))
        ).alias("% Coal Production with Favourable Conditions for Satellite Methane Monitoring"),
    )
    return by_country, favourable


def main() -> None:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--gem",
        type=Path,
        default=here.parent / "emissions" / "Global Coal Mine Tracker, May 2026__.xlsx",
    )
    ap.add_argument("--scores", type=Path, default=here / DEFAULT_SCORE_FILE)
    ap.add_argument("--outdir", type=Path, default=here)
    args = ap.parse_args()

    log(f"reading GEM workbook  {args.gem}")
    mine_production = build_mine_production(args.gem)
    log(f"  {mine_production.height} non-lignite mines with production data")

    log(f"reading scores        {args.scores}")
    scores = load_satellite_scores(args.scores)
    log(f"  {scores.height} operating coal mines with a satellite score")

    by_country, favourable = build_country_tables(mine_production, scores)

    args.outdir.mkdir(parents=True, exist_ok=True)
    mine_out = args.outdir / "gem_coal_mine_production_most_recent.csv"
    country_out = args.outdir / "gem_coal_production_per_country.csv"
    favourable_out = args.outdir / "gem_coal_production_per_country_favourable.csv"

    mine_production.write_csv(mine_out)
    by_country.write_csv(country_out)
    favourable.write_csv(favourable_out)

    n_countries = favourable.height - 1  # exclude World
    log("")
    log(f"wrote {mine_production.height} rows -> {mine_out}")
    log(f"wrote {by_country.height} rows -> {country_out}")
    log(f"wrote {favourable.height} rows ({n_countries} countries + World) -> {favourable_out}")
    world_pct = favourable.filter(pl.col("Country") == "World")[
        "% Coal Production with Favourable Conditions for Satellite Methane Monitoring"
    ][0]
    log(f"  World favourable: {world_pct:.2f}%")


if __name__ == "__main__":
    main()
