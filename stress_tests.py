"""
stress_tests.py
===============
Three stress tests targeting the QR critique of Section 17:

  TEST A — Rolling Walk-Forward (5 × 6-month OOS folds)
    Addresses: "no strict hold-out validation for architecture tests"
    Configs: v8 baseline, WIN_52_104_SLOW (conservative pick), ULTIMATE
    Fixed architecture per config — no re-optimisation per fold.
    Reports per-fold OOS Sharpe and IS/OOS degradation ratio.

  TEST B — ULTIMATE Permutation Test (200 simulations)
    Addresses: "supply-dilution edge or just BTC dominance?"
    Under ULTIMATE (BTC long leg), PERMUTE_SEED shuffles pct_rank so the
    SHORT basket is randomly selected from the investable universe.
    The long leg stays BTC regardless.  Null distribution = "long BTC + short
    random alts."  If ULTIMATE's real Sharpe is not in the tail of this
    distribution, the short-side supply-inflation signal has no alpha over
    random alt selection.

  TEST C — BTC Beta Decomposition (OLS alpha extraction)
    Addresses: "ULTIMATE is just a leveraged BTC dominance trade"
    Runs ULTIMATE with SAVE_BASKET_LOG.  Pairs combined_net returns against
    BTC per-period return.  OLS regression: r ~ alpha + beta * r_BTC.
    If alpha (intercept) is not significantly positive, all ULTIMATE return
    is explained by BTC beta exposure — no supply-dilution alpha.
"""
import sys, os, re, subprocess, tempfile, time
import numpy as np
import pandas as pd
from scipy import stats
sys.stdout.reconfigure(encoding="utf-8")

V7_PATH    = "D:/AI_Projects/circ_supply/perpetual_ls_v7.py"
BN_DIR     = "D:/AI_Projects/circ_supply/binance_perp_data/"
OUTPUT_DIR = "D:/AI_Projects/circ_supply/"
N_PERMS    = 200

with open(V7_PATH, encoding="utf-8") as f:
    BASE = f.read()

# v8 defaults
V8 = {
    "BULL_BAND":             "1.05",
    "BEAR_BAND":             "0.95",
    "SUPPLY_WINDOW":         "26",
    "LONG_QUALITY_LOOKBACK": "12",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def param_patch(s, ov):
    for k, v in ov.items():
        pat = rf"^({re.escape(k)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$"
        s = re.sub(pat, rf"\g<1>{v}\g<3>", s, flags=re.MULTILINE)
    return s


def suppress(s):
    s = s.replace("plt.savefig", "pass  # plt.savefig")
    return s.replace('print(f"[Plot]', 'pass  # print(f"[Plot]')


OLD_LONG = "        r_long_gross,  slip_long,  fund_long_basket  = basket_return(basket_long)"
NEW_LONG_BTC = """\
        _btc_fwd  = float(fwd["BTC"])  if ("BTC" in fwd.index  and pd.notna(fwd["BTC"]))  else np.nan
        _btc_fund = float(fund_row["BTC"]) if ("BTC" in fund_row.index and pd.notna(fund_row["BTC"])) else 0.0
        r_long_gross  = _btc_fwd
        slip_long     = TAKER_FEE
        fund_long_basket = _btc_fund"""

# Pre-built ULTIMATE source (BTC long leg + 104w pure signal + no momentum veto)
ULTIMATE_PARAMS = {
    **V8,
    "SUPPLY_WINDOW":      "52",
    "SUPPLY_WINDOW_SLOW": "104",
    "SIGNAL_SLOW_WEIGHT": "1.0",
    "MOMENTUM_VETO_PCT":  "1.0",   # disable momentum veto (neutral from ablation)
}
ULTIMATE_SRC = suppress(param_patch(BASE.replace(OLD_LONG, NEW_LONG_BTC), ULTIMATE_PARAMS))

# WIN_52_104_SLOW: longer windows + pure 104w signal, altcoin long basket retained
WIN52_PARAMS = {
    **V8,
    "SUPPLY_WINDOW":      "52",
    "SUPPLY_WINDOW_SLOW": "104",
    "SIGNAL_SLOW_WEIGHT": "1.0",
    "MOMENTUM_VETO_PCT":  "1.0",
}
WIN52_SRC = suppress(param_patch(BASE, WIN52_PARAMS))

# v8 baseline
V8_SRC = suppress(param_patch(BASE, V8))


def run_src(source, timeout=360):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(source)
        tmp = f.name
    try:
        r = subprocess.run([sys.executable, tmp], capture_output=True,
                           text=True, encoding="utf-8", timeout=timeout)
        return r.stdout if r.returncode == 0 else "__ERROR__\n" + r.stderr[-500:]
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"
    finally:
        os.unlink(tmp)


def parse_sharpe(stdout):
    if not stdout or stdout.startswith("__"):
        return float("nan")
    m = re.search(
        r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)",
        stdout)
    return float(m.group(1)) if m else float("nan")


def parse_ann(stdout):
    if not stdout or stdout.startswith("__"):
        return float("nan")
    m = re.search(r"L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%", stdout)
    return float(m.group(1)) if m else float("nan")


def parse_maxdd(stdout):
    if not stdout or stdout.startswith("__"):
        return float("nan")
    m = re.search(
        r"L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+"
        r"[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+([\-\+]\d+\.\d+)%",
        stdout)
    return float(m.group(1)) if m else float("nan")


def parse_periods(stdout):
    if not stdout or stdout.startswith("__"):
        return 0
    m = re.search(r"Rebalancing periods\s*:\s*(\d+)", stdout)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# TEST A — Rolling Walk-Forward
# ---------------------------------------------------------------------------

FOLDS = [
    ("IS  2022H1+H2",    "2022-01-01", "2023-06-30"),   # reference IS window
    ("OOS 2023-H2",      "2023-07-01", "2023-12-31"),
    ("OOS 2024-H1",      "2024-01-01", "2024-06-30"),
    ("OOS 2024-H2",      "2024-07-01", "2024-12-31"),
    ("OOS 2025-H1",      "2025-01-01", "2025-06-30"),
    ("OOS 2025-H2+",     "2025-07-01", "2026-01-05"),
]

CONFIGS = [
    ("v8",         V8_SRC),
    ("WIN52_SLOW", WIN52_SRC),
    ("ULTIMATE",   ULTIMATE_SRC),
]


def run_test_A():
    print("=" * 80)
    print("TEST A — Rolling Walk-Forward (5 × 6-month OOS folds)")
    print("Fixed architecture per config; no re-optimisation per fold.")
    print("=" * 80)
    print()

    results = {}  # config -> list of (label, sr, ann, periods)
    for cfg_name, cfg_src in CONFIGS:
        results[cfg_name] = []
        for fold_name, start, end in FOLDS:
            patched = param_patch(cfg_src, {
                "START_DATE": f'pd.Timestamp("{start}")',
                "END_DATE":   f'pd.Timestamp("{end}")',
            })
            out = run_src(patched)
            sr  = parse_sharpe(out)
            ann = parse_ann(out)
            n   = parse_periods(out)
            results[cfg_name].append((fold_name, sr, ann, n))
            tag = f"{sr:>+6.3f}" if not np.isnan(sr) else "  n/a "
            print(f"  [{cfg_name:<10}] {fold_name:<18} SR={tag}  Ann={ann:>+6.2f}%  N={n}", flush=True)

    # Summary table
    print()
    print(f"  {'Window':<20} {'v8 SR':>8} {'WIN52 SR':>10} {'ULTIMATE SR':>12}")
    print("  " + "-" * 55)
    for i, (fold_name, _, _) in enumerate(FOLDS):
        v8_sr  = results["v8"][i][1]
        w52_sr = results["WIN52_SLOW"][i][1]
        ult_sr = results["ULTIMATE"][i][1]
        is_row = "  <-- IS reference" if i == 0 else ""
        print(f"  {fold_name:<20} {v8_sr:>+7.3f}   {w52_sr:>+7.3f}    {ult_sr:>+9.3f}{is_row}")

    print()
    for cfg_name in ("v8", "WIN52_SLOW", "ULTIMATE"):
        oos_srs = [sr for (lbl, sr, ann, n) in results[cfg_name][1:] if not np.isnan(sr)]
        is_sr   = results[cfg_name][0][1]
        mean_oos = np.mean(oos_srs) if oos_srs else float("nan")
        deg      = mean_oos / is_sr if is_sr and not np.isnan(is_sr) and is_sr != 0 else float("nan")
        print(f"  {cfg_name:<12}  IS SR={is_sr:>+.3f}  "
              f"Mean OOS SR={mean_oos:>+.3f}  "
              f"OOS/IS ratio={deg:>.2f}x")

    return results


# ---------------------------------------------------------------------------
# TEST B — ULTIMATE Permutation Test
# ---------------------------------------------------------------------------

def run_test_B():
    print()
    print("=" * 80)
    print(f"TEST B — ULTIMATE Permutation Test ({N_PERMS} simulations)")
    print("Shuffles pct_rank (short basket random, long basket stays BTC).")
    print("Null: 'long BTC + short N random alts from universe'.")
    print("=" * 80)
    print()

    # Full-sample ULTIMATE real Sharpe
    print("  Running ULTIMATE (real signal)...", flush=True)
    real_out = run_src(ULTIMATE_SRC)
    real_sr  = parse_sharpe(real_out)
    real_ann = parse_ann(real_out)
    print(f"  ULTIMATE real: SR={real_sr:>+.3f}  Ann={real_ann:>+.2f}%\n")

    print(f"  Running {N_PERMS} permuted simulations...", flush=True)
    perm_srs = []
    for seed in range(N_PERMS):
        patched = param_patch(ULTIMATE_SRC, {"PERMUTE_SEED": str(seed)})
        out = run_src(patched)
        sr  = parse_sharpe(out)
        perm_srs.append(sr)
        if (seed + 1) % 20 == 0:
            valid = [x for x in perm_srs if not np.isnan(x)]
            print(f"    {seed+1}/{N_PERMS} done | "
                  f"mean={np.mean(valid):>+.3f} | "
                  f"above real={sum(x >= real_sr for x in valid)}/{len(valid)}", flush=True)

    valid = np.array([x for x in perm_srs if not np.isnan(x)])
    p_val = (valid >= real_sr).mean()

    print()
    print("  RESULTS:")
    print(f"  Real ULTIMATE Sharpe        : {real_sr:>+.3f}")
    print(f"  Permuted mean Sharpe        : {np.mean(valid):>+.3f}")
    print(f"  Permuted std Sharpe         : {np.std(valid):>.3f}")
    print(f"  Permuted 95th pct Sharpe    : {np.percentile(valid, 95):>+.3f}")
    print(f"  Permuted 99th pct Sharpe    : {np.percentile(valid, 99):>+.3f}")
    print(f"  Simulations >= real SR      : {(valid >= real_sr).sum()}/{len(valid)}")
    print(f"  Empirical p-value           : {p_val:.4f}")
    print()
    if p_val < 0.01:
        verdict = "SHORT-SIDE SUPPLY SIGNAL HAS ALPHA vs random alt selection (p<0.01)"
    elif p_val < 0.05:
        verdict = "Short-side supply signal marginally significant (p<0.05)"
    elif p_val < 0.10:
        verdict = "BORDERLINE: supply signal weakly distinguishable from noise (p<0.10)"
    else:
        verdict = "FAIL: supply signal on short side NOT distinguishable from random alt selection"
    print(f"  Verdict: {verdict}")

    # Percentile of real SR in null distribution
    pct_in_null = (valid < real_sr).mean() * 100
    print(f"  Real SR is at {pct_in_null:.1f}th percentile of null distribution")

    return real_sr, valid, p_val


# ---------------------------------------------------------------------------
# TEST C — BTC Beta Decomposition
# ---------------------------------------------------------------------------

def run_test_C():
    print()
    print("=" * 80)
    print("TEST C — BTC Beta Decomposition (OLS regression)")
    print("Regresses ULTIMATE per-period net returns on BTC per-period returns.")
    print("alpha = supply-dilution idiosyncratic return; beta = BTC macro exposure.")
    print("=" * 80)
    print()

    # Run ULTIMATE with basket log
    log_path = OUTPUT_DIR + "_stress_ultimate_log.csv"
    patched  = param_patch(ULTIMATE_SRC, {"SAVE_BASKET_LOG": f'"{log_path}"'})
    print("  Running ULTIMATE with SAVE_BASKET_LOG...", flush=True)
    out = run_src(patched, timeout=420)
    if out.startswith("__") or not os.path.exists(log_path):
        print(f"  ERROR: {out[:300]}")
        return

    log = pd.read_csv(log_path, parse_dates=["date"])
    print(f"  Basket log loaded: {len(log)} periods\n")

    # Get BTC per-period returns from Binance weekly prices
    bn = pd.read_parquet(BN_DIR + "weekly_ohlcv.parquet")
    btc_bn = bn[bn["symbol"] == "BTC"].copy()
    btc_bn = btc_bn.sort_values("week_start")
    btc_bn["btc_ret"] = btc_bn["close"].pct_change(4)   # 4-week (monthly) return
    btc_bn = btc_bn[["week_start", "btc_ret"]].dropna()

    # Align: log["date"] is the period start; match to nearest BTC weekly close
    log["date"] = pd.to_datetime(log["date"])
    merged = pd.merge_asof(
        log[["date", "combined_net"]].sort_values("date"),
        btc_bn.rename(columns={"week_start": "date"}),
        on="date", direction="nearest", tolerance=pd.Timedelta("10d")
    ).dropna()

    if len(merged) < 10:
        print(f"  Insufficient aligned periods ({len(merged)}). Check date alignment.")
        return

    r_strat = merged["combined_net"].values
    r_btc   = merged["btc_ret"].values

    # OLS: r_strat = alpha + beta * r_btc + eps
    result = stats.linregress(r_btc, r_strat)
    alpha  = result.intercept
    beta   = result.slope
    r2     = result.rvalue ** 2
    alpha_tstat = result.intercept / result.intercept_stderr if result.intercept_stderr else float("nan")
    beta_tstat  = result.slope    / result.stderr            if result.stderr            else float("nan")
    alpha_pval  = 2 * (1 - stats.t.cdf(abs(alpha_tstat), df=len(merged) - 2))
    beta_pval   = 2 * (1 - stats.t.cdf(abs(beta_tstat),  df=len(merged) - 2))

    # Annualise alpha (per-period → annual)
    alpha_ann = (1 + alpha) ** 12 - 1

    print("  OLS: r_strat = alpha + beta * r_BTC")
    print(f"  N periods     : {len(merged)}")
    print(f"  alpha (period): {alpha:>+.4f}  "
          f"t={alpha_tstat:>+.2f}  p={alpha_pval:.4f}  "
          f"ann≈{alpha_ann:>+.2%}")
    print(f"  beta          : {beta:>+.4f}  "
          f"t={beta_tstat:>+.2f}  p={beta_pval:.4f}")
    print(f"  R²            : {r2:.4f}  (fraction of variance explained by BTC)")
    print()

    # Interpretation
    unexplained = 1 - r2
    if alpha_pval < 0.05:
        alpha_verdict = (f"SIGNIFICANT positive alpha (p={alpha_pval:.3f}) — "
                         f"supply-dilution signal contributes {alpha_ann:+.2%}/yr above BTC beta")
    elif alpha_pval < 0.10:
        alpha_verdict = (f"Borderline alpha (p={alpha_pval:.3f}) — "
                         f"weakly significant at 10% level only")
    else:
        alpha_verdict = (f"NON-SIGNIFICANT alpha (p={alpha_pval:.3f}) — "
                         f"cannot reject H₀ that all return is BTC beta")

    print(f"  Alpha verdict : {alpha_verdict}")
    print(f"  Beta verdict  : net BTC beta = {beta:>+.4f}  "
          f"({'net-short' if beta < 0 else 'net-long'} BTC risk)")
    print(f"  R² = {r2:.3f}: {r2*100:.1f}% of period variance explained by BTC")
    print(f"         {unexplained*100:.1f}% is idiosyncratic (spread-specific or noise)")

    # Per-period residuals: positive residual = strategy beat its BTC-beta expectation
    merged["expected"] = alpha + beta * merged["btc_ret"]
    merged["residual"] = merged["combined_net"] - merged["expected"]
    merged["regime"]   = log["regime"].values[:len(merged)]

    print()
    print("  Regime-conditional residual alpha (strategy vs BTC-beta expectation):")
    for regime in ["Bull", "Bear", "Sideways"]:
        sub = merged[merged["regime"] == regime]["residual"]
        if len(sub) > 0:
            t_r, p_r = stats.ttest_1samp(sub, 0)
            print(f"    {regime:<10} N={len(sub):2d}  mean_residual={sub.mean():>+.4f}  "
                  f"t={t_r:>+.2f}  p={p_r:.3f}")

    # Clean up
    try:
        os.remove(log_path)
    except OSError:
        pass

    return alpha, beta, r2, alpha_pval


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("STRESS TESTS — Supply-Dilution L/S Strategy (ULTIMATE architecture)")
    print("Targets QR critiques: p-hacking, BTC dominance, permutation validity")
    print("=" * 80 + "\n")

    t0 = time.time()

    wf_results = run_test_A()
    real_sr, null_dist, pval = run_test_B()
    run_test_C()

    print()
    print("=" * 80)
    print(f"All tests complete in {(time.time()-t0)/60:.1f} min.")
    print("=" * 80)
