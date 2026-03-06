"""
cro_critique_tests.py
=====================
Tests every actionable point raised by the CRO/Quant Research critique.

Section A — Parameter Sensitivity (overfitting probe)
  A1  PCTL_TIGHT       entry/exit 10/15/90/85
  A2  PCTL_WIDE        entry/exit 15/25/85/75
  A3  SW_SHORTER       SUPPLY_WINDOW=24 (instead of 32)
  A4  SW_LONGER        SUPPLY_WINDOW=40
  A5  BANDS_WIDER      BULL=1.10 / BEAR=0.90
  A6  BANDS_NARROW     BULL=1.02 / BEAR=0.98

Section B — Slippage Stress (liquidity illusion)
  B1  BEAR_SLIP_3X     3x slippage during Bear + Sideways periods
  B2  BEAR_SLIP_5X     5x slippage during Bear + Sideways periods
  B3  ALL_SLIP_3X      3x slippage all periods
  B4  ALL_SLIP_5X      5x slippage all periods

Section C — Token Concentration (idiosyncratic risk)
  C1  EXCL_NEO         exclude NEO from universe
  C2  EXCL_NEO_THETA   exclude NEO + THETA
  C3  EXCL_CORE        exclude top-5 most frequent longs (force rotation)
  C4  STATIC_PORT      fixed basket: top-5 longs vs top-5 shorts (no signal)

Section D — Risk Controls (drawdown erasure)
  D1  CB_OFF           circuit breaker off (SHORT_CB_LOSS = 1.0)
  D2  ALTSEASON_OFF    altseason veto never triggers
  D3  SQUEEZE_OFF      squeeze exclusion off
  D4  ALL_CONTROLS_OFF all three off simultaneously

Section E — IS/OOS Regime Split
  E1  IS_ONLY          2022-01-01 to 2024-01-01 (bear market IS)
  E2  OOS_ONLY         2024-01-01 onwards (bull market OOS)
"""

import subprocess, sys, re, os
import pandas as pd
import numpy as np

V9_PATH = "D:/AI_Projects/circ_supply/perpetual_ls_v9.py"

with open(V9_PATH, encoding="utf-8") as f:
    BASE_SRC = f.read()

# ===========================================================================
#  Patch helpers
# ===========================================================================

def _replace1(src, old, new, label=""):
    if old not in src:
        print(f"  [WARN] patch target not found: {label or repr(old[:60])}")
        return src
    return src.replace(old, new, 1)


# --- A: parameter swaps (simple constant replacements) ---
def patch_params(src, **kwargs):
    replacements = {
        "LONG_ENTRY_PCT       = 0.12":   f"LONG_ENTRY_PCT       = {kwargs.get('lep', 0.12)}",
        "LONG_EXIT_PCT        = 0.18":   f"LONG_EXIT_PCT        = {kwargs.get('lxp', 0.18)}",
        "SHORT_ENTRY_PCT      = 0.88":   f"SHORT_ENTRY_PCT      = {kwargs.get('sep', 0.88)}",
        "SHORT_EXIT_PCT       = 0.82":   f"SHORT_EXIT_PCT       = {kwargs.get('sxp', 0.82)}",
        "SUPPLY_WINDOW        = 32":     f"SUPPLY_WINDOW        = {kwargs.get('sw', 32)}",
        "BULL_BAND            = 1.05":   f"BULL_BAND            = {kwargs.get('bull', 1.05)}",
        "BEAR_BAND            = 0.95":   f"BEAR_BAND            = {kwargs.get('bear', 0.95)}",
        "SHORT_CB_LOSS        = 0.40":   f"SHORT_CB_LOSS        = {kwargs.get('cb', 0.40)}",
        "ALTSEASON_THRESHOLD  = 0.75":   f"ALTSEASON_THRESHOLD  = {kwargs.get('alt', 0.75)}",
        "SHORT_SQUEEZE_PRIOR  = 0.40":   f"SHORT_SQUEEZE_PRIOR  = {kwargs.get('sq', 0.40)}",
        'START_DATE   = pd.Timestamp("2022-01-01")':
            f'START_DATE   = pd.Timestamp("{kwargs.get("start", "2022-01-01")}")',
        "END_DATE     = None":
            f'END_DATE     = pd.Timestamp("{kwargs["end"]}")' if "end" in kwargs else "END_DATE     = None",
    }
    for old, new in replacements.items():
        if old in src:
            src = src.replace(old, new, 1)
    return src


# --- B: regime-dependent slippage multiplier ---
_BASKET_RET_CALL = (
    "        r_long_gross,  slip_long,  fund_long_basket  = basket_return(basket_long)\n"
    "        r_short_gross, slip_short, fund_short_basket = basket_return(basket_short)"
)
def patch_slip_mult(src, bear_mult=1.0, all_mult=1.0):
    inject = (
        f"\n        _bear_mult = {bear_mult} if regime in ('Bear', 'Sideways') else 1.0"
        f"\n        _all_mult  = {all_mult}"
        f"\n        slip_long  *= _bear_mult * _all_mult"
        f"\n        slip_short *= _bear_mult * _all_mult"
    )
    new = _BASKET_RET_CALL + inject
    return _replace1(src, _BASKET_RET_CALL, new, "slip_mult")


# --- C: token exclusion ---
_ALL_SYMS_LINE = "        all_syms = set(univ[\"symbol\"])"
def patch_excl(src, excl_set):
    excl_repr = repr(excl_set)
    new = f'        all_syms = set(univ["symbol"]) - {excl_repr}'
    return _replace1(src, _ALL_SYMS_LINE, new, "token_excl")


# --- C4: static portfolio (fixed baskets, bypasses signal) ---
_BASKET_SHORT_LINE = "        basket_short = entry_short | stay_short"
def patch_static_port(src):
    static = (
        "        basket_short = entry_short | stay_short\n"
        "        # STATIC_PORT override: fixed top-frequency baskets\n"
        '        basket_long  = {"NEO","THETA","QNT","ZRX","IOTX"} & all_syms\n'
        '        basket_short = {"KAVA","1INCH","FIL","OP","GMT"}  & all_syms\n'
    )
    return _replace1(src, _BASKET_SHORT_LINE, static, "static_port")


# ===========================================================================
#  Runner
# ===========================================================================

def _run(src, label):
    safe = label.replace("$", "").replace("/", "").replace(" ", "_")
    tmp  = f"D:/AI_Projects/circ_supply/_tmp_cro_{safe}.py"
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


def _parse(out):
    m = re.search(
        r"L/S Combined \(net\)\s+([-+][\d.]+)%\s+[-+][\d.]+%"
        r"\s+[-+][\d.]+\s+([-+][\d.]+)\s+[-+][\d.]+\s+([-+][\d.]+)%",
        out
    )
    if m:
        return float(m.group(2)), float(m.group(1)), float(m.group(3))
    return None, None, None


def run(label, src):
    print(f"  {label:<26}", end="", flush=True)
    out = _run(src, label)
    sr, ann, mdd = _parse(out)
    if sr is None:
        # show last error line for debugging
        err = [l for l in out.split("\n") if "Error" in l or "error" in l]
        print(f"  FAIL  {err[-1][:60] if err else ''}")
    else:
        d_sr = sr - BASE_SR
        print(f"  SR={sr:+.3f}  ({d_sr:+.3f})  Ann={ann:+.1f}%  MDD={mdd:.1f}%")
    return sr, ann, mdd


# ===========================================================================
#  Baseline
# ===========================================================================

print("=" * 72)
print("  CRO CRITIQUE TESTS")
print("=" * 72)
print(f"  {'Test':<26}  {'SR(HAC)':>8}  {'dSR':>7}  {'Ann%':>7}  {'MDD%':>7}")
print("  " + "-" * 62)

print(f"\n  Computing baseline...", end="", flush=True)
_bsr, _bann, _bmdd = _parse(_run(BASE_SRC, "BASELINE"))
BASE_SR  = _bsr  if _bsr  is not None else 0.0
BASE_ANN = _bann if _bann is not None else 0.0
BASE_MDD = _bmdd if _bmdd is not None else 0.0
print(f"  SR={BASE_SR:+.3f}  Ann={BASE_ANN:+.1f}%  MDD={BASE_MDD:.1f}%")

results = {}

# ===========================================================================
#  A — Parameter Sensitivity
# ===========================================================================

print(f"\n  --- A: Parameter Sensitivity ---")

s = patch_params(BASE_SRC, lep=0.10, lxp=0.15, sep=0.90, sxp=0.85)
results["A1_PCTL_TIGHT"] = run("A1_PCTL_TIGHT", s)

s = patch_params(BASE_SRC, lep=0.15, lxp=0.25, sep=0.85, sxp=0.75)
results["A2_PCTL_WIDE"] = run("A2_PCTL_WIDE", s)

s = patch_params(BASE_SRC, sw=24)
results["A3_SW_SHORTER"] = run("A3_SW_SHORTER", s)

s = patch_params(BASE_SRC, sw=40)
results["A4_SW_LONGER"] = run("A4_SW_LONGER", s)

s = patch_params(BASE_SRC, bull=1.10, bear=0.90)
results["A5_BANDS_WIDER"] = run("A5_BANDS_WIDER", s)

s = patch_params(BASE_SRC, bull=1.02, bear=0.98)
results["A6_BANDS_NARROW"] = run("A6_BANDS_NARROW", s)

# ===========================================================================
#  B — Slippage Stress
# ===========================================================================

print(f"\n  --- B: Slippage Stress ---")

s = patch_slip_mult(BASE_SRC, bear_mult=3.0)
results["B1_BEAR_SLIP_3X"] = run("B1_BEAR_SLIP_3X", s)

s = patch_slip_mult(BASE_SRC, bear_mult=5.0)
results["B2_BEAR_SLIP_5X"] = run("B2_BEAR_SLIP_5X", s)

s = patch_slip_mult(BASE_SRC, all_mult=3.0)
results["B3_ALL_SLIP_3X"] = run("B3_ALL_SLIP_3X", s)

s = patch_slip_mult(BASE_SRC, all_mult=5.0)
results["B4_ALL_SLIP_5X"] = run("B4_ALL_SLIP_5X", s)

# combined: 5x bear + 3x all (upper bound stress)
s = patch_slip_mult(BASE_SRC, bear_mult=5.0, all_mult=3.0)
results["B5_WORST_SLIP"] = run("B5_WORST_SLIP", s)

# ===========================================================================
#  C — Token Concentration
# ===========================================================================

print(f"\n  --- C: Token Concentration ---")

s = patch_excl(BASE_SRC, {"NEO"})
results["C1_EXCL_NEO"] = run("C1_EXCL_NEO", s)

s = patch_excl(BASE_SRC, {"NEO", "THETA"})
results["C2_EXCL_NEO_THETA"] = run("C2_EXCL_NEO_THETA", s)

# Exclude entire persistent long core — forces genuine rotation
s = patch_excl(BASE_SRC, {"NEO", "THETA", "QNT", "ZRX", "IOTX"})
results["C3_EXCL_CORE"] = run("C3_EXCL_CORE", s)

# Static fixed basket (signal-free): top-5 longs vs top-5 shorts
s = patch_static_port(BASE_SRC)
results["C4_STATIC_PORT"] = run("C4_STATIC_PORT", s)

# ZEC excluded (already known, re-run for completeness)
s = _replace1(BASE_SRC, "| COMMODITY_BACKED)", '| COMMODITY_BACKED | {"ZEC"})', "zec_excl")
results["C5_EXCL_ZEC"] = run("C5_EXCL_ZEC", s)

# NEO + ZEC both excluded
s = _replace1(BASE_SRC, "| COMMODITY_BACKED)", '| COMMODITY_BACKED | {"ZEC"})', "zec_excl")
s = patch_excl(s, {"NEO"})
results["C6_EXCL_NEO_ZEC"] = run("C6_EXCL_NEO_ZEC", s)

# ===========================================================================
#  D — Risk Controls
# ===========================================================================

print(f"\n  --- D: Risk Controls (drawdown erasure probe) ---")

s = patch_params(BASE_SRC, cb=1.0)      # CB never triggers
results["D1_CB_OFF"] = run("D1_CB_OFF", s)

s = patch_params(BASE_SRC, alt=1.01)   # altseason veto never triggers
results["D2_ALTSEASON_OFF"] = run("D2_ALTSEASON_OFF", s)

s = patch_params(BASE_SRC, sq=1.0)    # squeeze exclusion never triggers
results["D3_SQUEEZE_OFF"] = run("D3_SQUEEZE_OFF", s)

s = patch_params(BASE_SRC, cb=1.0, alt=1.01, sq=1.0)
results["D4_ALL_CONTROLS_OFF"] = run("D4_ALL_CONTROLS_OFF", s)

# ===========================================================================
#  E — IS/OOS Regime Split
# ===========================================================================

print(f"\n  --- E: IS/OOS Regime Split ---")

s = patch_params(BASE_SRC, end="2024-01-01")
results["E1_IS_ONLY_2022_23"] = run("E1_IS_ONLY_2022_23", s)

s = patch_params(BASE_SRC, start="2024-01-01")
results["E2_OOS_ONLY_2024_26"] = run("E2_OOS_ONLY_2024_26", s)

# ===========================================================================
#  Summary table
# ===========================================================================

print()
print("=" * 72)
print("  FULL RESULTS SUMMARY")
print("=" * 72)
print(f"  {'Test':<26}  {'SR(HAC)':>8}  {'dSR':>7}  {'Ann%':>7}  {'MDD%':>7}")
print("  " + "-" * 62)
print(f"  {'BASELINE':<26}  {BASE_SR:>+8.3f}  {'':>7}  {BASE_ANN:>+7.1f}%  {BASE_MDD:>7.1f}%")
print()

sections = {
    "A: Parameter Sensitivity": ["A1_PCTL_TIGHT","A2_PCTL_WIDE","A3_SW_SHORTER","A4_SW_LONGER","A5_BANDS_WIDER","A6_BANDS_NARROW"],
    "B: Slippage Stress":       ["B1_BEAR_SLIP_3X","B2_BEAR_SLIP_5X","B3_ALL_SLIP_3X","B4_ALL_SLIP_5X","B5_WORST_SLIP"],
    "C: Token Concentration":   ["C1_EXCL_NEO","C2_EXCL_NEO_THETA","C3_EXCL_CORE","C4_STATIC_PORT","C5_EXCL_ZEC","C6_EXCL_NEO_ZEC"],
    "D: Risk Controls":         ["D1_CB_OFF","D2_ALTSEASON_OFF","D3_SQUEEZE_OFF","D4_ALL_CONTROLS_OFF"],
    "E: IS/OOS Split":          ["E1_IS_ONLY_2022_23","E2_OOS_ONLY_2024_26"],
}

for sec, keys in sections.items():
    print(f"  {sec}")
    for k in keys:
        sr, ann, mdd = results[k]
        if sr is not None:
            d = sr - BASE_SR
            print(f"  {k:<26}  {sr:>+8.3f}  {d:>+7.3f}  {ann:>+7.1f}%  {mdd:>7.1f}%")
        else:
            print(f"  {k:<26}  {'FAIL':>8}")
    print()

print("=" * 72)
