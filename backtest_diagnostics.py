"""
backtest_diagnostics.py
=======================
Quantifies the structural blind spots identified in the teardown:

  DIAG 1 — Funding Strip
    Run v8 with ZERO_FUNDING=True. Isolates pure signal alpha from funding harvest.

  DIAG 2 — Market Beta Decomposition
    Compute rolling 52w beta vs BTC for every token.
    At each period: avg beta of long basket, short basket, net portfolio beta.
    Shows how much implicit market directionality the strategy carries.

  DIAG 3 — Per-Token P&L Attribution
    For each token: total periods held, gross contribution to combined P&L.
    Quantifies concentration (KAVA, NEO) and identifies single-token drivers.

  DIAG 4 — Spread Distribution & Fat Tail Analysis
    Per-period spread, sorted by magnitude.
    Shows what fraction of total Sharpe comes from outlier periods.
    Computes CVaR and stress-tests the 5 worst periods.

  DIAG 5 — Bear Period Statistical Significance
    Bootstrap the Bear geo spread confidence interval.
    Shows whether Bear alpha is statistically distinguishable from zero.
"""

import subprocess, sys, re, os, tempfile
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

sys.stdout.reconfigure(encoding="utf-8")

V7_PATH    = "D:/AI_Projects/circ_supply/perpetual_ls_v7.py"
BN_DIR     = "D:/AI_Projects/circ_supply/binance_perp_data/"
OUTPUT_DIR = "D:/AI_Projects/circ_supply/"
LOG_PATH   = OUTPUT_DIR + "_diag_basket_log.csv"

V8_OVERRIDES = {
    "BULL_BAND":             "1.05",
    "BEAR_BAND":             "0.95",
    "LONG_QUALITY_LOOKBACK": "12",
    "SUPPLY_WINDOW":         "26",
}

# ─────────────────────────────────────────────────────────────────────────────
with open(V7_PATH, encoding="utf-8") as f:
    _BASE = f.read()

def patch_run(extra: dict, timeout=180) -> str:
    s = _BASE
    all_ov = {**V8_OVERRIDES, **extra}
    for key, val in all_ov.items():
        pat = rf'^({re.escape(key)}\s*=\s*)([^#\n]+?)([ \t]*#.*)?$'
        s = re.sub(pat, rf'\g<1>{val}\g<3>', s, flags=re.MULTILINE)
    s = s.replace("plt.savefig",    "pass")
    s = s.replace('print(f"[Plot]', 'pass  #')
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py",
                                     delete=False, encoding="utf-8") as f:
        f.write(s); tmp = f.name
    r = subprocess.run([sys.executable, tmp], capture_output=True,
                       text=True, encoding="utf-8", timeout=timeout)
    os.unlink(tmp)
    return r.stdout if r.returncode == 0 else "__ERROR__\n" + r.stderr[-400:]

def parse_metrics(stdout):
    def find(pat, g=1, cast=float):
        m = re.search(pat, stdout)
        return cast(m.group(g).replace("%","").replace("+","").strip()) if m else float("nan")
    ann    = find(r'L/S Combined \(net\)\s+([\+\-]\d+\.\d+)%')
    sharpe = find(r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+([\+\-]\d+\.\d+)')
    dd_m   = re.search(r'L/S Combined \(net\)\s+[\+\-]\d+\.\d+%\s+[\+\-]\d+\.\d+%\s+'
                       r'[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+[\+\-]\d+\.\d+\s+([\-\+]\d+\.\d+)%', stdout)
    maxdd  = float(dd_m.group(1)) if dd_m else float("nan")
    fund   = find(r'Net funding impact\s*:\s*[\-\+\d\.]+\s*\(([\-\+\d\.]+)%\)')
    return dict(ann=ann, sharpe=sharpe, maxdd=maxdd, fund=fund)

# ─────────────────────────────────────────────────────────────────────────────
#  GENERATE BASKET LOG (run v8 once with SAVE_BASKET_LOG)
# ─────────────────────────────────────────────────────────────────────────────

def generate_basket_log():
    print("Generating basket log from v8 run ...", flush=True)
    out = patch_run({"SAVE_BASKET_LOG": f'"{LOG_PATH}"'})
    if out.startswith("__ERROR__"):
        print("ERROR generating basket log:", out[:200])
        return None
    if not os.path.exists(LOG_PATH):
        print("ERROR: basket log file not created")
        return None
    df = pd.read_csv(LOG_PATH, parse_dates=["date"])
    print(f"  Loaded {len(df)} periods from basket log")
    return df

# ─────────────────────────────────────────────────────────────────────────────
#  DIAG 1 — FUNDING STRIP
# ─────────────────────────────────────────────────────────────────────────────

def diag_funding_strip():
    print("\n" + "="*70)
    print("DIAG 1 — FUNDING STRIP: Signal Alpha vs Funding Harvest")
    print("="*70)

    print("  Running v8 (real funding) ...", flush=True)
    out_real = patch_run({})
    m_real   = parse_metrics(out_real)

    print("  Running v8 (zero funding) ...", flush=True)
    out_zero = patch_run({"ZERO_FUNDING": "True"})
    m_zero   = parse_metrics(out_zero)

    # Extract cumulative funding from real run
    fund_m  = re.search(r'Net funding impact\s*:\s*([\-\+]\d+\.\d+) \(([\-\+]\d+\.\d+)%\)', out_real)
    fund_cum = float(fund_m.group(2)) if fund_m else float("nan")

    print(f"\n  {'Metric':<25} {'Real v8':>10} {'Zero Funding':>14} {'Delta':>10}")
    print("  " + "-"*60)
    for label, k in [("Ann. Return", "ann"), ("Sharpe", "sharpe"), ("MaxDD", "maxdd")]:
        rv = m_real[k]; zv = m_zero[k]
        pct = "%" if label != "Sharpe" else ""
        fmt = "+.2f"
        print(f"  {label:<25} {rv:>+10.2f}{pct} {zv:>+13.2f}{pct} {rv-zv:>+9.2f}{pct}")

    print(f"\n  Cumulative net funding P&L  : {fund_cum:+.2f}%")
    print(f"  Signal alpha (zero-funding) : {m_zero['ann']:+.2f}% ann.")
    print(f"  Funding contribution        : {m_real['ann'] - m_zero['ann']:+.2f}% ann.")
    pct_from_funding = (m_real["ann"] - m_zero["ann"]) / m_real["ann"] * 100 if m_real["ann"] != 0 else float("nan")
    print(f"  Funding share of total ret  : {pct_from_funding:.1f}%")

    return m_real, m_zero

# ─────────────────────────────────────────────────────────────────────────────
#  DIAG 2 — MARKET BETA DECOMPOSITION
# ─────────────────────────────────────────────────────────────────────────────

def diag_beta(log_df: pd.DataFrame):
    print("\n" + "="*70)
    print("DIAG 2 — MARKET BETA DECOMPOSITION")
    print("  Rolling 52w beta vs BTC for each token; avg per basket per period")
    print("="*70)

    # Load Binance weekly prices
    ohlcv = pd.read_parquet(f"{BN_DIR}/weekly_ohlcv.parquet")
    price_piv = (ohlcv.pivot(index="week_start", columns="symbol", values="close")
                 .sort_index())
    price_piv.index = pd.to_datetime(price_piv.index)

    # Compute weekly returns
    ret_piv = price_piv.pct_change().clip(-0.99, 10)
    btc_ret = ret_piv["BTC"].dropna()

    # Rolling 52w beta for each token vs BTC
    BETA_WINDOW = 52  # weeks
    beta_df = pd.DataFrame(index=ret_piv.index, columns=ret_piv.columns, dtype=float)

    for sym in ret_piv.columns:
        tok = ret_piv[sym].dropna()
        common = tok.index.intersection(btc_ret.index)
        if len(common) < BETA_WINDOW:
            continue
        aligned_tok = tok.loc[common]
        aligned_btc = btc_ret.loc[common]
        # Rolling covariance / variance
        roll_cov = aligned_tok.rolling(BETA_WINDOW).cov(aligned_btc)
        roll_var = aligned_btc.rolling(BETA_WINDOW).var()
        beta_series = roll_cov / roll_var.replace(0, float("nan"))
        beta_df.loc[beta_series.index, sym] = beta_series.values

    beta_df = beta_df.astype(float)

    # For each rebalancing period, compute avg basket beta
    long_betas, short_betas, net_betas = [], [], []
    dates_used = []
    LONG_SCALE = SHORT_SCALE = 0.75

    for _, row in log_df.iterrows():
        date = row["date"]
        long_syms  = [s for s in row["long_basket"].split("|")  if s]
        short_syms = [s for s in row["short_basket"].split("|") if s]

        # Find closest price row at or before date
        avail = beta_df.index[beta_df.index <= date]
        if len(avail) == 0:
            continue
        brow = beta_df.loc[avail[-1]]

        lb = [float(brow[s]) for s in long_syms  if s in brow.index and pd.notna(brow[s])]
        sb = [float(brow[s]) for s in short_syms if s in brow.index and pd.notna(brow[s])]

        if not lb or not sb:
            continue

        avg_lb = np.nanmean(lb)
        avg_sb = np.nanmean(sb)
        # Net portfolio beta: +LONG_SCALE × long_beta - SHORT_SCALE × short_beta
        net_b  = LONG_SCALE * avg_lb - SHORT_SCALE * avg_sb

        long_betas.append(avg_lb)
        short_betas.append(avg_sb)
        net_betas.append(net_b)
        dates_used.append(date)

    long_betas  = np.array(long_betas)
    short_betas = np.array(short_betas)
    net_betas   = np.array(net_betas)

    print(f"\n  {'Basket':<15} {'Avg Beta':>10} {'Std':>8} {'Min':>8} {'Max':>8}")
    print("  " + "-"*50)
    print(f"  {'Long basket':<15} {long_betas.mean():>+10.3f} {long_betas.std():>8.3f} {long_betas.min():>8.3f} {long_betas.max():>8.3f}")
    print(f"  {'Short basket':<15} {short_betas.mean():>+10.3f} {short_betas.std():>8.3f} {short_betas.min():>8.3f} {short_betas.max():>8.3f}")
    print(f"  {'Net portfolio':<15} {net_betas.mean():>+10.3f} {net_betas.std():>8.3f} {net_betas.min():>8.3f} {net_betas.max():>8.3f}")

    n_pos = (net_betas > 0.1).sum()
    n_neg = (net_betas < -0.1).sum()
    n_neu = len(net_betas) - n_pos - n_neg
    print(f"\n  Net beta > +0.1 (net long market)  : {n_pos}/{len(net_betas)} periods")
    print(f"  Net beta < -0.1 (net short market) : {n_neg}/{len(net_betas)} periods")
    print(f"  Net beta in [-0.1, +0.1] (neutral) : {n_neu}/{len(net_betas)} periods")

    # Plot
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    dates_arr = np.array(dates_used)

    axes[0].plot(dates_arr, long_betas,  label="Long basket beta",  color="steelblue")
    axes[0].plot(dates_arr, short_betas, label="Short basket beta", color="crimson")
    axes[0].axhline(1.0, color="gray", ls="--", lw=0.8, label="BTC beta=1")
    axes[0].axhline(0.0, color="black", lw=0.5)
    axes[0].set_ylabel("Rolling 52w Beta vs BTC")
    axes[0].set_title("Basket Beta Decomposition")
    axes[0].legend(fontsize=9)

    axes[1].fill_between(dates_arr, net_betas, 0,
                          where=net_betas > 0, alpha=0.4, color="steelblue",
                          label="Net long market")
    axes[1].fill_between(dates_arr, net_betas, 0,
                          where=net_betas < 0, alpha=0.4, color="crimson",
                          label="Net short market")
    axes[1].axhline(0, color="black", lw=0.8)
    axes[1].set_ylabel("Net Portfolio Beta vs BTC")
    axes[1].set_xlabel("Date")
    axes[1].legend(fontsize=9)
    axes[1].set_title(f"Net Beta (0.75×Long - 0.75×Short)  |  Avg: {net_betas.mean():+.3f}")

    plt.tight_layout()
    fname = OUTPUT_DIR + "diag_beta.png"
    plt.savefig(fname, dpi=120); plt.close()
    print(f"\n  [Plot] {fname}")

    return dates_used, long_betas, short_betas, net_betas

# ─────────────────────────────────────────────────────────────────────────────
#  DIAG 3 — PER-TOKEN P&L ATTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

def diag_token_attribution(log_df: pd.DataFrame):
    print("\n" + "="*70)
    print("DIAG 3 — PER-TOKEN P&L ATTRIBUTION")
    print("  Each token's gross contribution to combined P&L")
    print("="*70)

    ohlcv = pd.read_parquet(f"{BN_DIR}/weekly_ohlcv.parquet")
    price_piv = (ohlcv.pivot(index="week_start", columns="symbol", values="close")
                 .sort_index())
    price_piv.index = pd.to_datetime(price_piv.index)

    long_contrib  = {}
    short_contrib = {}
    long_periods  = {}
    short_periods = {}

    LONG_SCALE = SHORT_SCALE = 0.75

    # CMC dates are Sundays; Binance week_start is Monday 6 days earlier
    rebal_dates = log_df["date"].tolist()

    for i, row in log_df.iterrows():
        t0_cmc = row["date"]
        # Convert CMC Sunday -> Binance Monday (subtract 6 days)
        t0_bn = t0_cmc - pd.Timedelta(days=6)

        # Find closest available Binance week_start at or after t0_bn
        avail_t0 = price_piv.index[price_piv.index >= t0_bn - pd.Timedelta(days=3)]
        if len(avail_t0) == 0:
            continue
        t0_key = avail_t0[0]

        # Next rebalancing CMC date -> Binance Monday
        if i + 1 < len(rebal_dates):
            t1_cmc = rebal_dates[i + 1]
        else:
            t1_cmc = t0_cmc + pd.Timedelta(weeks=4)
        t1_bn = t1_cmc - pd.Timedelta(days=6)
        avail_t1 = price_piv.index[price_piv.index >= t1_bn - pd.Timedelta(days=3)]
        if len(avail_t1) == 0:
            continue
        t1_key = avail_t1[0]

        if t0_key == t1_key or t0_key not in price_piv.index or t1_key not in price_piv.index:
            continue
        p0 = price_piv.loc[t0_key]
        p1 = price_piv.loc[t1_key]
        fwd = (p1 / p0 - 1).clip(-0.99, 5)

        long_syms  = [s for s in row["long_basket"].split("|")  if s]
        short_syms = [s for s in row["short_basket"].split("|") if s]

        # Equal-weight
        nl = len(long_syms); ns = len(short_syms)

        for s in long_syms:
            if s in fwd.index and pd.notna(fwd[s]):
                contrib = LONG_SCALE * float(fwd[s]) / nl
                long_contrib[s]  = long_contrib.get(s, 0) + contrib
                long_periods[s]  = long_periods.get(s, 0) + 1

        for s in short_syms:
            if s in fwd.index and pd.notna(fwd[s]):
                contrib = SHORT_SCALE * (-float(fwd[s])) / ns
                short_contrib[s]  = short_contrib.get(s, 0) + contrib
                short_periods[s]  = short_periods.get(s, 0) + 1

    # Long attribution
    print(f"\n  --- Long Leg: Top/Bottom 10 contributors ---")
    print(f"  {'Symbol':<10} {'Periods':>8} {'Gross Contrib':>14} {'Per Period':>12}")
    print("  " + "-"*50)
    long_sorted = sorted(long_contrib.items(), key=lambda x: x[1], reverse=True)
    for sym, c in long_sorted[:10]:
        p = long_periods.get(sym, 0)
        print(f"  {sym:<10} {p:>8}   {c:>+12.2%}   {c/p:>+10.2%}")
    print("  ...")
    for sym, c in long_sorted[-5:]:
        p = long_periods.get(sym, 0)
        print(f"  {sym:<10} {p:>8}   {c:>+12.2%}   {c/p:>+10.2%}")

    # Short attribution
    print(f"\n  --- Short Leg: Top/Bottom 10 contributors ---")
    print(f"  {'Symbol':<10} {'Periods':>8} {'Gross Contrib':>14} {'Per Period':>12}")
    print("  " + "-"*50)
    short_sorted = sorted(short_contrib.items(), key=lambda x: x[1], reverse=True)
    for sym, c in short_sorted[:10]:
        p = short_periods.get(sym, 0)
        print(f"  {sym:<10} {p:>8}   {c:>+12.2%}   {c/p:>+10.2%}")
    print("  ...")
    for sym, c in short_sorted[-5:]:
        p = short_periods.get(sym, 0)
        print(f"  {sym:<10} {p:>8}   {c:>+12.2%}   {c/p:>+10.2%}")

    # Concentration: what % of total long P&L comes from top 3 tokens
    total_long  = sum(long_contrib.values())
    total_short = sum(short_contrib.values())
    top3_long   = sum(c for _, c in long_sorted[:3])
    top3_short  = sum(c for _, c in short_sorted[:3])
    print(f"\n  Long leg total gross P&L  : {total_long:+.2%}")
    if total_long != 0:
        print(f"  Top 3 tokens contribute   : {top3_long:+.2%} ({top3_long/total_long*100:.1f}% of total)")
    print(f"\n  Short leg total gross P&L : {total_short:+.2%}")
    if total_short != 0:
        print(f"  Top 3 tokens contribute   : {top3_short:+.2%} ({top3_short/total_short*100:.1f}% of total)")

    # Plot
    fig, axes = plt.subplots(1, 2, figsize=(14, 7))
    for ax, data, title in [
        (axes[0], long_sorted[:15],  "Long Leg: Top 15 Contributors"),
        (axes[1], short_sorted[:15], "Short Leg: Top 15 Contributors"),
    ]:
        syms  = [x[0] for x in data]
        vals  = [x[1] for x in data]
        cols  = ["steelblue" if v >= 0 else "crimson" for v in vals]
        ax.barh(syms[::-1], [v*100 for v in vals[::-1]], color=cols[::-1])
        ax.axvline(0, color="black", lw=0.8)
        ax.set_xlabel("Cumulative Gross Contribution (%)")
        ax.set_title(title)
        ax.set_xlim(min(min(v*100 for v in vals)-2, -5),
                    max(max(v*100 for v in vals)+2,  5))

    plt.tight_layout()
    fname = OUTPUT_DIR + "diag_attribution.png"
    plt.savefig(fname, dpi=120); plt.close()
    print(f"\n  [Plot] {fname}")


# ─────────────────────────────────────────────────────────────────────────────
#  DIAG 4 — SPREAD DISTRIBUTION & FAT TAIL ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def diag_spread_distribution(log_df: pd.DataFrame):
    print("\n" + "="*70)
    print("DIAG 4 — SPREAD DISTRIBUTION & FAT TAIL ANALYSIS")
    print("="*70)

    spreads = log_df["long_gross"] - log_df["short_gross"]
    combined = log_df["combined_net"]
    regimes  = log_df["regime"]

    # Sort by magnitude
    spread_sorted = spreads.sort_values(ascending=False)
    dates_sorted  = log_df.set_index("date")["regime"]

    # What fraction of total Sharpe do top N periods contribute?
    mean_s  = spreads.mean()
    std_s   = spreads.std()
    sharpe_full = mean_s / std_s * np.sqrt(12)

    # Drop top N and recompute Sharpe
    print(f"\n  Full spread Sharpe (gross): {sharpe_full:+.3f}")
    print(f"  Mean spread: {mean_s:+.2%}  |  Std: {std_s:+.2%}")
    print(f"\n  Sharpe sensitivity to removing top N periods:")
    print(f"  {'N removed':>12} {'Remaining':>10} {'Sharpe':>10} {'Sharpe %':>10}")
    print("  " + "-"*45)
    for n in [0, 1, 2, 3, 5]:
        top_idx = spread_sorted.index[:n]
        rem = spreads.drop(top_idx)
        if len(rem) < 5 or rem.std() == 0:
            continue
        sh = rem.mean() / rem.std() * np.sqrt(12)
        ratio = sh / sharpe_full * 100
        print(f"  {n:>12}  {len(rem):>9}  {sh:>+9.3f}  {ratio:>9.1f}%")

    # CVaR
    alpha = 0.05
    var   = np.percentile(spreads, alpha * 100)
    cvar  = spreads[spreads <= var].mean()
    print(f"\n  5% VaR  (monthly spread): {var:+.2%}")
    print(f"  5% CVaR (expected tail) : {cvar:+.2%}")
    print(f"  Best period             : {spread_sorted.iloc[0]:+.2%}  (date: {spread_sorted.index[0].date() if hasattr(spread_sorted.index[0], 'date') else spread_sorted.index[0]})")
    print(f"  Worst period            : {spread_sorted.iloc[-1]:+.2%}  (date: {spread_sorted.index[-1].date() if hasattr(spread_sorted.index[-1], 'date') else spread_sorted.index[-1]})")

    # Worst 5 periods
    print(f"\n  Worst 5 periods:")
    print(f"  {'Date':<14} {'Regime':<10} {'Spread':>10} {'Comb Net':>10}")
    print("  " + "-"*47)
    worst5 = spread_sorted.tail(5).iloc[::-1]
    for idx in worst5.index:
        row  = log_df[log_df.index == idx]
        if len(row) == 0:
            row = log_df[log_df["date"] == idx]
        if len(row) == 0:
            continue
        row = row.iloc[0]
        print(f"  {str(row['date'])[:10]:<14} {row['regime']:<10} {row['long_gross']-row['short_gross']:>+9.2%} {row['combined_net']:>+9.2%}")

    # Best 5 periods
    print(f"\n  Best 5 periods:")
    print(f"  {'Date':<14} {'Regime':<10} {'Spread':>10} {'Comb Net':>10}")
    print("  " + "-"*47)
    best5 = spread_sorted.head(5)
    for idx in best5.index:
        row = log_df[log_df.index == idx]
        if len(row) == 0:
            row = log_df[log_df["date"] == idx]
        if len(row) == 0:
            continue
        row = row.iloc[0]
        print(f"  {str(row['date'])[:10]:<14} {row['regime']:<10} {row['long_gross']-row['short_gross']:>+9.2%} {row['combined_net']:>+9.2%}")

    # Plot
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))

    col_map = {"Bull": "steelblue", "Bear": "crimson", "Sideways": "gray"}
    colors = [col_map.get(r, "gray") for r in regimes]

    regime_by_idx = dict(zip(log_df.index, log_df["regime"]))
    bar_colors = [col_map.get(regime_by_idx.get(i, "Sideways"), "gray")
                  for i in spread_sorted.index]
    axes[0].bar(range(len(spread_sorted)), spread_sorted.values * 100,
                color=bar_colors, alpha=0.7)
    axes[0].axhline(0, color="black", lw=0.8)
    axes[0].set_xlabel("Period rank (best to worst)")
    axes[0].set_ylabel("Gross Spread (%)")
    axes[0].set_title("Per-Period Gross Spread (sorted)")

    from scipy import stats as scipy_stats
    axes[1].hist(spreads * 100, bins=20, color="steelblue", alpha=0.7,
                 edgecolor="white", label="Actual")
    xs = np.linspace(spreads.min() * 100, spreads.max() * 100, 200)
    normal_pdf = scipy_stats.norm.pdf(xs, spreads.mean()*100, spreads.std()*100)
    normal_pdf *= len(spreads) * (spreads.std() * 100 * 2)
    axes[1].plot(xs, normal_pdf, "r--", lw=1.5, label="Normal fit")
    axes[1].axvline(var * 100, color="orange", lw=1.5, ls="--",
                    label=f"5% VaR = {var:.1%}")
    axes[1].set_xlabel("Gross Spread (%)")
    axes[1].set_title(f"Spread Distribution  |  Kurtosis={spreads.kurtosis():.2f}  Skew={spreads.skew():.2f}")
    axes[1].legend(fontsize=9)

    plt.tight_layout()
    fname = OUTPUT_DIR + "diag_spread_dist.png"
    plt.savefig(fname, dpi=120); plt.close()
    print(f"\n  [Plot] {fname}")


# ─────────────────────────────────────────────────────────────────────────────
#  DIAG 5 — BEAR PERIOD STATISTICAL SIGNIFICANCE
# ─────────────────────────────────────────────────────────────────────────────

def diag_bear_significance(log_df: pd.DataFrame):
    print("\n" + "="*70)
    print("DIAG 5 — BEAR PERIOD STATISTICAL SIGNIFICANCE")
    print("  Bootstrap CI on Bear geo spread. Is Bear alpha real or noise?")
    print("="*70)

    spreads = log_df["long_gross"] - log_df["short_gross"]
    regimes = log_df["regime"]

    N_BOOT = 10_000

    for regime in ["Bull", "Bear", "Sideways"]:
        mask = regimes == regime
        sub  = spreads[mask].values
        n    = len(sub)
        if n < 2:
            print(f"\n  {regime}: only {n} period(s), skip")
            continue

        geo_spread = (np.prod(1 + sub) ** (12 / n) - 1) * 100

        # Bootstrap
        boot_geo = []
        rng = np.random.default_rng(42)
        for _ in range(N_BOOT):
            sample = rng.choice(sub, size=n, replace=True)
            boot_geo.append((np.prod(1 + sample) ** (12 / n) - 1) * 100)
        boot_geo = np.array(boot_geo)

        ci_lo = np.percentile(boot_geo, 2.5)
        ci_hi = np.percentile(boot_geo, 97.5)
        p_zero = (boot_geo <= 0).mean()

        print(f"\n  {regime} regime ({n} periods):")
        print(f"    Geo spread ann.  : {geo_spread:+.2f}%")
        print(f"    Bootstrap 95% CI : [{ci_lo:+.2f}%, {ci_hi:+.2f}%]")
        print(f"    P(spread <= 0)   : {p_zero:.4f}  ({'significant' if p_zero < 0.05 else 'NOT significant at 5%'})")
        print(f"    Bootstrap mean   : {boot_geo.mean():+.2f}%")
        print(f"    Bootstrap std    : {boot_geo.std():.2f}%")

    # Plot bootstrap distributions
    fig, axes = plt.subplots(1, 3, figsize=(14, 5))
    rng = np.random.default_rng(42)
    for ax, regime in zip(axes, ["Bull", "Bear", "Sideways"]):
        mask = regimes == regime
        sub  = spreads[mask].values
        n    = len(sub)
        if n < 2:
            ax.set_title(f"{regime} (n={n}, insufficient)")
            continue
        geo_real = (np.prod(1 + sub) ** (12 / n) - 1) * 100
        boot = [(np.prod(1 + rng.choice(sub, n, replace=True)) ** (12/n) - 1)*100
                for _ in range(N_BOOT)]
        boot = np.array(boot)
        ci_lo, ci_hi = np.percentile(boot, [2.5, 97.5])
        p_zero = (boot <= 0).mean()
        ax.hist(boot, bins=50, color="steelblue", alpha=0.7)
        ax.axvline(geo_real, color="red", lw=2, label=f"Actual={geo_real:.1f}%")
        ax.axvline(0,        color="black", lw=1, ls="--", label="Zero")
        ax.axvline(ci_lo, color="orange", lw=1.2, ls=":", label=f"95% CI")
        ax.axvline(ci_hi, color="orange", lw=1.2, ls=":")
        ax.set_title(f"{regime} (n={n})\nP(≤0)={p_zero:.3f}")
        ax.set_xlabel("Ann. Geo Spread (%)")
        ax.legend(fontsize=7)

    plt.suptitle("Bootstrap Distribution of Ann. Geo Spread by Regime", fontsize=11)
    plt.tight_layout()
    fname = OUTPUT_DIR + "diag_bear_bootstrap.png"
    plt.savefig(fname, dpi=120); plt.close()
    print(f"\n  [Plot] {fname}")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("="*70)
    print("BACKTEST DIAGNOSTICS")
    print("="*70)

    log_df = generate_basket_log()
    if log_df is None:
        sys.exit(1)

    # Compute spread for downstream use
    log_df["spread"] = log_df["long_gross"] - log_df["short_gross"]
    log_df = log_df.reset_index(drop=True)

    diag_funding_strip()
    diag_beta(log_df)
    diag_token_attribution(log_df)
    diag_spread_distribution(log_df)
    diag_bear_significance(log_df)

    # Cleanup temp log
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)

    print("\n" + "="*70)
    print("All diagnostics complete.")
    print("="*70)
