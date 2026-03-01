"""
delta_neutral_strategies.py — Delta-Neutral Strategies Using Long-Side Supply-Inflation Alpha

Three market-neutral approaches that extract proven long-side alpha (P10 low-inflation
basket) while hedging market exposure, avoiding toxic concentrated shorts in
high-convexity tokens.

Strategy 1: Alpha-Hedged Long Basket
    Long P10 low-inflation tokens, short BTC/Index at trailing beta.

Strategy 2: Long P10 vs Short P40-P60 (Mid-Inflation Neutral)
    Avoid explosive top decile; short the boring middle instead.

Strategy 3: Inflation-Tilted Market-Neutral Book
    Full universe with inflation-based tilts, constrained to zero net exposure.
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from collections import OrderedDict

# ---------------------------------------------------------------------------
# CONFIG (matching existing codebase)
# ---------------------------------------------------------------------------
INPUT_FILE     = "C:/Users/Lenovo/AppData/Local/Temp/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
OUTPUT_DIR     = "C:/Users/Lenovo/AppData/Local/Temp/circ_supply/"
FFILL_LIMIT    = 1
INDEX_TOP_N    = 100
REGIME_MA      = 20
VOL_WINDOW     = 12
MIN_VOL        = 0.01
SLIPPAGE_K     = 0.0005
MIN_TURNOVER   = 0.001
MAX_SLIPPAGE   = 0.02
TOP_N_EXCLUDE  = 20

SUPPLY_WINDOW  = 13      # weeks for trailing supply inflation
HOLD_PRIMARY   = 4       # weeks (primary holding period)
HOLD_SENSITIVITY = 13    # weeks (sensitivity check)
BETA_WINDOW    = 12      # trailing periods for beta estimation
BETA_CLAMP_LO  = 0.5
BETA_CLAMP_HI  = 3.0
TILT_STRENGTH  = 0.03    # Strategy 3 tilt coefficient
MAX_WEIGHT     = 0.05    # Strategy 3 max individual weight (±5%)

WINSOR_LO      = 0.01
WINSOR_HI      = 0.99

# Exclusion sets
STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "GUSD", "FRAX",
    "LUSD", "MIM", "USDN", "USTC", "UST", "HUSD", "SUSD", "PAX",
    "USDS", "USDJ", "NUSD", "USDK", "USDX", "CUSD", "CEUR", "USDH",
    "USDD", "FDUSD", "PYUSD", "EURC", "EURS", "USDQ", "USDB", "USDTB",
    "SFRXETH", "OSETH", "CMETH",
}
CEX_TOKENS = {
    "BNB", "HT", "KCS", "OKB", "MX", "CRO", "BIX", "GT", "LEO", "FTT",
    "WBT", "BGB", "BTSE", "NEXO", "CEL", "LATOKEN", "BTMX",
}
MEMECOINS = {
    "DOGE", "SHIB", "FLOKI", "PEPE", "BONK", "WIF", "FARTCOIN",
    "SAFEMOON", "ELON", "DOGELON", "MEME", "TURBO", "POPCAT", "MOG",
    "BABYDOGE", "KISHU", "AKITA", "HOGE", "SAITAMA", "VOLT", "ELONGATE",
    "SAMO", "BOME", "NEIRO", "SPX", "BRETT", "MYRO", "SLERF", "TOSHI",
    "GIGA", "SUNDOG", "MOODENG", "PNUT", "ACT", "GOAT", "CHILLGUY",
    "PONKE", "LADYS", "COQ", "AIDOGE", "WOJAK", "HUHU", "MILADY",
    "BOBO", "QUACK", "BONE", "LEASH", "FLOOF", "PITBULL", "HOKK",
    "CATGIRL", "SFM", "LUNC",
}
FULL_EXCLUSIONS = STABLECOINS | CEX_TOKENS | MEMECOINS


# ===========================================================================
# DATA LOADING (from backtest_alternatives.py)
# ===========================================================================

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

    df["turnover"] = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"] = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)

    print(f"[Data] Rows: {len(df):,}  Symbols: {df['symbol'].nunique():,}  "
          f"Dates: {df['snapshot_date'].min().date()} to {df['snapshot_date'].max().date()}")
    return df


# ===========================================================================
# INDEX & REGIME
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
    return index_df


def compute_regime(index_df: pd.DataFrame) -> pd.Series:
    idx = index_df.sort_values("snapshot_date").copy()
    idx["index_price"] = (1 + idx["index_return"]).cumprod()
    idx["ma"] = idx["index_price"].rolling(REGIME_MA, min_periods=1).mean()
    idx["regime"] = np.where(idx["index_price"] >= idx["ma"], "Bull", "Bear")
    return idx.set_index("snapshot_date")["regime"]


# ===========================================================================
# PORTFOLIO STATS
# ===========================================================================

def _portfolio_stats(returns: pd.Series) -> dict:
    returns = returns.dropna()
    if len(returns) < 2:
        return dict(ann_return=np.nan, volatility=np.nan, sharpe=np.nan,
                    max_dd=np.nan, win_rate=np.nan, n_periods=0)

    cum = (1 + returns).cumprod()
    total_days = (returns.index[-1] - returns.index[0]).days
    total_years = max(total_days / 365.25, 1 / 52)
    cum_final = float(cum.iloc[-1])
    ann_return = cum_final ** (1.0 / total_years) - 1 if cum_final > 0 else np.nan

    gaps = np.diff(returns.index).astype("timedelta64[D]").astype(float)
    median_gap = float(np.median(gaps)) if len(gaps) > 0 else 7.0
    periods_per_yr = 365.25 / max(median_gap, 1.0)
    volatility = returns.std() * np.sqrt(periods_per_yr)

    sharpe = ann_return / volatility if (volatility > 0 and not np.isnan(ann_return)) else np.nan

    roll_max = cum.cummax()
    max_dd = ((cum - roll_max) / roll_max).min()

    win_rate = (returns > 0).mean()

    return dict(ann_return=ann_return, volatility=volatility, sharpe=sharpe,
                max_dd=max_dd, win_rate=win_rate, n_periods=len(returns))


# ===========================================================================
# PRECOMPUTE PIVOTS
# ===========================================================================

def precompute_pivots(df: pd.DataFrame) -> dict:
    price_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol", values="price", aggfunc="last"
    )
    slip_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol", values="slippage", aggfunc="last"
    )
    cap_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol", values="market_cap", aggfunc="last"
    )
    rank_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol", values="rank", aggfunc="last"
    )

    supply_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol",
        values="circulating_supply", aggfunc="last"
    )
    supply_pct_13 = supply_pivot.pct_change(SUPPLY_WINDOW)

    # Monthly rebalancing dates
    df_tmp = df[["snapshot_date"]].copy()
    df_tmp["ym"] = df_tmp["snapshot_date"].dt.to_period("M")
    rebal_dates = sorted(df_tmp.groupby("ym")["snapshot_date"].min())

    return dict(
        price=price_pivot,
        slip=slip_pivot,
        cap=cap_pivot,
        rank=rank_pivot,
        supply_pct_13=supply_pct_13,
        rebal_dates=rebal_dates,
    )


# ===========================================================================
# FORWARD RETURNS
# ===========================================================================

def compute_forward_returns(price_pivot, hold_periods):
    fwd_raw = price_pivot.shift(-hold_periods) / price_pivot - 1

    # Cross-sectional winsorization per snapshot date
    def _winsor_row(row):
        valid = row.dropna()
        if len(valid) < 4:
            return row
        lo = valid.quantile(WINSOR_LO)
        hi = valid.quantile(WINSOR_HI)
        return row.clip(lower=lo, upper=hi)

    fwd_raw = fwd_raw.apply(_winsor_row, axis=1)
    fwd_raw = fwd_raw.clip(lower=-1.0)
    return fwd_raw


# ===========================================================================
# EXCLUSION FILTER HELPER
# ===========================================================================

def apply_exclusion_filter(inf_row, rank_pivot, date):
    """Filter out stablecoins, CEX tokens, memecoins, and top-N mega-caps."""
    inf_row = inf_row[~inf_row.index.isin(FULL_EXCLUSIONS)]
    if date in rank_pivot.index:
        ranks_at_date = rank_pivot.loc[date].dropna()
        mega_caps = ranks_at_date[ranks_at_date <= TOP_N_EXCLUDE].index
        inf_row = inf_row[~inf_row.index.isin(mega_caps)]
    return inf_row


# ===========================================================================
# STRATEGY 1: ALPHA-HEDGED LONG BASKET
# ===========================================================================

def run_strategy1(pivots, index_df, hold_periods):
    """
    Long P10 low-inflation tokens, short BTC or cap-weighted index at trailing beta.

    Returns dict with keys: 'btc_hedge', 'index_hedge' — each containing
    sub-dicts with 'long', 'hedge', 'net' return series.
    """
    price_pivot = pivots["price"]
    slip_pivot = pivots["slip"]
    rank_pivot = pivots["rank"]
    supply_pct = pivots["supply_pct_13"]
    rebal_dates = pivots["rebal_dates"]

    fwd_returns = compute_forward_returns(price_pivot, hold_periods)

    # Build hedge return series: BTC and cap-weighted index
    # BTC forward returns
    btc_fwd = fwd_returns["BTC"] if "BTC" in fwd_returns.columns else None

    # Index forward returns (cap-weighted top-100)
    idx = index_df.sort_values("snapshot_date").set_index("snapshot_date")
    # Compute N-period compounded index return for each rebal date
    idx_ret_1p = idx["index_return"]

    results = {}

    for hedge_name, use_btc in [("btc_hedge", True), ("index_hedge", False)]:
        long_rets = []
        hedge_rets = []
        net_rets = []
        dates_used = []

        # Accumulate historical long and hedge returns for beta estimation
        hist_long = []
        hist_hedge = []

        for date in rebal_dates:
            if date not in supply_pct.index or date not in fwd_returns.index:
                continue

            # Get supply inflation and apply exclusions
            inf_row = supply_pct.loc[date].dropna()
            inf_row = apply_exclusion_filter(inf_row, rank_pivot, date)

            if len(inf_row) < 10:
                continue

            # Forward returns for eligible tokens
            fwd_row = fwd_returns.loc[date].reindex(inf_row.index).dropna()
            inf_row = inf_row.reindex(fwd_row.index)
            if len(inf_row) < 10:
                continue

            # Select long basket: bottom 10th percentile of inflation
            lo_cut = inf_row.quantile(0.10)
            long_mask = inf_row <= lo_cut
            long_syms = inf_row[long_mask].index
            if len(long_syms) < 2:
                continue

            long_fwd = fwd_row[long_syms]

            # Subtract slippage from long leg
            if date in slip_pivot.index:
                slip_row = slip_pivot.loc[date]
                long_fwd = long_fwd - slip_row.reindex(long_fwd.index).fillna(MAX_SLIPPAGE)
            long_fwd = long_fwd.clip(lower=-1.0)

            long_ret = long_fwd.mean()

            # Hedge return
            if use_btc:
                if btc_fwd is None or date not in btc_fwd.index or pd.isna(btc_fwd.loc[date]):
                    continue
                hedge_ret = btc_fwd.loc[date]
                # Subtract slippage for BTC hedge
                if date in slip_pivot.index and "BTC" in slip_pivot.columns:
                    btc_slip = slip_pivot.loc[date, "BTC"]
                    if not pd.isna(btc_slip):
                        hedge_ret = hedge_ret - btc_slip
            else:
                # Compute index forward return by compounding single-period returns
                end_date_candidates = idx_ret_1p.index[idx_ret_1p.index > date]
                if len(end_date_candidates) < hold_periods:
                    continue
                future_idx_rets = idx_ret_1p.loc[
                    end_date_candidates[:hold_periods]
                ]
                hedge_ret = (1 + future_idx_rets).prod() - 1
                # Apply slippage estimate for index (use median slippage across top-100)
                if date in slip_pivot.index:
                    top_ranks = rank_pivot.loc[date].dropna() if date in rank_pivot.index else pd.Series(dtype=float)
                    top_syms = top_ranks[top_ranks <= INDEX_TOP_N].index
                    if len(top_syms) > 0:
                        idx_slip = slip_pivot.loc[date].reindex(top_syms).median()
                        if not pd.isna(idx_slip):
                            hedge_ret = hedge_ret - idx_slip

            hedge_ret = max(hedge_ret, -1.0)

            hist_long.append(long_ret)
            hist_hedge.append(hedge_ret)

            # Estimate trailing beta
            if len(hist_long) < 3:
                beta = 1.0  # default before enough history
            else:
                window = min(BETA_WINDOW, len(hist_long))
                y = np.array(hist_long[-window:])
                x = np.array(hist_hedge[-window:])
                x_dm = x - x.mean()
                denom = (x_dm ** 2).sum()
                if denom > 1e-12:
                    beta = float((x_dm * (y - y.mean())).sum() / denom)
                else:
                    beta = 1.0
                beta = np.clip(beta, BETA_CLAMP_LO, BETA_CLAMP_HI)

            # Net return: long basket minus beta-sized hedge
            net_ret = max(long_ret - beta * hedge_ret, -1.0)

            long_rets.append(long_ret)
            hedge_rets.append(hedge_ret)
            net_rets.append(net_ret)
            dates_used.append(date)

        if not dates_used:
            results[hedge_name] = None
            continue

        idx_dt = pd.DatetimeIndex(dates_used)
        results[hedge_name] = {
            "long": pd.Series(long_rets, index=idx_dt, name="Long P10"),
            "hedge": pd.Series(hedge_rets, index=idx_dt, name="Hedge"),
            "net": pd.Series(net_rets, index=idx_dt, name="Net"),
        }

    return results


# ===========================================================================
# STRATEGY 2: LONG P10 vs SHORT P40-P60
# ===========================================================================

def run_strategy2(pivots, hold_periods):
    """
    Long bottom 10th percentile, short 40th-60th percentile (mid-inflation).
    Equal-weighted, full exclusions.
    """
    price_pivot = pivots["price"]
    slip_pivot = pivots["slip"]
    rank_pivot = pivots["rank"]
    supply_pct = pivots["supply_pct_13"]
    rebal_dates = pivots["rebal_dates"]

    fwd_returns = compute_forward_returns(price_pivot, hold_periods)

    long_rets = []
    short_rets = []
    net_rets = []
    dates_used = []

    for date in rebal_dates:
        if date not in supply_pct.index or date not in fwd_returns.index:
            continue

        inf_row = supply_pct.loc[date].dropna()
        inf_row = apply_exclusion_filter(inf_row, rank_pivot, date)

        if len(inf_row) < 10:
            continue

        fwd_row = fwd_returns.loc[date].reindex(inf_row.index).dropna()
        inf_row = inf_row.reindex(fwd_row.index)
        if len(inf_row) < 10:
            continue

        # Long: bottom 10th percentile (low inflation)
        lo_cut = inf_row.quantile(0.10)
        long_mask = inf_row <= lo_cut

        # Short: 40th-60th percentile (mid inflation)
        mid_lo = inf_row.quantile(0.40)
        mid_hi = inf_row.quantile(0.60)
        short_mask = (inf_row >= mid_lo) & (inf_row <= mid_hi)

        long_syms = inf_row[long_mask].index
        short_syms = inf_row[short_mask].index

        if len(long_syms) < 2 or len(short_syms) < 2:
            continue

        long_fwd = fwd_row[long_syms]
        short_fwd = fwd_row[short_syms]

        # Subtract slippage from both legs
        if date in slip_pivot.index:
            slip_row = slip_pivot.loc[date]
            long_fwd = long_fwd - slip_row.reindex(long_fwd.index).fillna(MAX_SLIPPAGE)
            short_fwd = short_fwd - slip_row.reindex(short_fwd.index).fillna(MAX_SLIPPAGE)
        long_fwd = long_fwd.clip(lower=-1.0)
        short_fwd = short_fwd.clip(lower=-1.0)

        long_ret = long_fwd.mean()
        short_ret = short_fwd.mean()

        # L/S: long low-inflation, short mid-inflation
        # Short leg slippage is already subtracted from short_fwd (which reduces short return),
        # so shorting tokens with lower returns = better for us.
        # Net = long_ret - short_ret (we profit from short when short_ret is negative)
        net_ret = max(long_ret - short_ret, -1.0)

        long_rets.append(long_ret)
        short_rets.append(short_ret)
        net_rets.append(net_ret)
        dates_used.append(date)

    if not dates_used:
        return None

    idx = pd.DatetimeIndex(dates_used)
    return {
        "long": pd.Series(long_rets, index=idx, name="Long P10"),
        "short": pd.Series(short_rets, index=idx, name="Short P40-P60"),
        "net": pd.Series(net_rets, index=idx, name="Net L/S"),
    }


# ===========================================================================
# STRATEGY 3: INFLATION-TILTED MARKET-NEUTRAL BOOK
# ===========================================================================

def run_strategy3(pivots, hold_periods):
    """
    Hold the full universe with inflation-based tilts, constrained to zero net exposure.
    w_i = (1/N) + tilt * z_i, then demean to sum=0, cap at ±5%.
    """
    price_pivot = pivots["price"]
    slip_pivot = pivots["slip"]
    rank_pivot = pivots["rank"]
    supply_pct = pivots["supply_pct_13"]
    rebal_dates = pivots["rebal_dates"]

    fwd_returns = compute_forward_returns(price_pivot, hold_periods)

    net_rets = []
    long_leg_rets = []
    short_leg_rets = []
    dates_used = []
    weight_sums = []  # for verification

    for date in rebal_dates:
        if date not in supply_pct.index or date not in fwd_returns.index:
            continue

        inf_row = supply_pct.loc[date].dropna()
        inf_row = apply_exclusion_filter(inf_row, rank_pivot, date)

        if len(inf_row) < 10:
            continue

        fwd_row = fwd_returns.loc[date].reindex(inf_row.index).dropna()
        inf_row = inf_row.reindex(fwd_row.index)
        if len(inf_row) < 10:
            continue

        N = len(inf_row)

        # Standardized negative inflation rank: lowest inflation → highest z
        # Rank ascending: lowest inflation gets rank 1 → after negation, highest z
        ranks = inf_row.rank(method="average")
        z = -(ranks - ranks.mean()) / ranks.std()

        # Raw weights: 1/N + tilt * z_i
        w = pd.Series(1.0 / N, index=inf_row.index) + TILT_STRENGTH * z

        # Demean to make dollar-neutral (sum = 0)
        w = w - w.mean()

        # Cap at ±MAX_WEIGHT, iteratively re-normalize
        for _ in range(10):
            w = w.clip(lower=-MAX_WEIGHT, upper=MAX_WEIGHT)
            excess = w.sum()
            if abs(excess) < 1e-10:
                break
            w = w - excess / len(w)

        weight_sums.append(w.sum())

        # Subtract slippage from forward returns
        fwd_adj = fwd_row.copy()
        if date in slip_pivot.index:
            slip_row = slip_pivot.loc[date]
            fwd_adj = fwd_adj - slip_row.reindex(fwd_adj.index).fillna(MAX_SLIPPAGE)
        fwd_adj = fwd_adj.clip(lower=-1.0)

        # Portfolio return = sum(w_i * r_i)
        common = w.index.intersection(fwd_adj.index)
        w_c = w.loc[common]
        r_c = fwd_adj.loc[common]

        net_ret = (w_c * r_c).sum()

        # Decompose into long leg and short leg for diagnosis
        long_mask = w_c > 0
        short_mask = w_c < 0
        if long_mask.any():
            long_leg_ret = (w_c[long_mask] * r_c[long_mask]).sum() / w_c[long_mask].sum()
        else:
            long_leg_ret = 0.0
        if short_mask.any():
            short_leg_ret = (w_c[short_mask] * r_c[short_mask]).sum() / abs(w_c[short_mask].sum())
        else:
            short_leg_ret = 0.0

        net_rets.append(net_ret)
        long_leg_rets.append(long_leg_ret)
        short_leg_rets.append(short_leg_ret)
        dates_used.append(date)

    if not dates_used:
        return None

    idx = pd.DatetimeIndex(dates_used)
    # Verify weights sum to zero
    max_wsum_deviation = max(abs(s) for s in weight_sums)
    print(f"  [S3 Verification] Max weight-sum deviation from zero: {max_wsum_deviation:.2e}")

    return {
        "long": pd.Series(long_leg_rets, index=idx, name="Long Leg"),
        "short": pd.Series(short_leg_rets, index=idx, name="Short Leg"),
        "net": pd.Series(net_rets, index=idx, name="Net"),
        "weight_sums": weight_sums,
    }


# ===========================================================================
# SUMMARY TABLE
# ===========================================================================

def format_summary_table(all_stats: OrderedDict) -> str:
    lines = []
    header = (
        f"  {'Strategy':<45} {'AnnRet':>9} {'Vol':>9} {'Sharpe':>8} "
        f"{'MaxDD':>9} {'WinRate':>8} {'N':>5}"
    )
    sep = "  " + "-" * (len(header.strip()))

    def _fmt(v):
        return f"{v:.2%}" if (v is not None and not np.isnan(v)) else "    N/A"
    def _fmtf(v):
        return f"{v:.3f}" if (v is not None and not np.isnan(v)) else "   N/A"

    lines.append(sep)
    lines.append(header)
    lines.append(sep)

    prev_group = None
    for name, stats in all_stats.items():
        group = name.split(":")[0].strip() if ":" in name else name
        if group != prev_group and prev_group is not None:
            lines.append(sep)
        prev_group = group

        lines.append(
            f"  {name:<45} {_fmt(stats['ann_return']):>9} {_fmt(stats['volatility']):>9} "
            f"{_fmtf(stats['sharpe']):>8} {_fmt(stats['max_dd']):>9} "
            f"{_fmt(stats['win_rate']):>8} {stats['n_periods']:>5}"
        )

    lines.append(sep)
    return "\n".join(lines)


# ===========================================================================
# REGIME BREAKDOWN
# ===========================================================================

def regime_breakdown(returns: pd.Series, regime_series: pd.Series, label: str):
    """Print performance stats broken down by Bull/Bear regime."""
    aligned = returns.copy()
    regimes = regime_series.reindex(aligned.index, method="ffill")

    print(f"\n  Regime Breakdown — {label}")
    for regime in ["Bull", "Bear"]:
        mask = regimes == regime
        sub = aligned[mask]
        if len(sub.dropna()) < 2:
            print(f"    {regime}: insufficient data")
            continue
        st = _portfolio_stats(sub)
        print(f"    {regime}: AnnRet={st['ann_return']:.2%}, Vol={st['volatility']:.2%}, "
              f"Sharpe={st['sharpe']:.3f}, MaxDD={st['max_dd']:.2%}, N={st['n_periods']}")


# ===========================================================================
# CHARTS
# ===========================================================================

def plot_all_strategies(strategy_nets: dict, hold_label: str):
    """Cumulative return chart for all strategies on one plot."""
    fig, ax = plt.subplots(figsize=(13, 6))
    colors = ["steelblue", "crimson", "darkorange", "mediumseagreen", "purple"]

    for i, (name, series) in enumerate(strategy_nets.items()):
        if series is None:
            continue
        cum = (1 + series.dropna()).cumprod()
        st = _portfolio_stats(series)
        sh = f"{st['sharpe']:.3f}" if not np.isnan(st.get('sharpe', np.nan)) else "N/A"
        ax.plot(cum.index, cum.values, color=colors[i % len(colors)], lw=2,
                label=f"{name} (Sharpe={sh})")

    ax.axhline(1, color="black", lw=0.6, ls="--")
    ax.set_xlabel("Rebalance Date")
    ax.set_ylabel("Cumulative Return (1 = start)")
    ax.set_title(f"Delta-Neutral Strategies — Cumulative Returns ({hold_label})")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out = OUTPUT_DIR + f"delta_neutral_all_{hold_label}.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"[Chart] All strategies cumulative: {out}")


def plot_strategy_legs(strat_result: dict, name: str, hold_label: str):
    """Per-strategy chart showing long leg, short/hedge leg, and net."""
    if strat_result is None:
        return

    fig, ax = plt.subplots(figsize=(12, 6))

    leg_names = {"long": "Long Leg", "short": "Short Leg", "hedge": "Hedge Leg", "net": "Net L/S"}
    colors = {"long": "steelblue", "short": "crimson", "hedge": "darkorange", "net": "mediumseagreen"}

    for key in ["long", "short", "hedge", "net"]:
        if key not in strat_result or strat_result[key] is None:
            continue
        if key == "weight_sums":
            continue
        s = strat_result[key]
        cum = (1 + s.dropna()).cumprod()
        ax.plot(cum.index, cum.values, color=colors.get(key, "gray"), lw=2,
                label=leg_names.get(key, key))

    ax.axhline(1, color="black", lw=0.6, ls="--")
    ax.set_xlabel("Rebalance Date")
    ax.set_ylabel("Cumulative Return (1 = start)")
    ax.set_title(f"{name} — Leg Decomposition ({hold_label})")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    safe_name = name.replace(" ", "_").replace("/", "_").replace(":", "")
    out = OUTPUT_DIR + f"delta_neutral_{safe_name}_{hold_label}.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"[Chart] {name} legs: {out}")


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    print("=" * 75)
    print("Delta-Neutral Strategies Using Long-Side Supply-Inflation Alpha")
    print("=" * 75)

    # Load data and precompute
    df = load_data(INPUT_FILE)
    index_df = build_index(df)
    regime_series = compute_regime(index_df)

    print("[Precompute] Building pivot tables...")
    pivots = precompute_pivots(df)
    print(f"[Precompute] Rebalancing dates: {len(pivots['rebal_dates'])}")

    # Run all strategies for both holding periods
    for hold_periods, hold_label in [(HOLD_PRIMARY, "4w"), (HOLD_SENSITIVITY, "13w")]:
        print(f"\n{'='*75}")
        print(f"HOLDING PERIOD: {hold_label} ({hold_periods} weeks)")
        print(f"{'='*75}")

        all_stats = OrderedDict()
        all_nets = OrderedDict()
        all_results = {}

        # ------ Strategy 1: Alpha-Hedged Long Basket ------
        print(f"\n--- Strategy 1: Alpha-Hedged Long Basket ({hold_label}) ---")
        s1 = run_strategy1(pivots, index_df, hold_periods)

        for variant, label in [("btc_hedge", "S1A: Long P10 / Short BTC"),
                                ("index_hedge", "S1B: Long P10 / Short Index")]:
            res = s1[variant]
            if res is None:
                all_stats[label] = dict(ann_return=np.nan, volatility=np.nan, sharpe=np.nan,
                                        max_dd=np.nan, win_rate=np.nan, n_periods=0)
                continue

            # Net stats
            st = _portfolio_stats(res["net"])
            all_stats[label] = st
            all_nets[label] = res["net"]
            all_results[label] = res

            # Long-leg standalone
            st_long = _portfolio_stats(res["long"])
            all_stats[f"  {label} (long leg only)"] = st_long

            # Hedge standalone
            st_hedge = _portfolio_stats(res["hedge"])
            all_stats[f"  {label} (hedge leg only)"] = st_hedge

            print(f"  {label}: AnnRet={st['ann_return']:.2%}, Sharpe={st['sharpe']:.3f}, "
                  f"MaxDD={st['max_dd']:.2%}, N={st['n_periods']}")

        # ------ Strategy 2: Long P10 vs Short P40-P60 ------
        print(f"\n--- Strategy 2: Long P10 vs Short P40-P60 ({hold_label}) ---")
        s2 = run_strategy2(pivots, hold_periods)
        label2 = "S2: Long P10 / Short P40-P60"

        if s2 is None:
            all_stats[label2] = dict(ann_return=np.nan, volatility=np.nan, sharpe=np.nan,
                                     max_dd=np.nan, win_rate=np.nan, n_periods=0)
        else:
            st = _portfolio_stats(s2["net"])
            all_stats[label2] = st
            all_nets[label2] = s2["net"]
            all_results[label2] = s2

            st_long = _portfolio_stats(s2["long"])
            all_stats[f"  {label2} (long leg only)"] = st_long

            st_short = _portfolio_stats(s2["short"])
            all_stats[f"  {label2} (short leg only)"] = st_short

            print(f"  {label2}: AnnRet={st['ann_return']:.2%}, Sharpe={st['sharpe']:.3f}, "
                  f"MaxDD={st['max_dd']:.2%}, N={st['n_periods']}")

        # ------ Strategy 3: Inflation-Tilted Market-Neutral ------
        print(f"\n--- Strategy 3: Inflation-Tilted Market-Neutral ({hold_label}) ---")
        s3 = run_strategy3(pivots, hold_periods)
        label3 = "S3: Inflation-Tilted Neutral"

        if s3 is None:
            all_stats[label3] = dict(ann_return=np.nan, volatility=np.nan, sharpe=np.nan,
                                     max_dd=np.nan, win_rate=np.nan, n_periods=0)
        else:
            st = _portfolio_stats(s3["net"])
            all_stats[label3] = st
            all_nets[label3] = s3["net"]
            all_results[label3] = s3

            st_long = _portfolio_stats(s3["long"])
            all_stats[f"  {label3} (long leg contrib)"] = st_long

            st_short = _portfolio_stats(s3["short"])
            all_stats[f"  {label3} (short leg contrib)"] = st_short

            print(f"  {label3}: AnnRet={st['ann_return']:.2%}, Sharpe={st['sharpe']:.3f}, "
                  f"MaxDD={st['max_dd']:.2%}, N={st['n_periods']}")

        # ------ Summary Table ------
        print(f"\n{'='*75}")
        print(f"SUMMARY TABLE — All Delta-Neutral Strategies ({hold_label})")
        print(f"{'='*75}")
        print(format_summary_table(all_stats))

        # ------ Regime Breakdown ------
        print(f"\n{'='*75}")
        print(f"REGIME BREAKDOWN ({hold_label})")
        print(f"{'='*75}")
        for name, series in all_nets.items():
            if series is not None:
                regime_breakdown(series, regime_series, name)

        # ------ Charts ------
        print(f"\n{'='*75}")
        print(f"CHARTS ({hold_label})")
        print(f"{'='*75}")

        # All strategies on one plot
        plot_all_strategies(all_nets, hold_label)

        # Per-strategy leg decomposition
        for name, res in all_results.items():
            plot_strategy_legs(res, name, hold_label)

    # ------ Cross-holding-period comparison ------
    print(f"\n{'='*75}")
    print("ANALYSIS COMPLETE")
    print(f"{'='*75}")
    print("\nKey questions answered:")
    print("  1. Does beta-hedging with BTC or Index preserve the long-side alpha?")
    print("  2. Is shorting mid-inflation tokens (P40-P60) more survivable than P90?")
    print("  3. Does spreading tilts across the full universe reduce convexity risk?")
    print("  4. Which approach best preserves the ~+15% ann long-side alpha?")
    print("\nDone.")


if __name__ == "__main__":
    main()
