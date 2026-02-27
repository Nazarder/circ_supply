"""
extreme_percentile.py

Head-to-head test: 1st percentile (lowest supply inflation) vs 99th percentile
(highest supply inflation) baskets, equal-weighted, no benchmark, no beta-hedging.

Goal: determine whether hyper-inflationary tokens structurally underperform
deflationary/stable tokens in absolute terms.
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
INPUT_FILE       = "D:/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
OUTPUT_FILE      = "D:/circ_supply/extreme_pct_cumulative.png"
FFILL_LIMIT      = 1
SUPPLY_WINDOW    = 13    # weeks (~90 days) for trailing supply inflation
FORWARD_PRDS     = 4     # weeks forward return holding period
SLIPPAGE_K       = 0.0005
MIN_TURNOVER     = 0.001
MAX_SLIPPAGE     = 0.02
LOW_PCT          = 0.10  # 10th percentile cutoff
HIGH_PCT         = 0.90  # 90th percentile cutoff


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _winsorize_cross_section(g):
    lo = g.quantile(0.01)
    hi = g.quantile(0.99)
    return g.clip(lower=lo, upper=hi)


def _portfolio_stats(returns: pd.Series) -> dict:
    returns = returns.dropna()
    if len(returns) < 2:
        return dict(ann_return=np.nan, volatility=np.nan, max_dd=np.nan)

    cum = (1 + returns).cumprod()

    total_days  = (returns.index[-1] - returns.index[0]).days
    total_years = max(total_days / 365.25, 1 / 52)
    cum_final   = float(cum.iloc[-1])
    ann_return  = cum_final ** (1.0 / total_years) - 1 if cum_final > 0 else np.nan

    gaps           = np.diff(returns.index).astype("timedelta64[D]").astype(float)
    median_gap     = float(np.median(gaps)) if len(gaps) > 0 else 7.0
    periods_per_yr = 365.25 / max(median_gap, 1.0)
    volatility     = returns.std() * np.sqrt(periods_per_yr)

    roll_max = cum.cummax()
    max_dd   = ((cum - roll_max) / roll_max).min()

    return dict(ann_return=ann_return, volatility=volatility, max_dd=max_dd)


# ---------------------------------------------------------------------------
# STEP 1 — Load & prep
# ---------------------------------------------------------------------------

def load_data(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["snapshot_date"])
    df = df.sort_values(["symbol", "snapshot_date"]).reset_index(drop=True)

    df = df[df["price"].notna() & (df["price"] > 0) & df["market_cap"].notna()].copy()

    df["circulating_supply"] = (
        df.groupby("symbol")["circulating_supply"]
        .transform(lambda s: s.ffill(limit=FFILL_LIMIT))
    )
    df["price"] = (
        df.groupby("symbol")["price"]
        .transform(lambda s: s.ffill(limit=FFILL_LIMIT))
    )
    df = df[df["circulating_supply"].notna() & (df["circulating_supply"] > 0)].copy()

    print(f"[Data] Rows: {len(df):,}  |  Symbols: {df['symbol'].nunique():,}  |  "
          f"Dates: {df['snapshot_date'].min().date()} to {df['snapshot_date'].max().date()}")
    return df


# ---------------------------------------------------------------------------
# STEP 2 — Feature engineering
# ---------------------------------------------------------------------------

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    grp = df.groupby("symbol", group_keys=False)

    # 13-week trailing supply inflation
    df["supply_pct_13p"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW)
    )

    # Slippage
    df["turnover"] = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"] = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)

    return df


# ---------------------------------------------------------------------------
# STEP 3 — Forward returns (winsorized, slippage-adjusted)
# ---------------------------------------------------------------------------

def build_forward_returns(df: pd.DataFrame) -> pd.DataFrame:
    price_pivot   = df.pivot_table(index="snapshot_date", columns="symbol",
                                   values="price",    aggfunc="last")
    slip_pivot    = df.pivot_table(index="snapshot_date", columns="symbol",
                                   values="slippage", aggfunc="last")

    fwd_raw = price_pivot.shift(-FORWARD_PRDS) / price_pivot - 1

    # Cross-sectional winsorize then floor at -1.0
    fwd_wins = fwd_raw.apply(
        lambda col: col, axis=0   # placeholder — apply row-wise below
    )
    fwd_wins = fwd_raw.T.apply(
        lambda row: row.clip(lower=row.quantile(0.01), upper=row.quantile(0.99))
    ).T
    fwd_wins = fwd_wins.clip(lower=-1.0)

    # Subtract slippage
    fwd_adj = fwd_wins - slip_pivot

    # Stack to long format
    fwd_long = fwd_adj.stack().reset_index()
    fwd_long.columns = ["snapshot_date", "symbol", "fwd_return"]

    return fwd_long


# ---------------------------------------------------------------------------
# STEP 4 — Monthly rebalancing: extreme percentile baskets
# ---------------------------------------------------------------------------

def run_extreme_baskets(df: pd.DataFrame, fwd_long: pd.DataFrame):
    df["ym"] = df["snapshot_date"].dt.to_period("M")

    # First snapshot of each month = rebalancing date
    rebal_dates = set(
        df.groupby("ym")["snapshot_date"].min()
    )

    # Supply inflation at each snapshot
    inf_df = df[["snapshot_date", "symbol", "supply_pct_13p"]].copy()
    inf_df = inf_df[inf_df["supply_pct_13p"].notna()]

    # Merge forward returns with inflation
    merged = fwd_long.merge(inf_df, on=["snapshot_date", "symbol"], how="inner")
    merged = merged[merged["snapshot_date"].isin(rebal_dates)]
    merged = merged[merged["fwd_return"].notna()]

    low_rets   = []
    high_rets  = []
    dates_used = []
    basket_sizes = []

    for date in sorted(rebal_dates):
        sl = merged[merged["snapshot_date"] == date].copy()
        if len(sl) < 10:   # need enough tokens to compute meaningful percentiles
            continue

        lo_cut = sl["supply_pct_13p"].quantile(LOW_PCT)
        hi_cut = sl["supply_pct_13p"].quantile(HIGH_PCT)

        basket_lo = sl[sl["supply_pct_13p"] <= lo_cut]
        basket_hi = sl[sl["supply_pct_13p"] >= hi_cut]

        if len(basket_lo) == 0 or len(basket_hi) == 0:
            continue

        # Equal-weighted return, hard floor at -1.0
        r_lo = float(basket_lo["fwd_return"].mean())
        r_hi = float(basket_hi["fwd_return"].mean())

        low_rets.append(max(r_lo, -1.0))
        high_rets.append(max(r_hi, -1.0))
        dates_used.append(date)
        basket_sizes.append((len(basket_lo), len(basket_hi)))

    idx = pd.DatetimeIndex(dates_used)
    low_series  = pd.Series(low_rets,  index=idx, name="1st Pct (Low Inflation)")
    high_series = pd.Series(high_rets, index=idx, name="99th Pct (High Inflation)")

    avg_lo = np.mean([s[0] for s in basket_sizes])
    avg_hi = np.mean([s[1] for s in basket_sizes])
    print(f"[Baskets] Rebalancing periods: {len(dates_used)}")
    print(f"[Baskets] Avg basket size -- Low: {avg_lo:.1f} tokens, High: {avg_hi:.1f} tokens")

    return low_series, high_series


# ---------------------------------------------------------------------------
# STEP 5 — Stats & output
# ---------------------------------------------------------------------------

def report_and_plot(low_series: pd.Series, high_series: pd.Series) -> None:
    def _fmt(v):
        return f"{v:.2%}" if not np.isnan(v) else "N/A"

    print("\n[Results] Extreme Percentile Basket Performance")
    print(f"  {'Basket':<30} {'Ann.Return':>12} {'Volatility':>12} {'MaxDD':>12}")
    for name, s in [("1st Pct (Low Inflation)", low_series),
                    ("99th Pct (High Inflation)", high_series)]:
        st = _portfolio_stats(s)
        print(f"  {name:<30} {_fmt(st['ann_return']):>12} "
              f"{_fmt(st['volatility']):>12} {_fmt(st['max_dd']):>12}")

    # Spread: Low minus High per period
    spread = low_series - high_series
    sp_stats = _portfolio_stats(spread)
    print(f"\n[Results] Performance Spread (Low minus High):")
    print(f"  Mean period spread : {spread.mean():.4f} ({spread.mean():.2%})")
    print(f"  Spread ann. return : {_fmt(sp_stats['ann_return'])}")
    print(f"  Spread ann. vol    : {_fmt(sp_stats['volatility'])}")
    print(f"  Periods Low > High : {(spread > 0).sum()} / {len(spread)} "
          f"({(spread > 0).mean():.1%})")

    # Cumulative curves
    cum_lo = (1 + low_series.dropna()).cumprod()
    cum_hi = (1 + high_series.dropna()).cumprod()

    fig, axes = plt.subplots(2, 1, figsize=(11, 9),
                             gridspec_kw={"height_ratios": [3, 1]})

    # Top panel: cumulative wealth (log scale)
    ax = axes[0]
    ax.semilogy(cum_lo.index, cum_lo.values, color="steelblue", lw=2.0,
                label="1st Pct — Low Inflation")
    ax.semilogy(cum_hi.index, cum_hi.values, color="crimson",   lw=2.0,
                label="99th Pct — High Inflation")
    ax.axhline(1, color="black", lw=0.6, ls="--")
    ax.set_ylabel("Cumulative Return (log scale, 1 = start)")
    ax.set_title(
        "Extreme Supply Inflation Baskets: 10th vs 90th Percentile\n"
        f"Equal-weight, {SUPPLY_WINDOW}-week trailing inflation, "
        f"{FORWARD_PRDS}-week forward return, slippage adjusted"
    )
    ax.legend(fontsize=10)
    ax.grid(True, which="both", alpha=0.3)

    # Bottom panel: rolling spread (Low - High) per period
    ax2 = axes[1]
    ax2.bar(spread.index, spread.values,
            color=["steelblue" if v >= 0 else "crimson" for v in spread.values],
            width=20, alpha=0.7)
    ax2.axhline(0, color="black", lw=0.8)
    ax2.set_ylabel("Period Spread\n(Low minus High)")
    ax2.set_xlabel("Rebalance Date")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(OUTPUT_FILE, dpi=150)
    plt.close(fig)
    print(f"\n[Chart] Saved: {OUTPUT_FILE}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Extreme Percentile Supply Inflation Test")
    print("10th Pct (Low) vs 90th Pct (High) -- No Benchmark")
    print("=" * 60)

    df       = load_data(INPUT_FILE)
    df       = engineer_features(df)
    fwd_long = build_forward_returns(df)
    low_s, high_s = run_extreme_baskets(df, fwd_long)
    report_and_plot(low_s, high_s)

    print("\nDone.")


if __name__ == "__main__":
    main()
