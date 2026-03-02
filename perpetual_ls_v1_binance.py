"""
perpetual_ls_v1_binance.py
==========================
v1 perpetual L/S backtest — same logic as perpetual_ls_backtest.py —
but with ACTUAL Binance USDT-M perp data:

  Prices  : Binance weekly mark-price OHLCV (weekly close)
             Alignment: CMC snapshot_date (Sunday) = Binance week_start + 6 days
  Funding : Binance actual 8h funding rates (summed over holding period per symbol)

All other logic identical to v1:
  - CMC supply-inflation signal
  - Same universe filters (rank 21-250, ex-stables/memes/CEX)
  - Same 10th / 90th percentile decile cuts
  - Same taker fee + slippage model
  - Same cap-weighted regime detection (Bull/Bear vs 20w MA)
  - Monthly rebalancing (first CMC snapshot per calendar month)
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

# ===========================================================================
#  CONFIGURATION
# ===========================================================================

INPUT_FILE   = "D:/AI_Projects/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
OUTPUT_DIR   = "D:/AI_Projects/circ_supply/"
BINANCE_DIR  = "D:/AI_Projects/circ_supply/binance_perp_data"

# --- Universe filters (identical to v1) ---
MAX_RANK         = 300
TOP_N_EXCLUDE    = 20
FFILL_LIMIT      = 1

# --- Signal (identical to v1) ---
SUPPLY_WINDOW    = 4

# --- Portfolio decile cuts (identical to v1) ---
LONG_PCT         = 0.10
SHORT_PCT        = 0.90

# --- Capital allocation (identical to v1) ---
LONG_LEVERAGE    = 1.00
SHORT_LEVERAGE   = 1.00

# --- Execution costs (identical to v1) ---
TAKER_FEE        = 0.0004
SLIPPAGE_K       = 0.0005
MIN_TURNOVER     = 0.001
MAX_SLIPPAGE     = 0.02

# --- Regime detection (identical to v1) ---
REGIME_MA_WINDOW = 20

# --- Forward return winsorization (identical to v1) ---
WINS_LOW  = 0.01
WINS_HIGH = 0.99

# ===========================================================================
#  TOKEN EXCLUSION LISTS  (identical to v1)
# ===========================================================================

STABLECOINS = {
    "USDT","USDC","BUSD","DAI","TUSD","USDP","GUSD","FRAX","LUSD","MIM",
    "USDN","USTC","UST","HUSD","SUSD","PAX","USDS","USDJ","NUSD","USDK",
    "USDX","CUSD","CEUR","USDH","USDD","FDUSD","PYUSD","EURC","EURS",
    "USDQ","USDB","USDTB","SFRXETH","OSETH","CMETH",
}
CEX_TOKENS = {
    "BNB","HT","KCS","OKB","MX","CRO","BIX","GT","LEO","FTT",
    "WBT","BGB","BTSE","NEXO","CEL","LATOKEN","BTMX",
}
MEMECOINS = {
    "DOGE","SHIB","FLOKI","PEPE","BONK","WIF","FARTCOIN","SAFEMOON","ELON",
    "DOGELON","MEME","TURBO","POPCAT","MOG","BABYDOGE","KISHU","AKITA","HOGE",
    "SAITAMA","VOLT","ELONGATE","SAMO","BOME","NEIRO","SPX","BRETT","MYRO",
    "SLERF","TOSHI","GIGA","SUNDOG","MOODENG","PNUT","ACT","GOAT","CHILLGUY",
    "PONKE","LADYS","COQ","AIDOGE","WOJAK","HUHU","MILADY","BOBO","QUACK",
    "BONE","LEASH","FLOOF","PITBULL","HOKK","CATGIRL","SFM","LUNC",
}
EXCLUDED = STABLECOINS | CEX_TOKENS | MEMECOINS


# ===========================================================================
#  HELPERS  (identical to v1)
# ===========================================================================

def _periods_per_year(returns: pd.Series) -> float:
    if len(returns) < 2:
        return 52.0
    gaps = np.diff(returns.index).astype("timedelta64[D]").astype(float)
    median_gap = float(np.median(gaps)) if len(gaps) > 0 else 7.0
    return 365.25 / max(median_gap, 1.0)


def portfolio_stats(returns: pd.Series) -> dict:
    returns = returns.dropna()
    if len(returns) < 2:
        nan = np.nan
        return dict(ann_return=nan, volatility=nan, sharpe=nan, sortino=nan, max_dd=nan)

    cum          = (1 + returns).cumprod()
    total_days   = (returns.index[-1] - returns.index[0]).days
    total_years  = max(total_days / 365.25, 1.0 / 52)
    cum_final    = float(cum.iloc[-1])
    ann_return   = (cum_final ** (1.0 / total_years) - 1) if cum_final > 0 else np.nan

    ppy          = _periods_per_year(returns)
    volatility   = returns.std() * np.sqrt(ppy)
    sharpe       = (ann_return / volatility) if volatility > 0 and not np.isnan(ann_return) else np.nan

    downside     = returns[returns < 0]
    if len(downside) > 0:
        dd_std   = np.sqrt((downside ** 2).mean()) * np.sqrt(ppy)
        sortino  = (ann_return / dd_std) if dd_std > 0 and not np.isnan(ann_return) else np.nan
    else:
        sortino  = np.nan

    roll_max     = cum.cummax()
    max_dd       = float(((cum - roll_max) / roll_max).min())

    return dict(ann_return=ann_return, volatility=volatility,
                sharpe=sharpe, sortino=sortino, max_dd=max_dd)


def _fmt_pct(v: float) -> str:
    return f"{v:+.2%}" if not np.isnan(v) else "    N/A"

def _fmt_f(v: float, decimals: int = 3) -> str:
    return f"{v:+.{decimals}f}" if not np.isnan(v) else "    N/A"


# ===========================================================================
#  STEP 1 — Load CMC data  (identical to v1)
# ===========================================================================

def load_cmc_data(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["snapshot_date"])
    df = df.sort_values(["symbol", "snapshot_date"]).reset_index(drop=True)
    df = df[df["price"].notna() & (df["price"] > 0) & df["market_cap"].notna()].copy()
    for col in ["price", "circulating_supply"]:
        df[col] = df.groupby("symbol")[col].transform(lambda s: s.ffill(limit=FFILL_LIMIT))
    df = df[df["circulating_supply"].notna() & (df["circulating_supply"] > 0)].copy()
    print(f"[Load CMC] Rows: {len(df):,}  Symbols: {df['symbol'].nunique():,}  "
          f"Dates: {df['snapshot_date'].min().date()} -> {df['snapshot_date'].max().date()}")
    return df


# ===========================================================================
#  [BINANCE] STEP 1b — Load Binance price and funding data
# ===========================================================================

def load_binance_data(binance_dir: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load Binance weekly OHLCV and funding data.

    Date alignment:
      CMC snapshot_date (Sunday) = Binance week_start (Monday) + 6 days
      → Binance week_start = CMC snapshot_date − 6 days

    Returns:
      price_pivot : DataFrame indexed by CMC snapshot_date, columns=symbol, values=close
      fund_df     : raw weekly funding (symbol, week_start, funding_sum)
    """
    ohlcv = pd.read_parquet(f"{binance_dir}/weekly_ohlcv.parquet")
    fund  = pd.read_parquet(f"{binance_dir}/weekly_funding.parquet")

    # Map Binance week_start (Monday) → CMC snapshot_date (Sunday)
    ohlcv["cmc_date"] = ohlcv["week_start"] + pd.Timedelta(days=6)

    price_pivot = ohlcv.pivot_table(
        index="cmc_date", columns="symbol", values="close", aggfunc="last"
    )

    print(f"[Load Binance] OHLCV: {ohlcv['symbol'].nunique()} symbols, "
          f"{ohlcv['cmc_date'].min().date()} -> {ohlcv['cmc_date'].max().date()}")
    print(f"[Load Binance] Funding: {fund['symbol'].nunique()} symbols, "
          f"{fund['week_start'].min().date()} -> {fund['week_start'].max().date()}")

    return price_pivot, fund


# ===========================================================================
#  STEP 2 — Broad-market index + regime  (identical to v1)
# ===========================================================================

def build_regime(df: pd.DataFrame) -> pd.DataFrame:
    top = df[df["rank"] <= 100].copy()
    top = top[top["price"].notna()].copy()
    top = top.sort_values(["symbol", "snapshot_date"])
    top["pct_ret"] = top.groupby("symbol")["price"].pct_change(1)
    top = top[top["pct_ret"].notna()]

    def cap_weighted(g):
        total_cap = g["market_cap"].sum()
        if total_cap == 0:
            return np.nan
        w = g["market_cap"] / total_cap
        return float((w * g["pct_ret"]).sum())

    idx = (
        top.groupby("snapshot_date")
        .apply(cap_weighted, include_groups=False)
        .reset_index()
        .rename(columns={0: "index_return"})
        .sort_values("snapshot_date")
    )

    idx["index_price"] = (1 + idx["index_return"].fillna(0)).cumprod()
    idx["index_ma"]    = idx["index_price"].rolling(REGIME_MA_WINDOW, min_periods=1).mean()
    idx["regime"]      = np.where(idx["index_price"] >= idx["index_ma"], "Bull", "Bear")

    print(f"[Regime] Bull periods: {(idx['regime']=='Bull').sum()}  "
          f"Bear periods: {(idx['regime']=='Bear').sum()}")
    return idx[["snapshot_date", "index_return", "regime"]]


# ===========================================================================
#  STEP 3 — Feature engineering  (identical to v1)
# ===========================================================================

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    grp = df.groupby("symbol", group_keys=False)
    df["supply_inf"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW)
    )
    df["turnover"]  = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"]  = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)
    return df


# ===========================================================================
#  [BINANCE] STEP 4 — Monthly forward returns using Binance mark prices
# ===========================================================================

def build_monthly_fwd_returns(
    df: pd.DataFrame,
    rebal_dates: list,
    price_pivot: pd.DataFrame,       # [BINANCE] Binance close prices, indexed by CMC date
) -> pd.DataFrame:
    """
    For each rebalancing pair (t0, t1):
      - Forward return = Binance close(t1) / Binance close(t0) - 1
      - Slippage proxy from CMC volume_24h / market_cap (unchanged from v1)
    Symbols not in Binance have nan forward return (excluded from basket average).
    """
    slip_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol", values="slippage", aggfunc="last"
    )

    sorted_rebals = sorted(rebal_dates)
    records = []

    for i, t0 in enumerate(sorted_rebals[:-1]):
        t1        = sorted_rebals[i + 1]
        hold_days = (t1 - t0).days

        # [BINANCE] Check both CMC dates exist in Binance price pivot
        if t0 not in price_pivot.index or t1 not in price_pivot.index:
            continue

        p0 = price_pivot.loc[t0]    # Binance close at t0
        p1 = price_pivot.loc[t1]    # Binance close at t1

        s0 = slip_pivot.loc[t0] if t0 in slip_pivot.index else pd.Series(MAX_SLIPPAGE, index=p0.index)

        fwd = p1 / p0 - 1

        # Cross-sectional winsorise (identical to v1)
        lo = fwd.quantile(WINS_LOW)
        hi = fwd.quantile(WINS_HIGH)
        fwd = fwd.clip(lower=lo, upper=hi).clip(lower=-1.0)

        for sym in fwd.index:
            if pd.isna(fwd[sym]):
                continue
            records.append({
                "rebal_date":       t0,
                "symbol":           sym,
                "fwd_return_gross": float(fwd[sym]),
                "slippage":         float(s0.get(sym, MAX_SLIPPAGE)),
                "hold_days":        hold_days,
            })

    return pd.DataFrame(records)


# ===========================================================================
#  [BINANCE] Funding helper — actual rates over holding period
# ===========================================================================

def _basket_actual_funding(
    symbols: list,
    t0: pd.Timestamp,
    t1: pd.Timestamp,
    fund_df: pd.DataFrame,
) -> float:
    """
    Returns the mean total funding rate paid by a long position over the holding period.
    Positive = longs paid shorts (drag for longs, credit for shorts).
    Computed as the mean of per-symbol funding_sum across holding-period weeks.

    Date mapping (same as v4):
      Binance week_start = CMC date - 6 days
      Holding period weeks: week_start > t0_bnb AND week_start <= t1_bnb + 1 day
    """
    t0_bnb = t0 - pd.Timedelta(days=6)
    t1_bnb = t1 - pd.Timedelta(days=6)

    rates = []
    for sym in symbols:
        mask = (
            (fund_df["symbol"] == sym) &
            (fund_df["week_start"] >  t0_bnb) &
            (fund_df["week_start"] <= t1_bnb + pd.Timedelta(days=1))
        )
        rows = fund_df[mask]
        if not rows.empty:
            rates.append(float(rows["funding_sum"].sum()))

    return float(np.mean(rates)) if rates else 0.0


# ===========================================================================
#  STEP 5 — Main backtest loop
# ===========================================================================

def run_backtest(
    df: pd.DataFrame,
    regime_df: pd.DataFrame,
    price_pivot: pd.DataFrame,      # [BINANCE]
    fund_df: pd.DataFrame,          # [BINANCE]
) -> dict:
    df["ym"] = df["snapshot_date"].dt.to_period("M")
    rebal_dates = sorted(df.groupby("ym")["snapshot_date"].min().tolist())

    fwd_df     = build_monthly_fwd_returns(df, rebal_dates, price_pivot)
    regime_map = regime_df.set_index("snapshot_date")["regime"].to_dict()

    inf_snap = df[df["snapshot_date"].isin(rebal_dates)].copy()
    inf_snap = inf_snap[inf_snap["supply_inf"].notna()]
    inf_snap = inf_snap[~inf_snap["symbol"].isin(EXCLUDED)]
    inf_snap = inf_snap[
        (inf_snap["rank"] > TOP_N_EXCLUDE) &
        (inf_snap["rank"] <= MAX_RANK)
    ]

    dates_out             = []
    long_gross_l          = []
    short_gross_l         = []
    long_net_l            = []
    short_net_l           = []
    combined_net_l        = []
    basket_sizes_l        = []
    regime_out_l          = []
    funding_long_cum      = 0.0
    funding_short_cum     = 0.0
    funding_history       = []

    sorted_rebals = sorted(rebal_dates)

    for idx_i, t0 in enumerate(sorted_rebals):
        univ = inf_snap[inf_snap["snapshot_date"] == t0].copy()
        if len(univ) < 10:
            continue

        regime = regime_map.get(t0, "Bear")

        lo_cut = univ["supply_inf"].quantile(LONG_PCT)
        hi_cut = univ["supply_inf"].quantile(SHORT_PCT)

        basket_long  = univ[univ["supply_inf"] <= lo_cut]["symbol"].tolist()
        basket_short = univ[univ["supply_inf"] >= hi_cut]["symbol"].tolist()

        if not basket_long or not basket_short:
            continue

        fwd_t0 = fwd_df[fwd_df["rebal_date"] == t0].set_index("symbol")

        def basket_return(symbols: list):
            rets, slips, hdays = [], [], []
            for sym in symbols:
                if sym in fwd_t0.index and not pd.isna(fwd_t0.loc[sym, "fwd_return_gross"]):
                    rets.append(fwd_t0.loc[sym, "fwd_return_gross"])
                    slips.append(fwd_t0.loc[sym, "slippage"])
                    hdays.append(fwd_t0.loc[sym, "hold_days"])
            if not rets:
                return np.nan, MAX_SLIPPAGE, 30
            return float(np.mean(rets)), float(np.mean(slips)), float(np.mean(hdays))

        r_long_gross,  slip_long,  hdays = basket_return(basket_long)
        r_short_gross, slip_short, _     = basket_return(basket_short)

        if np.isnan(r_long_gross) or np.isnan(r_short_gross):
            continue

        hold_days = max(hdays, 1.0)

        # Next rebalance date (needed for funding window)
        next_idx  = sorted_rebals.index(t0) + 1
        if next_idx >= len(sorted_rebals):
            continue
        t1 = sorted_rebals[next_idx]

        # ---- Taker fee (identical to v1) ----
        fee_cost = 2 * TAKER_FEE

        # ---- [BINANCE] Actual funding rates ----
        # Positive funding_sum = longs paid shorts = drag for longs, credit for shorts
        funding_drag_long    = _basket_actual_funding(basket_long,  t0, t1, fund_df)
        funding_credit_short = _basket_actual_funding(basket_short, t0, t1, fund_df)

        period_fund_long  = -funding_drag_long
        period_fund_short = +funding_credit_short
        funding_long_cum  += period_fund_long
        funding_short_cum += period_fund_short
        funding_history.append({
            "date":         t0,
            "fund_long":    period_fund_long,
            "fund_short":   period_fund_short,
            "net_funding":  period_fund_long + period_fund_short,
            "regime":       regime,
        })

        # ---- Net returns (identical structure to v1) ----
        r_long_net = (r_long_gross
                      - fee_cost
                      - slip_long
                      - funding_drag_long)

        r_short_net = (-r_short_gross
                       - fee_cost
                       - slip_short
                       + funding_credit_short)

        nav_denom  = LONG_LEVERAGE + SHORT_LEVERAGE
        r_combined = (LONG_LEVERAGE * r_long_net + SHORT_LEVERAGE * r_short_net) / nav_denom

        r_long_net  = max(r_long_net,  -1.0)
        r_short_net = max(r_short_net, -1.0)
        r_combined  = max(r_combined,  -1.0)

        dates_out.append(t0)
        long_gross_l.append(max(r_long_gross, -1.0))
        short_gross_l.append(max(r_short_gross, -1.0))
        long_net_l.append(r_long_net)
        short_net_l.append(r_short_net)
        combined_net_l.append(r_combined)
        basket_sizes_l.append((len(basket_long), len(basket_short)))
        regime_out_l.append(regime)

    idx = pd.DatetimeIndex(dates_out)

    spread_gross = pd.Series(
        [lg - sg for lg, sg in zip(long_gross_l, short_gross_l)],
        index=idx, name="Spread Gross"
    )

    return dict(
        dates             = dates_out,
        index             = idx,
        long_gross        = pd.Series(long_gross_l,   index=idx, name="Long Basket (gross)"),
        short_gross       = pd.Series(short_gross_l,  index=idx, name="Short Basket (gross)"),
        long_net          = pd.Series(long_net_l,     index=idx, name="Long Leg (net)"),
        short_net         = pd.Series(short_net_l,    index=idx, name="Short Leg (net)"),
        combined_net      = pd.Series(combined_net_l, index=idx, name="L/S Combined (net)"),
        spread_gross      = spread_gross,
        basket_sizes      = basket_sizes_l,
        regime            = regime_out_l,
        funding_long_cum  = funding_long_cum,
        funding_short_cum = funding_short_cum,
        funding_history   = pd.DataFrame(funding_history),
    )


# ===========================================================================
#  STEP 6 — Performance report
# ===========================================================================

def print_report(res: dict) -> None:
    print("\n" + "=" * 72)
    print("PERPETUAL L/S BACKTEST — v1 Logic + Actual Binance Data")
    print(f"Capital allocation: {LONG_LEVERAGE:.0%} Long / {SHORT_LEVERAGE:.0%} Short")
    print("Prices : Binance USDT-M perp weekly close (mark price)")
    print("Funding: Binance actual 8h rates (summed over holding period)")
    print("=" * 72)

    n = len(res["dates"])
    avg_lo = np.mean([s[0] for s in res["basket_sizes"]])
    avg_hi = np.mean([s[1] for s in res["basket_sizes"]])
    print(f"\n  Rebalancing periods : {n}")
    print(f"  Date range          : {res['dates'][0].date()} -> {res['dates'][-1].date()}")
    print(f"  Avg basket size     : Long {avg_lo:.1f} tokens | Short {avg_hi:.1f} tokens")

    regimes = np.array(res["regime"])
    print(f"  Bull periods        : {(regimes=='Bull').sum()}  "
          f"Bear periods: {(regimes=='Bear').sum()}")

    print(f"\n  {'Series':<32} {'Ann.Ret':>10} {'Vol':>10} "
          f"{'Sharpe':>8} {'Sortino':>8} {'MaxDD':>10}")
    print("  " + "-" * 70)

    series_to_report = [
        ("Long Basket  (gross)",  res["long_gross"]),
        ("Short Basket (gross)",  res["short_gross"]),
        ("Long Leg     (net)",    res["long_net"]),
        ("Short Leg    (net)*",   res["short_net"]),
        ("L/S Combined (net)",    res["combined_net"]),
    ]

    for name, s in series_to_report:
        st = portfolio_stats(s)
        print(
            f"  {name:<32} "
            f"{_fmt_pct(st['ann_return']):>10} "
            f"{_fmt_pct(st['volatility']):>10} "
            f"{_fmt_f(st['sharpe']):>8} "
            f"{_fmt_f(st['sortino']):>8} "
            f"{_fmt_pct(st['max_dd']):>10}"
        )

    print("  * Short Leg net: +ve = profit (inverse of shorted basket return, after costs)")

    spread = res["spread_gross"]
    win_rate = (spread > 0).mean()
    n_wins   = (spread > 0).sum()
    print(f"\n  Win rate (Long > Short, gross)  : {n_wins}/{len(spread)} ({win_rate:.1%})")
    print(f"  Mean period spread (gross)      : {spread.mean():.2%}")

    spread_st = portfolio_stats(spread)
    print(f"  Spread ann. vol                 : {_fmt_pct(spread_st['volatility'])}")
    print(f"  Spread excess kurtosis          : {float(spread.kurtosis()):.2f}")
    print(f"  Spread skewness                 : {float(spread.skew()):.2f}")

    # Regime breakdown
    print(f"\n  {'Regime':<12} {'N':>4}  {'Mean Spread':>12}  {'Win Rate':>10}  {'Ann.Geo.Spread':>16}")
    print("  " + "-" * 58)
    for reg in ["Bull", "Bear"]:
        mask  = np.array(res["regime"]) == reg
        s_reg = spread.values[mask]
        if len(s_reg) == 0:
            continue
        geo = float((1 + pd.Series(s_reg)).prod() ** (52 / max(mask.sum(), 1)) - 1)
        print(f"  {reg:<12} {mask.sum():>4}  {np.mean(s_reg):>+12.2%}  "
              f"{(s_reg > 0).mean():>10.1%}  {geo:>+16.2%}")

    # Actual funding attribution
    print(f"\n  --- Actual Binance Funding Rate Attribution ---")
    print(f"  Cumulative funding drag  (long leg)   : {res['funding_long_cum']:+.4f} "
          f"({res['funding_long_cum']:.2%})")
    print(f"  Cumulative funding credit(short leg)  : {res['funding_short_cum']:+.4f} "
          f"({res['funding_short_cum']:.2%})")
    net_fund = res["funding_long_cum"] + res["funding_short_cum"]
    print(f"  Net funding impact on strategy        : {net_fund:+.4f} ({net_fund:.2%})")

    if not res["funding_history"].empty:
        fh = res["funding_history"]
        print(f"  Avg per-period net funding            : {fh['net_funding'].mean():+.4f}")
        print(f"  Avg long basket funding (drag)        : {fh['fund_long'].mean():+.4f}")
        print(f"  Avg short basket funding (credit)     : {fh['fund_short'].mean():+.4f}")

    print("\n  Note: Funding rates are ACTUAL Binance 8h rates summed over holding period.")
    print("=" * 72)


# ===========================================================================
#  STEP 7 — Visualisation
# ===========================================================================

def plot_results(res: dict) -> None:
    idx = res["index"]

    def _shade_bear(ax):
        in_bear, bear_start = False, None
        for dt, reg in zip(res["dates"], res["regime"]):
            if reg == "Bear" and not in_bear:
                bear_start, in_bear = dt, True
            elif reg != "Bear" and in_bear:
                ax.axvspan(bear_start, dt, alpha=0.07, color="crimson", zorder=0)
                in_bear = False
        if in_bear:
            ax.axvspan(bear_start, res["dates"][-1], alpha=0.07, color="crimson", zorder=0)

    # Figure 1: Cumulative returns
    fig, axes = plt.subplots(3, 1, figsize=(13, 13),
                             gridspec_kw={"height_ratios": [3, 2, 1.5]})
    fig.suptitle(
        "Perpetual L/S Backtest — v1 Logic + Actual Binance Data\n"
        "Binance USDT-M Mark Prices + Actual Funding  |  Monthly Rebalance  |  "
        f"10th/90th Pct  |  {LONG_LEVERAGE:.0%}/{SHORT_LEVERAGE:.0%}",
        fontsize=12, fontweight="bold"
    )

    ax = axes[0]
    cum_long_gross  = (1 + res["long_gross"].dropna()).cumprod()
    cum_short_gross = (1 + res["short_gross"].dropna()).cumprod()
    cum_comb        = (1 + res["combined_net"].dropna()).cumprod()

    ax.semilogy(cum_long_gross.index,  cum_long_gross.values,
                color="steelblue",  lw=2.0, label="Long basket  (gross, equal-wt)")
    ax.semilogy(cum_short_gross.index, cum_short_gross.values,
                color="crimson",    lw=2.0, label="Short basket (gross, equal-wt)")
    ax.semilogy(cum_comb.index,        cum_comb.values,
                color="mediumseagreen", lw=2.5, ls="-", label="L/S Combined (net, after costs)")
    ax.axhline(1, color="black", lw=0.6, ls="--")
    _shade_bear(ax)
    ax.set_ylabel("Cumulative Return (log scale)")
    ax.legend(fontsize=9)
    ax.grid(True, which="both", alpha=0.25)
    ax.set_title("Cumulative Wealth (1 = start)")

    ax2 = axes[1]
    cum_long_net  = (1 + res["long_net"].dropna()).cumprod()
    cum_short_net = (1 + res["short_net"].dropna()).cumprod()
    ax2.plot(cum_long_net.index,  cum_long_net.values,  color="steelblue",  lw=1.8, label="Long leg (net)")
    ax2.plot(cum_short_net.index, cum_short_net.values, color="darkorange", lw=1.8, label="Short leg (net)")
    ax2.axhline(1, color="black", lw=0.6, ls="--")
    _shade_bear(ax2)
    ax2.set_ylabel("Cumulative Return")
    ax2.legend(fontsize=9)
    ax2.grid(True, alpha=0.25)
    ax2.set_title("Net Leg Performance (fees + slippage + actual funding applied)")

    ax3 = axes[2]
    spread = res["spread_gross"]
    colors = ["steelblue" if v >= 0 else "crimson" for v in spread.values]
    ax3.bar(spread.index, spread.values, color=colors, width=20, alpha=0.8)
    ax3.axhline(0, color="black", lw=0.8)
    ax3.set_ylabel("Period Spread\n(Long − Short, gross)")
    ax3.set_xlabel("Rebalance Date")
    ax3.grid(True, alpha=0.25)

    fig.tight_layout()
    out1 = OUTPUT_DIR + "perp_ls_v1_binance_cumulative.png"
    fig.savefig(out1, dpi=150)
    plt.close(fig)
    print(f"[Plot] {out1}")

    # Figure 2: Funding attribution + drawdown
    fig2, axes2 = plt.subplots(2, 1, figsize=(12, 9))

    ax4 = axes2[0]
    if not res["funding_history"].empty:
        fh = res["funding_history"].copy().set_index("date").sort_index()
        fh["cum_fund_long"]  = fh["fund_long"].cumsum()
        fh["cum_fund_short"] = fh["fund_short"].cumsum()
        fh["cum_net"]        = fh["net_funding"].cumsum()
        ax4.plot(fh.index, fh["cum_fund_long"],  color="steelblue",  lw=1.8, ls="--",
                 label="Cum. funding drag (long pays)")
        ax4.plot(fh.index, fh["cum_fund_short"], color="darkorange", lw=1.8, ls="--",
                 label="Cum. funding credit (short receives)")
        ax4.plot(fh.index, fh["cum_net"],        color="black",      lw=2.2,
                 label="Net funding impact")
        ax4.axhline(0, color="gray", lw=0.6, ls=":")
        _shade_bear(ax4)
        ax4.set_ylabel("Cumulative Funding P&L")
        ax4.set_title("Funding Rate Attribution — ACTUAL Binance 8h Rates")
        ax4.legend(fontsize=9)
        ax4.grid(True, alpha=0.25)

    ax5 = axes2[1]
    cum   = (1 + res["combined_net"].dropna()).cumprod()
    dd    = (cum - cum.cummax()) / cum.cummax()
    ax5.fill_between(dd.index, dd.values, 0, color="crimson", alpha=0.5, label="L/S Combined Drawdown")
    ax5.set_ylabel("Drawdown")
    ax5.set_xlabel("Date")
    ax5.set_title("L/S Combined Portfolio — Drawdown")
    ax5.legend(fontsize=9)
    ax5.grid(True, alpha=0.25)

    fig2.tight_layout()
    out2 = OUTPUT_DIR + "perp_ls_v1_binance_funding_drawdown.png"
    fig2.savefig(out2, dpi=150)
    plt.close(fig2)
    print(f"[Plot] {out2}")

    # Figure 3: Bull/Bear spread distribution
    regimes    = np.array(res["regime"])
    spread_arr = res["spread_gross"].values

    fig3, axes3 = plt.subplots(1, 2, figsize=(12, 5))
    fig3.suptitle("Spread Distribution by Regime (Gross Long − Short, Binance prices)",
                  fontsize=12)

    for i, (label, color) in enumerate([("Bull Regime", "steelblue"), ("Bear Regime", "crimson")]):
        reg_label = "Bull" if i == 0 else "Bear"
        mask = regimes == reg_label
        ax  = axes3[i]
        vals = spread_arr[mask]
        if len(vals) == 0:
            ax.set_title(f"{label} (no data)")
            continue
        ax.hist(vals, bins=20, color=color, alpha=0.7, edgecolor="black")
        ax.axvline(0,           color="black", lw=1.0, ls="--")
        ax.axvline(vals.mean(), color="black", lw=1.5, ls="-",
                   label=f"Mean: {vals.mean():.2%}")
        win = (vals > 0).mean()
        ax.set_title(f"{label} — Win Rate: {win:.1%}  n={len(vals)}")
        ax.set_xlabel("Period Spread (gross)")
        ax.set_ylabel("Frequency")
        ax.legend(fontsize=9)

    fig3.tight_layout()
    out3 = OUTPUT_DIR + "perp_ls_v1_binance_regime_breakdown.png"
    fig3.savefig(out3, dpi=150)
    plt.close(fig3)
    print(f"[Plot] {out3}")


# ===========================================================================
#  MAIN
# ===========================================================================

def main():
    print("=" * 72)
    print("Perpetual L/S Backtest — v1 Logic + Actual Binance Data")
    print(f"Capital allocation : {LONG_LEVERAGE:.0%} Long / {SHORT_LEVERAGE:.0%} Short")
    print(f"Signal             : {SUPPLY_WINDOW}-week trailing supply inflation (CMC)")
    print(f"Decile cuts        : Long <={LONG_PCT:.0%}  |  Short >={SHORT_PCT:.0%}")
    print(f"Prices             : Binance USDT-M weekly mark-price close")
    print(f"Funding            : Binance actual 8h rates, summed per holding period")
    print(f"Taker fee          : {TAKER_FEE:.4%} per side")
    print("=" * 72)

    df           = load_cmc_data(INPUT_FILE)
    price_pivot, fund_df = load_binance_data(BINANCE_DIR)
    regime_df    = build_regime(df)
    df           = engineer_features(df)
    results      = run_backtest(df, regime_df, price_pivot, fund_df)

    print_report(results)
    plot_results(results)

    print("\nDone.")


if __name__ == "__main__":
    main()
