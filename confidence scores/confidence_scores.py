#!/usr/bin/env python3
"""
confidence_scores.py - per-country UNFCCC reporting tier and confidence score
for coal mine methane (CMM) emissions data.

This is the standalone, self-contained equivalent of cmm-data-tool's
    Other/Confidence_csv.ipynb
reading from a copy of its source input (data/Confidence Scores.csv) kept
alongside this script rather than depending on that repo being checked out
next to it.

The source CSV is a manually-maintained table, not derived from anything
else in either repo - there is no script in cmm-data-tool (or anywhere else
checked) that computes TIER, CRITERIA1/2/3, or SCORE from raw UNFCCC/CRT
data; this script only reshapes an already-filled-in input, same as the
notebook did. If that input needs updating in future (new countries, revised
tiers), edit data/Confidence Scores.csv directly - there's nothing upstream
of it to re-run.

Columns, in the source file:
  COUNTRY, CODE, SCORE, CRITERIA1, CRITERIA2, CRITERIA3, TIER, CONFIDENCE
  * TIER       - the UNFCCC reporting tier(s) the country's CMM/coal sector
                 inventory uses (e.g. '1', '2,3', or 'Not reported').
  * CRITERIA1-3 and SCORE - a 0-2 rating against three unspecified criteria
                 and their sum; SCORE already equals CRITERIA1+CRITERIA2+
                 CRITERIA3 in the source data, so it's carried through as-is
                 rather than recomputed.
  * CONFIDENCE - a text label derived from SCORE (e.g. 'low confidence',
                 'medium confidence') - dropped in the output below, same as
                 the notebook; the numeric CONFIDENCE SCORE is kept instead.

Output (written to --outdir, default: this folder):
  * confidence_scores.csv
        one row per country, sorted by COUNTRY: COUNTRY, COUNTRY CODE, TIER,
        CRITERIA 1, CRITERIA 2, CRITERIA 3, CONFIDENCE SCORE.

Usage:
    python confidence_scores.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

DEFAULT_INPUT = Path("data") / "Confidence Scores.csv"

COLUMN_RENAMES = {
    "CODE": "COUNTRY CODE",
    "CRITERIA1": "CRITERIA 1",
    "CRITERIA2": "CRITERIA 2",
    "CRITERIA3": "CRITERIA 3",
    "SCORE": "CONFIDENCE SCORE",
}
COLUMNS_TO_KEEP = ["COUNTRY", "CODE", "TIER", "CRITERIA1", "CRITERIA2", "CRITERIA3", "SCORE"]


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def build_confidence_scores(input_path: Path) -> pl.DataFrame:
    df = pl.read_csv(input_path)
    missing = set(COLUMNS_TO_KEEP) - set(df.columns)
    if missing:
        raise SystemExit(f"{input_path.name}: missing columns {sorted(missing)}")

    return (
        df.select(COLUMNS_TO_KEEP)
        .sort("COUNTRY")
        .rename(COLUMN_RENAMES)
    )


def main() -> None:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=here / DEFAULT_INPUT)
    ap.add_argument("--outdir", type=Path, default=here)
    args = ap.parse_args()

    log(f"reading {args.input}")
    out = build_confidence_scores(args.input)

    args.outdir.mkdir(parents=True, exist_ok=True)
    out_path = args.outdir / "confidence_scores.csv"
    out.write_csv(out_path)

    log(f"wrote {out.height} rows -> {out_path}")


if __name__ == "__main__":
    main()
