"""
perpetual_ls_v6.py
==================
Supply-Dilution L/S Strategy -- Version 6

v4 + regime-aware rebalancing frequency:

  [V6]   Rebalancing frequency is now regime-dependent:
           Bear     -> monthly   (step=1): strongest signal, max alpha capture
           Bull     -> bi-monthly (step=2): weaker signal, halve transaction costs
           Sideways -> monthly   (step=1): check every month, v4 scaling unchanged

         The BTC beta hedge forward return now spans the actual holding period
         (2 months for Bull, 1 month for Bear/Sideways) so the hedge is correct
         for each holding window.

Everything else is identical to v4:
  - Real Binance mark prices, funding, ADTV, realised vol
  - Supply signal from CMC data (13w+52w composite, winsorised)
  - Regime detection from CMC cap-weighted index
  - Inner buffer band, altcoin-season veto, circuit breaker, BTC beta hedge
  - Lo (2002) HAC-corrected Sharpe

Data prerequisites (run fetch_binance_data.py first):
  binance_perp_data/weekly_ohlcv.parquet
  binance_perp_data/weekly_funding.parquet
  binance_perp_data/symbol_meta.csv
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import warnings
warnings.filterwarnings("ignore")

# ===========================================================================
#  CONFIGURATION  (identical to v3 except funding model removed)
# ===========================================================================

INPUT_FILE   = "D:/AI_Projects/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
OUTPUT_DIR   = "D:/AI_Projects/circ_supply/"
BN_DIR       = "D:/AI_Projects/circ_supply/binance_perp_data/"

# Universe
MAX_RANK             = 200
TOP_N_EXCLUDE        = 20
MIN_VOLUME           = 5_000_000       # Binance quote_volume / 7 (weekly → daily proxy)
MIN_MKTCAP           = 50_000_000
MIN_SUPPLY_HISTORY   = 26
FFILL_LIMIT          = 1

# Signal
SUPPLY_WINDOW        = 13
SUPPLY_WINDOW_SLOW   = 52
SIGNAL_SLOW_WEIGHT   = 0.50
SUPPLY_INF_WINS      = (0.02, 0.98)

# Portfolio
LONG_ENTRY_PCT       = 0.12
LONG_EXIT_PCT        = 0.18
SHORT_ENTRY_PCT      = 0.88
SHORT_EXIT_PCT       = 0.82
MIN_BASKET_SIZE      = 6
ADTV_POS_CAP         = 0.20
TOKEN_VOL_WINDOW     = 8

# Execution
TAKER_FEE            = 0.0004
SLIPPAGE_K           = 0.0005
MIN_TURNOVER         = 0.001
MAX_SLIPPAGE         = 0.02

# Regime
REGIME_MA_WINDOW     = 20
BULL_BAND            = 1.10
BEAR_BAND            = 0.90
HIGH_VOL_THRESHOLD   = 0.80
VOL_WINDOW           = 8

REGIME_LS_SCALE = {
    ("Sideways", False): (1.00, 1.00),
    ("Sideways", True):  (1.00, 0.75),
    ("Bull",     False): (1.00, 0.50),
    ("Bull",     True):  (0.75, 0.25),
    ("Bear",     False): (0.75, 0.75),
    ("Bear",     True):  (0.50, 0.25),
}

# [V6] Rebalancing step (in months) per regime
REBAL_STEP = {
    "Bear":     1,   # monthly  — strongest signal, maximise alpha capture
    "Bull":     2,   # bi-monthly — weaker signal, halve transaction costs
    "Sideways": 1,   # monthly  — keep checking; v4 scaling applies each month
}

ALTSEASON_THRESHOLD  = 0.75
ALTSEASON_LOOKBACK   = 4
SHORT_SQUEEZE_PRIOR  = 0.40
SHORT_CB_LOSS        = 0.40
BTC_HEDGE_ENABLED    = True
BTC_HEDGE_LOOKBACK   = 12
BTC_HEDGE_MAX        = 1.0

START_DATE  = pd.Timestamp("2022-01-01")
WINS_LOW    = 0.01
WINS_HIGH   = 0.99

# ===========================================================================
#  EXCLUSION LISTS
# ===========================================================================

STABLECOINS = {
    "USDT","USDC","BUSD","DAI","TUSD","USDP","GUSD","FRAX","LUSD","MIM",
    "USDN","USTC","UST","HUSD","SUSD","PAX","USDS","USDJ","NUSD","USDK",
    "USDX","CUSD","CEUR","USDH","USDD","FDUSD","PYUSD","EURC","EURS",
    "USDQ","USDB","USDTB",
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
WRAPPED_ASSETS = {
    "WBTC","BTCB","BBTC","RBTC","rBTC","FBTC",
    "UNIBTC","PUMPBTC","EBTC","LBTC","SolvBTC","xSolvBTC",
    "WETH","WBNB","renBTC","renETH","renDOGE","renZEC","BTC.b","SolvBTC.BBN",
}
LIQUID_STAKING = {
    "STETH","RETH","CBETH","ANKRETH","FRXETH",
    "OSETH","LSETH","METH","EZETH","EETH",
    "SFRXETH","CMETH","BETH","TETH","PZETH",
    "ETHX","PUFETH","RSETH",
    "JITOSOL","MSOL","BNSOL","JUPSOL","BSOL","BBSOL","JSOL",
    "SAVAX","sAVAX","STRX","HASUI","KHYPE",
    "slisBNB","WBETH","stkAAVE","STKAAVE",
}
PROTOCOL_SYNTHETICS = {"vBTC","vETH","vBNB","vXVS","VRT","VTHO"}
COMMODITY_BACKED    = {"PAXG","XAUT","XAUt","KAU"}

EXCLUDED = (STABLECOINS | CEX_TOKENS | MEMECOINS
            | WRAPPED_ASSETS | LIQUID_STAKING
            | PROTOCOL_SYNTHETICS | COMMODITY_BACKED)

# ===========================================================================
#  HELPERS  (identical to v3)
# ===========================================================================

def _ppy(returns: pd.Series) -> float:
    if len(returns) < 2:
        return 12.0
    gaps = np.diff(returns.index).astype("timedelta64[D]").astype(float)
    return 365.25 / max(float(np.median(gaps)), 1.0)


def sharpe_lo(returns: pd.Series, ppy: float, max_lags: int = 4) -> float:
    r = returns.dropna()
    if len(r) < max_lags + 2 or r.std() == 0:
        return np.nan
    sr_raw = r.mean() / r.std()
    ac = [float(r.autocorr(lag=q)) for q in range(1, max_lags + 1)]
    w  = [1.0 - q / (max_lags + 1) for q in range(1, max_lags + 1)]
    correction = 1.0 + 2.0 * sum(a * wi for a, wi in zip(ac, w))
    return sr_raw * np.sqrt(ppy) / np.sqrt(max(correction, 1e-8))


def portfolio_stats(returns: pd.Series) -> dict:
    returns = returns.dropna()
    if len(returns) < 2:
        nan = np.nan
        return dict(ann_return=nan, vol=nan, sharpe=nan, sharpe_lo=nan,
                    sortino=nan, max_dd=nan)
    cum         = (1 + returns).cumprod()
    total_years = max((returns.index[-1] - returns.index[0]).days / 365.25, 1/52)
    cum_final   = float(cum.iloc[-1])
    ann_return  = (cum_final ** (1 / total_years) - 1) if cum_final > 0 else np.nan
    ppy         = _ppy(returns)
    vol         = returns.std() * np.sqrt(ppy)
    sharpe      = ann_return / vol if vol > 0 and not np.isnan(ann_return) else np.nan
    slo         = sharpe_lo(returns, ppy)
    down        = returns[returns < 0]
    sortino     = (ann_return / (np.sqrt((down**2).mean()) * np.sqrt(ppy))
                   if len(down) > 0 else np.nan)
    roll_max    = cum.cummax()
    max_dd      = float(((cum - roll_max) / roll_max).min())
    return dict(ann_return=ann_return, vol=vol, sharpe=sharpe, sharpe_lo=slo,
                sortino=sortino, max_dd=max_dd)


def _fmt(v):      return f"{v:+.2%}"  if not np.isnan(v) else "    N/A"
def _fmtf(v, d=3): return f"{v:+.{d}f}" if not np.isnan(v) else "    N/A"


def get_ls_scales(regime: str, high_vol: bool) -> tuple:
    return REGIME_LS_SCALE.get((regime, high_vol),
           REGIME_LS_SCALE.get((regime, False), (1.0, 1.0)))


def inv_vol_adtv_weights(symbols: list, vol_map: dict, adtv_map: dict,
                          cap: float = ADTV_POS_CAP) -> dict:
    raw = {}
    for s in symbols:
        vol  = max(float(vol_map.get(s)  if pd.notna(vol_map.get(s))  else 1.0), 0.05)
        adtv = max(float(adtv_map.get(s) if pd.notna(adtv_map.get(s)) else 0.0), 0.0)
        raw[s] = (adtv ** 0.5 + 1.0) / vol
    total = sum(raw.values())
    if total <= 0:
        n = len(symbols); return {s: 1/n for s in symbols}
    w = {k: v/total for k, v in raw.items()}
    capped = {k: min(v, cap) for k, v in w.items()}
    total2 = sum(capped.values())
    if total2 <= 0:
        n = len(symbols); return {s: 1/n for s in symbols}
    return {k: v/total2 for k, v in capped.items()}


# ===========================================================================
#  STEP 1 -- Load CMC data
# ===========================================================================

def load_cmc(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["snapshot_date"])
    df = df[df["symbol"].apply(lambda s: str(s).isascii())]  # drop non-ASCII
    df = df.sort_values(["symbol", "snapshot_date"]).reset_index(drop=True)
    df = df[df["price"].notna() & (df["price"] > 0) & df["market_cap"].notna()].copy()
    for col in ["price", "circulating_supply"]:
        df[col] = df.groupby("symbol")[col].transform(lambda s: s.ffill(limit=FFILL_LIMIT))
    df = df[df["circulating_supply"].notna() & (df["circulating_supply"] > 0)].copy()
    print(f"[CMC] {len(df):,} rows | {df['symbol'].nunique():,} symbols | "
          f"{df['snapshot_date'].min().date()} -> {df['snapshot_date'].max().date()}")
    return df


# ===========================================================================
#  STEP 2 -- Load Binance data  [V4-A / V4-B / V4-C]
# ===========================================================================

def load_binance(bn_dir: str) -> tuple:
    """
    Returns:
        bn_price_piv  — DataFrame[cmc_date x symbol] of Binance weekly close (perp price)
        bn_adtv_piv   — DataFrame[cmc_date x symbol] of Binance weekly USDT quote_volume
        bn_tokv_piv   — DataFrame[cmc_date x symbol] of 8w rolling annualised realised vol
        bn_fund_piv   — DataFrame[cmc_date x symbol] of weekly funding_sum (actual 8h rates)
        onboard_map   — dict[symbol -> onboard_date]

    Alignment note:
      Binance 1w candles start on Monday.  CMC snapshots are on Sundays.
      A candle with week_start = Monday t covers Mon t → Sun t+6.
      Its close ≈ CMC snapshot on Sunday = week_start + 6 days.
      So:  cmc_date = week_start + 6 days  (always a Sunday)

    Funding alignment:
      A position held from Sunday t0 to Sunday t1 (= t0 + 7 days) pays/receives
      the funding rates during that week.  These are captured in the Binance
      weekly funding row whose week_start = t0 + 1 day (Monday), i.e.
      cmc_date = t1.  So funding for period (t0 → t1) = bn_fund_piv.loc[t1].
    """
    ohlcv   = pd.read_parquet(f"{bn_dir}/weekly_ohlcv.parquet")
    funding = pd.read_parquet(f"{bn_dir}/weekly_funding.parquet")
    meta    = pd.read_csv(f"{bn_dir}/symbol_meta.csv", parse_dates=["onboard_date"])

    # Align to CMC (Sunday) dates
    ohlcv["cmc_date"]   = ohlcv["week_start"]   + pd.Timedelta(days=6)
    funding["cmc_date"] = funding["week_start"]  + pd.Timedelta(days=6)

    # Price pivot (perp closes)
    bn_price_piv = ohlcv.pivot_table(
        index="cmc_date", columns="symbol", values="close", aggfunc="last")

    # ADTV pivot (weekly USDT quote volume)
    bn_adtv_piv = ohlcv.pivot_table(
        index="cmc_date", columns="symbol", values="quote_volume", aggfunc="last")

    # Realised vol: 8-week rolling std of weekly returns from Binance closes
    # Build a time-sorted close DF, compute pct_change per symbol, roll std
    close_df = (ohlcv.sort_values("cmc_date")
                     .pivot_table(index="cmc_date", columns="symbol",
                                  values="close", aggfunc="last"))
    ret_df = close_df.pct_change(1)
    tokv_df = ret_df.rolling(TOKEN_VOL_WINDOW, min_periods=4).std() * np.sqrt(52)
    bn_tokv_piv = tokv_df

    # Funding: keep raw (symbol, week_start, funding_sum) for range-sum lookups.
    # Do NOT try to align to cmc_date here — the funding week_start uses a
    # different period convention (W-SAT) than the OHLCV 1w candles (Monday).
    # We will sum over all rows where week_start falls within the holding period.
    bn_fund_raw = funding[["symbol", "week_start", "funding_sum"]].copy()
    # Dummy pivot for column reference only; actual lookup done in loop
    bn_fund_piv = bn_fund_raw  # pass through; renamed below for clarity

    onboard_map = dict(zip(meta["symbol"], meta["onboard_date"]))

    bn_fund_raw = bn_fund_piv   # rename for clarity in caller
    print(f"[Binance] price: {bn_price_piv.shape}  "
          f"funding rows: {len(bn_fund_raw):,}  "
          f"symbols w/ onboard: {len(onboard_map)}")
    return bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_raw, onboard_map


# ===========================================================================
#  STEP 3 -- Regime (identical to v3, uses CMC data)
# ===========================================================================

def build_regime(df: pd.DataFrame) -> pd.DataFrame:
    top = df[df["rank"] <= 100].copy().sort_values(["symbol", "snapshot_date"])
    top["pct_ret"] = top.groupby("symbol")["price"].pct_change(1)
    top = top[top["pct_ret"].notna()]

    def cap_wt(g):
        total = g["market_cap"].sum()
        return float((g["market_cap"] / total * g["pct_ret"]).sum()) if total > 0 else np.nan

    idx = (top.groupby("snapshot_date")
              .apply(cap_wt, include_groups=False)
              .reset_index().rename(columns={0: "index_return"})
              .sort_values("snapshot_date"))

    idx["index_price"] = (1 + idx["index_return"].fillna(0)).cumprod()
    idx["index_ma"]    = idx["index_price"].rolling(REGIME_MA_WINDOW, min_periods=1).mean()
    ratio              = idx["index_price"] / idx["index_ma"]
    idx["regime"]      = np.where(ratio >= BULL_BAND, "Bull",
                         np.where(ratio <= BEAR_BAND, "Bear", "Sideways"))

    btc_rets       = df[df["symbol"] == "BTC"].set_index("snapshot_date")["price"].pct_change(1)
    btc_vol_series = (btc_rets.reindex(idx["snapshot_date"])
                               .rolling(VOL_WINDOW, min_periods=4).std() * np.sqrt(52))
    idx["btc_vol_8w"] = btc_vol_series.values
    idx["high_vol"]   = idx["btc_vol_8w"] > HIGH_VOL_THRESHOLD

    n_bull = (idx["regime"] == "Bull").sum()
    n_bear = (idx["regime"] == "Bear").sum()
    n_side = (idx["regime"] == "Sideways").sum()
    print(f"[Regime] Bull={n_bull} Bear={n_bear} Sideways={n_side} "
          f"HighVol={idx['high_vol'].sum()}")
    return idx[["snapshot_date", "index_return", "regime", "high_vol"]]


# ===========================================================================
#  STEP 4 -- Feature engineering (supply signal from CMC, costs from Binance)
# ===========================================================================

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    grp = df.groupby("symbol", group_keys=False)

    df["supply_inf_13w"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW))
    df["supply_inf_52w"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW_SLOW))
    df["supply_inf"] = df["supply_inf_13w"]

    df["supply_hist_count"] = grp["supply_inf"].transform(
        lambda s: s.notna().cumsum())

    # Slippage proxy (kept from v3 as fallback for tokens with thin Binance data)
    df["turnover"] = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"] = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)

    return df


# ===========================================================================
#  STEP 5 -- Main backtest loop
# ===========================================================================

def run_backtest(df: pd.DataFrame, regime_df: pd.DataFrame,
                 bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_piv,
                 onboard_map: dict) -> dict:

    df["ym"]   = df["snapshot_date"].dt.to_period("M")
    all_rebal  = sorted(df.groupby("ym")["snapshot_date"].min().tolist())
    regime_map = regime_df.set_index("snapshot_date")[["regime","high_vol"]].to_dict("index")

    # Eligible universe snapshot (CMC supply filter, START_DATE applied in loop)
    inf_snap = df[df["snapshot_date"].isin(all_rebal)].copy()
    inf_snap = inf_snap[inf_snap["supply_inf_13w"].notna()]
    inf_snap = inf_snap[~inf_snap["symbol"].isin(EXCLUDED)]
    inf_snap = inf_snap[(inf_snap["rank"] > TOP_N_EXCLUDE) & (inf_snap["rank"] <= MAX_RANK)]
    inf_snap = inf_snap[inf_snap["market_cap"]  >= MIN_MKTCAP]
    inf_snap = inf_snap[inf_snap["supply_hist_count"] >= MIN_SUPPLY_HISTORY]

    # [V4-E] Restrict to tokens with an active Binance perp
    bn_symbols = set(bn_price_piv.columns)
    inf_snap = inf_snap[inf_snap["symbol"].isin(bn_symbols)]

    # CMC price pivot (for altcoin-season veto only — still uses spot prices for the
    # regime-level alt-rotation check, not for return computation)
    price_piv_cmc = df.pivot_table(index="snapshot_date", columns="symbol",
                                   values="price", aggfunc="last")
    slip_piv = df.pivot_table(index="snapshot_date", columns="symbol",
                              values="slippage", aggfunc="last")

    # Altcoin-season pre-computation (uses CMC spot prices for regime-level alt check)
    altseason_map = {}
    for i, t0r in enumerate(all_rebal):
        if i < ALTSEASON_LOOKBACK or t0r not in price_piv_cmc.index:
            altseason_map[t0r] = False; continue
        t_lb = all_rebal[i - ALTSEASON_LOOKBACK]
        if t_lb not in price_piv_cmc.index:
            altseason_map[t0r] = False; continue
        p_lb  = price_piv_cmc.loc[t_lb]
        p_now = price_piv_cmc.loc[t0r]
        btc_p0, btc_p1 = float(p_lb.get("BTC", np.nan)), float(p_now.get("BTC", np.nan))
        if pd.isna(btc_p0) or btc_p0 <= 0 or pd.isna(btc_p1):
            altseason_map[t0r] = False; continue
        btc_4w = btc_p1 / btc_p0 - 1
        top50  = df[(df["snapshot_date"] == t0r) &
                    (df["rank"].between(3, 50)) &
                    (~df["symbol"].isin(EXCLUDED | {"BTC", "ETH"}))]["symbol"].tolist()
        alt_rets = [p_now.get(s, np.nan)/p_lb.get(s, np.nan)-1
                    for s in top50
                    if pd.notna(p_lb.get(s)) and p_lb.get(s, 0) > 0 and pd.notna(p_now.get(s))]
        if len(alt_rets) < 10:
            altseason_map[t0r] = False; continue
        altseason_map[t0r] = sum(r > btc_4w for r in alt_rets) / len(alt_rets) > ALTSEASON_THRESHOLD

    # [V6] Build regime-aware rebalancing schedule:
    # Walk monthly dates stepping REBAL_STEP[regime] months at a time.
    all_eligible = [d for d in all_rebal if d >= START_DATE] if START_DATE else all_rebal
    active_rebals: list = []
    idx_step = 0
    while idx_step < len(all_eligible):
        t_cur = all_eligible[idx_step]
        active_rebals.append(t_cur)
        reg_cur = regime_map.get(t_cur, {}).get("regime", "Sideways")
        step    = REBAL_STEP.get(reg_cur, 1)
        idx_step += step
    sorted_rebals = active_rebals

    # [V6] Recompute BTC forward returns over ACTUAL holding periods
    # (spans 2 months for Bull periods, 1 month for Bear/Sideways)
    btc_fwd = {}
    for ii in range(len(sorted_rebals) - 1):
        t0r, t1r = sorted_rebals[ii], sorted_rebals[ii + 1]
        p0 = float(bn_price_piv.loc[t0r, "BTC"]) if (t0r in bn_price_piv.index
             and "BTC" in bn_price_piv.columns) else np.nan
        p1 = float(bn_price_piv.loc[t1r, "BTC"]) if (t1r in bn_price_piv.index
             and "BTC" in bn_price_piv.columns) else np.nan
        btc_fwd[t0r] = (p1 / p0 - 1) if pd.notna(p0) and pd.notna(p1) and p0 > 0 else np.nan

    # State
    prev_long_set  = set()
    prev_short_set = set()

    # Accumulators
    (dates_out, long_gross_l, short_gross_l,
     long_net_l, short_net_l, combined_net_l, combined_hedged_l,
     basket_sizes_l, regime_out_l, scale_out_l,
     fund_long_cum, fund_short_cum,
     cb_count, altseason_count,
     turnover_long_l, turnover_short_l,
     beta_hist_l, raw_hist_l, btc_hist_l,
     fund_actual_long_l, fund_actual_short_l) = (
        [], [], [], [], [], [], [],
        [], [], [],
        0.0, 0.0, 0, 0,
        [], [],
        [], [], [],
        [], []
    )

    for i, t0 in enumerate(sorted_rebals[:-1]):
        t1        = sorted_rebals[i + 1]
        hold_days = max((t1 - t0).days, 1)

        # Check Binance data available for this period
        if t0 not in bn_price_piv.index or t1 not in bn_price_piv.index:
            continue

        univ = inf_snap[inf_snap["snapshot_date"] == t0].copy()

        # [V4-E] Only tokens that had Binance perp listed BEFORE this rebalancing date
        univ = univ[univ["symbol"].apply(
            lambda s: pd.notna(onboard_map.get(s)) and onboard_map.get(s) <= t0)]

        # [V4-C] Liquidity filter using Binance quote_volume (weekly USDT volume)
        # MIN_VOLUME is a 24h proxy → weekly threshold = MIN_VOLUME * 7
        if t0 in bn_adtv_piv.index:
            adtv_now = bn_adtv_piv.loc[t0]
            univ = univ[univ["symbol"].apply(
                lambda s: pd.notna(adtv_now.get(s)) and float(adtv_now.get(s, 0)) >= MIN_VOLUME * 7)]

        if len(univ) < MIN_BASKET_SIZE * 2:
            continue

        # [V3-2] Cross-sectional winsorisation
        for col in ["supply_inf_13w", "supply_inf_52w"]:
            col_vals = univ[col].dropna()
            if len(col_vals) > 4:
                lo_w, hi_w = col_vals.quantile(SUPPLY_INF_WINS)
                univ[col]  = univ[col].clip(lo_w, hi_w)

        # [V3-1] Composite signal rank
        univ["rank_13w"] = univ["supply_inf_13w"].rank(pct=True)
        univ["rank_52w"] = univ["supply_inf_52w"].rank(pct=True)
        univ["rank_52w"] = univ["rank_52w"].fillna(univ["rank_13w"])
        univ["pct_rank"] = ((1 - SIGNAL_SLOW_WEIGHT) * univ["rank_13w"]
                            + SIGNAL_SLOW_WEIGHT      * univ["rank_52w"])

        rank_map = univ.set_index("symbol")["pct_rank"].to_dict()
        all_syms = set(univ["symbol"])

        # [V3-5] Squeeze exclusion using Binance prices [V4-A]
        squeezed = set()
        if i > 0:
            t_prev_r = sorted_rebals[i - 1]
            if t_prev_r in bn_price_piv.index:
                p_prev = bn_price_piv.loc[t_prev_r]
                p_now  = bn_price_piv.loc[t0]
                squeezed = {s for s in all_syms
                            if (s in p_prev and s in p_now
                                and pd.notna(p_prev[s]) and float(p_prev[s]) > 0
                                and pd.notna(p_now[s])
                                and float(p_now[s])/float(p_prev[s]) - 1 > SHORT_SQUEEZE_PRIOR)}

        # Inner buffer band
        entry_long   = {s for s in all_syms if rank_map[s] <= LONG_ENTRY_PCT}
        stay_long    = {s for s in (prev_long_set & all_syms) if rank_map[s] <= LONG_EXIT_PCT}
        basket_long  = entry_long | stay_long

        entry_short  = {s for s in all_syms if rank_map[s] >= SHORT_ENTRY_PCT} - squeezed
        stay_short   = {s for s in (prev_short_set & all_syms) if rank_map[s] >= SHORT_EXIT_PCT}
        basket_short = entry_short | stay_short

        overlap      = basket_long & basket_short
        basket_long  -= overlap
        basket_short -= overlap

        if len(basket_long) < MIN_BASKET_SIZE or len(basket_short) < MIN_BASKET_SIZE:
            prev_long_set  = basket_long
            prev_short_set = basket_short
            continue

        # Turnover tracking
        to_l = (1 - len(basket_long & prev_long_set) /
                max(len(basket_long | prev_long_set), 1)) if prev_long_set else 1.0
        to_s = (1 - len(basket_short & prev_short_set) /
                max(len(basket_short | prev_short_set), 1)) if prev_short_set else 1.0
        turnover_long_l.append(to_l)
        turnover_short_l.append(to_s)

        prev_long_set  = basket_long
        prev_short_set = basket_short

        # [V4-A] Forward returns from Binance perp prices
        p0_bn = bn_price_piv.loc[t0]
        p1_bn = bn_price_piv.loc[t1]
        fwd   = (p1_bn / p0_bn - 1).dropna()

        lo_f, hi_f = fwd.quantile(WINS_LOW), fwd.quantile(WINS_HIGH)
        fwd = fwd.clip(lower=lo_f, upper=hi_f).clip(lower=-1.0)

        # [V4-B] Actual funding: sum ALL weekly funding rows whose week_start falls
        # within the holding period (t0, t1].  The funding DataFrame uses W-SAT
        # period convention so week_start values are Sundays — different from the
        # OHLCV Monday week_start.  We therefore do a range query instead of an
        # exact-date lookup, tolerating any 1-day offset.
        fund_mask = (bn_fund_piv["week_start"] > t0) & \
                    (bn_fund_piv["week_start"] <= t1 + pd.Timedelta(days=1))
        fund_rows = bn_fund_piv[fund_mask]
        if len(fund_rows) > 0:
            fund_row = fund_rows.groupby("symbol")["funding_sum"].sum()
        else:
            fund_row = pd.Series(dtype=float)

        # [V4-C / V4-D] Position sizing: Binance ADTV and Binance realised vol
        adtv_row = bn_adtv_piv.loc[t0] if t0 in bn_adtv_piv.index else pd.Series(dtype=float)
        tokv_row = bn_tokv_piv.loc[t0] if t0 in bn_tokv_piv.index else pd.Series(dtype=float)
        sl_row   = slip_piv.loc[t0]    if t0 in slip_piv.index    else pd.Series(dtype=float)

        def basket_return(symbols: set) -> tuple:
            syms = [s for s in symbols if s in fwd.index and not pd.isna(fwd[s])]
            if not syms:
                return np.nan, MAX_SLIPPAGE
            vol_m  = {s: float(tokv_row.get(s, 1.0) if pd.notna(tokv_row.get(s)) else 1.0)
                      for s in syms}
            adtv_m = {s: float(adtv_row.get(s, 0)  if pd.notna(adtv_row.get(s))  else 0.0)
                      for s in syms}
            w      = inv_vol_adtv_weights(syms, vol_m, adtv_m)
            ret    = sum(w[s] * float(fwd[s]) for s in syms)
            slip   = sum(w[s] * float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)
                         for s in syms)
            # [V4-B] Actual funding for this basket (sum of weighted 8h rates over period)
            fund_sum = sum(w[s] * float(fund_row[s] if s in fund_row.index and
                                        pd.notna(fund_row[s]) else 0.0) for s in syms)
            return float(ret), float(slip), float(fund_sum)

        r_long_gross,  slip_long,  fund_long_basket  = basket_return(basket_long)
        r_short_gross, slip_short, fund_short_basket = basket_return(basket_short)

        if pd.isna(r_long_gross) or pd.isna(r_short_gross):
            continue

        # [V3-5] Circuit breaker
        cb_hit = False
        if r_short_gross > SHORT_CB_LOSS:
            r_short_gross = SHORT_CB_LOSS
            cb_hit = True
            cb_count += 1

        # Regime & L/S scaling
        reg_info  = regime_map.get(t0, {"regime": "Sideways", "high_vol": False})
        regime    = reg_info.get("regime",   "Sideways")
        high_vol  = bool(reg_info.get("high_vol", False))
        long_scale, short_scale = get_ls_scales(regime, high_vol)

        if altseason_map.get(t0, False):
            short_scale   = 0.0
            altseason_count += 1

        # Costs
        fee_cost = 2 * TAKER_FEE

        # [V4-B] Actual funding:
        #   Long position: you PAY if fund_long_basket > 0 (receive if < 0)
        #   Short position: you RECEIVE if fund_short_basket > 0 (pay if < 0)
        actual_fund_long_drag  = -fund_long_basket      # negative = drag when positive
        actual_fund_short_cred = +fund_short_basket     # positive = credit when positive

        r_long_net  = r_long_gross  - fee_cost - slip_long  + actual_fund_long_drag
        r_short_net = -r_short_gross - fee_cost - slip_short + actual_fund_short_cred

        denom      = long_scale + short_scale if (long_scale + short_scale) > 0 else 1.0
        r_combined = (long_scale * r_long_net + short_scale * r_short_net) / denom

        r_long_net  = max(r_long_net,  -1.0)
        r_short_net = max(r_short_net, -1.0)
        r_combined  = max(r_combined,  -1.0)

        # [V3-7] Rolling BTC beta hedge
        hedge_ret = 0.0
        beta_used = np.nan
        if BTC_HEDGE_ENABLED and len(raw_hist_l) >= BTC_HEDGE_LOOKBACK:
            hist_c = raw_hist_l[-BTC_HEDGE_LOOKBACK:]
            hist_b = btc_hist_l[-BTC_HEDGE_LOOKBACK:]
            pairs  = [(c, b) for c, b in zip(hist_c, hist_b)
                      if not np.isnan(c) and not np.isnan(b)]
            if len(pairs) >= 4:
                c_arr = np.array([p[0] for p in pairs])
                b_arr = np.array([p[1] for p in pairs])
                var_b = np.var(b_arr)
                if var_b > 1e-10:
                    beta      = float(np.clip(np.cov(c_arr, b_arr)[0, 1] / var_b,
                                              0.0, BTC_HEDGE_MAX))
                    btc_r     = btc_fwd.get(t0, 0.0) or 0.0
                    hedge_ret = -beta * btc_r
                    beta_used = beta

        raw_hist_l.append(r_combined)
        btc_hist_l.append(btc_fwd.get(t0, np.nan))
        beta_hist_l.append(beta_used)

        r_combined_hedged = max(r_combined + hedge_ret, -1.0)

        # Funding attribution accumulators
        fund_long_cum  += actual_fund_long_drag
        fund_short_cum += actual_fund_short_cred
        fund_actual_long_l.append(actual_fund_long_drag)
        fund_actual_short_l.append(actual_fund_short_cred)

        dates_out.append(t0)
        long_gross_l.append(max(r_long_gross,  -1.0))
        short_gross_l.append(max(r_short_gross, -1.0))
        long_net_l.append(r_long_net)
        short_net_l.append(r_short_net)
        combined_net_l.append(r_combined)
        combined_hedged_l.append(r_combined_hedged)
        basket_sizes_l.append((len(basket_long), len(basket_short)))
        regime_out_l.append(regime)
        scale_out_l.append((long_scale, short_scale))

    idx = pd.DatetimeIndex(dates_out)
    return dict(
        index            = idx,
        dates            = dates_out,
        long_gross       = pd.Series(long_gross_l,      index=idx, name="Long Gross"),
        short_gross      = pd.Series(short_gross_l,     index=idx, name="Short Gross"),
        long_net         = pd.Series(long_net_l,        index=idx, name="Long Net"),
        short_net        = pd.Series(short_net_l,       index=idx, name="Short Net"),
        combined_net     = pd.Series(combined_net_l,    index=idx, name="Combined Net"),
        combined_hedged  = pd.Series(combined_hedged_l, index=idx, name="Combined Hedged"),
        spread_gross     = pd.Series(
            [lg - sg for lg, sg in zip(long_gross_l, short_gross_l)],
            index=idx, name="Spread Gross"),
        basket_sizes     = basket_sizes_l,
        regime           = regime_out_l,
        scale            = scale_out_l,
        fund_long_cum    = fund_long_cum,
        fund_short_cum   = fund_short_cum,
        fund_actual_long = fund_actual_long_l,
        fund_actual_short= fund_actual_short_l,
        cb_count         = cb_count,
        altseason_count  = altseason_count,
        beta_hist        = beta_hist_l,
        turnover_long    = turnover_long_l,
        turnover_short   = turnover_short_l,
    )


# ===========================================================================
#  STEP 6 -- Report
# ===========================================================================

def print_report(res: dict) -> None:
    print("\n" + "=" * 76)
    print("PERPETUAL L/S BACKTEST v6  [REGIME-AWARE REBALANCING]")
    print("  Signal : 50% rank(13w) + 50% rank(52w), winsorised 2-98pct")
    print("  Prices : Binance USDT-M weekly perp close (not CMC spot)")
    print("  Funding: Actual 8h Binance funding rates (not synthetic model)")
    print("  Weights: inv-vol x sqrt(ADTV), 20% cap  (Binance vol/volume)")
    print(f"  Rebal  : Bear={REBAL_STEP['Bear']}mo  Bull={REBAL_STEP['Bull']}mo  Sideways={REBAL_STEP['Sideways']}mo")
    print("=" * 76)

    n      = len(res["dates"])
    avg_lo = np.mean([s[0] for s in res["basket_sizes"]])
    avg_hi = np.mean([s[1] for s in res["basket_sizes"]])
    regs   = np.array(res["regime"])
    scales = res["scale"]
    avg_ls = (np.mean([s[0] for s in scales]), np.mean([s[1] for s in scales]))
    valid_b = [b for b in res["beta_hist"] if not np.isnan(b)]

    print(f"\n  Rebalancing periods : {n}")
    print(f"  Avg basket size     : Long {avg_lo:.1f} | Short {avg_hi:.1f} tokens")
    print(f"  Regime breakdown    : Bull={(regs=='Bull').sum()}  "
          f"Bear={(regs=='Bear').sum()}  Sideways={(regs=='Sideways').sum()}")
    print(f"  Avg effective scale : Long {avg_ls[0]:.2f}x / Short {avg_ls[1]:.2f}x")
    print(f"  CB triggered        : {res['cb_count']} period(s)")
    print(f"  Alt-season veto     : {res['altseason_count']} period(s)")
    if valid_b:
        print(f"  Avg rolling beta    : {np.mean(valid_b):.3f}  "
              f"(range {np.min(valid_b):.3f} -- {np.max(valid_b):.3f})")
    if res["turnover_long"]:
        print(f"  Avg monthly turnover: Long {np.mean(res['turnover_long']):.1%}  "
              f"Short {np.mean(res['turnover_short']):.1%}")

    print(f"\n  {'Series':<34} {'Ann.Ret':>9} {'Vol':>9} "
          f"{'Sharpe':>7} {'Sharpe*':>8} {'Sortino':>8} {'MaxDD':>9}")
    print("  " + "-" * 76)
    for name, s in [
        ("Long basket  (gross)",      res["long_gross"]),
        ("Short basket (gross)",      res["short_gross"]),
        ("Long leg     (net)",        res["long_net"]),
        ("Short leg    (net)^",       res["short_net"]),
        ("L/S Combined (net)",        res["combined_net"]),
        ("L/S Combined (BTC-hedged)", res["combined_hedged"]),
    ]:
        st = portfolio_stats(s)
        print(f"  {name:<34} {_fmt(st['ann_return']):>9} {_fmt(st['vol']):>9} "
              f"{_fmtf(st['sharpe']):>7} {_fmtf(st['sharpe_lo']):>8} "
              f"{_fmtf(st['sortino']):>8} {_fmt(st['max_dd']):>9}")
    print("  * Lo (2002) HAC-corrected Sharpe   ^ Short net: +ve = profit for short")

    sp = res["spread_gross"]
    print(f"\n  Win rate (Long > Short, gross) : {(sp>0).sum()}/{len(sp)} ({(sp>0).mean():.1%})")
    print(f"  Mean period spread (gross)     : {sp.mean():.2%}")
    print(f"  Spread ann. vol                : {_fmt(portfolio_stats(sp)['vol'])}")
    print(f"  Spread excess kurtosis         : {sp.kurtosis():.2f}")
    print(f"  Spread skewness                : {sp.skew():.2f}")

    print(f"\n  --- Regime-Conditional Spread (gross) ---")
    print(f"  {'Regime':<12} {'N':>4} {'Mean Spread':>13} {'Win Rate':>10} "
          f"{'Ann.Geo.Spread':>16}")
    for regime in ["Bull", "Bear", "Sideways"]:
        mask  = regs == regime
        if mask.sum() == 0: continue
        sub   = sp.iloc[list(np.where(mask)[0])]
        n_sub = len(sub)
        cum   = (1 + sub.clip(lower=-0.99)).prod()
        ann   = cum ** (12 / n_sub) - 1 if n_sub > 0 else np.nan
        print(f"  {regime:<12} {n_sub:>4}   {sub.mean():>+12.2%}   "
              f"{(sub>0).mean():>8.1%}   {ann:>+14.2%}")

    # Actual funding breakdown
    fl  = res["fund_actual_long"]
    fs  = res["fund_actual_short"]
    fcl = res["fund_long_cum"]
    fcs = res["fund_short_cum"]
    print(f"\n  --- Actual Funding Rate Attribution (Binance data) ---")
    print(f"  Cum. funding impact (long leg)  : {fcl:+.4f} ({fcl:.2%})")
    print(f"  Cum. funding impact (short leg) : {fcs:+.4f} ({fcs:.2%})")
    net_f = fcl + fcs
    print(f"  Net funding impact              : {net_f:+.4f} ({net_f:.2%})")
    if fl:
        print(f"  Avg period long funding drag    : {np.mean(fl):+.4f} ({np.mean(fl):.2%})")
        print(f"  Avg period short funding credit : {np.mean(fs):+.4f} ({np.mean(fs):.2%})")
    print("=" * 76)


# ===========================================================================
#  STEP 7 -- Plots
# ===========================================================================

def plot_results(res: dict) -> None:

    def shade_regimes(ax):
        prev, start = None, None
        for dt, reg in zip(res["dates"], res["regime"]):
            if reg != prev:
                if start and prev == "Bear":
                    ax.axvspan(start, dt, alpha=0.09, color="crimson",   zorder=0)
                elif start and prev == "Bull":
                    ax.axvspan(start, dt, alpha=0.06, color="steelblue", zorder=0)
                start, prev = dt, reg
        if start and prev == "Bear":
            ax.axvspan(start, res["dates"][-1], alpha=0.09, color="crimson",   zorder=0)
        elif start and prev == "Bull":
            ax.axvspan(start, res["dates"][-1], alpha=0.06, color="steelblue", zorder=0)

    # Figure 1: cumulative wealth
    fig, axes = plt.subplots(3, 1, figsize=(13, 13),
                             gridspec_kw={"height_ratios": [3, 2, 1.5]})
    fig.suptitle(
        "Supply-Dilution L/S v4 | ACTUAL Binance perp prices + real funding rates\n"
        "Composite 13w+52w signal | inv-vol weights | altseason veto | CB | post-2022",
        fontsize=11, fontweight="bold")

    ax = axes[0]
    for series, color, lw, ls, label in [
        (res["long_gross"],      "steelblue",     1.5, "-",  "Long basket (gross)"),
        (res["short_gross"],     "crimson",        1.5, "-",  "Short basket (gross)"),
        (res["combined_net"],    "silver",         1.8, "--", "L/S Combined (unhedged)"),
        (res["combined_hedged"], "mediumseagreen", 2.5, "-",  "L/S Combined (BTC-hedged)"),
    ]:
        cum = (1 + series.dropna()).cumprod()
        ax.semilogy(cum.index, cum.values, color=color, lw=lw, ls=ls, label=label)
    ax.axhline(1, color="black", lw=0.6, ls="--")
    shade_regimes(ax)
    ax.set_ylabel("Cumulative Return (log)")
    ax.set_title("Cumulative Wealth — v4 (Real Data)")
    ax.legend(fontsize=9); ax.grid(True, which="both", alpha=0.2)

    ax2 = axes[1]
    for s, c, lbl in [
        (res["combined_hedged"], "mediumseagreen", "Hedged combined net"),
        (res["combined_net"],    "silver",         "Unhedged combined net"),
        (res["long_net"],        "steelblue",      "Long leg net"),
    ]:
        cum = (1 + s.dropna()).cumprod()
        ax2.plot(cum.index, cum.values, color=c, lw=1.8, label=lbl)
    ax2.axhline(1, color="black", lw=0.6, ls="--")
    shade_regimes(ax2)
    ax2.set_ylabel("Cumulative Return")
    ax2.set_title("Net Performance (after all costs + hedge)")
    ax2.legend(fontsize=9); ax2.grid(True, alpha=0.2)

    ax3 = axes[2]
    sp   = res["spread_gross"]
    cols = ["steelblue" if v >= 0 else "crimson" for v in sp.values]
    ax3.bar(sp.index, sp.values, color=cols, width=20, alpha=0.8)
    ax3.axhline(0, color="black", lw=0.8)
    ax3.set_ylabel("Period Spread (gross)"); ax3.set_xlabel("Rebalance Date")
    ax3.grid(True, alpha=0.2)
    fig.tight_layout()
    out1 = OUTPUT_DIR + "perp_ls_v6_cumulative.png"
    fig.savefig(out1, dpi=150); plt.close(fig)
    print(f"[Plot] {out1}")

    # Figure 2: regime breakdown + drawdown
    fig2, axes2 = plt.subplots(2, 1, figsize=(12, 8))

    ax4 = axes2[0]
    rc  = {"Bull": "steelblue", "Bear": "crimson", "Sideways": "gray"}
    for j, (dt, reg) in enumerate(zip(res["dates"], res["regime"])):
        ax4.bar(dt, sp.values[j], color=rc.get(reg, "gray"), width=20, alpha=0.7)
    ax4.axhline(0, color="black", lw=0.8)
    ax4.legend(handles=[Patch(color=c, alpha=0.7, label=l) for l, c in rc.items()], fontsize=9)
    ax4.set_title("Per-Period Gross Spread by Regime (v4 — Real Data)")
    ax4.set_ylabel("Spread Return"); ax4.grid(True, alpha=0.2)

    ax5 = axes2[1]
    cum_h  = (1 + res["combined_hedged"].clip(lower=-0.99)).cumprod()
    dd_h   = (cum_h - cum_h.cummax()) / cum_h.cummax()
    cum_uh = (1 + res["combined_net"].clip(lower=-0.99)).cumprod()
    dd_uh  = (cum_uh - cum_uh.cummax()) / cum_uh.cummax()
    ax5.fill_between(dd_h.index,  dd_h.values,  0, color="mediumseagreen", alpha=0.55, label="Hedged")
    ax5.fill_between(dd_uh.index, dd_uh.values, 0, color="crimson",        alpha=0.25, label="Unhedged")
    ax5.set_ylabel("Drawdown"); ax5.set_xlabel("Date")
    ax5.set_title("Drawdown: BTC-Hedged vs Unhedged Combined")
    ax5.legend(fontsize=9); ax5.grid(True, alpha=0.2)
    fig2.tight_layout()
    out2 = OUTPUT_DIR + "perp_ls_v6_regime_dd.png"
    fig2.savefig(out2, dpi=150); plt.close(fig2)
    print(f"[Plot] {out2}")

    # Figure 3: v4 vs v6 scorecard (monthly vs regime-aware frequency)
    V4 = dict(
        combined_ann=-0.0511, combined_dd=-0.2289, sharpe=-0.130,
        win_rate=0.480, mean_spread=0.0195,
        bull_geo=0.2111, bear_geo=0.4810, side_geo=-0.1451,
    )
    regimes_list = ["Bull", "Bear", "Sideways"]
    v6_geo = {}
    regs = np.array(res["regime"])
    for regime in regimes_list:
        mask = regs == regime
        sub  = sp.iloc[list(np.where(mask)[0])]
        v6_geo[regime] = ((1 + sub.clip(lower=-0.99)).prod() ** (12/max(len(sub),1)) - 1
                          if len(sub) > 0 else np.nan)

    v6_st = portfolio_stats(res["combined_hedged"])

    fig3, axes3 = plt.subplots(2, 2, figsize=(14, 10))
    fig3.suptitle(f"v4 (monthly) vs v6 (Bear={REBAL_STEP['Bear']}mo / Bull={REBAL_STEP['Bull']}mo) — Post-2022",
                  fontsize=13, fontweight="bold")
    x = np.arange(len(regimes_list)); w = 0.36

    ax6 = axes3[0, 0]
    ax6.bar(x - w/2, [V4["bull_geo"],  V4["bear_geo"],  V4["side_geo"]],
            w, label="v4 (monthly)", color="silver", edgecolor="black")
    ax6.bar(x + w/2, [v6_geo.get(r, 0) for r in regimes_list],
            w, label="v6 (regime-freq)", color="steelblue", edgecolor="black")
    ax6.axhline(0, color="black", lw=0.8)
    ax6.set_xticks(x); ax6.set_xticklabels(regimes_list)
    ax6.set_title("Ann. Geometric Spread by Regime"); ax6.set_ylabel("Geo. Spread")
    ax6.legend(fontsize=9); ax6.grid(True, alpha=0.2)

    ax7 = axes3[0, 1]
    fund_labels = ["Long drag\n(cum.)", "Short credit\n(cum.)", "Net\nimpact"]
    v4_funding  = [-0.0252, -0.0106, -0.0357]   # from v4 output
    v6_funding  = [res["fund_long_cum"], res["fund_short_cum"],
                   res["fund_long_cum"] + res["fund_short_cum"]]
    xf = np.arange(len(fund_labels))
    ax7.bar(xf - w/2, v4_funding, w, label="v4 (monthly)", color="silver", edgecolor="black")
    ax7.bar(xf + w/2, v6_funding, w, label="v6 (regime-freq)", color="steelblue", edgecolor="black")
    ax7.axhline(0, color="black", lw=0.8)
    ax7.set_xticks(xf); ax7.set_xticklabels(fund_labels)
    ax7.set_title("Funding Attribution: v4 vs v6")
    ax7.set_ylabel("Cumulative Funding"); ax7.legend(fontsize=9); ax7.grid(True, alpha=0.2)

    ax8 = axes3[1, 0]
    cum_v6h = (1 + res["combined_hedged"].dropna()).cumprod()
    cum_v6u = (1 + res["combined_net"].dropna()).cumprod()
    ax8.plot(cum_v6h.index, cum_v6h.values, "mediumseagreen", lw=2.5, label="v6 BTC-hedged")
    ax8.plot(cum_v6u.index, cum_v6u.values, "steelblue",      lw=1.5, ls="--", label="v6 unhedged")
    ax8.axhline(1, color="black", lw=0.6, ls="--")
    ax8.set_title("v6 Combined NAV"); ax8.set_ylabel("Cumulative Return")
    ax8.legend(fontsize=9); ax8.grid(True, alpha=0.2)

    ax9 = axes3[1, 1]
    ax9.axis("off")
    rows = [
        ["Metric",              "v4 (monthly)",    "v6 (regime-freq)"],
        ["Combined net (ann.)", f"{V4['combined_ann']:+.1%}",
         f"{v6_st['ann_return']:+.1%}"],
        ["MaxDD (BTC-hedged)",  f"{V4['combined_dd']:+.1%}",
         f"{v6_st['max_dd']:+.1%}"],
        ["Win rate",            f"{V4['win_rate']:.1%}",
         f"{(sp > 0).mean():.1%}"],
        ["Mean spread",         f"{V4['mean_spread']:+.2%}",
         f"{sp.mean():+.2%}"],
        ["Bull geo spread",     f"{V4['bull_geo']:+.1%}",
         f"{v6_geo.get('Bull', float('nan')):+.1%}"],
        ["Bear geo spread",     f"{V4['bear_geo']:+.1%}",
         f"{v6_geo.get('Bear', float('nan')):+.1%}"],
        ["Sideways geo spread", f"{V4['side_geo']:+.1%}",
         f"{v6_geo.get('Sideways', float('nan')):+.1%}"],
    ]
    tbl = ax9.table(cellText=rows[1:], colLabels=rows[0],
                    loc="center", cellLoc="center")
    tbl.auto_set_font_size(False); tbl.set_fontsize(10)
    tbl.scale(1.2, 1.7)
    for j in range(3):
        tbl[0, j].set_facecolor("#2c3e50")
        tbl[0, j].set_text_props(color="white", fontweight="bold")
    ax9.set_title("v4 vs v6 Scorecard", fontweight="bold", pad=14)

    fig3.tight_layout()
    out3 = OUTPUT_DIR + "perp_ls_v6_vs_v4.png"
    fig3.savefig(out3, dpi=150); plt.close(fig3)
    print(f"[Plot] {out3}")


# ===========================================================================
#  MAIN
# ===========================================================================

def main():
    print("=" * 76)
    print("Supply-Dilution L/S Strategy -- Version 6  [REGIME-AWARE REBALANCING]")
    print("Binance USDT-M perp prices | Actual 8h funding rates | Perp ADTV/vol")
    print(f"Rebal freq: Bear={REBAL_STEP['Bear']}mo / Bull={REBAL_STEP['Bull']}mo / Sideways={REBAL_STEP['Sideways']}mo")
    print("=" * 76)

    df = load_cmc(INPUT_FILE)
    bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_piv, onboard_map = load_binance(BN_DIR)
    regime_df = build_regime(df)
    df        = engineer_features(df)

    results = run_backtest(df, regime_df,
                           bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_piv,
                           onboard_map)

    if not results["dates"]:
        print("[ERROR] No rebalancing periods survived all filters.")
        return

    print_report(results)
    plot_results(results)
    print("\nDone.")


if __name__ == "__main__":
    main()
