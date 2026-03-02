"""
perpetual_ls_backtest.py
========================
Comprehensive Long/Short Market-Neutral Backtest
Supply-Dilution Hypothesis — Simulated Perpetual Futures Execution

Strategy
--------
* Universe  : Top 250 tokens by market cap (ex-stablecoins, memecoins, CEX tokens, mega-caps rank<=20)
* Signal    : Trailing ~30-day (4-week) circulating supply inflation rate
* Long leg  : Bottom decile (<=10th pct, lowest inflation)  ~20-25 tokens
* Short leg : Top decile    (>=90th pct, highest inflation) ~20-25 tokens
* Rebalance : Monthly (first weekly snapshot of each calendar month)
* Weighting : Equal-weight within each basket

Capital Allocation (configurable)
----------------------------------
  LONG_LEVERAGE / SHORT_LEVERAGE
    100 / 100  -> pure dollar-neutral (default)
    130 / 30   -> 130/30 long-biased

Execution Cost Model
---------------------
  1. Taker fee : 0.04% per side (open + close -> 0.08% round-trip per rebalance)
  2. Slippage  : Proportional inverse-turnover model, capped at MAX_SLIPPAGE
  3. Funding   : Synthetic 8h-rate model (regime-dependent, basket-specific)
                 Positive rate -> longs PAY shorts (typical crypto contango)

Funding Rate Assumptions (8h rate)
------------------------------------
  Bull market  low-inflation long  : +0.008%  (0.00008)
  Bull market  high-inflation short: +0.015%  (0.00015)
  Bear market  low-inflation long  : +0.002%  (0.00002)
  Bear market  high-inflation short: +0.005%  (0.00005)
  Source: approximated from Binance/Bybit historical averages (2019-2024).
  3 payments/day × ~30 days = 90 payments per monthly holding period.

Performance Metrics
--------------------
  Long basket  : Ann. Return, Volatility, Sharpe, Sortino, MaxDD
  Short basket : same (gross return of shorted basket)
  L/S Spread   : Combined portfolio Ann. Return, Vol, Sharpe, Sortino, MaxDD
  Win rate     : % of monthly periods where Long basket beats Short basket (gross)
  Funding P&L  : Cumulative funding drag/credit breakdown
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

# ===========================================================================
#  CONFIGURATION  —  edit these to change strategy behaviour
# ===========================================================================

INPUT_FILE  = "D:/AI_Projects/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
OUTPUT_DIR  = "D:/AI_Projects/circ_supply/"

# --- Universe filters ---
MAX_RANK         = 250    # only tokens ranked <= this are eligible
TOP_N_EXCLUDE    = 20     # additionally exclude mega-caps (rank <= this)
FFILL_LIMIT      = 1      # max periods to forward-fill missing price/supply

# --- Signal ---
SUPPLY_WINDOW    = 4      # trailing weeks for supply inflation (~30 days)

# --- Portfolio decile cuts ---
LONG_PCT         = 0.10   # tokens at or below 10th pct -> long
SHORT_PCT        = 0.90   # tokens at or above 90th pct -> short

# --- Capital allocation (100/100 default; 130/30 by setting below) ---
LONG_LEVERAGE    = 1.00   # notional long  as fraction of NAV
SHORT_LEVERAGE   = 1.00   # notional short as fraction of NAV

# --- Execution costs ---
TAKER_FEE        = 0.0004  # 0.04% taker fee per side
SLIPPAGE_K       = 0.0005
MIN_TURNOVER     = 0.001
MAX_SLIPPAGE     = 0.02

# --- Funding rate model (8h rate per holding basket per market regime) ---
# Positive = longs pay shorts; Negative = shorts pay longs
FUNDING_8H = {
    ("Bull", "long"):  +0.0000800,   # low-inflation longs : 0.008% per 8h
    ("Bull", "short"): +0.0001500,   # high-inflation shorts: 0.015% per 8h
    ("Bear", "long"):  +0.0000200,   # 0.002% per 8h
    ("Bear", "short"): +0.0000500,   # 0.005% per 8h
}
FUNDING_PERIODS_PER_DAY = 3          # standard 8h perp schedule

# --- Regime detection ---
REGIME_MA_WINDOW = 20    # rolling MA window (in weekly periods) for bull/bear

# --- Forward return winsorization ---
WINS_LOW  = 0.01
WINS_HIGH = 0.99

# ===========================================================================
#  TOKEN EXCLUSION LISTS  (inherited from extreme_percentile.py)
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
#  HELPERS
# ===========================================================================

def _periods_per_year(returns: pd.Series) -> float:
    """Estimate annualisation factor from median gap between index timestamps."""
    if len(returns) < 2:
        return 52.0
    gaps = np.diff(returns.index).astype("timedelta64[D]").astype(float)
    median_gap = float(np.median(gaps)) if len(gaps) > 0 else 7.0
    return 365.25 / max(median_gap, 1.0)


def portfolio_stats(returns: pd.Series) -> dict:
    """Annualised return, vol, Sharpe, Sortino (target=0), MaxDD."""
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

    # Sortino: downside deviation (returns below 0 only)
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
#  STEP 1 — Load & preprocess
# ===========================================================================

def load_data(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["snapshot_date"])
    df = df.sort_values(["symbol", "snapshot_date"]).reset_index(drop=True)

    df = df[df["price"].notna() & (df["price"] > 0) & df["market_cap"].notna()].copy()

    # Forward-fill at most 1 gap for price and supply
    for col in ["price", "circulating_supply"]:
        df[col] = df.groupby("symbol")[col].transform(lambda s: s.ffill(limit=FFILL_LIMIT))

    df = df[df["circulating_supply"].notna() & (df["circulating_supply"] > 0)].copy()

    print(f"[Load] Rows: {len(df):,}  Symbols: {df['symbol'].nunique():,}  "
          f"Dates: {df['snapshot_date'].min().date()} -> {df['snapshot_date'].max().date()}")
    return df


# ===========================================================================
#  STEP 2 — Broad-market index + regime
# ===========================================================================

def build_regime(df: pd.DataFrame) -> pd.DataFrame:
    """
    Cap-weighted index of top-100 tokens; Bull = price >= 20-week MA.
    Returns DataFrame with columns [snapshot_date, index_return, regime].
    """
    top = df[df["rank"] <= 100].copy()
    top = top[top["price"].notna()].copy()

    # Compute 1-period price return per symbol across time (not within date groups)
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
#  STEP 3 — Feature engineering
# ===========================================================================

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute trailing supply inflation and slippage proxy."""
    grp = df.groupby("symbol", group_keys=False)

    df["supply_inf"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW)
    )
    df["turnover"]  = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"]  = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)

    return df


# ===========================================================================
#  STEP 4 — Monthly forward returns (price ratio to next rebalance)
# ===========================================================================

def build_monthly_fwd_returns(
    df: pd.DataFrame,
    rebal_dates: list,
) -> pd.DataFrame:
    """
    For each rebalancing date T and the next date T+1, compute the
    raw price-based forward return = price(T+1) / price(T) - 1.
    Returns a DataFrame: [rebal_date, symbol, fwd_return_gross, slippage, hold_days].
    """
    price_pivot   = df.pivot_table(index="snapshot_date", columns="symbol",
                                   values="price",    aggfunc="last")
    slip_pivot    = df.pivot_table(index="snapshot_date", columns="symbol",
                                   values="slippage", aggfunc="last")

    sorted_rebals = sorted(rebal_dates)
    records = []

    for i, t0 in enumerate(sorted_rebals[:-1]):
        t1 = sorted_rebals[i + 1]
        hold_days = (t1 - t0).days

        if t0 not in price_pivot.index or t1 not in price_pivot.index:
            continue

        p0 = price_pivot.loc[t0]
        p1 = price_pivot.loc[t1]
        s0 = slip_pivot.loc[t0] if t0 in slip_pivot.index else pd.Series(MAX_SLIPPAGE, index=p0.index)

        fwd = p1 / p0 - 1

        # Cross-sectional winsorise
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
#  STEP 5 — Main backtest loop
# ===========================================================================

def run_backtest(df: pd.DataFrame, regime_df: pd.DataFrame) -> dict:
    """
    Core strategy loop.  Returns a dict with all time series and funding stats.
    """
    # ---- Build universe at each rebalancing date ----
    df["ym"] = df["snapshot_date"].dt.to_period("M")
    rebal_dates = sorted(df.groupby("ym")["snapshot_date"].min().tolist())

    fwd_df    = build_monthly_fwd_returns(df, rebal_dates)
    regime_map = regime_df.set_index("snapshot_date")["regime"].to_dict()

    # Supply inflation snapshot at each rebalancing date
    inf_snap = df[df["snapshot_date"].isin(rebal_dates)].copy()
    inf_snap = inf_snap[inf_snap["supply_inf"].notna()]
    inf_snap = inf_snap[~inf_snap["symbol"].isin(EXCLUDED)]
    inf_snap = inf_snap[
        (inf_snap["rank"] > TOP_N_EXCLUDE) &
        (inf_snap["rank"] <= MAX_RANK)
    ]

    # Accumulators
    dates_out       = []
    long_gross      = []   # equal-weighted return of long basket (before costs)
    short_gross     = []   # equal-weighted return of short basket (before costs)
    long_net        = []   # after taker fee + slippage + funding
    short_net       = []   # gross return of shorted basket after costs (sign convention: +ve = profit)
    combined_net    = []   # LONG_LEV * long_net - SHORT_LEV * short_net (as spread P&L)
    basket_sizes    = []
    regime_out      = []
    funding_long_cum  = 0.0   # running funding drag on long leg
    funding_short_cum = 0.0   # running funding credit on short leg (sign: +ve = receipt)
    funding_history   = []    # per-period funding impact

    for t0 in rebal_dates:
        # Universe at this date
        univ = inf_snap[inf_snap["snapshot_date"] == t0].copy()
        if len(univ) < 10:
            continue

        regime = regime_map.get(t0, "Bear")

        # Decile cuts
        lo_cut = univ["supply_inf"].quantile(LONG_PCT)
        hi_cut = univ["supply_inf"].quantile(SHORT_PCT)

        basket_long  = univ[univ["supply_inf"] <= lo_cut]["symbol"].tolist()
        basket_short = univ[univ["supply_inf"] >= hi_cut]["symbol"].tolist()

        if not basket_long or not basket_short:
            continue

        # Forward returns for this period
        fwd_t0 = fwd_df[fwd_df["rebal_date"] == t0].set_index("symbol")

        def basket_return(symbols: list) -> tuple[float, float, float]:
            """
            Equal-weighted gross return, avg slippage, hold_days for a basket.
            Returns (gross_ret, avg_slip, hold_days).
            """
            rets   = []
            slips  = []
            hdays  = []
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

        # ---- Taker fee cost (open + close = 2 × TAKER_FEE) ----
        fee_cost = 2 * TAKER_FEE

        # ---- Funding rate cost (longs pay, shorts receive when rate > 0) ----
        n_payments = FUNDING_PERIODS_PER_DAY * hold_days
        r_fund_long  = FUNDING_8H.get((regime, "long"),  0.0)
        r_fund_short = FUNDING_8H.get((regime, "short"), 0.0)

        # Long leg: pays funding -> drag
        funding_drag_long = r_fund_long * n_payments   # positive = drag on longs

        # Short leg: receives funding when rate > 0 -> credit
        # (when rate < 0, shorts pay)
        funding_credit_short = r_fund_short * n_payments  # positive = receipt by shorts

        # Track funding attribution
        period_fund_long  = -funding_drag_long    # negative = cost to long leg
        period_fund_short = +funding_credit_short # positive = gain to short leg
        funding_long_cum  += period_fund_long
        funding_short_cum += period_fund_short
        funding_history.append({
            "date":         t0,
            "fund_long":    period_fund_long,
            "fund_short":   period_fund_short,
            "net_funding":  period_fund_long + period_fund_short,
        })

        # ---- Net returns ----
        # Long: gross return minus entry/exit fees, slippage, and funding drag
        r_long_net = (r_long_gross
                      - fee_cost
                      - slip_long
                      - funding_drag_long)

        # Short: we earn the inverse of the shorted basket's return
        # Short P&L = -r_short_gross (profit if short goes down)
        # Minus fees + slippage, plus funding receipt
        r_short_net = (-r_short_gross
                       - fee_cost
                       - slip_short
                       + funding_credit_short)

        # Combined portfolio return on NAV:
        # = (LONG_LEV × long_net + SHORT_LEV × short_net) / (LONG_LEV + SHORT_LEV)
        nav_denom   = LONG_LEVERAGE + SHORT_LEVERAGE
        r_combined  = (LONG_LEVERAGE * r_long_net + SHORT_LEVERAGE * r_short_net) / nav_denom

        # Floor at -1.0 (total wipe-out, cannot lose more than NAV)
        r_long_net  = max(r_long_net,  -1.0)
        r_short_net = max(r_short_net, -1.0)
        r_combined  = max(r_combined,  -1.0)

        dates_out.append(t0)
        long_gross.append(max(r_long_gross, -1.0))
        short_gross.append(max(r_short_gross, -1.0))
        long_net.append(r_long_net)
        short_net.append(r_short_net)
        combined_net.append(r_combined)
        basket_sizes.append((len(basket_long), len(basket_short)))
        regime_out.append(regime)

    idx = pd.DatetimeIndex(dates_out)

    # Spread: gross Low minus gross High (win rate signal)
    spread_gross = pd.Series(
        [lg - sg for lg, sg in zip(long_gross, short_gross)],
        index=idx, name="Spread Gross"
    )

    return dict(
        dates           = dates_out,
        index           = idx,
        long_gross      = pd.Series(long_gross,   index=idx, name="Long Basket (gross)"),
        short_gross     = pd.Series(short_gross,  index=idx, name="Short Basket (gross)"),
        long_net        = pd.Series(long_net,     index=idx, name="Long Leg (net)"),
        short_net       = pd.Series(short_net,    index=idx, name="Short Leg (net)"),
        combined_net    = pd.Series(combined_net, index=idx, name="L/S Combined (net)"),
        spread_gross    = spread_gross,
        basket_sizes    = basket_sizes,
        regime          = regime_out,
        funding_long_cum  = funding_long_cum,
        funding_short_cum = funding_short_cum,
        funding_history   = pd.DataFrame(funding_history),
    )


# ===========================================================================
#  STEP 6 — Performance report
# ===========================================================================

def print_report(res: dict) -> None:
    print("\n" + "=" * 72)
    print("PERPETUAL L/S BACKTEST RESULTS — Supply-Dilution Hypothesis")
    print(f"Capital allocation: {LONG_LEVERAGE:.0%} Long / {SHORT_LEVERAGE:.0%} Short")
    print("=" * 72)

    n = len(res["dates"])
    avg_lo = np.mean([s[0] for s in res["basket_sizes"]])
    avg_hi = np.mean([s[1] for s in res["basket_sizes"]])
    print(f"\n  Rebalancing periods : {n}")
    print(f"  Avg basket size     : Long {avg_lo:.1f} tokens | Short {avg_hi:.1f} tokens")

    regimes = np.array(res["regime"])
    print(f"  Bull periods        : {(regimes=='Bull').sum()}  "
          f"Bear periods: {(regimes=='Bear').sum()}")

    # ---- Per-leg stats ----
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

    # ---- Win rate ----
    spread = res["spread_gross"]
    win_rate = (spread > 0).mean()
    n_wins   = (spread > 0).sum()
    print(f"\n  Win rate (Long > Short, gross)  : {n_wins}/{len(spread)} ({win_rate:.1%})")
    print(f"  Mean period spread (gross)      : {spread.mean():.2%}")

    spread_st = portfolio_stats(spread)
    print(f"  Spread ann. vol                 : {_fmt_pct(spread_st['volatility'])}")

    # ---- Funding attribution ----
    print(f"\n  --- Funding Rate P&L Attribution ---")
    print(f"  Cumulative funding drag  (long leg)   : {res['funding_long_cum']:+.4f} "
          f"({res['funding_long_cum']:.2%})")
    print(f"  Cumulative funding credit(short leg)  : {res['funding_short_cum']:+.4f} "
          f"({res['funding_short_cum']:.2%})")
    net_fund = res["funding_long_cum"] + res["funding_short_cum"]
    print(f"  Net funding impact on strategy        : {net_fund:+.4f} ({net_fund:.2%})")

    if not res["funding_history"].empty:
        fh = res["funding_history"]
        print(f"  Avg per-period net funding            : {fh['net_funding'].mean():+.4f}")

    print("\n  Note: Funding rates are SYNTHETIC (regime-dependent model).")
    print("        Replace FUNDING_8H dict with real exchange data for live use.")
    print("=" * 72)


# ===========================================================================
#  STEP 7 — Visualisation
# ===========================================================================

def plot_results(res: dict, regime_df: pd.DataFrame) -> None:
    regime_map = regime_df.set_index("snapshot_date")["regime"].to_dict()
    idx = res["index"]

    def _shade_bear(ax):
        """Shade bear-market periods in light red."""
        in_bear, bear_start = False, None
        for dt, reg in zip(res["dates"], res["regime"]):
            if reg == "Bear" and not in_bear:
                bear_start, in_bear = dt, True
            elif reg != "Bear" and in_bear:
                ax.axvspan(bear_start, dt, alpha=0.07, color="crimson", zorder=0)
                in_bear = False
        if in_bear:
            ax.axvspan(bear_start, res["dates"][-1], alpha=0.07, color="crimson", zorder=0)

    # -----------------------------------------------------------------
    # Figure 1: Cumulative returns — gross baskets + net combined
    # -----------------------------------------------------------------
    fig, axes = plt.subplots(3, 1, figsize=(13, 13),
                             gridspec_kw={"height_ratios": [3, 2, 1.5]})
    fig.suptitle(
        "Perpetual L/S Backtest — Supply-Dilution Hypothesis\n"
        f"{LONG_LEVERAGE:.0%}/{SHORT_LEVERAGE:.0%} Capital Allocation  |  "
        f"10th/90th Pct Decile  |  Monthly Rebalance",
        fontsize=13, fontweight="bold"
    )

    # --- Panel 1: cumulative wealth (log scale) ---
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

    # --- Panel 2: net legs ---
    ax2 = axes[1]
    cum_long_net  = (1 + res["long_net"].dropna()).cumprod()
    cum_short_net = (1 + res["short_net"].dropna()).cumprod()

    ax2.plot(cum_long_net.index,  cum_long_net.values,
             color="steelblue",  lw=1.8, label="Long leg (net of fees+funding)")
    ax2.plot(cum_short_net.index, cum_short_net.values,
             color="darkorange", lw=1.8, label="Short leg (net of fees+funding)")
    ax2.axhline(1, color="black", lw=0.6, ls="--")
    _shade_bear(ax2)
    ax2.set_ylabel("Cumulative Return")
    ax2.legend(fontsize=9)
    ax2.grid(True, alpha=0.25)
    ax2.set_title("Net Leg Performance (fees + slippage + funding applied)")

    # --- Panel 3: per-period spread bar ---
    ax3 = axes[2]
    spread = res["spread_gross"]
    colors = ["steelblue" if v >= 0 else "crimson" for v in spread.values]
    ax3.bar(spread.index, spread.values, color=colors, width=20, alpha=0.8)
    ax3.axhline(0, color="black", lw=0.8)
    ax3.set_ylabel("Period Spread\n(Long − Short, gross)")
    ax3.set_xlabel("Rebalance Date")
    ax3.grid(True, alpha=0.25)

    fig.tight_layout()
    out1 = OUTPUT_DIR + "perp_ls_cumulative.png"
    fig.savefig(out1, dpi=150)
    plt.close(fig)
    print(f"[Plot] Saved: {out1}")

    # -----------------------------------------------------------------
    # Figure 2: Funding attribution + drawdown
    # -----------------------------------------------------------------
    fig2, axes2 = plt.subplots(2, 1, figsize=(12, 9))

    # --- Funding cumulative impact ---
    ax4 = axes2[0]
    if not res["funding_history"].empty:
        fh = res["funding_history"].copy()
        fh = fh.set_index("date").sort_index()
        fh["cum_fund_long"]  = fh["fund_long"].cumsum()
        fh["cum_fund_short"] = fh["fund_short"].cumsum()
        fh["cum_net"]        = fh["net_funding"].cumsum()

        ax4.plot(fh.index, fh["cum_fund_long"],  color="steelblue",
                 lw=1.8, ls="--", label="Cumulative funding drag (long pays)")
        ax4.plot(fh.index, fh["cum_fund_short"], color="darkorange",
                 lw=1.8, ls="--", label="Cumulative funding credit (short receives)")
        ax4.plot(fh.index, fh["cum_net"],        color="black",
                 lw=2.2, label="Net funding impact on strategy")
        ax4.axhline(0, color="gray", lw=0.6, ls=":")
        _shade_bear(ax4)
        ax4.set_ylabel("Cumulative Funding P&L")
        ax4.set_title("Funding Rate Attribution (Synthetic 8h Model)")
        ax4.legend(fontsize=9)
        ax4.grid(True, alpha=0.25)

    # --- Drawdown ---
    ax5 = axes2[1]
    cum   = (1 + res["combined_net"].dropna()).cumprod()
    dd    = (cum - cum.cummax()) / cum.cummax()
    ax5.fill_between(dd.index, dd.values, 0, color="crimson", alpha=0.5, label="Combined L/S Drawdown")
    ax5.set_ylabel("Drawdown")
    ax5.set_xlabel("Date")
    ax5.set_title("L/S Combined Portfolio — Drawdown")
    ax5.legend(fontsize=9)
    ax5.grid(True, alpha=0.25)

    fig2.tight_layout()
    out2 = OUTPUT_DIR + "perp_ls_funding_drawdown.png"
    fig2.savefig(out2, dpi=150)
    plt.close(fig2)
    print(f"[Plot] Saved: {out2}")

    # -----------------------------------------------------------------
    # Figure 3: Bull/Bear regime breakdown
    # -----------------------------------------------------------------
    regimes = np.array(res["regime"])
    bull_idx = np.where(regimes == "Bull")[0]
    bear_idx = np.where(regimes == "Bear")[0]
    idx_arr  = np.array(res["index"])

    spread_arr = res["spread_gross"].values

    fig3, axes3 = plt.subplots(1, 2, figsize=(12, 5))
    fig3.suptitle("Spread Distribution by Market Regime (Gross Long − Short per period)",
                  fontsize=12)

    for i, (label, mask, color) in enumerate([
        ("Bull Regime", bull_idx, "steelblue"),
        ("Bear Regime", bear_idx, "crimson"),
    ]):
        ax = axes3[i]
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
    out3 = OUTPUT_DIR + "perp_ls_regime_breakdown.png"
    fig3.savefig(out3, dpi=150)
    plt.close(fig3)
    print(f"[Plot] Saved: {out3}")


# ===========================================================================
#  MAIN
# ===========================================================================

def main():
    print("=" * 72)
    print("Perpetual L/S Backtest — Supply-Dilution Hypothesis")
    print(f"Capital allocation : {LONG_LEVERAGE:.0%} Long / {SHORT_LEVERAGE:.0%} Short")
    print(f"Signal             : {SUPPLY_WINDOW}-week trailing supply inflation")
    print(f"Decile cuts        : Long <={LONG_PCT:.0%}  |  Short >={SHORT_PCT:.0%}")
    print(f"Taker fee          : {TAKER_FEE:.4%} per side")
    print("=" * 72)

    df         = load_data(INPUT_FILE)
    regime_df  = build_regime(df)
    df         = engineer_features(df)
    results    = run_backtest(df, regime_df)

    print_report(results)
    plot_results(results, regime_df)

    print("\nDone.")


if __name__ == "__main__":
    main()
