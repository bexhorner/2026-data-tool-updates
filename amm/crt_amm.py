#!/usr/bin/env python3
"""
crt_amm.py - build an abandoned-underground-mines CH4 (1.B.1.a.i.3) time
series from UNFCCC using the same CRT + DI method as crt_methane.py.

di_amm_long.csv / di_amm_wide.csv (built by di_pull.py) only reach 2020/2021
because they read the DI alone, and the DI's own coverage stops there for
most parties. This script mirrors crt_methane.py's two-source approach for
the sibling category 1.B.1.a.i.3 "Abandoned underground mines" instead of
the parent 1.B.1.a "Coal mining and handling":

    < floor year (default 2022)   UNFCCC DI, Annex I only - as di_amm_long.csv
                                   already did (non-Annex parties don't report
                                   this subcategory in the DI at all)
    >= floor year                 UNFCCC CRT, parsed the same way as
                                   crt_methane.py (Table1.B.1, same workbook -
                                   just a different row of it), NOT limited to
                                   Annex I - whichever parties have actually
                                   submitted a CRT-format workbook are
                                   included (an increasing number of non-Annex
                                   parties now do, under the Paris Agreement's
                                   Enhanced Transparency Framework - e.g.
                                   Colombia, Brazil, Argentina, Mongolia,
                                   Georgia, Serbia all already show up here)

Row label caveat: unlike the parent category ('1. B. 1. a. Coal mining and
handling', spaced), the abandoned-mines row is written unspaced in the raw
CRT cells ('1.B.1.a.i.3. Abandoned underground mines (number of mines)'), and
that parenthetical is just how the cell is labelled - the EMISSIONS CH4 (kt)
column next to it is a normal emissions figure, not a mine count. So
clean_amm() matches on the row text ('abandoned underground mines',
case-insensitive) rather than the numeric prefix, the same way di_pull.py
matches DI category labels by text to span both eras' numbering
('1.B.1.a.1.iii' vs '1.B.1.a.i.3'). Confirmed against real 2026-round
workbooks (AUS/DEU/POL): exactly one row matches per country-year, values in
the same ballpark as the existing di_amm_long.csv series.

DRAFT (V0) FALLBACK
    crt_methane.py's draft policy ([[crt-methane-draft-policy]]) skips any
    submission with "V0" in its filename outright. That's fine when a party
    has a later, non-draft submission to fall back on - but some parties'
    *only* CRT-format submission anywhere is a V0 draft (e.g. China's only
    entry across the 2024/2025/2026 rounds is
    'CHN-CRT-2024-V0.11-...started.zip'), and for those, skipping V0 means
    zero AMM coverage at all rather than a lower-confidence number.

    So this script parses every round twice as cheaply as possible: once
    with drafts included (skip_drafts=False on crt_methane.py's read_round,
    which tags each row '_is_draft' instead of dropping V0 - the actual
    zip/tar decompression only happens once per round either way) to get
    the full (country, year) coverage achievable, and once with drafts
    filtered back out to get the same non-draft-only series crt_methane.py
    would produce. Whatever (country, year) pairs exist only in the
    draft-inclusive version - i.e. no non-draft CRT submission, in any
    round, covers them at all - are added back as a flagged fallback, SOURCE
    suffixed " (unreviewed draft submission)" (FILENAME already names the
    underlying V0/_started workbook). A draft can only fill a total gap this
    way; it can never outrank or replace a genuine submission, regardless of
    which round each happens to sit in. Pass --no-draft-fallback to turn
    this off and reproduce crt_methane.py's plain V0-skip behaviour.

DI<->CRT BOUNDARY GAP-FILL
    The two sources don't always meet cleanly at floor_year: the DI Zenodo
    mirror's per-country coverage sometimes stops a year or two before
    floor_year (e.g. Australia's series ends 2020; CRT starts 2022; 2021 is a
    hole neither source fills on its own - found this by hand, then checked
    for it generally). A CRT submission usually carries the party's full
    1990-current series in one workbook set though, so the missing year is
    often right there, just excluded by the plain >=floor_year parse.

    So: find every country whose DI coverage ends before floor_year - 1
    (cheap, DI-only), then re-scan CRT for just those countries with the
    floor dropped to 1990 - read_round's new `countries=` filter skips every
    non-matching zip/tar member before it's even opened, so this stays cheap
    no matter how far back it has to look or how large the bundle is. Any
    (country, year) recovered this way is restricted to years DI genuinely
    lacks (never overwrites a real DI figure, never touches >=floor_year -
    that's the main CRT pass's job) and SOURCE-suffixed
    " (CRT, pre-2022 DI gap-fill)" for transparency. Confirmed against real
    data: Australia 2021 = 53.3 kt (a genuine reported figure); Cyprus 2021 =
    "NO" (consistent with Cyprus's other years). Pass --no-gap-fill to turn
    this off.

NON-ANNEX SHEET
    Mirrors crt_methane.py's --non-annex-sheet mechanism (same
    load_non_annex_sheet: wide Country-by-year sheet -> melt -> name->ISO3 ->
    tidy long frame), pointed at "UNFCCC Non-Annex AMM not from CRT table"
    instead of the CH4 pipeline's "UNFCCC Non-Annex CMM" sheet - a
    hand-maintained source for non-Annex abandoned-mines figures that predate
    or fall outside the CRT-format submissions this script already picks up
    organically (see above; the CRT half was never Annex-restricted, this
    sheet is for whatever it still doesn't reach). Pass --no-non-annex-sheet
    to skip it.

    Adds an ANNEX_FLAG column, but NOT with crt_methane.py's meaning. There,
    ANNEX_FLAG is really "which half of the pipeline" (True = CRT/DI pull,
    False = hand sheet) rather than a real party check - harmless for the CH4
    pipeline's own dedup logic (crt_methane_gapfill.py explicitly relies on
    that "which half" ordering as its tie-breaker) but actively misleading
    here, since the CRT half already carries plenty of non-Annex parties
    (Serbia included) that would wrongly read True. So here ANNEX_FLAG is a
    real lookup against crt_methane.py's own _ANNEX_ONE party list, applied
    uniformly to every row regardless of source half. One consequence: a
    country-year that exists via both CRT and the hand sheet (e.g. Serbia
    2022/2023 - a known duplication pattern, see crt-methane-data-layout) no
    longer has different ANNEX_FLAG values to tell the two rows apart; a
    future dedup step for this file needs a different tie-breaker (FILENAME,
    say) than crt_methane_gapfill.py's ANNEX_FLAG-based one.

IEA SHEET + SOURCE COLUMN
    A second Google Sheet, "IEA AMM" (id
    1hstKeDhR0oZ2-S5oA5NVqHQOYzVL6gBJpTm999lqFyU), adds an IEA-estimated
    figure alongside the UNFCCC-reported ones. Unlike the non-Annex sheet,
    it's a flat country -> single-value snapshot (no year column, no melt
    needed) - just "country","AMM (kt)". Assumed to be 2025 data
    (--iea-year), matching this project's existing convention for IEA
    releases (see crt_methane_gapfill.py's "IEA 2026" extra source, which is
    likewise one year behind its release name) - the sheet itself carries no
    year, so this is a judgement call, not a certainty; override with
    --iea-year if wrong. "World" and regional rollups ("Other EU17
    countries" etc.) don't resolve to an ISO3 code via country_name_to_code
    and are dropped (logged), same as an unmatched name anywhere else in
    this pipeline; the other 17 rows are real countries. Added as extra
    rows, not a gap-filler - a country can now carry both a UNFCCC row and
    an IEA row for the same year, same as crt_methane_gapfill.py's GEM/IEA
    extras for the CH4 pipeline. Pass --no-iea to skip it.

    Per the user's request, SOURCE is simplified to exactly two values across
    the whole file: "UNFCCC" for every CRT/DI/non-Annex-sheet row, "IEA" for
    the new sheet's rows - not the detailed category label or the draft-
    fallback/gap-fill annotations SOURCE used to carry (those are still
    fully recoverable from FILENAME: the actual workbook name reveals V0/
    draft status, "historic" means DI, the non-Annex sheet's constant
    filename is unambiguous, and the gap-fill/IEA rows get their own
    distinct FILENAME values too).

Reuses crt_methane.py's CRT-round discovery/parsing, DI-pull, non-Annex
sheet and name-matching plumbing verbatim (discover_rounds, read_round,
coalesce_join, load_di_historic, load_non_annex_sheet,
country_name_to_code) so extraction stays consistent throughout. Differs
only in: the CRT row filter (clean_amm, replacing clean_unfccc), the DI
category label, the round-priority merge (merge_amm_rounds, a
null-safe-equality fix over merge_historic_unfccc - see its docstring)
needed to carry FILENAME correctly once draft rows are in the mix, and its
own IEA-sheet loader (load_iea_amm, no melt needed - the sheet has no year
axis to begin with).

Usage:
    python crt_amm.py --crt-root "G:\\My Drive\\CRT Tables"
    python crt_amm.py --crt-root "G:\\My Drive\\CRT Tables" --di-source zenodo --out crt_amm_ch4.csv
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from functools import reduce
from pathlib import Path

import pandas as pd
import polars as pl

sys.path.insert(0, str(Path(__file__).parent.parent / "emissions"))
from crt_methane import (  # noqa: E402
    OUTPUT_COLUMNS,
    _ANNEX_ONE,
    _debug_frame,
    _NON_ANNEX_FILENAME,
    coalesce_join,
    country_name_to_code,
    discover_rounds,
    load_di_historic,
    load_non_annex_sheet,
    log,
    read_round,
)

_AMM_SOURCE_LABEL = "UNFCCC"  # SOURCE value for every CRT/DI/non-Annex-sheet row
_AMM_ROW_TEXT = "abandoned underground mines"
_AMM_DI_CATEGORY = "Abandoned Underground Mines"
_AMM_NON_ANNEX_SHEET = "193s-y7YC-iUsUDvh-8uNV4xtLvbM91JApHKcr1eOIaE"  # "UNFCCC Non-Annex AMM not from CRT table"
_AMM_NON_ANNEX_TAB = "Sheet1"
_AMM_IEA_SHEET = "1hstKeDhR0oZ2-S5oA5NVqHQOYzVL6gBJpTm999lqFyU"  # "IEA AMM"
_AMM_IEA_TAB = "Sheet1"
_AMM_IEA_YEAR = 2025
_AMM_IEA_SOURCE_LABEL = "IEA"
_AMM_IEA_FILENAME = "IEA AMM (Google Sheet)"


def clean_amm(df_raw: pd.DataFrame) -> pl.DataFrame:
    """crt_methane.py::clean_unfccc, but keeps the abandoned-underground-mines
    row instead of the parent coal-mining-and-handling row. Also carries a
    DRAFT_FLAG through when df_raw has a '_is_draft' column (i.e. it came from
    read_round(..., skip_drafts=False)); otherwise DRAFT_FLAG is always False."""
    has_draft_col = "_is_draft" in df_raw.columns
    stacked = pl.from_pandas(df_raw.astype(str))
    unnamed = [c for c in stacked.columns if c == "" or re.fullmatch(r"column_\d+", c)]
    df = (
        stacked.drop(unnamed)
        .with_columns(pl.col("GREENHOUSE GAS SOURCE AND SINK CATEGORIES").str.strip_chars())
        .filter(
            pl.col("GREENHOUSE GAS SOURCE AND SINK CATEGORIES")
            .str.to_lowercase()
            .str.contains(_AMM_ROW_TEXT)
        )
        .select(
            pl.col("year").replace("None", None).cast(pl.Int64).alias("YEAR"),
            pl.col("country_code").replace("Com", "AUS").alias("COUNTRY_CODE"),
            pl.col("GREENHOUSE GAS SOURCE AND SINK CATEGORIES").alias("SOURCE"),
            # non-strict: besides the numeric-with-N notation keys (NA/NE/NO)
            # crt_methane.py's clean_unfccc pre-filters, drafts have surfaced
            # 'C' (confidential) too - cast whatever doesn't parse to null
            # rather than hard-failing on every CRT notation key there is.
            pl.col("EMISSIONS CH4 (kt)").cast(pl.Float64, strict=False).alias("EMISSIONS_CH4_KT"),
            pl.col("filename").alias("FILENAME"),
            (pl.col("_is_draft") == "True").alias("DRAFT_FLAG")
            if has_draft_col
            else pl.lit(False).alias("DRAFT_FLAG"),
        )
    )
    return df.with_columns(pl.col("COUNTRY_CODE").replace("None", "SWE"))


def merge_amm_rounds(df_priority: pl.DataFrame, df_supplementary: pl.DataFrame) -> pl.DataFrame:
    """crt_methane.py::merge_historic_unfccc, with one correctness fix over
    the original '==' comparison for picking which side's FILENAME to keep.

    Two null-related cases that plain '==' gets wrong (null == anything,
    including null == null, is null -> falsy -> falls through to whichever
    side is on the .otherwise() branch, right or wrong):

    1. The row exists on the priority side with a real, legitimately-null
       value (a reported 'NO'/non-numeric figure, not a gap) and no match at
       all on the supplementary side. FILENAME_right is then null too (never
       populated), so '==' silently drops the priority side's real filename.
       eq_missing (null == null -> True, unlike '==') fixes this.
    2. The mirror image: no match at all on the priority side, and the
       supplementary side's own value also happens to be null. Now eq_missing
       alone gets it wrong the other way - both sides null, eq_missing says
       True, so it would keep the (nonexistent) priority FILENAME instead of
       the real one on the supplementary side. Guarding with
       FILENAME.is_not_null() catches this: a priority row that doesn't
       really exist has no FILENAME either, so the guard sends it to
       FILENAME_right regardless of what eq_missing says.
    """
    joined = df_priority.join(
        df_supplementary, how="full", on=["YEAR", "COUNTRY_CODE", "SOURCE"], coalesce=True
    )
    return joined.select(
        "YEAR",
        "COUNTRY_CODE",
        "SOURCE",
        coalesce_join("EMISSIONS_CH4_KT"),
        pl.when(
            pl.col("EMISSIONS_CH4_KT").eq_missing(coalesce_join("EMISSIONS_CH4_KT"))
            & pl.col("FILENAME").is_not_null()
        )
        .then("FILENAME")
        .otherwise("FILENAME_right")
        .alias("FILENAME"),
    )


def clean_amm_round(raw: pd.DataFrame, floor_year: int, label: str, debug: bool) -> pl.DataFrame:
    """crt_methane.py::clean_round, calling clean_amm instead of clean_unfccc."""
    try:
        cleaned = clean_amm(raw)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"clean_amm failed ({type(exc).__name__}: {exc}).\n"
            "The CRT Table1.B.1 layout probably differs from what this expects - "
            "inspect a workbook and check the 'Abandoned underground mines' row "
            "and 'EMISSIONS CH4 (kt)' column."
        )
    if debug:
        _debug_frame("  raw cleaned", cleaned)
    null_cc = cleaned.filter(
        pl.col("COUNTRY_CODE").is_null() | (pl.col("COUNTRY_CODE") == "None")
    )
    if len(null_cc):
        log(
            f"  ! {len(null_cc)} row(s) with no COUNTRY_CODE (parse_unfccc "
            f"filename regex missed): {sorted(set(null_cc['FILENAME'].to_list()))[:5]}"
        )
    out = cleaned.filter(pl.col("YEAR") >= floor_year).with_columns(pl.lit(label).alias("SOURCE"))
    # crt_methane.py's clean_round only warns on a bad COUNTRY_CODE (the
    # floor-year filter happens to remove every case it has hit so far - all
    # pre-2022). Not guaranteed in general, and a null/unusable code has no
    # business flowing into the round-priority merge or the draft-fallback
    # diff below, so drop it here rather than let it surface as a crash there.
    unusable = out.filter(pl.col("COUNTRY_CODE").is_null() | (pl.col("COUNTRY_CODE") == "None"))
    if len(unusable):
        log(
            f"  ! dropping {len(unusable)} post-floor row(s) with no usable COUNTRY_CODE: "
            f"{sorted(set(unusable['FILENAME'].to_list()))[:5]}"
        )
        out = out.filter(
            pl.col("COUNTRY_CODE").is_not_null() & (pl.col("COUNTRY_CODE") != "None")
        )
    before = len(out)
    out = out.unique(subset=["YEAR", "COUNTRY_CODE", "SOURCE"], keep="first")
    if debug and len(out) != before:
        log(f"  dropped {before - len(out)} duplicate (year, country) row(s) in this round")
    return out


def load_iea_amm(
    sheet: str, tab: str, year: int, source_label: str, filename: str, debug: bool
) -> pl.DataFrame:
    """The 'IEA AMM' sheet -> tidy OUTPUT_COLUMNS frame. Flat country ->
    single value, no year axis (unlike load_non_annex_sheet's wide
    Country-by-year sheet), so no melt - just fetch, map names, assign the
    one given `year` to every row. 'World' and regional rollups don't
    resolve via country_name_to_code and are dropped, same handling
    load_non_annex_sheet already gives an unmatched name."""
    import requests

    doc = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", sheet)
    doc_id = doc.group(1) if doc else sheet.strip()
    url = f"https://docs.google.com/spreadsheets/d/{doc_id}/gviz/tq?tqx=out:csv&sheet={requests.utils.quote(tab)}"
    log(f"fetching the IEA AMM sheet ('{tab}' tab, gviz CSV) ...")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    if resp.text.lstrip().lower().startswith(("<!doctype", "<html")):
        sys.exit(
            f"the IEA sheet returned HTML, not CSV - {doc_id} is probably not shared "
            '"anyone with the link can view", or the tab name is wrong.'
        )
    wide = pd.read_csv(io.StringIO(resp.text))
    name_col, value_col = wide.columns[0], wide.columns[1]

    frame = pl.from_pandas(wide.astype(str)).select(
        pl.col(name_col).alias("_name"),
        pl.col(value_col)
        .str.replace_all(",", "")
        .str.strip_chars()
        .replace({"": None, "nan": None, "None": None})
        .cast(pl.Float64, strict=False)
        .alias("EMISSIONS_CH4_KT"),
    ).filter(pl.col("_name").str.strip_chars() != "")

    names = frame["_name"].unique().to_list()
    code_map = {n: country_name_to_code(n) for n in names}
    unresolved = sorted(n for n, c in code_map.items() if c is None)
    if unresolved:
        log(f"  ! IEA sheet: {len(unresolved)} name(s) not matched, dropped: {unresolved}")

    out = (
        frame.with_columns(
            pl.col("_name").replace_strict(code_map, default=None).alias("COUNTRY_CODE")
        )
        .drop_nulls(["COUNTRY_CODE", "EMISSIONS_CH4_KT"])
        .with_columns(
            pl.lit(year, dtype=pl.Int64).alias("YEAR"),
            pl.lit(source_label).alias("SOURCE"),
            pl.lit(filename).alias("FILENAME"),
        )
        .select(OUTPUT_COLUMNS)
    )
    if debug:
        _debug_frame("  IEA sheet", out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--crt-root",
        type=Path,
        default=Path("crt_data"),
        help="dir of <year>/ CRT-zip folders, same layout crt_methane.py uses (default ./crt_data)",
    )
    ap.add_argument("--out", type=Path, default=Path("crt_amm_ch4.csv"))
    ap.add_argument(
        "--floor-year",
        type=int,
        default=2022,
        help="CRT supplies this year onward; DI supplies everything before (default 2022)",
    )
    ap.add_argument(
        "--category-label",
        default=_AMM_SOURCE_LABEL,
        help="SOURCE label written for every CRT/DI/non-Annex-sheet row (default 'UNFCCC'; "
        "the IEA sheet's rows always get 'IEA', see --iea-source-label)",
    )
    ap.add_argument("--no-di", action="store_true", help="CRT only, skip the DI historic pull")
    ap.add_argument("--di-only", action="store_true", help="DI only, skip CRT parsing")
    ap.add_argument(
        "--di-source",
        choices=["api", "zenodo"],
        default="zenodo",
        help="'zenodo' (default) = the maintainers' Zenodo mirror, no WAF; "
        "'api' = live di.unfccc.int (Imperva-walled, needs --cookies)",
    )
    ap.add_argument("--keep-eua", action="store_true", help="do not remap DI party code EUA -> EU")
    ap.add_argument(
        "--cookies",
        default="",
        help="UNFCCC Incapsula cookies for the DI API query "
        '("visid_incap_...=...; incap_ses_...=..."); defaults to $UNFCCC_COOKIES',
    )
    ap.add_argument(
        "--recurse-zips",
        action="store_true",
        help="descend one level into nested .zip members (default: flat, as crt_methane.py)",
    )
    ap.add_argument(
        "--no-draft-fallback",
        action="store_true",
        help="don't fall back to a V0 draft submission for a (country, year) with no "
        "non-draft CRT coverage - reproduces crt_methane.py's plain V0-skip behaviour",
    )
    ap.add_argument(
        "--no-gap-fill",
        action="store_true",
        help="don't fill a DI<->CRT boundary gap (a country whose DI coverage stops "
        "before floor_year) from that country's own CRT submission, if it has one",
    )
    ap.add_argument(
        "--non-annex-sheet",
        default=_AMM_NON_ANNEX_SHEET,
        help="Google Sheet URL or bare doc id for the hand-maintained non-Annex AMM sheet "
        f"(default: {_AMM_NON_ANNEX_SHEET!r}, 'UNFCCC Non-Annex AMM not from CRT table')",
    )
    ap.add_argument(
        "--non-annex-tab",
        default=_AMM_NON_ANNEX_TAB,
        help=f"tab name within --non-annex-sheet (default {_AMM_NON_ANNEX_TAB!r})",
    )
    ap.add_argument(
        "--no-non-annex-sheet", action="store_true", help="skip the non-Annex hand-sheet pull"
    )
    ap.add_argument(
        "--iea-sheet",
        default=_AMM_IEA_SHEET,
        help=f"Google Sheet URL or bare doc id for the IEA AMM sheet (default: {_AMM_IEA_SHEET!r})",
    )
    ap.add_argument(
        "--iea-tab", default=_AMM_IEA_TAB, help=f"tab name within --iea-sheet (default {_AMM_IEA_TAB!r})"
    )
    ap.add_argument(
        "--iea-year",
        type=int,
        default=_AMM_IEA_YEAR,
        help="inventory year to assign the IEA sheet's rows (it carries no year column "
        f"itself; default {_AMM_IEA_YEAR}, a judgement call - see the module docstring)",
    )
    ap.add_argument(
        "--iea-source-label", default=_AMM_IEA_SOURCE_LABEL, help="SOURCE label for --iea-sheet rows"
    )
    ap.add_argument("--no-iea", action="store_true", help="skip the IEA sheet pull")
    ap.add_argument("--list-rounds", action="store_true", help="print discovered rounds and exit")
    ap.add_argument("--debug", action="store_true", help="print extraction diagnostics")
    args = ap.parse_args()

    if args.no_di and args.di_only:
        ap.error("--no-di and --di-only are mutually exclusive")

    if args.list_rounds:
        for r in discover_rounds(args.crt_root):
            print(f"{r.name}  {len(list(r.glob('*.zip')))} zip(s)  {r}")
        return

    parts: list[pl.DataFrame] = []

    if not args.di_only:
        rounds = discover_rounds(args.crt_root)
        log(f"CRT rounds (oldest first): {[r.name for r in rounds]}")
        # skip_drafts=False: one parse per round either way (the expensive part
        # is the zip/tar decompression, not this), tags each row DRAFT_FLAG so
        # both a draft-inclusive and a draft-free series can be built from it.
        cleaned_rounds = [
            clean_amm_round(
                read_round(r, args.recurse_zips, args.floor_year, skip_drafts=False),
                args.floor_year,
                args.category_label,
                args.debug,
            )
            for r in rounds
        ]
        # main series: draft (V0) rows excluded - crt_methane.py's own policy.
        # Oldest round is priority; each newer round only fills missing
        # (YEAR, COUNTRY_CODE, SOURCE).
        crt = reduce(
            merge_amm_rounds,
            [c.filter(~pl.col("DRAFT_FLAG")).drop("DRAFT_FLAG") for c in cleaned_rounds],
        ).select(OUTPUT_COLUMNS)

        if not args.no_draft_fallback:
            # everything achievable if drafts count too, same round-priority
            # merge. Any (COUNTRY_CODE, YEAR) that appears here but not in the
            # draft-free `crt` above has zero non-draft coverage anywhere, so
            # it can only have come from a draft - safe to add back as a
            # flagged fallback without ever outranking a real submission.
            crt_with_drafts = reduce(
                merge_amm_rounds, [c.drop("DRAFT_FLAG") for c in cleaned_rounds]
            ).select(OUTPUT_COLUMNS)
            draft_only = crt_with_drafts.join(crt, on=["COUNTRY_CODE", "YEAR"], how="anti")
            if len(draft_only):
                log(
                    f"  ! {len(draft_only)} row(s) have no non-draft CRT submission "
                    "anywhere, kept as a flagged draft fallback (see FILENAME): "
                    f"{sorted(set(draft_only['COUNTRY_CODE'].to_list()))}"
                )
                crt = pl.concat([crt, draft_only], how="vertical_relaxed")

        if args.debug:
            _debug_frame("CRT merged", crt)
        log(
            f"CRT >= {args.floor_year}: {len(crt)} rows, "
            f"{crt['COUNTRY_CODE'].n_unique()} countries"
        )
        parts.append(crt)

    if not args.no_di:
        di = load_di_historic(
            args.floor_year,
            args.category_label,
            args.keep_eua,
            args.debug,
            args.cookies,
            args.di_source,
            "annex-one",
            _AMM_DI_CATEGORY,
        )
        log(f"DI < {args.floor_year}: {len(di)} rows, {di['COUNTRY_CODE'].n_unique()} countries")
        parts.append(di)

        if not args.di_only and not args.no_gap_fill:
            # DI's own coverage sometimes stops a year or two short of
            # floor_year for a given country (e.g. Australia's Zenodo-mirror
            # series ends 2020, CRT starts 2022 -> 2021 is a hole neither
            # source fills). But a CRT submission normally carries the full
            # 1990-current series in one workbook set, so the missing year is
            # often sitting right there, just excluded by the >=floor_year
            # parse. Find those (COUNTRY_CODE, YEAR) gaps from DI alone (cheap),
            # then re-scan CRT for exactly those countries with the floor
            # dropped - `countries=` skips every non-matching zip/tar member
            # before it's even opened, so this stays cheap regardless of how
            # far back it has to look.
            di_max_year = di.group_by("COUNTRY_CODE").agg(pl.col("YEAR").max().alias("di_max"))
            gap_countries = di_max_year.filter(pl.col("di_max") < args.floor_year - 1)
            if len(gap_countries):
                codes = frozenset(gap_countries["COUNTRY_CODE"].to_list())
                log(
                    f"  DI->CRT gap: {len(codes)} countr(y/ies) with a year missing "
                    f"between DI and floor_year {args.floor_year}: {sorted(codes)}"
                )
                gap_rounds = [
                    clean_amm_round(
                        read_round(r, args.recurse_zips, 1990, skip_drafts=False, countries=codes),
                        1990,
                        args.category_label,
                        args.debug,
                    )
                    for r in discover_rounds(args.crt_root)
                ]
                gap_all = reduce(
                    merge_amm_rounds, [c.drop("DRAFT_FLAG") for c in gap_rounds]
                ).select(OUTPUT_COLUMNS)
                gap_fill = gap_all.join(
                    gap_countries.select("COUNTRY_CODE"), on="COUNTRY_CODE", how="inner"
                ).filter(pl.col("YEAR") < args.floor_year)
                # only the exact missing years - never touch a year DI already
                # has, or a year >= floor_year (that's the main CRT pass's job).
                gap_fill = gap_fill.join(
                    di.select("COUNTRY_CODE", "YEAR"), on=["COUNTRY_CODE", "YEAR"], how="anti"
                )
                if len(gap_fill):
                    log(
                        f"  DI->CRT gap-fill: {len(gap_fill)} row(s) recovered from CRT "
                        "(see FILENAME): "
                        f"{sorted(gap_fill.select('COUNTRY_CODE', 'YEAR').rows())}"
                    )
                    parts.append(gap_fill)
                else:
                    log("  DI->CRT gap-fill: no CRT workbook covers the missing year(s)")

    if not args.no_non_annex_sheet:
        non_annex = load_non_annex_sheet(
            args.non_annex_sheet, args.non_annex_tab, args.category_label, args.debug
        )
        log(
            f"non-Annex sheet: {len(non_annex)} rows, "
            f"{non_annex['COUNTRY_CODE'].n_unique()} countries"
        )
        parts.append(non_annex)

    if not args.no_iea:
        iea = load_iea_amm(
            args.iea_sheet,
            args.iea_tab,
            args.iea_year,
            args.iea_source_label,
            _AMM_IEA_FILENAME,
            args.debug,
        )
        log(f"IEA sheet: {len(iea)} rows, {iea['COUNTRY_CODE'].n_unique()} countries, year {args.iea_year}")
        parts.append(iea)

    out = pl.concat(parts, how="vertical_relaxed").sort(["COUNTRY_CODE", "YEAR"]).select(OUTPUT_COLUMNS)
    # unlike crt_methane.py, ANNEX_FLAG here is a real party-list check
    # (crt_methane.py's own _ANNEX_ONE), applied uniformly regardless of
    # source half - see the module docstring for why.
    out = out.with_columns(pl.col("COUNTRY_CODE").is_in(list(_ANNEX_ONE)).alias("ANNEX_FLAG"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.write_csv(args.out)

    log("")
    log(f"wrote {len(out)} rows -> {args.out}")
    log(f"  countries : {out['COUNTRY_CODE'].n_unique()}")
    log(f"  years     : {out['YEAR'].min()}-{out['YEAR'].max()}")
    tagged = out.with_columns(
        pl.when(pl.col("SOURCE") == args.iea_source_label)
        .then(pl.lit("IEA sheet"))
        .when(pl.col("FILENAME") == "historic")
        .then(pl.lit("DI historic"))
        .when(pl.col("FILENAME") == _NON_ANNEX_FILENAME)
        .then(pl.lit("non-Annex sheet"))
        # a UNFCCC row below floor_year that isn't DI historic or the hand
        # sheet can only be the DI<->CRT gap-fill (the only other UNFCCC
        # source willing to go below floor_year) - SOURCE no longer carries
        # an explicit tag for it (see IEA SHEET + SOURCE COLUMN docstring).
        .when(pl.col("YEAR") < args.floor_year)
        .then(pl.lit("CRT (DI gap-fill)"))
        .otherwise(pl.lit("CRT"))
        .alias("_src")
    )
    summary = (
        tagged.group_by("_src")
        .agg(
            pl.len().alias("rows"),
            pl.col("YEAR").min().alias("from"),
            pl.col("YEAR").max().alias("to"),
        )
        .sort("_src")
    )
    for row in summary.iter_rows(named=True):
        log(f"  {row['_src']:<12} {row['rows']:>6} rows  {row['from']}-{row['to']}")


if __name__ == "__main__":
    main()
