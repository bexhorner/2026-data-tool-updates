#!/usr/bin/env python3
"""
crt_methane.py - build a coal-mining CH4 (1.B.1.a) time series from UNFCCC.

Standalone: no dagster, no Selenium. The extraction functions in the VENDORED
block below are copied verbatim from ember-data-processing (paths + line ranges
noted there); keep them in sync if that repo changes.

    pip install pandas polars openpyxl unfccc-di-api
    pip install requests beautifulsoup4                 # for --fetch
    pip install playwright && playwright install chromium   # for --fetch-browser
    pip install pycountry                               # for --non-annex-sheet

TWO SOURCES, ONE SERIES
    < floor year (default 2021)   UNFCCC Data Interface (DI) API - "historic",
                                  un-revised figures. (get_historic_unfccc_data)

    >= floor year                 UNFCCC Common Reporting Tables (CRT), parsed
                                  from the per-country .xlsx (read_excel ->
                                  Table1.B.1, parse_unfccc, clean_unfccc). Older
                                  submission rounds are preferred; newer rounds
                                  only fill the years the older ones don't cover
                                  (merge_historic_unfccc, applied oldest-first).

    DI is filtered to < floor and CRT to >= floor, so the two halves don't
    overlap - the result is a straight concat. The category label is normalised
    to one string across both halves.

    unfccc.int (both the reports listing used by --fetch and the DI API) sits
    behind the Imperva/Incapsula WAF and refuses cloud/datacentre IPs. If it
    blocks you, pass browser cookies via --cookies or $UNFCCC_COOKIES (copy the
    visid_incap_/incap_ses_ values from DevTools -> Application -> Cookies), run
    from a residential connection, or use --no-di / supply the zips manually.

    For the historic half specifically, --di-source zenodo reads the maintainers'
    Zenodo mirror of the DI instead of the live API (same fallback di_pull.py
    uses): no WAF, at the cost of a large first-run download and their scrape's
    provenance rather than a first-hand query. Old (< floor) figures don't
    change, so the mirror's lag is immaterial.

DRAFT (V0) FALLBACK
    A CRT workbook whose filename contains "V0" is an unreviewed draft/interim
    submission. The default policy is to skip these outright when a country
    has any non-draft CRT submission covering a (year, round) - but some
    parties' *only* CRT-format submission anywhere is a V0 draft, and for
    those, skipping V0 means zero >= floor-year coverage rather than a
    lower-confidence number. So every round is parsed once with drafts kept
    (tagged, not dropped), a draft-free series and a draft-inclusive series
    are both built with the same round-priority merge, and whatever
    (country, year) pairs exist only in the draft-inclusive version - i.e. no
    non-draft submission, in any round, covers them at all - are added back
    as a flagged fallback, SOURCE suffixed " (unreviewed draft submission)"
    (FILENAME already names the underlying V0/_started workbook). A draft can
    only fill a total gap this way; it can never outrank or replace a genuine
    submission. Pass --no-draft-fallback to drop V0 outright instead (the old
    behaviour). Ported from crt_amm.py, which added this first.

NON-ANNEX I
    1.B.1.a "Coal mining and handling" is an Annex-I-only DI category - non-Annex
    inventories stop at 1.B.1 "Solid Fuels", and even that is sparse. So
    ember-data-processing does NOT get non-Annex from the API; it reads a
    hand-maintained Google Sheet tab. --non-annex-sheet <url|id> mirrors that
    (fetches the "UNFCCC Non Annex" tab via the gviz CSV endpoint, melts,
    name->ISO3) and adds an ANNEX_FLAG column. --di-scope non-annex-one|all is
    the other, non-repo option: live DI, per-party, label-filtered (pass
    --di-category "1.B.1 Solid Fuels").

CRT ZIPS
    The parser reads per-round folders, each named starting with its 4-digit
    year and holding EITHER loose per-country CRT zips OR one .tar.* bundle of
    them (<top>/<ISO3>/<submission-id>/<name>.zip, as mirrored by
    ember-data-processing); the bundle is streamed, never unpacked to disk:

        <crt-root>/2024/*.zip   |   <crt-root>/2024/2024-unfccc-crt.tar.xz
        <crt-root>/2025/*.zip   |   <crt-root>/2025/2025-unfccc-crt.tar.xz
        <crt-root>/2026/*.zip

    --fetch builds that layout for you: it scrapes the UNFCCC reports listing
    (document_type = Common Reporting Tables), resolves each /documents/N landing
    page to the real file, and downloads it into <crt-root>/<cycle year>/. The
    listing scrape + resolver + download are ported from old/crf_harvester.py.

    Plain --fetch uses requests, which Imperva frequently blocks at the TLS
    layer even with valid --cookies. --fetch-browser runs the same scrape/
    resolve/download through a real headless Chromium, which passes. Without
    either, download the zips yourself from
    https://unfccc.int/ghg-inventories-annex-i-parties/<year>. "Round" = the
    folder name.

USAGE
    python crt_methane.py --fetch-browser --out ./crt_methane_ch4.csv --debug
    python crt_methane.py --fetch --cookies "visid_incap_...=...; incap_ses_...=..."
    python crt_methane.py --fetch-only --fetch-rounds 2024,2025,2026
    python crt_methane.py --crt-root ./crt_data --out ./crt_methane_ch4.csv
    python crt_methane.py --di-only --out ./di_only.csv

OUTPUT
    Long CSV: COUNTRY_CODE, YEAR, EMISSIONS_CH4_KT, SOURCE, FILENAME
      SOURCE   - normalised category label (identical for every row)
      FILENAME - "historic" for DI rows; the winning CRT workbook path otherwise
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tarfile
import time
from functools import reduce
from pathlib import Path
from zipfile import BadZipFile, ZipFile, ZipInfo

try:
    import pandas as pd
    import polars as pl
except ImportError as exc:  # noqa: BLE001
    sys.exit(f"missing dependency: {exc}\n  pip install pandas polars openpyxl unfccc-di-api")

VERSION = "2026.09.16.2"

# DI party code -> code used elsewhere in the methane pipeline. transform_coal_
# emissions_unfccc maps EUA -> EU when it folds the historic frame in; mirror
# that so the two halves agree. Disable with --keep-eua.
EUA_REMAP = {"EUA": "EU"}

OUTPUT_COLUMNS = ["COUNTRY_CODE", "YEAR", "EMISSIONS_CH4_KT", "SOURCE", "FILENAME"]


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _first_present(columns, candidates: list[str]) -> str:
    lower = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand in columns:
            return cand
        if cand.lower() in lower:
            return lower[cand.lower()]
    raise KeyError(f"none of {candidates} in {list(columns)}")


# ===========================================================================
# VENDORED from ember-data-processing - do not "improve", keep verbatim so it
# tracks the pipeline. Sources:
#   src/resources/source_resources/unfccc_selenium.py   (parse_unfccc,
#       read_excel, read_zip_files, clean_unfccc, get_historic_unfccc_data)
#   src/pipelines/methane/transformations_sources/unfccc.py  (merge_historic_unfccc)
#   src/helpers/dataframe.py                            (coalesce_join)
#   src/pipelines/methane/assets_sources/unfccc.py      ('Base year' drop)
# ===========================================================================
def parse_unfccc(df: pd.DataFrame, filename: str) -> pd.DataFrame:
    """unfccc_selenium.py:74-108 - flatten the 3-row CRT header, pull country/
    year out of the filename.

    Deviation from the repo: the basename is taken *before* the branch below,
    not after. The ember-data-processing tar mirror nests each submission's
    workbooks under a folder called 'Common Reporting Tables 1990-2023/', which
    otherwise makes every file take the 'Common Reporting Tables' branch - whose
    country_pattern needs a '/' that the very next line strips, so country_code
    came out None for the whole submission.
    """
    filename = filename.split("/")[-1]
    if "EUA_CRT" in filename:
        year_pattern = r"_(\d{4})"
        country_pattern = r"(^[A-Za-z]{2})"
    elif "Common Reporting Tables" in filename:
        year_pattern = r"V\d\.\d-(\d{4})"
        country_pattern = r"\/([A-Za-z]{3})"
    else:
        year_pattern = r"V\d\.\d-(\d{4})"
        country_pattern = r"(^[A-Za-z]{3})"

    year_re = re.search(year_pattern, filename)
    year = None if year_re is None else year_re.group(1)
    country_re = re.search(country_pattern, filename)
    country = None if country_re is None else country_re.group(1)

    columns = df.iloc[:3].T
    citing_regex = re.compile(r"\(\d\)")
    columns[0] = columns[0].str.replace(citing_regex, "", regex=True).ffill()
    columns[1] = columns[1].str.replace(citing_regex, "", regex=True).str.strip()
    columns[2] = columns[2].str.replace(citing_regex, "", regex=True).ffill()
    columns = columns.fillna("")

    column_names = columns.apply(lambda x: f"{x[0]} {x[1]} {x[2]}", axis=1)
    df_data = df.iloc[3:].copy()
    df_data.columns = list(column_names.str.replace("  ", " ").str.strip())

    df_data["filename"] = filename
    df_data["year"] = year
    df_data["country_code"] = country
    return df_data


def read_excel(z: ZipFile, file: ZipInfo) -> pd.DataFrame | None:
    """unfccc_selenium.py:110-121 - Table1.B.1 sheet, 5 header rows skipped."""
    if Path(file.filename).suffix.lower() != ".xlsx":
        return None
    data = z.read(file)
    return pd.read_excel(
        io.BytesIO(data),
        sheet_name="Table1.B.1",
        engine="openpyxl",
        skiprows=5,
    )


def clean_unfccc(df_raw: pd.DataFrame) -> pl.DataFrame:
    """unfccc_selenium.py:151-175 - keep the '1.B.1.a Coal mining and handling'
    row, take EMISSIONS CH4 (kt).

    Two deviations from the repo:
      - it does ``.drop("")``. polars >=1.x renames a pandas column named ""
        to "column_0" during ``from_pandas``, so we drop whichever form the
        unnamed leading label column arrived as.
      - carries a DRAFT_FLAG column through when df_raw has a '_is_draft'
        column (i.e. it came from read_round(..., skip_drafts=False));
        otherwise DRAFT_FLAG is always False. Same pattern as
        crt_amm.py::clean_amm - see its "DRAFT (V0) FALLBACK" note.
      - the emissions cast is non-strict: besides the numeric-with-N notation
        keys (NA/NE/NO) pre-filtered below, draft workbooks have surfaced 'C'
        (confidential) too - cast whatever doesn't parse to null rather than
        hard-failing on every CRT notation key there is.
    """
    has_draft_col = "_is_draft" in df_raw.columns
    stacked = pl.from_pandas(df_raw.astype(str))
    unnamed = [c for c in stacked.columns if c == "" or re.fullmatch(r"column_\d+", c)]
    df = (
        stacked.drop(unnamed)
        .with_columns(pl.col("GREENHOUSE GAS SOURCE AND SINK CATEGORIES").str.strip_chars())
        .filter(
            pl.col("GREENHOUSE GAS SOURCE AND SINK CATEGORIES").str.contains(
                "1. B. 1. a. Coal mining and handling"
            )
        )
        .select(
            pl.col("year").replace("None", None).cast(pl.Int64).alias("YEAR"),
            pl.col("country_code").replace("Com", "AUS").alias("COUNTRY_CODE"),
            pl.col("GREENHOUSE GAS SOURCE AND SINK CATEGORIES").alias("SOURCE"),
            pl.when(pl.col("EMISSIONS CH4 (kt)").str.contains("N"))
            .then(None)
            .otherwise(pl.col("EMISSIONS CH4 (kt)"))
            .cast(pl.Float64, strict=False)
            .alias("EMISSIONS_CH4_KT"),
            pl.col("filename").alias("FILENAME"),
            (pl.col("_is_draft") == "True").alias("DRAFT_FLAG")
            if has_draft_col
            else pl.lit(False).alias("DRAFT_FLAG"),
        )
    )
    # 2024
    return df.with_columns(pl.col("COUNTRY_CODE").replace("None", "SWE"))


def coalesce_join(col_name: str, suffix: str = "_right") -> pl.Expr:
    """helpers/dataframe.py:280-281."""
    return pl.coalesce(col_name, col_name + suffix)


def merge_crt_rounds(
    df_priority: pl.DataFrame,
    df_supplementary: pl.DataFrame,
) -> pl.DataFrame:
    """merge_historic_unfccc, with the null-safety fix crt_amm.py's
    merge_amm_rounds needed once draft (V0) rows are in the mix - see its
    docstring for the two null-related cases plain '==' gets wrong:

    1. A priority-side row with a real, legitimately-null value (a reported
       'NO'/non-numeric figure, not a gap) and no match at all on the
       supplementary side: FILENAME_right is null too, so '==' silently drops
       the priority side's real FILENAME. eq_missing (null == null -> True,
       unlike '==') fixes this.
    2. The mirror image: no match on the priority side, and the
       supplementary side's own value also happens to be null. eq_missing
       alone gets this wrong the other way; the added
       FILENAME.is_not_null() guard catches it, since a priority row that
       doesn't really exist has no FILENAME either.

    Used for the CRT round-priority merge instead of merge_historic_unfccc
    once drafts are in play; merge_historic_unfccc itself is left untouched
    (vendored, and still correct for the no-draft case)."""
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


def merge_historic_unfccc(
    df_priority: pl.DataFrame,
    df_supplementary: pl.DataFrame,
) -> pl.DataFrame:
    """transformations_sources/unfccc.py:6-21 - priority wins per (YEAR,
    COUNTRY_CODE, SOURCE); supplementary only fills gaps."""
    return df_priority.join(
        df_supplementary, how="full", on=["YEAR", "COUNTRY_CODE", "SOURCE"], coalesce=True
    ).select(
        "YEAR",
        "COUNTRY_CODE",
        "SOURCE",
        coalesce_join("EMISSIONS_CH4_KT"),
        pl.when(pl.col("EMISSIONS_CH4_KT") == coalesce_join("EMISSIONS_CH4_KT"))
        .then("FILENAME")
        .otherwise("FILENAME_right")
        .alias("FILENAME"),
    )


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def install_unfccc_cookies(cookie_str: str) -> bool:
    """Get past the Imperva/Incapsula WAF in front of di.unfccc.int by replaying
    cookies copied from a browser that has already solved the JS challenge.

    Borrowed from '2026 Data Tool Updates/di_pull.py' (install_cookies). Patches
    requests at the session level so it works whatever unfccc_di_api does
    internally. Cookie string looks like:
        "visid_incap_...=...; incap_ses_...=..."
    Copy from DevTools -> Application -> Cookies -> unfccc.int. They expire with
    the browser session. No-op (returns False) when no cookies are supplied.
    """
    import os

    raw = cookie_str or os.environ.get("UNFCCC_COOKIES", "")
    if not raw.strip():
        return False
    try:
        import requests
    except ImportError:
        return False
    jar = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            jar[k.strip()] = v.strip()
    original = requests.Session.request

    def patched(self, method, url, **kw):
        if "unfccc.int" in str(url):
            ck = kw.get("cookies") or {}
            ck.update(jar)
            kw["cookies"] = ck
            hd = kw.get("headers") or {}
            hd.setdefault("User-Agent", _BROWSER_UA)
            hd.setdefault("Accept", "application/json, text/plain, */*")
            hd.setdefault("Referer", "https://di.unfccc.int/detailed_data_by_party")
            kw["headers"] = hd
        return original(self, method, url, **kw)

    requests.Session.request = patched
    log(f"installed {len(jar)} Incapsula cookie(s) for unfccc.int")
    return True


def di_historic_query() -> pd.DataFrame:
    """unfccc_selenium.py:183-193 - live DI query, Annex I, CH4, category 9835,
    measure 10460."""
    import unfccc_di_api

    reader = unfccc_di_api.UNFCCCSingleCategoryApiReader(party_category="annexOne")
    return reader.query(
        party_codes=reader.parties["code"],
        gases=["CH4"],
        category_ids=[9835],
        measure_ids=[10460],
    )


# ===========================================================================
# DI Zenodo mirror - end of vendored code. Same fallback di_pull.py uses when
# the Imperva WAF blocks the live API: unfccc_di_api.ZenodoReader reads the
# maintainers' Zenodo data package (a periodic scrape of the whole DI, every
# party incl. non-Annex I). No WAF, no cookies. It is not year-for-year current
# with the live API, but for the < floor-year "historic" half that gap is old
# data that never changes. The GitHub mirror di_pull.py's header mentions
# (openclimatedata/unfccc-detailed-data-by-party) is abandoned at inventory
# year 2019, so it is not used here.
# ===========================================================================
_DI_CATEGORY = "1.B.1.a Coal mining and handling"
_DI_MEASURE = "Net emissions/removals"

# di_pull.py:153-155 - the 43 Annex I DI party codes (ZenodoReader has no
# party_category filter, so scope is applied by list).
_ANNEX_ONE = frozenset(
    "AUS AUT BLR BEL BGR CAN HRV CYP CZE DNK EST FIN FRA DEU GRC HUN ISL IRL ITA "
    "JPN KAZ LVA LIE LTU LUX MLT MCO NLD NZL NOR POL PRT ROU RUS SVK SVN ESP SWE "
    "CHE TUR UKR GBR USA".split()
)


def di_historic_query_zenodo(scope: str, category: str = _DI_CATEGORY) -> pd.DataFrame:
    """<category> / CH4 / net-emission rows from the DI Zenodo mirror.

    unfccc_di_api.ZenodoReader reads the whole ~parquet into a pandas frame in
    its constructor (pd.read_parquet on every column) and OOMs a modest machine
    - a per-party .query() can't help, it only slices that frame. So fetch the
    same file via pooch (cached) and stream it with polars: predicate pushdown
    means only the ~1.5k matching rows are ever materialised."""
    import inspect

    import pooch
    import unfccc_di_api

    params = inspect.signature(unfccc_di_api.ZenodoReader.__init__).parameters
    url = params["url"].default
    known_hash = params["known_hash"].default
    path = pooch.retrieve(url=url, known_hash=known_hash)

    norm_cat = (
        pl.col("category").str.replace_all(r"\s+", " ").str.strip_chars().str.to_lowercase()
    )
    norm_gas = pl.col("gas").str.replace_all("₄", "4").str.replace_all("₂", "2").str.to_uppercase()
    norm_meas = (
        pl.col("measure").str.replace_all(r"\s+", " ").str.strip_chars().str.to_lowercase()
    )
    df = (
        pl.scan_parquet(path)
        .filter(
            norm_cat.str.contains(_norm_ws(category), literal=True)
            & (norm_gas == "CH4")
            & (norm_meas == _norm_ws(_DI_MEASURE))
        )
        .select("party", "year", "numberValue")
        .collect()
        .to_pandas()
    )
    if scope == "annex-one":
        df = df[df["party"].isin(_ANNEX_ONE)]
    elif scope == "non-annex-one":
        df = df[~df["party"].isin(_ANNEX_ONE)]
    if df.empty:
        raise RuntimeError(f"Zenodo mirror had no 1.B.1.a CH4 net-emission rows for scope {scope}")
    return df


def _norm_ws(text: object) -> str:
    """Collapse whitespace and case-fold. DI category labels carry doubled
    spaces ('1.B.1.a  Coal Mining and Handling') and vary in case."""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def _narrow_di(raw: pd.DataFrame, category: str = _DI_CATEGORY) -> pd.DataFrame:
    """A full per-party DI frame -> just its <category> / CH4 / net-emission rows.
    di_pull.py's filter_rows: whitespace-collapsed substring category match,
    subscript gas symbols normalised, exact measure (kept lenient - if a party
    reports the category with no 'Net emissions/removals' measure row, keep the
    category+gas rows rather than drop the party)."""
    ccol = _first_present(raw.columns, ["category", "categoryName"])
    gcol = _first_present(raw.columns, ["gas", "gasName"])
    cat = raw[ccol].map(_norm_ws)
    gas = (
        raw[gcol].astype(str).str.replace("₄", "4", regex=False)
        .str.replace("₂", "2", regex=False).str.upper()
    )
    out = raw[cat.str.contains(_norm_ws(category), regex=False) & (gas == "CH4")]
    try:
        mcol = _first_present(out.columns, ["measure", "measureName"])
        sel = out[mcol].map(_norm_ws) == _norm_ws(_DI_MEASURE)
        if sel.any():
            out = out[sel]
    except KeyError:
        pass
    return out


def di_query_api_label(scope: str, cookies: str, category: str = _DI_CATEGORY) -> pd.DataFrame:
    """Live DI, per party, filtered to <category> / CH4 / net emissions by label.
    Unlike di_historic_query (annexOne, fixed category_ids 9835/10460) this also
    reaches the nonAnnexOne tree, whose category ids differ. Needs --cookies to
    clear the Imperva wall."""
    install_unfccc_cookies(cookies)
    import unfccc_di_api

    api = unfccc_di_api.UNFCCCApiReader()
    # the sub-readers only give the party lists; queries go through the top-level
    # reader, which auto-routes each code to the right tree (di_pull.py's pattern).
    trees = []
    if scope in ("annex-one", "all"):
        trees.append(("annexOne", api.annex_one_reader))
    if scope in ("non-annex-one", "all"):
        trees.append(("nonAnnexOne", api.non_annex_one_reader))

    kept: list[pd.DataFrame] = []
    for tree, sub in trees:
        codes = sorted(sub.parties["code"].tolist())
        log(f"  {tree}: {len(codes)} parties")
        for i, code in enumerate(codes, 1):
            try:
                narrowed = _narrow_di(api.query(party_code=code), category)
            except Exception as exc:  # noqa: BLE001
                log(f"    [{i}/{len(codes)}] {code}: FAILED {exc}")
                continue
            if len(narrowed):
                kept.append(narrowed)
                log(f"    [{i}/{len(codes)}] {code}: {len(narrowed)} CH4 row(s)")
            time.sleep(0.5)
    if not kept:
        raise RuntimeError(
            f"live DI returned no {category!r} CH4 rows for scope {scope} "
            "(WAF block, or the label differs / is absent in this tree - "
            "non-Annex inventories stop at '1.B.1 Solid Fuels')"
        )
    return pd.concat(kept, ignore_index=True)


# ===========================================================================
# FETCH - download the CRT zips. Ported from '2026 Data Tool Updates/old/
# crf_harvester.py' (cmd_crt, parse_reports_listing, resolve_document,
# file_candidates, session, and the Incapsula guards). Trimmed to the CRT
# reports-listing path only. Needs: pip install requests beautifulsoup4
# ===========================================================================
_FETCH_SLEEP = 6.0  # crf_harvester.py:65 "Incapsula rate-limits; 3s tripped it"
_REPORTS_CRT = (
    "https://unfccc.int/reports?f%5B0%5D=document_type%3A4593"
    "&items_per_page=50&page=%2C%2C{page}"
)
_CRT_TITLE = re.compile(r"^\s*(.+?)\.\s*(\d{4})\s+Common Reporting Tables?", re.I)
_WAF_RE = re.compile(r"_Incapsula_Resource|Incapsula incident ID", re.I)


def _clean_text(t: str) -> str:
    for ch in ("\xa0", " ", " ", "​"):
        t = t.replace(ch, " ")
    return re.sub(r"\s+", " ", t or "").strip()


def _is_challenge(text: str) -> bool:
    return len(text) < 5000 and bool(_WAF_RE.search(text))


def _guard(text: str, what: str) -> str:
    if _is_challenge(text):
        sys.exit(
            f"Incapsula challenge on {what}.\n"
            'Pass --cookies "visid_incap_...=...; incap_ses_...=..." from a '
            "browser that has loaded unfccc.int (DevTools -> Application -> "
            "Cookies), or set $UNFCCC_COOKIES."
        )
    return text


def _fetch_session(cookies: str):
    import requests
    from requests.adapters import HTTPAdapter

    try:
        from urllib3.util.retry import Retry
    except ImportError:  # pragma: no cover
        from requests.packages.urllib3.util.retry import Retry

    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Connection": "close",  # unfccc.int drops keep-alive
        }
    )
    raw = cookies or os.environ.get("UNFCCC_COOKIES", "")
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            s.cookies.set(k.strip(), v.strip(), domain=".unfccc.int")
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def _parse_reports_listing(html: str) -> list[dict]:
    """crf_harvester.py parse_reports_listing - rows of the CRT reports table:
    party, cycle year, submission date, /documents/N url."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for tr in soup.find_all("tr"):
        cells = [_clean_text(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        link = next(
            (a["href"] for a in tr.find_all("a", href=True) if "/documents/" in a["href"]),
            None,
        )
        if not link:
            continue
        if link.startswith("/"):
            link = "https://unfccc.int" + link
        if link in seen:
            continue
        seen.add(link)
        title = cells[0]
        m = _CRT_TITLE.match(title)
        party = m.group(1).strip() if m else (cells[2] if len(cells) > 2 else "")
        cycle = int(m.group(2)) if m else None
        date = ""
        for c in cells:
            dm = re.search(r"\d{1,2}\s+\w{3,9}\s+\d{4}", c)
            if dm and "Common Reporting" not in c:
                date = dm.group(0)
                break
        out.append(
            {"party": party, "cycle_year": cycle, "submission_date": date, "doc_url": link}
        )
    return out


def _file_candidates(html: str) -> list[str]:
    """crf_harvester.py file_candidates - every plausible download link on a
    /documents/N page (deliberately loose)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if not h or h.startswith(("#", "mailto:")):
            continue
        low = h.lower()
        label = _clean_text(a.get_text(" ", strip=True)).lower()
        hit = (
            any(e in low for e in (".zip", ".7z", ".xlsx", ".xls"))
            or "/sites/default/files/" in low
            or "/download" in low
            or label in ("open", "download", "télécharger")
        )
        if not hit:
            continue
        if h.startswith("//"):
            h = "https:" + h
        elif h.startswith("/"):
            h = "https://unfccc.int" + h
        out.append(h)
    return out


class _RequestsTransport:
    """Plain requests. Fast, but Imperva often blocks it at the TLS layer even
    with valid cookies."""

    def __init__(self, cookies: str):
        self._s = _fetch_session(cookies)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text(self, url: str) -> str:
        r = self._s.get(url, timeout=90)
        r.raise_for_status()
        return _guard(r.text, url)

    def download(self, url: str, dest: Path) -> int:
        with self._s.get(url, timeout=300, stream=True) as resp:
            resp.raise_for_status()
            head = next(resp.iter_content(2048), b"")
            if b"Incapsula" in head or _is_challenge(head.decode("latin-1", "ignore")):
                raise RuntimeError("challenged on download")
            with open(dest, "wb") as fh:
                fh.write(head)
                for chunk in resp.iter_content(1 << 16):
                    fh.write(chunk)
        return dest.stat().st_size


class _PlaywrightTransport:
    """A real browser makes the requests, so the JS challenge, TLS fingerprint
    and cookies all line up. Needs:  pip install playwright
    (uses your installed Chrome if present, else `playwright install chromium`).

    If it still stalls on the challenge, add --headed: a visible window is the
    least bot-like and lets you clear an interactive check by hand. A hard
    IP-level block (datacentre / VPN / CGNAT ranges) cannot be beaten from any
    client - use a residential connection.
    """

    _WEBDRIVER_MASK = (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
    )

    def __init__(self, cookies: str = "", headed: bool = False):
        self._cookies = cookies or os.environ.get("UNFCCC_COOKIES", "")
        self._headed = headed

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sys.exit("--fetch-browser needs Playwright:\n  pip install playwright")
        self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        last_exc: Exception | None = None
        for kw in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
            try:
                self._browser = self._pw.chromium.launch(
                    headless=not self._headed, args=args, **kw
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        else:
            self._pw.stop()
            sys.exit(
                f"could not launch a browser ({last_exc}).\n"
                "Install one:  playwright install chromium"
            )
        self._ctx = self._browser.new_context(
            user_agent=_BROWSER_UA,
            locale="en-GB",
            viewport={"width": 1920, "height": 1080},
        )
        self._ctx.add_init_script(self._WEBDRIVER_MASK)
        for part in self._cookies.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                try:
                    self._ctx.add_cookies(
                        [{"name": k.strip(), "value": v.strip(), "domain": ".unfccc.int", "path": "/"}]
                    )
                except Exception:  # noqa: BLE001
                    pass
        self._page = self._ctx.new_page()
        return self

    def __exit__(self, *exc):
        for closer in (getattr(self, "_ctx", None), getattr(self, "_browser", None)):
            try:
                closer and closer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        return False

    def text(self, url: str) -> str:
        try:
            self._page.goto(url, wait_until="commit", timeout=60_000)
        except Exception:  # noqa: BLE001 - challenge often aborts the first nav
            pass
        deadline = time.monotonic() + 75
        last = ""
        while time.monotonic() < deadline:
            self._page.wait_for_timeout(2_500)
            try:
                self._page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:  # noqa: BLE001
                pass
            try:
                html = self._page.content()
            except Exception:  # noqa: BLE001 - "page is navigating"; retry
                continue
            last = html
            # challenge stub is ~900 bytes; the real listing/table page is large
            if len(html) > 15_000 and not _is_challenge(html):
                return html
        raise RuntimeError(
            f"page never settled to real content ({len(last)} bytes): {url}\n"
            "  the challenge may need --headed (solve it by hand once), or this "
            "IP range is hard-blocked by the WAF"
        )

    def download(self, url: str, dest: Path) -> int:
        resp = self._ctx.request.get(url, timeout=300_000)
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status}")
        body = resp.body()
        if body[:2000].find(b"Incapsula") != -1:
            raise RuntimeError("challenged on download")
        dest.write_bytes(body)
        return len(body)


def _resolve_document(transport, url: str) -> str | None:
    """crf_harvester.py resolve_document - a /documents/N page wraps the real
    file; return its direct URL, preferring .zip/.7z, then spreadsheets."""
    html = transport.text(url)
    cands = _file_candidates(html)

    def rank(h: str) -> tuple:
        h = h.lower().split("?")[0]
        return (
            h.endswith((".zip", ".7z")),
            h.endswith((".xlsx", ".xls")),
            "/sites/default/files/" in h,
        )

    for key in (0, 1, 2):
        for h in cands:
            if rank(h)[key]:
                return h
    return cands[0] if cands else None


def fetch_crt_zips(
    crt_root: Path,
    rounds: list[int] | None,
    parties: list[str] | None,
    cookies: str,
    browser: bool = False,
    headed: bool = False,
    max_pages: int = 12,
) -> None:
    """Scrape the CRT reports listing and download each submission into
    <crt_root>/<cycle_year>/. Cached/resumable."""
    try:
        import bs4  # noqa: F401
    except ImportError:
        sys.exit("--fetch needs: pip install beautifulsoup4")
    if not browser:
        try:
            import requests  # noqa: F401
        except ImportError:
            sys.exit("--fetch needs: pip install requests  (or use --fetch-browser)")

    if browser:
        transport_cm = _PlaywrightTransport(cookies, headed=headed)
    else:
        transport_cm = _RequestsTransport(cookies)
    with transport_cm as transport:
        listing: list[dict] = []
        page = 0
        while page < max_pages:
            url = _REPORTS_CRT.format(page=page)
            try:
                html = transport.text(url)
            except Exception as exc:  # noqa: BLE001
                log(f"  listing page {page}: FAILED ({exc})")
                break
            got = _parse_reports_listing(html)
            have = {x["doc_url"] for x in listing}
            new = [g for g in got if g["doc_url"] not in have]
            log(f"  listing page {page}: {len(got)} rows, {len(new)} new")
            if not new:
                break
            listing += new
            page += 1
            time.sleep(_FETCH_SLEEP)

        if not listing:
            sys.exit(
                "no CRT rows found - either the reports listing markup changed, "
                "or you are still being challenged. Try --fetch-browser."
            )

        want_rounds = set(rounds) if rounds else None
        want_parties = [p.lower() for p in parties] if parties else None
        todo = [
            row
            for row in listing
            if row["cycle_year"] is not None
            and (want_rounds is None or row["cycle_year"] in want_rounds)
            and (want_parties is None or any(p in row["party"].lower() for p in want_parties))
        ]
        log(
            f"  {len(todo)} submission(s) to fetch; "
            f"rounds {sorted({row['cycle_year'] for row in todo})}"
        )

        ok = 0
        for i, row in enumerate(todo, 1):
            tag = f"[{i}/{len(todo)}] {row['party']} {row['cycle_year']}"
            dest_dir = crt_root / str(row["cycle_year"])
            dest_dir.mkdir(parents=True, exist_ok=True)
            try:
                direct = _resolve_document(transport, row["doc_url"])
            except Exception as exc:  # noqa: BLE001
                log(f"  {tag}: resolve failed ({exc})")
                continue
            if not direct:
                log(f"  {tag}: no file found on {row['doc_url']}")
                continue
            fname = (
                direct.rsplit("/", 1)[-1].split("?")[0]
                or f"{row['party']}_{row['cycle_year']}.zip"
            )
            dest = dest_dir / fname
            if dest.exists() and dest.stat().st_size > 1024:
                log(f"  {tag}: have {fname}")
                ok += 1
                continue
            try:
                size = transport.download(direct, dest)
                log(f"  {tag}: {size // 1024} KB -> {row['cycle_year']}/{fname}")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                log(f"  {tag}: download failed ({exc})")
                dest.unlink(missing_ok=True)
            time.sleep(_FETCH_SLEEP)

    log(f"  CRT zips present: {ok}/{len(todo)}")
    if ok == 0:
        sys.exit("nothing downloaded")


# ===========================================================================
# script logic
# ===========================================================================
def _iter_workbooks(zf: ZipFile, recurse: bool):
    """(ZipFile, ZipInfo) per member; optionally one level into nested .zip
    members (some CRT bundles are a zip of zips)."""
    for info in zf.infolist():
        if info.filename.lower().endswith(".zip") and recurse:
            try:
                with ZipFile(io.BytesIO(zf.read(info))) as inner:
                    yield from ((inner, i) for i in inner.infolist())
            except BadZipFile:
                log(f"  ! bad nested zip: {info.filename}")
        else:
            yield zf, info


_INNER_YEAR = re.compile(r"V\d\.\d-(\d{4})")
_TARBALL_SUFFIXES = (".tar.xz", ".tar.gz", ".tar.bz2", ".tar.zst", ".tgz", ".tar")


def _label_matches_countries(label: str, countries: frozenset[str] | None) -> bool:
    """True if `label` (a loose zip filename, or a tar member path like
    './2024-unfccc-crt/CHN/645298/CHN-...zip') plausibly belongs to one of
    `countries` (ISO3 codes) - or `countries` is None (no filter)."""
    if countries is None:
        return True
    name = Path(label).name.upper()
    if any(name.startswith(f"{c}-") for c in countries):
        return True
    segments = {s.upper() for s in label.replace("\\", "/").split("/")}
    return bool(segments & countries)


def _read_crt_zip(
    label: str,
    src,
    recurse: bool,
    floor_year: int,
    frames: dict[str, pd.DataFrame],
    skip_drafts: bool = True,
    countries: frozenset[str] | None = None,
) -> None:
    """Parse one per-country CRT .zip into `frames`. `src` is a path or raw
    bytes (bytes when the zip came out of a .tar.* bundle, so nothing is
    written to disk).

    skip_drafts=False (non-default at the _read_crt_zip/read_round level;
    crt_methane.py's own main() and crt_amm.py both pass it) keeps V0 draft
    submissions instead of dropping them, tagging each resulting frame with a
    '_is_draft' column so a caller can still tell them apart downstream -
    see clean_unfccc's DRAFT_FLAG and the draft-fallback logic in main().

    countries (non-default; crt_amm.py's DI/CRT boundary gap-fill uses this)
    restricts parsing to zips whose label matches one of these ISO3 codes -
    cheap, since it's checked before the zip is even opened."""
    if not _label_matches_countries(label, countries):
        return
    is_draft = "V0" in Path(label).name  # draft / placeholder submissions
    if skip_drafts and is_draft:
        return
    try:
        zf_arg = src if isinstance(src, (str, Path)) else io.BytesIO(src)
        with ZipFile(zf_arg) as zf:
            infos = zf.infolist()
            if not infos or "awaiting_submission" in infos[0].filename:
                return
            for owner, info in _iter_workbooks(zf, recurse):
                if info.filename.lower().endswith(".7z"):
                    continue
                ym = _INNER_YEAR.search(Path(info.filename).name)
                if ym and int(ym.group(1)) < floor_year:
                    continue
                try:
                    raw = read_excel(owner, info)  # None unless .xlsx
                except Exception as exc:  # noqa: BLE001
                    log(f"  ! {label}:{info.filename} unreadable ({exc})")
                    continue
                if raw is not None:
                    parsed = parse_unfccc(raw, info.filename)
                    if not skip_drafts:
                        parsed["_is_draft"] = is_draft
                    frames[f"{label}/{info.filename}"] = parsed
    except BadZipFile:
        log(f"  ! bad zip, skipped: {label}")


def read_round(
    round_dir: Path,
    recurse: bool,
    floor_year: int,
    skip_drafts: bool = True,
    countries: frozenset[str] | None = None,
) -> pd.DataFrame:
    """One submission round -> raw pandas frame. This is unfccc_selenium.py
    read_zip_files with the hard-coded unfccc/2026 path replaced by round_dir,
    plus: skip inner per-year workbooks below the floor (a CRT zip holds one
    .xlsx per inventory year 1990..submission-2), and accept a round packaged
    either as loose per-country *.zip or as a single *.tar.* bundle of them
    (<top>/<ISO3>/<submission-id>/<name>.zip), which is streamed, not unpacked.

    skip_drafts, countries: see _read_crt_zip. A tar member that doesn't match
    `countries` is skipped before extractfile() decompresses it - restricting
    to a small country set (crt_amm.py's gap-fill) stays cheap even on a
    multi-GB bundle."""
    frames: dict[str, pd.DataFrame] = {}
    zips = sorted(round_dir.glob("*.zip"))
    tarballs = sorted(
        p for p in round_dir.iterdir() if p.name.lower().endswith(_TARBALL_SUFFIXES)
    )
    if not zips and not tarballs:
        raise SystemExit(f"no .zip or .tar.* archives in {round_dir}")

    for zip_path in zips:
        if not _label_matches_countries(zip_path.name, countries):
            continue
        _read_crt_zip(zip_path.name, zip_path, recurse, floor_year, frames, skip_drafts, countries)

    for tb in tarballs:
        log(f"  streaming {tb.name} (decompressing in memory, can take a few minutes) ...")
        try:
            with tarfile.open(tb, mode="r:*") as tar:
                for member in tar:
                    if not member.isfile() or not member.name.lower().endswith(".zip"):
                        continue
                    if not _label_matches_countries(member.name, countries):
                        continue
                    fh = tar.extractfile(member)
                    if fh is None:
                        continue
                    _read_crt_zip(
                        member.name, fh.read(), recurse, floor_year, frames, skip_drafts, countries
                    )
        except tarfile.TarError as exc:
            log(f"  ! could not read {tb.name} ({exc})")

    if not frames:
        raise SystemExit(f"no CRT workbooks parsed under {round_dir}")
    log(f"  {round_dir.name}: {len(frames)} workbook(s)")
    return pd.concat((_dedupe_columns(f) for f in frames.values()), ignore_index=True)


def _dedupe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Flattening the 3-row CRT header can repeat a label (several blank trailing
    columns; a memo header printed twice; template quirks across countries).
    pandas.concat can't align an axis with duplicate labels, so keep each
    label's first occurrence and rename every repeat to ``column_<n>`` - which
    clean_unfccc's existing 'drop unnamed / column_\\d+' step then discards."""
    seen: set[str] = set()
    cols = list(df.columns)
    for i, c in enumerate(cols):
        if c in seen:
            cols[i] = f"column_{i}"
        else:
            seen.add(c)
    if cols != list(df.columns):
        df = df.copy()
        df.columns = cols
    return df


def clean_round(raw: pd.DataFrame, floor_year: int, label: str, debug: bool) -> pl.DataFrame:
    """clean_unfccc -> floor filter -> normalised label."""
    try:
        cleaned = clean_unfccc(raw)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"clean_unfccc failed ({type(exc).__name__}: {exc}).\n"
            "The CRT Table1.B.1 layout probably differs from what the repo "
            "expects - inspect a workbook and check the '1.B.1.a Coal mining "
            "and handling' row and 'EMISSIONS CH4 (kt)' column."
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
    # a null/unusable COUNTRY_CODE has no business flowing into the
    # round-priority merge or the draft-fallback diff below (crashes sorted()
    # on a set mixing None and str) - drop it here rather than just warn, as
    # crt_amm.py::clean_amm_round does. The floor-year filter above happened
    # to remove every case hit so far (all pre-2022), but that's not guaranteed.
    unusable = out.filter(pl.col("COUNTRY_CODE").is_null() | (pl.col("COUNTRY_CODE") == "None"))
    if len(unusable):
        log(
            f"  ! dropping {len(unusable)} post-floor row(s) with no usable COUNTRY_CODE: "
            f"{sorted(set(unusable['FILENAME'].to_list()))[:5]}"
        )
        out = out.filter(
            pl.col("COUNTRY_CODE").is_not_null() & (pl.col("COUNTRY_CODE") != "None")
        )
    # one round can contain two workbooks for a country (e.g. a "... (2).zip"
    # browser copy). merge_crt_rounds joins on (YEAR, COUNTRY_CODE, SOURCE),
    # so collapse duplicates here first.
    before = len(out)
    out = out.unique(subset=["YEAR", "COUNTRY_CODE", "SOURCE"], keep="first")
    if debug and len(out) != before:
        log(f"  dropped {before - len(out)} duplicate (year, country) row(s) in this round")
    return out


def load_di_historic(
    floor_year: int,
    label: str,
    keep_eua: bool,
    debug: bool,
    cookies: str = "",
    source: str = "api",
    scope: str = "annex-one",
    di_category: str = _DI_CATEGORY,
) -> pl.DataFrame:
    """DI query -> tidy frame, years below the floor only. Mirrors the
    unfccc_emissions_annex_historic asset ('Base year' dropped) and the select
    in transform_coal_emissions_unfccc.

    source='api'    live di.unfccc.int (Imperva-walled; needs a residential IP
                    or --cookies). scope=annex-one uses the fast fixed-id query;
                    other scopes / a non-default di_category go per-party and
                    filter by category label.
    source='zenodo' the maintainers' Zenodo mirror (di_pull.py's fallback). No
                    WAF; polars-streams the cached parquet. Carries this category
                    for Annex I only.
    """
    default_cat = _norm_ws(di_category) == _norm_ws(_DI_CATEGORY)
    if source == "zenodo":
        log(f"streaming the DI Zenodo mirror parquet (scope {scope}, CH4, {di_category!r}) ...")
        try:
            raw = di_historic_query_zenodo(scope, di_category)
        except ImportError:
            sys.exit("missing dependency: unfccc-di-api\n  pip install unfccc-di-api")
        except Exception as exc:  # noqa: BLE001
            sys.exit(f"Zenodo DI read failed ({type(exc).__name__}: {exc}).")
    else:
        install_unfccc_cookies(cookies)
        try:
            if scope == "annex-one" and default_cat:
                log("querying the UNFCCC DI API (annexOne, CH4, 1.B.1.a) ...")
                raw = di_historic_query()
            else:
                log(f"querying the UNFCCC DI API per-party (scope {scope}, CH4, {di_category!r}) ...")
                raw = di_query_api_label(scope, cookies, di_category)
        except ImportError:
            sys.exit("missing dependency: unfccc-di-api\n  pip install unfccc-di-api")
        except Exception as exc:  # noqa: BLE001
            sys.exit(
                f"DI query failed ({type(exc).__name__}: {exc}).\n"
                "The DI API sits behind the Imperva/Incapsula WAF and blocks "
                "cloud/datacentre IPs. Options:\n"
                "  - run from a normal residential/office connection\n"
                '  - pass --cookies "visid_incap_...=...; incap_ses_...=..." '
                "(copied from a browser at unfccc.int; or set $UNFCCC_COOKIES)\n"
                "  - --di-source zenodo  (the maintainers' mirror; Annex I only "
                "for this category)\n"
                "  - --no-di and supply pre-floor-year data separately"
            )
    di = pl.from_pandas(raw)
    if debug:
        _debug_frame("  DI raw", di)

    ycol = _first_present(di.columns, ["year", "yearName"])
    pcol = _first_present(di.columns, ["party", "partyCode", "party_code"])
    vcol = _first_present(di.columns, ["numberValue", "value", "number_value"])
    remap = {} if keep_eua else EUA_REMAP

    di = (
        di.with_columns(pl.col(ycol).cast(pl.Utf8).str.strip_chars().alias("_y"))
        .filter(pl.col("_y") != "Base year")
        .select(
            pl.col("_y").cast(pl.Int64, strict=False).alias("YEAR"),
            pl.col(pcol).cast(pl.Utf8).replace(remap).alias("COUNTRY_CODE"),
            pl.col(vcol).cast(pl.Float64, strict=False).alias("EMISSIONS_CH4_KT"),
        )
        .drop_nulls(["YEAR"])
        .filter(pl.col("YEAR") < floor_year)
        .with_columns(
            pl.lit(label).alias("SOURCE"),
            pl.lit("historic").alias("FILENAME"),
        )
    )
    before = len(di)
    di = di.unique(subset=["COUNTRY_CODE", "YEAR"], keep="first")
    if len(di) != before:
        log(f"  ! DI had {before - len(di)} duplicate (country, year) row(s); kept first")
    return di.select(OUTPUT_COLUMNS)


# ===========================================================================
# Non-Annex I historic - the repo-faithful route. ember-data-processing does
# NOT query the DI for non-Annex parties (they don't report 1.B.1.a there);
# unfccc_emissions_non_annex reads the "UNFCCC Non Annex" tab of a Google Sheet
# maintained by hand, and transform_coal_emissions_unfccc melts + name->code +
# comma-strips it. Mirrored here.
#   assets_sources/unfccc.py:unfccc_emissions_non_annex
#   transformations_curated/production_emissions.py:transform_coal_emissions_unfccc (66-82)
#   transformations_sources/pycountry.py:country_name_to_code
# ===========================================================================
_NON_ANNEX_FILENAME = "UNFCCC Non Annex (manual sheet)"


def country_name_to_code(country: str) -> str | None:
    """transformations_sources/pycountry.py, verbatim."""
    import pycountry

    try:
        return pycountry.countries.lookup(country).alpha_3
    except LookupError:
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
        return manual.get(country)


def _resolve_sheet_id(sheet: str) -> str:
    """Accept a full Sheets URL or a bare doc id, return the doc id."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", sheet)
    return m.group(1) if m else sheet.strip()


def load_non_annex_sheet(sheet: str, tab: str, label: str, debug: bool) -> pl.DataFrame:
    """The 'UNFCCC Non Annex' tab (wide: Country + one column per year) ->
    tidy long frame. Fetched unauthenticated via the gviz CSV endpoint, so the
    sheet must be link-viewable."""
    import requests

    doc = _resolve_sheet_id(sheet)
    url = (
        f"https://docs.google.com/spreadsheets/d/{doc}/gviz/tq"
        f"?tqx=out:csv&sheet={requests.utils.quote(tab)}"
    )
    log(f"fetching the '{tab}' tab (gviz CSV) ...")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    if resp.text.lstrip().lower().startswith(("<!doctype", "<html")):
        sys.exit(
            f"the sheet returned HTML, not CSV - {doc} is probably not shared "
            '"anyone with the link can view", or the tab name is wrong.'
        )
    wide = pd.read_csv(io.StringIO(resp.text))
    id_col = wide.columns[0]  # "Country"
    long = wide.melt(id_vars=[id_col], var_name="YEAR", value_name="EMISSIONS_CH4_KT")

    frame = pl.from_pandas(long.astype(str)).select(
        pl.col(id_col).alias("_name"),
        pl.col("YEAR").str.strip_chars().cast(pl.Int64, strict=False).alias("YEAR"),
        pl.col("EMISSIONS_CH4_KT")
        .str.replace_all(",", "")
        .str.strip_chars()
        .replace({"": None, "nan": None, "None": None})
        .cast(pl.Float64, strict=False)
        .alias("EMISSIONS_CH4_KT"),
    )
    frame = frame.filter(
        (pl.col("_name").str.strip_chars() != "")
        & pl.col("YEAR").is_not_null()
        & pl.col("EMISSIONS_CH4_KT").is_not_null()
    )
    names = frame["_name"].unique().to_list()
    code_map = {n: country_name_to_code(n) for n in names}
    unresolved = sorted(n for n, c in code_map.items() if c is None)
    if unresolved:
        log(f"  ! {len(unresolved)} sheet country name(s) not matched, dropped: {unresolved}")
    frame = (
        frame.with_columns(
            pl.col("_name").replace_strict(code_map, default=None).alias("COUNTRY_CODE")
        )
        .drop_nulls(["COUNTRY_CODE"])
        .with_columns(
            pl.lit(label).alias("SOURCE"),
            pl.lit(_NON_ANNEX_FILENAME).alias("FILENAME"),
        )
    )
    before = len(frame)
    frame = frame.unique(subset=["COUNTRY_CODE", "YEAR"], keep="first")
    if len(frame) != before:
        log(f"  ! non-Annex sheet had {before - len(frame)} duplicate (country, year) row(s)")
    if debug:
        _debug_frame("  non-Annex sheet", frame)
    return frame.select(OUTPUT_COLUMNS)


def _debug_frame(tag: str, df: pl.DataFrame) -> None:
    if not len(df):
        log(f"{tag}: empty")
        return
    yr = "YEAR" if "YEAR" in df.columns else None
    cc = "COUNTRY_CODE" if "COUNTRY_CODE" in df.columns else None
    span = f"{df[yr].min()}-{df[yr].max()}" if yr else "?"
    n_cc = df[cc].n_unique() if cc else "?"
    log(f"{tag}: {len(df)} rows | {n_cc} countries | years {span} | cols {df.columns}")


def discover_rounds(crt_root: Path) -> list[Path]:
    """Subfolders whose name *starts* with a 4-digit year and hold either loose
    CRT .zip files or a single .tar.* bundle of them, oldest year first. Accepts
    '2024', '2024 CRTs', '2024-crt', etc."""
    if not crt_root.is_dir():
        raise SystemExit(
            f"{crt_root} does not exist. Use --fetch to download the CRT zips, "
            "or point --crt-root at a folder of per-round subfolders (each named "
            "starting with its 4-digit year, e.g. '2024' or '2024 CRTs')."
        )
    dated: list[tuple[int, Path]] = []
    for p in sorted(crt_root.iterdir()):
        m = re.match(r"(19|20)\d{2}", p.name)
        if not (p.is_dir() and m):
            continue
        has_zip = any(p.glob("*.zip"))
        has_tar = any(q.name.lower().endswith(_TARBALL_SUFFIXES) for q in p.iterdir())
        if has_zip or has_tar:
            dated.append((int(m.group(0)), p))
    if not dated:
        raise SystemExit(
            f"no <year>* subfolders with .zip or .tar.* files under {crt_root} "
            "(expected e.g. '2024 CRTs/'  '2025 CRTs/'  '2026 CRTs/')"
        )
    by_year: dict[int, Path] = {}
    for year, p in dated:
        if year in by_year:
            raise SystemExit(
                f"round {year} has two folders: {by_year[year].name!r} and "
                f"{p.name!r} - keep one per round"
            )
        by_year[year] = p
    return [by_year[y] for y in sorted(by_year)]


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--version", action="version", version=f"crt_methane {VERSION}")
    ap.add_argument(
        "--crt-root",
        type=Path,
        default=Path("crt_data"),
        help="dir of <year>/ CRT-zip folders (also where --fetch downloads to; default ./crt_data)",
    )
    ap.add_argument("--out", type=Path, default=Path("crt_methane_ch4.csv"))
    ap.add_argument(
        "--floor-year",
        type=int,
        default=2021,
        help="CRT supplies this year onward; DI supplies everything before (default 2021 - "
        "lowered from 2022 on 2026-09-16 because the DI Zenodo mirror's per-country "
        "coverage sometimes stops a year short, e.g. Australia's ended 2020, leaving "
        "2021 uncovered by either source at the old floor)",
    )
    ap.add_argument(
        "--category-label",
        default="1.B.1.a Coal mining and handling",
        help="normalised SOURCE label written for every row",
    )
    ap.add_argument("--no-di", action="store_true", help="CRT only, skip the DI historic pull")
    ap.add_argument("--di-only", action="store_true", help="DI only, skip CRT parsing")
    ap.add_argument(
        "--di-source",
        choices=["api", "zenodo"],
        default="api",
        help="'api' = live di.unfccc.int (Imperva-walled); 'zenodo' = the "
        "maintainers' mirror (di_pull.py's fallback), no WAF, different provenance",
    )
    ap.add_argument(
        "--di-scope",
        choices=["annex-one", "non-annex-one", "all"],
        default="annex-one",
        help="DI party scope for the historic half. non-annex-one / all require "
        "--di-source api + --cookies (the Zenodo mirror carries 1.B.1.a for "
        "Annex I only; non-Annex parties report it only in the live DI's "
        "nonAnnexOne tree)",
    )
    ap.add_argument(
        "--di-category",
        default=_DI_CATEGORY,
        help="DI category label to match for the historic half (substring, "
        f"case/space-insensitive; default {_DI_CATEGORY!r}). Non-Annex "
        "inventories stop at '1.B.1 Solid Fuels' - pass that for --di-scope "
        "non-annex-one",
    )
    ap.add_argument(
        "--non-annex-sheet",
        default="1G-r_7ZvI5p-YU201tist4-RjnPLJg2pxnnnMFGt4iis",
        help="Google Sheet URL or doc id of the hand-maintained non-Annex data "
        "(the repo-faithful non-Annex source; must be link-viewable). Adds an "
        "ANNEX_FLAG column to the output. Needs: pip install pycountry",
    )
    ap.add_argument(
        "--non-annex-tab",
        default="UNFCCC Non Annex",
        help="tab name within --non-annex-sheet (default 'UNFCCC Non Annex')",
    )
    ap.add_argument("--keep-eua", action="store_true", help="do not remap DI party code EUA -> EU")
    ap.add_argument(
        "--fetch",
        action="store_true",
        help="download CRT zips into <crt-root>/<round>/ before parsing "
        "(needs: pip install requests beautifulsoup4)",
    )
    ap.add_argument(
        "--fetch-browser",
        action="store_true",
        help="fetch via a real headless Chromium (beats the Imperva WAF when "
        "--cookies can't); needs: pip install playwright && playwright install chromium",
    )
    ap.add_argument(
        "--headed",
        action="store_true",
        help="with --fetch-browser, show the browser window (least bot-like; "
        "lets you clear an interactive check by hand)",
    )
    ap.add_argument("--fetch-only", action="store_true", help="do the fetch, then stop")
    ap.add_argument(
        "--fetch-rounds",
        type=lambda s: [int(x) for x in s.split(",")],
        help="cycle years to download, e.g. 2024,2025,2026 (default: all in the listing)",
    )
    ap.add_argument(
        "--fetch-parties",
        type=lambda s: [x.strip() for x in s.split(",")],
        help="restrict --fetch to parties whose name contains one of these (e.g. Australia,Poland)",
    )
    ap.add_argument(
        "--cookies",
        default="",
        help="UNFCCC Incapsula cookies for --fetch and the DI query "
        '("visid_incap_...=...; incap_ses_...=..."); defaults to $UNFCCC_COOKIES',
    )
    ap.add_argument(
        "--recurse-zips",
        action="store_true",
        help="descend one level into nested .zip members (default: flat, as the repo)",
    )
    ap.add_argument(
        "--no-draft-fallback",
        action="store_true",
        help="don't fall back to a V0 draft submission for a (country, year) with no "
        "non-draft CRT coverage anywhere - drop V0 outright instead (old behaviour)",
    )
    ap.add_argument("--list-rounds", action="store_true", help="print discovered rounds and exit")
    ap.add_argument("--debug", action="store_true", help="print extraction diagnostics")
    args = ap.parse_args()

    do_fetch = args.fetch or args.fetch_only or args.fetch_browser
    if args.no_di and args.di_only:
        ap.error("--no-di and --di-only are mutually exclusive")
    if do_fetch and args.di_only:
        ap.error("--fetch* cannot be combined with --di-only")

    if do_fetch:
        how = (
            ("a visible browser" if args.headed else "headless Chromium")
            if args.fetch_browser
            else "the reports listing (requests)"
        )
        log(f"fetching CRT zips via {how} ...")
        fetch_crt_zips(
            args.crt_root,
            args.fetch_rounds,
            args.fetch_parties,
            args.cookies,
            browser=args.fetch_browser,
            headed=args.headed,
        )
        if args.fetch_only:
            return

    if args.list_rounds:
        for r in discover_rounds(args.crt_root):
            print(f"{r.name}  {len(list(r.glob('*.zip')))} zip(s)  {r}")
        return

    parts: list[pl.DataFrame] = []

    if not args.di_only:
        rounds = discover_rounds(args.crt_root)
        log(f"CRT rounds (oldest first): {[r.name for r in rounds]}")
        # skip_drafts=False: one parse per round either way (the expensive
        # part is the zip/tar decompression, not this), tags each row
        # DRAFT_FLAG so both a draft-inclusive and a draft-free series can be
        # built from it - same approach crt_amm.py uses, see its "DRAFT (V0)
        # FALLBACK" docstring note.
        cleaned_rounds = [
            clean_round(
                read_round(r, args.recurse_zips, args.floor_year, skip_drafts=False),
                args.floor_year,
                args.category_label,
                args.debug,
            )
            for r in rounds
        ]
        # main series: draft (V0) rows excluded. Oldest round is priority;
        # each newer round only fills missing (YEAR, COUNTRY_CODE, SOURCE).
        crt = reduce(
            merge_crt_rounds,
            [c.filter(~pl.col("DRAFT_FLAG")).drop("DRAFT_FLAG") for c in cleaned_rounds],
        ).select(OUTPUT_COLUMNS)

        if not args.no_draft_fallback:
            # everything achievable if drafts count too, same round-priority
            # merge. Any (COUNTRY_CODE, YEAR) that appears here but not in the
            # draft-free `crt` above has zero non-draft coverage anywhere, so
            # it can only have come from a draft - safe to add back as a
            # flagged fallback without ever outranking a real submission.
            crt_with_drafts = reduce(
                merge_crt_rounds, [c.drop("DRAFT_FLAG") for c in cleaned_rounds]
            ).select(OUTPUT_COLUMNS)
            draft_only = crt_with_drafts.join(crt, on=["COUNTRY_CODE", "YEAR"], how="anti")
            if len(draft_only):
                log(
                    f"  ! {len(draft_only)} row(s) have no non-draft CRT submission "
                    "anywhere, kept as a flagged draft fallback: "
                    f"{sorted(set(draft_only['COUNTRY_CODE'].to_list()))}"
                )
                draft_only = draft_only.with_columns(
                    (pl.col("SOURCE") + " (unreviewed draft submission)").alias("SOURCE")
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
            args.di_scope,
            args.di_category,
        )
        log(f"DI < {args.floor_year}: {len(di)} rows, {di['COUNTRY_CODE'].n_unique()} countries")
        parts.append(di)

    non_annex = None
    if args.non_annex_sheet:
        non_annex = load_non_annex_sheet(
            args.non_annex_sheet, args.non_annex_tab, args.category_label, args.debug
        )
        log(
            f"non-Annex sheet: {len(non_annex)} rows, "
            f"{non_annex['COUNTRY_CODE'].n_unique()} countries, "
            f"years {non_annex['YEAR'].min()}-{non_annex['YEAR'].max()}"
        )

    out_cols = list(OUTPUT_COLUMNS)
    if non_annex is not None:
        # ember-data-processing's ANNEX_FLAG: CRT + DI historic are Annex I,
        # the hand-maintained sheet is not.
        parts = [p.with_columns(pl.lit(True).alias("ANNEX_FLAG")) for p in parts]
        parts.append(non_annex.with_columns(pl.lit(False).alias("ANNEX_FLAG")))
        out_cols.append("ANNEX_FLAG")

    out = (
        pl.concat(parts, how="vertical_relaxed")
        .sort(["COUNTRY_CODE", "YEAR"])
        .select(out_cols)
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.write_csv(args.out)

    log("")
    log(f"wrote {len(out)} rows -> {args.out}")
    log(f"  countries : {out['COUNTRY_CODE'].n_unique()}")
    log(f"  years     : {out['YEAR'].min()}-{out['YEAR'].max()}")
    tagged = out.with_columns(
        pl.when(pl.col("FILENAME") == "historic")
        .then(pl.lit("DI historic"))
        .when(pl.col("FILENAME") == _NON_ANNEX_FILENAME)
        .then(pl.lit("non-Annex sheet"))
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
