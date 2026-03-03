"""
unlock_preview.py
=================
Simulates what performance would look like if we had FORWARD-LOOKING unlock
calendar data — i.e., knowing one month in advance which tokens will
experience significant supply increases.

Uses exact v7 logic plus a "pre-unlock short signal":
  - At rebal date t0, compute next_supply_inf(t0) =
        circ_supply(t0 + 13 weeks) / circ_supply(t0) - 1
    This is intentionally look-ahead (simulates TokenUnlocks data).
  - Any token with next_supply_inf > UNLOCK_THRESHOLD (5%) is forced into
    the short candidate pool with composite rank 0.95.
  - Regular v7 signal still applies to all other tokens.

Runs two backtests: (1) regular v7 baseline, (2) v7 + unlock pre-signal.
Outputs a side-by-side comparison table plus per-token unlock attribution.

START_DATE = 2022-01-01 (same as v7 baseline).
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

# ===========================================================================
#  CONFIGURATION  (identical to v7 + unlock additions)
# ===========================================================================

INPUT_FILE   = "D:/AI_Projects/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
BN_DIR       = "D:/AI_Projects/circ_supply/binance_perp_data/"

# Universe
MAX_RANK             = 200
TOP_N_EXCLUDE        = 20
MIN_VOLUME           = 5_000_000
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
    ("Sideways", False): (0.00, 0.00),
    ("Sideways", True):  (0.00, 0.00),
    ("Bull",     False): (0.75, 0.75),
    ("Bull",     True):  (0.50, 0.25),
    ("Bear",     False): (0.75, 0.75),
    ("Bear",     True):  (0.50, 0.25),
}

ALTSEASON_THRESHOLD  = 0.75
ALTSEASON_LOOKBACK   = 4
MOMENTUM_VETO_PCT    = 0.50
SHORT_SQUEEZE_PRIOR  = 0.40
SHORT_CB_LOSS        = 0.40

START_DATE = pd.Timestamp("2022-01-01")
WINS_LOW   = 0.01
WINS_HIGH  = 0.99

# Unlock pre-signal additions
UNLOCK_THRESHOLD     = 0.05   # 5% next-13w supply increase triggers short
UNLOCK_FORCED_RANK   = 0.95   # composite rank assigned to unlock candidates

# ===========================================================================
#  EXCLUSION LISTS  (identical to v7)
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
#  HELPERS  (identical to v7)
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
    cum        = (1 + returns).cumprod()
    total_years = max((returns.index[-1] - returns.index[0]).days / 365.25, 1/52)
    cum_final  = float(cum.iloc[-1])
    ann_return = (cum_final ** (1 / total_years) - 1) if cum_final > 0 else np.nan
    ppy        = _ppy(returns)
    vol        = returns.std() * np.sqrt(ppy)
    sharpe     = ann_return / vol if vol > 0 and not np.isnan(ann_return) else np.nan
    slo        = sharpe_lo(returns, ppy)
    down       = returns[returns < 0]
    sortino    = (ann_return / (np.sqrt((down**2).mean()) * np.sqrt(ppy))
                  if len(down) > 0 else np.nan)
    roll_max   = cum.cummax()
    max_dd     = float(((cum - roll_max) / roll_max).min())
    return dict(ann_return=ann_return, vol=vol, sharpe=sharpe, sharpe_lo=slo,
                sortino=sortino, max_dd=max_dd)


def get_ls_scales(regime: str, high_vol: bool) -> tuple:
    return REGIME_LS_SCALE.get((regime, high_vol),
           REGIME_LS_SCALE.get((regime, False), (0.0, 0.0)))


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
#  STEP 1 — Load CMC data  (identical to v7 load_cmc + engineer_features)
# ===========================================================================

def load_cmc(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["snapshot_date"])
    df = df[df["symbol"].apply(lambda s: str(s).isascii())]
    df = df.sort_values(["symbol", "snapshot_date"]).reset_index(drop=True)
    df = df[df["price"].notna() & (df["price"] > 0) & df["market_cap"].notna()].copy()
    for col in ["price", "circulating_supply"]:
        df[col] = df.groupby("symbol")[col].transform(
            lambda s: s.ffill(limit=FFILL_LIMIT))
    df = df[df["circulating_supply"].notna() & (df["circulating_supply"] > 0)].copy()
    print(f"[CMC] {len(df):,} rows | {df['symbol'].nunique():,} symbols | "
          f"{df['snapshot_date'].min().date()} -> {df['snapshot_date'].max().date()}")
    return df


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    grp = df.groupby("symbol", group_keys=False)
    df["supply_inf_13w"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW))
    df["supply_inf_52w"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW_SLOW))
    df["supply_inf"] = df["supply_inf_13w"]
    df["supply_hist_count"] = grp["supply_inf"].transform(
        lambda s: s.notna().cumsum())
    df["turnover"] = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"] = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)
    return df


# ===========================================================================
#  STEP 2 — Load Binance data  (identical to v7)
# ===========================================================================

def load_binance(bn_dir: str) -> tuple:
    ohlcv   = pd.read_parquet(f"{bn_dir}/weekly_ohlcv.parquet")
    funding = pd.read_parquet(f"{bn_dir}/weekly_funding.parquet")
    meta    = pd.read_csv(f"{bn_dir}/symbol_meta.csv", parse_dates=["onboard_date"])

    ohlcv["cmc_date"]   = ohlcv["week_start"] + pd.Timedelta(days=6)
    funding["cmc_date"] = funding["week_start"] + pd.Timedelta(days=6)

    bn_price_piv = ohlcv.pivot_table(
        index="cmc_date", columns="symbol", values="close", aggfunc="last")
    bn_adtv_piv  = ohlcv.pivot_table(
        index="cmc_date", columns="symbol", values="quote_volume", aggfunc="last")

    close_df = (ohlcv.sort_values("cmc_date")
                     .pivot_table(index="cmc_date", columns="symbol",
                                  values="close", aggfunc="last"))
    ret_df   = close_df.pct_change(1)
    tokv_df  = ret_df.rolling(TOKEN_VOL_WINDOW, min_periods=4).std() * np.sqrt(52)
    bn_tokv_piv = tokv_df

    bn_fund_raw = funding[["symbol", "week_start", "funding_sum"]].copy()
    onboard_map = dict(zip(meta["symbol"], meta["onboard_date"]))

    print(f"[Binance] price: {bn_price_piv.shape}  "
          f"funding rows: {len(bn_fund_raw):,}  "
          f"symbols w/ onboard: {len(onboard_map)}")
    return bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_raw, onboard_map


# ===========================================================================
#  STEP 3 — Regime  (identical to v7)
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
#  STEP 4 — Pre-compute next-period supply inflation (FORWARD-LOOKING)
#
#  For each (symbol, snap_date) pair, look up circ_supply SUPPLY_WINDOW
#  periods later in the same symbol's time series.
#  This is intentionally look-ahead — it simulates having TokenUnlocks data.
# ===========================================================================

def build_next_supply_inf(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns a DataFrame with columns [symbol, snap_date, next_supply_inf]
    where next_supply_inf = supply(t + SUPPLY_WINDOW) / supply(t) - 1.
    This is a forward-looking signal (look-ahead bias by design).
    """
    records = []
    for sym, grp in df.groupby("symbol"):
        grp = grp.sort_values("snapshot_date").reset_index(drop=True)
        n = len(grp)
        for i in range(n):
            t0 = grp.loc[i, "snapshot_date"]
            s0 = grp.loc[i, "circulating_supply"]
            j  = i + SUPPLY_WINDOW
            if j < n:
                s_fwd = grp.loc[j, "circulating_supply"]
                if pd.notna(s0) and s0 > 0 and pd.notna(s_fwd):
                    nsi = s_fwd / s0 - 1
                else:
                    nsi = np.nan
            else:
                nsi = np.nan
            records.append({"symbol": sym, "snap_date": t0,
                             "next_supply_inf": nsi})
    nsi_df = pd.DataFrame(records)
    print(f"[NextSupplyInf] {len(nsi_df):,} rows computed "
          f"({nsi_df['next_supply_inf'].notna().sum():,} non-NaN)")
    return nsi_df


# ===========================================================================
#  STEP 5 — Main backtest loop
#  use_unlock_signal: if True, injects the forward-looking unlock candidates
#                     into the short pool.
#  Returns: result dict (same structure as v7) + unlock_log list
# ===========================================================================

def run_backtest(df: pd.DataFrame,
                 regime_df: pd.DataFrame,
                 bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_raw,
                 onboard_map: dict,
                 nsi_df: pd.DataFrame = None,
                 use_unlock_signal: bool = False) -> tuple:
    """
    Returns (result_dict, unlock_log).
    unlock_log is non-empty only when use_unlock_signal=True.
    """

    df["ym"]   = df["snapshot_date"].dt.to_period("M")
    all_rebal  = sorted(df.groupby("ym")["snapshot_date"].min().tolist())
    regime_map = regime_df.set_index("snapshot_date")[["regime","high_vol"]].to_dict("index")

    inf_snap = df[df["snapshot_date"].isin(all_rebal)].copy()
    inf_snap = inf_snap[inf_snap["supply_inf_13w"].notna()]
    inf_snap = inf_snap[~inf_snap["symbol"].isin(EXCLUDED)]
    inf_snap = inf_snap[(inf_snap["rank"] > TOP_N_EXCLUDE) & (inf_snap["rank"] <= MAX_RANK)]
    inf_snap = inf_snap[inf_snap["market_cap"]  >= MIN_MKTCAP]
    inf_snap = inf_snap[inf_snap["supply_hist_count"] >= MIN_SUPPLY_HISTORY]

    bn_symbols = set(bn_price_piv.columns)
    inf_snap   = inf_snap[inf_snap["symbol"].isin(bn_symbols)]

    # Build next_supply_inf lookup: {(symbol, snap_date): value}
    nsi_lookup = {}
    if use_unlock_signal and nsi_df is not None:
        for _, row in nsi_df.iterrows():
            nsi_lookup[(row["symbol"], row["snap_date"])] = row["next_supply_inf"]

    price_piv_cmc = df.pivot_table(index="snapshot_date", columns="symbol",
                                   values="price", aggfunc="last")
    slip_piv      = df.pivot_table(index="snapshot_date", columns="symbol",
                                   values="slippage", aggfunc="last")

    # Altseason pre-computation (identical to v7)
    altseason_map = {}
    for i, t0r in enumerate(all_rebal):
        if i < ALTSEASON_LOOKBACK or t0r not in price_piv_cmc.index:
            altseason_map[t0r] = False; continue
        t_lb = all_rebal[i - ALTSEASON_LOOKBACK]
        if t_lb not in price_piv_cmc.index:
            altseason_map[t0r] = False; continue
        p_lb  = price_piv_cmc.loc[t_lb]
        p_now = price_piv_cmc.loc[t0r]
        btc_p0 = float(p_lb.get("BTC", np.nan))
        btc_p1 = float(p_now.get("BTC", np.nan))
        if pd.isna(btc_p0) or btc_p0 <= 0 or pd.isna(btc_p1):
            altseason_map[t0r] = False; continue
        btc_4w = btc_p1 / btc_p0 - 1
        top50  = df[(df["snapshot_date"] == t0r) &
                    (df["rank"].between(3, 50)) &
                    (~df["symbol"].isin(EXCLUDED | {"BTC", "ETH"}))]["symbol"].tolist()
        alt_rets = [p_now.get(s, np.nan)/p_lb.get(s, np.nan)-1
                    for s in top50
                    if pd.notna(p_lb.get(s)) and p_lb.get(s, 0) > 0
                    and pd.notna(p_now.get(s))]
        if len(alt_rets) < 10:
            altseason_map[t0r] = False; continue
        altseason_map[t0r] = (sum(r > btc_4w for r in alt_rets) / len(alt_rets)
                               > ALTSEASON_THRESHOLD)

    sorted_rebals = [d for d in all_rebal if d >= START_DATE] if START_DATE else all_rebal

    # State
    prev_long_set  = set()
    prev_short_set = set()

    # Accumulators
    (dates_out, long_gross_l, short_gross_l,
     long_net_l, short_net_l, combined_net_l,
     basket_sizes_l, regime_out_l, scale_out_l,
     cb_count, altseason_count, momentum_veto_count,
     turnover_long_l, turnover_short_l,
     fund_actual_long_l, fund_actual_short_l) = (
        [], [], [],
        [], [], [],
        [], [], [],
        0, 0, 0,
        [], [],
        [], []
    )
    fund_long_cum  = 0.0
    fund_short_cum = 0.0

    # Unlock-specific tracking
    unlock_log = []   # list of dicts: {t0, symbol, next_supply_inf, fwd_return, via_unlock}

    for i, t0 in enumerate(sorted_rebals[:-1]):
        t1 = sorted_rebals[i + 1]

        if t0 not in bn_price_piv.index or t1 not in bn_price_piv.index:
            continue

        univ = inf_snap[inf_snap["snapshot_date"] == t0].copy()
        univ = univ[univ["symbol"].apply(
            lambda s: pd.notna(onboard_map.get(s)) and onboard_map.get(s) <= t0)]

        if t0 in bn_adtv_piv.index:
            adtv_now = bn_adtv_piv.loc[t0]
            univ = univ[univ["symbol"].apply(
                lambda s: (pd.notna(adtv_now.get(s)) and
                           float(adtv_now.get(s, 0)) >= MIN_VOLUME * 7))]

        if len(univ) < MIN_BASKET_SIZE * 2:
            continue

        # Cross-sectional winsorisation
        for col in ["supply_inf_13w", "supply_inf_52w"]:
            col_vals = univ[col].dropna()
            if len(col_vals) > 4:
                lo_w, hi_w = col_vals.quantile(SUPPLY_INF_WINS)
                univ[col]  = univ[col].clip(lo_w, hi_w)

        # Composite signal rank (2-layer, identical to v7)
        univ["rank_13w"] = univ["supply_inf_13w"].rank(pct=True)
        univ["rank_52w"] = univ["supply_inf_52w"].rank(pct=True)
        univ["rank_52w"] = univ["rank_52w"].fillna(univ["rank_13w"])
        univ["pct_rank"] = ((1 - SIGNAL_SLOW_WEIGHT) * univ["rank_13w"]
                          + SIGNAL_SLOW_WEIGHT        * univ["rank_52w"])

        rank_map = univ.set_index("symbol")["pct_rank"].to_dict()
        all_syms = set(univ["symbol"])

        # ----------------------------------------------------------------
        # Unlock pre-signal injection (forward-looking, only when enabled)
        # Any token in universe with next_supply_inf > UNLOCK_THRESHOLD
        # gets its composite rank overridden to UNLOCK_FORCED_RANK,
        # forcing it into the short candidate pool.
        # ----------------------------------------------------------------
        unlock_candidates_this_period = set()
        if use_unlock_signal:
            for sym in all_syms:
                nsi_val = nsi_lookup.get((sym, t0), np.nan)
                if pd.notna(nsi_val) and nsi_val > UNLOCK_THRESHOLD:
                    rank_map[sym] = UNLOCK_FORCED_RANK
                    unlock_candidates_this_period.add(sym)

        # Quantile thresholds on the (possibly modified) composite rank
        rank_series = pd.Series(rank_map)
        long_thresh  = float(rank_series.quantile(LONG_ENTRY_PCT))
        long_exit_t  = float(rank_series.quantile(LONG_EXIT_PCT))
        short_thresh = float(rank_series.quantile(SHORT_ENTRY_PCT))
        short_exit_t = float(rank_series.quantile(SHORT_EXIT_PCT))

        # Squeeze exclusion (identical to v7)
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

        # Build long and short baskets
        entry_long   = {s for s in all_syms if rank_map[s] <= long_thresh}
        stay_long    = {s for s in (prev_long_set & all_syms) if rank_map[s] <= long_exit_t}
        basket_long  = entry_long | stay_long

        entry_short_raw = {s for s in all_syms if rank_map[s] >= short_thresh} - squeezed
        stay_short_raw  = {s for s in (prev_short_set & all_syms)
                           if rank_map[s] >= short_exit_t} - squeezed

        # Momentum veto within short candidate pool (identical to v7)
        momentum_vetoed = set()
        if i > 0 and len(entry_short_raw) > MIN_BASKET_SIZE:
            t_prev_r = sorted_rebals[i - 1]
            if t_prev_r in bn_price_piv.index and t0 in bn_price_piv.index:
                p_prev_bn = bn_price_piv.loc[t_prev_r]
                p_now_bn  = bn_price_piv.loc[t0]
                mom_rets  = {}
                for s in entry_short_raw:
                    p0m = float(p_prev_bn[s]) if (s in p_prev_bn and pd.notna(p_prev_bn[s])) else np.nan
                    p1m = float(p_now_bn[s])  if (s in p_now_bn  and pd.notna(p_now_bn[s]))  else np.nan
                    if pd.notna(p0m) and p0m > 0 and pd.notna(p1m):
                        mom_rets[s] = p1m / p0m - 1
                if len(mom_rets) >= MIN_BASKET_SIZE * 2:
                    threshold = np.percentile(list(mom_rets.values()),
                                              MOMENTUM_VETO_PCT * 100)
                    candidates_vetoed = {s for s, r in mom_rets.items() if r > threshold}
                    remaining = entry_short_raw - candidates_vetoed
                    if len(remaining) >= MIN_BASKET_SIZE:
                        momentum_vetoed = candidates_vetoed
                        momentum_veto_count += len(momentum_vetoed)

        entry_short  = entry_short_raw - momentum_vetoed
        stay_short   = stay_short_raw
        basket_short = entry_short | stay_short

        overlap      = basket_long & basket_short
        basket_long  -= overlap
        basket_short -= overlap

        if len(basket_long) < MIN_BASKET_SIZE or len(basket_short) < MIN_BASKET_SIZE:
            prev_long_set  = basket_long
            prev_short_set = basket_short
            continue

        # Regime & L/S scaling (identical to v7)
        reg_info  = regime_map.get(t0, {"regime": "Sideways", "high_vol": False})
        regime    = reg_info.get("regime",   "Sideways")
        high_vol  = bool(reg_info.get("high_vol", False))
        long_scale, short_scale = get_ls_scales(regime, high_vol)

        if altseason_map.get(t0, False):
            short_scale = 0.0
            altseason_count += 1

        to_l = (1 - len(basket_long & prev_long_set) /
                max(len(basket_long | prev_long_set), 1)) if prev_long_set else 1.0
        to_s = (1 - len(basket_short & prev_short_set) /
                max(len(basket_short | prev_short_set), 1)) if prev_short_set else 1.0

        prev_long_set  = basket_long
        prev_short_set = basket_short

        if long_scale == 0.0 and short_scale == 0.0:
            turnover_long_l.append(to_l)
            turnover_short_l.append(to_s)
            dates_out.append(t0)
            long_gross_l.append(0.0);  short_gross_l.append(0.0)
            long_net_l.append(0.0);    short_net_l.append(0.0)
            combined_net_l.append(0.0)
            basket_sizes_l.append((len(basket_long), len(basket_short)))
            regime_out_l.append(regime)
            scale_out_l.append((0.0, 0.0))
            fund_actual_long_l.append(0.0)
            fund_actual_short_l.append(0.0)
            continue

        turnover_long_l.append(to_l)
        turnover_short_l.append(to_s)

        # Forward returns from Binance perp prices
        p0_bn = bn_price_piv.loc[t0]
        p1_bn = bn_price_piv.loc[t1]
        fwd   = (p1_bn / p0_bn - 1).dropna()

        lo_f, hi_f = fwd.quantile(WINS_LOW), fwd.quantile(WINS_HIGH)
        fwd = fwd.clip(lower=lo_f, upper=hi_f).clip(lower=-1.0)

        # Actual funding
        fund_mask = (bn_fund_raw["week_start"] > t0) & \
                    (bn_fund_raw["week_start"] <= t1 + pd.Timedelta(days=1))
        fund_rows = bn_fund_raw[fund_mask]
        fund_row  = fund_rows.groupby("symbol")["funding_sum"].sum() if len(fund_rows) > 0 \
                    else pd.Series(dtype=float)

        adtv_row = bn_adtv_piv.loc[t0] if t0 in bn_adtv_piv.index else pd.Series(dtype=float)
        tokv_row = bn_tokv_piv.loc[t0] if t0 in bn_tokv_piv.index else pd.Series(dtype=float)
        sl_row   = slip_piv.loc[t0]    if t0 in slip_piv.index    else pd.Series(dtype=float)

        def basket_return(symbols: set) -> tuple:
            syms = [s for s in symbols if s in fwd.index and not pd.isna(fwd[s])]
            if not syms:
                return np.nan, MAX_SLIPPAGE, 0.0
            vol_m  = {s: float(tokv_row.get(s, 1.0) if pd.notna(tokv_row.get(s)) else 1.0)
                      for s in syms}
            adtv_m = {s: float(adtv_row.get(s, 0)  if pd.notna(adtv_row.get(s))  else 0.0)
                      for s in syms}
            w      = inv_vol_adtv_weights(syms, vol_m, adtv_m)
            ret    = sum(w[s] * float(fwd[s]) for s in syms)
            slip   = sum(w[s] * float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)
                         for s in syms)
            fund_s = sum(w[s] * float(fund_row[s] if s in fund_row.index and
                                      pd.notna(fund_row[s]) else 0.0) for s in syms)
            return float(ret), float(slip), float(fund_s)

        r_long_gross,  slip_long,  fund_long_basket  = basket_return(basket_long)
        r_short_gross, slip_short, fund_short_basket = basket_return(basket_short)

        if pd.isna(r_long_gross) or pd.isna(r_short_gross):
            continue

        if r_short_gross > SHORT_CB_LOSS:
            r_short_gross = SHORT_CB_LOSS
            cb_count += 1

        fee_cost = 2 * TAKER_FEE
        actual_fund_long_drag  = -fund_long_basket
        actual_fund_short_cred = +fund_short_basket

        r_long_net  = r_long_gross  - fee_cost - slip_long  + actual_fund_long_drag
        r_short_net = -r_short_gross - fee_cost - slip_short + actual_fund_short_cred

        denom      = long_scale + short_scale if (long_scale + short_scale) > 0 else 1.0
        r_combined = (long_scale * r_long_net + short_scale * r_short_net) / denom

        r_long_net  = max(r_long_net,  -1.0)
        r_short_net = max(r_short_net, -1.0)
        r_combined  = max(r_combined,  -1.0)

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
        basket_sizes_l.append((len(basket_long), len(basket_short)))
        regime_out_l.append(regime)
        scale_out_l.append((long_scale, short_scale))

        # ------------------------------------------------------------------
        # Unlock attribution logging:
        # Record each token that was in basket_short via the unlock signal
        # (i.e., in unlock_candidates_this_period AND in basket_short),
        # along with its actual forward return.
        # ------------------------------------------------------------------
        if use_unlock_signal:
            unlock_shorted = unlock_candidates_this_period & basket_short
            for sym in unlock_shorted:
                fwd_ret = float(fwd[sym]) if sym in fwd.index and not pd.isna(fwd[sym]) else np.nan
                nsi_val = nsi_lookup.get((sym, t0), np.nan)
                # Check whether this token would have been in the short basket
                # via the regular v7 signal (i.e., original rank >= short_thresh
                # before the override).  We re-check the original supply ranks.
                orig_row = univ[univ["symbol"] == sym]
                if not orig_row.empty:
                    orig_pct_rank = float(orig_row.iloc[0].get("pct_rank", 0)
                                          if "pct_rank" in orig_row.columns else 0)
                else:
                    orig_pct_rank = 0.0
                via_unlock_only = (orig_pct_rank < short_thresh)
                unlock_log.append({
                    "t0":             t0,
                    "t1":             t1,
                    "symbol":         sym,
                    "next_supply_inf": nsi_val,
                    "fwd_return":     fwd_ret,
                    "regime":         regime,
                    "via_unlock_only": via_unlock_only,
                })

    idx = pd.DatetimeIndex(dates_out)
    result = dict(
        index             = idx,
        dates             = dates_out,
        long_gross        = pd.Series(long_gross_l,   index=idx, name="Long Gross"),
        short_gross       = pd.Series(short_gross_l,  index=idx, name="Short Gross"),
        long_net          = pd.Series(long_net_l,     index=idx, name="Long Net"),
        short_net         = pd.Series(short_net_l,    index=idx, name="Short Net"),
        combined_net      = pd.Series(combined_net_l, index=idx, name="Combined Net"),
        spread_gross      = pd.Series(
            [lg - sg for lg, sg in zip(long_gross_l, short_gross_l)],
            index=idx, name="Spread Gross"),
        basket_sizes      = basket_sizes_l,
        regime            = regime_out_l,
        scale             = scale_out_l,
        fund_long_cum     = fund_long_cum,
        fund_short_cum    = fund_short_cum,
        fund_actual_long  = fund_actual_long_l,
        fund_actual_short = fund_actual_short_l,
        cb_count          = cb_count,
        altseason_count   = altseason_count,
        momentum_veto_count = momentum_veto_count,
        turnover_long     = turnover_long_l,
        turnover_short    = turnover_short_l,
    )
    return result, unlock_log


# ===========================================================================
#  STEP 6 — Comparison report
# ===========================================================================

def _fmt(v):
    return f"{v:+.2%}" if not np.isnan(v) else "    N/A"


def _fmtf(v, d=3):
    return f"{v:+.{d}f}" if not np.isnan(v) else "    N/A"


def _delta(a, b, pct=True):
    """Format delta between two values (b - a)."""
    if np.isnan(a) or np.isnan(b):
        return "    N/A"
    d = b - a
    return f"{d:+.2%}" if pct else f"{d:+.3f}"


def print_comparison(res_v7: dict, res_unlock: dict) -> None:
    sp7 = res_v7["spread_gross"]
    spu = res_unlock["spread_gross"]
    st7 = portfolio_stats(res_v7["combined_net"])
    stu = portfolio_stats(res_unlock["combined_net"])

    regs7 = np.array(res_v7["regime"])
    regsu = np.array(res_unlock["regime"])

    def geo_spread(sp, regs, reg):
        mask = regs == reg
        sub  = sp.iloc[list(np.where(mask)[0])]
        if len(sub) == 0:
            return np.nan
        return (1 + sub.clip(lower=-0.99)).prod() ** (12/len(sub)) - 1

    bear_v7  = geo_spread(sp7, regs7, "Bear")
    bull_v7  = geo_spread(sp7, regs7, "Bull")
    bear_unk = geo_spread(spu, regsu, "Bear")
    bull_unk = geo_spread(spu, regsu, "Bull")

    wr7  = (sp7 > 0).mean()
    wru  = (spu > 0).mean()

    print("\n" + "=" * 78)
    print("UNLOCK PREVIEW — v7 baseline vs v7 + forward-looking unlock pre-signal")
    print(f"  START_DATE:        {START_DATE.date()}")
    print(f"  UNLOCK_THRESHOLD:  {UNLOCK_THRESHOLD:.0%}  (next-13w supply growth)")
    print(f"  UNLOCK_FORCED_RANK: {UNLOCK_FORCED_RANK:.2f}  (overrides composite rank)")
    print("=" * 78)

    print(f"\n  {'Metric':<30} {'v7 (baseline)':>15} {'v7+unlock signal':>17} {'Delta':>10}")
    print("  " + "-" * 74)

    rows = [
        ("Combined net (ann.)",  st7["ann_return"],  stu["ann_return"],  True),
        ("MaxDD",                st7["max_dd"],       stu["max_dd"],       True),
        ("Sharpe (raw)",         st7["sharpe"],       stu["sharpe"],       False),
        ("Sharpe (Lo HAC)",      st7["sharpe_lo"],    stu["sharpe_lo"],    False),
        ("Win rate (spread)",    wr7,                 wru,                 True),
        ("Mean period spread",   sp7.mean(),          spu.mean(),          True),
        ("Bear geo spread",      bear_v7,             bear_unk,            True),
        ("Bull geo spread",      bull_v7,             bull_unk,            True),
    ]
    for label, v7_val, vu_val, is_pct in rows:
        v7_str = _fmt(v7_val)   if is_pct else _fmtf(v7_val)
        vu_str = _fmt(vu_val)   if is_pct else _fmtf(vu_val)
        dl_str = _delta(v7_val, vu_val, pct=is_pct)
        print(f"  {label:<30} {v7_str:>15} {vu_str:>17} {dl_str:>10}")

    n7  = len(res_v7["dates"])
    nu  = len(res_unlock["dates"])
    avg_lo7  = np.mean([s[0] for s in res_v7["basket_sizes"]])    if n7 else np.nan
    avg_hi7  = np.mean([s[1] for s in res_v7["basket_sizes"]])    if n7 else np.nan
    avg_lou  = np.mean([s[0] for s in res_unlock["basket_sizes"]]) if nu else np.nan
    avg_hiu  = np.mean([s[1] for s in res_unlock["basket_sizes"]]) if nu else np.nan

    print(f"\n  Periods (v7/unlock)       : {n7} / {nu}")
    print(f"  Avg long basket size      : {avg_lo7:.1f} / {avg_lou:.1f}")
    print(f"  Avg short basket size     : {avg_hi7:.1f} / {avg_hiu:.1f}")
    print(f"  CB count (v7/unlock)      : {res_v7['cb_count']} / {res_unlock['cb_count']}")
    print(f"  Altseason veto (v7/unlock): {res_v7['altseason_count']} / {res_unlock['altseason_count']}")


def print_unlock_attribution(unlock_log: list) -> None:
    if not unlock_log:
        print("\n[Unlock Attribution] No unlock-signal tokens were shorted.")
        return

    ul = pd.DataFrame(unlock_log)

    # Split by whether the token was ONLY in short basket via unlock signal
    # (not already caught by regular supply signal)
    only_unlock = ul[ul["via_unlock_only"] == True]
    also_regular = ul[ul["via_unlock_only"] == False]

    print("\n" + "=" * 78)
    print("UNLOCK ATTRIBUTION — tokens shorted via the unlock pre-signal")
    print("=" * 78)

    total_unk       = len(ul)
    only_unk_count  = len(only_unlock)
    print(f"\n  Total unlock-signal short instances     : {total_unk}")
    print(f"  Instances ONLY via unlock (incremental) : {only_unk_count}")
    print(f"  Instances also caught by regular signal : {len(also_regular)}")

    # Only-via-unlock token performance
    if only_unk_count > 0:
        sub = only_unlock.dropna(subset=["fwd_return"])
        if len(sub) > 0:
            # For shorts, profit = -fwd_return (we are short the token)
            sub = sub.copy()
            sub["short_profit"] = -sub["fwd_return"]
            wr = (sub["short_profit"] > 0).mean()
            mean_pr = sub["short_profit"].mean()
            print(f"\n  [Incremental unlock-only shorts]")
            print(f"  N with valid forward return : {len(sub)}")
            print(f"  Win rate (short profit > 0) : {wr:.1%}")
            print(f"  Mean short profit per period: {mean_pr:+.2%}")

            # Per-symbol summary
            sym_summary = (sub.groupby("symbol")
                              .agg(count=("t0","count"),
                                   mean_nsi=("next_supply_inf","mean"),
                                   mean_fwd=("fwd_return","mean"),
                                   mean_short_profit=("short_profit","mean"))
                              .reset_index()
                              .sort_values("count", ascending=False))

            print(f"\n  Per-symbol unlock attribution "
                  f"(incremental unlock-only, sorted by frequency):")
            print(f"  {'Symbol':<12} {'Count':>6} {'Avg next_supply_inf':>20} "
                  f"{'Avg fwd ret':>13} {'Avg short profit':>17}")
            print("  " + "-" * 72)
            for _, r in sym_summary.iterrows():
                print(f"  {r['symbol']:<12} {int(r['count']):>6} "
                      f"{r['mean_nsi']:>19.1%} "
                      f"{r['mean_fwd']:>12.2%} "
                      f"{r['mean_short_profit']:>16.2%}")

    # Full period-by-period log of incremental unlock shorts
    if only_unk_count > 0:
        print(f"\n  Full incremental unlock-signal log "
              f"(only_via_unlock=True, sorted by date):")
        print(f"  {'Date':<12} {'Symbol':<10} {'next_supply_inf':>16} "
              f"{'Fwd ret':>10} {'Short profit':>14} {'Regime':>10}")
        print("  " + "-" * 76)
        sub_sorted = only_unlock.dropna(subset=["fwd_return"]).sort_values("t0")
        for _, r in sub_sorted.iterrows():
            sp = -r["fwd_return"]
            print(f"  {str(r['t0'].date()):<12} {r['symbol']:<10} "
                  f"{r['next_supply_inf']:>15.1%} "
                  f"{r['fwd_return']:>9.2%} "
                  f"{sp:>13.2%} "
                  f"{r['regime']:>10}")


# ===========================================================================
#  MAIN
# ===========================================================================

def main():
    print("=" * 78)
    print("unlock_preview.py — Forward-Looking Unlock Signal Simulation")
    print(f"  START_DATE={START_DATE.date()}  UNLOCK_THRESHOLD={UNLOCK_THRESHOLD:.0%}")
    print("=" * 78)

    df        = load_cmc(INPUT_FILE)
    (bn_price_piv, bn_adtv_piv, bn_tokv_piv,
     bn_fund_raw, onboard_map) = load_binance(BN_DIR)
    regime_df = build_regime(df)
    df        = engineer_features(df)

    # Build forward-looking supply inflation table
    # (uses ALL rebalancing dates, not just post-2022, for the lookup)
    print("\n[NextSupplyInf] Building forward-looking supply inflation table...")
    nsi_df = build_next_supply_inf(df)

    # --- Run 1: v7 baseline ---
    print("\n" + "-" * 60)
    print("Running v7 BASELINE backtest...")
    print("-" * 60)
    res_v7, _ = run_backtest(df, regime_df, bn_price_piv, bn_adtv_piv,
                              bn_tokv_piv, bn_fund_raw, onboard_map,
                              nsi_df=None, use_unlock_signal=False)

    # --- Run 2: v7 + unlock pre-signal ---
    print("\n" + "-" * 60)
    print("Running v7 + UNLOCK SIGNAL backtest...")
    print("-" * 60)
    res_unlock, unlock_log = run_backtest(df, regime_df, bn_price_piv, bn_adtv_piv,
                                          bn_tokv_piv, bn_fund_raw, onboard_map,
                                          nsi_df=nsi_df, use_unlock_signal=True)

    if not res_v7["dates"]:
        print("[ERROR] v7 baseline produced no rebalancing periods.")
        return
    if not res_unlock["dates"]:
        print("[ERROR] unlock simulation produced no rebalancing periods.")
        return

    # --- Comparison table ---
    print_comparison(res_v7, res_unlock)

    # --- Unlock attribution ---
    print_unlock_attribution(unlock_log)

    print("\n" + "=" * 78)
    print("Done.")
    print("=" * 78)


if __name__ == "__main__":
    main()
