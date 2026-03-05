"""
ablation_study.py
=================
Leave-one-out veto ablation: disable each veto/component individually and
compare Sharpe, Ann Return, MaxDD vs the full v8 baseline.

Goal: identify which of the 19 free parameters independently contribute
signal. Any component that adds < 0.05 Sharpe is a candidate for removal
to reduce the parameter count.
"""
import sys, os, re, subprocess, tempfile, multiprocessing
import pandas as pd, numpy as np
sys.stdout.reconfigure(encoding='utf-8')

V7_PATH = 'D:/AI_Projects/circ_supply/perpetual_ls_v7.py'
with open(V7_PATH, encoding='utf-8') as f:
    BASE = f.read()

# v8 baseline params (already defaults in file, but be explicit)
V8 = {
    'BULL_BAND':            '1.05',
    'BEAR_BAND':            '0.95',
    'SUPPLY_WINDOW':        '26',
    'LONG_QUALITY_LOOKBACK':'12',
}

def patch_run(extra, timeout=240):
    s = BASE
    for k, v in {**V8, **extra}.items():
        pat = rf'^({re.escape(k)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$'
        s = re.sub(pat, rf'\g<1>{v}\g<3>', s, flags=re.MULTILINE)
    s = s.replace('plt.savefig', 'pass').replace('print(f"[Plot]', 'pass  #')
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False,
                                     encoding='utf-8') as f:
        f.write(s); tmp = f.name
    r = subprocess.run([sys.executable, tmp], capture_output=True, text=True,
                       encoding='utf-8', timeout=timeout)
    os.unlink(tmp)
    return r.stdout if r.returncode == 0 else '__ERROR__\n' + r.stderr[-600:]

def parse(stdout):
    def find(pat, g=1, cast=float):
        m = re.search(pat, stdout)
        return cast(m.group(g).replace('%','').replace('+','').strip()) if m else float('nan')
    ann    = find(r'L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%')
    sharpe = find(r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)')
    hac    = find(r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+'
                  r'[\+\-]\d+\.\d+\s+([\+\-]\d+\.\d+)')
    dd_m   = re.search(r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+'
                       r'[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+([\-\+]\d+\.\d+)%',
                       stdout)
    maxdd  = float(dd_m.group(1)) if dd_m else float('nan')
    win    = find(r'Win rate \(Long > Short, gross\)\s*:\s*\d+/\d+\s*\((\d+\.\d+)%\)')
    periods= find(r'Rebalancing periods\s*:\s*(\d+)', cast=int)
    cb     = find(r'CB triggered\s*:\s*(\d+)', cast=int)
    return dict(ann=ann, sharpe=sharpe, hac=hac, maxdd=maxdd,
                win=win, periods=int(periods) if not np.isnan(periods) else 0,
                cb=int(cb) if not np.isnan(cb) else 0)

# ---------------------------------------------------------------------------
# Test matrix: (label, patch_dict, description)
# ---------------------------------------------------------------------------
# Disabling logic:
#   Momentum veto off    : MOMENTUM_VETO_PCT=1.0   → 100th pct threshold → nothing above max → no veto
#   Long quality veto off: LONG_QUALITY_VETO_PCT=0.0 → 0th pct → nothing below min → no veto
#   Altseason off        : ALTSEASON_THRESHOLD=1.01 → fraction can never exceed 1.0 → never fires
#   Circuit breaker off  : SHORT_CB_LOSS=9.9        → 990% loss needed → never fires
#   Short squeeze off    : SHORT_SQUEEZE_PRIOR=9.9  → 990% rally → never fires
#   No buffer bands      : LONG_EXIT_PCT=LONG_ENTRY_PCT, SHORT_EXIT_PCT=SHORT_ENTRY_PCT
#                          (entry=exit → no hysteresis)
#   No regime (always on): BULL_BAND=0.0           → index/MA20 >= 0 always → always Bull
#   No slow signal (13w only): SIGNAL_SLOW_WEIGHT=0.0  → uses only 26w fast signal
#   No fast signal (52w only): SIGNAL_SLOW_WEIGHT=1.0  → uses only 52w slow signal

tests = [
    # label, patch_dict
    ('v8 full baseline',         {}),
    ('-- remove momentum veto',  {'MOMENTUM_VETO_PCT':       '1.0'}),
    ('-- remove long qual veto',  {'LONG_QUALITY_VETO_PCT':  '0.0'}),
    ('-- remove altseason veto',  {'ALTSEASON_THRESHOLD':    '1.01'}),
    ('-- remove circuit breaker', {'SHORT_CB_LOSS':          '9.9'}),
    ('-- remove squeeze excl.',   {'SHORT_SQUEEZE_PRIOR':    '9.9'}),
    ('-- remove buffer bands',    {'LONG_EXIT_PCT':          '0.12',
                                   'SHORT_EXIT_PCT':         '0.88'}),
    ('-- regime always active',   {'BULL_BAND':              '0.0'}),
    ('-- 26w signal only',        {'SIGNAL_SLOW_WEIGHT':     '0.0'}),
    ('-- 52w signal only',        {'SIGNAL_SLOW_WEIGHT':     '1.0'}),
    ('-- remove ALL vetoes',      {'MOMENTUM_VETO_PCT':      '1.0',
                                   'LONG_QUALITY_VETO_PCT':  '0.0',
                                   'ALTSEASON_THRESHOLD':    '1.01',
                                   'SHORT_CB_LOSS':          '9.9',
                                   'SHORT_SQUEEZE_PRIOR':    '9.9'}),
    ('-- pure signal (no vetoes, always active)',
                                  {'MOMENTUM_VETO_PCT':      '1.0',
                                   'LONG_QUALITY_VETO_PCT':  '0.0',
                                   'ALTSEASON_THRESHOLD':    '1.01',
                                   'SHORT_CB_LOSS':          '9.9',
                                   'SHORT_SQUEEZE_PRIOR':    '9.9',
                                   'BULL_BAND':              '0.0'}),
]

def _run(args):
    label, patch = args
    out = patch_run(patch)
    if out.startswith('__ERROR__'):
        return label, None, out
    return label, parse(out), None

print('='*80)
print('PARAMETER ABLATION STUDY — v8 Strategy')
print('Leave-one-out: disable each component, measure Sharpe delta vs baseline')
print('='*80)
print()

# Run baseline first synchronously, then parallel for the rest
print('Running baseline...', flush=True)
_, base_m, _ = _run(tests[0])

print('Running ablation tests (sequential — Windows mp guard)...', flush=True)
results_rest = [_run(t) for t in tests[1:]]

all_results = [(tests[0][0], base_m, None)] + results_rest

# Header
print(f'\n  {"Config":<40} {"Ann":>8} {"Sharpe":>8} {"HAC*":>8} '
      f'{"MaxDD":>8} {"Win%":>6} {"Perds":>6} {"dSharpe":>9}')
print('  ' + '-'*97)

base_sharpe = base_m['sharpe'] if base_m else float('nan')

for label, m, err in all_results:
    if err or m is None:
        print(f'  {label:<40} ERROR')
        continue
    dsharpe = m['sharpe'] - base_sharpe if label != 'v8 full baseline' else float('nan')
    dsharpe_str = f'{dsharpe:>+8.3f}' if not np.isnan(dsharpe) else '        '
    flag = ''
    if not np.isnan(dsharpe):
        if dsharpe < -0.05:   flag = ' <<< HURTS'
        elif dsharpe > 0.05:  flag = ' >>> HELPS'
        elif abs(dsharpe) < 0.02: flag = ' ~ neutral'
    print(f'  {label:<40} {m["ann"]:>+7.2f}%  {m["sharpe"]:>+7.3f}  '
          f'{m["hac"]:>+7.3f}  {m["maxdd"]:>+7.2f}%  {m["win"]:>5.1f}%  '
          f'{m["periods"]:>5}  {dsharpe_str}{flag}')

print()
print('  dSharpe: change in Sharpe when component is removed (negative = component helps)')
print('  Threshold: |dSharpe| >= 0.05 to be considered meaningful')

# Minimal parameter set recommendation
print()
print('='*80)
print('PARAMETER REDUCTION RECOMMENDATION')
print('='*80)
print()
print('  Components where |dSharpe| < 0.05 when removed are candidates for elimination.')
print('  Each eliminated component removes 1-2 free parameters from the 19-param model.')
print()
neutral = [(l, m['sharpe'] - base_sharpe) for l, m, e in all_results[1:-2]
           if m and not np.isnan(m['sharpe'])]
hurts   = [(l, d) for l, d in neutral if d < -0.05]
helps   = [(l, d) for l, d in neutral if d >  0.05]
neut    = [(l, d) for l, d in neutral if abs(d) <= 0.05]

if hurts:
    print('  Components that HURT when removed (keep — they add value):')
    for l, d in hurts:
        print(f'    {l:<40} dSharpe={d:+.3f}')
if helps:
    print('  Components that HELP when removed (consider dropping):')
    for l, d in helps:
        print(f'    {l:<40} dSharpe={d:+.3f}')
if neut:
    print('  Components that are NEUTRAL (candidates for removal):')
    for l, d in neut:
        print(f'    {l:<40} dSharpe={d:+.3f}')

current_params = 19
removable = len(neut) + len(helps)
print(f'\n  Current free parameters: ~{current_params}')
print(f'  Potentially removable components: {removable}')
print(f'  Target: reduce to <= 6 free parameters')

print('\nDone.')
