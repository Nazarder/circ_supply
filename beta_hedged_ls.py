"""
beta_hedged_ls.py -- Beta-Hedged Long/Short Strategy Backtest

Thesis: isolate the negative alpha of supply dilution by shorting a basket of
high-inflation altcoins while hedging market beta via a long position in major
proven crypto assets.

Short Leg : Q4 supply inflation altcoins, inv-vol weighted, slippage-adjusted
Long Leg  : 3 variations tested simultaneously
  A -- 100% BTC
  B -- 50% BTC + 50% ETH
  C -- Top 10 non-stablecoin assets, cap-weighted

Portfolio modes (tested for each Long Leg variation):
  Dollar-Neutral : Long $1 vs Short $1  -> R = R_long - R_short
  Beta-Neutral   : Long (beta*$1) vs Short $1 -> R = beta*R_long - R_short
                   beta estimated from trailing BETA_WINDOW monthly returns
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
INPUT_FILE    = "D:/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
OUTPUT_DIR    = "D:/circ_supply/"
FFILL_LIMIT   = 1
SUPPLY_WINDOW = 13      # weeks for trailing supply inflation
FORWARD_PRDS  = 4       # weeks holding period
VOL_WINDOW    = 12      # trailing periods for inv-vol weighting
MIN_VOL       = 0.01    # 1% floor for vol estimation
BETA_WINDOW   = 12      # trailing periods for beta estimation
MIN_BETA      = 0.5     # clamp beta to prevent extreme leverage
MAX_BETA      = 3.0
SLIPPAGE_K    = 0.0005
MIN_TURNOVER  = 0.001
MAX_SLIPPAGE  = 0.02
INDEX_TOP_N   = 100     # for regime detection only
REGIME_MA     = 20

STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "GUSD", "FRAX",
    "LUSD", "MIM", "USDN", "USTC", "UST", "HUSD", "SUSD", "PAX",
    "USDS", "USDJ", "NUSD", "USDK", "USDX", "CUSD", "CEUR", "USDH",
    "USDD", "FDUSD", "PYUSD", "EURC", "EURS",
}


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
        return dict(ann_return=np.nan, volatility=np.nan, sharpe=np.nan, max_dd=np.nan)
    cum = (1 + returns).cumprod()
    total_days  = (returns.index[-1] - returns.index[0]).days
    total_years = max(total_days / 365.25, 1 / 52)
    cum_final   = float(cum.iloc[-1])
    ann_return  = cum_final ** (1.0 / total_years) - 1 if cum_final > 0 else np.nan
    gaps           = np.diff(returns.index).astype("timedelta64[D]").astype(float)
    median_gap     = float(np.median(gaps)) if len(gaps) > 0 else 30.0
    periods_per_yr = 365.25 / max(median_gap, 1.0)
    volatility     = returns.std() * np.sqrt(periods_per_yr)
    sharpe = ann_return / volatility if (volatility > 0 and not np.isnan(ann_return)) else np.nan
    roll_max = cum.cummax()
    max_dd   = ((cum - roll_max) / roll_max).min()
    return dict(ann_return=ann_return, volatility=volatility, sharpe=sharpe, max_dd=max_dd)


def _calc_beta(short_arr: np.ndarray, long_arr: np.ndarray) -> float:
    mask = ~(np.isnan(short_arr) | np.isnan(long_arr))
    if mask.sum() < 4:
        return 1.0
    s = short_arr[mask]
    l = long_arr[mask]
    var_l = float(np.var(l))
    if var_l < 1e-12:
        return 1.0
    beta = float(np.cov(s, l)[0, 1] / var_l)
    return float(np.clip(beta, MIN_BETA, MAX_BETA))


# ---------------------------------------------------------------------------
# STEP 1 -- Load data
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
    df["pct_return"] = (
        df.groupby("symbol")["price"]
        .transform(lambda s: s.pct_change(1))
    )
    df["pct_return"] = (
        df.groupby("snapshot_date")["pct_return"]
        .transform(_winsorize_cross_section)
    )
    print(f"[Data] Rows: {len(df):,}  Symbols: {df['symbol'].nunique():,}  "
          f"Dates: {df['snapshot_date'].min().date()} to {df['snapshot_date'].max().date()}")
    return df


# ---------------------------------------------------------------------------
# STEP 2 -- Feature engineering
# ---------------------------------------------------------------------------

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    grp = df.groupby("symbol", group_keys=False)

    df["supply_pct_13p"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW)
    )
    df["trailing_vol"] = grp["pct_return"].transform(
        lambda s: s.rolling(VOL_WINDOW, min_periods=4).std()
    )
    df["turnover"] = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"]  = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)
    return df


# ---------------------------------------------------------------------------
# STEP 3 -- Regime (for chart shading only)
# ---------------------------------------------------------------------------

def compute_regime(df: pd.DataFrame) -> pd.DataFrame:
    top = df[df["rank"] <= INDEX_TOP_N].copy()
    top = top[top["pct_return"].notna()].copy()

    def _cap_wt(g):
        tc = g["market_cap"].sum()
        return np.nan if tc == 0 else (g["market_cap"] / tc * g["pct_return"]).sum()

    idx = (
        top.groupby("snapshot_date", group_keys=False)
        .apply(_cap_wt, include_groups=False)
        .reset_index()
        .rename(columns={0: "index_return"})
        .sort_values("snapshot_date")
    )
    idx["index_price"] = (1 + idx["index_return"]).cumprod()
    idx["ma20"] = idx["index_price"].rolling(REGIME_MA, min_periods=1).mean()
    idx["regime"] = np.where(idx["index_price"] >= idx["ma20"], "Bull", "Bear")
    return idx[["snapshot_date", "regime"]].set_index("snapshot_date")["regime"].to_dict()


# ---------------------------------------------------------------------------
# STEP 4 -- Build all return inputs per rebalancing date
# ---------------------------------------------------------------------------

def build_return_inputs(df: pd.DataFrame) -> dict:
    df["ym"] = df["snapshot_date"].dt.to_period("M")
    rebal_dates = sorted(
        df.groupby("ym")["snapshot_date"].min()
    )

    # Price pivot for forward return computation
    price_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol", values="price", aggfunc="last"
    )
    all_snap_dates = sorted(price_pivot.index)
    snap_idx = pd.Index(all_snap_dates)

    # Slippage pivot
    slip_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol", values="slippage", aggfunc="last"
    )

    # Trailing vol pivot
    vol_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol", values="trailing_vol", aggfunc="last"
    )

    # Supply inflation and market cap at each date
    inf_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol", values="supply_pct_13p", aggfunc="last"
    )
    cap_pivot = df.pivot_table(
        index="snapshot_date", columns="symbol", values="market_cap", aggfunc="last"
    )

    # Pre-compute 4-week forward price for each symbol at each date
    fwd_price = price_pivot.shift(-FORWARD_PRDS)
    fwd_raw   = fwd_price / price_pivot - 1

    # Cross-sectional winsorize forward returns (altcoins / all symbols)
    fwd_wins = fwd_raw.apply(lambda col: col, axis=0)  # copy
    for dt in fwd_raw.index:
        row = fwd_raw.loc[dt]
        valid = row.dropna()
        if len(valid) >= 4:
            lo = valid.quantile(0.01)
            hi = valid.quantile(0.99)
            fwd_wins.loc[dt] = row.clip(lower=lo, upper=hi)
    fwd_wins = fwd_wins.clip(lower=-1.0)

    short_rets  = {}
    long_A_rets = {}
    long_B_rets = {}
    long_C_rets = {}
    basket_sizes = []

    for date in rebal_dates:
        if date not in price_pivot.index:
            continue

        # Forward date availability check
        date_pos = snap_idx.searchsorted(date)
        if date_pos + FORWARD_PRDS >= len(all_snap_dates):
            continue  # no forward data

        # ---- Short Leg: Q4 supply inflation altcoins ----
        inf_row = inf_pivot.loc[date] if date in inf_pivot.index else pd.Series(dtype=float)
        inf_row = inf_row.dropna()

        # Exclude stablecoins from short basket
        inf_row = inf_row[~inf_row.index.isin(STABLECOINS)]

        if len(inf_row) < 8:
            continue

        q4_cut = inf_row.quantile(0.75)
        q4_syms = inf_row[inf_row >= q4_cut].index.tolist()

        if len(q4_syms) == 0:
            continue

        # Forward returns for Q4 tokens (winsorized)
        q4_fwd = fwd_wins.loc[date, [s for s in q4_syms if s in fwd_wins.columns]].dropna()
        # Subtract slippage (short side only)
        if date in slip_pivot.index:
            slip_row = slip_pivot.loc[date]
            q4_fwd = q4_fwd - slip_row.reindex(q4_fwd.index).fillna(MAX_SLIPPAGE)
        q4_fwd = q4_fwd.clip(lower=-1.0)

        if len(q4_fwd) == 0:
            continue

        # Inv-vol weights
        if date in vol_pivot.index:
            vol_row = vol_pivot.loc[date].reindex(q4_fwd.index).fillna(MIN_VOL).clip(lower=MIN_VOL)
        else:
            vol_row = pd.Series(MIN_VOL, index=q4_fwd.index)

        inv_vol = 1.0 / vol_row
        weights = inv_vol / inv_vol.sum()
        short_ret = float((weights * q4_fwd).sum())
        basket_sizes.append(len(q4_fwd))

        # ---- Long Leg A: 100% BTC ----
        btc_ret = np.nan
        if "BTC" in fwd_wins.columns and date in fwd_wins.index:
            btc_ret = float(fwd_wins.loc[date, "BTC"]) if not pd.isna(fwd_wins.loc[date, "BTC"]) else np.nan

        # ---- Long Leg B: 50% BTC + 50% ETH ----
        eth_ret = np.nan
        if "ETH" in fwd_wins.columns and date in fwd_wins.index:
            eth_ret = float(fwd_wins.loc[date, "ETH"]) if not pd.isna(fwd_wins.loc[date, "ETH"]) else np.nan
        be_ret = np.nan
        if not np.isnan(btc_ret) and not np.isnan(eth_ret):
            be_ret = 0.5 * btc_ret + 0.5 * eth_ret
        elif not np.isnan(btc_ret):
            be_ret = btc_ret
        elif not np.isnan(eth_ret):
            be_ret = eth_ret

        # ---- Long Leg C: Top 10 non-stablecoin by market cap ----
        top10_ret = np.nan
        if date in cap_pivot.index:
            cap_row = cap_pivot.loc[date].dropna()
            cap_row = cap_row[~cap_row.index.isin(STABLECOINS)]
            top10_syms = cap_row.nlargest(10).index.tolist()
            top10_fwd = fwd_wins.loc[date, [s for s in top10_syms if s in fwd_wins.columns]].dropna()
            top10_cap = cap_row.reindex(top10_fwd.index).dropna()
            top10_fwd = top10_fwd.reindex(top10_cap.index)
            if len(top10_fwd) > 0 and top10_cap.sum() > 0:
                w_cap = top10_cap / top10_cap.sum()
                top10_ret = float((w_cap * top10_fwd).sum())

        short_rets[date]  = short_ret
        long_A_rets[date] = btc_ret
        long_B_rets[date] = be_ret
        long_C_rets[date] = top10_ret

    dates_used = sorted(short_rets.keys())
    print(f"[Inputs] Rebalancing periods: {len(dates_used)}")
    if basket_sizes:
        print(f"[Inputs] Avg Q4 short basket size: {np.mean(basket_sizes):.1f} tokens")

    def _to_series(d, name):
        return pd.Series(
            {dt: d[dt] for dt in dates_used if dt in d},
            name=name,
            dtype=float,
        )

    return dict(
        short  = _to_series(short_rets,  "Short (Q4)"),
        long_A = _to_series(long_A_rets, "Long BTC"),
        long_B = _to_series(long_B_rets, "Long BTC+ETH"),
        long_C = _to_series(long_C_rets, "Long Top10"),
        dates  = dates_used,
    )


# ---------------------------------------------------------------------------
# STEP 5 -- Portfolio construction (Dollar-Neutral and Beta-Neutral)
# ---------------------------------------------------------------------------

def run_backtest(inputs: dict) -> dict:
    short_s  = inputs["short"]
    long_A_s = inputs["long_A"]
    long_B_s = inputs["long_B"]
    long_C_s = inputs["long_C"]
    dates    = inputs["dates"]

    dn_A, dn_B, dn_C = [], [], []
    bn_A, bn_B, bn_C = [], [], []
    betas_A, betas_B, betas_C = [], [], []

    s_hist, a_hist, b_hist, c_hist = [], [], [], []

    for i, date in enumerate(dates):
        s = float(short_s.get(date, np.nan))
        a = float(long_A_s.get(date, np.nan))
        b = float(long_B_s.get(date, np.nan))
        c = float(long_C_s.get(date, np.nan))

        s_hist.append(s)
        a_hist.append(a)
        b_hist.append(b)
        c_hist.append(c)

        # Dollar-Neutral: L/S = long - short, floor -1
        dn_A.append(max(a - s, -1.0) if not np.isnan(a) and not np.isnan(s) else np.nan)
        dn_B.append(max(b - s, -1.0) if not np.isnan(b) and not np.isnan(s) else np.nan)
        dn_C.append(max(c - s, -1.0) if not np.isnan(c) and not np.isnan(s) else np.nan)

        # Beta estimation from trailing history
        if i < BETA_WINDOW:
            beta_A = beta_B = beta_C = 1.0
        else:
            s_arr = np.array(s_hist[-BETA_WINDOW:], dtype=float)
            beta_A = _calc_beta(s_arr, np.array(a_hist[-BETA_WINDOW:], dtype=float))
            beta_B = _calc_beta(s_arr, np.array(b_hist[-BETA_WINDOW:], dtype=float))
            beta_C = _calc_beta(s_arr, np.array(c_hist[-BETA_WINDOW:], dtype=float))

        betas_A.append(beta_A)
        betas_B.append(beta_B)
        betas_C.append(beta_C)

        # Beta-Neutral: beta*long - short, floor -1
        bn_A.append(max(beta_A * a - s, -1.0) if not np.isnan(a) and not np.isnan(s) else np.nan)
        bn_B.append(max(beta_B * b - s, -1.0) if not np.isnan(b) and not np.isnan(s) else np.nan)
        bn_C.append(max(beta_C * c - s, -1.0) if not np.isnan(c) and not np.isnan(s) else np.nan)

    idx = pd.DatetimeIndex(dates)
    portfolios = {
        "DN-BTC":     pd.Series(dn_A, index=idx, name="DN: Long BTC"),
        "DN-BTC/ETH": pd.Series(dn_B, index=idx, name="DN: Long BTC+ETH"),
        "DN-Top10":   pd.Series(dn_C, index=idx, name="DN: Long Top10"),
        "BN-BTC":     pd.Series(bn_A, index=idx, name="BN: Long BTC"),
        "BN-BTC/ETH": pd.Series(bn_B, index=idx, name="BN: Long BTC+ETH"),
        "BN-Top10":   pd.Series(bn_C, index=idx, name="BN: Long Top10"),
    }

    print(f"\n[Beta] Avg trailing beta (short basket vs long leg):")
    print(f"  vs BTC    : {np.nanmean(betas_A):.3f}  "
          f"(min={np.nanmin(betas_A):.2f}, max={np.nanmax(betas_A):.2f})")
    print(f"  vs BTC+ETH: {np.nanmean(betas_B):.3f}  "
          f"(min={np.nanmin(betas_B):.2f}, max={np.nanmax(betas_B):.2f})")
    print(f"  vs Top10  : {np.nanmean(betas_C):.3f}  "
          f"(min={np.nanmin(betas_C):.2f}, max={np.nanmax(betas_C):.2f})")

    return portfolios


# ---------------------------------------------------------------------------
# STEP 6 -- Report and plot
# ---------------------------------------------------------------------------

def report_and_plot(portfolios: dict, inputs: dict, regime_map: dict) -> None:
    def _fmt(v):
        return f"{v:.2%}" if (v is not None and not np.isnan(v)) else "   N/A"
    def _fmtf(v):
        return f"{v:.3f}" if (v is not None and not np.isnan(v)) else "   N/A"

    print("\n[Results] Beta-Hedged L/S Strategy Performance")
    print(f"  {'Portfolio':<22} {'Ann.Return':>12} {'Volatility':>12} {'Sharpe':>10} {'MaxDD':>10}")

    sections = [
        ("Dollar-Neutral", ["DN-BTC", "DN-BTC/ETH", "DN-Top10"]),
        ("Beta-Neutral",   ["BN-BTC", "BN-BTC/ETH", "BN-Top10"]),
    ]
    for section, keys in sections:
        print(f"  --- {section} ---")
        for key in keys:
            s = _portfolio_stats(portfolios[key])
            print(
                f"  {portfolios[key].name:<22} "
                f"{_fmt(s['ann_return']):>12} {_fmt(s['volatility']):>12} "
                f"{_fmtf(s['sharpe']):>10} {_fmt(s['max_dd']):>10}"
            )

    # Also show short leg and long legs standalone for reference
    print(f"\n[Reference] Standalone Leg Performance")
    print(f"  {'Leg':<22} {'Ann.Return':>12} {'Volatility':>12} {'Sharpe':>10} {'MaxDD':>10}")
    for key, name in [("short", "Short Leg (Q4)"),
                      ("long_A", "Long BTC"),
                      ("long_B", "Long BTC+ETH"),
                      ("long_C", "Long Top10")]:
        s = _portfolio_stats(inputs[key].dropna())
        print(
            f"  {name:<22} "
            f"{_fmt(s['ann_return']):>12} {_fmt(s['volatility']):>12} "
            f"{_fmtf(s['sharpe']):>10} {_fmt(s['max_dd']):>10}"
        )

    # Build regime series for shading
    dates = inputs["dates"]
    regime_ser = pd.Series(
        [regime_map.get(d, "Bull") for d in dates],
        index=pd.DatetimeIndex(dates),
    )

    def _shade_bear(ax, reg_ser):
        in_bear, bear_start = False, None
        first_bear_done = False
        for dt, reg in reg_ser.items():
            if reg == "Bear" and not in_bear:
                bear_start, in_bear = dt, True
            elif reg != "Bear" and in_bear:
                lbl = "Bear regime" if not first_bear_done else None
                ax.axvspan(bear_start, dt, alpha=0.08, color="crimson", label=lbl)
                in_bear, first_bear_done = False, True
        if in_bear:
            ax.axvspan(bear_start, reg_ser.index[-1], alpha=0.08, color="crimson")

    colors = {
        "DN-BTC":     "royalblue",
        "DN-BTC/ETH": "darkorange",
        "DN-Top10":   "mediumseagreen",
        "BN-BTC":     "royalblue",
        "BN-BTC/ETH": "darkorange",
        "BN-Top10":   "mediumseagreen",
    }
    ls_style = {k: "-" if k.startswith("DN") else "--" for k in portfolios}

    # -- Chart 1: Dollar-Neutral --
    fig, ax = plt.subplots(figsize=(11, 6))
    for key in ["DN-BTC", "DN-BTC/ETH", "DN-Top10"]:
        s = portfolios[key].dropna()
        cum = (1 + s).cumprod()
        ax.plot(cum.index, cum.values, color=colors[key], lw=2,
                label=portfolios[key].name)
    _shade_bear(ax, regime_ser)
    ax.axhline(1, color="black", lw=0.5, ls="--")
    bear_patch = Patch(facecolor="crimson", alpha=0.15, label="Bear regime")
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles + [bear_patch], labels + ["Bear regime"], fontsize=9)
    ax.set_xlabel("Rebalance Date")
    ax.set_ylabel("Cumulative Return (1 = start)")
    ax.set_title(
        "Beta-Hedged L/S -- Dollar-Neutral (Long $1 vs Short $1)\n"
        "Short: Q4 supply inflation (inv-vol, slippage-adj)  |  "
        f"Hold: {FORWARD_PRDS} weeks"
    )
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out1 = OUTPUT_DIR + "bh_ls_dollar_neutral.png"
    fig.savefig(out1, dpi=150)
    plt.close(fig)
    print(f"\n[Chart] Saved: {out1}")

    # -- Chart 2: Beta-Neutral --
    fig, ax = plt.subplots(figsize=(11, 6))
    for key in ["BN-BTC", "BN-BTC/ETH", "BN-Top10"]:
        s = portfolios[key].dropna()
        cum = (1 + s).cumprod()
        ax.plot(cum.index, cum.values, color=colors[key], lw=2,
                label=portfolios[key].name)
    _shade_bear(ax, regime_ser)
    ax.axhline(1, color="black", lw=0.5, ls="--")
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(handles + [bear_patch], labels + ["Bear regime"], fontsize=9)
    ax.set_xlabel("Rebalance Date")
    ax.set_ylabel("Cumulative Return (1 = start)")
    ax.set_title(
        "Beta-Hedged L/S -- Beta-Neutral (Long beta*$1 vs Short $1)\n"
        "Short: Q4 supply inflation (inv-vol, slippage-adj)  |  "
        f"Hold: {FORWARD_PRDS} weeks  |  Beta window: {BETA_WINDOW} periods"
    )
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out2 = OUTPUT_DIR + "bh_ls_beta_neutral.png"
    fig.savefig(out2, dpi=150)
    plt.close(fig)
    print(f"[Chart] Saved: {out2}")

    # -- Chart 3: Combined comparison (all 6, 2-panel) --
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 10), sharex=True)
    for key, ax in [("DN-BTC", ax1), ("DN-BTC/ETH", ax1), ("DN-Top10", ax1),
                    ("BN-BTC", ax2), ("BN-BTC/ETH", ax2), ("BN-Top10", ax2)]:
        s = portfolios[key].dropna()
        cum = (1 + s).cumprod()
        ax.plot(cum.index, cum.values, color=colors[key], lw=1.8,
                ls="-", label=portfolios[key].name)
    for ax in [ax1, ax2]:
        _shade_bear(ax, regime_ser)
        ax.axhline(1, color="black", lw=0.5, ls="--")
        ax.grid(True, alpha=0.3)
        handles, labels = ax.get_legend_handles_labels()
        ax.legend(handles + [bear_patch], labels + ["Bear regime"], fontsize=8)
    ax1.set_ylabel("Cumulative Return (1 = start)")
    ax1.set_title("Dollar-Neutral (Long $1 vs Short $1)")
    ax2.set_ylabel("Cumulative Return (1 = start)")
    ax2.set_title(f"Beta-Neutral (Long beta*$1 vs Short $1, window={BETA_WINDOW})")
    ax2.set_xlabel("Rebalance Date")
    fig.suptitle(
        "Beta-Hedged L/S: Short Q4 Supply Inflation vs Long Major Assets\n"
        "Blue=BTC  Orange=BTC+ETH  Green=Top10",
        fontsize=11,
    )
    fig.tight_layout()
    out3 = OUTPUT_DIR + "bh_ls_combined.png"
    fig.savefig(out3, dpi=150)
    plt.close(fig)
    print(f"[Chart] Saved: {out3}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Beta-Hedged Long/Short Strategy Backtest")
    print("Short Q4 supply inflation vs Long BTC / BTC+ETH / Top10")
    print("=" * 60)

    df         = load_data(INPUT_FILE)
    df         = engineer_features(df)
    regime_map = compute_regime(df)
    inputs     = build_return_inputs(df)
    portfolios = run_backtest(inputs)
    report_and_plot(portfolios, inputs, regime_map)

    print("\nDone.")


if __name__ == "__main__":
    main()
