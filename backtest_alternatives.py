"""
backtest_alternatives.py — Alternative Backtest Methodology

Expose Short-Leg Blind Spots by systematically varying:
  1. Winsorization level (None, 1/99, 0.5/99.5)
  2. Holding period (4w, 8w, 13w, 26w)
  3. Selection granularity (Quartile 25/75, Decile 10/90, Vigintile 5/95)
  4. Weighting (Equal-weight, Inverse-vol)
  5. Exclusion filters (None, Full)
  6. Supply lookback (13w, 26w)
  7. Regime filter (None, Bull-only, Bear-only)

Tests A-F isolate each methodological choice rather than sweeping all combinations.
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from collections import OrderedDict

# ---------------------------------------------------------------------------
# CONFIG
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

# Exclusion sets (from extreme_percentile.py)
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
# DATA LOADING (reused from V2)
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

    # Single-period returns (unwinsorized — winsorization applied later per config)
    df["pct_return"] = (
        df.groupby("symbol")["price"]
        .transform(lambda s: s.pct_change(1))
    )

    # Slippage model
    df["turnover"] = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"] = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)

    print(f"[Data] Rows: {len(df):,}  Symbols: {df['symbol'].nunique():,}  "
          f"Dates: {df['snapshot_date'].min().date()} to {df['snapshot_date'].max().date()}")
    return df


# ===========================================================================
# INDEX & REGIME (reused from V2)
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
# PORTFOLIO STATS (reused from V2)
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
# PRECOMPUTE PIVOTS (avoids redundant pivot_table calls)
# ===========================================================================

def precompute_pivots(df: pd.DataFrame) -> dict:
    """Build all pivot tables once; individual tests slice from these."""
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
    vol_pivot = (
        df.pivot_table(
            index="snapshot_date", columns="symbol", values="pct_return", aggfunc="last"
        )
        .rolling(VOL_WINDOW, min_periods=4)
        .std()
    )

    # Supply inflation for multiple lookback windows
    supply_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol",
        values="circulating_supply", aggfunc="last"
    )
    supply_pct_13 = supply_pivot.pct_change(13)
    supply_pct_26 = supply_pivot.pct_change(26)

    # Monthly rebalancing dates
    df_tmp = df[["snapshot_date"]].copy()
    df_tmp["ym"] = df_tmp["snapshot_date"].dt.to_period("M")
    rebal_dates = sorted(df_tmp.groupby("ym")["snapshot_date"].min())

    return dict(
        price=price_pivot,
        slip=slip_pivot,
        cap=cap_pivot,
        rank=rank_pivot,
        vol=vol_pivot,
        supply_pct_13=supply_pct_13,
        supply_pct_26=supply_pct_26,
        rebal_dates=rebal_dates,
    )


# ===========================================================================
# FORWARD RETURNS WITH CONFIGURABLE WINSORIZATION
# ===========================================================================

def compute_forward_returns(price_pivot, hold_periods, winsor_lo, winsor_hi):
    """
    Compute N-period forward returns with optional cross-sectional winsorization.

    winsor_lo/winsor_hi: quantile bounds (e.g. 0.01/0.99). None = no winsorization.
    """
    fwd_raw = price_pivot.shift(-hold_periods) / price_pivot - 1

    if winsor_lo is not None and winsor_hi is not None:
        # Cross-sectional winsorization per snapshot date
        def _winsor_row(row):
            valid = row.dropna()
            if len(valid) < 4:
                return row
            lo = valid.quantile(winsor_lo)
            hi = valid.quantile(winsor_hi)
            return row.clip(lower=lo, upper=hi)

        fwd_raw = fwd_raw.apply(_winsor_row, axis=1)

    # Floor at -1.0 (can't lose more than 100%)
    fwd_raw = fwd_raw.clip(lower=-1.0)
    return fwd_raw


# ===========================================================================
# CORE L/S ENGINE
# ===========================================================================

def run_ls_config(
    pivots: dict,
    regime_series: pd.Series,
    *,
    hold_periods: int = 4,
    supply_lookback: int = 13,
    winsor_lo=0.01,
    winsor_hi=0.99,
    selection_lo=0.25,    # long basket: bottom X% of inflation
    selection_hi=0.75,    # short basket: top X% of inflation
    weighting="equal",    # "equal" or "inv_vol"
    apply_exclusions=False,
    regime_filter=None,   # None, "Bull", "Bear"
):
    """
    Run a single L/S configuration and return the L/S return series.

    Long: tokens in bottom `selection_lo` percentile of supply inflation (low dilution)
    Short: tokens in top `selection_hi` percentile of supply inflation (high dilution)
    L/S return = mean(long basket fwd) - mean(short basket fwd)
    """
    price_pivot = pivots["price"]
    slip_pivot = pivots["slip"]
    vol_pivot = pivots["vol"]
    rank_pivot = pivots["rank"]
    rebal_dates = pivots["rebal_dates"]

    # Select supply lookback
    if supply_lookback == 13:
        supply_pct = pivots["supply_pct_13"]
    elif supply_lookback == 26:
        supply_pct = pivots["supply_pct_26"]
    else:
        raise ValueError(f"Unsupported supply_lookback: {supply_lookback}")

    # Forward returns with configured winsorization
    fwd_returns = compute_forward_returns(price_pivot, hold_periods, winsor_lo, winsor_hi)

    ls_rets = []
    dates_used = []

    for date in rebal_dates:
        if date not in supply_pct.index or date not in fwd_returns.index:
            continue

        # Regime filter
        if regime_filter is not None:
            regime_at_date = regime_series.get(date, None)
            if regime_at_date != regime_filter:
                continue

        # Get supply inflation for this date
        inf_row = supply_pct.loc[date].dropna()

        # Apply exclusion filters
        if apply_exclusions:
            inf_row = inf_row[~inf_row.index.isin(FULL_EXCLUSIONS)]
            # Exclude top-N mega-caps
            if date in rank_pivot.index:
                ranks_at_date = rank_pivot.loc[date].dropna()
                mega_caps = ranks_at_date[ranks_at_date <= TOP_N_EXCLUDE].index
                inf_row = inf_row[~inf_row.index.isin(mega_caps)]

        if len(inf_row) < 10:
            continue

        # Forward returns available for these tokens
        fwd_row = fwd_returns.loc[date].reindex(inf_row.index).dropna()
        inf_row = inf_row.reindex(fwd_row.index)

        if len(inf_row) < 10:
            continue

        # Select baskets
        lo_cut = inf_row.quantile(selection_lo)
        hi_cut = inf_row.quantile(selection_hi)

        long_mask = inf_row <= lo_cut     # low inflation = long
        short_mask = inf_row >= hi_cut    # high inflation = short

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

        # Weighting
        if weighting == "inv_vol":
            if date in vol_pivot.index:
                vol_row = vol_pivot.loc[date]
                long_vol = vol_row.reindex(long_fwd.index).fillna(MIN_VOL).clip(lower=MIN_VOL)
                short_vol = vol_row.reindex(short_fwd.index).fillna(MIN_VOL).clip(lower=MIN_VOL)
            else:
                long_vol = pd.Series(MIN_VOL, index=long_fwd.index)
                short_vol = pd.Series(MIN_VOL, index=short_fwd.index)

            long_w = (1.0 / long_vol) / (1.0 / long_vol).sum()
            short_w = (1.0 / short_vol) / (1.0 / short_vol).sum()

            long_ret = (long_w * long_fwd).sum()
            short_ret = (short_w * short_fwd).sum()
        else:
            # Equal weight
            long_ret = long_fwd.mean()
            short_ret = short_fwd.mean()

        # L/S: long low-inflation, short high-inflation
        ls_return = max(long_ret - short_ret, -1.0)
        ls_rets.append(ls_return)
        dates_used.append(date)

    if not dates_used:
        return None

    return pd.Series(ls_rets, index=pd.DatetimeIndex(dates_used), name="L/S")


# ===========================================================================
# TEST CONFIGURATIONS
# ===========================================================================

def define_tests() -> OrderedDict:
    """Define all test configurations A-F as described in the plan."""
    tests = OrderedDict()

    # --- Test A: Winsorization effect ---
    # Decile L/S, 4w hold, equal-weight, full exclusions, 13w supply
    base_A = dict(
        hold_periods=4, supply_lookback=13,
        selection_lo=0.10, selection_hi=0.90,
        weighting="equal", apply_exclusions=True,
    )
    tests["A1: No winsor"] = {**base_A, "winsor_lo": None, "winsor_hi": None}
    tests["A2: 1/99 pct"]  = {**base_A, "winsor_lo": 0.01, "winsor_hi": 0.99}
    tests["A3: 0.5/99.5"]  = {**base_A, "winsor_lo": 0.005, "winsor_hi": 0.995}

    # --- Test B: Holding period sensitivity ---
    # Decile L/S, equal-weight, full exclusions, no winsorization, 13w supply
    base_B = dict(
        supply_lookback=13, winsor_lo=None, winsor_hi=None,
        selection_lo=0.10, selection_hi=0.90,
        weighting="equal", apply_exclusions=True,
    )
    tests["B1: 4w hold"]  = {**base_B, "hold_periods": 4}
    tests["B2: 8w hold"]  = {**base_B, "hold_periods": 8}
    tests["B3: 13w hold"] = {**base_B, "hold_periods": 13}
    tests["B4: 26w hold"] = {**base_B, "hold_periods": 26}

    # --- Test C: Selection granularity ---
    # 13w hold, equal-weight, full exclusions, no winsorization, 13w supply
    base_C = dict(
        hold_periods=13, supply_lookback=13,
        winsor_lo=None, winsor_hi=None,
        weighting="equal", apply_exclusions=True,
    )
    tests["C1: Quartile (25/75)"]  = {**base_C, "selection_lo": 0.25, "selection_hi": 0.75}
    tests["C2: Decile (10/90)"]    = {**base_C, "selection_lo": 0.10, "selection_hi": 0.90}
    tests["C3: Vigintile (5/95)"]  = {**base_C, "selection_lo": 0.05, "selection_hi": 0.95}

    # --- Test D: Weighting effect ---
    # Decile L/S, 13w hold, full exclusions, no winsorization, 13w supply
    base_D = dict(
        hold_periods=13, supply_lookback=13,
        winsor_lo=None, winsor_hi=None,
        selection_lo=0.10, selection_hi=0.90,
        apply_exclusions=True,
    )
    tests["D1: Equal-wt"]  = {**base_D, "weighting": "equal"}
    tests["D2: Inv-vol"]   = {**base_D, "weighting": "inv_vol"}

    # --- Test E: Supply lookback ---
    # Decile L/S, 13w hold, equal-weight, full exclusions, no winsorization
    base_E = dict(
        hold_periods=13, winsor_lo=None, winsor_hi=None,
        selection_lo=0.10, selection_hi=0.90,
        weighting="equal", apply_exclusions=True,
    )
    tests["E1: 13w supply"] = {**base_E, "supply_lookback": 13}
    tests["E2: 26w supply"] = {**base_E, "supply_lookback": 26}

    return tests


# ===========================================================================
# RESULT TABLE FORMATTING
# ===========================================================================

def format_results_table(results: dict) -> str:
    """Format all test results into a readable table."""
    lines = []
    header = (
        f"  {'Config':<28} {'AnnRet':>9} {'Vol':>9} {'Sharpe':>8} "
        f"{'MaxDD':>9} {'WinRate':>8} {'N':>5}"
    )
    separator = "  " + "-" * len(header.strip())

    def _fmt(v):
        return f"{v:.2%}" if (v is not None and not np.isnan(v)) else "    N/A"
    def _fmtf(v):
        return f"{v:.3f}" if (v is not None and not np.isnan(v)) else "   N/A"

    current_test = None
    for name, stats in results.items():
        test_letter = name[0]
        if test_letter != current_test:
            current_test = test_letter
            lines.append(separator)
            test_labels = {
                "A": "Test A: Winsorization Effect",
                "B": "Test B: Holding Period Sensitivity",
                "C": "Test C: Selection Granularity",
                "D": "Test D: Weighting Effect",
                "E": "Test E: Supply Lookback",
                "F": "Test F: Best Config + Regime Filter",
            }
            lines.append(f"  === {test_labels.get(test_letter, 'Test ' + test_letter)} ===")
            lines.append(header)
            lines.append(separator)

        lines.append(
            f"  {name:<28} {_fmt(stats['ann_return']):>9} {_fmt(stats['volatility']):>9} "
            f"{_fmtf(stats['sharpe']):>8} {_fmt(stats['max_dd']):>9} "
            f"{_fmt(stats['win_rate']):>8} {stats['n_periods']:>5}"
        )

    lines.append(separator)
    return "\n".join(lines)


# ===========================================================================
# CHARTING: TOP 3 CONFIGURATIONS
# ===========================================================================

def plot_top_configs(results: dict, series_dict: dict) -> None:
    """Plot cumulative returns for the top 3 performing configurations by Sharpe."""
    # Rank by Sharpe ratio (descending), filter out NaN
    ranked = sorted(
        [(name, stats) for name, stats in results.items()
         if not np.isnan(stats.get("sharpe", np.nan))],
        key=lambda x: x[1]["sharpe"],
        reverse=True,
    )

    if len(ranked) == 0:
        print("[Chart] No valid configurations to plot.")
        return

    top_n = min(3, len(ranked))
    top_configs = ranked[:top_n]

    colors = ["steelblue", "crimson", "darkorange", "mediumseagreen"]
    fig, ax = plt.subplots(figsize=(12, 6))

    for i, (name, stats) in enumerate(top_configs):
        s = series_dict[name]
        cum = (1 + s.dropna()).cumprod()
        sharpe_str = f"{stats['sharpe']:.3f}"
        ax.plot(cum.index, cum.values, color=colors[i], lw=2,
                label=f"{name} (Sharpe={sharpe_str})")

    ax.axhline(1, color="black", lw=0.6, ls="--")
    ax.set_xlabel("Rebalance Date")
    ax.set_ylabel("Cumulative Return (1 = start)")
    ax.set_title("Top Performing L/S Configurations by Sharpe Ratio")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out = OUTPUT_DIR + "alt_backtest_top3.png"
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"[Chart] Top-3 cumulative plot saved: {out}")

    # Also plot all Test B (holding period) configs together
    b_configs = [(n, s) for n, s in series_dict.items() if n.startswith("B")]
    if len(b_configs) > 1:
        fig, ax = plt.subplots(figsize=(12, 6))
        for i, (name, s) in enumerate(b_configs):
            cum = (1 + s.dropna()).cumprod()
            st = results[name]
            sharpe_str = f"{st['sharpe']:.3f}" if not np.isnan(st.get("sharpe", np.nan)) else "N/A"
            ax.plot(cum.index, cum.values, color=colors[i % len(colors)], lw=2,
                    label=f"{name} (Sharpe={sharpe_str})")
        ax.axhline(1, color="black", lw=0.6, ls="--")
        ax.set_xlabel("Rebalance Date")
        ax.set_ylabel("Cumulative Return (1 = start)")
        ax.set_title("Test B: Holding Period Sensitivity — Cumulative L/S Returns")
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        out_b = OUTPUT_DIR + "alt_backtest_hold_period.png"
        fig.savefig(out_b, dpi=150)
        plt.close(fig)
        print(f"[Chart] Holding period comparison saved: {out_b}")

    # Plot all Test A (winsorization) configs together
    a_configs = [(n, s) for n, s in series_dict.items() if n.startswith("A")]
    if len(a_configs) > 1:
        fig, ax = plt.subplots(figsize=(12, 6))
        for i, (name, s) in enumerate(a_configs):
            cum = (1 + s.dropna()).cumprod()
            st = results[name]
            sharpe_str = f"{st['sharpe']:.3f}" if not np.isnan(st.get("sharpe", np.nan)) else "N/A"
            ax.plot(cum.index, cum.values, color=colors[i % len(colors)], lw=2,
                    label=f"{name} (Sharpe={sharpe_str})")
        ax.axhline(1, color="black", lw=0.6, ls="--")
        ax.set_xlabel("Rebalance Date")
        ax.set_ylabel("Cumulative Return (1 = start)")
        ax.set_title("Test A: Winsorization Effect — Cumulative L/S Returns")
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        out_a = OUTPUT_DIR + "alt_backtest_winsorization.png"
        fig.savefig(out_a, dpi=150)
        plt.close(fig)
        print(f"[Chart] Winsorization comparison saved: {out_a}")


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    print("=" * 70)
    print("Alternative Backtest Methodology — Expose Short-Leg Blind Spots")
    print("=" * 70)

    # Step 1: Load data and precompute
    df = load_data(INPUT_FILE)
    index_df = build_index(df)
    regime_series = compute_regime(index_df)

    print("[Precompute] Building pivot tables...")
    pivots = precompute_pivots(df)
    print(f"[Precompute] Rebalancing dates: {len(pivots['rebal_dates'])}")

    # Step 2: Define and run tests A-E
    tests = define_tests()
    results = OrderedDict()
    series_dict = OrderedDict()

    print(f"\n[Tests] Running {len(tests)} configurations (A-E)...")
    for name, cfg in tests.items():
        print(f"  Running: {name}...", end=" ", flush=True)
        ls_series = run_ls_config(pivots, regime_series, **cfg)
        if ls_series is not None and len(ls_series.dropna()) >= 2:
            stats = _portfolio_stats(ls_series)
            results[name] = stats
            series_dict[name] = ls_series
            sharpe_str = f"{stats['sharpe']:.3f}" if not np.isnan(stats.get('sharpe', np.nan)) else "N/A"
            print(f"OK (n={stats['n_periods']}, Sharpe={sharpe_str})")
        else:
            results[name] = dict(ann_return=np.nan, volatility=np.nan, sharpe=np.nan,
                                 max_dd=np.nan, win_rate=np.nan, n_periods=0)
            print("SKIP (insufficient data)")

    # Step 3: Test F — Best config + regime filter
    # Find best performing config from A-E by Sharpe
    valid_results = {k: v for k, v in results.items()
                     if not np.isnan(v.get("sharpe", np.nan))}

    if valid_results:
        best_name = max(valid_results, key=lambda k: valid_results[k]["sharpe"])
        best_cfg = tests[best_name]
        print(f"\n[Test F] Best config from A-E: {best_name} "
              f"(Sharpe={valid_results[best_name]['sharpe']:.3f})")
        print(f"[Test F] Adding regime filters to: {best_cfg}")

        for regime_label, regime_val in [("No filter", None), ("Bull only", "Bull"),
                                         ("Bear only", "Bear")]:
            fname = f"F: {best_name[:12]}+{regime_label}"
            print(f"  Running: {fname}...", end=" ", flush=True)
            ls_series = run_ls_config(pivots, regime_series,
                                      **best_cfg, regime_filter=regime_val)
            if ls_series is not None and len(ls_series.dropna()) >= 2:
                stats = _portfolio_stats(ls_series)
                results[fname] = stats
                series_dict[fname] = ls_series
                sharpe_str = f"{stats['sharpe']:.3f}" if not np.isnan(stats.get('sharpe', np.nan)) else "N/A"
                print(f"OK (n={stats['n_periods']}, Sharpe={sharpe_str})")
            else:
                results[fname] = dict(ann_return=np.nan, volatility=np.nan, sharpe=np.nan,
                                      max_dd=np.nan, win_rate=np.nan, n_periods=0)
                print("SKIP (insufficient data)")

    # Step 4: Print summary table
    print("\n" + "=" * 70)
    print("SUMMARY TABLE — All Test Configurations")
    print("=" * 70)
    print(format_results_table(results))

    # Step 5: Highlight which methodological choices matter most
    print("\n" + "=" * 70)
    print("KEY FINDINGS")
    print("=" * 70)

    # Winsorization impact (A)
    a_results = {k: v for k, v in results.items() if k.startswith("A")}
    if len(a_results) >= 2:
        a_sharpes = {k: v["sharpe"] for k, v in a_results.items() if not np.isnan(v.get("sharpe", np.nan))}
        if a_sharpes:
            best_a = max(a_sharpes, key=a_sharpes.get)
            worst_a = min(a_sharpes, key=a_sharpes.get)
            print(f"\n  [Winsorization] Best: {best_a} (Sharpe={a_sharpes[best_a]:.3f}), "
                  f"Worst: {worst_a} (Sharpe={a_sharpes[worst_a]:.3f})")
            print(f"  -> Winsorization Sharpe spread: {a_sharpes[best_a] - a_sharpes[worst_a]:.3f}")

    # Holding period impact (B)
    b_results = {k: v for k, v in results.items() if k.startswith("B")}
    if len(b_results) >= 2:
        b_sharpes = {k: v["sharpe"] for k, v in b_results.items() if not np.isnan(v.get("sharpe", np.nan))}
        if b_sharpes:
            best_b = max(b_sharpes, key=b_sharpes.get)
            worst_b = min(b_sharpes, key=b_sharpes.get)
            print(f"\n  [Hold Period] Best: {best_b} (Sharpe={b_sharpes[best_b]:.3f}), "
                  f"Worst: {worst_b} (Sharpe={b_sharpes[worst_b]:.3f})")
            print(f"  -> Holding period Sharpe spread: {b_sharpes[best_b] - b_sharpes[worst_b]:.3f}")

    # Selection granularity impact (C)
    c_results = {k: v for k, v in results.items() if k.startswith("C")}
    if len(c_results) >= 2:
        c_sharpes = {k: v["sharpe"] for k, v in c_results.items() if not np.isnan(v.get("sharpe", np.nan))}
        if c_sharpes:
            best_c = max(c_sharpes, key=c_sharpes.get)
            print(f"\n  [Granularity] Best: {best_c} (Sharpe={c_sharpes[best_c]:.3f})")

    # Weighting impact (D)
    d_results = {k: v for k, v in results.items() if k.startswith("D")}
    if len(d_results) >= 2:
        d_sharpes = {k: v["sharpe"] for k, v in d_results.items() if not np.isnan(v.get("sharpe", np.nan))}
        if d_sharpes:
            best_d = max(d_sharpes, key=d_sharpes.get)
            worst_d = min(d_sharpes, key=d_sharpes.get)
            print(f"\n  [Weighting] Best: {best_d} (Sharpe={d_sharpes[best_d]:.3f}), "
                  f"Worst: {worst_d} (Sharpe={d_sharpes[worst_d]:.3f})")
            print(f"  -> Weighting Sharpe spread: {d_sharpes[best_d] - d_sharpes[worst_d]:.3f}")

    # Overall best
    all_sharpes = {k: v["sharpe"] for k, v in results.items()
                   if not np.isnan(v.get("sharpe", np.nan))}
    if all_sharpes:
        overall_best = max(all_sharpes, key=all_sharpes.get)
        print(f"\n  [OVERALL BEST] {overall_best}")
        best_stats = results[overall_best]
        print(f"    Ann Return : {best_stats['ann_return']:.2%}")
        print(f"    Volatility : {best_stats['volatility']:.2%}")
        print(f"    Sharpe     : {best_stats['sharpe']:.3f}")
        print(f"    Max DD     : {best_stats['max_dd']:.2%}")
        print(f"    Win Rate   : {best_stats['win_rate']:.1%}")
        print(f"    N Periods  : {best_stats['n_periods']}")

        positive_sharpe = {k: v for k, v in all_sharpes.items() if v > 0}
        print(f"\n  Configs with positive Sharpe: {len(positive_sharpe)} / {len(all_sharpes)}")
        for name, sh in sorted(positive_sharpe.items(), key=lambda x: x[1], reverse=True):
            print(f"    {name}: {sh:.3f}")

    # Step 6: Plot top 3 + comparison charts
    print()
    plot_top_configs(results, series_dict)

    print("\nDone.")


if __name__ == "__main__":
    main()
