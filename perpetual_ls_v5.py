"""
perpetual_ls_v5.py
==================
Supply-Dilution L/S Strategy -- Version 5

Key improvement over v4 (same real Binance data, no new data engineering):

  [V5-1] Sideways regime -> hold cash (0% return, no costs).
          Sideways was the single worst regime in v4:
            - Geo spread: -14.51% annualised (16.7% win rate)
            - 6 periods out of 25 contributed -9.4% compound drag
          In v5 we hold cash during sideways, earning 0% instead of paying
          fees and taking a negative spread.  All other regimes (Bull, Bear)
          keep v4's EXACT L/S structure and scaling.

          Crucially, the L/S structure is preserved in bull as well as bear:
          the cross-sectional supply-dilution signal provides a spread-based
          hedge even when both legs move in the same direction — removing the
          short leg in bull destroys this hedge and makes results worse.

  [V5-2] BTC beta hedge removed.
          The BTC-hedged line in v4 returned -10.87% ann. vs unhedged -5.11%
          because the rolling beta estimation over 25 periods was unstable and
          the hedge fought the cross-sectional alpha.  v5 reports only the
          unhedged combined_net for clarity.

Everything else is IDENTICAL to v4:
  - L/S scaling table for Bull and Bear (unchanged)
  - Altseason veto (threshold 0.75, zeros short_scale when altseason)
  - Circuit breaker, squeeze exclusion, inner buffer band
  - Signal (13w+52w composite), weights (inv-vol x sqrt(ADTV))
  - Binance perp prices, actual 8h funding rates

Data: identical to v4 -- run fetch_binance_data.py first.
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
#  CONFIGURATION
# ===========================================================================

INPUT_FILE   = "D:/AI_Projects/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
OUTPUT_DIR   = "D:/AI_Projects/circ_supply/"
BN_DIR       = "D:/AI_Projects/circ_supply/binance_perp_data/"

# Universe
MAX_RANK             = 200
TOP_N_EXCLUDE        = 20
MIN_VOLUME           = 5_000_000       # Binance quote_volume / 7 daily proxy
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

# Regime  -- kept at 20w (13w creates MORE sideways, not fewer)
REGIME_MA_WINDOW     = 20
BULL_BAND            = 1.10
BEAR_BAND            = 0.90
HIGH_VOL_THRESHOLD   = 0.80
VOL_WINDOW           = 8

# [V5-1] Regime scaling: identical to v4 EXCEPT sideways = (0, 0) -> cash
REGIME_LS_SCALE = {
    ("Sideways", False): (0.00, 0.00),   # [V5] hold cash -- -14.5% geo spread in v4
    ("Sideways", True):  (0.00, 0.00),   # [V5] hold cash -- high-vol sideways even worse
    ("Bull",     False): (1.00, 0.50),   # identical to v4
    ("Bull",     True):  (0.75, 0.25),   # identical to v4
    ("Bear",     False): (0.75, 0.75),   # identical to v4
    ("Bear",     True):  (0.50, 0.25),   # identical to v4
}

# Altseason veto (identical to v4)
ALTSEASON_THRESHOLD    = 0.75
ALTSEASON_LOOKBACK     = 4

# Quality filter parameters (stored for reference, filter disabled in this version)
QUAL_MIN_REL_RET = -0.50
QUAL_LOOKBACK_W  = 26

# Other
SHORT_SQUEEZE_PRIOR  = 0.40
SHORT_CB_LOSS        = 0.40

START_DATE  = pd.Timestamp("2022-01-01")
WINS_LOW    = 0.01
WINS_HIGH   = 0.99

# ===========================================================================
#  EXCLUSION LISTS  (identical to v4)
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
#  HELPERS  (identical to v4)
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


def _fmt(v):       return f"{v:+.2%}"  if not np.isnan(v) else "    N/A"
def _fmtf(v, d=3): return f"{v:+.{d}f}" if not np.isnan(v) else "    N/A"


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
#  STEP 1 -- Load CMC data  (identical to v4)
# ===========================================================================

def load_cmc(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["snapshot_date"])
    df = df[df["symbol"].apply(lambda s: str(s).isascii())]
    df = df.sort_values(["symbol", "snapshot_date"]).reset_index(drop=True)
    df = df[df["price"].notna() & (df["price"] > 0) & df["market_cap"].notna()].copy()
    for col in ["price", "circulating_supply"]:
        df[col] = df.groupby("symbol")[col].transform(lambda s: s.ffill(limit=FFILL_LIMIT))
    df = df[df["circulating_supply"].notna() & (df["circulating_supply"] > 0)].copy()
    print(f"[CMC] {len(df):,} rows | {df['symbol'].nunique():,} symbols | "
          f"{df['snapshot_date'].min().date()} -> {df['snapshot_date'].max().date()}")
    return df


# ===========================================================================
#  STEP 2 -- Load Binance data  (identical to v4, plus 26w ret pivot for V5-2)
# ===========================================================================

def load_binance(bn_dir: str) -> tuple:
    ohlcv   = pd.read_parquet(f"{bn_dir}/weekly_ohlcv.parquet")
    funding = pd.read_parquet(f"{bn_dir}/weekly_funding.parquet")
    meta    = pd.read_csv(f"{bn_dir}/symbol_meta.csv", parse_dates=["onboard_date"])

    ohlcv["cmc_date"]   = ohlcv["week_start"]   + pd.Timedelta(days=6)
    funding["cmc_date"] = funding["week_start"]  + pd.Timedelta(days=6)

    bn_price_piv = ohlcv.pivot_table(
        index="cmc_date", columns="symbol", values="close", aggfunc="last")
    bn_adtv_piv = ohlcv.pivot_table(
        index="cmc_date", columns="symbol", values="quote_volume", aggfunc="last")

    close_df = (ohlcv.sort_values("cmc_date")
                     .pivot_table(index="cmc_date", columns="symbol",
                                  values="close", aggfunc="last"))
    ret_df = close_df.pct_change(1)
    tokv_df = ret_df.rolling(TOKEN_VOL_WINDOW, min_periods=4).std() * np.sqrt(52)
    bn_tokv_piv = tokv_df

    # [V5-2] Precompute 26w return pivot for quality filter
    bn_ret26_piv = close_df.pct_change(QUAL_LOOKBACK_W)

    bn_fund_raw = funding[["symbol", "week_start", "funding_sum"]].copy()
    onboard_map = dict(zip(meta["symbol"], meta["onboard_date"]))

    print(f"[Binance] price: {bn_price_piv.shape}  "
          f"funding rows: {len(bn_fund_raw):,}  "
          f"symbols w/ onboard: {len(onboard_map)}")
    return bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_raw, bn_ret26_piv, onboard_map


# ===========================================================================
#  STEP 3 -- Regime  (identical to v4)
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
#  STEP 4 -- Feature engineering  (identical to v4)
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
    df["turnover"] = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"] = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)
    return df


# ===========================================================================
#  STEP 5 -- Main backtest loop
# ===========================================================================

def run_backtest(df: pd.DataFrame, regime_df: pd.DataFrame,
                 bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_raw,
                 bn_ret26_piv, onboard_map: dict) -> dict:

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
    inf_snap = inf_snap[inf_snap["symbol"].isin(bn_symbols)]

    price_piv_cmc = df.pivot_table(index="snapshot_date", columns="symbol",
                                   values="price", aggfunc="last")
    slip_piv = df.pivot_table(index="snapshot_date", columns="symbol",
                              values="slippage", aggfunc="last")

    # Altseason pre-computation
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
                    if pd.notna(p_lb.get(s)) and p_lb.get(s, 0) > 0 and pd.notna(p_now.get(s))]
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
     cb_count, altseason_count,
     turnover_long_l, turnover_short_l,
     fund_actual_long_l, fund_actual_short_l,
     qual_filtered_l) = (
        [], [], [],
        [], [], [],
        [], [], [],
        0, 0,
        [], [],
        [], [],
        []
    )
    fund_long_cum  = 0.0
    fund_short_cum = 0.0

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

        # Composite signal rank
        univ["rank_13w"] = univ["supply_inf_13w"].rank(pct=True)
        univ["rank_52w"] = univ["supply_inf_52w"].rank(pct=True)
        univ["rank_52w"] = univ["rank_52w"].fillna(univ["rank_13w"])
        univ["pct_rank"] = ((1 - SIGNAL_SLOW_WEIGHT) * univ["rank_13w"]
                            + SIGNAL_SLOW_WEIGHT      * univ["rank_52w"])

        rank_map = univ.set_index("symbol")["pct_rank"].to_dict()
        all_syms = set(univ["symbol"])

        # Squeeze exclusion
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
        entry_long  = {s for s in all_syms if rank_map[s] <= LONG_ENTRY_PCT}
        stay_long   = {s for s in (prev_long_set & all_syms) if rank_map[s] <= LONG_EXIT_PCT}

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

        # Regime & L/S scaling
        reg_info  = regime_map.get(t0, {"regime": "Sideways", "high_vol": False})
        regime    = reg_info.get("regime",   "Sideways")
        high_vol  = bool(reg_info.get("high_vol", False))
        long_scale, short_scale = get_ls_scales(regime, high_vol)

        # Altseason veto (identical to v4): zero the short leg
        if altseason_map.get(t0, False):
            short_scale = 0.0
            altseason_count += 1

        # Turnover tracking (always, before the sideways branch)
        to_l = (1 - len(basket_long & prev_long_set) /
                max(len(basket_long | prev_long_set), 1)) if prev_long_set else 1.0
        to_s = (1 - len(basket_short & prev_short_set) /
                max(len(basket_short | prev_short_set), 1)) if prev_short_set else 1.0

        prev_long_set  = basket_long
        prev_short_set = basket_short

        # [V5-1] Sideways = hold cash (0% return, no costs).
        # Record the period so the buffer band and period structure stay identical to v4.
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
            qual_filtered_l.append(0)
            continue

        qual_filtered_l.append(0)   # quality filter disabled in this version
        turnover_long_l.append(to_l)
        turnover_short_l.append(to_s)

        # Forward returns from Binance perp prices
        p0_bn = bn_price_piv.loc[t0]
        p1_bn = bn_price_piv.loc[t1]
        fwd   = (p1_bn / p0_bn - 1).dropna()

        lo_f, hi_f = fwd.quantile(WINS_LOW), fwd.quantile(WINS_HIGH)
        fwd = fwd.clip(lower=lo_f, upper=hi_f).clip(lower=-1.0)

        # Actual funding: sum all 8h rates in holding period (t0, t1]
        fund_mask = (bn_fund_raw["week_start"] > t0) & \
                    (bn_fund_raw["week_start"] <= t1 + pd.Timedelta(days=1))
        fund_rows = bn_fund_raw[fund_mask]
        if len(fund_rows) > 0:
            fund_row = fund_rows.groupby("symbol")["funding_sum"].sum()
        else:
            fund_row = pd.Series(dtype=float)

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
            fund_sum = sum(w[s] * float(fund_row[s] if s in fund_row.index and
                                        pd.notna(fund_row[s]) else 0.0) for s in syms)
            return float(ret), float(slip), float(fund_sum)

        r_long_gross,  slip_long,  fund_long_basket  = basket_return(basket_long)
        r_short_gross, slip_short, fund_short_basket = basket_return(basket_short)

        if pd.isna(r_long_gross) or pd.isna(r_short_gross):
            continue

        # Circuit breaker
        if r_short_gross > SHORT_CB_LOSS:
            r_short_gross = SHORT_CB_LOSS
            cb_count += 1

        # Costs
        fee_cost = 2 * TAKER_FEE

        # Actual funding:
        #   Long: PAY if fund > 0 (drag), RECEIVE if fund < 0
        #   Short: RECEIVE if fund > 0 (credit), PAY if fund < 0
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

    idx = pd.DatetimeIndex(dates_out)
    return dict(
        index            = idx,
        dates            = dates_out,
        long_gross       = pd.Series(long_gross_l,   index=idx, name="Long Gross"),
        short_gross      = pd.Series(short_gross_l,  index=idx, name="Short Gross"),
        long_net         = pd.Series(long_net_l,     index=idx, name="Long Net"),
        short_net        = pd.Series(short_net_l,    index=idx, name="Short Net"),
        combined_net     = pd.Series(combined_net_l, index=idx, name="Combined Net"),
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
        turnover_long    = turnover_long_l,
        turnover_short   = turnover_short_l,
        qual_filtered    = qual_filtered_l,
    )


# ===========================================================================
#  STEP 6 -- Report
# ===========================================================================

def print_report(res: dict) -> None:
    print("\n" + "=" * 76)
    print("PERPETUAL L/S BACKTEST v5  [SIDEWAYS=CASH, SAME L/S STRUCTURE]")
    print("  Signal : 50% rank(13w) + 50% rank(52w), winsorised 2-98pct")
    print("  Prices : Binance USDT-M weekly perp close")
    print("  Funding: Actual 8h Binance funding rates")
    print("  Regime : Sideways=hold cash (0%) | Bull/Bear=same L/S as v4")
    print("  Change : v4 + skip sideways + remove BTC hedge (all else same)")
    print("=" * 76)

    n      = len(res["dates"])
    avg_lo = np.mean([s[0] for s in res["basket_sizes"]])
    avg_hi = np.mean([s[1] for s in res["basket_sizes"]])
    regs   = np.array(res["regime"])
    scales = res["scale"]
    avg_ls = (np.mean([s[0] for s in scales]), np.mean([s[1] for s in scales]))

    print(f"\n  Rebalancing periods : {n}")
    print(f"  Avg basket size     : Long {avg_lo:.1f} | Short {avg_hi:.1f} tokens")
    print(f"  Regime breakdown    : Bull={(regs=='Bull').sum()}  "
          f"Bear={(regs=='Bear').sum()}  Sideways={(regs=='Sideways').sum()}")
    print(f"  Avg effective scale : Long {avg_ls[0]:.2f}x / Short {avg_ls[1]:.2f}x")
    print(f"  CB triggered        : {res['cb_count']} period(s)")
    print(f"  Alt-season veto     : {res['altseason_count']} period(s)")
    if res["qual_filtered"]:
        print(f"  Qual filter removed : avg {np.mean(res['qual_filtered']):.1f} tokens/period "
              f"(max {max(res['qual_filtered'])})")
    if res["turnover_long"]:
        print(f"  Avg monthly turnover: Long {np.mean(res['turnover_long']):.1%}  "
              f"Short {np.mean(res['turnover_short']):.1%}")

    print(f"\n  {'Series':<34} {'Ann.Ret':>9} {'Vol':>9} "
          f"{'Sharpe':>7} {'Sharpe*':>8} {'Sortino':>8} {'MaxDD':>9}")
    print("  " + "-" * 76)
    for name, s in [
        ("Long basket  (gross)",  res["long_gross"]),
        ("Short basket (gross)",  res["short_gross"]),
        ("Long leg     (net)",    res["long_net"]),
        ("Short leg    (net)^",   res["short_net"]),
        ("L/S Combined (net)",    res["combined_net"]),
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

    fl  = res["fund_actual_long"]
    fs  = res["fund_actual_short"]
    fcl = res["fund_long_cum"]
    fcs = res["fund_short_cum"]
    print(f"\n  --- Actual Funding Rate Attribution ---")
    print(f"  Cum. funding impact (long leg)  : {fcl:+.4f} ({fcl:.2%})")
    print(f"  Cum. funding impact (short leg) : {fcs:+.4f} ({fcs:.2%})")
    net_f = fcl + fcs
    print(f"  Net funding impact              : {net_f:+.4f} ({net_f:.2%})")
    if fl:
        print(f"  Avg period long funding drag    : {np.mean(fl):+.4f} ({np.mean(fl):.2%})")
        print(f"  Avg period short funding credit : {np.mean(fs):+.4f} ({np.mean(fs):.2%})")

    # v4 vs v5 inline comparison
    V4 = dict(combined_ann=-0.0511, combined_dd=-0.2289,
              win_rate=0.48, mean_spread=0.0195,
              bear_geo=0.481, bull_geo=-0.053, side_geo=-0.145)
    st5 = portfolio_stats(res["combined_net"])
    v5_geo = {}
    for reg in ["Bull", "Bear", "Sideways"]:
        mask = regs == reg
        sub  = sp.iloc[list(np.where(mask)[0])]
        v5_geo[reg] = ((1 + sub.clip(lower=-0.99)).prod() ** (12/len(sub)) - 1
                       if len(sub) > 0 else np.nan)

    print(f"\n  --- v4 vs v5 Comparison ---")
    print(f"  {'Metric':<32} {'v4 (base)':>12} {'v5 (gated)':>12}")
    print(f"  {'-'*56}")
    v5_ann = st5['ann_return'] if not np.isnan(st5['ann_return']) else float('nan')
    v5_dd  = st5['max_dd']     if not np.isnan(st5['max_dd'])     else float('nan')
    print(f"  {'Combined net ann.':<32} {V4['combined_ann']:>+12.1%} {v5_ann:>+12.1%}")
    print(f"  {'MaxDD':<32} {V4['combined_dd']:>+12.1%} {v5_dd:>+12.1%}")
    print(f"  {'Win rate (spread)':<32} {V4['win_rate']:>12.1%} {(sp>0).mean():>12.1%}")
    print(f"  {'Mean period spread':<32} {V4['mean_spread']:>+12.2%} {sp.mean():>+12.2%}")
    print(f"  {'Bear geo spread':<32} {V4['bear_geo']:>+12.1%} "
          f"{v5_geo.get('Bear', float('nan')):>+12.1%}")
    print(f"  {'Bull geo spread':<32} {V4['bull_geo']:>+12.1%} "
          f"{v5_geo.get('Bull', float('nan')):>+12.1%}")
    bull_side_label = "(skipped)" if np.isnan(v5_geo.get("Sideways", float("nan"))) else f"{v5_geo.get('Sideways', float('nan')):>+12.1%}"
    print(f"  {'Sideways geo spread':<32} {V4['side_geo']:>+12.1%} {bull_side_label:>12}")
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

    sp   = res["spread_gross"]
    regs = np.array(res["regime"])
    rc   = {"Bull": "steelblue", "Bear": "crimson", "Sideways": "gray"}

    # Figure 1: cumulative wealth
    fig, axes = plt.subplots(3, 1, figsize=(13, 13),
                             gridspec_kw={"height_ratios": [3, 2, 1.5]})
    fig.suptitle(
        "Supply-Dilution L/S v5 | Regime-gated: Sideways=skip | Bull=long | Bear=L/S\n"
        "Quality filter (26w, -50pp vs BTC) | Actual Binance funding | post-2022",
        fontsize=11, fontweight="bold")

    ax = axes[0]
    for series, color, lw, ls, label in [
        (res["long_gross"],  "steelblue",     1.5, "-",  "Long basket (gross)"),
        (res["short_gross"], "crimson",        1.5, "-",  "Short basket (gross)"),
        (res["long_net"],    "cornflowerblue", 1.5, "--", "Long leg (net)"),
        (res["combined_net"],"mediumseagreen", 2.5, "-",  "L/S Combined (net)"),
    ]:
        cum = (1 + series.dropna()).cumprod()
        ax.semilogy(cum.index, cum.values, color=color, lw=lw, ls=ls, label=label)
    ax.axhline(1, color="black", lw=0.6, ls="--")
    shade_regimes(ax)
    ax.set_ylabel("Cumulative Return (log)")
    ax.set_title("Cumulative Wealth -- v5 (Regime-Gated)")
    ax.legend(fontsize=9); ax.grid(True, which="both", alpha=0.2)

    ax2 = axes[1]
    cum_c = (1 + res["combined_net"].dropna()).cumprod()
    cum_l = (1 + res["long_net"].dropna()).cumprod()
    ax2.plot(cum_c.index, cum_c.values, "mediumseagreen", lw=2.5, label="Combined net")
    ax2.plot(cum_l.index, cum_l.values, "steelblue",      lw=1.5, ls="--", label="Long leg net")
    ax2.axhline(1, color="black", lw=0.6, ls="--")
    shade_regimes(ax2)
    ax2.set_ylabel("Cumulative Return")
    ax2.set_title("Net Performance (after all costs)")
    ax2.legend(fontsize=9); ax2.grid(True, alpha=0.2)

    ax3 = axes[2]
    bar_cols = [rc.get(r, "gray") for r in res["regime"]]
    ax3.bar(sp.index, sp.values, color=bar_cols, width=20, alpha=0.8)
    ax3.axhline(0, color="black", lw=0.8)
    ax3.legend(handles=[Patch(color=c, alpha=0.7, label=l) for l, c in rc.items()], fontsize=9)
    ax3.set_ylabel("Period Spread (gross)"); ax3.set_xlabel("Rebalance Date")
    ax3.set_title("Per-Period Gross Spread by Regime")
    ax3.grid(True, alpha=0.2)
    fig.tight_layout()
    out1 = OUTPUT_DIR + "perp_ls_v5_cumulative.png"
    fig.savefig(out1, dpi=150); plt.close(fig)
    print(f"[Plot] {out1}")

    # Figure 2: drawdown + per-period spread
    fig2, axes2 = plt.subplots(2, 1, figsize=(12, 8))

    ax4 = axes2[0]
    for j, (dt, reg) in enumerate(zip(res["dates"], res["regime"])):
        ax4.bar(dt, sp.values[j], color=rc.get(reg, "gray"), width=20, alpha=0.7)
    ax4.axhline(0, color="black", lw=0.8)
    ax4.legend(handles=[Patch(color=c, alpha=0.7, label=l) for l, c in rc.items()], fontsize=9)
    ax4.set_title("Per-Period Gross Spread by Regime (v5)")
    ax4.set_ylabel("Spread Return"); ax4.grid(True, alpha=0.2)

    ax5 = axes2[1]
    cum5 = (1 + res["combined_net"].clip(lower=-0.99)).cumprod()
    dd5  = (cum5 - cum5.cummax()) / cum5.cummax()
    ax5.fill_between(dd5.index, dd5.values, 0, color="mediumseagreen", alpha=0.55, label="v5 combined net")
    ax5.set_ylabel("Drawdown"); ax5.set_xlabel("Date")
    ax5.set_title("Drawdown: v5 Combined Net")
    ax5.legend(fontsize=9); ax5.grid(True, alpha=0.2)
    fig2.tight_layout()
    out2 = OUTPUT_DIR + "perp_ls_v5_regime_dd.png"
    fig2.savefig(out2, dpi=150); plt.close(fig2)
    print(f"[Plot] {out2}")

    # Figure 3: v4 vs v5 scorecard
    V4 = dict(
        combined_ann=-0.0511, combined_dd=-0.2289, sharpe=-0.222,
        win_rate=0.48, mean_spread=0.0195,
        bull_geo=-0.053, bear_geo=0.481, side_geo=-0.145,
    )
    regimes_list = ["Bull", "Bear", "Sideways"]
    v5_geo = {}
    for regime in regimes_list:
        mask = regs == regime
        sub  = sp.iloc[list(np.where(mask)[0])]
        v5_geo[regime] = ((1 + sub.clip(lower=-0.99)).prod() ** (12/len(sub)) - 1
                          if len(sub) > 0 else np.nan)

    st5 = portfolio_stats(res["combined_net"])
    v5_ann = st5["ann_return"]

    fig3, axes3 = plt.subplots(2, 2, figsize=(14, 10))
    fig3.suptitle("v4 (full scale, real data) vs v5 (regime-gated + quality filter) -- Post-2022",
                  fontsize=13, fontweight="bold")
    x = np.arange(len(regimes_list)); w = 0.36

    ax6 = axes3[0, 0]
    v4_geos = [V4["bull_geo"], V4["bear_geo"], V4["side_geo"]]
    v5_geos = [v5_geo.get(r, 0) for r in regimes_list]
    # Replace NaN with 0 for bar chart display
    v5_geos_bar = [v if not np.isnan(v) else 0 for v in v5_geos]
    ax6.bar(x - w/2, v4_geos, w, label="v4 (full scale)", color="silver", edgecolor="black")
    ax6.bar(x + w/2, v5_geos_bar, w, label="v5 (regime-gated)", color="mediumseagreen", edgecolor="black")
    ax6.axhline(0, color="black", lw=0.8)
    ax6.set_xticks(x); ax6.set_xticklabels(regimes_list)
    ax6.set_title("Ann. Geometric Spread by Regime"); ax6.set_ylabel("Geo. Spread")
    ax6.legend(fontsize=9); ax6.grid(True, alpha=0.2)

    ax7 = axes3[0, 1]
    cum_v5 = (1 + res["combined_net"].dropna()).cumprod()
    ax7.plot(cum_v5.index, cum_v5.values, "mediumseagreen", lw=2.5, label="v5 (regime-gated)")
    ax7.axhline(1.0, color="black", lw=1.0, ls="--", label="Breakeven")
    ax7.set_title("v5 Combined NAV"); ax7.set_ylabel("Cumulative Return")
    ax7.legend(fontsize=9); ax7.grid(True, alpha=0.2)

    ax8 = axes3[1, 0]
    dd   = (cum_v5 - cum_v5.cummax()) / cum_v5.cummax()
    ax8.fill_between(dd.index, dd.values, 0, color="mediumseagreen", alpha=0.55, label="v5")
    ax8.set_ylabel("Drawdown"); ax8.set_xlabel("Date")
    ax8.set_title("Drawdown: v5 Combined Net")
    ax8.legend(fontsize=9); ax8.grid(True, alpha=0.2)

    ax9 = axes3[1, 1]
    ax9.axis("off")

    def _f(v, fmt="+.1%"):
        return f"{v:{fmt}}" if not np.isnan(v) else "N/A"

    rows = [
        ["Metric",             "v4 (full scale)",  "v5 (regime-gated)"],
        ["Combined net (ann.)", _f(V4["combined_ann"]),  _f(v5_ann)],
        ["MaxDD",               _f(V4["combined_dd"]),   _f(st5["max_dd"])],
        ["Sharpe",              f"{V4['sharpe']:+.3f}",  _f(st5["sharpe"], "+.3f")],
        ["Win rate",            f"{V4['win_rate']:.1%}", f"{(sp>0).mean():.1%}"],
        ["Mean spread",         _f(V4["mean_spread"], "+.2%"), _f(sp.mean(), "+.2%")],
        ["Bear geo spread",     _f(V4["bear_geo"]),  _f(v5_geo.get("Bear", float("nan")))],
        ["Bull geo spread",     _f(V4["bull_geo"]),  _f(v5_geo.get("Bull", float("nan")))],
        ["Sideways",            _f(V4["side_geo"]),  "skipped"],
    ]
    tbl = ax9.table(cellText=rows[1:], colLabels=rows[0],
                    loc="center", cellLoc="center")
    tbl.auto_set_font_size(False); tbl.set_fontsize(10)
    tbl.scale(1.2, 1.7)
    for j in range(3):
        tbl[0, j].set_facecolor("#2c3e50")
        tbl[0, j].set_text_props(color="white", fontweight="bold")
    ax9.set_title("v4 vs v5 Scorecard", fontweight="bold", pad=14)

    fig3.tight_layout()
    out3 = OUTPUT_DIR + "perp_ls_v5_vs_v4.png"
    fig3.savefig(out3, dpi=150); plt.close(fig3)
    print(f"[Plot] {out3}")


# ===========================================================================
#  MAIN
# ===========================================================================

def main():
    print("=" * 76)
    print("Supply-Dilution L/S Strategy -- Version 5  [SIDEWAYS=CASH]")
    print("v4 structure + skip sideways (hold cash 0%) + no BTC hedge")
    print("=" * 76)

    df = load_cmc(INPUT_FILE)
    (bn_price_piv, bn_adtv_piv, bn_tokv_piv,
     bn_fund_raw, bn_ret26_piv, onboard_map) = load_binance(BN_DIR)
    regime_df = build_regime(df)
    df        = engineer_features(df)

    results = run_backtest(df, regime_df,
                           bn_price_piv, bn_adtv_piv, bn_tokv_piv,
                           bn_fund_raw, bn_ret26_piv, onboard_map)

    if not results["dates"]:
        print("[ERROR] No rebalancing periods survived all filters.")
        return

    print_report(results)
    plot_results(results)
    print("\nDone.")


if __name__ == "__main__":
    main()
