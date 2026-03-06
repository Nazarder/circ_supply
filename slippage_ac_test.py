"""
slippage_ac_test.py
===================
Replace the parametric slippage model (k / turnover) with a
data-driven Almgren-Chriss market impact model using actual Binance
volatility and ADTV.

AC formula (one-way market impact per trade):
    impact = eta * sigma_daily * sqrt(pos_per_token / daily_ADTV)
    eta          = 0.5  (half-permanent impact; conservative lower bound)
    sigma_daily  = ann_vol / sqrt(252)   [from bn_tokv_piv, 8w rolling]
    daily_ADTV   = weekly_quote_volume / 7  [from bn_adtv_piv]
    pos_per_token = AUM * scale / basket_size  [AUM = new parameter]

No new data required — uses existing weekly_ohlcv.parquet.

Tests
-----
  Parametric baselines (k=0.0005, k=0.002, k=0.004) with ZEC excluded
  AC at $1M / $5M / $10M / $20M AUM — full universe
  AC at $1M / $5M / $10M / $20M AUM — ZEC excluded
  AC $5M / $10M — ZEC excluded + BTC_LONG

What an order-book API would add
---------------------------------
  Currently: vol and ADTV proxy price impact but not quoted spread.
  With bid-ask data (e.g. Binance /fapi/v1/depth snapshots at each rebal):
    - Replace eta * sigma * sqrt(pos/V)  with  actual_spread/2  for small trades
    - Add the AC impact term only for the component exceeding the spread
    - This separates quoted spread (tick friction) from market impact (depth)
  Ask user for Binance REST API key / Kaiko or Amberdata historical L2 feed
  to upgrade to the full cost model.
"""

import sys, os, re, subprocess, tempfile, time
import numpy as np
sys.stdout.reconfigure(encoding="utf-8")

V9_PATH = "D:/AI_Projects/circ_supply/perpetual_ls_v9.py"

ETA = 0.5        # Almgren-Chriss half-permanent impact coefficient

# Exact strings from v9.py that we need to patch
_SLIPPAGE_K_LINE = "SLIPPAGE_K           = 0.0005"
_OLD_SLIP = (
    "            slip   = sum(w[s] * float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)\n"
    "                         for s in syms)"
)


# ===========================================================================
#  Helpers
# ===========================================================================

def _load():
    with open(V9_PATH, encoding="utf-8") as f:
        return f.read()


def _patch_param(src, key, val):
    return re.sub(
        rf"^({re.escape(key)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$",
        rf"\g<1>{val}\3", src, flags=re.MULTILINE
    )


def _run_src(src, timeout=380):
    src = src.replace("plot_results(results)", "pass  # suppressed")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(src)
        tmp = f.name
    try:
        r = subprocess.run(
            [sys.executable, tmp], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _parse(out):
    nan = float("nan")
    if not out or "__TIMEOUT__" in out:
        return nan, nan, nan, 0, nan
    def f(pat):
        m = re.search(pat, out)
        return float(m.group(1).replace("%", "").replace("+", "").strip()) if m else nan
    ann    = f(r"L/S Combined \(net\)\s+([\+\-]?\d+\.\d+)%")
    sharpe = f(r"L/S Combined \(net\)\s+[\+\-]?\d+\.\d+%\s+[\+\-]?\d+\.\d+%\s+([\+\-]?\d+\.\d+)")
    maxdd  = f(r"L/S Combined \(net\)\s+[\+\-]?\d+\.\d+%\s+[\+\-]?\d+\.\d+%\s+"
               r"[\+\-]?\d+\.\d+\s+[\+\-]?\d+\.\d+\s+[\+\-]?\d+\.\d+\s+([\+\-]?\d+\.\d+)%")
    # Avg slippage from per-side rows (long leg net vs long leg gross)
    m2 = re.search(r"Rebalancing periods\s*:\s*(\d+)", out)
    n = int(m2.group(1)) if m2 else 0
    # Extract mean annual slippage proxy: (long_gross - long_net) - funding - fees
    # Better: parse avg monthly turnover to estimate slippage cost
    # For now just return avg period slip from diagnostic if printed
    slip_ann = nan
    return ann, sharpe, maxdd, n, slip_ann


def _run(params=None, src_fns=None):
    src = _load()
    if params:
        for k, v in params.items():
            src = _patch_param(src, k, v)
    if src_fns:
        for fn in src_fns:
            src = fn(src)
    out = _run_src(src)
    ann, sr, dd, n, slip = _parse(out)
    return ann, sr, dd, n


# ===========================================================================
#  Patch functions
# ===========================================================================

def patch_zec(src):
    return src.replace('| COMMODITY_BACKED)', '| COMMODITY_BACKED | {"ZEC"})', 1)


def patch_btc_long(src):
    OLD = ('        r_long_gross,  slip_long,  fund_long_basket  '
           '= basket_return(basket_long)')
    NEW = (OLD + '\n'
           '        _btc_r = float(fwd.get("BTC", np.nan))\n'
           '        if not np.isnan(_btc_r):\n'
           '            r_long_gross = _btc_r\n'
           '            fund_long_basket = (float(fund_row["BTC"])\n'
           '                if "BTC" in fund_row.index and pd.notna(fund_row["BTC"]) else 0.0)\n')
    if OLD not in src:
        print("  [WARN] BTC_LONG patch not found")
    return src.replace(OLD, NEW, 1)


def make_patch_ac(aum, eta=ETA):
    """Return a patch function that injects AUM and replaces the slip formula."""

    def _patch(src):
        # ── 1. Inject AUM constant right after SLIPPAGE_K ──────────────────
        if _SLIPPAGE_K_LINE not in src:
            print(f"  [WARN] AC patch: SLIPPAGE_K line not found verbatim")
        else:
            src = src.replace(
                _SLIPPAGE_K_LINE,
                _SLIPPAGE_K_LINE + f"\nAUM              = {aum:<12}  # USD portfolio size (AC slippage)"
            )

        # ── 2. Replace slip formula in basket_return ────────────────────────
        # Almgren-Chriss: impact = eta × (ann_vol/√252) × √(pos_per_tok / daily_ADTV)
        # tokv_row[s] = annualised vol (already in closure scope)
        # adtv_row[s] = weekly dollar volume → divide by 7 for daily
        NEW_SLIP = (
            f"            # Almgren-Chriss impact (eta={eta}): σ_daily × √(pos/ADTV)\n"
            f"            _ac_pos = AUM * 0.75 / max(len(syms), 1)\n"
            f"            slip = sum(\n"
            f"                w[s] * min(\n"
            f"                    {eta} * (\n"
            f"                        float(tokv_row.get(s, 1.2)\n"
            f"                              if pd.notna(tokv_row.get(s)) else 1.2)\n"
            f"                        / np.sqrt(252))\n"
            f"                    * np.sqrt(max(_ac_pos / max(\n"
            f"                        float(adtv_row.get(s, 7e6)\n"
            f"                              if pd.notna(adtv_row.get(s)) else 7e6) / 7.0,\n"
            f"                        1.0), 0.0)),\n"
            f"                    MAX_SLIPPAGE)\n"
            f"                for s in syms)"
        )

        if _OLD_SLIP not in src:
            print(f"  [WARN] AC patch: old slip formula not found for AUM={aum:,}")
        src = src.replace(_OLD_SLIP, NEW_SLIP, 1)
        return src

    return _patch


# ===========================================================================
#  Also: compute and print what average slippage the AC model produces
#  by patching SAVE_BASKET_LOG and post-processing
# ===========================================================================

def _estimate_ac_slippage_stats(aum, eta=ETA):
    """
    Load Binance weekly OHLCV and compute the mean AC slippage per period
    across the 2022-2026 universe. Returns (mean_slip_pct, median_slip_pct).
    """
    import pandas as pd
    BN_DIR = "D:/AI_Projects/circ_supply/binance_perp_data/"
    ohlcv = pd.read_parquet(f"{BN_DIR}/weekly_ohlcv.parquet")
    ohlcv["cmc_date"] = ohlcv["week_start"] + pd.Timedelta(days=6)

    # Build ann_vol and weekly ADTV pivots
    close_df = ohlcv.pivot_table(index="cmc_date", columns="symbol",
                                  values="close", aggfunc="last").sort_index()
    ret_df   = close_df.pct_change(1)
    vol_piv  = ret_df.rolling(8, min_periods=4).std() * np.sqrt(52)   # annualised
    adtv_piv = ohlcv.pivot_table(index="cmc_date", columns="symbol",
                                  values="quote_volume", aggfunc="last")

    # For each symbol/date, compute AC impact at a typical position size
    # Use avg_basket = 10 tokens (typical v9 basket)
    pos_per_tok = aum * 0.75 / 10.0

    slips = []
    common_dates = vol_piv.index.intersection(adtv_piv.index)
    for dt in common_dates:
        vrow = vol_piv.loc[dt].dropna()
        arow = adtv_piv.loc[dt].dropna()
        syms = vrow.index.intersection(arow.index)
        for s in syms:
            v = float(vrow[s]) / np.sqrt(252)
            a = max(float(arow[s]) / 7.0, 1.0)
            slips.append(min(eta * v * np.sqrt(pos_per_tok / a), 0.02))

    if not slips:
        return float("nan"), float("nan")
    return float(np.mean(slips)), float(np.median(slips))


# ===========================================================================
#  Main
# ===========================================================================

if __name__ == "__main__":
    t0 = time.time()

    print("\n" + "=" * 82)
    print("  SLIPPAGE: ALMGREN-CHRISS (AC) vs PARAMETRIC — v9 Supply-Dilution L/S")
    print("=" * 82)

    # ── Pre-compute AC slippage statistics for each AUM ─────────────────────
    print("\n  [Pre-computing mean AC slippage across universe 2022-2026...]")
    print(f"  {'AUM':>12}  {'Mean slip/trade':>16}  {'Median slip/trade':>18}  "
          f"{'≈ equiv k?':>12}")
    print("  " + "-" * 62)
    aum_levels = [1_000_000, 5_000_000, 10_000_000, 20_000_000]
    ac_stats = {}
    for aum in aum_levels:
        mean_s, med_s = _estimate_ac_slippage_stats(aum)
        ac_stats[aum] = (mean_s, med_s)
        # Rough parametric equiv: k ≈ mean_slip * typical_turnover
        # turnover (CMC volume_24h / market_cap) ~ 0.05-0.15; use 0.10
        k_equiv = mean_s * 0.10 if not np.isnan(mean_s) else float("nan")
        print(f"  ${aum:>11,}  {mean_s:>+14.3%}  {med_s:>+16.3%}  "
              f"k≈{k_equiv:.4f}", flush=True)

    # ── Backtest runs ────────────────────────────────────────────────────────
    print(f"\n  {'Test':<44}  {'SR':>7}  {'Ann':>8}  {'MaxDD':>8}  {'N':>4}  {'dSR':>8}")
    print("  " + "-" * 84)

    TESTS = [
        # Parametric baselines
        ("Param k=0.0005  (full, baseline)",
            None,                          None),
        ("Param k=0.0005  ZEC excl",
            None,                          [patch_zec]),
        ("Param k=0.002   ZEC excl",
            {"SLIPPAGE_K": "0.002"},       [patch_zec]),
        ("Param k=0.004   ZEC excl",
            {"SLIPPAGE_K": "0.004"},       [patch_zec]),

        # AC full universe (ZEC in)
        ("AC $1M  AUM (full)",
            None,                          [make_patch_ac(1_000_000)]),
        ("AC $5M  AUM (full)",
            None,                          [make_patch_ac(5_000_000)]),
        ("AC $10M AUM (full)",
            None,                          [make_patch_ac(10_000_000)]),
        ("AC $20M AUM (full)",
            None,                          [make_patch_ac(20_000_000)]),

        # AC ZEC excluded
        ("AC $1M  AUM  ZEC excl",
            None,                          [make_patch_ac(1_000_000), patch_zec]),
        ("AC $5M  AUM  ZEC excl",
            None,                          [make_patch_ac(5_000_000), patch_zec]),
        ("AC $10M AUM  ZEC excl",
            None,                          [make_patch_ac(10_000_000), patch_zec]),
        ("AC $20M AUM  ZEC excl",
            None,                          [make_patch_ac(20_000_000), patch_zec]),

        # AC + BTC_LONG + ZEC excl (best architecture under realistic costs)
        ("AC $5M  + ZEC excl + BTC_LONG",
            None,                          [make_patch_ac(5_000_000),  patch_zec, patch_btc_long]),
        ("AC $10M + ZEC excl + BTC_LONG",
            None,                          [make_patch_ac(10_000_000), patch_zec, patch_btc_long]),
        ("AC $20M + ZEC excl + BTC_LONG",
            None,                          [make_patch_ac(20_000_000), patch_zec, patch_btc_long]),
    ]

    baseline_sr = float("nan")
    results = {}

    for label, params, src_fns in TESTS:
        t_s = time.time()
        ann, sr, dd, n = _run(params, src_fns)
        results[label] = (ann, sr, dd, n)

        if "baseline" in label:
            baseline_sr = sr
            dsr_str = " (baseline)"
        elif np.isnan(sr) or np.isnan(baseline_sr):
            dsr_str = "    N/A"
        else:
            dsr_str = f"{sr - baseline_sr:>+8.3f}"

        ann_s = f"{ann:>+7.2f}%" if not np.isnan(ann) else "    N/A"
        sr_s  = f"{sr:>+6.3f}"  if not np.isnan(sr)  else "   N/A"
        dd_s  = f"{dd:>+7.2f}%" if not np.isnan(dd)  else "    N/A"

        print(f"  {label:<44}  {sr_s}  {ann_s}  {dd_s}  {n:>4}  {dsr_str}"
              f"   [{time.time()-t_s:.0f}s]", flush=True)

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 82)
    print("  SUMMARY")
    print("=" * 82)

    def _get(label, idx=1):
        return results.get(label, (float("nan"),) * 4)[idx]

    print("\n  AC model slippage vs parametric equivalents:")
    for aum in aum_levels:
        mean_s = ac_stats[aum][0]
        sr_full = _get(f"AC ${aum//1_000_000}M {'':>1}AUM (full)".replace("  ", " "))
        sr_zec  = _get(f"AC ${aum//1_000_000}M {'':>1}AUM  ZEC excl".replace("  ", " "))
        if np.isnan(sr_full):
            # try with different spacing
            for k in results:
                if f"AC ${aum//1_000_000}M" in k and "full" in k:
                    sr_full = results[k][1]; break
            for k in results:
                if f"AC ${aum//1_000_000}M" in k and "ZEC excl" in k and "BTC_LONG" not in k:
                    sr_zec = results[k][1]; break
        print(f"    AUM=${aum:>12,}  mean_slip={mean_s:>+.3%}  "
              f"SR_full={sr_full:>+.3f}  SR_ZEC_excl={sr_zec:>+.3f}")

    print(f"\n  BTC_LONG under AC slippage:")
    for aum in [5_000_000, 10_000_000, 20_000_000]:
        for k in results:
            if f"${aum//1_000_000}M" in k and "BTC_LONG" in k:
                print(f"    AUM=${aum:>12,}  SR={results[k][1]:>+.3f}  Ann={results[k][0]:>+.2f}%")

    print(f"\n  What an order-book API would add:")
    print(f"    - Quoted bid-ask spread at rebalance times separates tick friction")
    print(f"      from market impact (currently conflated in the AC formula)")
    print(f"    - For liquid perps (LINK, DOT, ADA): spread ≈ 1-5 bps dominates")
    print(f"      over AC impact at $1M-$5M position → AC model overstates cost")
    print(f"    - For illiquid perps at MIN_VOLUME: spread ≈ 10-30 bps but market")
    print(f"      depth thins rapidly → AC model may understate cost at $10M+")
    print(f"    - To provide: Binance /fapi/v1/depth snapshots at weekly rebal dates,")
    print(f"      OR historical L2 data from Kaiko / Amberdata / CoinAPI")

    print(f"\n  Runtime: {(time.time()-t0)/60:.1f} min")
    print("=" * 82)
