#!/usr/bin/env python3
"""
crt_flaring_timeline.py - pull the full 1990.. time series of one Table1.B.1
row across all CRT submission rounds (default: everything under "G:/My Drive/
CRT Tables"), round-priority merged.

A CRT zip holds one .xlsx per inventory year, so a single round already spans
the whole timeline - no DI splice needed, and unlike crt_methane.py/
crt_amm.py there is no floor_year: every round is read in full (1990..
submission-2) and merged, oldest round preferred, exactly the way
crt_methane.py merges its own CRT rounds. Walks every workbook, grabs the
requested category row (default 1.B.1.a.i.4 "Flaring of drained methane or
conversion of methane to CO2"), and writes one row per (country, year) with
every value column from Table1.B.1.

ROUND PRIORITY
    Rounds are discovered under --crt-root (crt_methane.py's discover_rounds:
    subfolders named starting with a 4-digit year, oldest first) and merged
    whole-row per (COUNTRY_CODE, YEAR): the earliest round that has a row for
    that country-year wins it entirely, a later round only fills a
    country-year no earlier round covers at all. With the current data that
    means 2024 preferred, 2025 fills what 2024 doesn't have, 2026 fills what
    neither does. Unlike crt_methane.py's column-wise coalesce merge, this
    keeps a winning round's entire row together (all value columns from the
    same submitted workbook), since splicing e.g. one round's CH4 figure with
    another round's CO2 figure for the same country-year would misrepresent a
    single workbook row as two.

DRAFT (V0) FALLBACK
    Same policy as crt_methane.py/crt_amm.py: a CRT workbook whose filename
    contains "V0" is an unreviewed draft/interim submission, skipped by
    default when a non-draft submission covers the same (country, year) in
    any round. Every round is parsed once with drafts kept (tagged, not
    dropped); a draft-free round-priority merge and a draft-inclusive one are
    both built, and whatever (country, year) pairs exist only in the
    draft-inclusive version - i.e. no non-draft submission, in any round,
    covers them at all - are added back with DRAFT_FLAG=true. A draft can
    only fill a total gap this way; it can never outrank or replace a genuine
    submission. Pass --no-draft-fallback to drop V0 outright instead.

Reuses the vendored extractors in crt_methane.py (discover_rounds, read_round
-> parse_unfccc -> Table1.B.1, skiprows=5). Annex I only, because that is who
files CRTs (in practice an increasing number of non-Annex parties do too -
see crt_amm.py - and they flow through here the same way; nothing here
filters by party).

    pip install pandas polars openpyxl

USAGE
    python crt_flaring_timeline.py --crt-root "G:/My Drive/CRT Tables"
        -> flaring_timeline.csv  (default --category 1.B.1.a.i.4)
    python crt_flaring_timeline.py --crt-root ./crt_data --long --out flaring_timeline_long.csv
    python crt_flaring_timeline.py --crt-root ./crt_data --category 1.B.1.a.i.3 --out amm_timeline.csv

NOTES
  * --category matches the GREENHOUSE GAS SOURCE AND SINK CATEGORIES cell after
    lower-casing and stripping spaces, anchored at the start, so "1.B.1.a.i.4"
    hits "1.B.1.a.i.4. Flaring ..." but not the footnote that mentions it.
  * CRT notation keys (NO, NE, NA, IE, C, NO,NE ...) become null in the numeric
    columns; the raw EMISSIONS CH4 cell is kept verbatim in CH4_RAW so "NO"
    (not occurring) stays distinguishable from an empty cell.
  * DRAFT_FLAG is true only for a (country, year) whose winning row came from
    the V0-draft fallback - a value present via any non-draft submission
    always keeps DRAFT_FLAG=false, even if that submission happens to be a
    later round than an unused draft.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
import polars as pl

sys.path.insert(0, str(Path(__file__).parent.parent / "emissions"))
import crt_methane as cm  # noqa: E402

DEFAULT_CATEGORY = "1.B.1.a.i.4"
CAT_COL = "GREENHOUSE GAS SOURCE AND SINK CATEGORIES"
NON_VALUE = {"", CAT_COL, "filename", "year", "country_code", "_is_draft"}

# flattened Table1.B.1 header -> tidy output name
VALUE_RENAME = {
    "ACTIVITY DATA Amount of fuel produced (Mt)": "ACTIVITY_DATA",
    "IMPLIED EMISSION FACTORS CH4 (kg/t)": "IEF_CH4",
    "IMPLIED EMISSION FACTORS CO2 (kg/t)": "IEF_CO2",
    "EMISSIONS CH4 (kt)": "EMISSIONS_CH4_KT",
    "EMISSIONS CO2 (kt)": "EMISSIONS_CO2_KT",
    "RECOVERY/FLARING CH4 (kt)": "RECOVERY_FLARING_CH4_KT",
    "RECOVERY/FLARING CO2 (kt)": "RECOVERY_FLARING_CO2_KT",
}


def _num(x: object) -> float | None:
    """CRT cell -> float, or None for blanks and notation keys (NO/NE/NA/IE/C)."""
    s = str(x).strip().replace(",", "")
    if s in ("", "nan", "NaN", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def extract_category(raw: pd.DataFrame, category: str) -> pl.DataFrame:
    """One round's raw frame (from cm.read_round(..., skip_drafts=False)) ->
    tidy rows for `category`, DRAFT_FLAG carried through from the '_is_draft'
    column. Empty frame (not an error) if the round has no matching row -
    round coverage of a category can genuinely vary."""
    if CAT_COL not in raw.columns:
        sys.exit(f"no {CAT_COL!r} column - is this really a folder of CRT zips?")

    key = re.sub(r"\s+", "", category).lower()
    cat_norm = raw[CAT_COL].astype(str).str.replace(r"\s+", "", regex=True).str.lower()
    rows = raw[cat_norm.str.startswith(key)].copy()
    if rows.empty:
        return pl.DataFrame()

    has_draft_col = "_is_draft" in rows.columns
    # column_N is _dedupe_columns's placeholder for a repeated flattened header
    # (crt_methane.py's clean_unfccc drops these too, for the same reason) -
    # without this, reading every round/draft surfaces far more of them than
    # the original single-round/non-draft-only version ever hit, as empty
    # noise columns in the output.
    value_cols = [
        c for c in rows.columns if c not in NON_VALUE and not re.fullmatch(r"column_\d+", c)
    ]
    keep_cols = ["country_code", "year", *value_cols]
    if has_draft_col:
        keep_cols.append("_is_draft")
    df = pl.from_pandas(rows[keep_cols].astype(str))

    tidy = df.select(
        pl.col("country_code").alias("COUNTRY_CODE"),
        pl.col("year").cast(pl.Int64, strict=False).alias("YEAR"),
        pl.col("EMISSIONS CH4 (kt)").alias("CH4_RAW")
        if "EMISSIONS CH4 (kt)" in value_cols
        else pl.lit(None).alias("CH4_RAW"),
        *[
            pl.col(src)
            .map_elements(_num, return_dtype=pl.Float64)
            .alias(VALUE_RENAME.get(src, src.upper().replace(" ", "_")))
            for src in value_cols
        ],
        (pl.col("_is_draft") == "True").alias("DRAFT_FLAG")
        if has_draft_col
        else pl.lit(False).alias("DRAFT_FLAG"),
    )

    tidy = tidy.filter(pl.col("COUNTRY_CODE").is_not_null() & (pl.col("COUNTRY_CODE") != "None"))

    # a handful of workbooks (malformed/atypical enough that parse_unfccc's
    # filename regex misses the year - the same rows this session found also
    # carry mangled 'ENE'/'Fin'-style garbage in COUNTRY_CODE, though most hit
    # otherwise-legitimate codes too) can't be placed on the timeline at all
    # without a year - drop rather than let a null join key reach the
    # round-priority merge.
    bad_year = tidy.filter(pl.col("YEAR").is_null())
    if len(bad_year):
        cm.log(
            f"  ! {len(bad_year)} row(s) with no parseable YEAR, dropped: "
            f"{sorted(set(bad_year['COUNTRY_CODE'].to_list()))}"
        )
        tidy = tidy.filter(pl.col("YEAR").is_not_null())

    return (
        tidy
        # a duplicate (country, year) within one round (e.g. a browser "(2).zip"
        # copy) prefers the non-draft copy if both exist.
        .sort(["COUNTRY_CODE", "YEAR", "DRAFT_FLAG"])
        .unique(subset=["COUNTRY_CODE", "YEAR"], keep="first")
        .sort(["COUNTRY_CODE", "YEAR"])
    )


def _prioritize_rounds(round_frames: list[pl.DataFrame]) -> pl.DataFrame:
    """Whole-row round-priority merge: `round_frames` in priority order
    (oldest/most-preferred first); the first frame with a row for a given
    (COUNTRY_CODE, YEAR) wins it entirely. diagonal_relaxed handles rounds
    whose Table1.B.1 template carries a slightly different value-column set."""
    tagged = [f.with_columns(pl.lit(i).alias("_round_rank")) for i, f in enumerate(round_frames)]
    return (
        pl.concat(tagged, how="diagonal_relaxed")
        .sort(["COUNTRY_CODE", "YEAR", "_round_rank"])
        .unique(subset=["COUNTRY_CODE", "YEAR"], keep="first")
        .drop("_round_rank")
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--crt-root",
        type=Path,
        default=Path("G:/My Drive/CRT Tables"),
        help="root dir of <year>/ CRT-round subfolders, same layout crt_methane.py uses "
        "(each round already spans the whole 1990..submission-2 timeline; rounds are "
        "merged oldest-first - 2024 preferred, 2025/2026 only fill gaps 2024 doesn't cover)",
    )
    ap.add_argument(
        "--category", default=DEFAULT_CATEGORY, help=f"row code (default {DEFAULT_CATEGORY})"
    )
    ap.add_argument("--out", type=Path, default=Path("flaring_timeline.csv"))
    ap.add_argument(
        "--long", action="store_true", help="tidy long output instead of one row per (country, year)"
    )
    ap.add_argument("--recurse-zips", action="store_true", help="descend one level into nested .zip members")
    ap.add_argument(
        "--no-draft-fallback",
        action="store_true",
        help="don't fall back to a V0 draft submission for a (country, year) with no "
        "non-draft CRT coverage in any round - drop V0 outright instead (old behaviour)",
    )
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    rounds = cm.discover_rounds(args.crt_root)
    cm.log(f"CRT rounds (oldest first, most preferred first): {[r.name for r in rounds]}")

    round_frames: list[pl.DataFrame] = []
    for r in rounds:
        raw = cm.read_round(r, args.recurse_zips, 0, skip_drafts=False)
        tidy_round = extract_category(raw, args.category)
        if tidy_round.is_empty():
            cm.log(f"  ! category {args.category!r} not found in any workbook under {r}")
            continue
        round_frames.append(tidy_round)
        cm.log(
            f"  {r.name}: {tidy_round.height} row(s), "
            f"{tidy_round['COUNTRY_CODE'].n_unique()} countries"
        )

    if not round_frames:
        sys.exit(f"category {args.category!r} not found in any round under {args.crt_root}")

    non_draft_rounds = [f.filter(~pl.col("DRAFT_FLAG")).drop("DRAFT_FLAG") for f in round_frames]
    tidy = _prioritize_rounds(non_draft_rounds)

    if not args.no_draft_fallback:
        # everything achievable if drafts count too, same round-priority merge.
        # Any (COUNTRY_CODE, YEAR) that appears here but not in the draft-free
        # `tidy` above has zero non-draft coverage anywhere, so it can only
        # have come from a draft - safe to add back without ever outranking a
        # genuine submission.
        with_draft_rounds = [f.drop("DRAFT_FLAG") for f in round_frames]
        tidy_with_drafts = _prioritize_rounds(with_draft_rounds)
        draft_only = tidy_with_drafts.join(tidy, on=["COUNTRY_CODE", "YEAR"], how="anti")
        if len(draft_only):
            cm.log(
                f"  ! {len(draft_only)} row(s) have no non-draft CRT submission "
                "in any round, kept as a flagged draft fallback: "
                f"{sorted(set(draft_only['COUNTRY_CODE'].to_list()))}"
            )
            draft_only = draft_only.with_columns(pl.lit(True).alias("DRAFT_FLAG"))
            tidy = pl.concat(
                [tidy.with_columns(pl.lit(False).alias("DRAFT_FLAG")), draft_only],
                how="diagonal_relaxed",
            )
        else:
            tidy = tidy.with_columns(pl.lit(False).alias("DRAFT_FLAG"))
    else:
        tidy = tidy.with_columns(pl.lit(False).alias("DRAFT_FLAG"))

    tidy = tidy.sort(["COUNTRY_CODE", "YEAR"])

    out_cols = [c for c in tidy.columns if c not in ("COUNTRY_CODE", "YEAR", "CH4_RAW", "DRAFT_FLAG")]
    # belt-and-braces alongside extract_category's column_N drop: a value
    # column that came out entirely empty across every round merged together
    # is a mangled/duplicate header artifact, not real data - drop it so it
    # doesn't pollute the output schema.
    empty_cols = [c for c in out_cols if tidy[c].null_count() == tidy.height]
    if empty_cols:
        cm.log(f"  ! dropping {len(empty_cols)} all-null value column(s): {empty_cols}")
        tidy = tidy.drop(empty_cols)
        out_cols = [c for c in out_cols if c not in empty_cols]
    if args.long:
        tidy = (
            tidy.unpivot(
                index=["COUNTRY_CODE", "YEAR", "DRAFT_FLAG"],
                on=out_cols,
                variable_name="MEASURE",
                value_name="VALUE",
            )
            .drop_nulls("VALUE")
            .sort(["COUNTRY_CODE", "YEAR", "MEASURE"])
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tidy.write_csv(args.out)

    reported = (
        tidy.filter(pl.col("VALUE").is_not_null())["COUNTRY_CODE"].unique().sort().to_list()
        if args.long
        else tidy.filter(
            pl.any_horizontal(pl.col(c).is_not_null() for c in out_cols)
        )["COUNTRY_CODE"].unique().sort().to_list()
    )
    cm.log(f"wrote {len(tidy)} rows -> {args.out}")
    cm.log(f"  category  : {args.category}")
    cm.log(f"  countries : {tidy['COUNTRY_CODE'].n_unique()} total, {len(reported)} with a numeric value")
    cm.log(f"  years     : {tidy['YEAR'].min()}-{tidy['YEAR'].max()}")
    cm.log(f"  reporting : {reported}")


if __name__ == "__main__":
    main()
