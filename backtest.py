"""
Cryptocurrency Token Unlock Backtesting Script
Test three hypotheses about token supply inflation and price performance.
Dataset: Top 300 cryptocurrencies, weekly snapshots (Jan 2017 – Feb 2026).
Supply unlocks inferred from circulating_supply changes.

NOTE: Survivorship bias is not corrected — tokens that dropped out of the
top 300 simply have no data after exit.
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats

# ---------------------------------------------------------------------------
# CONFIG — all windows in snapshot periods (~1 period ≈ 1 week)
# ---------------------------------------------------------------------------
INPUT_FILE           = "cmc_historical_top300_filtered_with_supply.csv"
FFILL_LIMIT          = 1          # periods (1 week max stale fill)
INDEX_TOP_N          = 100        # coins for cap-weighted benchmark
UNLOCK_THRESH        = 0.03       # 3% 1-period supply jump threshold
COOLDOWN_PRDS        = 4          # ~30-day cooldown between events per token
EVENT_WINDOW         = 2          # periods each side for event study (T-2 to T+2)
CONT_QUARTILE        = 0.75       # Q4 threshold (top 25% = highest inflation)
FORWARD_PRDS         = 4          # ~30-day forward return for H2/H3
SUPPLY_WINDOW_SHORT  = 4          # ~30 days
SUPPLY_WINDOW_MEDIUM = 13         # ~90 days
SUPPLY_WINDOW_LONG   = 52         # ~365 days
RETURN_CAP           = 1.0        # clip pct_return at ±100% per period
OUTPUT_DIR           = "D:/circ_supply/"


# ===========================================================================
# STEP 1 — Data Loading & Preprocessing
# ===========================================================================

def load_data(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["snapshot_date"])
    df = df.sort_values(["symbol", "snapshot_date"]).reset_index(drop=True)

    # Filter invalid rows (reuse pattern from circulating_supply.py)
    df = df[df["price"].notna() & (df["price"] > 0) & df["market_cap"].notna()].copy()

    # Forward-fill circulating_supply and price per symbol (limit = FFILL_LIMIT)
    df["circulating_supply"] = (
        df.groupby("symbol")["circulating_supply"]
        .transform(lambda s: s.ffill(limit=FFILL_LIMIT))
    )
    df["price"] = (
        df.groupby("symbol")["price"]
        .transform(lambda s: s.ffill(limit=FFILL_LIMIT))
    )

    # Drop rows where circulating_supply is still NaN after ffill
    df = df[df["circulating_supply"].notna() & (df["circulating_supply"] > 0)].copy()

    # Compute period-over-period return from price (clipped at ±RETURN_CAP)
    df["pct_return"] = (
        df.groupby("symbol")["price"]
        .transform(lambda s: s.pct_change(1).clip(-RETURN_CAP, RETURN_CAP))
    )

    # Integer period index per token (0-based, within each symbol)
    df["period_idx"] = df.groupby("symbol").cumcount()

    print(f"[Data] Shape after load & filter: {df.shape}")
    print(f"[Data] Date range: {df['snapshot_date'].min().date()} to {df['snapshot_date'].max().date()}")
    print(f"[Data] Unique symbols: {df['symbol'].nunique()}")
    return df


# ===========================================================================
# STEP 2 — Broad Market Index (cap-weighted, top N by rank)
# ===========================================================================

def build_index(df: pd.DataFrame) -> pd.DataFrame:
    top = df[df["rank"] <= INDEX_TOP_N].copy()
    top = top[top["pct_return"].notna()].copy()

    def cap_weighted_return(g):
        total_cap = g["market_cap"].sum()
        if total_cap == 0:
            return np.nan
        weights = g["market_cap"] / total_cap
        return (weights * g["pct_return"]).sum()

    index_df = (
        top.groupby("snapshot_date", group_keys=False)
        .apply(cap_weighted_return, include_groups=False)
        .reset_index()
        .rename(columns={0: "index_return"})
    )
    print(f"[Index] Snapshots in benchmark: {len(index_df)}")
    return index_df


# ===========================================================================
# STEP 3 — Feature Engineering
# ===========================================================================

def _apply_cooldown(flag_series: pd.Series, cooldown: int) -> pd.Series:
    """Suppress unlock flags within cooldown periods after each event."""
    result = flag_series.copy().astype(bool)
    idx = np.where(result)[0]
    suppress_until = -1
    for i in idx:
        if i <= suppress_until:
            result.iloc[i] = False
        else:
            suppress_until = i + cooldown
    return result


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    grp = df.groupby("symbol", group_keys=False)

    # Supply inflation features
    df["supply_pct_1p"]  = grp["circulating_supply"].transform(lambda s: s.pct_change(1))
    df["supply_pct_4p"]  = grp["circulating_supply"].transform(lambda s: s.pct_change(SUPPLY_WINDOW_SHORT))
    df["supply_pct_13p"] = grp["circulating_supply"].transform(lambda s: s.pct_change(SUPPLY_WINDOW_MEDIUM))
    df["supply_pct_52p"] = grp["circulating_supply"].transform(lambda s: s.pct_change(SUPPLY_WINDOW_LONG))

    # Baseline emission: rolling median of supply_pct_1p over 12 periods
    df["baseline_emission"] = grp["supply_pct_1p"].transform(
        lambda s: s.rolling(12, min_periods=4).median()
    )
    df["supply_spike"] = df["supply_pct_1p"] - df["baseline_emission"]

    # --- Large Unlock Event Flag (H1) ---
    # Require ≥ 2*EVENT_WINDOW periods of history before flagging
    min_history = 2 * EVENT_WINDOW
    df["raw_unlock"] = (
        df["supply_spike"].gt(UNLOCK_THRESH)
        & df["period_idx"].ge(min_history)
        & df["supply_pct_1p"].notna()
        & df["baseline_emission"].notna()
    )

    # Apply per-symbol cooldown
    df["is_unlock_event"] = (
        df.groupby("symbol")["raw_unlock"]
        .transform(lambda s: _apply_cooldown(s, COOLDOWN_PRDS))
    )
    df.drop(columns=["raw_unlock"], inplace=True)

    # --- Continuous Inflation Quartile Flag (H2/H3) ---
    # Quartile rank at each snapshot across all tokens

    def _safe_qcut_quartile(s):
        """Assign quartile labels 1-4; return NaN for groups too small or all-NaN."""
        valid_count = s.notna().sum()
        if valid_count < 4:
            return pd.Series(np.nan, index=s.index)
        ranks = s.rank(method="first")
        try:
            return pd.qcut(ranks, 4, labels=[1, 2, 3, 4])
        except ValueError:
            return pd.Series(np.nan, index=s.index)

    for col, qcol in [("supply_pct_13p", "quartile_13p"), ("supply_pct_52p", "quartile_52p")]:
        df[qcol] = (
            df.groupby("snapshot_date")[col]
            .transform(_safe_qcut_quartile)
            .astype("Int64")
        )

    n_events = df["is_unlock_event"].sum()
    print(f"[Features] Unlock events found: {n_events}")
    return df


# ===========================================================================
# STEP 4 — Hypothesis 1: Event Study on Large Unlocks
# ===========================================================================

def run_h1_event_study(df: pd.DataFrame, index_df: pd.DataFrame) -> None:
    print("\n[H1] Running event study on large unlock events...")

    # Map snapshot_date → index_return
    idx_map = index_df.set_index("snapshot_date")["index_return"].to_dict()

    events = df[df["is_unlock_event"]].copy()
    if events.empty:
        print("[H1] No unlock events found. Skipping.")
        return

    # Build sorted date arrays per symbol for window lookup
    # Deduplicate by keeping last row per symbol+date (handles any duplicate snapshots)
    df_dedup = df.drop_duplicates(subset=["symbol", "snapshot_date"], keep="last")
    symbol_dates = {
        sym: grp["snapshot_date"].sort_values().reset_index(drop=True)
        for sym, grp in df_dedup.groupby("symbol")
    }
    symbol_returns = {
        sym: grp.set_index("snapshot_date")["pct_return"]
        for sym, grp in df_dedup.groupby("symbol")
    }

    window = range(-EVENT_WINDOW, EVENT_WINDOW + 1)  # e.g. -2 … +2
    all_car = []

    for _, row in events.iterrows():
        sym = row["symbol"]
        t0  = row["snapshot_date"]
        dates = symbol_dates[sym]
        ret_s = symbol_returns[sym]

        # Find T=0 position in this symbol's date array
        t0_pos = dates.searchsorted(t0)
        if t0_pos >= len(dates) or dates.iloc[t0_pos] != t0:
            continue

        # Gather returns for T-EW to T+EW
        abnormal_returns = []
        valid = True
        for offset in window:
            pos = t0_pos + offset
            if pos < 0 or pos >= len(dates):
                valid = False
                break
            d = dates.iloc[pos]
            tok_ret = ret_s.get(d, np.nan)
            idx_ret = idx_map.get(d, np.nan)
            # scalar-safe NaN check (handles both float and pandas NA)
            if pd.isna(tok_ret) or pd.isna(idx_ret):
                valid = False
                break
            abnormal_returns.append(float(tok_ret) - float(idx_ret))

        if valid and len(abnormal_returns) == len(window):
            # Cumulative Abnormal Return: running sum
            car = np.cumsum(abnormal_returns)
            all_car.append(car)

    if not all_car:
        print("[H1] Not enough complete event windows. Skipping plot.")
        return

    car_matrix = np.array(all_car)  # shape: (n_events, window_len)
    n = car_matrix.shape[0]
    acar = car_matrix.mean(axis=0)
    ci   = 1.96 * car_matrix.std(axis=0) / np.sqrt(n)

    print(f"[H1] Events with complete windows: {n}")

    # t-test on post-event CAR (index from T=0 onward → offset EVENT_WINDOW)
    post_idx   = EVENT_WINDOW  # index of T=0 in the window
    post_car   = car_matrix[:, post_idx:]  # T=0 to T+EW columns
    final_post = post_car[:, -1]           # CAR at T+EVENT_WINDOW
    t_stat, p_val = stats.ttest_1samp(final_post, 0)
    print(f"[H1] t-test on post-event CAR (T=0 to T+{EVENT_WINDOW}): "
          f"t={t_stat:.4f}, p={p_val:.4f}, n={n}")

    # Plot
    x_ticks = list(window)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(x_ticks, acar, color="steelblue", lw=2, label="ACAR")
    ax.fill_between(x_ticks, acar - ci, acar + ci, alpha=0.25,
                    color="steelblue", label="95% CI")
    ax.axhline(0, color="black", lw=0.8, ls="--")
    ax.axvline(0, color="red", lw=0.8, ls="--", label="Event (T=0)")
    ax.set_xlabel("Periods relative to unlock event")
    ax.set_ylabel("Cumulative Abnormal Return")
    ax.set_title(f"H1: Event Study — Large Supply Unlocks\n"
                 f"n={n} events, threshold={UNLOCK_THRESH:.0%}, ±{EVENT_WINDOW} periods")
    ax.legend()
    fig.tight_layout()
    out = OUTPUT_DIR + "h1_event_study.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"[H1] Saved: {out}")


# ===========================================================================
# STEP 5 — Hypothesis 2 & 3: Portfolio Sort (High vs Low Supply Inflation)
# ===========================================================================

def _portfolio_stats(returns: pd.Series) -> dict:
    """Compute annualised return, vol, Sharpe, and max drawdown from a return series."""
    returns = returns.dropna()
    if len(returns) < 2:
        return dict(ann_return=np.nan, volatility=np.nan, sharpe=np.nan, max_dd=np.nan)

    # Geometric annualised return
    cum = (1 + returns).cumprod()
    n   = len(returns)
    ann_return = cum.iloc[-1] ** (52 / n) - 1

    volatility = returns.std() * np.sqrt(52)
    sharpe     = ann_return / volatility if volatility != 0 else np.nan

    # Max drawdown
    roll_max = cum.cummax()
    drawdown = (cum - roll_max) / roll_max
    max_dd   = drawdown.min()

    return dict(ann_return=ann_return, volatility=volatility, sharpe=sharpe, max_dd=max_dd)


def _run_portfolio_hypothesis(
    df: pd.DataFrame,
    index_df: pd.DataFrame,
    quartile_col: str,
    supply_col: str,
    label: str,
    out_file: str,
) -> None:
    print(f"\n[{label}] Building monthly-rebalanced Q1/Q4 portfolios...")

    # Add year-month for monthly rebalancing
    df_h = df.copy()
    df_h["ym"] = df_h["snapshot_date"].dt.to_period("M")

    # For each snapshot, compute FORWARD_PRDS-period forward return per token
    # Forward return = price at T+FORWARD_PRDS / price at T - 1
    price_pivot = df_h.pivot_table(
        index="snapshot_date", columns="symbol", values="price", aggfunc="last"
    )
    fwd_returns = price_pivot.shift(-FORWARD_PRDS) / price_pivot - 1
    fwd_returns = fwd_returns.clip(-RETURN_CAP, RETURN_CAP)
    fwd_long = fwd_returns.stack().reset_index()
    fwd_long.columns = ["snapshot_date", "symbol", "fwd_return"]

    # First snapshot of each month = rebalancing date
    first_snap = (
        df_h.groupby("ym")["snapshot_date"].min().reset_index(name="snapshot_date")
    )
    rebal_dates = set(first_snap["snapshot_date"])

    # Join quartile labels to forward returns
    q_df = df_h[["snapshot_date", "symbol", quartile_col]].copy()
    q_df = q_df[q_df[quartile_col].notna()]
    fwd_long = fwd_long.merge(q_df, on=["snapshot_date", "symbol"], how="left")
    fwd_long = fwd_long[fwd_long["snapshot_date"].isin(rebal_dates)]

    # Equal-weight portfolio returns per rebalancing date
    q1_rets = []
    q4_rets = []
    dates_used = []

    for date in sorted(rebal_dates):
        slice_ = fwd_long[fwd_long["snapshot_date"] == date]
        q1 = slice_[slice_[quartile_col] == 1]["fwd_return"].dropna()
        q4 = slice_[slice_[quartile_col] == 4]["fwd_return"].dropna()
        if len(q1) == 0 or len(q4) == 0:
            continue
        q1_rets.append(q1.mean())
        q4_rets.append(q4.mean())
        dates_used.append(date)

    if not dates_used:
        print(f"[{label}] No rebalancing dates with sufficient data. Skipping.")
        return

    q1_series = pd.Series(q1_rets, index=pd.DatetimeIndex(dates_used), name="Q1 (Low Inflation)")
    q4_series = pd.Series(q4_rets, index=pd.DatetimeIndex(dates_used), name="Q4 (High Inflation)")

    # Index returns for the same dates
    idx_map = index_df.set_index("snapshot_date")["index_return"]
    # Sum index returns over FORWARD_PRDS following each rebalance date
    # Approximation: use the index return at the rebalance date as a 1-period proxy,
    # or accumulate the next FORWARD_PRDS periods of index returns.
    all_idx_dates = sorted(index_df["snapshot_date"].unique())
    idx_fwd = []
    for d in dates_used:
        pos = pd.Index(all_idx_dates).searchsorted(d)
        window_rets = []
        for k in range(FORWARD_PRDS):
            if pos + k < len(all_idx_dates):
                d2 = all_idx_dates[pos + k]
                r  = idx_map.get(d2, np.nan)
                if not np.isnan(r):
                    window_rets.append(r)
        idx_fwd.append(np.prod([1 + r for r in window_rets]) - 1 if window_rets else np.nan)

    idx_series = pd.Series(idx_fwd, index=pd.DatetimeIndex(dates_used), name="Broad Market Index")

    # Stats
    stats_dict = {}
    for name, s in [("Q1 (Low)", q1_series), ("Q4 (High)", q4_series), ("Index", idx_series)]:
        stats_dict[name] = _portfolio_stats(s)

    print(f"\n[{label}] Performance Summary:")
    print(f"  {'Portfolio':<20} {'Ann.Return':>12} {'Volatility':>12} {'Sharpe':>10} {'MaxDD':>10}")
    for name, s in stats_dict.items():
        print(f"  {name:<20} {s['ann_return']:>11.2%} {s['volatility']:>11.2%} "
              f"{s['sharpe']:>10.3f} {s['max_dd']:>10.2%}")

    # Cumulative return curves
    def cum_curve(s):
        s = s.dropna()
        return (1 + s).cumprod()

    fig, ax = plt.subplots(figsize=(10, 5))
    for s, color, lw in [
        (q1_series, "steelblue", 1.5),
        (q4_series, "crimson", 1.5),
        (idx_series, "gray", 1.0),
    ]:
        c = cum_curve(s)
        ax.plot(c.index, c.values, label=s.name, color=color, lw=lw)

    ax.axhline(1, color="black", lw=0.6, ls="--")
    ax.set_xlabel("Rebalance Date")
    ax.set_ylabel("Cumulative Return (1 = start)")
    ax.set_title(f"{label}: High vs Low Supply Inflation Portfolios\n"
                 f"Metric: {supply_col}, forward window: {FORWARD_PRDS} periods")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_file, dpi=150)
    plt.close(fig)
    print(f"[{label}] Saved: {out_file}")


def run_h2_h3(df: pd.DataFrame, index_df: pd.DataFrame) -> None:
    _run_portfolio_hypothesis(
        df, index_df,
        quartile_col="quartile_13p",
        supply_col="supply_pct_13p",
        label="H2",
        out_file=OUTPUT_DIR + "h2_continuous_pressure_90d.png",
    )
    _run_portfolio_hypothesis(
        df, index_df,
        quartile_col="quartile_52p",
        supply_col="supply_pct_52p",
        label="H3",
        out_file=OUTPUT_DIR + "h3_continuous_pressure_365d.png",
    )


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    print("=" * 60)
    print("Cryptocurrency Token Unlock Backtesting Script")
    print("NOTE: Survivorship bias not corrected — tokens exiting")
    print("      top 300 have no subsequent data.")
    print("=" * 60)

    df = load_data(INPUT_FILE)
    index_df = build_index(df)
    df = engineer_features(df)
    run_h1_event_study(df, index_df)
    run_h2_h3(df, index_df)

    print("\nDone.")


if __name__ == "__main__":
    main()
