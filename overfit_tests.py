"""
overfit_tests.py
================
Four targeted tests to confirm or refute whether the v7→v8 parameter
changes are genuine improvements or IS-specific overfitting.

  TEST A — Sub-period Stability
    Split the 45 periods into two halves: 2022-2023 (IS-like) and 2024-2026
    (OOS-like). Does v8 beat v7 baseline in BOTH halves, or only one?
    If v8 only wins the OOS half (which it wasn't explicitly tuned on), that's
    evidence of genuine signal. If it only wins IS, that's a red flag.

  TEST B — Parameter Neighbourhood Robustness
    Run a 5×4 grid: SUPPLY_WINDOW {13,20,26,32,40} × BULL/BEAR bands
    {1.03/0.97, 1.05/0.95, 1.07/0.93, 1.10/0.90}.
    If v8 (26w, 1.05/0.95) sits on a flat plateau → robust.
    If it's a sharp isolated peak surrounded by much worse values → overfit.

  TEST C — LONG_QUALITY_LOOKBACK Sensitivity
    Sweep: 3, 6, 9, 12, 18, 24 months (all other params at v8 defaults).
    If 12 is a clear isolated maximum → suspect.
    If performance is flat across a range → exact value doesn't matter (non-issue).

  TEST D — IS-only Parameter Selection → OOS Test
    Train: find best SUPPLY_WINDOW + BANDS on IS data (2022-01 → 2023-12).
    Test: apply IS winner to OOS (2024-01 → 2026-01).
    Compare: IS-winner on OOS vs v7 baseline on OOS vs v8 on OOS.
    The critical question: does picking params on IS produce a strategy that
    still beats v7 baseline on OOS, or does the "improvement" evaporate?
"""
import sys, os, re, subprocess, tempfile, time
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")

V7_PATH = "D:/AI_Projects/circ_supply/perpetual_ls_v7.py"
with open(V7_PATH, encoding="utf-8") as f:
    BASE = f.read()

# v7 baseline and v8 param sets
V7 = {"SUPPLY_WINDOW": "13", "BULL_BAND": "1.10", "BEAR_BAND": "0.90",
      "LONG_QUALITY_LOOKBACK": "6"}
V8 = {"SUPPLY_WINDOW": "26", "BULL_BAND": "1.05", "BEAR_BAND": "0.95",
      "LONG_QUALITY_LOOKBACK": "12"}

IS_END   = "2023-12-31"
OOS_START = "2024-01-01"
IS_START  = "2022-01-01"
OOS_END   = "2026-02-01"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def patch(s, ov):
    for k, v in ov.items():
        pat = rf"^({re.escape(k)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$"
        s = re.sub(pat, rf"\g<1>{v}\g<3>", s, flags=re.MULTILINE)
    return s

def suppress(s):
    return s.replace("plt.savefig", "pass").replace('print(f"[Plot]', 'pass  #')

def run_src(src, timeout=360):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(src); tmp = f.name
    try:
        r = subprocess.run([sys.executable, tmp], capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
        return r.stdout if r.returncode == 0 else "__ERROR__\n" + r.stderr[-300:]
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    finally:
        os.unlink(tmp)

def parse(out):
    nan = float("nan")
    if not out or out.startswith("__"): return nan, nan, nan, 0
    def f(pat):
        m = re.search(pat, out)
        return float(m.group(1).replace("%","").replace("+","").strip()) if m else nan
    ann    = f(r"L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%")
    sharpe = f(r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)")
    maxdd  = f(r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+"
               r"[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+([\-\+]\d+\.\d+)%")
    m = re.search(r"Rebalancing periods\s*:\s*(\d+)", out)
    return ann, sharpe, maxdd, int(m.group(1)) if m else 0

BASE_S = suppress(BASE)

def run(params, start=None, end=None):
    ov = dict(params)
    if start: ov["START_DATE"] = f'pd.Timestamp("{start}")'
    if end:   ov["END_DATE"]   = f'pd.Timestamp("{end}")'
    return parse(run_src(patch(BASE_S, ov)))

# ---------------------------------------------------------------------------
# TEST A — Sub-period Stability
# ---------------------------------------------------------------------------

def run_test_A():
    print("=" * 72)
    print("TEST A — Sub-period Stability")
    print("Does v8 beat v7 in BOTH halves, or only the half it was tuned on?")
    print(f"  First half  (IS-like) : {IS_START} → {IS_END}")
    print(f"  Second half (OOS-like): {OOS_START} → {OOS_END}")
    print("=" * 72)
    print()
    print(f"  {'Config':<22} {'Period':<14} {'SR':>7} {'Ann':>8} {'MaxDD':>8} {'N':>4}")
    print("  " + "-" * 62)

    results = {}
    for label, params in [("v7 baseline", V7), ("v8 current", V8)]:
        for period_label, start, end in [
            ("Full 2022-2026", IS_START, OOS_END),
            ("First  half IS ", IS_START, IS_END),
            ("Second half OOS", OOS_START, OOS_END),
        ]:
            ann, sr, dd, n = run(params, start, end)
            key = (label, period_label)
            results[key] = sr
            print(f"  {label:<22} {period_label:<14} {sr:>+6.3f}  {ann:>+7.2f}%  {dd:>+7.2f}%  {n:>4}",
                  flush=True)
        print()

    # Verdict
    v7_is  = results.get(("v7 baseline", "First  half IS "), float("nan"))
    v8_is  = results.get(("v8 current",  "First  half IS "), float("nan"))
    v7_oos = results.get(("v7 baseline", "Second half OOS"), float("nan"))
    v8_oos = results.get(("v8 current",  "Second half OOS"), float("nan"))

    print(f"  v8 vs v7 in IS  half : {'v8 WINS' if v8_is  > v7_is  else 'v7 WINS or TIES'}  "
          f"(Δ={v8_is - v7_is:+.3f})")
    print(f"  v8 vs v7 in OOS half : {'v8 WINS' if v8_oos > v7_oos else 'v7 WINS or TIES'}  "
          f"(Δ={v8_oos - v7_oos:+.3f})")
    print()
    if v8_is > v7_is and v8_oos > v7_oos:
        print("  Verdict: v8 WINS BOTH halves → improvement is likely GENUINE,")
        print("  not specific to the IS window.")
    elif v8_is > v7_is and v8_oos <= v7_oos:
        print("  Verdict: v8 wins IS but LOSES OOS → OVERFITTING signal.")
        print("  The parameter changes exploit IS-specific patterns.")
    elif v8_is <= v7_is and v8_oos > v7_oos:
        print("  Verdict: v8 wins OOS but not IS — unusual. v8 may capture")
        print("  structural improvements that don't show on the (harder) IS period.")
    else:
        print("  Verdict: v7 wins or ties both — v8 changes are suspect.")

    return results

# ---------------------------------------------------------------------------
# TEST B — Parameter Neighbourhood Robustness
# ---------------------------------------------------------------------------

def run_test_B():
    print()
    print("=" * 72)
    print("TEST B — Parameter Neighbourhood Robustness")
    print("Is v8 on a flat plateau (robust) or a sharp peak (overfit)?")
    print("Grid: SUPPLY_WINDOW × BULL/BEAR bands (symmetric)")
    print("=" * 72)
    print()

    windows = [13, 20, 26, 32, 40]
    bands   = [(1.03, 0.97), (1.05, 0.95), (1.07, 0.93), (1.10, 0.90)]

    # Header
    hdr = f"  {'SW':>4}  " + "  ".join(f"B={b[0]:.2f}/{b[1]:.2f}" for b in bands)
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    grid = {}
    for sw in windows:
        row = f"  {sw:>4}w "
        for bull, bear in bands:
            params = {**V8, "SUPPLY_WINDOW": str(sw),
                      "BULL_BAND": str(bull), "BEAR_BAND": str(bear)}
            _, sr, _, _ = run(params)
            grid[(sw, bull)] = sr
            marker = " *" if (sw == 26 and bull == 1.05) else "  "
            row += f"  {sr:>+5.3f}{marker}"
            print(f"\r{row}", end="", flush=True)
        print(f"\r{row}", flush=True)

    print()
    print("  (* = v8 default)")

    # Find max, check neighbourhood
    valid = {k: v for k, v in grid.items() if not np.isnan(v)}
    best_k = max(valid, key=valid.get)
    best_v = valid[best_k]
    v8_val = grid.get((26, 1.05), float("nan"))
    rank   = sorted(valid.values(), reverse=True).index(v8_val) + 1
    total  = len(valid)

    # Plateau metric: how many grid points within 0.10 SR of v8?
    near_v8 = sum(1 for v in valid.values() if abs(v - v8_val) <= 0.10)
    above_v8 = sum(1 for v in valid.values() if v > v8_val + 0.05)

    print(f"\n  v8 (26w, 1.05/0.95)  SR = {v8_val:>+.3f}")
    print(f"  Grid maximum         SR = {best_v:>+.3f}  at SW={best_k[0]}w, "
          f"BULL={best_k[1]:.2f}")
    print(f"  v8 rank in grid      : {rank} / {total}")
    print(f"  Grid points within ±0.10 SR of v8 : {near_v8} / {total}  "
          f"({'plateau' if near_v8 >= total//2 else 'peak'})")
    print(f"  Grid points > v8 + 0.05            : {above_v8} / {total}")

    if above_v8 >= 3:
        print("\n  Verdict: MULTIPLE configs outperform v8 significantly.")
        print("  v8 is NOT the optimum — but the improvement direction (longer SW)")
        print("  should still be visible across the grid.")
    elif near_v8 >= total // 2:
        print("\n  Verdict: v8 sits on a FLAT PLATEAU — nearby values perform")
        print("  similarly. Exact parameter values are not critical → robust.")
    else:
        print("\n  Verdict: v8 near the PEAK of a relatively narrow region.")
        print("  Some sensitivity to exact parameter choice.")

    return grid

# ---------------------------------------------------------------------------
# TEST C — LONG_QUALITY_LOOKBACK Sensitivity
# ---------------------------------------------------------------------------

def run_test_C():
    print()
    print("=" * 72)
    print("TEST C — LONG_QUALITY_LOOKBACK Sensitivity")
    print("Is 12 months a genuine optimum or a lucky IS peak?")
    print("All other params at v8 defaults.")
    print("=" * 72)
    print()

    lookbacks = [3, 6, 9, 12, 18, 24]
    base_sr = None

    print(f"  {'Lookback':>10}  {'SR':>8} {'Ann':>8} {'MaxDD':>8} {'dSR':>8}")
    print("  " + "-" * 50)

    for lb in lookbacks:
        params = {**V8, "LONG_QUALITY_LOOKBACK": str(lb)}
        ann, sr, dd, n = run(params)
        if base_sr is None:
            base_sr = sr
            dsr_str = "  (base)"
        else:
            dsr_str = f"{sr - base_sr:>+7.3f}"
        marker = " *" if lb == 12 else ""
        print(f"  {lb:>9}m{marker}  {sr:>+7.3f}  {ann:>+7.2f}%  {dd:>+7.2f}%  {dsr_str}",
              flush=True)

    print()
    print("  (* = v8 default of 12 months)")
    print("  Flat curve → lookback value doesn't matter (non-issue).")
    print("  Sharp peak at 12 → suspect overfit.")

# ---------------------------------------------------------------------------
# TEST D — IS-only Parameter Selection → OOS Test
# ---------------------------------------------------------------------------

def run_test_D():
    print()
    print("=" * 72)
    print("TEST D — IS-only Parameter Selection → OOS Test")
    print(f"  IS window : {IS_START} → {IS_END}")
    print(f"  OOS window: {OOS_START} → {OOS_END}")
    print("  Step 1: grid-search SUPPLY_WINDOW × BANDS on IS only")
    print("  Step 2: take the IS winner, test it on OOS")
    print("  Step 3: compare IS-winner(OOS) vs v7(OOS) vs v8(OOS)")
    print("=" * 72)
    print()

    # Reduced grid for IS search (6 combos)
    grid_params = [
        ("SW13 B1.10/0.90", {"SUPPLY_WINDOW":"13","BULL_BAND":"1.10","BEAR_BAND":"0.90","LONG_QUALITY_LOOKBACK":"6"}),
        ("SW13 B1.05/0.95", {"SUPPLY_WINDOW":"13","BULL_BAND":"1.05","BEAR_BAND":"0.95","LONG_QUALITY_LOOKBACK":"6"}),
        ("SW26 B1.10/0.90", {"SUPPLY_WINDOW":"26","BULL_BAND":"1.10","BEAR_BAND":"0.90","LONG_QUALITY_LOOKBACK":"6"}),
        ("SW26 B1.05/0.95", {"SUPPLY_WINDOW":"26","BULL_BAND":"1.05","BEAR_BAND":"0.95","LONG_QUALITY_LOOKBACK":"6"}),
        ("SW40 B1.10/0.90", {"SUPPLY_WINDOW":"40","BULL_BAND":"1.10","BEAR_BAND":"0.90","LONG_QUALITY_LOOKBACK":"6"}),
        ("SW40 B1.05/0.95", {"SUPPLY_WINDOW":"40","BULL_BAND":"1.05","BEAR_BAND":"0.95","LONG_QUALITY_LOOKBACK":"6"}),
    ]

    print("  --- IS Grid Search ---")
    print(f"  {'Config':<20} {'IS SR':>8} {'IS Ann':>8} {'IS N':>5}")
    print("  " + "-" * 46)

    is_results = {}
    for label, params in grid_params:
        ann, sr, dd, n = run(params, IS_START, IS_END)
        is_results[label] = (sr, params)
        print(f"  {label:<20} {sr:>+7.3f}  {ann:>+7.2f}%  {n:>5}", flush=True)

    # Find IS winner
    is_winner_label = max(is_results, key=lambda k: is_results[k][0]
                          if not np.isnan(is_results[k][0]) else -999)
    is_winner_sr, is_winner_params = is_results[is_winner_label]
    print(f"\n  IS winner: {is_winner_label}  (IS SR={is_winner_sr:+.3f})")

    # Test on OOS
    print()
    print("  --- OOS Comparison ---")
    print(f"  {'Config':<26} {'OOS SR':>8} {'OOS Ann':>8} {'OOS N':>6}")
    print("  " + "-" * 52)

    oos_configs = [
        ("v7 baseline",        V7),
        ("v8 current",         V8),
        (f"IS winner ({is_winner_label})", is_winner_params),
    ]
    oos_results = {}
    for label, params in oos_configs:
        ann, sr, dd, n = run(params, OOS_START, OOS_END)
        oos_results[label] = sr
        print(f"  {label:<26} {sr:>+7.3f}  {ann:>+7.2f}%  {n:>6}", flush=True)

    print()
    v7_oos = oos_results.get("v7 baseline", float("nan"))
    v8_oos = oos_results.get("v8 current", float("nan"))
    wi_oos = list(oos_results.values())[2]

    print(f"  v8 vs v7 on OOS     : {v8_oos - v7_oos:>+.3f} SR")
    print(f"  IS-winner vs v7 OOS : {wi_oos - v7_oos:>+.3f} SR")
    print(f"  IS-winner vs v8 OOS : {wi_oos - v8_oos:>+.3f} SR")
    print()

    if wi_oos > v7_oos + 0.05:
        print("  Verdict: IS-selected params BEAT v7 on OOS.")
        print("  The parameter improvement direction generalises out-of-sample.")
        print("  This is evidence AGAINST pure overfitting.")
    elif wi_oos > v7_oos - 0.05:
        print("  Verdict: IS-selected params roughly MATCH v7 on OOS.")
        print("  The improvement is marginal — some overfitting, some real signal.")
    else:
        print("  Verdict: IS-selected params UNDERPERFORM v7 on OOS.")
        print("  Selecting params on IS produced a strategy worse than the")
        print("  untuned v7 baseline. Strong evidence of overfitting.")

    return oos_results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    t0 = time.time()
    print("\n" + "=" * 72)
    print("OVERFITTING CONFIRMATION TESTS — v7 vs v8 parameter changes")
    print("=" * 72 + "\n")

    run_test_A()
    grid = run_test_B()
    run_test_C()
    run_test_D()

    print()
    print("=" * 72)
    print(f"All tests complete in {(time.time()-t0)/60:.1f} min.")
    print("=" * 72)
