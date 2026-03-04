"""
perpetual_ls_v7.py  (defaults updated to v8 parameters)
==================
Supply-Dilution L/S Strategy -- Version 7/8

v8 parameter changes vs v7 baseline:
  SUPPLY_WINDOW = 26 (was 13)   — slower fast signal, less noise
  BULL_BAND     = 1.05 (was 1.10) — tighter regime band → more Bull periods
  BEAR_BAND     = 0.95 (was 0.90) — tighter regime band → more Bear periods
  LONG_QUALITY_LOOKBACK = 12 (was 6) — longer quality veto lookback
  Combined net ann: +13.93% | Sharpe: +0.765 | MaxDD: -14.46%


Key improvements over v6 (fixes all diagnosed structural failures):

  [V7-1] Monthly rebalancing always.
          v6's bi-monthly Bull step collapsed observation count from ~25 to 19,
          making every statistic unreliable.  Back to monthly cadence throughout.

  [V7-2] Sideways regime -> hold cash (restored from v5).
          v6 reverted to v4's full L/S in Sideways; win rate was only 33.3%
          on an 85%-turnover strategy.  Cash is better (confirmed in v5).

  [V7-3] BTC beta hedge removed permanently.
          Cost -3.77% ann. in v6 (avg beta 0.453, estimated from <20 periods
          -> deeply unreliable).  Same failure mode as v4->v5 transition.

  [V7-4] Momentum veto on short selection.
          Tokens whose trailing 1-month return exceeds the 60th percentile of
          the investable universe are ineligible for shorting that period.
          Targets the 15.8% CB hit rate in v6: CB events came from strongly
          momentum-driven tokens that happened to have high supply inflation.

  [V7-5] Momentum veto applied within short candidate pool.
          The veto is computed relative to the tokens that already qualify for
          shorting (top SHORT_ENTRY_PCT by supply inflation).  Within that pool
          we exclude the top 50% by trailing 1m Binance return.  This always
          leaves roughly half the candidates intact and prevents shrinking
          baskets below MIN_BASKET_SIZE while still removing the most dangerous
          momentum-driven shorts.

  [V7-6] Symmetric L/S scaling in both Bull and Bear; Sideways = cash.
          Bull: (0.75L, 0.75S) — same as Bear. Prior v7 attempt used
          (1.0L, 0.20S) which made the strategy 83% net-long altcoins;
          in the 2024-25 BTC-dominant bull, low-inflation alts still
          declined, destroying the combined return even when the spread
          was positive (72% win rate in Bull). Symmetric scaling captures
          the spread directly without net directional exposure to alts.

  [V7-7] Signal unchanged from v4/v6: 50% rank(13w) + 50% rank(52w).
          Testing showed that adding a 4-week supply-change component
          corrupted long-basket selection: tokens with a temporarily quiet
          4-week supply but high 13w/52w inflation were incorrectly promoted.
          The 2-layer signal is retained; architectural improvements (V7-1 to
          V7-6) carry the bulk of the expected performance gain.

  [V7-8] Buffer band unchanged (12%/18% long, 82%/88% short).
          Widening the entry thresholds risked shrinking baskets below
          MIN_BASKET_SIZE in thinner markets.  Cost reduction comes from
          the momentum veto and sideways cash, not from band widening.

Everything else identical to v4/v6:
  - Real Binance USDT-M perp prices and 8h funding rates
  - inv-vol x sqrt(ADTV) position sizing, 20% per-token cap
  - Altcoin-season veto (zeros short leg when alts dominate)
  - Short-squeeze exclusion (tokens that rallied >40% in prior month)
  - Circuit breaker (caps short loss at 40% per period)
  - Lo (2002) HAC-corrected Sharpe

Data: run fetch_binance_data.py first.
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
MIN_VOLUME           = 5_000_000        # ADTV floor (daily USD proxy, both sides)
MIN_MKTCAP           = 50_000_000
MIN_SUPPLY_HISTORY   = 26
FFILL_LIMIT          = 1

# Signal (2-layer, unchanged from v4/v6)
SUPPLY_WINDOW        = 26               # v8: 26w fast window (was 13)
SUPPLY_WINDOW_SLOW   = 52
SIGNAL_SLOW_WEIGHT   = 0.50
SUPPLY_INF_WINS      = (0.02, 0.98)

# Portfolio  (buffer band unchanged from v4/v6)
LONG_ENTRY_PCT       = 0.12
LONG_EXIT_PCT        = 0.18
SHORT_ENTRY_PCT      = 0.88
SHORT_EXIT_PCT       = 0.82
MIN_BASKET_SIZE      = 6
ADTV_POS_CAP         = 0.20
WEIGHT_SCHEME        = "equal"   # "inv_vol" | "equal"
TOKEN_VOL_WINDOW     = 8

# Execution
TAKER_FEE            = 0.0004
SLIPPAGE_K           = 0.0005
MIN_TURNOVER         = 0.001
MAX_SLIPPAGE         = 0.02

# Regime
REGIME_MA_WINDOW     = 20
BULL_BAND            = 1.05             # v8: tighter band (was 1.10)
BEAR_BAND            = 0.95             # v8: tighter band (was 0.90)
HIGH_VOL_THRESHOLD   = 0.80
VOL_WINDOW           = 8

# [V7-6] Regime L/S scaling: symmetric in Bull & Bear, cash in Sideways.
# Bull uses same (0.75, 0.75) as Bear — capture the spread symmetrically.
# (1.0L, 0.20S) previously used made strategy 83% net-long altcoins, which
# destroyed returns in the 2024-25 BTC-dominant bull where alts declined.
REGIME_LS_SCALE = {
    ("Sideways", False): (0.00, 0.00),   # [V7-2] hold cash
    ("Sideways", True):  (0.00, 0.00),   # [V7-2] hold cash
    ("Bull",     False): (0.75, 0.75),   # [V7-6] symmetric L/S, same as Bear
    ("Bull",     True):  (0.50, 0.25),   # high-vol bull: scale back
    ("Bear",     False): (0.75, 0.75),   # unchanged from v4/v6
    ("Bear",     True):  (0.50, 0.25),   # unchanged from v4/v6
}

# Altseason veto (unchanged)
ALTSEASON_THRESHOLD  = 0.75
ALTSEASON_LOOKBACK   = 4

# [V7-4/V7-5] Momentum veto: within short candidate pool, veto top 50% by 1m return
MOMENTUM_VETO_PCT    = 0.50   # percentile threshold within short candidate pool

# [V7-9] Long quality veto: within long candidate pool, remove bottom LONG_QUALITY_VETO_PCT
# by BTC-relative 6m return. Dying tokens (NEO, THETA) consistently underperform BTC
# regardless of their genuinely low supply inflation. Applied to both entry and stay.
LONG_QUALITY_VETO_PCT      = 0.33   # veto bottom 33% by BTC-relative 6m return within long pool
LONG_QUALITY_LOOKBACK      = 12     # v8: 12m lookback (was 6)

# Short squeeze / CB (unchanged)
SHORT_SQUEEZE_PRIOR  = 0.40
SHORT_CB_LOSS        = 0.40

START_DATE   = pd.Timestamp("2022-01-01")
END_DATE     = None          # if set, only periods <= END_DATE are traded
PERMUTE_SEED = -1            # if >= 0, shuffles pct_rank across symbols each period
ZERO_FUNDING = False         # if True, zeros all funding rates (isolates signal alpha)
SAVE_BASKET_LOG = ""         # if non-empty, saves per-period CSV to this path
WINS_LOW    = 0.01
WINS_HIGH   = 0.99

# ===========================================================================
#  EXCLUSION LISTS  (unchanged from v4/v6)
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
#  STEP 1 -- Load CMC data
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
#  STEP 2 -- Load Binance data
# ===========================================================================

def load_binance(bn_dir: str) -> tuple:
    ohlcv   = pd.read_parquet(f"{bn_dir}/weekly_ohlcv.parquet")
    funding = pd.read_parquet(f"{bn_dir}/weekly_funding.parquet")
    meta    = pd.read_csv(f"{bn_dir}/symbol_meta.csv", parse_dates=["onboard_date"])

    ohlcv["cmc_date"]   = ohlcv["week_start"] + pd.Timedelta(days=6)
    funding["cmc_date"] = funding["week_start"] + pd.Timedelta(days=6)

    bn_price_piv = ohlcv.pivot_table(
        index="cmc_date", columns="symbol", values="close", aggfunc="last")
    bn_adtv_piv = ohlcv.pivot_table(
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
#  STEP 3 -- Regime
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
#  STEP 4 -- Feature engineering  [V7-7: adds 4w supply signal]
# ===========================================================================

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    grp = df.groupby("symbol", group_keys=False)

    # Use market_cap / price as the supply proxy.
    # The raw circulating_supply column from CMC is corrupted for ~64% of the
    # investable universe (median error up to 85x, peak errors up to 3M x for
    # tokens like ATOM, VET, COMP, XLM). The derived supply = market_cap / price
    # is consistent and subject only to ~1% price/mcap reporting-lag noise.
    df["supply_derived"] = df["market_cap"] / df["price"]

    df["supply_inf_13w"] = grp["supply_derived"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW))
    df["supply_inf_52w"] = grp["supply_derived"].transform(
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
                 onboard_map: dict) -> dict:

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

    price_piv_cmc = df.pivot_table(index="snapshot_date", columns="symbol",
                                   values="price", aggfunc="last")
    slip_piv = df.pivot_table(index="snapshot_date", columns="symbol",
                              values="slippage", aggfunc="last")

    # Altseason pre-computation (unchanged from v4/v6)
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

    # [V7-1] Always monthly rebalancing (no regime-aware step skipping)
    sorted_rebals = [d for d in all_rebal if d >= START_DATE] if START_DATE else all_rebal
    if END_DATE is not None:
        sorted_rebals = [d for d in sorted_rebals if d <= END_DATE]

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

    # Basket composition log and trade count accumulators
    basket_log           = []
    total_long_opens     = 0
    long_quality_veto_count = 0
    total_long_closes  = 0
    total_short_opens  = 0
    total_short_closes = 0

    for i, t0 in enumerate(sorted_rebals[:-1]):
        t1 = sorted_rebals[i + 1]

        if t0 not in bn_price_piv.index or t1 not in bn_price_piv.index:
            continue

        univ = inf_snap[inf_snap["snapshot_date"] == t0].copy()
        if len(univ) == 0:
            continue

        # .astype(bool) is required: pandas.apply() on an empty Series defaults to
        # float64 dtype, which pandas interprets as column selection rather than row
        # selection, producing a 0-column DataFrame.  Explicit bool cast prevents this.
        univ = univ[univ["symbol"].apply(
            lambda s: pd.notna(onboard_map.get(s)) and onboard_map.get(s) <= t0
        ).astype(bool)]

        if t0 in bn_adtv_piv.index:
            adtv_now = bn_adtv_piv.loc[t0]
            univ = univ[univ["symbol"].apply(
                lambda s: (pd.notna(adtv_now.get(s)) and
                           float(adtv_now.get(s, 0)) >= MIN_VOLUME * 7)
            ).astype(bool)]

        if len(univ) < MIN_BASKET_SIZE * 2:
            continue

        # Cross-sectional winsorisation
        for col in ["supply_inf_13w", "supply_inf_52w"]:
            col_vals = univ[col].dropna()
            if len(col_vals) > 4:
                lo_w, hi_w = col_vals.quantile(SUPPLY_INF_WINS)
                univ[col]  = univ[col].clip(lo_w, hi_w)

        # Composite signal rank (2-layer, same as v4/v6)
        univ["rank_13w"] = univ["supply_inf_13w"].rank(pct=True)
        univ["rank_52w"] = univ["supply_inf_52w"].rank(pct=True)
        univ["rank_52w"] = univ["rank_52w"].fillna(univ["rank_13w"])
        univ["pct_rank"] = ((1 - SIGNAL_SLOW_WEIGHT) * univ["rank_13w"]
                          + SIGNAL_SLOW_WEIGHT        * univ["rank_52w"])

        # Permutation test hook: shuffle pct_rank across symbols (destroys signal)
        if PERMUTE_SEED >= 0:
            rng = np.random.default_rng(PERMUTE_SEED + i)
            vals = univ["pct_rank"].values.copy()
            rng.shuffle(vals)
            univ["pct_rank"] = vals

        rank_map = univ.set_index("symbol")["pct_rank"].to_dict()
        all_syms = set(univ["symbol"])

        # Cross-sectional quantile thresholds on the COMPOSITE rank.
        # Because pct_rank is a weighted average of three correlated uniforms,
        # its distribution is compressed toward 0.5 (not uniform).  Using the
        # composite's own quantile ensures exactly LONG_ENTRY_PCT * N tokens
        # are eligible each period, regardless of signal correlation structure.
        long_thresh  = float(univ["pct_rank"].quantile(LONG_ENTRY_PCT))
        long_exit_t  = float(univ["pct_rank"].quantile(LONG_EXIT_PCT))
        short_thresh = float(univ["pct_rank"].quantile(SHORT_ENTRY_PCT))
        short_exit_t = float(univ["pct_rank"].quantile(SHORT_EXIT_PCT))

        # Squeeze exclusion (unchanged — uses Binance prices)
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

        # Build long and short baskets using data-driven quantile thresholds
        entry_long   = {s for s in all_syms if rank_map[s] <= long_thresh}
        stay_long    = {s for s in (prev_long_set & all_syms) if rank_map[s] <= long_exit_t}

        # [V7-9] Long quality veto: within (entry_long | stay_long), veto the bottom
        # LONG_QUALITY_VETO_PCT by BTC-relative 6m return. Dying tokens (NEO, THETA)
        # underperform BTC consistently in both Bull (don't rally) and Bear (fall harder).
        # Applied to BOTH entry and stay positions so zombies can't persist indefinitely.
        long_quality_vetoed = set()
        all_long_candidates = entry_long | stay_long
        if i >= LONG_QUALITY_LOOKBACK and len(all_long_candidates) > MIN_BASKET_SIZE:
            t_6m = sorted_rebals[i - LONG_QUALITY_LOOKBACK]
            if t_6m in bn_price_piv.index and t0 in bn_price_piv.index:
                p_6m_bn  = bn_price_piv.loc[t_6m]
                p_now_bn = bn_price_piv.loc[t0]
                btc_6m_ret = np.nan
                if ("BTC" in p_6m_bn and pd.notna(p_6m_bn["BTC"]) and
                        float(p_6m_bn["BTC"]) > 0 and "BTC" in p_now_bn and
                        pd.notna(p_now_bn["BTC"])):
                    btc_6m_ret = float(p_now_bn["BTC"]) / float(p_6m_bn["BTC"]) - 1
                long_btc_alpha = {}
                for s in all_long_candidates:
                    p0l = float(p_6m_bn[s])  if (s in p_6m_bn  and pd.notna(p_6m_bn[s]))  else np.nan
                    p1l = float(p_now_bn[s]) if (s in p_now_bn and pd.notna(p_now_bn[s])) else np.nan
                    if pd.notna(p0l) and p0l > 0 and pd.notna(p1l):
                        tok_ret = p1l / p0l - 1
                        long_btc_alpha[s] = (tok_ret - btc_6m_ret
                                             if pd.notna(btc_6m_ret) else tok_ret)
                if len(long_btc_alpha) >= MIN_BASKET_SIZE:
                    lo_thresh = np.percentile(list(long_btc_alpha.values()),
                                              LONG_QUALITY_VETO_PCT * 100)
                    candidates_vetoed = {s for s, r in long_btc_alpha.items() if r < lo_thresh}
                    remaining = all_long_candidates - candidates_vetoed
                    if len(remaining) >= MIN_BASKET_SIZE:
                        long_quality_vetoed = candidates_vetoed
                        long_quality_veto_count += len(long_quality_vetoed)

        entry_long  = entry_long  - long_quality_vetoed
        stay_long   = stay_long   - long_quality_vetoed
        basket_long = entry_long  | stay_long

        # Initial short candidate pool (supply signal + squeeze filter)
        entry_short_raw = {s for s in all_syms if rank_map[s] >= short_thresh} - squeezed
        stay_short_raw  = {s for s in (prev_short_set & all_syms)
                           if rank_map[s] >= short_exit_t} - squeezed

        # [V7-4/V7-5] Momentum veto applied WITHIN the short candidate pool.
        # Compute trailing 1m Binance return for each short candidate; exclude
        # the top MOMENTUM_VETO_PCT fraction by momentum — but only if doing so
        # still leaves >= MIN_BASKET_SIZE entry candidates.
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
                    # Only apply if enough candidates remain after veto
                    remaining = entry_short_raw - candidates_vetoed
                    if len(remaining) >= MIN_BASKET_SIZE:
                        momentum_vetoed = candidates_vetoed
                        momentum_veto_count += len(momentum_vetoed)

        entry_short  = entry_short_raw - momentum_vetoed
        stay_short   = stay_short_raw   # stay positions not vetoed by momentum
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

        # Altseason veto (zeros short leg)
        if altseason_map.get(t0, False):
            short_scale = 0.0
            altseason_count += 1

        # Turnover tracking (always, before the sideways cash branch)
        to_l = (1 - len(basket_long & prev_long_set) /
                max(len(basket_long | prev_long_set), 1)) if prev_long_set else 1.0
        to_s = (1 - len(basket_short & prev_short_set) /
                max(len(basket_short | prev_short_set), 1)) if prev_short_set else 1.0

        # Capture previous baskets BEFORE updating state (for opens/closes tracking)
        prev_long_set_before  = set(prev_long_set)
        prev_short_set_before = set(prev_short_set)

        prev_long_set  = basket_long
        prev_short_set = basket_short

        # Log basket composition and accumulate trade counts
        basket_log.append({
            "date":         t0,
            "regime":       regime,
            "long":         sorted(basket_long),
            "short":        sorted(basket_short),
            "long_opens":   sorted(basket_long  - prev_long_set_before),
            "long_closes":  sorted(prev_long_set_before  - basket_long),
            "short_opens":  sorted(basket_short - prev_short_set_before),
            "short_closes": sorted(prev_short_set_before - basket_short),
        })
        total_long_opens   += len(basket_long  - prev_long_set_before)
        total_long_closes  += len(prev_long_set_before  - basket_long)
        total_short_opens  += len(basket_short - prev_short_set_before)
        total_short_closes += len(prev_short_set_before - basket_short)

        # [V7-2] Sideways = hold cash (0% return, no costs)
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

        # Actual funding: sum all 8h rates in holding period (t0, t1]
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
            if WEIGHT_SCHEME == "equal":
                n = len(syms); w = {s: 1/n for s in syms}
            else:
                w = inv_vol_adtv_weights(syms, vol_m, adtv_m)
            ret    = sum(w[s] * float(fwd[s]) for s in syms)
            slip   = sum(w[s] * float(sl_row.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)
                         for s in syms)
            fund_s = 0.0 if ZERO_FUNDING else sum(
                w[s] * float(fund_row[s] if s in fund_row.index and
                             pd.notna(fund_row[s]) else 0.0) for s in syms)
            return float(ret), float(slip), float(fund_s)

        r_long_gross,  slip_long,  fund_long_basket  = basket_return(basket_long)
        r_short_gross, slip_short, fund_short_basket = basket_return(basket_short)

        if pd.isna(r_long_gross) or pd.isna(r_short_gross):
            continue

        # Circuit breaker (unchanged)
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

        # [V7-3] No BTC hedge -- combined_net is the final return
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
        momentum_veto_count      = momentum_veto_count,
        long_quality_veto_count  = long_quality_veto_count,
        turnover_long     = turnover_long_l,
        turnover_short    = turnover_short_l,
        basket_log        = basket_log,
        total_long_opens  = total_long_opens,
        total_long_closes = total_long_closes,
        total_short_opens  = total_short_opens,
        total_short_closes = total_short_closes,
    )


# ===========================================================================
#  STEP 6 -- Report
# ===========================================================================

def print_report(res: dict) -> None:
    print("\n" + "=" * 78)
    print("PERPETUAL L/S BACKTEST v7")
    print("  Signal : 50% rank(13w) + 50% rank(52w), winsorised 2-98pct")
    print("  Prices : Binance USDT-M weekly perp close")
    print("  Funding: Actual 8h Binance funding rates")
    print("  Rebal  : Monthly always (no regime-aware stepping)")
    print("  Regime : Sideways=cash | Bull=(0.75L, 0.75S) symmetric | Bear=(0.75L, 0.75S)")
    print("  Shorts : Momentum veto (top-50pct 1m return within candidate pool excluded)")
    print("  Hedge  : None (BTC beta hedge removed)")
    print("=" * 78)

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
    print(f"  Momentum veto (tot) : {res['momentum_veto_count']} token-periods removed (shorts)")
    print(f"  Long quality veto   : {res.get('long_quality_veto_count',0)} token-periods removed (longs)")
    if res["turnover_long"]:
        print(f"  Avg monthly turnover: Long {np.mean(res['turnover_long']):.1%}  "
              f"Short {np.mean(res['turnover_short']):.1%}")

    print(f"\n  {'Series':<34} {'Ann.Ret':>9} {'Vol':>9} "
          f"{'Sharpe':>7} {'Sharpe*':>8} {'Sortino':>8} {'MaxDD':>9}")
    print("  " + "-" * 78)
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

    # v4 vs v6 vs v7 comparison
    V4 = dict(combined_ann=-0.0511, combined_dd=-0.2289, sharpe=-0.222,
              win_rate=0.48,  mean_spread=0.0195,
              bear_geo=0.481, bull_geo=0.211, side_geo=-0.145)
    V6 = dict(combined_ann=+0.0010, combined_dd=-0.1922, sharpe=+0.003,
              win_rate=0.526, mean_spread=0.0334,
              bear_geo=0.5334, bull_geo=0.5191, side_geo=0.0134)

    st7 = portfolio_stats(res["combined_net"])
    v7_geo = {}
    for reg in ["Bull", "Bear", "Sideways"]:
        mask = regs == reg
        sub  = sp.iloc[list(np.where(mask)[0])]
        v7_geo[reg] = ((1 + sub.clip(lower=-0.99)).prod() ** (12/len(sub)) - 1
                       if len(sub) > 0 else np.nan)

    def _f(v, fmt="+.1%"):
        return f"{v:{fmt}}" if not np.isnan(v) else "  N/A"

    print(f"\n  --- v4 / v6 / v7 Comparison ---")
    print(f"  {'Metric':<28} {'v4 (base)':>10} {'v6':>10} {'v7':>10}")
    print(f"  {'-'*60}")
    print(f"  {'Combined net (ann.)':<28} {_f(V4['combined_ann']):>10} "
          f"{_f(V6['combined_ann']):>10} {_f(st7['ann_return']):>10}")
    print(f"  {'MaxDD':<28} {_f(V4['combined_dd']):>10} "
          f"{_f(V6['combined_dd']):>10} {_f(st7['max_dd']):>10}")
    print(f"  {'Sharpe':<28} {V4['sharpe']:>+10.3f} {V6['sharpe']:>+10.3f} "
          f"{_f(st7['sharpe'], '+.3f'):>10}")
    print(f"  {'Win rate (spread)':<28} {V4['win_rate']:>10.1%} {V6['win_rate']:>10.1%} "
          f"{(sp>0).mean():>10.1%}")
    print(f"  {'Mean period spread':<28} {V4['mean_spread']:>+10.2%} "
          f"{V6['mean_spread']:>+10.2%} {sp.mean():>+10.2%}")
    print(f"  {'Bear geo spread':<28} {_f(V4['bear_geo']):>10} "
          f"{_f(V6['bear_geo']):>10} {_f(v7_geo.get('Bear', float('nan'))):>10}")
    print(f"  {'Bull geo spread':<28} {_f(V4['bull_geo']):>10} "
          f"{_f(V6['bull_geo']):>10} {_f(v7_geo.get('Bull', float('nan'))):>10}")
    side_v7 = ("(skip)" if np.isnan(v7_geo.get("Sideways", float("nan"))) else
                _f(v7_geo.get("Sideways", float("nan"))))
    print(f"  {'Sideways geo spread':<28} {_f(V4['side_geo']):>10} "
          f"{_f(V6['side_geo']):>10} {side_v7:>10}")
    print("=" * 78)

    # --- Trade Counts ---
    tlo = res.get("total_long_opens",   0)
    tlc = res.get("total_long_closes",  0)
    tso = res.get("total_short_opens",  0)
    tsc = res.get("total_short_closes", 0)
    bl  = res.get("basket_log", [])
    n_bl = max(len(bl), 1)

    print(f"\n  --- Trade Count ---")
    print(f"  Long leg  : {tlo} opens, {tlc} closes ({tlo + tlc} total)")
    print(f"  Short leg : {tso} opens, {tsc} closes ({tso + tsc} total)")
    print(f"  All legs  : {tlo + tlc + tso + tsc} trades | "
          f"{(tlo + tlc + tso + tsc) / n_bl:.1f} avg per period")

    # --- Avg basket size by regime ---
    if bl:
        print(f"\n  --- Avg Basket Size by Regime ---")
        print(f"  {'Regime':<10} {'N':>4}  {'Avg Long':>9}  {'Avg Short':>10}")
        for reg in ["Bull", "Bear", "Sideways"]:
            sub = [e for e in bl if e["regime"] == reg]
            if not sub:
                continue
            al = np.mean([len(e["long"])  for e in sub])
            as_ = np.mean([len(e["short"]) for e in sub])
            print(f"  {reg:<10} {len(sub):>4}  {al:>9.1f}  {as_:>10.1f}")

    # --- Most frequent tokens ---
    if bl:
        from collections import Counter
        long_freq  = Counter(t for e in bl for t in e["long"])
        short_freq = Counter(t for e in bl for t in e["short"])
        top_long   = long_freq.most_common(10)
        top_short  = short_freq.most_common(10)
        total_periods = len(bl)
        print(f"\n  --- Most Frequent Long Basket Tokens (of {total_periods} periods) ---")
        for sym, cnt in top_long:
            print(f"    {sym:<12}  {cnt:>3} periods  ({cnt/total_periods:.0%})")
        print(f"\n  --- Most Frequent Short Basket Tokens (of {total_periods} periods) ---")
        for sym, cnt in top_short:
            print(f"    {sym:<12}  {cnt:>3} periods  ({cnt/total_periods:.0%})")

    # --- Per-period basket listing ---
    if bl:
        print(f"\n  --- Per-Period Baskets ---")
        print(f"  {'Date':<12} {'Rgm':<8} {'Long basket':<55} {'Short basket'}")
        print(f"  {'-'*130}")
        for e in bl:
            date_s  = e["date"].strftime("%Y-%m-%d")
            regime_s = e["regime"][:4]
            lo_s = ",".join(e["long"])
            sh_s = ",".join(e["short"])
            # Truncate to fit terminal width
            if len(lo_s) > 53:
                lo_s = lo_s[:50] + "..."
            if len(sh_s) > 53:
                sh_s = sh_s[:50] + "..."
            print(f"  {date_s:<12} {regime_s:<8} {lo_s:<55} {sh_s}")

    print("=" * 78)


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
        "Supply-Dilution L/S v7 | Monthly rebal | Sideways=cash | No BTC hedge\n"
        "Momentum veto on shorts | 2-layer signal (13w+52w) | Symmetric (0.75L, 0.75S)",
        fontsize=11, fontweight="bold")

    ax = axes[0]
    for series, color, lw, ls, label in [
        (res["long_gross"],  "steelblue",      1.5, "-",  "Long basket (gross)"),
        (res["short_gross"], "crimson",          1.5, "-",  "Short basket (gross)"),
        (res["long_net"],    "cornflowerblue",  1.5, "--", "Long leg (net)"),
        (res["combined_net"],"mediumseagreen",  2.5, "-",  "L/S Combined (net)"),
    ]:
        cum = (1 + series.dropna()).cumprod()
        ax.semilogy(cum.index, cum.values, color=color, lw=lw, ls=ls, label=label)
    ax.axhline(1, color="black", lw=0.6, ls="--")
    shade_regimes(ax)
    ax.set_ylabel("Cumulative Return (log)")
    ax.set_title("Cumulative Wealth — v7")
    ax.legend(fontsize=9); ax.grid(True, which="both", alpha=0.2)

    ax2 = axes[1]
    cum_c = (1 + res["combined_net"].dropna()).cumprod()
    cum_l = (1 + res["long_net"].dropna()).cumprod()
    ax2.plot(cum_c.index, cum_c.values, "mediumseagreen", lw=2.5, label="Combined net (v7)")
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
    out1 = OUTPUT_DIR + "perp_ls_v7_cumulative.png"
    fig.savefig(out1, dpi=150); plt.close(fig)
    print(f"[Plot] {out1}")

    # Figure 2: drawdown + per-period spread
    fig2, axes2 = plt.subplots(2, 1, figsize=(12, 8))

    ax4 = axes2[0]
    for j, (dt, reg) in enumerate(zip(res["dates"], res["regime"])):
        ax4.bar(dt, sp.values[j], color=rc.get(reg, "gray"), width=20, alpha=0.7)
    ax4.axhline(0, color="black", lw=0.8)
    ax4.legend(handles=[Patch(color=c, alpha=0.7, label=l) for l, c in rc.items()], fontsize=9)
    ax4.set_title("Per-Period Gross Spread by Regime (v7)")
    ax4.set_ylabel("Spread Return"); ax4.grid(True, alpha=0.2)

    ax5 = axes2[1]
    cum7 = (1 + res["combined_net"].clip(lower=-0.99)).cumprod()
    dd7  = (cum7 - cum7.cummax()) / cum7.cummax()
    ax5.fill_between(dd7.index, dd7.values, 0, color="mediumseagreen", alpha=0.55,
                     label="v7 combined net")
    ax5.set_ylabel("Drawdown"); ax5.set_xlabel("Date")
    ax5.set_title("Drawdown: v7 Combined Net")
    ax5.legend(fontsize=9); ax5.grid(True, alpha=0.2)
    fig2.tight_layout()
    out2 = OUTPUT_DIR + "perp_ls_v7_regime_dd.png"
    fig2.savefig(out2, dpi=150); plt.close(fig2)
    print(f"[Plot] {out2}")

    # Figure 3: v6 vs v7 scorecard
    V6 = dict(
        combined_ann=+0.0010, combined_dd=-0.1922, sharpe=+0.003,
        win_rate=0.526, mean_spread=0.0334,
        bull_geo=0.5191, bear_geo=0.5334, side_geo=0.0134,
    )
    st7 = portfolio_stats(res["combined_net"])
    v7_geo = {}
    for regime in ["Bull", "Bear", "Sideways"]:
        mask = regs == regime
        sub  = sp.iloc[list(np.where(mask)[0])]
        v7_geo[regime] = ((1 + sub.clip(lower=-0.99)).prod() ** (12/len(sub)) - 1
                          if len(sub) > 0 else np.nan)

    regimes_list = ["Bull", "Bear", "Sideways"]
    fig3, axes3 = plt.subplots(2, 2, figsize=(14, 10))
    fig3.suptitle("v6 (regime-aware freq) vs v7 (structural fixes) — Post-2022",
                  fontsize=13, fontweight="bold")
    x = np.arange(len(regimes_list)); w = 0.36

    ax6 = axes3[0, 0]
    v6_geos = [V6["bull_geo"], V6["bear_geo"], V6["side_geo"]]
    v7_geos = [v7_geo.get(r, 0) for r in regimes_list]
    v7_geos_bar = [v if not np.isnan(v) else 0 for v in v7_geos]
    ax6.bar(x - w/2, v6_geos,     w, label="v6", color="silver",        edgecolor="black")
    ax6.bar(x + w/2, v7_geos_bar, w, label="v7", color="mediumseagreen", edgecolor="black")
    ax6.axhline(0, color="black", lw=0.8)
    ax6.set_xticks(x); ax6.set_xticklabels(regimes_list)
    ax6.set_title("Ann. Geometric Spread by Regime"); ax6.set_ylabel("Geo. Spread")
    ax6.legend(fontsize=9); ax6.grid(True, alpha=0.2)

    ax7 = axes3[0, 1]
    cum_v7 = (1 + res["combined_net"].dropna()).cumprod()
    ax7.plot(cum_v7.index, cum_v7.values, "mediumseagreen", lw=2.5, label="v7 combined net")
    ax7.axhline(1.0, color="black", lw=1.0, ls="--", label="Breakeven")
    ax7.set_title("v7 Combined NAV"); ax7.set_ylabel("Cumulative Return")
    ax7.legend(fontsize=9); ax7.grid(True, alpha=0.2)

    ax8 = axes3[1, 0]
    dd = (cum_v7 - cum_v7.cummax()) / cum_v7.cummax()
    ax8.fill_between(dd.index, dd.values, 0, color="mediumseagreen", alpha=0.55, label="v7")
    ax8.set_ylabel("Drawdown"); ax8.set_xlabel("Date")
    ax8.set_title("Drawdown: v7 Combined Net")
    ax8.legend(fontsize=9); ax8.grid(True, alpha=0.2)

    ax9 = axes3[1, 1]
    ax9.axis("off")

    def _f(v, fmt="+.1%"):
        return f"{v:{fmt}}" if not np.isnan(v) else "N/A"

    rows = [
        ["Metric",              "v6",                    "v7"],
        ["Combined net (ann.)", _f(V6["combined_ann"]),  _f(st7["ann_return"])],
        ["MaxDD",               _f(V6["combined_dd"]),   _f(st7["max_dd"])],
        ["Sharpe",              f"{V6['sharpe']:+.3f}",  _f(st7["sharpe"], "+.3f")],
        ["Win rate",            f"{V6['win_rate']:.1%}", f"{(sp>0).mean():.1%}"],
        ["Mean spread",         _f(V6["mean_spread"],"+.2%"), _f(sp.mean(),"+.2%")],
        ["Bear geo spread",     _f(V6["bear_geo"]),      _f(v7_geo.get("Bear", float("nan")))],
        ["Bull geo spread",     _f(V6["bull_geo"]),      _f(v7_geo.get("Bull", float("nan")))],
        ["Sideways",            _f(V6["side_geo"]),      "skipped"],
    ]
    tbl = ax9.table(cellText=rows[1:], colLabels=rows[0],
                    loc="center", cellLoc="center")
    tbl.auto_set_font_size(False); tbl.set_fontsize(10)
    tbl.scale(1.2, 1.7)
    for j in range(3):
        tbl[0, j].set_facecolor("#2c3e50")
        tbl[0, j].set_text_props(color="white", fontweight="bold")
    ax9.set_title("v6 vs v7 Scorecard", fontweight="bold", pad=14)

    fig3.tight_layout()
    out3 = OUTPUT_DIR + "perp_ls_v7_vs_v6.png"
    fig3.savefig(out3, dpi=150); plt.close(fig3)
    print(f"[Plot] {out3}")


# ===========================================================================
#  MAIN
# ===========================================================================

def main():
    print("=" * 78)
    print("Supply-Dilution L/S Strategy -- Version 7")
    print("Monthly rebal | Sideways=cash | No BTC hedge | Momentum veto on shorts")
    print("2-layer signal (13w+52w) | Symmetric (0.75L, 0.75S) | 2x ADTV floor for shorts")
    print("=" * 78)

    df = load_cmc(INPUT_FILE)
    (bn_price_piv, bn_adtv_piv, bn_tokv_piv,
     bn_fund_raw, onboard_map) = load_binance(BN_DIR)
    regime_df = build_regime(df)
    df        = engineer_features(df)

    results = run_backtest(df, regime_df,
                           bn_price_piv, bn_adtv_piv, bn_tokv_piv,
                           bn_fund_raw, onboard_map)

    if not results["dates"]:
        print("[ERROR] No rebalancing periods survived all filters.")
        return

    print_report(results)
    plot_results(results)

    if SAVE_BASKET_LOG:
        bl  = results["basket_log"]
        idx = results["dates"]
        rows = []
        for i, e in enumerate(bl):
            rows.append({
                "date":         e["date"],
                "regime":       e["regime"],
                "long_basket":  "|".join(sorted(e["long"])),
                "short_basket": "|".join(sorted(e["short"])),
                "long_gross":   results["long_gross"].iloc[i]  if i < len(results["long_gross"])  else float("nan"),
                "short_gross":  results["short_gross"].iloc[i] if i < len(results["short_gross"]) else float("nan"),
                "combined_net": results["combined_net"].iloc[i] if i < len(results["combined_net"]) else float("nan"),
                "fund_long":    results["fund_actual_long"][i]  if i < len(results["fund_actual_long"])  else float("nan"),
                "fund_short":   results["fund_actual_short"][i] if i < len(results["fund_actual_short"]) else float("nan"),
            })
        pd.DataFrame(rows).to_csv(SAVE_BASKET_LOG, index=False)
        print(f"[Log] Basket log saved: {SAVE_BASKET_LOG}")

    print("\nDone.")


if __name__ == "__main__":
    main()
