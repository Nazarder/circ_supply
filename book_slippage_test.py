"""
book_slippage_test.py
=====================
Integrate live Binance order-book slippage (orderbook_slippage.csv) into the
v9 backtest.  Tests multiple AUM levels, ZEC exclusion, and BTC_LONG combo.

Slippage formula (per token):
  pos_per_token  = AUM * 0.75 / max(n_tokens, 1)          # gross notional per leg
  impact_scaled  = impact_$375K[s] * sqrt(pos / 375_000)  # square-root scaling
  token_slip     = max(half_spread[s], impact_scaled)
  slip (leg)     = weighted_avg(token_slip)

Falls back to parametric model for tokens not in orderbook_slippage.csv.

Tests
-----
  BOOK_$1M     — book slippage, AUM=$1M
  BOOK_$5M     — book slippage, AUM=$5M   (baseline AUM)
  BOOK_$10M    — book slippage, AUM=$10M
  BOOK_$20M    — book slippage, AUM=$20M
  BOOK_$5M_ZEC — book slippage, $5M, ZEC excluded
  BOOK_$10M_ZEC— book slippage, $10M, ZEC excluded
  BOOK_$20M_ZEC— book slippage, $20M, ZEC excluded
  BOOK_BTC_$5M — book slippage, $5M, BTC long
  BOOK_BTC_$10M— book slippage, $10M, BTC long
  BOOK_BTC_ZEC_$10M— book slippage, $10M, ZEC excl + BTC long
  BOOK_BTC_ZEC_$20M— book slippage, $20M, ZEC excl + BTC long
"""

import subprocess, sys, re, textwrap, os
import pandas as pd
import numpy as np

V9_PATH  = "D:/AI_Projects/circ_supply/perpetual_ls_v9.py"
BOOK_CSV = "D:/AI_Projects/circ_supply/orderbook_slippage.csv"

# ── Load order book data for pre-compute stats ────────────────────────────────
book_df = pd.read_csv(BOOK_CSV)
book_df = book_df.set_index("symbol")

# ── Patch strings (exact matches against v9.py) ───────────────────────────────

# 1. Book data loader — injected after warnings.filterwarnings("ignore")
_BOOK_LOADER = textwrap.dedent("""\
    # Book slippage data (injected by book_slippage_test.py)
    import csv as _csv
    _BOOK_CSV_PATH = "D:/AI_Projects/circ_supply/orderbook_slippage.csv"
    _BOOK_SLIP = {}   # symbol -> (half_spread, impact_at_375K)
    with open(_BOOK_CSV_PATH, newline="") as _f:
        for _row in _csv.DictReader(_f):
            try:
                _hs = float(_row["half_spread"])   if _row["half_spread"] else float("nan")
                _im = float(_row["impact_$375K"])  if _row["impact_$375K"] else float("nan")
                _BOOK_SLIP[_row["symbol"]] = (_hs, _im)
            except (ValueError, KeyError):
                pass
""")

_AFTER_WARNINGS = 'warnings.filterwarnings("ignore")'

# 2. AUM constant — injected after SLIPPAGE_K line
_SLIPPAGE_K_LINE = "SLIPPAGE_K           = 0.0005"
_AUM_INJECT      = "\nAUM                  = 5_000_000   # gross notional; overridden per test"

# 3. Slip formula replacement
_OLD_SLIP = (
    "            slip   = sum(w[s] * float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)\n"
    "                         for s in syms)"
)
_NEW_SLIP = (
    "            def _bk_slip(s):\n"
    "                _pos = AUM * 0.75 / max(len(syms), 1)\n"
    "                if s in _BOOK_SLIP:\n"
    "                    _hs, _im = _BOOK_SLIP[s]\n"
    "                    _hs  = 0.0 if (_hs != _hs) else float(_hs)\n"
    "                    if _im == _im:\n"
    "                        _impact = _im * (max(_pos / 375_000.0, 0.0) ** 0.5)\n"
    "                    else:\n"
    "                        _impact = _hs\n"
    "                    return min(max(_hs, _impact), MAX_SLIPPAGE)\n"
    "                return float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)\n"
    "            slip   = sum(w[s] * _bk_slip(s) for s in syms)"
)

# 4. ZEC exclusion patch
_OLD_COMMODITY = "| COMMODITY_BACKED)"
_NEW_COMMODITY_ZEC = '| COMMODITY_BACKED | {"ZEC"})'

# 5. BTC long patch (replaces long basket return with BTC perp return)
_BTC_FUND_LINE = "        r_long_gross,  slip_long,  fund_long_basket  = basket_return(basket_long)"
_BTC_FUND_REPLACE = (
    "        r_long_gross,  slip_long,  fund_long_basket  = basket_return(basket_long)\n"
    "        _btc_r   = float(fwd.get('BTC', float('nan')))\n"
    "        _btc_fnd = float(fund_row['BTC'] if 'BTC' in fund_row.index and\n"
    "                         pd.notna(fund_row['BTC']) else 0.0)\n"
    "        if not (_btc_r != _btc_r):   # not nan\n"
    "            r_long_gross     = _btc_r\n"
    "            slip_long        = float(sl_row.get('BTC', MAX_SLIPPAGE) or MAX_SLIPPAGE)\n"
    "            fund_long_basket = _btc_fnd\n"
)


# ── Runner ────────────────────────────────────────────────────────────────────

def _build_src(src, aum, zec=False, btc_long=False):
    # 1. book loader
    src = src.replace(
        _AFTER_WARNINGS,
        _AFTER_WARNINGS + "\n" + _BOOK_LOADER,
        1
    )
    # 2. AUM constant
    src = src.replace(
        _SLIPPAGE_K_LINE,
        _SLIPPAGE_K_LINE + f"\nAUM                  = {aum}",
        1
    )
    # 3. slip formula
    src = src.replace(_OLD_SLIP, _NEW_SLIP, 1)
    # 4. ZEC
    if zec:
        src = src.replace(_OLD_COMMODITY, _NEW_COMMODITY_ZEC, 1)
    # 5. BTC long
    if btc_long:
        src = src.replace(_BTC_FUND_LINE, _BTC_FUND_REPLACE, 1)
    return src


def _run_src(src, label):
    safe_label = label.replace("$", "").replace("/", "")
    tmp = f"D:/AI_Projects/circ_supply/_tmp_book_{safe_label}.py"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        res = subprocess.run(
            [sys.executable, tmp],
            capture_output=True, timeout=300
        )
        out = (res.stdout + res.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        out = "TIMEOUT"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return out


def _parse(out, label):
    # L/S Combined (net)   +21.38%   +17.65%  +1.211   +1.593   +2.215   -11.10%
    # Columns: Ann.Ret, Vol, Sharpe(basic), Sharpe*(HAC), Sortino, MaxDD
    m = re.search(
        r"L/S Combined \(net\)\s+([-+][\d.]+)%\s+[-+][\d.]+%\s+[-+][\d.]+\s+([-+][\d.]+)\s+[-+][\d.]+\s+([-+][\d.]+)%",
        out
    )
    if m:
        ann = float(m.group(1))
        sr  = float(m.group(2))   # HAC-corrected Sharpe*
        mdd = float(m.group(3))
    else:
        ann = sr = mdd = None
    return {"label": label, "SR": sr, "Ann%": ann, "MDD%": mdd}


# ── Baseline (parametric, no patches) ─────────────────────────────────────────

with open(V9_PATH, encoding="utf-8") as f:
    BASE_SRC = f.read()

TESTS = [
    # label             aum           zec    btc
    ("BOOK_$1M",        1_000_000,    False, False),
    ("BOOK_$5M",        5_000_000,    False, False),
    ("BOOK_$10M",       10_000_000,   False, False),
    ("BOOK_$20M",       20_000_000,   False, False),
    ("BOOK_$5M_ZEC",    5_000_000,    True,  False),
    ("BOOK_$10M_ZEC",   10_000_000,   True,  False),
    ("BOOK_$20M_ZEC",   20_000_000,   True,  False),
    ("BOOK_BTC_$5M",    5_000_000,    False, True),
    ("BOOK_BTC_$10M",   10_000_000,   False, True),
    ("BOOK_BTC_ZEC_$10M", 10_000_000, True,  True),
    ("BOOK_BTC_ZEC_$20M", 20_000_000, True,  True),
]


# ── Pre-compute typical book slippage stats ───────────────────────────────────

print("=" * 72)
print("  ORDER BOOK SLIPPAGE — PRE-COMPUTE STATS")
print("=" * 72)

for aum in [1_000_000, 5_000_000, 10_000_000, 20_000_000]:
    pos = aum * 0.75 / 10   # 10-token basket
    im  = book_df["impact_$375K"].dropna()
    hs  = book_df["half_spread"].dropna()
    im_scaled = im * np.sqrt(max(pos / 375_000, 0))
    slip_tok  = np.maximum(hs, im_scaled).clip(upper=0.02)
    print(f"\n  AUM=${aum/1e6:.0f}M  pos/token=${pos/1e3:.0f}K:")
    print(f"    Median slip/token : {slip_tok.median()*1e4:.1f} bps")
    print(f"    Mean   slip/token : {slip_tok.mean()*1e4:.1f} bps")
    print(f"    P75               : {slip_tok.quantile(0.75)*1e4:.1f} bps")
    print(f"    P90               : {slip_tok.quantile(0.90)*1e4:.1f} bps")

print()
print("  ZEC book slip   : "
      f"{book_df.loc['ZEC', 'total_$375K']*1e4:.1f} bps  "
      f"(half-spread={book_df.loc['ZEC', 'half_spread']*1e4:.1f} bps)")
print()


# ── Verify patch strings match ─────────────────────────────────────────────────

checks = {
    "_AFTER_WARNINGS":  _AFTER_WARNINGS,
    "_SLIPPAGE_K_LINE": _SLIPPAGE_K_LINE,
    "_OLD_SLIP":        _OLD_SLIP,
    "_OLD_COMMODITY":   _OLD_COMMODITY,
    "_BTC_FUND_LINE":   _BTC_FUND_LINE,
}
ok = True
for name, pat in checks.items():
    if pat not in BASE_SRC:
        print(f"[WARN] patch target not found in v9.py: {name!r}")
        ok = False
if ok:
    print("[OK] All patch targets verified in v9.py\n")


# ── Run tests ─────────────────────────────────────────────────────────────────

results = []
for label, aum, zec, btc in TESTS:
    print(f"  Running {label} ...", flush=True, end="")
    src = _build_src(BASE_SRC, aum, zec=zec, btc_long=btc)
    out = _run_src(src, label)
    r   = _parse(out, label)
    results.append(r)
    sr_s = f"{r['SR']:+.3f}" if r["SR"] is not None else " FAIL"
    print(f"  SR={sr_s}")

# ── Print summary table ────────────────────────────────────────────────────────

print()
print("=" * 72)
print("  BOOK SLIPPAGE BACKTEST RESULTS")
print("=" * 72)
print(f"  {'Label':<26} {'SR(HAC)':>9} {'Ann%':>8} {'MDD%':>8}")
print("  " + "-" * 56)
for r in results:
    sr  = f"{r['SR']:+.3f}"  if r["SR"]   is not None else "  N/A"
    ann = f"{r['Ann%']:+.1f}%" if r["Ann%"] is not None else "  N/A"
    mdd = f"{r['MDD%']:.1f}%"  if r["MDD%"] is not None else "  N/A"
    print(f"  {r['label']:<26} {sr:>9} {ann:>8} {mdd:>8}")

print()
print()
print("  Reference (v9 parametric k=0.0005, $5M AUM):")
print("    SR(HAC)=+1.593  Ann=+21.4%  MDD=-11.1%   [no ZEC excl, no BTC long]")
print("  Reference (v9 parametric, ZEC excluded):")
print("    SR(HAC) from critique_tests: see ZEC_EXCL result")
print("=" * 72)
