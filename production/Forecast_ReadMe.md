# IEA Coal Production Forecast — Ember Interpretation Methodology (2025 vintage)

Rebuilds `iea_coal_production_forecast.csv` (`Code, Year, Production` in kt,
2025–2030) from **IEA *Coal 2025: Analysis and forecast to 2030***. Replaces the
2024 vintage, which followed *Coal 2024* and stopped at 2027. Builder:
`iea_coal_production_forecast.py` (`--check` re-derives the interpolated years).

## What changed vs the 2024 ReadMe

- Horizon extended 2027 → **2030**; base year is now 2025.
- Country anchors are read from *Coal 2025* **General Annex Table 4 (Total coal
  production, Mt)** for the years the table publishes: **2025, 2027, 2030**
  (plus supporting numbers from the Supply chapter narrative).
- **Mongolia (MNG)** added as its own row (Table 4 breaks it out: 105 / 97 / 102 Mt).
- Gap years are filled by explicit recipes so the series is reproducible from
  the anchors (see below), rather than by ad-hoc spreadsheet entry.

## Anchor sources

| Row | 2025 | 2027 | 2030 | Source in *Coal 2025* |
|---|---|---|---|---|
| CHN | 4730 | 4563 | 4439 | Table 4; p.48 "decline by 291 Mt to 4 439 Mt from 2025" |
| IND | 1089 | 1154 | 1283 | Table 4; p.51 "nearly 1.3 bn t by 2030, ~3%/yr" |
| AUS | 446 | 438 | 409 | Table 4; p.54–55 |
| MNG | 105 | 97 | 102 | Table 4 |
| IDN | 778 | 713 | 671 | Table 4; p.53 "fall by 107 Mt to 671 Mt by 2030" |
| USA | 473 | 456 | 386 | Table 4; p.56 "386 Mt by 2030, −18% on 2025" |
| RUS | 427 | 425 | 422 | Table 4; p.58 |
| COL | 67 | 58 | 49→50 | Table 4 *Central & South America* row (Colombia dominates) |
| CAN | 50 | — | 45 | p.60 "+6% to 50 Mt" (2025), "−10% to 45 Mt by 2030" |
| ZAF | 234→233 | 234 | 228 | p.61 "estimated at 234 Mt in 2025 … 228 Mt in 2030" |

EU rows (**DEU, POL, BGR, CZE**) are a hand allocation of the EU aggregate
(Table 4: EU 242 Mt in 2025 → 132 Mt in 2030; Supply p.58–59 identifies Germany
as the largest lignite producer, Poland as ~all EU steam/met coal, with Bulgaria
and Czechia the next lignite producers and a revised Bulgarian NECP slowing —
not halting — its decline).

## Gap-year recipes (all reproducible from the anchors)

- **2026** = geometric mean of the 2025 and 2027 anchors — *unless* the report is
  silent on that country's near term, in which case 2026 is **carried from the
  2024-vintage forecast file** (IND, USA, COL, BGR, CZE).
- **2028, 2029** = constant annual rate (CAGR) between the 2027 and 2030 anchors.
- **CAN** has no 2027 anchor → a single CAGR glide 2025 → 2030.
- **Flat** (held at 2025) where *Coal 2025* says output stays "close to 2025 …
  through to 2030": **THA, VNM, PHL, ZMB, ETH**; **TZA** is not covered and is
  carried flat from the prior vintage.
- **Manual interior path** where the report gives a country-specific narrative
  the smooth recipes can't capture:
  - **CHN / AUS / IDN / RUS** — 2026 dips below both neighbouring anchors
    (safety-campaign / low-price / loss-making cuts described in Supply);
  - **LAO** — ~10 %/yr to 2029, then a step in 2030 for the new export-oriented
    coal-fired plant (p.53);
  - **ZWE** — front-loaded ramp to the "+2 Mt by 2030" target (Hwange refurb +
    regional steel demand, p.61);
  - **MOZ** — Benga coking-coal mine "set to more than triple over the next two
    years" (p.61), then ~8 %/yr;
  - **COL** — interior years track the Colombia-specific decline toward ~44 Mt,
    with 2030 snapped back to the regional-row value (50).

`--check` prints the recipe re-derivation: RECIPE rows match the stored series to
< 0.01 %; MANUAL rows are shown against a plain CAGR for reference only.

## Assumptions carried from the 2024 ReadMe

- Where the report calls production "flat", the base-year value is held forward.
- Where only two endpoints are given, the annual rate of change between them is
  assumed constant.
- Where a 2025 baseline is missing, the prior US EIA / prior-vintage value is
  used with the report's directional change applied.
- African countries: no change assumed except Mozambique and Zimbabwe.

## Output / downstream

`iea_coal_production_forecast.csv` mirrors the Dropbox source
`methane/source_data/iea/iea_production_forecast.csv` consumed by
`src/pipelines/methane/assets_sources/iea.py :: iea_coal_production_forecast`
→ `transform_iea_production` (renames `Code`→`COUNTRY_CODE`,
`Production`→`PRODUCTION_KT`, `/1000` → `PRODUCTION_TOTAL_MT`, flags
`Year >= 2025` as `FORECAST_FLAG`).
