"""Standalone local recreation of Ember's EIA coal-production fetch.

Mirrors:
  - src/resources/source_resources/eia.py :: EiaResource.get_coal_production_urls / get_coal_production_data
  - src/pipelines/methane/assets_sources/eia.py :: eia_coal_production_clean

Pulls annual coal production by country from the EIA v2 International API
(activityId=1 "Production", productId 7 "Coal" + 130 "Metallurgical coal",
unit MT, country-level only), cleans/pivots it the same way the pipeline does,
and writes CSV + parquet. The last date chunk has no end year, so it naturally
runs through the latest available year (2024 as of now).

Usage:
    export EIA_API_KEY=xxxxxxxx        # free key: https://www.eia.gov/opendata/register.php
    uv run --env-file .env.local python eia_coal_production.py       # from the repo (has polars)
    # or:  pip install polars  &&  python eia_coal_production.py
    python eia_coal_production.py --api-key xxxx --outdir . --raw
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import polars as pl

BASE_URL = "https://api.eia.gov/v2/international/data/"
API_LIMIT = 5000  # EIA returns at most 5000 rows per request

# productId -> friendly name (as the API returns it in productName)
PRODUCTS: dict[str, str] = {"7": "Coal", "130": "Metallurgical coal"}

# Same decade chunking as the pipeline; final chunk is open-ended -> latest year.
DATE_PARAMS: list[str] = [
    "start=1990&end=2000",
    "start=2000&end=2010",
    "start=2010&end=2020",
    "start=2020",
]


def build_urls(api_key: str) -> list[str]:
    return [
        f"{BASE_URL}?api_key={api_key}"
        f"&frequency=annual&data[0]=value&facets[activityId][]=1&facets[productId][]={p}"
        f"&facets[unit][]=MT&facets[countryRegionTypeId][]=c&{d}"
        "&sort[0][column]=countryRegionId&sort[0][direction]=asc&offset=0"
        f"&length={API_LIMIT}"
        for d in DATE_PARAMS
        for p in PRODUCTS
    ]


def _snake_upper(name: str) -> str:
    """countryRegionId -> COUNTRY_REGION_ID (matches the repo's IO-manager convention)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()


def fetch_raw(api_key: str, retries: int = 3, pause: float = 2.0) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for url in build_urls(api_key):
        shown = url.replace(api_key, "***")
        print(f"Requesting: {shown}", file=sys.stderr)
        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(url, timeout=120) as resp:
                    payload = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:500]
                if attempt == retries:
                    raise SystemExit(f"HTTP {e.code} from EIA: {body}") from e
                print(f"  HTTP {e.code}, retrying ({attempt}/{retries})...", file=sys.stderr)
                time.sleep(pause * attempt)
            except urllib.error.URLError as e:
                if attempt == retries:
                    raise
                print(f"  {e}, retrying ({attempt}/{retries})...", file=sys.stderr)
                time.sleep(pause * attempt)

        rows = payload["response"]["data"]
        # EIA returns some fields with inconsistent types across rows (e.g. value as
        # str "3" in one row, number in another). Force everything to string on the
        # way in, exactly like the pandas version tolerated; clean() casts later.
        keys = {k for r in rows for k in r}
        df = pl.DataFrame(
            [{k: (None if r.get(k) is None else str(r.get(k))) for k in keys} for r in rows],
            schema={k: pl.Utf8 for k in keys},
        )
        if df.height == API_LIMIT:
            raise SystemExit(
                f"Chunk returned exactly {API_LIMIT} rows for {shown} -- response is probably "
                "truncated. Narrow the date range / split the query (see SuspectedTruncatedResponseFailure)."
            )
        df.columns = [_snake_upper(c) for c in df.columns]
        frames.append(df)

    return pl.concat(frames, how="diagonal_relaxed")


def clean(raw: pl.DataFrame) -> pl.DataFrame:
    return (
        raw.select(
            pl.col("PERIOD").cast(pl.Int64).alias("YEAR"),
            "PRODUCT_NAME",
            pl.col("COUNTRY_REGION_ID").alias("COUNTRY_CODE"),
            (
                pl.col("VALUE").cast(pl.Utf8).replace("--", None).replace("ie", None).cast(pl.Float64)
                / 1000
            ).alias("PRODUCTION_MT"),
        )
        # The API treats both `start` and `end` as inclusive, so the decade chunks
        # (…end=2000 / start=2000…) each return the boundary years 2000/2010/2020
        # twice. The rows are identical; drop the dupes before pivoting.
        .unique(subset=["YEAR", "COUNTRY_CODE", "PRODUCT_NAME"], keep="first")
        .pivot(on="PRODUCT_NAME", index=["YEAR", "COUNTRY_CODE"], values="PRODUCTION_MT")
        .rename(
            {
                "Coal": "PRODUCTION_TOTAL_MT",
                "Metallurgical coal": "PRODUCTION_METALLURGICAL_MT",
            }
        )
        .with_columns(
            pl.col("PRODUCTION_TOTAL_MT").max().over("COUNTRY_CODE").alias("PRODUCTION_MT_MAX"),
            (pl.col("PRODUCTION_TOTAL_MT") - pl.col("PRODUCTION_METALLURGICAL_MT")).alias(
                "PRODUCTION_OTHER_MT"
            ),
            pl.lit(False).alias("FORECAST_FLAG"),
            pl.lit("EIA").alias("SOURCE"),
        )
        .filter(pl.col("PRODUCTION_MT_MAX") > 0)
        .drop("PRODUCTION_MT_MAX")
        .sort(["COUNTRY_CODE", "YEAR"])
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api-key", default=os.environ.get("EIA_API_KEY"))
    ap.add_argument("--outdir", default=str(Path(__file__).parent))
    ap.add_argument("--raw", action="store_true", help="also write the un-pivoted raw rows")
    args = ap.parse_args()

    if not args.api_key:
        raise SystemExit(
            "No EIA API key. Register (free) at https://www.eia.gov/opendata/register.php "
            "then set EIA_API_KEY or pass --api-key."
        )

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    raw = fetch_raw(args.api_key)
    print(f"Fetched {raw.height} raw rows, years {raw['PERIOD'].min()}-{raw['PERIOD'].max()}", file=sys.stderr)
    if args.raw:
        raw.write_csv(outdir / "eia_coal_production_raw.csv")

    out = clean(raw)
    out.write_csv(outdir / "eia_coal_production.csv")
    out.write_parquet(outdir / "eia_coal_production.parquet")
    print(f"Wrote {out.height} rows to {outdir / 'eia_coal_production.csv'}", file=sys.stderr)
    print(out.head(10))


if __name__ == "__main__":
    main()
