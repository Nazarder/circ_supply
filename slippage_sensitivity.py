"""
slippage_sensitivity.py
=======================
Re-runs the v7 full-history backtest across a range of SLIPPAGE_K multipliers
to show how robust the strategy is to execution quality assumptions.

Loads data once, then patches v7's SLIPPAGE_K constant before each run.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd

import perpetual_ls_v7_full as v7

# ---------------------------------------------------------------------------
# Load all data once
# ---------------------------------------------------------------------------
print("Loading data...")
df_raw = v7.load_cmc(v7.INPUT_FILE)
(bn_price_piv, bn_adtv_piv, bn_tokv_piv,
 bn_fund_raw, onboard_map) = v7.load_binance(v7.BN_DIR)
regime_df = v7.build_regime(df_raw)
print("Data loaded.\n")

# ---------------------------------------------------------------------------
# Sensitivity grid
# ---------------------------------------------------------------------------
BASE_K   = 0.0005          # baseline SLIPPAGE_K in v7
BASE_MAX = 0.02            # MAX_SLIPPAGE stays fixed (prevents runaway values)

MULTIPLIERS = [0.25, 0.50, 1.00, 1.50, 2.00, 3.00, 5.00]

rows = []

for mult in MULTIPLIERS:
    # Patch module-level constants — engineer_features and run_backtest
    # both read these from v7's global namespace at call time.
    v7.SLIPPAGE_K   = BASE_K * mult

    df = v7.engineer_features(df_raw.copy())
    res = v7.run_backtest(df, regime_df,
                          bn_price_piv, bn_adtv_piv, bn_tokv_piv,
                          bn_fund_raw, onboard_map)

    if not res["dates"]:
        continue

    cn  = res["combined_net"]
    sn  = res["spread_net"]
    st  = v7.portfolio_stats(cn)
    regs = np.array(res["regime"])

    # Regime-conditional net spread geo returns
    def geo(series, mask):
        sub = series.iloc[list(np.where(mask)[0])]
        if len(sub) < 2:
            return np.nan
        cum = (1 + sub.clip(lower=-0.99)).prod()
        return cum ** (12 / len(sub)) - 1

    bull_geo = geo(sn, regs == "Bull")
    bear_geo = geo(sn, regs == "Bear")

    # Average per-period slippage (bps, active periods only)
    active = [sc[0] > 0 or sc[1] > 0 for sc in res["scale"]]
    sli_l = [v for v, m in zip(res.get("slip_actual_long",  []), active) if m]
    sli_s = [v for v, m in zip(res.get("slip_actual_short", []), active) if m]
    avg_slip_bps = (np.mean(sli_l) + np.mean(sli_s)) * 10000 if sli_l else np.nan

    rows.append(dict(
        mult        = mult,
        slip_k_bps  = BASE_K * mult * 10000,
        avg_slip_bps= avg_slip_bps,
        ann_ret     = st["ann_return"],
        vol         = st["vol"],
        sharpe      = st["sharpe"],
        max_dd      = st["max_dd"],
        win_rate    = (sn > 0).mean(),
        mean_spread = sn.mean(),
        bull_geo    = bull_geo,
        bear_geo    = bear_geo,
    ))
    print(f"  SLIPPAGE_K={BASE_K*mult:.5f} ({mult:.2f}x) done  |  "
          f"Ann.Ret {st['ann_return']:+.2%}  Sharpe {st['sharpe']:+.3f}")

# Restore baseline
v7.SLIPPAGE_K = BASE_K

# ---------------------------------------------------------------------------
# Print results table
# ---------------------------------------------------------------------------
print()
print("=" * 100)
print("SLIPPAGE SENSITIVITY ANALYSIS  —  v7 Full History")
print("  Fixed: MAX_SLIPPAGE=2%, all other parameters unchanged")
print("=" * 100)
print(f"\n  {'Mult':>6}  {'K (bps)':>8}  {'Avg slip':>9}  {'Ann.Ret':>9}  "
      f"{'Vol':>8}  {'Sharpe':>8}  {'MaxDD':>9}  {'WinRate':>8}  "
      f"{'MnSpread':>9}  {'Bull geo':>9}  {'Bear geo':>9}")
print("  " + "-" * 96)
for r in rows:
    marker = " <-- baseline" if r["mult"] == 1.0 else ""
    print(f"  {r['mult']:>5.2f}x  {r['slip_k_bps']:>7.1f}bp  "
          f"{r['avg_slip_bps']:>8.0f}bp  "
          f"{r['ann_ret']:>+8.2%}  {r['vol']:>+7.2%}  "
          f"{r['sharpe']:>+8.3f}  {r['max_dd']:>+8.2%}  "
          f"{r['win_rate']:>7.1%}  {r['mean_spread']:>+8.2%}  "
          f"{r['bull_geo']:>+8.2%}  {r['bear_geo']:>+8.2%}{marker}")

print()
# Break-even analysis
base_ret = next(r["ann_ret"] for r in rows if r["mult"] == 1.0)
print("  Break-even analysis (combined net ann. return):")
for r in rows:
    delta = r["ann_ret"] - base_ret
    status = "POSITIVE" if r["ann_ret"] > 0 else "NEGATIVE"
    print(f"    {r['mult']:.2f}x  ->  {r['ann_ret']:+.2%}  ({delta:+.2%} vs baseline)  [{status}]")

print("=" * 100)
