"""
final_tests.py
==============
Definitive, single-run test suite for v9 strategy.

Slippage model (principled, data-driven):
  - Half-spread and order-book impact from live Binance snapshot (orderbook_slippage.csv)
  - Impact scaled by sqrt(AUM*0.75/n / 375_000) for position size
  - Impact further scaled by sqrt(adtv_ref[s] / adtv_t[s]) for historical liquidity
    => 2022 bear market automatically gets 2-4x higher impact than the 2026 reference
  - Fallback to parametric if token not in book CSV
  - Liquidity penalty capped at sqrt(25) = 5x to prevent extreme outliers

Baseline: v9 with MIN_SUPPLY_HISTORY=32 (bug fixed) + above slippage model

Tests
-----
  S0  PARAM_ONLY        parametric k=0.0005 (old model, for reference)
  S1  BOOK_STATIC       book snapshot, no ADTV scaling (static liquidity)
  S2  BOOK_ADTV         book + ADTV scaling (new honest baseline)

  A1  PCTL_TIGHT        entry/exit 10/15/90/85
  A2  PCTL_WIDE         entry/exit 15/25/85/75
  A3  SW_SHORTER        SUPPLY_WINDOW=24
  A4  SW_LONGER         SUPPLY_WINDOW=40
  A5  BANDS_WIDER       BULL=1.10 / BEAR=0.90
  A6  BANDS_NARROW      BULL=1.02 / BEAR=0.98

  B1  EXCL_ZEC          exclude ZEC
  B2  EXCL_NEO          exclude NEO
  B3  EXCL_NEO_ZEC      exclude NEO + ZEC
  B4  EXCL_CORE         exclude top-5 persistent longs
  B5  STATIC_PORT       fixed basket top-8 each side (signal-free)

  C1  CB_OFF            circuit breaker off
  C2  ALTSEASON_OFF     altseason veto off
  C3  SQUEEZE_OFF       squeeze exclusion off
  C4  ALL_CONTROLS_OFF  all three off

  D1  IS_ONLY           2022-01-01 to 2024-01-01
  D2  OOS_ONLY          2024-01-01 onwards
"""

import subprocess, sys, re, os
import pandas as pd
import numpy as np

V9_PATH  = "D:/AI_Projects/circ_supply/perpetual_ls_v9.py"
BOOK_CSV = "D:/AI_Projects/circ_supply/orderbook_slippage.csv"

# ---------------------------------------------------------------------------
# Load reference data for pre-run stats (not injected into subprocess)
# ---------------------------------------------------------------------------
book_df = pd.read_csv(BOOK_CSV, index_col="symbol")
adtv_ref_ser  = book_df["adtv_weekly"].dropna()
impact_ser     = book_df["impact_$375K"].dropna()
hs_ser         = book_df["half_spread"].dropna()

print("=" * 72)
print("  FINAL DEFINITIVE TEST SUITE  —  v9 with book+ADTV slippage")
print("=" * 72)
print()
print("  Reference order-book stats (March 2026 snapshot):")
print(f"    Tokens with book data : {len(book_df)}")
print(f"    Median half-spread    : {hs_ser.median()*1e4:.1f} bps")
print(f"    Median impact @$375K  : {impact_ser.median()*1e4:.1f} bps")
print(f"    Median ref ADTV/week  : {adtv_ref_ser.median()/1e6:.1f}M USD")
print()

# ---------------------------------------------------------------------------
# Load v9 source
# ---------------------------------------------------------------------------
with open(V9_PATH, encoding="utf-8") as f:
    BASE_SRC = f.read()

# Verify base file has MIN_SUPPLY_HISTORY=32 (bug fixed)
if "MIN_SUPPLY_HISTORY   = 32" not in BASE_SRC:
    raise RuntimeError("v9.py still has MIN_SUPPLY_HISTORY bug — fix it first")

# ---------------------------------------------------------------------------
# Patch strings (verified against v9.py)
# ---------------------------------------------------------------------------

_AFTER_WARNINGS = 'warnings.filterwarnings("ignore")'
_SLIPPAGE_K_LINE = "SLIPPAGE_K           = 0.0005"
_OLD_SLIP = (
    "            slip   = sum(w[s] * float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)\n"
    "                         for s in syms)"
)
_BASKET_RET_CALLS = (
    "        r_long_gross,  slip_long,  fund_long_basket  = basket_return(basket_long)\n"
    "        r_short_gross, slip_short, fund_short_basket = basket_return(basket_short)"
)
_ALL_SYMS_LINE    = '        all_syms = set(univ["symbol"])'
_BASKET_SHORT_LINE = "        basket_short = entry_short | stay_short"

# ---------------------------------------------------------------------------
# Book data loader (injected at module level after warnings.filterwarnings)
# ---------------------------------------------------------------------------
# Using explicit string concat to avoid any encoding/indentation issues
_BOOK_LOADER = (
    "\n"
    "import csv as _bk_csv\n"
    "_BOOK_SLIP = {}\n"
    "_ADTV_REF  = {}\n"
    "_BK_PATH   = 'D:/AI_Projects/circ_supply/orderbook_slippage.csv'\n"
    "with open(_BK_PATH, newline='') as _bkf:\n"
    "    for _bkr in _bk_csv.DictReader(_bkf):\n"
    "        _sym = _bkr['symbol']\n"
    "        try:\n"
    "            _hs = float(_bkr['half_spread'])  if _bkr['half_spread']  else float('nan')\n"
    "            _im = float(_bkr['impact_$375K']) if _bkr['impact_$375K'] else float('nan')\n"
    "            _ar = float(_bkr['adtv_weekly'])  if _bkr['adtv_weekly']  else float('nan')\n"
    "            _BOOK_SLIP[_sym] = (_hs, _im)\n"
    "            _ADTV_REF[_sym]  = _ar\n"
    "        except (ValueError, KeyError):\n"
    "            pass\n"
)

# Book+ADTV slip formula (replaces parametric inside basket_return)
# adtv_row is already in scope (set at line ~721 of v9.py)
_NEW_SLIP_ADTV = (
    "            def _bk_slip_fn(s):\n"
    "                _pos = AUM * 0.75 / max(len(syms), 1)\n"
    "                if s in _BOOK_SLIP:\n"
    "                    _hs, _im = _BOOK_SLIP[s]\n"
    "                    _hs = 0.0 if (_hs != _hs) else float(_hs)\n"
    "                    _ar = float(_ADTV_REF.get(s, 0) or 0)\n"
    "                    _at = float(adtv_row.get(s, 0) if pd.notna(adtv_row.get(s)) else 0)\n"
    "                    if _ar > 0 and _at > 0:\n"
    "                        _liq = min((_ar / _at) ** 0.5, 5.0)\n"
    "                    else:\n"
    "                        _liq = 1.0\n"
    "                    if _im == _im:\n"
    "                        _impact = _im * (max(_pos / 375000.0, 0.0) ** 0.5) * _liq\n"
    "                    else:\n"
    "                        _impact = _hs * _liq\n"
    "                    return min(max(_hs, _impact), MAX_SLIPPAGE)\n"
    "                return float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)\n"
    "            slip   = sum(w[s] * _bk_slip_fn(s) for s in syms)"
)

# Book STATIC slip (no ADTV scaling)
_NEW_SLIP_STATIC = (
    "            def _bk_slip_fn(s):\n"
    "                _pos = AUM * 0.75 / max(len(syms), 1)\n"
    "                if s in _BOOK_SLIP:\n"
    "                    _hs, _im = _BOOK_SLIP[s]\n"
    "                    _hs = 0.0 if (_hs != _hs) else float(_hs)\n"
    "                    if _im == _im:\n"
    "                        _impact = _im * (max(_pos / 375000.0, 0.0) ** 0.5)\n"
    "                    else:\n"
    "                        _impact = _hs\n"
    "                    return min(max(_hs, _impact), MAX_SLIPPAGE)\n"
    "                return float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)\n"
    "            slip   = sum(w[s] * _bk_slip_fn(s) for s in syms)"
)

# ---------------------------------------------------------------------------
# Patch builders
# ---------------------------------------------------------------------------

def _r1(src, old, new, tag=""):
    if old not in src:
        print(f"  [PATCH-WARN] not found: {tag or old[:50]!r}")
        return src
    return src.replace(old, new, 1)


def apply_book_loader(src):
    return _r1(src,
        _AFTER_WARNINGS,
        _AFTER_WARNINGS + _BOOK_LOADER,
        "book_loader")


def apply_aum(src, aum=5_000_000):
    return _r1(src,
        _SLIPPAGE_K_LINE,
        _SLIPPAGE_K_LINE + f"\nAUM                  = {aum}",
        "AUM")


def apply_slip_adtv(src):
    return _r1(src, _OLD_SLIP, _NEW_SLIP_ADTV, "slip_adtv")


def apply_slip_static(src):
    return _r1(src, _OLD_SLIP, _NEW_SLIP_STATIC, "slip_static")


def apply_params(src, **kw):
    mapping = {
        "LONG_ENTRY_PCT       = 0.12":  f"LONG_ENTRY_PCT       = {kw['lep']}" if "lep" in kw else None,
        "LONG_EXIT_PCT        = 0.18":  f"LONG_EXIT_PCT        = {kw['lxp']}" if "lxp" in kw else None,
        "SHORT_ENTRY_PCT      = 0.88":  f"SHORT_ENTRY_PCT      = {kw['sep']}" if "sep" in kw else None,
        "SHORT_EXIT_PCT       = 0.82":  f"SHORT_EXIT_PCT       = {kw['sxp']}" if "sxp" in kw else None,
        "SUPPLY_WINDOW        = 32":    f"SUPPLY_WINDOW        = {kw['sw']}"  if "sw"  in kw else None,
        "BULL_BAND            = 1.05":  f"BULL_BAND            = {kw['bull']}" if "bull" in kw else None,
        "BEAR_BAND            = 0.95":  f"BEAR_BAND            = {kw['bear']}" if "bear" in kw else None,
        "SHORT_CB_LOSS        = 0.40":  f"SHORT_CB_LOSS        = {kw['cb']}"  if "cb"  in kw else None,
        "ALTSEASON_THRESHOLD  = 0.75":  f"ALTSEASON_THRESHOLD  = {kw['alt']}" if "alt" in kw else None,
        "SHORT_SQUEEZE_PRIOR  = 0.40":  f"SHORT_SQUEEZE_PRIOR  = {kw['sq']}"  if "sq"  in kw else None,
        'START_DATE   = pd.Timestamp("2022-01-01")':
            f'START_DATE   = pd.Timestamp("{kw["start"]}")' if "start" in kw else None,
        "END_DATE     = None":
            f'END_DATE     = pd.Timestamp("{kw["end"]}")' if "end" in kw else None,
    }
    for old, new in mapping.items():
        if new is not None:
            src = _r1(src, old, new, old[:30])
    return src


def apply_excl(src, tokens: set):
    new = f'        all_syms = set(univ["symbol"]) - {tokens!r}'
    return _r1(src, _ALL_SYMS_LINE, new, "token_excl")


def apply_static_port(src):
    new = (
        "        basket_short = entry_short | stay_short\n"
        "        basket_long  = {'NEO','THETA','QNT','ZRX','IOTX','AR','KSM','YFI'} & all_syms\n"
        "        basket_short = {'KAVA','1INCH','FIL','OP','GMT','DYDX','GALA','SNX'} & all_syms\n"
    )
    return _r1(src, _BASKET_SHORT_LINE, new, "static_port")


# ---------------------------------------------------------------------------
# Build the honest baseline source (book + ADTV scaling, $5M AUM)
# ---------------------------------------------------------------------------

def make_baseline(raw_src):
    src = apply_book_loader(raw_src)
    src = apply_aum(src, 5_000_000)
    src = apply_slip_adtv(src)
    return src


BASELINE_SRC = make_baseline(BASE_SRC)

# Verify all patches applied
checks = {
    "book_loader": "_BOOK_SLIP" in BASELINE_SRC,
    "adtv_ref":    "_ADTV_REF"  in BASELINE_SRC,
    "aum_const":   "AUM                  = 5000000" in BASELINE_SRC,
    "slip_fn":     "_bk_slip_fn" in BASELINE_SRC,
    "slip_adtv":   "_liq" in BASELINE_SRC,
    "min_hist_32": "MIN_SUPPLY_HISTORY   = 32" in BASELINE_SRC,
}
all_ok = all(checks.values())
for k, v in checks.items():
    status = "OK" if v else "FAIL"
    print(f"  [{status}] {k}")
if not all_ok:
    raise RuntimeError("Patch verification failed — fix before running tests")
print()

# ---------------------------------------------------------------------------
# Runner + parser
# ---------------------------------------------------------------------------

def _run(src, label):
    safe = re.sub(r"[^A-Za-z0-9_]", "", label)
    tmp  = f"D:/AI_Projects/circ_supply/_tmp_final_{safe}.py"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        res = subprocess.run([sys.executable, tmp],
                             capture_output=True, timeout=300)
        out = (res.stdout + res.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        out = "TIMEOUT"
    finally:
        try: os.remove(tmp)
        except OSError: pass
    return out


def _parse(out):
    # Primary: L/S Combined (net) line
    m = re.search(
        r"L/S Combined \(net\)\s+([-+][\d.]+)%\s+[-+][\d.]+%"
        r"\s+[-+][\d.]+\s+([-+][\d.]+)\s+[-+][\d.]+\s+([-+][\d.]+)%",
        out)
    if m:
        return float(m.group(2)), float(m.group(1)), float(m.group(3))
    return None, None, None


def run_test(label, src, base_sr):
    print(f"  {label:<26}", end="", flush=True)
    out  = _run(src, label)
    sr, ann, mdd = _parse(out)
    if sr is None:
        errs = [l for l in out.split("\n") if "rror" in l or "race" in l]
        print(f"  FAIL  {errs[-1][:55] if errs else ''}")
    else:
        dsr = sr - base_sr if base_sr is not None else float("nan")
        dsr_s = f"({dsr:+.3f})" if base_sr is not None else ""
        print(f"  SR={sr:+.3f} {dsr_s:<9}  Ann={ann:+.1f}%  MDD={mdd:.1f}%")
    return sr, ann, mdd


results = {}

# ===========================================================================
#  S — Slippage model comparison (establish what each model does)
# ===========================================================================
print("  --- S: Slippage Model Comparison ---")
print(f"  {'Label':<26}  {'SR':>7}  {'(dSR)':>9}  {'Ann%':>6}  {'MDD%':>6}")
print("  " + "-" * 64)

# S0: Original parametric (v9 unchanged)
results["S0_PARAM"] = run_test("S0_PARAM", BASE_SRC, None)
PARAM_SR = results["S0_PARAM"][0]

# S1: Book snapshot, no ADTV scaling
s1 = apply_book_loader(BASE_SRC)
s1 = apply_aum(s1, 5_000_000)
s1 = apply_slip_static(s1)
results["S1_BOOK_STATIC"] = run_test("S1_BOOK_STATIC", s1, PARAM_SR)

# S2: Book + ADTV scaling (honest baseline for all remaining tests)
results["S2_BOOK_ADTV"] = run_test("S2_BOOK_ADTV", BASELINE_SRC, PARAM_SR)
BASE_SR = results["S2_BOOK_ADTV"][0]

print(f"\n  >>> All tests below use S2 (book+ADTV) as baseline (SR={BASE_SR:+.3f})")
print()

# ===========================================================================
#  A — Parameter Sensitivity
# ===========================================================================
print("  --- A: Parameter Sensitivity ---")

for label, kw in [
    ("A1_PCTL_TIGHT",  dict(lep=0.10, lxp=0.15, sep=0.90, sxp=0.85)),
    ("A2_PCTL_WIDE",   dict(lep=0.15, lxp=0.25, sep=0.85, sxp=0.75)),
    ("A3_SW_SHORTER",  dict(sw=24)),
    ("A4_SW_LONGER",   dict(sw=40)),
    ("A5_BANDS_WIDER", dict(bull=1.10, bear=0.90)),
    ("A6_BANDS_NARROW",dict(bull=1.02, bear=0.98)),
]:
    src = apply_params(BASELINE_SRC, **kw)
    results[label] = run_test(label, src, BASE_SR)

# ===========================================================================
#  B — Token Concentration
# ===========================================================================
print()
print("  --- B: Token Concentration ---")

zec_excl_patch = '| COMMODITY_BACKED | {"ZEC"})'

for label, fn in [
    ("B1_EXCL_ZEC",
     lambda s: _r1(s, "| COMMODITY_BACKED)", zec_excl_patch, "zec_excl")),
    ("B2_EXCL_NEO",
     lambda s: apply_excl(s, {"NEO"})),
    ("B3_EXCL_NEO_ZEC",
     lambda s: apply_excl(
         _r1(s, "| COMMODITY_BACKED)", zec_excl_patch, "zec_excl"),
         {"NEO"})),
    ("B4_EXCL_CORE",
     lambda s: apply_excl(s, {"NEO", "THETA", "QNT", "ZRX", "IOTX"})),
    ("B5_STATIC_PORT",
     lambda s: apply_static_port(apply_params(s, cb=1.0))),
]:
    src = fn(BASELINE_SRC)
    results[label] = run_test(label, src, BASE_SR)

# ===========================================================================
#  C — Risk Controls
# ===========================================================================
print()
print("  --- C: Risk Controls ---")

for label, kw in [
    ("C1_CB_OFF",           dict(cb=1.0)),
    ("C2_ALTSEASON_OFF",    dict(alt=1.01)),
    ("C3_SQUEEZE_OFF",      dict(sq=1.0)),
    ("C4_ALL_CONTROLS_OFF", dict(cb=1.0, alt=1.01, sq=1.0)),
]:
    src = apply_params(BASELINE_SRC, **kw)
    results[label] = run_test(label, src, BASE_SR)

# ===========================================================================
#  D — IS/OOS Regime Split
# ===========================================================================
print()
print("  --- D: IS/OOS Regime Split ---")

for label, kw in [
    ("D1_IS_2022_2023",  dict(end="2024-01-01")),
    ("D2_OOS_2024_2026", dict(start="2024-01-01")),
]:
    src = apply_params(BASELINE_SRC, **kw)
    results[label] = run_test(label, src, BASE_SR)

# ===========================================================================
#  Final summary
# ===========================================================================
print()
print("=" * 72)
print("  FINAL RESULTS SUMMARY")
print(f"  Baseline (S2 Book+ADTV, $5M AUM, MIN_SUPPLY_HISTORY=32)")
print("=" * 72)
print(f"  {'Test':<26}  {'SR(HAC)':>8}  {'dSR':>8}  {'Ann%':>7}  {'MDD%':>7}")
print("  " + "-" * 64)

sections = {
    "S  Slippage model":        ["S0_PARAM","S1_BOOK_STATIC","S2_BOOK_ADTV"],
    "A  Parameter sensitivity": ["A1_PCTL_TIGHT","A2_PCTL_WIDE","A3_SW_SHORTER","A4_SW_LONGER","A5_BANDS_WIDER","A6_BANDS_NARROW"],
    "B  Token concentration":   ["B1_EXCL_ZEC","B2_EXCL_NEO","B3_EXCL_NEO_ZEC","B4_EXCL_CORE","B5_STATIC_PORT"],
    "C  Risk controls":         ["C1_CB_OFF","C2_ALTSEASON_OFF","C3_SQUEEZE_OFF","C4_ALL_CONTROLS_OFF"],
    "D  IS/OOS split":          ["D1_IS_2022_2023","D2_OOS_2024_2026"],
}

for sec, keys in sections.items():
    print(f"\n  {sec}")
    for k in keys:
        sr, ann, mdd = results[k]
        if sr is not None:
            ref = PARAM_SR if k.startswith("S") else BASE_SR
            dsr = sr - ref
            print(f"  {k:<26}  {sr:>+8.3f}  {dsr:>+8.3f}  {ann:>+7.1f}%  {mdd:>7.1f}%")
        else:
            print(f"  {k:<26}  {'FAIL':>8}")

print()
print("=" * 72)
