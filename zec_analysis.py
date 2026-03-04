import sys, os, re, subprocess, tempfile
import pandas as pd, numpy as np
sys.stdout.reconfigure(encoding='utf-8')

BN_DIR   = 'D:/AI_Projects/circ_supply/binance_perp_data/'
LOG_PATH = 'D:/AI_Projects/circ_supply/_diag_basket_log.csv'
V7_PATH  = 'D:/AI_Projects/circ_supply/perpetual_ls_v7.py'

with open(V7_PATH, encoding='utf-8') as f:
    BASE = f.read()

V8 = {'BULL_BAND':'1.05','BEAR_BAND':'0.95',
      'LONG_QUALITY_LOOKBACK':'12','SUPPLY_WINDOW':'26'}

def patch_run(extra, timeout=180):
    s = BASE
    for k,v in {**V8,**extra}.items():
        pat = rf'^({re.escape(k)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$'
        s = re.sub(pat, rf'\g<1>{v}\g<3>', s, flags=re.MULTILINE)
    s = s.replace('plt.savefig','pass').replace('print(f"[Plot]','pass  #')
    with tempfile.NamedTemporaryFile(mode='w',suffix='.py',delete=False,encoding='utf-8') as f:
        f.write(s); tmp = f.name
    r = subprocess.run([sys.executable,tmp],capture_output=True,text=True,
                       encoding='utf-8',timeout=timeout)
    os.unlink(tmp)
    return r.stdout if r.returncode==0 else '__ERROR__\n'+r.stderr[-400:]

def parse_key(stdout):
    def find(pat, g=1, cast=float):
        m = re.search(pat, stdout)
        return cast(m.group(g).replace('%','').replace('+','').strip()) if m else float('nan')
    ann    = find(r'L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%')
    sharpe = find(r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)')
    dd_m   = re.search(r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+'
                       r'[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+([\-\+]\d+\.\d+)%', stdout)
    maxdd  = float(dd_m.group(1)) if dd_m else float('nan')
    bull   = find(r'Bull\s+\d+\s+[\+\-]\d+\.\d+%\s+\d+\.\d+%\s+([\+\-]\d+\.\d+)%')
    bear   = find(r'Bear\s+\d+\s+[\+\-]\d+\.\d+%\s+\d+\.\d+%\s+([\+\-]\d+\.\d+)%')
    periods= find(r'Rebalancing periods\s*:\s*(\d+)', cast=int)
    return dict(ann=ann, sharpe=sharpe, maxdd=maxdd, bull=bull, bear=bear, periods=int(periods) if not np.isnan(periods) else 0)

# ─── Load Binance prices ────────────────────────────────────────────────────
ohlcv = pd.read_parquet(f'{BN_DIR}/weekly_ohlcv.parquet')
price_piv = (ohlcv.pivot(index='week_start', columns='symbol', values='close')
             .sort_index())
price_piv.index = pd.to_datetime(price_piv.index)

# ─── Ensure basket log exists ───────────────────────────────────────────────
if not os.path.exists(LOG_PATH):
    print('Generating basket log...')
    patch_run({'SAVE_BASKET_LOG': f'"{LOG_PATH}"'})
log = pd.read_csv(LOG_PATH, parse_dates=['date'])

# ============================================================================
# SECTION 1 — ZEC per-period attribution
# ============================================================================
print('='*70)
print('SECTION 1 — ZEC PER-PERIOD RETURN TRACE')
print('='*70)

zec_rows = log[log['long_basket'].str.contains('ZEC', na=False)].copy()
print(f'\nZEC appears in {len(zec_rows)}/45 long-basket periods\n')
print(f'  {"Date":<12} {"Regime":<10} {"ZEC ret":>9} {"BTC ret":>9} {"Alpha":>9} {"Basket size"}')
print('  '+'-'*60)

zec_contribs = []
for _, row in zec_rows.iterrows():
    t0_cmc = row['date']
    t0_bn  = t0_cmc - pd.Timedelta(days=6)
    avail0 = price_piv.index[price_piv.index >= t0_bn - pd.Timedelta(days=3)]
    if len(avail0)==0: continue
    t0k = avail0[0]
    # next rebal
    future_cmcs = log['date'][log['date'] > t0_cmc]
    t1_cmc = future_cmcs.iloc[0] if len(future_cmcs)>0 else t0_cmc+pd.Timedelta(weeks=4)
    t1_bn  = t1_cmc - pd.Timedelta(days=6)
    avail1 = price_piv.index[price_piv.index >= t1_bn - pd.Timedelta(days=3)]
    if len(avail1)==0: continue
    t1k = avail1[0]
    if t0k == t1k: continue

    zec_r = price_piv.loc[t1k,'ZEC']/price_piv.loc[t0k,'ZEC']-1 if 'ZEC' in price_piv else float('nan')
    btc_r = price_piv.loc[t1k,'BTC']/price_piv.loc[t0k,'BTC']-1 if 'BTC' in price_piv else float('nan')
    alpha = zec_r - btc_r
    bsize = len(row['long_basket'].split('|'))
    zec_contribs.append(zec_r)
    print(f'  {str(t0_cmc)[:10]:<12} {row["regime"]:<10} {zec_r:>+8.2%}  {btc_r:>+8.2%}  {alpha:>+8.2%}  {bsize}')

print(f'\n  Total ZEC return across {len(zec_contribs)} periods: {sum(zec_contribs):+.2%}')
print(f'  Avg per period: {np.mean(zec_contribs):+.2%}')
print(f'  Positive periods: {sum(r>0 for r in zec_contribs)}/{len(zec_contribs)}')

# ============================================================================
# SECTION 2 — Run v8 WITH ZEC excluded
# ============================================================================
print('\n'+'='*70)
print('SECTION 2 — V8 WITH ZEC EXCLUDED FROM UNIVERSE')
print('='*70)

# Add ZEC to EXCLUDED set via source patch
zec_excl_patch = '{"ZEC"} | '  # prepend to EXCLUDED
# Patch EXCLUDED line directly
zec_excluded_source = BASE
# Find the EXCLUDED= line and prepend ZEC
zec_excluded_source = re.sub(
    r'(EXCLUDED\s*=\s*\()',
    r'\g<1>{"ZEC"} | ',
    zec_excluded_source
)
# Apply v8 params + no plots
for k,v in V8.items():
    pat = rf'^({re.escape(k)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$'
    zec_excluded_source = re.sub(pat, rf'\g<1>{v}\g<3>', zec_excluded_source, flags=re.MULTILINE)
zec_excluded_source = zec_excluded_source.replace('plt.savefig','pass').replace('print(f"[Plot]','pass  #')

with tempfile.NamedTemporaryFile(mode='w',suffix='.py',delete=False,encoding='utf-8') as f:
    f.write(zec_excluded_source); tmp = f.name
r = subprocess.run([sys.executable,tmp],capture_output=True,text=True,encoding='utf-8',timeout=180)
os.unlink(tmp)
out_no_zec = r.stdout if r.returncode==0 else '__ERROR__\n'+r.stderr[-300:]

print('\n  Running v8 (full) ...')
out_v8 = patch_run({})
m_full   = parse_key(out_v8)
m_nozec  = parse_key(out_no_zec)

print(f'\n  {"Metric":<20} {"v8 (full)":>12} {"v8 no ZEC":>12} {"Delta":>10}')
print('  '+'-'*55)
for label, k in [('Ann. Return','ann'),('Sharpe','sharpe'),('MaxDD','maxdd'),
                  ('Bull spread','bull'),('Bear spread','bear')]:
    pct = '%' if label != 'Sharpe' else ''
    print(f'  {label:<20} {m_full[k]:>+11.2f}{pct} {m_nozec[k]:>+11.2f}{pct} {m_full[k]-m_nozec[k]:>+9.2f}{pct}')

# ============================================================================
# SECTION 3 — Run v8 on 2020-2021 data (earlier OOS)
# ============================================================================
print('\n'+'='*70)
print('SECTION 3 — V8 ON EARLIER DATA (2020-07 to 2021-12)')
print('  Tests if strategy works before ZEC\'s main run window')
print('='*70)

# Check what Binance data we have before 2022
pre2022 = ohlcv[ohlcv['week_start'] < '2022-01-01']
n_pre2022 = pre2022['symbol'].nunique()
earliest = pre2022['week_start'].min()
print(f'\n  Binance data available from: {earliest.date()}')
print(f'  Symbols with pre-2022 data: {n_pre2022}')

# Need 26w supply history + 12m LQ lookback = ~18 months before first trade
# So with data from 2020-01, first trade possible around 2021-07
print(f'\n  With 26w supply signal + 12m LQ lookback, first trade ~2021-07')
print(f'  Running v8 with START_DATE=2020-07-01 ...')

out_early = patch_run({'START_DATE': 'pd.Timestamp("2020-07-01")',
                       'END_DATE':   'pd.Timestamp("2021-12-31")'})
m_early = parse_key(out_early)

print(f'\n  Periods: {m_early["periods"]}')
print(f'  {"Metric":<20} {"Value":>12}')
print('  '+'-'*35)
for label, k in [('Ann. Return','ann'),('Sharpe','sharpe'),('MaxDD','maxdd'),
                  ('Bull spread','bull'),('Bear spread','bear')]:
    pct = '%' if label != 'Sharpe' else ''
    print(f'  {label:<20} {m_early[k]:>+11.2f}{pct}')

# Also check what ZEC looked like in 2020-2021
if 'ZEC' in price_piv.columns:
    zec_early = price_piv['ZEC'].loc['2020-01-01':'2021-12-31'].dropna()
    btc_early = price_piv['BTC'].loc['2020-01-01':'2021-12-31'].dropna()
    common = zec_early.index.intersection(btc_early.index)
    zec_tot = zec_early.loc[common].iloc[-1] / zec_early.loc[common].iloc[0] - 1
    btc_tot = btc_early.loc[common].iloc[-1] / btc_early.loc[common].iloc[0] - 1
    print(f'\n  ZEC 2020-2021 total return: {zec_tot:+.2%}')
    print(f'  BTC 2020-2021 total return: {btc_tot:+.2%}')
    print(f'  ZEC underperformed BTC by: {zec_tot-btc_tot:+.2%}')

# ============================================================================
# SECTION 4 — Top period breakdown: what exactly drove Jan 2023 & Nov 2024
# ============================================================================
print('\n'+'='*70)
print('SECTION 4 — TOP PERIOD BREAKDOWN (Best 5 spreads decomposed)')
print('  Which tokens drove each outlier period?')
print('='*70)

# Sort log by spread (gross)
log['spread'] = log['long_gross'] - log['short_gross']
top5 = log.nlargest(5, 'spread')

for _, row in top5.iterrows():
    t0_cmc = row['date']
    t0_bn  = t0_cmc - pd.Timedelta(days=6)
    avail0 = price_piv.index[price_piv.index >= t0_bn - pd.Timedelta(days=3)]
    if len(avail0)==0: continue
    t0k = avail0[0]
    future_cmcs = log['date'][log['date'] > t0_cmc]
    t1_cmc = future_cmcs.iloc[0] if len(future_cmcs)>0 else t0_cmc+pd.Timedelta(weeks=4)
    t1_bn = t1_cmc - pd.Timedelta(days=6)
    avail1 = price_piv.index[price_piv.index >= t1_bn - pd.Timedelta(days=3)]
    if len(avail1)==0: continue
    t1k = avail1[0]
    if t0k==t1k: continue

    print(f'\n  {str(t0_cmc)[:10]}  {row["regime"]}  gross spread={row["spread"]:+.2%}  combined_net={row["combined_net"]:+.2%}')
    long_syms  = [s for s in row['long_basket'].split('|')  if s]
    short_syms = [s for s in row['short_basket'].split('|') if s]
    fwd = {}
    for s in long_syms + short_syms:
        if s in price_piv.columns:
            p0 = price_piv.loc[t0k, s]
            p1 = price_piv.loc[t1k, s]
            if pd.notna(p0) and p0 > 0 and pd.notna(p1):
                fwd[s] = p1/p0 - 1

    nl = len(long_syms); ns = len(short_syms)
    long_rets  = [(s, fwd.get(s,float('nan')), 0.75*fwd.get(s,0)/nl) for s in long_syms]
    short_rets = [(s, fwd.get(s,float('nan')), -0.75*fwd.get(s,0)/ns) for s in short_syms]

    print(f'    LONG  ({nl} tokens, scale 0.75):')
    for s,r,contrib in sorted(long_rets, key=lambda x: x[2], reverse=True):
        print(f'      {s:<10} ret={r:>+8.2%}  contrib={contrib:>+7.3%}')
    print(f'    SHORT ({ns} tokens, scale 0.75):')
    for s,r,contrib in sorted(short_rets, key=lambda x: x[2], reverse=True):
        print(f'      {s:<10} ret={r:>+8.2%}  contrib={contrib:>+7.3%}')

# ============================================================================
# SECTION 5 — Parameter overfitting: does v8's edge survive alternative
#             combinations we did NOT test (out-of-grid)
# ============================================================================
print('\n'+'='*70)
print('SECTION 5 — METHODOLOGY OVERFITTING AUDIT')
print('  Tests v8 with each param reverted to baseline, one at a time')
print('  If any single reversion collapses performance -> that param is overfit')
print('='*70)

tests = [
    ('v8 full',           {}),
    ('revert BULL_BAND',  {'BULL_BAND':'1.10'}),
    ('revert BEAR_BAND',  {'BEAR_BAND':'0.90'}),
    ('revert both bands', {'BULL_BAND':'1.10','BEAR_BAND':'0.90'}),
    ('revert SUPPLY_WIN', {'SUPPLY_WINDOW':'13'}),
    ('revert LQ_lookback',{'LONG_QUALITY_LOOKBACK':'6'}),
    ('revert ALL (=v7)',  {'BULL_BAND':'1.10','BEAR_BAND':'0.90',
                           'SUPPLY_WINDOW':'13','LONG_QUALITY_LOOKBACK':'6'}),
]

print(f'\n  {"Config":<25} {"Ann":>8} {"Sharpe":>8} {"MaxDD":>8} {"Bull":>8} {"Bear":>8}')
print('  '+'-'*68)
for label, extra in tests:
    out = patch_run(extra)
    m = parse_key(out)
    print(f'  {label:<25} {m["ann"]:>+7.2f}%  {m["sharpe"]:>+7.3f}  {m["maxdd"]:>+7.2f}%  {m["bull"]:>+7.2f}%  {m["bear"]:>+7.2f}%')

print('\nDone.')
