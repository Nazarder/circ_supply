"""
backtest_v3.py — V3 Cryptocurrency Token Unlock Backtesting Script

Inherits all V2 improvements and adds:
  8. Regime-conditional L/S for H2/H3:
       - Bear-Only    : long Q1 / short Q4 only in Bear markets; 0% (cash) in Bull
       - Bull-Reverse : long Q4 / short Q1 only in Bull markets; 0% (cash) in Bear
       - Regime-Switch: Bear -> long Q1/short Q4; Bull -> long Q4/short Q1

The core finding from V2 that motivates V3:
  - The unconditional L/S went bankrupt because Q4 (high supply inflation)
    outperforms Q1 (low supply inflation) in bull markets, destroying the short leg.
  - H1 regime breakdown showed supply-dilution ACAR is negative only in Bear markets
    (ACAR Bear=-2.49%, Bull=+6.33%).
  - V3 tests whether regime-gating the H2/H3 L/S recovers profitability.
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
INPUT_FILE           = "D:/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
OUTPUT_DIR           = "D:/circ_supply/"
INDEX_TOP_N          = 100
EVENT_WINDOW         = 2
COOLDOWN_PRDS        = 4
ZSCORE_THRESH        = 3.0
ROLLING_WINDOW       = 12
REGIME_MA_WINDOW     = 20
VOL_WINDOW           = 12
MIN_VOL              = 0.01
FORWARD_PRDS         = 4
SLIPPAGE_K           = 0.0005
MIN_TURNOVER         = 0.001
MAX_SLIPPAGE         = 0.02
SUPPLY_WINDOW_SHORT  = 4
SUPPLY_WINDOW_MEDIUM = 13
SUPPLY_WINDOW_LONG   = 52
FFILL_LIMIT          = 1


# ===========================================================================
# STEP 1 — Data Loading & Preprocessing  [unchanged from V2]
# ===========================================================================

def _winsorize_cross_section(g):
    lo = g.quantile(0.01)
    hi = g.quantile(0.99)
    return g.clip(lower=lo, upper=hi)


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
    df["pct_return"] = (
        df.groupby("symbol")["price"]
        .transform(lambda s: s.pct_change(1))
    )
    df["pct_return"] = (
        df.groupby("snapshot_date")["pct_return"]
        .transform(_winsorize_cross_section)
    )
    df["period_idx"] = df.groupby("symbol").cumcount()
    print(f"[Data] Shape after load & filter: {df.shape}")
    print(f"[Data] Date range: {df['snapshot_date'].min().date()} to {df['snapshot_date'].max().date()}")
    print(f"[Data] Unique symbols: {df['symbol'].nunique()}")
    return df


# ===========================================================================
# STEP 2 — Broad Market Index  [unchanged from V2]
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
# STEP 3 — Regime Detection  [unchanged from V2]
# ===========================================================================

def compute_regime(index_df: pd.DataFrame) -> pd.DataFrame:
    idx = index_df.sort_values("snapshot_date").copy()
    idx["index_price"] = (1 + idx["index_return"]).cumprod()
    idx["index_ma20"] = idx["index_price"].rolling(REGIME_MA_WINDOW, min_periods=1).mean()
    idx["regime"] = np.where(idx["index_price"] >= idx["index_ma20"], "Bull", "Bear")
    return idx[["snapshot_date", "index_price", "index_ma20", "regime"]]


# ===========================================================================
# STEP 4 — Feature Engineering  [unchanged from V2]
# ===========================================================================

def _apply_cooldown(flag_series: pd.Series, cooldown: int) -> pd.Series:
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
    df["supply_pct_1p"]  = grp["circulating_supply"].transform(lambda s: s.pct_change(1))
    df["supply_pct_4p"]  = grp["circulating_supply"].transform(lambda s: s.pct_change(SUPPLY_WINDOW_SHORT))
    df["supply_pct_13p"] = grp["circulating_supply"].transform(lambda s: s.pct_change(SUPPLY_WINDOW_MEDIUM))
    df["supply_pct_52p"] = grp["circulating_supply"].transform(lambda s: s.pct_change(SUPPLY_WINDOW_LONG))
    df["supply_roll_mean"] = grp["supply_pct_1p"].transform(
        lambda s: s.rolling(ROLLING_WINDOW, min_periods=4).mean()
    )
    df["supply_roll_std"] = grp["supply_pct_1p"].transform(
        lambda s: s.rolling(ROLLING_WINDOW, min_periods=4).std()
    )
    df["supply_zscore"] = (
        (df["supply_pct_1p"] - df["supply_roll_mean"])
        / df["supply_roll_std"].clip(lower=1e-8)
    )
    min_history = 2 * EVENT_WINDOW
    df["raw_unlock"] = (
        df["supply_zscore"].gt(ZSCORE_THRESH)
        & df["supply_pct_1p"].gt(0)
        & df["supply_roll_std"].gt(0)
        & df["period_idx"].ge(min_history)
        & df["supply_pct_1p"].notna()
        & df["supply_roll_mean"].notna()
    )
    df["is_unlock_event"] = (
        df.groupby("symbol")["raw_unlock"]
        .transform(lambda s: _apply_cooldown(s, COOLDOWN_PRDS))
    )
    df.drop(columns=["raw_unlock"], inplace=True)
    df["turnover"] = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"] = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)

    def _safe_qcut_quartile(s):
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
    print(f"[Features] Z-score unlock events flagged: {df['is_unlock_event'].sum()}")
    return df


# ===========================================================================
# STEP 5 — Beta Helper  [unchanged from V2]
# ===========================================================================

def _compute_beta(token_rets: np.ndarray, index_rets: np.ndarray) -> float:
    if len(token_rets) < 4:
        return 1.0
    cov_mat = np.cov(token_rets, index_rets)
    var_idx = cov_mat[1, 1]
    if var_idx < 1e-12:
        return 1.0
    return float(cov_mat[0, 1] / var_idx)


# ===========================================================================
# STEP 6 — Portfolio Stats  [unchanged from V2]
# ===========================================================================

def _portfolio_stats(returns: pd.Series) -> dict:
    returns = returns.dropna()
    if len(returns) < 2:
        return dict(ann_return=np.nan, volatility=np.nan, sharpe=np.nan, max_dd=np.nan)
    cum = (1 + returns).cumprod()
    total_days = (returns.index[-1] - returns.index[0]).days
    total_years = max(total_days / 365.25, 1 / 52)
    cum_final = float(cum.iloc[-1])
    if cum_final <= 0:
        ann_return = np.nan
    else:
        ann_return = cum_final ** (1.0 / total_years) - 1
    gaps = np.diff(returns.index).astype("timedelta64[D]").astype(float)
    median_gap = float(np.median(gaps)) if len(gaps) > 0 else 7.0
    periods_per_year = 365.25 / max(median_gap, 1.0)
    volatility = returns.std() * np.sqrt(periods_per_year)
    sharpe = ann_return / volatility if (volatility > 0 and not np.isnan(ann_return)) else np.nan
    roll_max = cum.cummax()
    max_dd = ((cum - roll_max) / roll_max).min()
    return dict(ann_return=ann_return, volatility=volatility, sharpe=sharpe, max_dd=max_dd)


# ===========================================================================
# STEP 7 — H1 Beta-Hedged Event Study  [unchanged from V2]
# ===========================================================================

def run_h1_event_study(
    df: pd.DataFrame,
    index_df: pd.DataFrame,
    regime_df: pd.DataFrame,
) -> None:
    print("\n[H1] Running beta-hedged event study on Z-score unlock events...")

    idx_map = index_df.set_index("snapshot_date")["index_return"].to_dict()
    regime_map = regime_df.set_index("snapshot_date")["regime"].to_dict()

    events = df[df["is_unlock_event"]].copy()
    if events.empty:
        print("[H1] No unlock events found. Skipping.")
        return

    df_dedup = df.drop_duplicates(subset=["symbol", "snapshot_date"], keep="last")
    symbol_dates = {
        sym: grp["snapshot_date"].sort_values().reset_index(drop=True)
        for sym, grp in df_dedup.groupby("symbol")
    }
    symbol_returns = {
        sym: grp.set_index("snapshot_date")["pct_return"]
        for sym, grp in df_dedup.groupby("symbol")
    }
    symbol_slippage = {
        sym: grp.set_index("snapshot_date")["slippage"]
        for sym, grp in df_dedup.groupby("symbol")
    }

    window = range(-EVENT_WINDOW, EVENT_WINDOW + 1)
    all_car = []
    regimes = []

    for _, row in events.iterrows():
        sym  = row["symbol"]
        t0   = row["snapshot_date"]
        dates  = symbol_dates[sym]
        ret_s  = symbol_returns[sym]
        slip_s = symbol_slippage[sym]

        t0_pos = dates.searchsorted(t0)
        if t0_pos >= len(dates) or dates.iloc[t0_pos] != t0:
            continue

        beta_start = max(0, t0_pos - ROLLING_WINDOW)
        beta_tok, beta_idx = [], []
        for bp in range(beta_start, t0_pos):
            d = dates.iloc[bp]
            tr = ret_s.get(d, np.nan)
            ir = idx_map.get(d, np.nan)
            if not (pd.isna(tr) or pd.isna(ir)):
                beta_tok.append(float(tr))
                beta_idx.append(float(ir))
        beta = _compute_beta(np.array(beta_tok), np.array(beta_idx))

        slip_t0 = float(slip_s.get(t0, 0.0))
        half_slip = slip_t0 / 2.0

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
            if pd.isna(tok_ret) or pd.isna(idx_ret):
                valid = False
                break
            ar = float(tok_ret) - beta * float(idx_ret)
            if offset == 0:
                ar -= half_slip
            abnormal_returns.append(ar)

        if valid and len(abnormal_returns) == len(window):
            all_car.append(np.cumsum(abnormal_returns))
            regimes.append(regime_map.get(t0, "Unknown"))

    if not all_car:
        print("[H1] Not enough complete event windows. Skipping plot.")
        return

    car_matrix = np.array(all_car)
    n = car_matrix.shape[0]
    acar = car_matrix.mean(axis=0)
    ci   = 1.96 * car_matrix.std(axis=0) / np.sqrt(n)

    print(f"[H1] Z-score events flagged:       {df['is_unlock_event'].sum()}")
    print(f"[H1] Events with complete windows: {n}")

    x_ticks = list(window)
    labels  = [f"T={x:+d}" for x in x_ticks]
    print("[H1] Beta-hedged ACAR trajectory (T-2 to T+2):")
    print("     " + "  ".join(f"{l}: {v:.2%}" for l, v in zip(labels, acar)))

    final_post = car_matrix[:, EVENT_WINDOW + EVENT_WINDOW]
    t_stat, p_val = stats.ttest_1samp(final_post, 0)
    print(f"[H1] t-test on post-event ACAR: t={t_stat:.4f}, p={p_val:.4f}, n={n}")

    regimes_arr = np.array(regimes)
    bull_mask = regimes_arr == "Bull"
    bear_mask = regimes_arr == "Bear"
    n_bull, n_bear = bull_mask.sum(), bear_mask.sum()
    acar_bull = car_matrix[bull_mask].mean(axis=0) if n_bull > 0 else np.full(len(window), np.nan)
    acar_bear = car_matrix[bear_mask].mean(axis=0) if n_bear > 0 else np.full(len(window), np.nan)
    print("[H1] Regime breakdown:")
    print(f"     BULL events: {n_bull}, ACAR at T+{EVENT_WINDOW}: {acar_bull[-1]:.2%}")
    print(f"     BEAR events: {n_bear}, ACAR at T+{EVENT_WINDOW}: {acar_bear[-1]:.2%}")

    # Chart 1: Overall
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(x_ticks, acar, color="steelblue", lw=2, label="ACAR (beta-hedged)")
    ax.fill_between(x_ticks, acar - ci, acar + ci, alpha=0.25, color="steelblue", label="95% CI")
    ax.axhline(0, color="black", lw=0.8, ls="--")
    ax.axvline(0, color="red", lw=0.8, ls="--", label="Event (T=0)")
    ax.set_xlabel("Periods relative to unlock event")
    ax.set_ylabel("Cumulative Abnormal Return (beta-hedged)")
    ax.set_title(f"H1 V3: Event Study — Z-score Supply Unlocks\nn={n}, Z>{ZSCORE_THRESH:.1f}, +-{EVENT_WINDOW} periods")
    ax.legend()
    fig.tight_layout()
    out1 = OUTPUT_DIR + "v3_h1_event_study.png"
    fig.savefig(out1, dpi=150)
    plt.close(fig)
    print(f"[H1] Saved: {out1}")

    # Chart 2: Bull vs Bear
    fig, ax = plt.subplots(figsize=(8, 5))
    if n_bull > 0:
        ci_bull = 1.96 * car_matrix[bull_mask].std(axis=0) / np.sqrt(n_bull)
        ax.plot(x_ticks, acar_bull, color="steelblue", lw=2, label=f"Bull (n={n_bull})")
        ax.fill_between(x_ticks, acar_bull - ci_bull, acar_bull + ci_bull, alpha=0.20, color="steelblue")
    if n_bear > 0:
        ci_bear = 1.96 * car_matrix[bear_mask].std(axis=0) / np.sqrt(n_bear)
        ax.plot(x_ticks, acar_bear, color="crimson", lw=2, label=f"Bear (n={n_bear})")
        ax.fill_between(x_ticks, acar_bear - ci_bear, acar_bear + ci_bear, alpha=0.20, color="crimson")
    ax.axhline(0, color="black", lw=0.8, ls="--")
    ax.axvline(0, color="black", lw=0.8, ls=":", alpha=0.6)
    ax.set_xlabel("Periods relative to unlock event")
    ax.set_ylabel("ACAR (beta-hedged)")
    ax.set_title(f"H1 V3: Bull vs Bear Regime — Supply Unlock ACAR\nZ>{ZSCORE_THRESH:.1f}, +-{EVENT_WINDOW} periods")
    ax.legend()
    fig.tight_layout()
    out2 = OUTPUT_DIR + "v3_h1_bull_bear.png"
    fig.savefig(out2, dpi=150)
    plt.close(fig)
    print(f"[H1] Saved: {out2}")


# ===========================================================================
# STEP 8 — H2/H3 Regime-Conditional L/S  [V3 core upgrade]
# ===========================================================================

def run_h2_h3_longshort(
    df: pd.DataFrame,
    index_df: pd.DataFrame,
    regime_df: pd.DataFrame,
) -> None:
    print("\n[H2/H3] Building regime-conditional L/S portfolios...")

    regime_map = regime_df.set_index("snapshot_date")["regime"].to_dict()

    all_results = {}
    for cfg in [
        dict(quartile_col="quartile_13p", supply_col="supply_pct_13p",
             label="H2", out_file=OUTPUT_DIR + "v3_h2_regime_ls.png"),
        dict(quartile_col="quartile_52p", supply_col="supply_pct_52p",
             label="H3", out_file=OUTPUT_DIR + "v3_h3_regime_ls.png"),
    ]:
        result = _run_ls_hypothesis(df, index_df, regime_map, **cfg)
        if result is not None:
            all_results[cfg["label"]] = result

    # Combined summary table
    def _fmt(v):
        return f"{v:.2%}" if (v is not None and not np.isnan(v)) else "    N/A"
    def _fmtf(v):
        return f"{v:.3f}" if (v is not None and not np.isnan(v)) else "    N/A"

    print("\n[H2/H3] Regime-Conditional Portfolio Performance:")
    print(f"  {'Portfolio':<22} {'Ann.Return':>12} {'Volatility':>12} {'Sharpe':>10} {'MaxDD':>10}")

    row_order = [
        ("Unconditional", "unconditional"),
        ("Bear-Only",     "bear_only"),
        ("Bull-Reverse",  "bull_reverse"),
        ("Regime-Switch", "regime_switch"),
        ("Index",         "index"),
    ]

    for label in ["H2", "H3"]:
        if label not in all_results:
            continue
        res = all_results[label]
        print(f"  --- {label} ---")
        for name, key in row_order:
            s = _portfolio_stats(res[key])
            tag = f"{name} ({label})"
            print(
                f"  {tag:<22} {_fmt(s['ann_return']):>12} {_fmt(s['volatility']):>12} "
                f"{_fmtf(s['sharpe']):>10} {_fmt(s['max_dd']):>10}"
            )


def _run_ls_hypothesis(
    df: pd.DataFrame,
    index_df: pd.DataFrame,
    regime_map: dict,
    quartile_col: str,
    supply_col: str,
    label: str,
    out_file: str,
):
    df_h = df.copy()
    df_h["ym"] = df_h["snapshot_date"].dt.to_period("M")

    # Forward returns
    price_pivot = df_h.pivot_table(
        index="snapshot_date", columns="symbol", values="price", aggfunc="last"
    )
    slip_pivot = df_h.pivot_table(
        index="snapshot_date", columns="symbol", values="slippage", aggfunc="last"
    )
    fwd_returns = price_pivot.shift(-FORWARD_PRDS) / price_pivot - 1
    fwd_long = fwd_returns.stack().reset_index()
    fwd_long.columns = ["snapshot_date", "symbol", "fwd_return_raw"]

    fwd_long["fwd_return_raw"] = (
        fwd_long.groupby("snapshot_date")["fwd_return_raw"]
        .transform(_winsorize_cross_section)
    )
    fwd_long["fwd_return_raw"] = fwd_long["fwd_return_raw"].clip(lower=-1.0)

    slip_long = slip_pivot.stack().reset_index()
    slip_long.columns = ["snapshot_date", "symbol", "slippage"]
    fwd_long = fwd_long.merge(slip_long, on=["snapshot_date", "symbol"], how="left")
    fwd_long["slippage"] = fwd_long["slippage"].fillna(MAX_SLIPPAGE)
    fwd_long["fwd_return"] = fwd_long["fwd_return_raw"] - fwd_long["slippage"]

    # Trailing volatility
    vol_pivot = (
        df_h.pivot_table(index="snapshot_date", columns="symbol", values="pct_return", aggfunc="last")
        .rolling(VOL_WINDOW, min_periods=4)
        .std()
    )
    vol_long = vol_pivot.stack().reset_index()
    vol_long.columns = ["snapshot_date", "symbol", "trailing_vol"]
    fwd_long = fwd_long.merge(vol_long, on=["snapshot_date", "symbol"], how="left")
    fwd_long["trailing_vol"] = fwd_long["trailing_vol"].fillna(MIN_VOL).clip(lower=MIN_VOL)

    # Rebalancing dates
    rebal_dates = set(
        df_h.groupby("ym")["snapshot_date"].min().reset_index(name="snapshot_date")["snapshot_date"]
    )

    # Quartile labels
    q_df = df_h[["snapshot_date", "symbol", quartile_col]].copy()
    q_df = q_df[q_df[quartile_col].notna()]
    fwd_long = fwd_long.merge(q_df, on=["snapshot_date", "symbol"], how="left")
    fwd_long = fwd_long[fwd_long["snapshot_date"].isin(rebal_dates)]

    # Accumulators for 4 strategy variants
    unconditional_rets = []
    bear_only_rets     = []
    bull_reverse_rets  = []
    switch_rets        = []
    dates_used         = []
    regime_used        = []

    for date in sorted(rebal_dates):
        sl = fwd_long[fwd_long["snapshot_date"] == date]
        q1 = sl[sl[quartile_col] == 1].copy().dropna(subset=["fwd_return", "trailing_vol"])
        q4 = sl[sl[quartile_col] == 4].copy().dropna(subset=["fwd_return", "trailing_vol"])

        if len(q1) < 2 or len(q4) < 2:
            continue

        # Inverse-vol weights
        q1["inv_vol"] = 1.0 / q1["trailing_vol"]
        q4["inv_vol"] = 1.0 / q4["trailing_vol"]
        w1 = q1["inv_vol"] / q1["inv_vol"].sum()
        w4 = q4["inv_vol"] / q4["inv_vol"].sum()

        long_ret  = (w1 * q1["fwd_return"]).sum()   # return of Q1 (low inflation)
        short_ret = (w4 * q4["fwd_return"]).sum()   # return of Q4 (high inflation)
        ls_raw    = long_ret - short_ret             # standard L/S (long Q1, short Q4)

        regime = regime_map.get(date, "Bear")

        # ---- Regime-conditional variants ----
        # Unconditional: always trade standard L/S
        unc = ls_raw

        # Bear-Only: trade L/S only in Bear; hold cash (0%) in Bull
        bear = ls_raw if regime == "Bear" else 0.0

        # Bull-Reverse: long Q4 / short Q1 only in Bull; cash in Bear
        # (momentum trade: buy the high-emission outperformers, short the laggards)
        bull_rev = (-ls_raw) if regime == "Bull" else 0.0

        # Regime-Switch: standard L/S in Bear, reversed in Bull
        switch = ls_raw if regime == "Bear" else (-ls_raw)

        # Clip all at -1.0 (forced liquidation floor)
        unconditional_rets.append(max(unc,      -1.0))
        bear_only_rets.append(    max(bear,     -1.0))
        bull_reverse_rets.append( max(bull_rev, -1.0))
        switch_rets.append(       max(switch,   -1.0))
        dates_used.append(date)
        regime_used.append(regime)

    if not dates_used:
        print(f"[{label}] No rebalancing dates with sufficient data. Skipping.")
        return None

    idx_dt = pd.DatetimeIndex(dates_used)

    def _make_series(rets, name):
        return pd.Series(rets, index=idx_dt, name=name)

    unconditional = _make_series(unconditional_rets, "Unconditional L/S")
    bear_only     = _make_series(bear_only_rets,     "Bear-Only L/S")
    bull_reverse  = _make_series(bull_reverse_rets,  "Bull-Reverse L/S")
    regime_switch = _make_series(switch_rets,         "Regime-Switch L/S")

    # Index forward returns
    idx_ret_map = index_df.set_index("snapshot_date")["index_return"]
    all_idx_dates = sorted(index_df["snapshot_date"].unique())
    idx_fwd = []
    for d in dates_used:
        pos = pd.Index(all_idx_dates).searchsorted(d)
        window_rets = []
        for k in range(FORWARD_PRDS):
            if pos + k < len(all_idx_dates):
                r = idx_ret_map.get(all_idx_dates[pos + k], np.nan)
                if not pd.isna(r):
                    window_rets.append(float(r))
        idx_fwd.append(np.prod([1 + r for r in window_rets]) - 1 if window_rets else np.nan)
    index_series = _make_series(idx_fwd, "Index")

    # Regime counts
    regimes_arr = np.array(regime_used)
    n_bear_periods = (regimes_arr == "Bear").sum()
    n_bull_periods = (regimes_arr == "Bull").sum()
    print(f"[{label}] Rebalancing dates: {len(dates_used)} "
          f"(Bear={n_bear_periods}, Bull={n_bull_periods})")

    # Collect into dict for caller
    result = dict(
        unconditional=unconditional,
        bear_only=bear_only,
        bull_reverse=bull_reverse,
        regime_switch=regime_switch,
        index=index_series,
    )

    # ---- Chart ----
    style = [
        ("unconditional", "dimgray",    1.2, "--", "Unconditional L/S"),
        ("bear_only",     "steelblue",  2.0, "-",  "Bear-Only L/S"),
        ("bull_reverse",  "darkorange", 2.0, "-",  "Bull-Reverse L/S"),
        ("regime_switch", "mediumseagreen", 2.0, "-", "Regime-Switch L/S"),
        ("index",         "black",      1.0, "-",  "Cap-weighted Index"),
    ]

    fig, ax = plt.subplots(figsize=(11, 6))
    for key, color, lw, ls, lbl in style:
        s = result[key].dropna()
        cum = (1 + s).cumprod()
        ax.plot(cum.index, cum.values, color=color, lw=lw, ls=ls, label=lbl)

    # Shade Bear periods
    regime_ser = pd.Series(regime_used, index=idx_dt)
    in_bear = False
    bear_start = None
    for dt, reg in regime_ser.items():
        if reg == "Bear" and not in_bear:
            bear_start = dt
            in_bear = True
        elif reg != "Bear" and in_bear:
            ax.axvspan(bear_start, dt, alpha=0.08, color="crimson", label=None)
            in_bear = False
    if in_bear:
        ax.axvspan(bear_start, regime_ser.index[-1], alpha=0.08, color="crimson")
    # One legend entry for shading
    from matplotlib.patches import Patch
    bear_patch = Patch(facecolor="crimson", alpha=0.15, label="Bear regime")

    handles, lbls = ax.get_legend_handles_labels()
    ax.legend(handles + [bear_patch], lbls + ["Bear regime"], fontsize=8)
    ax.axhline(1, color="black", lw=0.5, ls="--")
    ax.set_xlabel("Rebalance Date")
    ax.set_ylabel("Cumulative Return (1 = start)")
    ax.set_title(
        f"{label} V3: Regime-Conditional L/S vs Index\n"
        f"Metric: {supply_col}, inv-vol weights, {FORWARD_PRDS}-period hold, slippage adj."
    )
    fig.tight_layout()
    fig.savefig(out_file, dpi=150)
    plt.close(fig)
    print(f"[{label}] Saved: {out_file}")

    return result


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    print("=" * 60)
    print("Cryptocurrency Token Unlock Backtesting V3")
    print("Regime-Conditional L/S for H2/H3")
    print("NOTE: Survivorship bias not corrected.")
    print("=" * 60)

    df = load_data(INPUT_FILE)
    index_df = build_index(df)
    regime_df = compute_regime(index_df)
    df = engineer_features(df)
    run_h1_event_study(df, index_df, regime_df)
    run_h2_h3_longshort(df, index_df, regime_df)

    print("\nDone.")


if __name__ == "__main__":
    main()
