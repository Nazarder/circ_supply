"""
blind_spots.py
==============
Five tests covering the main blind spots and weak points identified in the critique.

  TEST 1 — Signal Dispersion Filter
    Does the supply signal only work when there's real cross-sectional dispersion?
    If high-IQR periods drive all the alpha, low-dispersion periods are noise we trade.

  TEST 2 — 26w vs 52w Rank Correlation
    Are the two signal layers genuinely independent, or are they ~85% correlated?
    High correlation = blending adds no diversification, just false complexity.

  TEST 3 — Slippage Sensitivity Sweep
    How quickly does Sharpe degrade as SLIPPAGE_K increases from 0.0005 toward 0.005?
    If the strategy breaks at 2×-3× current k, real-world execution could kill it.

  TEST 4 — Winsorisation Off
    We clip the top/bottom 2% of supply changes. Does removing that cap help or hurt?
    Extreme inflators may be the most predictive shorts — winsorising removes them.

  TEST 5 — Regime-Conditional Permutation (Bull vs Bear)
    Does the supply signal have edge in Bull periods? In Bear periods?
    Or is the full-sample p=0.05 entirely driven by one regime?
    Method: run 100 permutations per regime using START_DATE/END_DATE to isolate.
"""
import sys, os, re, subprocess, tempfile
import numpy as np
import pandas as pd
from scipy import stats
sys.stdout.reconfigure(encoding="utf-8")

V7_PATH = "D:/AI_Projects/circ_supply/perpetual_ls_v7.py"
CMC_PATH = "D:/AI_Projects/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
BN_DIR   = "D:/AI_Projects/circ_supply/binance_perp_data/"
LOG_PATH = "D:/AI_Projects/circ_supply/_blind_spots_log.csv"
N_PERMS  = 100   # per regime; 100 × 2 regimes = 200 total runs

with open(V7_PATH, encoding="utf-8") as f:
    BASE = f.read()

V8 = {"BULL_BAND": "1.05", "BEAR_BAND": "0.95",
      "SUPPLY_WINDOW": "26", "LONG_QUALITY_LOOKBACK": "12"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def param_patch(s, ov):
    for k, v in ov.items():
        pat = rf"^({re.escape(k)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$"
        s = re.sub(pat, rf"\g<1>{v}\g<3>", s, flags=re.MULTILINE)
    return s

def suppress(s):
    s = s.replace("plt.savefig", "pass")
    return s.replace('print(f"[Plot]', 'pass  #')

def run_src(source, timeout=360):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(source); tmp = f.name
    try:
        r = subprocess.run([sys.executable, tmp], capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
        return r.stdout if r.returncode == 0 else "__ERROR__\n" + r.stderr[-400:]
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    finally:
        os.unlink(tmp)

def parse(stdout):
    nan = float("nan")
    if not stdout or stdout.startswith("__"): return nan, nan, nan, 0
    def find(pat):
        m = re.search(pat, stdout)
        return float(m.group(1).replace('%','').replace('+','').strip()) if m else nan
    ann    = find(r"L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%")
    sharpe = find(r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)")
    maxdd  = find(r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+"
                  r"[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+([\-\+]\d+\.\d+)%")
    m = re.search(r"Rebalancing periods\s*:\s*(\d+)", stdout)
    return ann, sharpe, maxdd, int(m.group(1)) if m else 0

V8_SRC = suppress(param_patch(BASE, V8))

# ---------------------------------------------------------------------------
# TEST 1 — Signal Dispersion Filter
# ---------------------------------------------------------------------------

def run_test_1():
    print("=" * 75)
    print("TEST 1 — Signal Dispersion Filter")
    print("Does the supply signal only work when cross-sectional dispersion is high?")
    print("=" * 75)

    # Get per-period returns + regimes from basket log
    log_src = param_patch(V8_SRC, {"SAVE_BASKET_LOG": f'"{LOG_PATH}"'})
    out = run_src(log_src, timeout=420)
    if out.startswith("__") or not os.path.exists(LOG_PATH):
        print(f"  ERROR running strategy: {out[:200]}")
        return None

    log = pd.read_csv(LOG_PATH, parse_dates=["date"])
    print(f"  Basket log: {len(log)} periods\n")

    # Load CMC data and compute per-period signal IQR (dispersion)
    cmc = pd.read_csv(CMC_PATH, parse_dates=["snapshot_date"])
    cmc = cmc.rename(columns={"snapshot_date": "date"})
    cmc = cmc.sort_values(["symbol", "date"])
    cmc["supply_derived"] = cmc["market_cap"] / cmc["price"]
    cmc["supply_inf_26w"] = cmc.groupby("symbol")["supply_derived"].transform(
        lambda s: s.pct_change(26))
    cmc["supply_inf_52w"] = cmc.groupby("symbol")["supply_derived"].transform(
        lambda s: s.pct_change(52))

    # Nearest-date match: basket log dates may not align exactly to CMC weekly dates
    cmc_dates = pd.to_datetime(sorted(cmc["date"].unique()))

    def nearest_cmc_date(dt):
        deltas = abs(cmc_dates - dt)
        return cmc_dates[deltas.argmin()]

    dispersion = []
    for dt in log["date"]:
        snap = cmc[cmc["date"] == nearest_cmc_date(dt)][["supply_inf_26w", "supply_inf_52w"]].dropna()
        if len(snap) < 10:
            dispersion.append({"date": dt, "iqr_26w": np.nan, "iqr_52w": np.nan,
                                "iqr_composite": np.nan})
            continue
        snap["rank_26w"] = snap["supply_inf_26w"].rank(pct=True)
        snap["rank_52w"] = snap["supply_inf_52w"].rank(pct=True)
        snap["composite"] = 0.5 * snap["rank_26w"] + 0.5 * snap["rank_52w"]
        dispersion.append({
            "date":          dt,
            "iqr_26w":       snap["supply_inf_26w"].quantile(0.75) - snap["supply_inf_26w"].quantile(0.25),
            "iqr_52w":       snap["supply_inf_52w"].quantile(0.75) - snap["supply_inf_52w"].quantile(0.25),
            "iqr_composite": snap["composite"].quantile(0.75) - snap["composite"].quantile(0.25),
            "n_tokens":      len(snap),
        })

    disp_df = pd.DataFrame(dispersion)
    merged  = pd.merge(log[["date", "combined_net", "regime"]], disp_df, on="date")
    merged  = merged.dropna(subset=["combined_net", "iqr_26w"])

    # Split into high vs low dispersion halves
    med_iqr = merged["iqr_26w"].median()
    hi  = merged[merged["iqr_26w"] >= med_iqr]["combined_net"]
    lo  = merged[merged["iqr_26w"] <  med_iqr]["combined_net"]

    def ann_sharpe(x):
        if len(x) < 3: return float("nan"), float("nan")
        ann = (1 + x.mean()) ** 12 - 1
        sr  = x.mean() / x.std() * np.sqrt(12) if x.std() > 0 else float("nan")
        return ann, sr

    hi_ann, hi_sr = ann_sharpe(hi)
    lo_ann, lo_sr = ann_sharpe(lo)

    # Spearman correlation: IQR vs per-period return
    rho, pval = stats.spearmanr(merged["iqr_26w"], merged["combined_net"])

    print(f"  Median 26w IQR across periods: {med_iqr:.4f}")
    print(f"  High-dispersion half (N={len(hi)}): Ann={hi_ann:>+.2%}  SR={hi_sr:>+.3f}")
    print(f"  Low-dispersion  half (N={len(lo)}): Ann={lo_ann:>+.2%}  SR={lo_sr:>+.3f}")
    print(f"  Spearman(IQR, return): rho={rho:>+.3f}  p={pval:.3f}")
    print()

    if rho > 0.2 and pval < 0.10:
        print("  Finding: signal is DISPERSION-CONDITIONAL — higher IQR periods drive alpha.")
        print("  Implication: add a minimum dispersion filter before entering positions.")
    elif abs(rho) < 0.1:
        print("  Finding: signal return is INDEPENDENT of dispersion level — the signal")
        print("  fires uniformly regardless of how much cross-sectional spread exists.")
    else:
        print("  Finding: weak or mixed relationship between dispersion and returns.")

    try: os.remove(LOG_PATH)
    except: pass
    return merged


# ---------------------------------------------------------------------------
# TEST 2 — 26w vs 52w Rank Correlation
# ---------------------------------------------------------------------------

def run_test_2():
    print()
    print("=" * 75)
    print("TEST 2 — 26w vs 52w Rank Correlation")
    print("If the two signal layers are highly correlated, blending is cosmetic.")
    print("=" * 75)

    cmc = pd.read_csv(CMC_PATH, parse_dates=["snapshot_date"])
    cmc = cmc.rename(columns={"snapshot_date": "date"})
    cmc = cmc.sort_values(["symbol", "date"])
    cmc["supply_derived"] = cmc["market_cap"] / cmc["price"]
    cmc["sup26"] = cmc.groupby("symbol")["supply_derived"].transform(
        lambda s: s.pct_change(26))
    cmc["sup52"] = cmc.groupby("symbol")["supply_derived"].transform(
        lambda s: s.pct_change(52))

    # Compute Spearman rank correlation per rebalancing date (monthly = every 4 weeks)
    dates = sorted(cmc["date"].unique())
    # Sample ~monthly (every 4 weeks)
    dates_monthly = [d for i, d in enumerate(dates) if i % 4 == 0]

    corrs = []
    for dt in dates_monthly:
        snap = cmc[cmc["date"] == dt][["sup26", "sup52"]].dropna()
        if len(snap) < 15:
            continue
        rho, _ = stats.spearmanr(snap["sup26"], snap["sup52"])
        corrs.append(rho)

    corrs = np.array(corrs)
    print(f"  Dates sampled (monthly): {len(corrs)}")
    print(f"  Spearman rank-corr(26w, 52w): mean={corrs.mean():.3f}  "
          f"std={corrs.std():.3f}  min={corrs.min():.3f}  max={corrs.max():.3f}")
    print(f"  % of periods with corr > 0.80: {(corrs > 0.80).mean()*100:.1f}%")
    print(f"  % of periods with corr > 0.90: {(corrs > 0.90).mean()*100:.1f}%")
    print()

    if corrs.mean() > 0.85:
        print("  Finding: HIGHLY CORRELATED (>0.85 mean). The two-layer signal is")
        print("  largely redundant. Blending adds minimal diversification — it's")
        print("  complexity theatre. Consider dropping one window entirely.")
    elif corrs.mean() > 0.70:
        print("  Finding: Moderately correlated (0.70-0.85). Some diversification value")
        print("  but less than a two-factor model implies.")
    else:
        print("  Finding: Genuine diversification between windows (corr<0.70).")
        print("  The two-layer signal is justified.")
    return corrs


# ---------------------------------------------------------------------------
# TEST 3 — Slippage Sensitivity Sweep
# ---------------------------------------------------------------------------

def run_test_3():
    print()
    print("=" * 75)
    print("TEST 3 — Slippage Sensitivity Sweep (SLIPPAGE_K)")
    print("Baseline k=0.0005. Real execution may be 2x-10x higher.")
    print("=" * 75)
    print()

    ks = [0.0001, 0.0005, 0.001, 0.002, 0.003, 0.005, 0.010]
    base_sr = None

    print(f"  {'k':>8}  {'Ann':>9} {'Sharpe':>8} {'MaxDD':>8} {'dSR':>8}")
    print("  " + "-" * 50)

    for k in ks:
        src = param_patch(V8_SRC, {"SLIPPAGE_K": str(k)})
        out = run_src(src)
        ann, sr, dd, n = parse(out)
        if base_sr is None:
            base_sr = sr
            dsr_str = "        "
        else:
            dsr_str = f"{sr - base_sr:>+7.3f}"
        print(f"  {k:>8.4f}  {ann:>+8.2f}%  {sr:>+7.3f}  {dd:>+7.2f}%  {dsr_str}",
              flush=True)

    print()
    print("  Interpretation: at what k does Sharpe drop below 0.5? Below 0.0?")
    print("  k=0.001 = 2x current assumption | k=0.005 = 10x current assumption")


# ---------------------------------------------------------------------------
# TEST 4 — Winsorisation Off
# ---------------------------------------------------------------------------

def run_test_4():
    print()
    print("=" * 75)
    print("TEST 4 — Winsorisation Sensitivity")
    print("Baseline: clip supply changes at 2nd/98th pct. Extreme inflators may be")
    print("the most predictive shorts — clipping removes that information.")
    print("=" * 75)
    print()

    configs = [
        ("wins_02_98  (baseline)",  "(0.02, 0.98)"),   # current
        ("wins_01_99  (looser)",    "(0.01, 0.99)"),
        ("wins_05_95  (tighter)",   "(0.05, 0.95)"),
        ("wins_00_100 (none)",      "(0.00, 1.00)"),   # no clipping
        ("wins_10_90  (aggressive)","(0.10, 0.90)"),
    ]

    base_sr = None
    print(f"  {'Config':<28} {'Ann':>8} {'Sharpe':>8} {'MaxDD':>8} {'dSR':>8}")
    print("  " + "-" * 62)

    for label, wins_val in configs:
        src = param_patch(V8_SRC, {"SUPPLY_INF_WINS": wins_val})
        out = run_src(src)
        ann, sr, dd, n = parse(out)
        if base_sr is None:
            base_sr = sr
            dsr_str = "        "
        else:
            dsr = sr - base_sr
            dsr_str = f"{dsr:>+7.3f}"
        print(f"  {label:<28} {ann:>+7.2f}%  {sr:>+7.3f}  {dd:>+7.2f}%  {dsr_str}",
              flush=True)

    print()
    print("  If wins_00_100 (no clipping) improves: extreme inflators are predictive.")
    print("  If it hurts: outliers are noise / ZEC effect dominating extreme low-end.")


# ---------------------------------------------------------------------------
# TEST 5 — Regime-Conditional Permutation (Bull vs Bear)
# ---------------------------------------------------------------------------

def run_test_5(log_df=None):
    print()
    print("=" * 75)
    print("TEST 5 — Regime-Conditional Permutation (Bull vs Bear)")
    print(f"100 permutations per regime. Tests which regime actually drives the signal.")
    print("=" * 75)

    # Get regime dates from basket log (reuse from test 1 if available)
    if log_df is None:
        log_src = param_patch(V8_SRC, {"SAVE_BASKET_LOG": f'"{LOG_PATH}"'})
        out = run_src(log_src, timeout=420)
        if out.startswith("__") or not os.path.exists(LOG_PATH):
            print(f"  ERROR: {out[:200]}"); return
        log_df = pd.read_csv(LOG_PATH, parse_dates=["date"])

    for regime in ["Bull", "Bear"]:
        reg_dates = log_df[log_df["regime"] == regime]["date"].sort_values()
        if len(reg_dates) < 3:
            print(f"  {regime}: too few periods ({len(reg_dates)}), skipping.")
            continue

        start = reg_dates.min().strftime("%Y-%m-%d")
        end   = reg_dates.max().strftime("%Y-%m-%d")
        n_periods = len(reg_dates)

        print(f"\n  {regime} regime: {n_periods} periods | {start} → {end}")

        # Real signal, this regime's date range
        real_src = param_patch(V8_SRC, {
            "START_DATE": f'pd.Timestamp("{start}")',
            "END_DATE":   f'pd.Timestamp("{end}")',
        })
        real_out = run_src(real_src)
        _, real_sr, _, real_n = parse(real_out)
        print(f"  Real signal: SR={real_sr:>+.3f}  (N={real_n} active periods)", flush=True)

        if np.isnan(real_sr):
            print(f"  Could not parse real SR for {regime}, skipping permutation.")
            continue

        # Permutation
        perm_srs = []
        for seed in range(N_PERMS):
            src = param_patch(real_src, {"PERMUTE_SEED": str(seed)})
            out = run_src(src)
            _, sr, _, _ = parse(out)
            perm_srs.append(sr)
            if (seed + 1) % 25 == 0:
                valid_so_far = [x for x in perm_srs if not np.isnan(x)]
                print(f"    {seed+1}/{N_PERMS} | "
                      f"null mean={np.mean(valid_so_far):>+.3f} | "
                      f"above real={sum(x >= real_sr for x in valid_so_far)}/{len(valid_so_far)}",
                      flush=True)

        valid = np.array([x for x in perm_srs if not np.isnan(x)])
        if len(valid) == 0:
            print(f"  No valid permutation results for {regime}.")
            continue

        p_val  = (valid >= real_sr).mean()
        pctile = (valid < real_sr).mean() * 100

        print(f"\n  {regime} RESULTS:")
        print(f"    Real SR         : {real_sr:>+.3f}")
        print(f"    Null mean SR    : {np.mean(valid):>+.3f}")
        print(f"    Null std        : {np.std(valid):>.3f}")
        print(f"    Null 95th pct   : {np.percentile(valid, 95):>+.3f}")
        print(f"    Sims >= real    : {(valid >= real_sr).sum()}/{len(valid)}")
        print(f"    p-value         : {p_val:.4f}")
        print(f"    Real SR pctile  : {pctile:.1f}th")

        if p_val < 0.01:
            verdict = "STRONG SIGNAL in this regime (p<0.01)"
        elif p_val < 0.05:
            verdict = "Signal present (p<0.05)"
        elif p_val < 0.10:
            verdict = "Borderline signal (p<0.10)"
        else:
            verdict = "NO SIGNAL — random selection beats or matches real signal"
        print(f"    Verdict: {verdict}")

    try: os.remove(LOG_PATH)
    except: pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time
    print("\n" + "=" * 75)
    print("BLIND SPOTS & WEAK POINT TESTS — v8 Supply-Dilution L/S Strategy")
    print("=" * 75 + "\n")
    t0 = time.time()

    log_df = run_test_1()
    run_test_2()
    run_test_3()
    run_test_4()
    run_test_5(log_df)

    print()
    print("=" * 75)
    print(f"All tests complete in {(time.time()-t0)/60:.1f} min.")
    print("=" * 75)
