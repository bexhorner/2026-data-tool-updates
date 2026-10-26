#!/usr/bin/env python3
"""
gem_coal_satellite_detection.py - % of a country's coal production that is
currently being detected by satellite, as the counterpart to
gem_coal_satellite_favourability.py's % under *favourable* viewing conditions.

There is no notebook equivalent of this script - favourability measures
whether a mine's location and overpass timing make satellite detection
*possible*; this measures whether a mine actually *has* a nearby plume
detection in the combined satellite record built by satellite_plume_dataset.py
(Carbon Mapper + Kayrros Methane Watch + IMEO MARS, coal sector only).

Method:
  1. Start from the same mine population as the favourability script: every
     non-lignite mine with a most-recent annual coal output AND a matching
     Operating-status satellite-viewing score (build_mine_production +
     load_satellite_scores, imported from gem_coal_satellite_favourability.py
     so both scripts stay in sync on what counts as "a mine").
  2. Attach each mine's Latitude/Longitude from the GEM workbook.
  3. For every mine, find the closest coal-sector plume detection (great-
     circle distance) in merged_satellite_plumes_coal.csv, searched globally
     by coordinates - NOT by matching country names between the two datasets,
     which sidesteps any country-spelling mismatch between GEM and the plume
     sources entirely.
  4. A mine counts as "currently detected" if that nearest plume is within
     --threshold-km (default 5 km). This is a judgement call, not a derived
     constant: open-pit coal mines commonly span several hundred metres to a
     few kilometres, and satellite plume geolocation carries its own
     uncertainty, so 5 km gives room for both without being so wide that
     detections over a neighbouring, undetected mine get misattributed.
     Tighten or loosen it with --threshold-km to see how sensitive the
     country percentages are to this choice.
  5. Sum by country (favourability's World-row pattern repeated: % detected
     by mine count, and % detected weighted by production).

This is a plume-to-mine match, not a mine-to-plume one: a single plume near
several closely-spaced mines will mark all of them "detected" (no double-
counting of production, since each mine's own output is what's being tested,
not the plume's). Conversely a plume whose country is recorded differently
to any GEM mine nearby (shapefile/MARS naming quirks - see
satellite_plume_dataset.py) still counts, because the match never looks at
country at all.

Not used here, but worth knowing about: the GEM workbook itself carries a
"Has associated plume data" Y/N column (231 of 5,382 non-closed mines are
marked Y). That's GEM's own curation, not derived from the same three
sources this script matches against, so it isn't a substitute for - but may
be a useful cross-check against - the detection flag computed here.

Inputs:
  * Global Coal Mine Tracker, May 2026__.xlsx
        "Non-closed mines" + "Closed mines" -> GEM Mine ID -> Latitude, Longitude
        (plus everything gem_coal_satellite_favourability.py reads)
  * combined_classes score file (same as gem_coal_satellite_favourability.py)
  * merged_satellite_plumes_coal.csv (from satellite_plume_dataset.py)

Outputs (written to --outdir, default: this folder):
  * gem_coal_mine_detection_status.csv
        one row per mine: Unit ID, Country, Mine Name, Status, Most Recent
        Coal Output (Annual, Mt), nearest plume distance (km), detected
  * gem_coal_production_per_country_detected.csv
        one row per country: mine counts and production totals, split by
        detected/not, and % Coal Mines / % Coal Production Currently
        Detected by Satellite

Usage:
    python gem_coal_satellite_detection.py
    python gem_coal_satellite_detection.py --threshold-km 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

from gem_coal_satellite_favourability import (
    DEFAULT_SCORE_FILE,
    GEM_CLOSED_SHEET,
    GEM_NONCLOSED_SHEET,
    build_mine_production,
    load_satellite_scores,
)

EARTH_RADIUS_KM = 6371.0
DEFAULT_THRESHOLD_KM = 5.0
MINE_CHUNK_SIZE = 500


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def load_mine_locations(gem_path: Path) -> pl.DataFrame:
    """GEM Mine ID -> Latitude, Longitude, deduped across Non-closed + Closed."""
    import pandas as pd

    frames = []
    for sheet in (GEM_NONCLOSED_SHEET, GEM_CLOSED_SHEET):
        raw = pd.read_excel(gem_path, sheet_name=sheet, usecols=["GEM Mine ID", "Latitude", "Longitude"])
        frames.append(raw.drop_duplicates())
    raw = pd.concat(frames, ignore_index=True).dropna(subset=["GEM Mine ID"])
    raw = raw.groupby("GEM Mine ID", as_index=False).first()

    return pl.DataFrame(
        {
            "Unit ID": raw["GEM Mine ID"].astype("string"),
            "Latitude": pd.to_numeric(raw["Latitude"], errors="coerce"),
            "Longitude": pd.to_numeric(raw["Longitude"], errors="coerce"),
        }
    )


def build_mine_population(gem_path: Path, score_path: Path) -> pl.DataFrame:
    """Same mine population as gem_coal_satellite_favourability.py's country
    tables (non-lignite, Operating, with both a production figure and a
    satellite score), with Latitude/Longitude attached."""
    mine_production = build_mine_production(gem_path)
    scores = load_satellite_scores(score_path)

    merged = mine_production.join(scores, on="Unit ID", how="left").filter(
        pl.col("year_frac_0").is_not_null() & pl.col("Most Recent Coal Output (Annual, Mt)").is_not_null()
    ).select("Unit ID", "Country", "Mine Name", "Status", "Most Recent Coal Output (Annual, Mt)")

    locations = load_mine_locations(gem_path)
    merged = merged.join(locations, on="Unit ID", how="left")

    missing_location = merged.filter(pl.col("Latitude").is_null() | pl.col("Longitude").is_null())
    if missing_location.height:
        log(f"  dropping {missing_location.height} mines with no GEM Latitude/Longitude")
        merged = merged.filter(pl.col("Latitude").is_not_null() & pl.col("Longitude").is_not_null())

    return merged


def load_plume_coordinates(plumes_path: Path) -> tuple[np.ndarray, np.ndarray]:
    plumes = pl.read_csv(plumes_path, columns=["latitude", "longitude"]).drop_nulls()
    return plumes["latitude"].to_numpy(), plumes["longitude"].to_numpy()


def nearest_plume_distance_km(
    mine_lat: np.ndarray, mine_lon: np.ndarray, plume_lat: np.ndarray, plume_lon: np.ndarray
) -> np.ndarray:
    """Great-circle distance from each mine to its closest plume, chunked over
    mines so the mines x plumes distance matrix never fully materialises."""
    mine_lat_r = np.radians(mine_lat)
    mine_lon_r = np.radians(mine_lon)
    plume_lat_r = np.radians(plume_lat)
    plume_lon_r = np.radians(plume_lon)

    out = np.empty(mine_lat.shape, dtype=float)
    for start in range(0, len(mine_lat_r), MINE_CHUNK_SIZE):
        end = start + MINE_CHUNK_SIZE
        dlat = plume_lat_r[np.newaxis, :] - mine_lat_r[start:end, np.newaxis]
        dlon = plume_lon_r[np.newaxis, :] - mine_lon_r[start:end, np.newaxis]
        a = (
            np.sin(dlat / 2) ** 2
            + np.cos(mine_lat_r[start:end, np.newaxis]) * np.cos(plume_lat_r[np.newaxis, :]) * np.sin(dlon / 2) ** 2
        )
        dist = 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
        out[start:end] = dist.min(axis=1)
    return out


def build_country_tables(mines: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    by_country = (
        mines.group_by("Country")
        .agg(
            pl.len().alias("n_mines"),
            pl.col("detected").sum().alias("n_mines_detected"),
            pl.col("Most Recent Coal Output (Annual, Mt)").sum().alias("production_total_mt"),
            pl.col("Most Recent Coal Output (Annual, Mt)").filter(pl.col("detected")).sum().alias("production_detected_mt"),
        )
        .with_columns(pl.col("production_detected_mt").fill_null(0.0))
        .sort("production_total_mt", descending=True)
    )

    world = by_country.select(
        pl.lit("World").alias("Country"),
        pl.col("n_mines").sum(),
        pl.col("n_mines_detected").sum(),
        pl.col("production_total_mt").sum(),
        pl.col("production_detected_mt").sum(),
    )
    by_country = pl.concat([by_country, world], how="vertical")

    detected = by_country.select(
        "Country",
        "n_mines",
        "n_mines_detected",
        "production_total_mt",
        "production_detected_mt",
        (100 * pl.col("n_mines_detected") / pl.col("n_mines")).alias(
            "% Coal Mines Currently Detected by Satellite"
        ),
        (100 * pl.col("production_detected_mt") / pl.col("production_total_mt")).alias(
            "% Coal Production Currently Detected by Satellite"
        ),
    )
    return by_country, detected


def main() -> None:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--gem",
        type=Path,
        default=here.parent / "emissions" / "Global Coal Mine Tracker, May 2026__.xlsx",
    )
    ap.add_argument("--scores", type=Path, default=here / DEFAULT_SCORE_FILE)
    ap.add_argument("--plumes", type=Path, default=here / "merged_satellite_plumes_coal.csv")
    ap.add_argument("--threshold-km", type=float, default=DEFAULT_THRESHOLD_KM)
    ap.add_argument("--outdir", type=Path, default=here)
    args = ap.parse_args()

    if not args.plumes.exists():
        raise SystemExit(f"{args.plumes} not found - run satellite_plume_dataset.py first")

    log(f"reading GEM workbook  {args.gem}")
    log(f"reading scores        {args.scores}")
    mines = build_mine_population(args.gem, args.scores)
    log(f"  {mines.height} mines in the favourability population")

    log(f"reading plumes        {args.plumes}")
    plume_lat, plume_lon = load_plume_coordinates(args.plumes)
    log(f"  {len(plume_lat)} coal-sector plume detections")

    distance_km = nearest_plume_distance_km(
        mines["Latitude"].to_numpy(), mines["Longitude"].to_numpy(), plume_lat, plume_lon
    )
    mines = mines.with_columns(
        pl.Series("nearest_plume_distance_km", distance_km),
        (pl.Series("nearest_plume_distance_km", distance_km) <= args.threshold_km).alias("detected"),
    )

    by_country, detected = build_country_tables(mines)

    args.outdir.mkdir(parents=True, exist_ok=True)
    mine_out = args.outdir / "gem_coal_mine_detection_status.csv"
    country_out = args.outdir / "gem_coal_production_per_country_detected.csv"

    mines.drop("Latitude", "Longitude").write_csv(mine_out)
    detected.write_csv(country_out)

    n_countries = detected.height - 1  # exclude World
    log("")
    log(f"wrote {mines.height} rows -> {mine_out}")
    log(f"wrote {detected.height} rows ({n_countries} countries + World) -> {country_out}")
    world = detected.filter(pl.col("Country") == "World")
    world_pct = world["% Coal Production Currently Detected by Satellite"][0]
    world_mine_pct = world["% Coal Mines Currently Detected by Satellite"][0]
    log(f"  World detected (production-weighted): {world_pct:.2f}%")
    log(f"  World detected (mine count):          {world_mine_pct:.2f}%")
    log(f"  (threshold: {args.threshold_km:.1f} km)")


if __name__ == "__main__":
    main()
