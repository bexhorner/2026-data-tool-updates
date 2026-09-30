#!/usr/bin/env python3
"""
crt_methane_combine_non_annex.py - refresh the non-Annex I half of
crt_methane_ch4.csv from a hand-maintained Google Sheet, without re-running
the (slow) CRT/DI pull in crt_methane.py.

crt_methane_ch4.csv holds two halves, told apart by ANNEX_FLAG:
  - ANNEX_FLAG=true   CRT + DI historic (built by crt_methane.py's main pull)
  - ANNEX_FLAG=false  the hand-maintained non-Annex sheet
    (crt_methane.py::load_non_annex_sheet)

This script leaves the ANNEX_FLAG=true half untouched and replaces the
ANNEX_FLAG=false half with a fresh pull of --sheet, reusing
crt_methane.py::load_non_annex_sheet so the melt / name->ISO3 / dedupe logic
stays identical to a full crt_methane.py run. --sheet defaults to the
"UNFCCC Non-Annex CMM" sheet.

Usage:
    python crt_methane_combine_non_annex.py
    python crt_methane_combine_non_annex.py --in crt_methane_ch4.csv \
        --sheet 1G-r_7ZvI5p-YU201tist4-RjnPLJg2pxnnnMFGt4iis --tab Sheet1 \
        --out crt_methane_ch4.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
from crt_methane import OUTPUT_COLUMNS, load_non_annex_sheet, log  # noqa: E402

DEFAULT_SHEET = "1G-r_7ZvI5p-YU201tist4-RjnPLJg2pxnnnMFGt4iis"
DEFAULT_TAB = "Sheet1"


def main() -> None:
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--in", dest="inp", type=Path, default=here / "crt_methane_ch4.csv")
    ap.add_argument("--sheet", default=DEFAULT_SHEET, help="Google Sheet URL or bare doc id")
    ap.add_argument("--tab", default=DEFAULT_TAB, help="tab name within --sheet")
    ap.add_argument(
        "--category-label",
        default="1.B.1.a Coal mining and handling",
        help="normalised SOURCE label written for the sheet rows",
    )
    ap.add_argument("--out", type=Path, default=here / "crt_methane_ch4.csv")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    log(f"reading existing     {args.inp}")
    existing = pl.read_csv(args.inp)
    missing = {"COUNTRY_CODE", "YEAR", "EMISSIONS_CH4_KT", "ANNEX_FLAG"} - set(existing.columns)
    if missing:
        raise SystemExit(f"{args.inp.name}: missing columns {sorted(missing)}")

    annex = existing.filter(pl.col("ANNEX_FLAG")).select([*OUTPUT_COLUMNS, "ANNEX_FLAG"])
    log(f"  keeping {annex.height} Annex-I row(s) (CRT + DI) unchanged")

    non_annex = load_non_annex_sheet(
        args.sheet, args.tab, args.category_label, args.debug
    ).with_columns(pl.lit(False).alias("ANNEX_FLAG"))
    log(
        f"  pulled {non_annex.height} non-Annex row(s) from the sheet, "
        f"{non_annex['COUNTRY_CODE'].n_unique()} countries"
    )

    out = pl.concat([annex, non_annex], how="vertical_relaxed").sort(["COUNTRY_CODE", "YEAR"])

    before = existing.height
    if out.height != before:
        log(f"  row count changed: {before} -> {out.height}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.write_csv(args.out)
    log(f"wrote {out.height} rows -> {args.out}")


if __name__ == "__main__":
    main()
