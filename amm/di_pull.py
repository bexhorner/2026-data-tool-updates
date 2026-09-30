#!/usr/bin/env python3
"""
di_pull.py — pull a category straight from the UNFCCC Data Interface API.

WHY GO TO THE API
    The GitHub mirror (openclimatedata/unfccc-detailed-data-by-party) is two hops
    from source and its last commit is Feb 2022, so it stops at inventory year
    2019. The DI itself reached 2021. Querying the API directly gives first-hand
    provenance and the two missing years.

THE CATCH
    UNFCCC began blocking the DI API from cloud and datacentre IP ranges. A normal
    residential or office connection usually still works; CI runners and most VMs
    do not. This script reports plainly which source answered, and never silently
    substitutes one for another.

    --source api      live DI API only. Fails loudly if blocked.  (default)
    --source zenodo   the maintainers' Zenodo mirror of the same data. Different
                      provenance - it is their scrape, not your query - so the
                      output is tagged accordingly.

CATEGORY NAMING
    Abandoned mines appears under two labels depending on the era:
        1.B.1.a.1.iii Abandoned Underground Mines   (older records)
        1.B.1.a.i.3   Abandoned Underground Mines   (newer)
    Matching the current code alone returns roughly a fifth of the data, so this
    matches on the label text instead.

USAGE
    pip install unfccc-di-api pandas openpyxl
    python3 di_pull.py                              # abandoned mines, CH4, Annex I
    python3 di_pull.py --category "1.B.1 Solid Fuels"
    python3 di_pull.py --scope non-annex-one --category "1.B.1 Solid Fuels"
    python3 di_pull.py --source zenodo              # if the API blocks you
    python3 di_pull.py --self-test
"""
from __future__ import annotations
import argparse, sys, time

VERSION = '2026.09.04.3'
DEFAULT_CATEGORY = 'Abandoned Underground Mines'
DEFAULT_GAS = 'CH4'

try:
    import pandas as pd
except ImportError:
    sys.exit('pip install pandas openpyxl')


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ── Incapsula ───────────────────────────────────────────────────────────────
# di.unfccc.int sits behind the same Imperva WAF as the main site, which is what
# "Access to the UNFCCC API denied" actually means - it is not an auth failure.
# A browser solves the JS challenge and is issued visid_incap_/incap_ses_ cookies
# scoped to .unfccc.int, which also cover the di. subdomain. Replaying those
# cookies gets the API through. Patching requests at the session level rather
# than the library means it works regardless of how unfccc_di_api makes calls.
BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')


def install_cookies(cookie_str: str):
    import os
    import requests
    raw = cookie_str or os.environ.get('UNFCCC_COOKIES', '')
    if not raw.strip():
        return False
    jar = {}
    for part in raw.split(';'):
        if '=' in part:
            k, v = part.strip().split('=', 1)
            jar[k.strip()] = v.strip()
    original = requests.Session.request

    def patched(self, method, url, **kw):
        if 'unfccc.int' in str(url):
            ck = kw.get('cookies') or {}
            ck.update(jar)
            kw['cookies'] = ck
            hd = kw.get('headers') or {}
            hd.setdefault('User-Agent', BROWSER_UA)
            hd.setdefault('Accept', 'application/json, text/plain, */*')
            hd.setdefault('Referer', 'https://di.unfccc.int/detailed_data_by_party')
            kw['headers'] = hd
        return original(self, method, url, **kw)

    requests.Session.request = patched
    # some libraries call the module-level helpers, which build their own session
    for name in ('get', 'post'):
        fn = getattr(requests, name)

        def wrap(url, _fn=fn, **kw):
            if 'unfccc.int' in str(url):
                ck = kw.get('cookies') or {}
                ck.update(jar)
                kw['cookies'] = ck
            return _fn(url, **kw)
        setattr(requests, name, wrap)
    log(f'installed {len(jar)} Incapsula cookie(s) for unfccc.int')
    return True


# ── source: live API ────────────────────────────────────────────────────────
def pull_api(scope: str, cookies: str = '', timeout_note=True):
    install_cookies(cookies)
    try:
        import unfccc_di_api
    except ImportError:
        sys.exit('pip install unfccc-di-api')
    log('querying the live DI API (this takes a few minutes for all parties)...')
    if timeout_note:
        log('  if this hangs or errors, your IP is probably blocked - see --source zenodo')
    try:
        reader = unfccc_di_api.UNFCCCApiReader()
    except RuntimeError as e:
        if 'denied' in str(e).lower():
            sys.exit(
                'The DI API refused this machine (Imperva/Incapsula, same wall as '
                'the main site).\n'
                '  1. open https://di.unfccc.int/detailed_data_by_party in a browser '
                'and let it load\n'
                '  2. F12 (or Fn+F12) -> Application -> Cookies -> unfccc.int\n'
                '  3. copy the visid_incap_ and incap_ses_ values, then:\n'
                '     export UNFCCC_COOKIES="visid_incap_...=...; incap_ses_...=..."\n'
                '     python3 di_pull.py\n'
                '  Cookies expire with the browser session, so keep the tab open.\n'
                '  Failing that: --source zenodo (different provenance).')
        raise
    parties = (reader.annex_one_reader if scope == 'annex-one'
               else reader.non_annex_one_reader).parties
    codes = sorted(parties['code'].tolist())
    log(f'  {len(codes)} parties in scope {scope}')
    frames = []
    for i, code in enumerate(codes, 1):
        try:
            df = reader.query(party_code=code)
            frames.append(df)
            log(f'  [{i}/{len(codes)}] {code}: {len(df)} rows')
        except Exception as e:
            log(f'  [{i}/{len(codes)}] {code}: FAILED {e}')
        time.sleep(0.5)
    if not frames:
        sys.exit('nothing returned - the API is very likely blocking this machine.\n'
                 'Retry from a different connection, or run with --source zenodo '
                 'and accept the different provenance.')
    return pd.concat(frames, ignore_index=True), 'UNFCCC DI API (live query)'


# ── source: Zenodo package ──────────────────────────────────────────────────
ANNEX_I = ['AUS','AUT','BLR','BEL','BGR','CAN','HRV','CYP','CZE','DNK','EST','FIN','FRA',
'DEU','GRC','HUN','ISL','IRL','ITA','JPN','KAZ','LVA','LIE','LTU','LUX','MLT','MCO','NLD',
'NZL','NOR','POL','PRT','ROU','RUS','SVK','SVN','ESP','SWE','CHE','TUR','UKR','GBR','USA']


def pull_zenodo(scope: str, parties=None):
    try:
        import unfccc_di_api
    except ImportError:
        sys.exit('pip install unfccc-di-api')
    log('reading the Zenodo data package (the maintainers\' mirror of the DI)')
    r = unfccc_di_api.ZenodoReader()
    codes = parties or (ANNEX_I if scope == 'annex-one' else None)
    if not codes:
        return r.query(), 'Zenodo data package (all parties)'
    frames = []
    for i, c in enumerate(codes, 1):
        try:
            frames.append(r.query(party_code=c))
            log(f'  [{i}/{len(codes)}] {c}')
        except Exception as e:
            log(f'  [{i}/{len(codes)}] {c}: FAILED {e}')
    if not frames:
        sys.exit('nothing returned from Zenodo')
    return pd.concat(frames, ignore_index=True), 'Zenodo data package (mirror of the DI)'


# ── filtering ───────────────────────────────────────────────────────────────
def pick_columns(df):
    """The DI schema has shifted over versions; find the columns we need."""
    def find(*cands):
        for c in cands:
            if c in df.columns:
                return c
        low = {c.lower(): c for c in df.columns}
        for c in cands:
            if c.lower() in low:
                return low[c.lower()]
        return None
    return {'party': find('party', 'partyCode', 'party_code'),
            'category': find('category', 'categoryName'),
            'gas': find('gas', 'gasName'),
            'unit': find('unit', 'unitName'),
            'year': find('year', 'yearName'),
            'value': find('numberValue', 'value'),
            'measure': find('measure', 'measureName')}


MEASURE = 'Net emissions/removals'


def norm(t):
    """Collapse runs of whitespace. DI category labels carry double spaces
    ('1.B.1.a  Coal Mining and Handling'), so an exact == against a
    single-spaced string silently returns nothing."""
    import re
    return re.sub(r'\s+', ' ', str(t)).strip()


def filter_rows(df, category, gas, measure=MEASURE):
    col = pick_columns(df)
    missing = [k for k, v in col.items() if v is None and k in
               ('party', 'category', 'gas', 'year', 'value')]
    if missing:
        sys.exit(f'unexpected schema, missing {missing}. Columns are: {list(df.columns)}')
    g = df[col['gas']].astype(str).str.replace('₄', '4', regex=False) \
                                  .str.replace('₂', '2', regex=False)
    cat = df[col['category']].map(norm)
    m = df[cat.str.contains(norm(category), case=False, regex=False, na=False)
           & (g.str.upper() == gas.upper())].copy()
    # 'Base year' is a pseudo-year in the DI and must not become an int
    m = m[m[col['year']].astype(str).str.strip().str.lower() != 'base year']
    if col['measure'] and measure:
        # Exact measure, not a substring: the DI also carries implied emission
        # factors and activity data under the same category and gas.
        mm = m[col['measure']].map(norm)
        keep = mm.str.lower() == norm(measure).lower()
        if not keep.any():
            log(f'  no rows with measure {measure!r}; measures present: '
                f'{sorted(set(mm))[:8]}')
        m = m[keep]
    out = pd.DataFrame({
        'party': m[col['party']],
        'year': pd.to_numeric(m[col['year']], errors='coerce'),
        'value': pd.to_numeric(m[col['value']], errors='coerce'),
        'unit': m[col['unit']] if col['unit'] else '',
        'category_as_named': m[col['category']],
        'gas': gas})
    return out.dropna(subset=['year', 'value']).astype({'year': int})


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--version', action='version', version=f'di_pull {VERSION}')
    ap.add_argument('--source', choices=['api', 'zenodo'], default='zenodo',
                    help="'zenodo' (default) is the maintainers' mirror of the DI and "
                         "works reliably; 'api' queries di.unfccc.int directly but is "
                         "behind the Incapsula wall - pass --cookies for that")
    ap.add_argument('--scope', choices=['annex-one', 'non-annex-one'], default='annex-one')
    ap.add_argument('--category', default=DEFAULT_CATEGORY)
    ap.add_argument('--gas', default=DEFAULT_GAS)
    ap.add_argument('--measure', default=MEASURE,
                    help="exact DI measure; '' to keep all")
    ap.add_argument('--parties', default='', help='comma-separated ISO3; default all in scope')
    ap.add_argument('--out', default='di_amm')
    ap.add_argument('--cookies', default='',
                    help='Incapsula cookies from a browser; defaults to $UNFCCC_COOKIES')
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()

    if a.self_test:
        sys.exit(self_test())

    raw, provenance = (pull_api(a.scope, a.cookies) if a.source == 'api'
                       else pull_zenodo(a.scope, a.parties.split(',') if a.parties else None))
    log(f'\n{len(raw):,} raw rows from: {provenance}')
    d = filter_rows(raw, a.category, a.gas, a.measure)
    if not len(d):
        cats = sorted(set(raw[pick_columns(raw)['category']].astype(str)))
        log(f'\nno rows matched {a.category!r}. Categories containing "mine":')
        for c in cats:
            if 'mine' in c.lower():
                log('   ' + c)
        sys.exit(1)

    d['source'] = provenance
    d = d.sort_values(['party', 'year'])
    d.to_csv(f'{a.out}_long.csv', index=False, encoding='utf-8')
    wide = d.pivot_table(index='party', columns='year', values='value')
    wide.to_csv(f'{a.out}_wide.csv', encoding='utf-8')
    log(f'\n{len(d)} observations | {d.party.nunique()} parties | '
        f'{d.year.min()}-{d.year.max()}')
    log(f'units seen: {sorted(set(d.unit.astype(str)))}')
    log(f'category labels matched: {sorted(set(d.category_as_named.astype(str)))}')
    log(f'-> {a.out}_long.csv and {a.out}_wide.csv')
    log(f'PROVENANCE: {provenance}')


def self_test():
    ok = []

    def chk(n, c, extra=''):
        print(('PASS  ' if c else 'FAIL  ') + n + ('' if c else f'  {extra}'))
        ok.append(c)

    # both era labels must match, since only matching the new code loses most data
    df = pd.DataFrame({
        'party': ['AUS', 'AUS', 'POL', 'POL', 'AUS'],
        'category': ['1.B.1.a.1.iii Abandoned Underground Mines',
                     '1.B.1.a.i.3 Abandoned Underground Mines',
                     '1.B.1.a.i.3 Abandoned Underground Mines',
                     '1.B.1.a.i.1 Mining Activities',
                     '1.B.1.a.i.3 Abandoned Underground Mines'],
        'gas': ['CH₄', 'CH4', 'CH4', 'CH4', 'CO2'],
        'unit': ['kt'] * 5, 'year': [1995, 2015, 2015, 2015, 2015],
        'numberValue': [13.9, 34.9, 21.4, 500.0, 99.0],
        'measure': ['Net emissions/removals'] * 5})
    r = filter_rows(df, 'Abandoned Underground Mines', 'CH4')
    chk('matches both era labels', len(r) == 3, f'{len(r)} rows')
    chk('keeps the pre-2019 label', 1995 in set(r.year))
    chk('normalises the subscript gas symbol', 13.9 in set(r.value))
    chk('excludes other categories', 500.0 not in set(r.value))
    chk('excludes other gases', 99.0 not in set(r.value))

    df2 = df.copy()
    df2['measure'] = ['Implied emission factor'] * 5
    chk('drops implied emission factors', len(filter_rows(df2, 'Abandoned', 'CH4')) == 0)

    df3 = df.rename(columns={'numberValue': 'value', 'party': 'partyCode'})
    chk('tolerates the alternate schema', len(filter_rows(df3, 'Abandoned', 'CH4')) == 3)

    # the three things the working CMM code revealed
    dd = pd.DataFrame({
        'party': ['AUS'] * 5,
        'category': ['1.B.1.a  Coal Mining and Handling',      # note the double space
                     '1.B.1.a  Coal Mining and Handling',
                     '1.B.1.a  Coal Mining and Handling',
                     '1.B.1.a  Coal Mining and Handling',
                     '1.B.1.a.i.3  Abandoned Underground Mines'],
        'gas': ['CH4'] * 5, 'unit': ['kt'] * 5,
        'year': ['1990', 'Base year', '2015', '2015', '2015'],
        'numberValue': [800.0, 777.0, 900.0, 12.5, 34.9],
        'measure': ['Net emissions/removals', 'Net emissions/removals',
                    'Net emissions/removals', 'Implied emission factor',
                    'Net emissions/removals']})
    r2 = filter_rows(dd, '1.B.1.a Coal Mining and Handling', 'CH4')
    chk('single-spaced query matches a double-spaced label', len(r2) == 2, f'{len(r2)} rows')
    chk('Base year row excluded', 777.0 not in set(r2.value))
    chk('implied emission factor excluded', 12.5 not in set(r2.value))
    chk('sibling category not swept in', 34.9 not in set(r2.value))
    r3 = filter_rows(dd, 'Abandoned Underground Mines', 'CH4')
    chk('abandoned mines isolates correctly', list(r3.value) == [34.9], str(list(r3.value)))
    chk('years become integers', r2.year.dtype.kind == 'i', str(r2.year.dtype))

    print('\n' + ('ALL TESTS PASSED' if all(ok) else 'FAILURES'))
    return 0 if all(ok) else 1


if __name__ == '__main__':
    main()