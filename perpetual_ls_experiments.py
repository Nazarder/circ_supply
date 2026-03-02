"""
perpetual_ls_experiments.py
===========================
Isolated experiments on top of the v6 base (Bear=1mo / Bull=2mo regime freq).
Each experiment changes exactly ONE parameter vs v6. Then a COMBINED run stacks
the individually-improving changes.

Data loaded once. All experiments share the same regime/signal pre-computation.

Experiments
-----------
  v6_base    : baseline (Bear=1mo, Bull=2mo, v4 scaling, BTC hedge ON)
  A_no_hedge : remove BTC beta hedge
  B_side_cash: Sideways regime → hold cash (scale 0,0)
  C_bull_3mo : Bull rebalance step = 3 months (quarterly)
  D_wide_bask: widen basket cut 12% → 18% (more tokens, less concentration)
  E_fund_sig : blend 30% funding-rank into supply signal
  F_momentum : exclude long candidates that underperform BTC by >25pp (13w)
  G_stop_loss: exclude short tokens that squeezed >50% last period (1-period ban)
  H_fund_gate: halve short_scale when short basket avg 8h funding < -0.10%
  COMBINED   : stack A + B + C + D + G + H  (the clean, non-signal changes)
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

# ===========================================================================
#  FIXED CONFIG  (v6 defaults — not changed by experiments)
# ===========================================================================

CMC_FILE   = "cmc_historical_top300_filtered_with_supply.csv"
BN_DIR     = "binance_perp_data"
START_DATE = pd.Timestamp("2022-01-01")

TOP_N_EXCLUDE        = 20
MAX_RANK             = 250
MIN_MKTCAP           = 1e8
MIN_SUPPLY_HISTORY   = 26
FFILL_LIMIT          = 1

SUPPLY_WINDOW        = 13
SUPPLY_WINDOW_SLOW   = 52
SIGNAL_SLOW_WEIGHT   = 0.50
SUPPLY_INF_WINS      = (0.02, 0.98)
MIN_VOLUME           = 1e6

MIN_BASKET_SIZE      = 6
ADTV_POS_CAP         = 0.20
TOKEN_VOL_WINDOW     = 8

TAKER_FEE            = 0.0004
SLIPPAGE_K           = 0.0005
MIN_TURNOVER         = 0.001
MAX_SLIPPAGE         = 0.02

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

ALTSEASON_THRESHOLD  = 0.75
ALTSEASON_LOOKBACK   = 4
SHORT_SQUEEZE_PRIOR  = 0.40
SHORT_CB_LOSS        = 0.40
BTC_HEDGE_LOOKBACK   = 12
BTC_HEDGE_MAX        = 1.0

WINS_LOW  = 0.01
WINS_HIGH = 0.99

# ===========================================================================
#  EXPERIMENT CONFIGS  (each overrides exactly ONE group of defaults)
# ===========================================================================

_D = dict(
    btc_hedge        = True,
    sideways_cash    = False,
    bull_step        = 2,
    bear_step        = 1,
    side_step        = 1,
    rebal_freq       = "monthly",  # "monthly" | "biweekly" | "weekly"
    long_entry_pct   = 0.12,
    short_entry_pct  = 0.88,
    long_exit_pct    = 0.18,
    short_exit_pct   = 0.82,
    funding_weight   = 0.00,   # 0 = off, 0.3 = 30% funding rank in signal
    momentum_weeks   = 0,      # 0 = off, 13 = 13w relative momentum filter
    stop_loss_pct    = 0.00,   # 0 = off, 0.5 = exclude shorts that rose >50%
    fund_short_gate  = 0.00,   # 0 = off, -0.001 = halve short when avg fund < -0.1%/8h
)

EXPERIMENTS = {
    "v6_base":     {**_D},
    "A_no_hedge":  {**_D, "btc_hedge": False},
    "B_side_cash": {**_D, "sideways_cash": True},
    "C_bull_3mo":  {**_D, "bull_step": 3},
    "D_wide_bask": {**_D, "long_entry_pct": 0.18, "short_entry_pct": 0.82,
                          "long_exit_pct":  0.25, "short_exit_pct":  0.75},
    "E_fund_sig":  {**_D, "funding_weight": 0.30},
    "F_momentum":  {**_D, "momentum_weeks": 13},
    "G_stop_loss": {**_D, "stop_loss_pct": 0.50},
    "H_fund_gate": {**_D, "fund_short_gate": -0.001},
    # ── Biweekly rebalancing ─────────────────────────────────────────────────
    # I_biweekly: step=1 for all regimes → rebalance every 2 weeks always
    "I_biweekly":    {**_D, "rebal_freq": "biweekly",
                           "bull_step": 1, "bear_step": 1, "side_step": 1},
    # J_biweek_b2: biweekly but skip one in Bull (Bear/Side=2w, Bull=4w)
    "J_biweek_b2":   {**_D, "rebal_freq": "biweekly"},   # inherits bull_step=2
    "COMBINED":    {**_D,
                    "btc_hedge":      False,
                    "sideways_cash":  True,
                    "bull_step":      3,
                    "long_entry_pct": 0.18, "short_entry_pct": 0.82,
                    "long_exit_pct":  0.25, "short_exit_pct":  0.75,
                    "funding_weight": 0.30,
                    "momentum_weeks": 13,
                    "stop_loss_pct":  0.50,
                    "fund_short_gate":-0.001},
}

# ===========================================================================
#  EXCLUSION LISTS  (same as v4/v6)
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
#  HELPERS
# ===========================================================================

def _ppy(returns):
    if len(returns) < 2: return 12.0
    gaps = np.diff(returns.index).astype("timedelta64[D]").astype(float)
    return 365.25 / max(float(np.median(gaps)), 1.0)

def sharpe_lo(returns, ppy, max_lags=4):
    r = returns.dropna()
    if len(r) < max_lags + 2 or r.std() == 0: return np.nan
    sr_raw = r.mean() / r.std()
    ac = [float(r.autocorr(lag=q)) for q in range(1, max_lags + 1)]
    w  = [1.0 - q / (max_lags + 1) for q in range(1, max_lags + 1)]
    corr = 1.0 + 2.0 * sum(a * wi for a, wi in zip(ac, w))
    return sr_raw * np.sqrt(ppy) / np.sqrt(max(corr, 1e-8))

def portfolio_stats(returns):
    r = returns.dropna()
    if len(r) < 2:
        return dict(ann_return=np.nan, vol=np.nan, sharpe=np.nan,
                    sharpe_lo=np.nan, sortino=np.nan, max_dd=np.nan)
    cum        = (1 + r).cumprod()
    yrs        = max((r.index[-1] - r.index[0]).days / 365.25, 1/52)
    cf         = float(cum.iloc[-1])
    ann        = (cf ** (1/yrs) - 1) if cf > 0 else np.nan
    ppy        = _ppy(r)
    vol        = r.std() * np.sqrt(ppy)
    sharpe     = ann / vol if vol > 0 and not np.isnan(ann) else np.nan
    slo        = sharpe_lo(r, ppy)
    dn         = r[r < 0]
    sortino    = (ann / (np.sqrt((dn**2).mean()) * np.sqrt(ppy))
                  if len(dn) > 0 and not np.isnan(ann) else np.nan)
    mx         = cum.cummax()
    max_dd     = float(((cum - mx) / mx).min())
    return dict(ann_return=ann, vol=vol, sharpe=sharpe, sharpe_lo=slo,
                sortino=sortino, max_dd=max_dd)

def _fmt(v):    return f"{v:+.2%}" if pd.notna(v) else "  N/A  "
def _fmtf(v):  return f"{v:+.3f}" if pd.notna(v) else "  N/A  "

def inv_vol_adtv_weights(symbols, vol_map, adtv_map, cap=ADTV_POS_CAP):
    raw = {}
    for s in symbols:
        vol  = max(float(vol_map.get(s)  if pd.notna(vol_map.get(s))  else 1.0), 0.05)
        adtv = max(float(adtv_map.get(s) if pd.notna(adtv_map.get(s)) else 0.0), 0.0)
        raw[s] = (adtv ** 0.5 + 1.0) / vol
    total = sum(raw.values())
    if total <= 0: n = len(symbols); return {s: 1/n for s in symbols}
    w = {k: min(v/total, cap) for k, v in raw.items()}
    t2 = sum(w.values())
    if t2 <= 0: n = len(symbols); return {s: 1/n for s in symbols}
    return {k: v/t2 for k, v in w.items()}

# ===========================================================================
#  DATA LOADING  (identical to v6)
# ===========================================================================

def load_cmc(path):
    df = pd.read_csv(path, parse_dates=["snapshot_date"])
    df = df[df["symbol"].apply(lambda s: str(s).isascii())]
    df = df.sort_values(["symbol","snapshot_date"]).reset_index(drop=True)
    df = df[df["price"].notna() & (df["price"] > 0) & df["market_cap"].notna()].copy()
    for col in ["price","circulating_supply"]:
        df[col] = df.groupby("symbol")[col].transform(lambda s: s.ffill(limit=FFILL_LIMIT))
    df = df[df["circulating_supply"].notna() & (df["circulating_supply"] > 0)].copy()
    print(f"[CMC] {len(df):,} rows | {df['symbol'].nunique():,} symbols | "
          f"{df['snapshot_date'].min().date()} -> {df['snapshot_date'].max().date()}")
    return df

def load_binance(bn_dir):
    ohlcv   = pd.read_parquet(f"{bn_dir}/weekly_ohlcv.parquet")
    funding = pd.read_parquet(f"{bn_dir}/weekly_funding.parquet")
    meta    = pd.read_csv(f"{bn_dir}/symbol_meta.csv", parse_dates=["onboard_date"])

    ohlcv["cmc_date"]   = ohlcv["week_start"]   + pd.Timedelta(days=6)
    funding["cmc_date"] = funding["week_start"]  + pd.Timedelta(days=6)

    bn_price_piv = ohlcv.pivot_table(index="cmc_date", columns="symbol",
                                     values="close", aggfunc="last")
    bn_adtv_piv  = ohlcv.pivot_table(index="cmc_date", columns="symbol",
                                     values="quote_volume", aggfunc="last")

    close_df  = ohlcv.sort_values("cmc_date").pivot_table(
        index="cmc_date", columns="symbol", values="close", aggfunc="last")
    tokv_df   = close_df.pct_change(1).rolling(TOKEN_VOL_WINDOW, min_periods=4).std() * np.sqrt(52)

    # Keep raw funding with funding_mean for the precomputed trailing signal
    bn_fund_raw = funding[["symbol","week_start","funding_sum","funding_mean"]].copy()

    onboard_map = dict(zip(meta["symbol"], meta["onboard_date"]))
    print(f"[Binance] price: {bn_price_piv.shape}  funding rows: {len(bn_fund_raw):,}  "
          f"symbols w/ onboard: {len(onboard_map)}")
    return bn_price_piv, bn_adtv_piv, tokv_df, bn_fund_raw, onboard_map

def build_regime(df):
    top = df[df["rank"] <= 100].copy().sort_values(["symbol","snapshot_date"])
    top["pct_ret"] = top.groupby("symbol")["price"].pct_change(1)
    top = top[top["pct_ret"].notna()]
    def cap_wt(g):
        t = g["market_cap"].sum()
        return float((g["market_cap"]/t * g["pct_ret"]).sum()) if t > 0 else np.nan
    idx = (top.groupby("snapshot_date")
              .apply(cap_wt, include_groups=False)
              .reset_index().rename(columns={0:"index_return"})
              .sort_values("snapshot_date"))
    idx["index_price"] = (1 + idx["index_return"].fillna(0)).cumprod()
    idx["index_ma"]    = idx["index_price"].rolling(REGIME_MA_WINDOW, min_periods=1).mean()
    ratio = idx["index_price"] / idx["index_ma"]
    idx["regime"] = np.where(ratio >= BULL_BAND, "Bull",
                    np.where(ratio <= BEAR_BAND, "Bear", "Sideways"))
    btc_rets = df[df["symbol"]=="BTC"].set_index("snapshot_date")["price"].pct_change(1)
    vol_s    = btc_rets.reindex(idx["snapshot_date"]).rolling(VOL_WINDOW, min_periods=4).std() * np.sqrt(52)
    idx["high_vol"] = vol_s.values > HIGH_VOL_THRESHOLD
    print(f"[Regime] Bull={(idx['regime']=='Bull').sum()} "
          f"Bear={(idx['regime']=='Bear').sum()} "
          f"Sideways={(idx['regime']=='Sideways').sum()}")
    return idx[["snapshot_date","index_return","regime","high_vol"]]

def engineer_features(df):
    grp = df.groupby("symbol", group_keys=False)
    df["supply_inf_13w"]     = grp["circulating_supply"].transform(lambda s: s.pct_change(SUPPLY_WINDOW))
    df["supply_inf_52w"]     = grp["circulating_supply"].transform(lambda s: s.pct_change(SUPPLY_WINDOW_SLOW))
    df["supply_inf"]         = df["supply_inf_13w"]
    df["supply_hist_count"]  = grp["supply_inf"].transform(lambda s: s.notna().cumsum())
    df["turnover"]           = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"]           = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)
    return df

# ===========================================================================
#  PRE-COMPUTATION  (done once, shared across all experiments)
# ===========================================================================

def precompute_fund_4w(bn_fund_raw, all_rebal):
    """4-week trailing avg 8h funding rate per symbol per rebal date.
    Positive = market in contango (longs paying); negative = backwardation."""
    result = {}
    for t0 in all_rebal:
        t_lb = t0 - pd.Timedelta(weeks=4)
        mask = (bn_fund_raw["week_start"] > t_lb) & (bn_fund_raw["week_start"] <= t0)
        rows = bn_fund_raw[mask]
        if len(rows) > 0 and "funding_mean" in rows.columns:
            result[t0] = rows.groupby("symbol")["funding_mean"].mean().to_dict()
        else:
            result[t0] = {}
    return result

def precompute_momentum_13w(bn_price_piv, all_rebal):
    """13-week price return per symbol per rebal date (Binance perp prices)."""
    result = {}
    price_dates = sorted(bn_price_piv.index)
    for t0 in all_rebal:
        t_lb_target = t0 - pd.Timedelta(weeks=13)
        # find closest earlier date in Binance pivot
        candidates = [d for d in price_dates if d <= t_lb_target]
        if not candidates or t0 not in bn_price_piv.index:
            result[t0] = {}
            continue
        t_lb = max(candidates)
        p0 = bn_price_piv.loc[t_lb]
        p1 = bn_price_piv.loc[t0]
        rets = (p1 / p0 - 1).dropna()
        result[t0] = rets.to_dict()
    return result

# ===========================================================================
#  MAIN BACKTEST  (parametrised by cfg dict)
# ===========================================================================

def run_experiment(cfg, df, regime_df, bn_price_piv, bn_adtv_piv,
                   bn_tokv_piv, bn_fund_raw, onboard_map,
                   fund_4w, momentum_13w):

    df["ym"]  = df["snapshot_date"].dt.to_period("M")
    all_rebal = sorted(df.groupby("ym")["snapshot_date"].min().tolist())
    regime_map = regime_df.set_index("snapshot_date")[["regime","high_vol"]].to_dict("index")

    # ── Universe snapshot ────────────────────────────────────────────────────
    bn_symbols = set(bn_price_piv.columns)
    inf_snap = df[df["snapshot_date"].isin(all_rebal)].copy()
    inf_snap = inf_snap[inf_snap["supply_inf_13w"].notna()]
    inf_snap = inf_snap[~inf_snap["symbol"].isin(EXCLUDED)]
    inf_snap = inf_snap[(inf_snap["rank"] > TOP_N_EXCLUDE) & (inf_snap["rank"] <= MAX_RANK)]
    inf_snap = inf_snap[inf_snap["market_cap"]  >= MIN_MKTCAP]
    inf_snap = inf_snap[inf_snap["supply_hist_count"] >= MIN_SUPPLY_HISTORY]
    inf_snap = inf_snap[inf_snap["symbol"].isin(bn_symbols)]

    # ── Altseason map ────────────────────────────────────────────────────────
    price_piv_cmc = df.pivot_table(index="snapshot_date", columns="symbol",
                                   values="price", aggfunc="last")
    slip_piv = df.pivot_table(index="snapshot_date", columns="symbol",
                               values="slippage", aggfunc="last")

    altseason_map = {}
    for ii, t0r in enumerate(all_rebal):
        if ii < ALTSEASON_LOOKBACK or t0r not in price_piv_cmc.index:
            altseason_map[t0r] = False; continue
        t_lb = all_rebal[ii - ALTSEASON_LOOKBACK]
        if t_lb not in price_piv_cmc.index:
            altseason_map[t0r] = False; continue
        p_lb  = price_piv_cmc.loc[t_lb]
        p_now = price_piv_cmc.loc[t0r]
        btc0  = float(p_lb.get("BTC", np.nan))
        btc1  = float(p_now.get("BTC", np.nan))
        if not pd.notna(btc0) or btc0 <= 0 or not pd.notna(btc1):
            altseason_map[t0r] = False; continue
        btc_4w = btc1/btc0 - 1
        top50  = df[(df["snapshot_date"]==t0r) &
                    (df["rank"].between(3,50)) &
                    (~df["symbol"].isin(EXCLUDED | {"BTC","ETH"}))]["symbol"].tolist()
        alt_rets = [p_now.get(s,np.nan)/p_lb.get(s,np.nan)-1 for s in top50
                    if pd.notna(p_lb.get(s)) and p_lb.get(s,0)>0 and pd.notna(p_now.get(s))]
        altseason_map[t0r] = (sum(r > btc_4w for r in alt_rets) / len(alt_rets) > ALTSEASON_THRESHOLD
                               if len(alt_rets) >= 10 else False)

    # ── Regime-aware rebalancing schedule ────────────────────────────────────
    step_map = {"Bear": cfg["bear_step"], "Bull": cfg["bull_step"], "Sideways": cfg["side_step"]}

    if cfg.get("rebal_freq", "monthly") in ("biweekly", "weekly"):
        # Use Binance weekly price dates as the candidate pool
        _all_bn = sorted(bn_price_piv.index.tolist())
        _step_n = 2 if cfg.get("rebal_freq") == "biweekly" else 1
        _cands  = [d for d in _all_bn if d >= START_DATE][::_step_n]
        # Map each candidate to the most recent monthly CMC snapshot (for signal/regime)
        _monthly = sorted(all_rebal)
        def _nearest_monthly(t):
            hits = [d for d in _monthly if d <= t]
            return max(hits) if hits else None
        sig_map      = {t: _nearest_monthly(t) for t in _cands}
        all_eligible = [t for t in _cands if sig_map.get(t) is not None]
    else:
        all_eligible = [d for d in all_rebal if d >= START_DATE]
        sig_map      = {d: d for d in all_eligible}

    active_rebals = []
    ii = 0
    while ii < len(all_eligible):
        t_cur   = all_eligible[ii]
        active_rebals.append(t_cur)
        sig_cur = sig_map.get(t_cur, t_cur)
        reg     = regime_map.get(sig_cur, {}).get("regime", "Sideways")
        ii     += step_map.get(reg, 1)
    sorted_rebals = active_rebals

    # ── BTC forward returns (over actual holding periods) ────────────────────
    btc_fwd = {}
    for ii in range(len(sorted_rebals)-1):
        t0r, t1r = sorted_rebals[ii], sorted_rebals[ii+1]
        p0 = float(bn_price_piv.loc[t0r,"BTC"]) if (t0r in bn_price_piv.index
             and "BTC" in bn_price_piv.columns) else np.nan
        p1 = float(bn_price_piv.loc[t1r,"BTC"]) if (t1r in bn_price_piv.index
             and "BTC" in bn_price_piv.columns) else np.nan
        btc_fwd[t0r] = (p1/p0-1) if pd.notna(p0) and pd.notna(p1) and p0>0 else np.nan

    # ── State ────────────────────────────────────────────────────────────────
    prev_long_set  = set()
    prev_short_set = set()
    zombie_shorts  = set()   # [G] 1-period ban on squeezed short tokens
    raw_hist, btc_hist, beta_hist = [], [], []

    # ── Accumulators ─────────────────────────────────────────────────────────
    dates_out, long_gross_l, short_gross_l = [], [], []
    long_net_l, short_net_l, combined_net_l, combined_hedged_l = [], [], [], []
    basket_sizes_l, regime_out_l, scale_out_l = [], [], []
    fund_long_cum, fund_short_cum = 0.0, 0.0
    fund_l_l, fund_s_l = [], []
    cb_count, altseason_count = 0, 0

    for i, t0 in enumerate(sorted_rebals[:-1]):
        t1        = sorted_rebals[i+1]
        hold_days = max((t1 - t0).days, 1)

        if t0 not in bn_price_piv.index or t1 not in bn_price_piv.index:
            continue

        # sig_date: monthly CMC snapshot used for signal/regime (same as t0 for monthly)
        sig_date = sig_map.get(t0, t0)

        univ = inf_snap[inf_snap["snapshot_date"] == sig_date].copy()
        univ = univ[univ["symbol"].apply(
            lambda s: pd.notna(onboard_map.get(s)) and onboard_map.get(s) <= t0)]

        if t0 in bn_adtv_piv.index:
            adtv_now = bn_adtv_piv.loc[t0]
            univ = univ[univ["symbol"].apply(
                lambda s: pd.notna(adtv_now.get(s)) and float(adtv_now.get(s,0)) >= MIN_VOLUME*7)]

        if len(univ) < MIN_BASKET_SIZE * 2:
            continue

        # Winsorise supply signal
        for col in ["supply_inf_13w","supply_inf_52w"]:
            cv = univ[col].dropna()
            if len(cv) > 4:
                lo_w, hi_w = cv.quantile(SUPPLY_INF_WINS)
                univ[col]  = univ[col].clip(lo_w, hi_w)

        # Supply composite rank [0,1]
        univ["rank_13w"] = univ["supply_inf_13w"].rank(pct=True)
        univ["rank_52w"] = univ["supply_inf_52w"].rank(pct=True)
        univ["rank_52w"] = univ["rank_52w"].fillna(univ["rank_13w"])
        supply_rank = ((1-SIGNAL_SLOW_WEIGHT)*univ["rank_13w"]
                       + SIGNAL_SLOW_WEIGHT*univ["rank_52w"])

        # [E] Blend funding-rank into signal
        if cfg["funding_weight"] > 0 and t0 in fund_4w:
            f4w = fund_4w[t0]
            univ["fund_4w"] = univ["symbol"].map(f4w)
            valid_fund = univ["fund_4w"].notna()
            if valid_fund.sum() >= 4:
                univ["fund_rank"] = univ["fund_4w"].rank(pct=True).fillna(0.5)
                w = cfg["funding_weight"]
                pct_rank = (1-w)*supply_rank + w*univ["fund_rank"]
                # Re-rank to restore uniform [0,1] — the supply/funding correlation
                # can compress the distribution, starving the entry thresholds
                pct_rank = pct_rank.rank(pct=True)
            else:
                pct_rank = supply_rank
        else:
            pct_rank = supply_rank

        univ["pct_rank"] = pct_rank
        rank_map  = univ.set_index("symbol")["pct_rank"].to_dict()
        all_syms  = set(univ["symbol"])

        # Squeeze exclusion
        squeezed = set()
        if i > 0:
            t_prev = sorted_rebals[i-1]
            if t_prev in bn_price_piv.index:
                p_prev = bn_price_piv.loc[t_prev]
                p_now  = bn_price_piv.loc[t0]
                squeezed = {s for s in all_syms
                            if s in p_prev and s in p_now
                            and pd.notna(p_prev[s]) and float(p_prev[s])>0
                            and pd.notna(p_now[s])
                            and float(p_now[s])/float(p_prev[s])-1 > SHORT_SQUEEZE_PRIOR}

        # Inner buffer band
        lep  = cfg["long_entry_pct"]
        lxp  = cfg["long_exit_pct"]
        sep  = cfg["short_entry_pct"]
        sxp  = cfg["short_exit_pct"]

        entry_long   = {s for s in all_syms if rank_map[s] <= lep}
        stay_long    = {s for s in (prev_long_set & all_syms) if rank_map[s] <= lxp}
        basket_long  = entry_long | stay_long

        entry_short  = ({s for s in all_syms if rank_map[s] >= sep} - squeezed)
        # [G] Exclude last period's squeezed short tokens
        if cfg["stop_loss_pct"] > 0:
            entry_short -= zombie_shorts
        stay_short   = {s for s in (prev_short_set & all_syms) if rank_map[s] >= sxp}
        basket_short = entry_short | stay_short

        # [F] Momentum filter: exclude long tokens with 13w return < BTC - 25pp
        if cfg["momentum_weeks"] > 0 and t0 in momentum_13w:
            mom = momentum_13w[t0]
            btc_mom = mom.get("BTC", 0.0)
            THRESH = -0.25
            weak   = {s for s in basket_long
                      if s in mom and (mom[s] - btc_mom) < THRESH}
            basket_long -= weak

        overlap      = basket_long & basket_short
        basket_long  -= overlap
        basket_short -= overlap

        if len(basket_long) < MIN_BASKET_SIZE or len(basket_short) < MIN_BASKET_SIZE:
            prev_long_set  = basket_long
            prev_short_set = basket_short
            continue

        # Turnover
        to_l = (1 - len(basket_long  & prev_long_set)  / max(len(basket_long  | prev_long_set),  1)) if prev_long_set  else 1.0
        to_s = (1 - len(basket_short & prev_short_set) / max(len(basket_short | prev_short_set), 1)) if prev_short_set else 1.0

        prev_long_set  = basket_long
        prev_short_set = basket_short

        # Forward returns
        p0_bn = bn_price_piv.loc[t0]
        p1_bn = bn_price_piv.loc[t1]
        fwd   = (p1_bn / p0_bn - 1).dropna()
        lo_f, hi_f = fwd.quantile(WINS_LOW), fwd.quantile(WINS_HIGH)
        fwd   = fwd.clip(lower=lo_f, upper=hi_f).clip(lower=-1.0)

        # Funding over holding period
        fund_mask = (bn_fund_raw["week_start"] >  t0) & \
                    (bn_fund_raw["week_start"] <= t1 + pd.Timedelta(days=1))
        fund_rows = bn_fund_raw[fund_mask]
        fund_row  = fund_rows.groupby("symbol")["funding_sum"].sum() if len(fund_rows)>0 else pd.Series(dtype=float)

        adtv_row = bn_adtv_piv.loc[t0]       if t0       in bn_adtv_piv.index else pd.Series(dtype=float)
        tokv_row = bn_tokv_piv.loc[t0]       if t0       in bn_tokv_piv.index else pd.Series(dtype=float)
        sl_row   = slip_piv.loc[sig_date]    if sig_date in slip_piv.index    else pd.Series(dtype=float)

        def basket_ret(syms):
            s_list = [s for s in syms if s in fwd.index and pd.notna(fwd[s])]
            if not s_list: return np.nan, MAX_SLIPPAGE, 0.0
            vol_m  = {s: float(tokv_row.get(s,1.0) if pd.notna(tokv_row.get(s)) else 1.0) for s in s_list}
            adtv_m = {s: float(adtv_row.get(s,0)  if pd.notna(adtv_row.get(s)) else 0.0) for s in s_list}
            w      = inv_vol_adtv_weights(s_list, vol_m, adtv_m)
            ret    = sum(w[s]*float(fwd[s]) for s in s_list)
            slip   = sum(w[s]*float(sl_row.get(s,MAX_SLIPPAGE) or MAX_SLIPPAGE) for s in s_list)
            fund   = sum(w[s]*float(fund_row[s] if s in fund_row.index and pd.notna(fund_row[s]) else 0.0) for s in s_list)
            return float(ret), float(slip), float(fund)

        r_lg, slip_l, fund_l = basket_ret(basket_long)
        r_sg, slip_s, fund_s = basket_ret(basket_short)

        if pd.isna(r_lg) or pd.isna(r_sg):
            continue

        # [G] Record which shorts squeezed this period (for next period's ban)
        if cfg["stop_loss_pct"] > 0:
            new_zombies = set()
            for s in basket_short:
                if s in p0_bn.index and s in p1_bn.index:
                    p0s = float(p0_bn[s]) if pd.notna(p0_bn[s]) and float(p0_bn[s])>0 else np.nan
                    p1s = float(p1_bn[s]) if pd.notna(p1_bn[s]) else np.nan
                    if pd.notna(p0s) and pd.notna(p1s) and (p1s/p0s-1) > cfg["stop_loss_pct"]:
                        new_zombies.add(s)
            zombie_shorts = new_zombies

        # Circuit breaker
        if r_sg > SHORT_CB_LOSS:
            r_sg = SHORT_CB_LOSS
            cb_count += 1

        # Regime & scaling  (use sig_date for monthly CMC regime lookup)
        reg_info = regime_map.get(sig_date, {"regime":"Sideways","high_vol":False})
        regime   = reg_info.get("regime",  "Sideways")
        high_vol = bool(reg_info.get("high_vol", False))

        long_scale  = REGIME_LS_SCALE.get((regime, high_vol),
                      REGIME_LS_SCALE.get((regime, False), (1.0,1.0)))[0]
        short_scale = REGIME_LS_SCALE.get((regime, high_vol),
                      REGIME_LS_SCALE.get((regime, False), (1.0,1.0)))[1]

        # [B] Sideways = hold cash
        if cfg["sideways_cash"] and regime == "Sideways":
            long_scale = short_scale = 0.0

        # Altseason veto
        if altseason_map.get(sig_date, False):
            short_scale = 0.0
            altseason_count += 1

        # [H] Funding-aware short gate
        if cfg["fund_short_gate"] != 0.0 and t0 in fund_4w:
            f4w_short = [fund_4w[t0].get(s, 0.0) for s in basket_short if s in fund_4w[t0]]
            if f4w_short:
                avg_fund_short = np.mean(f4w_short)
                if avg_fund_short < cfg["fund_short_gate"]:
                    short_scale *= 0.5

        # [B] Record 0-return period when Sideways=cash
        if long_scale == 0.0 and short_scale == 0.0:
            dates_out.append(t0); long_gross_l.append(0.0); short_gross_l.append(0.0)
            long_net_l.append(0.0); short_net_l.append(0.0)
            combined_net_l.append(0.0); combined_hedged_l.append(0.0)
            basket_sizes_l.append((len(basket_long), len(basket_short)))
            regime_out_l.append(regime); scale_out_l.append((0.0,0.0))
            fund_l_l.append(0.0); fund_s_l.append(0.0)
            raw_hist.append(0.0); btc_hist.append(btc_fwd.get(t0, np.nan)); beta_hist.append(np.nan)
            continue

        # Net returns
        fee      = 2 * TAKER_FEE
        r_ln     = r_lg  - fee - slip_l - fund_l
        r_sn     = -r_sg - fee - slip_s + fund_s

        denom    = long_scale + short_scale if (long_scale + short_scale) > 0 else 1.0
        r_comb   = (long_scale*r_ln + short_scale*r_sn) / denom

        r_ln   = max(r_ln,   -1.0)
        r_sn   = max(r_sn,   -1.0)
        r_comb = max(r_comb, -1.0)

        # [A] BTC beta hedge
        hedge_ret = 0.0
        beta_used = np.nan
        if cfg["btc_hedge"] and len(raw_hist) >= BTC_HEDGE_LOOKBACK:
            pairs = [(c,b) for c,b in zip(raw_hist[-BTC_HEDGE_LOOKBACK:], btc_hist[-BTC_HEDGE_LOOKBACK:])
                     if not np.isnan(c) and not np.isnan(b)]
            if len(pairs) >= 4:
                ca = np.array([p[0] for p in pairs])
                ba = np.array([p[1] for p in pairs])
                vb = np.var(ba)
                if vb > 1e-10:
                    beta      = float(np.clip(np.cov(ca,ba)[0,1]/vb, 0.0, BTC_HEDGE_MAX))
                    btc_r     = btc_fwd.get(t0, 0.0) or 0.0
                    hedge_ret = -beta * btc_r
                    beta_used = beta

        raw_hist.append(r_comb)
        btc_hist.append(btc_fwd.get(t0, np.nan))
        beta_hist.append(beta_used)
        r_hedged = max(r_comb + hedge_ret, -1.0)

        fund_long_cum  += -fund_l
        fund_short_cum += +fund_s
        fund_l_l.append(-fund_l)
        fund_s_l.append(+fund_s)

        dates_out.append(t0)
        long_gross_l.append(max(r_lg,  -1.0))
        short_gross_l.append(max(r_sg, -1.0))
        long_net_l.append(r_ln)
        short_net_l.append(r_sn)
        combined_net_l.append(r_comb)
        combined_hedged_l.append(r_hedged)
        basket_sizes_l.append((len(basket_long), len(basket_short)))
        regime_out_l.append(regime)
        scale_out_l.append((long_scale, short_scale))

    idx = pd.DatetimeIndex(dates_out)
    spread = pd.Series([lg-sg for lg,sg in zip(long_gross_l,short_gross_l)], index=idx)
    regs   = np.array(regime_out_l)

    def geo_spread(mask):
        sub = spread.iloc[list(np.where(mask)[0])]
        if len(sub) == 0: return np.nan
        prod = (1 + sub.clip(lower=-0.99)).prod()
        if prod <= 0: return np.nan
        return float(prod ** (12/len(sub)) - 1)

    return dict(
        n            = len(dates_out),
        dates        = dates_out,
        combined_net = pd.Series(combined_net_l, index=idx),
        combined_hed = pd.Series(combined_hedged_l, index=idx),
        long_gross   = pd.Series(long_gross_l, index=idx),
        short_gross  = pd.Series(short_gross_l, index=idx),
        long_net     = pd.Series(long_net_l, index=idx),
        short_net    = pd.Series(short_net_l, index=idx),
        spread       = spread,
        regime       = regs,
        cb_count     = cb_count,
        altseason_count = altseason_count,
        fund_l_cum   = fund_long_cum,
        fund_s_cum   = fund_short_cum,
        bull_geo     = geo_spread(regs=="Bull"),
        bear_geo     = geo_spread(regs=="Bear"),
        side_geo     = geo_spread(regs=="Sideways"),
        bull_n       = int((regs=="Bull").sum()),
        bear_n       = int((regs=="Bear").sum()),
        side_n       = int((regs=="Sideways").sum()),
        avg_bask     = (np.mean([s[0] for s in basket_sizes_l]) if basket_sizes_l else 0,
                        np.mean([s[1] for s in basket_sizes_l]) if basket_sizes_l else 0),
    )

# ===========================================================================
#  REPORTING
# ===========================================================================

def print_results(all_res):
    print("\n" + "=" * 110)
    print("EXPERIMENT RESULTS — each vs v6_base  |  Bear=1mo base / Bull step varies / Sideways as noted")
    print("=" * 110)
    print(f"{'Experiment':<16} {'N':>3} {'B':>2} {'Br':>3} {'S':>3} "
          f"{'CombNet':>9} {'Δvs v6':>8} {'MaxDD':>8} "
          f"{'Bull Geo':>9} {'Bear Geo':>9} {'Side Geo':>9} "
          f"{'WinRate':>8} {'CB':>3} {'AvgBsk':>7}")
    print("-" * 110)

    base = all_res.get("v6_base")
    base_st = portfolio_stats(base["combined_net"]) if base else {}

    for name, res in all_res.items():
        st    = portfolio_stats(res["combined_net"])
        delta = st["ann_return"] - base_st.get("ann_return", np.nan)
        wr    = (res["spread"] > 0).mean()
        bg    = (res["avg_bask"][0] + res["avg_bask"][1]) / 2
        print(f"{name:<16} {res['n']:>3} "
              f"{res['bull_n']:>2} {res['bear_n']:>3} {res['side_n']:>3} "
              f"{_fmt(st['ann_return']):>9} {_fmt(delta):>8} "
              f"{_fmt(st['max_dd']):>8} "
              f"{_fmt(res['bull_geo']):>9} {_fmt(res['bear_geo']):>9} "
              f"{_fmt(res['side_geo']):>9} "
              f"{wr:>8.1%} {res['cb_count']:>3} {bg:>7.1f}")

    print("=" * 110)
    print("Columns: N=periods  B=Bull  Br=Bear  S=Side  CombNet=ann.return(unhedged)")
    print("         Δvs_v6=improvement over v6_base  WinRate=L>S gross  CB=circuit-breaker hits")

    # Detailed stats for key experiments
    print("\n" + "─" * 76)
    for key in ["v6_base", "E_fund_sig", "I_biweekly", "J_biweek_b2", "COMBINED"]:
        if key not in all_res: continue
        res = all_res[key]
        st  = portfolio_stats(res["combined_net"])
        sth = portfolio_stats(res["combined_hed"])
        stl = portfolio_stats(res["long_net"])
        sts = portfolio_stats(res["short_net"])
        print(f"\n[{key}]  n={res['n']}  Bull={res['bull_n']} Bear={res['bear_n']} Side={res['side_n']}")
        print(f"  {'Series':<28} {'Ann.Ret':>9} {'Vol':>9} {'Sharpe':>8} {'MaxDD':>9}")
        print(f"  {'-'*57}")
        for nm, sr in [("Long net",     res["long_net"]),
                       ("Short net",    res["short_net"]),
                       ("Combined net", res["combined_net"]),
                       ("Combined hed", res["combined_hed"])]:
            s = portfolio_stats(sr)
            print(f"  {nm:<28} {_fmt(s['ann_return']):>9} {_fmt(s['vol']):>9} "
                  f"{_fmtf(s['sharpe']):>8} {_fmt(s['max_dd']):>9}")
        print(f"  CB hits: {res['cb_count']}  "
              f"Fund drag(long): {res['fund_l_cum']:+.2%}  "
              f"Fund credit(short): {res['fund_s_cum']:+.2%}")

# ===========================================================================
#  MAIN
# ===========================================================================

def main():
    print("=" * 80)
    print("Supply-Dilution L/S — Experiment Suite (v6 base + 8 isolated experiments)")
    print("=" * 80)

    df = load_cmc(CMC_FILE)
    bn_price_piv, bn_adtv_piv, bn_tokv_piv, bn_fund_raw, onboard_map = load_binance(BN_DIR)
    regime_df = build_regime(df)
    df        = engineer_features(df)

    # Pre-compute signals once — use ALL Binance weekly dates so biweekly
    # experiments (I/J) can look up fund_4w / momentum for any weekly date
    df["ym"]      = df["snapshot_date"].dt.to_period("M")
    all_rebal     = sorted(df.groupby("ym")["snapshot_date"].min().tolist())
    all_bn_dates  = sorted(bn_price_piv.index.tolist())
    print(f"[Pre-compute] 4w trailing funding ({len(all_bn_dates)} Binance weekly dates)...")
    fund_4w  = precompute_fund_4w(bn_fund_raw, all_bn_dates)
    print(f"[Pre-compute] 13w momentum ({len(all_bn_dates)} dates)...")
    mom_13w  = precompute_momentum_13w(bn_price_piv, all_bn_dates)

    all_res = {}
    for name, cfg in EXPERIMENTS.items():
        print(f"\n>>> Running: {name}  "
              f"(freq={cfg.get('rebal_freq','monthly')} "
              f"hedge={cfg['btc_hedge']} side_cash={cfg['sideways_cash']} "
              f"steps={cfg['bull_step']}/{cfg['bear_step']}/{cfg['side_step']} "
              f"lep={cfg['long_entry_pct']:.0%} fund_w={cfg['funding_weight']:.0%} "
              f"mom={cfg['momentum_weeks']}w sl={cfg['stop_loss_pct']:.0%} "
              f"gate={cfg['fund_short_gate']})")
        all_res[name] = run_experiment(
            cfg, df, regime_df, bn_price_piv, bn_adtv_piv,
            bn_tokv_piv, bn_fund_raw, onboard_map, fund_4w, mom_13w)
        r = all_res[name]
        st = portfolio_stats(r["combined_net"])
        print(f"    → {r['n']} periods | combined_net={_fmt(st['ann_return'])} "
              f"| MaxDD={_fmt(st['max_dd'])} | bear_geo={_fmt(r['bear_geo'])}")

    print_results(all_res)
    print("\nDone.")


if __name__ == "__main__":
    main()
