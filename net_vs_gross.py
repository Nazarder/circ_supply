"""
net_vs_gross.py
===============
Computes gross vs net P&L breakdown using the basket log.
Gross = raw price return (no fees, slippage, funding).
Net   = after 0.04% taker fee x2, slippage model, actual funding.
"""
import sys, os, re, subprocess, tempfile
import pandas as pd, numpy as np
sys.stdout.reconfigure(encoding='utf-8')

V7_PATH  = 'D:/AI_Projects/circ_supply/perpetual_ls_v7.py'
LOG_PATH = 'D:/AI_Projects/circ_supply/_diag_basket_log.csv'
BN_DIR   = 'D:/AI_Projects/circ_supply/binance_perp_data/'

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

# ── Ensure basket log exists ─────────────────────────────────────────────────
if not os.path.exists(LOG_PATH):
    print('Generating basket log...')
    patch_run({'SAVE_BASKET_LOG': f'"{LOG_PATH}"'})

log = pd.read_csv(LOG_PATH, parse_dates=['date'])

# ── Compute per-period gross combined ────────────────────────────────────────
# Combined gross = (long_gross - short_gross) / 2
# (since denom = long_scale + short_scale = 1.5, each scale = 0.75)
# r_combined_gross = (0.75*long_gross + 0.75*(-short_gross)) / 1.5
#                  = (long_gross - short_gross) / 2

log['gross_combined'] = (log['long_gross'] - log['short_gross']) / 2.0
log['cost_drag']      = log['gross_combined'] - log['combined_net']
log['funding_net']    = log['fund_long'] + log['fund_short']  # fund_long is negative, fund_short positive

# Sideways periods have 0 combined_net AND 0 gross (cash)
# Exclude them from cost analysis (no trades = no costs)
active = log[log['combined_net'] != 0].copy()

# ── Summary stats ─────────────────────────────────────────────────────────────
def geo_ann(series, ppy=12):
    s = series.dropna()
    if len(s) == 0: return float('nan')
    cum = (1 + s).prod()
    n_years = len(s) / ppy
    return (cum ** (1/n_years) - 1) * 100

n_active = len(active)
n_total  = len(log)
n_cash   = n_total - n_active

gross_ann  = geo_ann(log['gross_combined'])
net_ann    = geo_ann(log['combined_net'])
cost_ann   = gross_ann - net_ann

# Cumulative
gross_cum = (1 + log['gross_combined']).prod() - 1
net_cum   = (1 + log['combined_net']).prod() - 1
cost_cum  = gross_cum - net_cum

# Cost breakdown over active periods
avg_fee_slip = active['cost_drag'].mean() - active['funding_net'].mean()
avg_funding  = active['funding_net'].mean()
avg_total_cost = active['cost_drag'].mean()

print('='*65)
print('NET VS GROSS P&L BREAKDOWN — v8 Strategy')
print('='*65)

print(f'\n  Periods: {n_total} total | {n_active} active | {n_cash} cash (Sideways)')

print(f'\n  {"":30} {"Gross":>10} {"Net":>10} {"Cost drag":>10}')
print('  ' + '-'*55)
print(f'  {"Ann. return (geo)":30} {gross_ann:>+9.2f}%  {net_ann:>+9.2f}%  {cost_ann:>+9.2f}%')
print(f'  {"Cumulative total":30} {gross_cum:>+9.2%}  {net_cum:>+9.2%}  {cost_cum:>+9.2%}')

print(f'\n  Per-period cost breakdown (active periods only):')
print(f'  {"Avg fee + slippage drag":35} {avg_fee_slip:>+9.3%}')
print(f'  {"Avg net funding (short credit - long drag)":35} {avg_funding:>+9.3%}')
print(f'  {"Avg total cost drag":35} {avg_total_cost:>+9.3%}')

print(f'\n  Cost drag as % of gross return: {abs(cost_ann/gross_ann)*100:.1f}%')
print(f'  Funding offset of fee+slip:     {abs(avg_funding/avg_fee_slip)*100:.1f}%')

# ── Per-regime breakdown ──────────────────────────────────────────────────────
print(f'\n  {"Regime":<12} {"N":>4}  {"Gross ann":>10} {"Net ann":>10} {"Cost drag":>10} {"Fund":>8}')
print('  ' + '-'*58)
for regime in ['Bull', 'Bear', 'Sideways']:
    sub = log[log['regime'] == regime].copy()
    if len(sub) == 0: continue
    g = geo_ann(sub['gross_combined'], ppy=12)
    n = geo_ann(sub['combined_net'],   ppy=12)
    f = sub['funding_net'].mean() * 100
    drag = (g - n) if not (np.isnan(g) or np.isnan(n)) else float('nan')
    print(f'  {regime:<12} {len(sub):>4}  {g:>+9.2f}%  {n:>+9.2f}%  {drag:>+9.2f}%  {f:>+7.3f}%')

# ── Monthly cost table ────────────────────────────────────────────────────────
print(f'\n  Per-period detail (active periods only):')
print(f'  {"Date":<12} {"Rgm":<8} {"Gross":>8} {"Net":>8} {"Drag":>8} {"Fund":>8}')
print('  ' + '-'*55)
for _, row in active.iterrows():
    print(f'  {str(row["date"])[:10]:<12} {row["regime"]:<8} '
          f'{row["gross_combined"]:>+7.2%}  {row["combined_net"]:>+7.2%}  '
          f'{row["cost_drag"]:>+7.2%}  {row["funding_net"]:>+7.2%}')

# ── Capital requirement estimate ─────────────────────────────────────────────
print(f'\n' + '='*65)
print('LIVE TRADING — MINIMUM CAPITAL ESTIMATE')
print('='*65)

# Parameters from v8
ADTV_FLOOR    = 5_000_000   # USD/day
SCALE         = 0.75
AVG_LONG_N    = 8.9
AVG_SHORT_N   = 10.9
ADTV_IMPACT_MAX = 0.05      # max 5% of weekly ADTV per trade
WEEKLY_ADTV   = ADTV_FLOOR * 7  # proxy weekly volume

# Max position size per token to avoid excessive market impact
max_pos_per_token = ADTV_IMPACT_MAX * WEEKLY_ADTV   # $1.75M

# At 1/N equal weight, scale 0.75:
# position per token = capital × SCALE × (1/N)
# capital = position / (SCALE / N) = position × N / SCALE

min_cap_long  = max_pos_per_token * AVG_LONG_N  / SCALE
min_cap_short = max_pos_per_token * AVG_SHORT_N / SCALE
min_capital   = max(min_cap_long, min_cap_short)

print(f'\n  ADTV floor              : ${ADTV_FLOOR/1e6:.0f}M/day')
print(f'  Weekly ADTV proxy       : ${WEEKLY_ADTV/1e6:.0f}M')
print(f'  Max impact per trade    : {ADTV_IMPACT_MAX:.0%} of weekly ADTV = ${max_pos_per_token/1e6:.2f}M per token')
print(f'  Avg long basket size    : {AVG_LONG_N} tokens')
print(f'  Avg short basket size   : {AVG_SHORT_N} tokens')
print(f'\n  Minimum capital (long)  : ${min_cap_long/1e6:.1f}M')
print(f'  Minimum capital (short) : ${min_cap_short/1e6:.1f}M')
print(f'  Minimum capital (max)   : ${min_capital/1e6:.1f}M')

# Realistic capital for <1% ADTV impact
realistic_impact = 0.01
real_pos = realistic_impact * WEEKLY_ADTV
real_cap = max(real_pos * AVG_LONG_N / SCALE,
               real_pos * AVG_SHORT_N / SCALE)
print(f'\n  At 1% ADTV impact threshold: ${real_cap/1e6:.1f}M minimum capital')

print(f'\n  Monthly rebalancing trades : ~{int((AVG_LONG_N + AVG_SHORT_N) * 0.45 * 2):.0f} (opens+closes, avg turnover ~45%)')
print(f'  Fee per period (2×0.04%)   : 0.08% of gross notional')
print(f'  Annual fee cost estimate   : ~{0.08 * 12:.2f}% of capital')

print('\nDone.')
