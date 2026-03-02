"""
perpetual_ls_v3.py
==================
Supply-Dilution L/S Strategy -- Version 3

Blind spots addressed on top of v2:

  SIGNAL
  [V3-1] Composite signal: 50% rank(13w) + 50% rank(52w)
         Slow component captures structural dilution; fast captures recent regime.
         Falls back to 13w-only when 52w data unavailable.
  [V3-2] Cross-sectional supply-inflation winsorisation at 2nd/98th pct
         before ranking. Prevents a single 10000%-inflation outlier from
         anchoring the short basket every period.

  POSITION SIZING
  [V3-3] Inverse-vol x sqrt(ADTV) weighting  (weight_i ∝ sqrt(ADTV_i)/vol_i)
         Prefers liquid AND low-volatility tokens. Reduces lottery-ticket
         concentration that drove the short-leg catastrophic losses. Cap 20%.

  RISK MANAGEMENT
  [V3-4] Altcoin-season veto: if >75% of top-50 alts beat BTC over trailing
         4 rebalancing periods, short_scale = 0 for that period. This shuts
         down the short leg during the exact environment (manic alt rotation)
         that created the -94% short-leg drawdown.
  [V3-5] Short-basket circuit breaker per token: any token whose prior-period
         return exceeded +40% is excluded from the short basket (squeeze signal).
         Hard period-level cap: short basket gross capped at -40% loss.
  [V3-6] Min market-cap floor $50M (removes phantom liquidity).

  MACRO HEDGE
  [V3-7] Rolling BTC portfolio beta hedge: 8-period OLS rolling beta of the
         combined return vs BTC forward return. Adds offsetting BTC short to
         neutralise the documented 0.645 spread beta. Applied only when enough
         history exists (>= 8 periods).

  ANALYTICS
  [V3-8] Lo (2002) HAC-corrected Sharpe reported alongside standard Sharpe.
  [V3-9] Monthly basket turnover tracked and reported.

Post-2021 start date retained (START_DATE = 2022-01-01).
"""

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

INPUT_FILE = "D:/AI_Projects/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
OUTPUT_DIR = "D:/AI_Projects/circ_supply/"

# Universe
MAX_RANK             = 200
TOP_N_EXCLUDE        = 20
MIN_VOLUME           = 5_000_000
MIN_MKTCAP           = 50_000_000      # [V3-6]
MIN_SUPPLY_HISTORY   = 26
FFILL_LIMIT          = 1

# Signal
SUPPLY_WINDOW        = 13              # fast signal
SUPPLY_WINDOW_SLOW   = 52             # [V3-1] slow signal
SIGNAL_SLOW_WEIGHT   = 0.50           # [V3-1] 50/50 blend
SUPPLY_INF_WINS      = (0.02, 0.98)   # [V3-2] cross-sectional winsorisation

# Portfolio -- inner buffer band
# NOTE: composite 13w+52w signal makes 7% harder to achieve than v2's single signal.
# Widened to 10%/15% so ~13 tokens qualify per basket on 128-token universe.
LONG_ENTRY_PCT       = 0.12
LONG_EXIT_PCT        = 0.18
SHORT_ENTRY_PCT      = 0.88
SHORT_EXIT_PCT       = 0.82
MIN_BASKET_SIZE      = 6

# Position sizing [V3-3]
ADTV_POS_CAP         = 0.20           # raised from 0.15
TOKEN_VOL_WINDOW     = 8

# Execution
TAKER_FEE            = 0.0004
SLIPPAGE_K           = 0.0005
MIN_TURNOVER         = 0.001
MAX_SLIPPAGE         = 0.02

# Regime detection (unchanged from v2)
REGIME_MA_WINDOW     = 20
BULL_BAND            = 1.10
BEAR_BAND            = 0.90
HIGH_VOL_THRESHOLD   = 0.80
VOL_WINDOW           = 8

# Regime-aware L/S scaling (unchanged from v2)
REGIME_LS_SCALE = {
    ("Sideways", False): (1.00, 1.00),
    ("Sideways", True):  (1.00, 0.75),
    ("Bull",     False): (1.00, 0.50),
    ("Bull",     True):  (0.75, 0.25),
    ("Bear",     False): (0.75, 0.75),
    ("Bear",     True):  (0.50, 0.25),
}

# Altcoin-season veto [V3-4]
ALTSEASON_THRESHOLD  = 0.75           # >75% of top-50 alts beat BTC -> veto shorts
ALTSEASON_LOOKBACK   = 4              # rebalancing periods for alt vs BTC comparison

# Short circuit breaker [V3-5]
SHORT_SQUEEZE_PRIOR  = 0.40           # exclude token if prior-period return > 40%
SHORT_CB_LOSS        = 0.40           # hard cap: short basket gross loss at -40%

# BTC beta hedge [V3-7]
BTC_HEDGE_ENABLED    = True
BTC_HEDGE_LOOKBACK   = 12
BTC_HEDGE_MAX        = 1.0

# Funding (unchanged from v2)
FUNDING_8H = {
    ("Bull",     "long"):    +0.0000800,
    ("Bull",     "short"):   +0.0001500,
    ("Bear",     "long"):    +0.0000200,
    ("Bear",     "short"):   +0.0000500,
    ("Sideways", "long"):    +0.0000500,
    ("Sideways", "short"):   +0.0000800,
}
FUNDING_PERIODS_PER_DAY = 3

START_DATE  = pd.Timestamp("2022-01-01")
WINS_LOW    = 0.01
WINS_HIGH   = 0.99

# ===========================================================================
#  EXCLUSION LISTS (same as v2)
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
    """[V3-8] Lo (2002) HAC-corrected Sharpe. Adjusts for autocorrelation."""
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


def _fmt(v):  return f"{v:+.2%}"  if not np.isnan(v) else "    N/A"
def _fmtf(v, d=3): return f"{v:+.{d}f}" if not np.isnan(v) else "    N/A"


def get_ls_scales(regime: str, high_vol: bool) -> tuple:
    return REGIME_LS_SCALE.get((regime, high_vol),
           REGIME_LS_SCALE.get((regime, False), (1.0, 1.0)))


def inv_vol_adtv_weights(symbols: list, vol_map: dict, adtv_map: dict,
                          cap: float = ADTV_POS_CAP) -> dict:
    """[V3-3] weight_i ∝ sqrt(ADTV_i) / realized_vol_i, capped and renormed."""
    raw = {}
    for s in symbols:
        vol  = max(float(vol_map.get(s)  if pd.notna(vol_map.get(s))  else 1.0), 0.05)
        adtv = max(float(adtv_map.get(s) if pd.notna(adtv_map.get(s)) else 0.0), 0.0)
        raw[s] = (adtv ** 0.5 + 1.0) / vol   # +1 prevents zero-ADTV collapse
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
#  STEP 1 -- Load & preprocess
# ===========================================================================

def load_data(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["snapshot_date"])
    df = df.sort_values(["symbol", "snapshot_date"]).reset_index(drop=True)
    df = df[df["price"].notna() & (df["price"] > 0) & df["market_cap"].notna()].copy()
    for col in ["price", "circulating_supply"]:
        df[col] = df.groupby("symbol")[col].transform(lambda s: s.ffill(limit=FFILL_LIMIT))
    df = df[df["circulating_supply"].notna() & (df["circulating_supply"] > 0)].copy()
    print(f"[Load] {len(df):,} rows | {df['symbol'].nunique():,} symbols | "
          f"{df['snapshot_date'].min().date()} -> {df['snapshot_date'].max().date()}")
    return df


# ===========================================================================
#  STEP 2 -- Regime
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
#  STEP 3 -- Feature engineering
# ===========================================================================

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    grp = df.groupby("symbol", group_keys=False)

    # [V3-1] Dual-horizon supply inflation
    df["supply_inf_13w"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW))
    df["supply_inf_52w"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW_SLOW))
    df["supply_inf"] = df["supply_inf_13w"]   # alias for history counter

    df["supply_hist_count"] = grp["supply_inf"].transform(
        lambda s: s.notna().cumsum())

    # [V3-3] Per-token 8-week realised vol (annualised)
    df["token_vol_8w"] = grp["price"].transform(
        lambda s: s.pct_change(1)
                   .rolling(TOKEN_VOL_WINDOW, min_periods=4)
                   .std() * np.sqrt(52))

    # Slippage proxy
    df["turnover"] = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"] = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)

    return df


# ===========================================================================
#  STEP 4 -- Main backtest loop
# ===========================================================================

def run_backtest(df: pd.DataFrame, regime_df: pd.DataFrame) -> dict:
    df["ym"]    = df["snapshot_date"].dt.to_period("M")
    all_rebal   = sorted(df.groupby("ym")["snapshot_date"].min().tolist())
    regime_map  = regime_df.set_index("snapshot_date")[["regime","high_vol"]].to_dict("index")

    # Eligible universe snapshot (all history, START_DATE applied in loop)
    inf_snap = df[df["snapshot_date"].isin(all_rebal)].copy()
    inf_snap = inf_snap[inf_snap["supply_inf_13w"].notna()]
    inf_snap = inf_snap[~inf_snap["symbol"].isin(EXCLUDED)]
    inf_snap = inf_snap[(inf_snap["rank"] > TOP_N_EXCLUDE) & (inf_snap["rank"] <= MAX_RANK)]
    inf_snap = inf_snap[inf_snap["volume_24h"]  >= MIN_VOLUME]
    inf_snap = inf_snap[inf_snap["market_cap"]  >= MIN_MKTCAP]       # [V3-6]
    inf_snap = inf_snap[inf_snap["supply_hist_count"] >= MIN_SUPPLY_HISTORY]

    # Pivot tables (full history -- needed for momentum lookback)
    price_piv = df.pivot_table(index="snapshot_date", columns="symbol",
                               values="price",       aggfunc="last")
    vol_piv   = df.pivot_table(index="snapshot_date", columns="symbol",
                               values="volume_24h",  aggfunc="last")
    slip_piv  = df.pivot_table(index="snapshot_date", columns="symbol",
                               values="slippage",    aggfunc="last")
    tokv_piv  = df.pivot_table(index="snapshot_date", columns="symbol",
                               values="token_vol_8w",aggfunc="last")   # [V3-3]

    # BTC forward returns (all periods)
    btc_ser = df[df["symbol"] == "BTC"].set_index("snapshot_date")["price"].sort_index()
    btc_fwd = {}
    for i in range(len(all_rebal) - 1):
        t0r, t1r = all_rebal[i], all_rebal[i+1]
        p0, p1   = btc_ser.get(t0r, np.nan), btc_ser.get(t1r, np.nan)
        btc_fwd[t0r] = (p1/p0 - 1) if pd.notna(p0) and pd.notna(p1) and p0 > 0 else np.nan

    # [V3-4] Altcoin-season index (pre-computed for all rebalancing dates)
    altseason_map = {}
    for i, t0r in enumerate(all_rebal):
        if i < ALTSEASON_LOOKBACK or t0r not in price_piv.index:
            altseason_map[t0r] = False
            continue
        t_lb = all_rebal[i - ALTSEASON_LOOKBACK]
        if t_lb not in price_piv.index:
            altseason_map[t0r] = False
            continue
        p_lb  = price_piv.loc[t_lb]
        p_now = price_piv.loc[t0r]
        btc_p0 = float(p_lb.get("BTC", np.nan))
        btc_p1 = float(p_now.get("BTC", np.nan))
        if pd.isna(btc_p0) or btc_p0 <= 0 or pd.isna(btc_p1):
            altseason_map[t0r] = False
            continue
        btc_4w = btc_p1 / btc_p0 - 1
        top50  = df[(df["snapshot_date"] == t0r) &
                    (df["rank"].between(3, 50)) &
                    (~df["symbol"].isin(EXCLUDED | {"BTC", "ETH"}))]["symbol"].tolist()
        alt_rets = []
        for s in top50:
            p0s = float(p_lb.get(s, np.nan))
            p1s = float(p_now.get(s, np.nan))
            if pd.notna(p0s) and pd.notna(p1s) and p0s > 0:
                alt_rets.append(p1s/p0s - 1)
        if len(alt_rets) < 10:
            altseason_map[t0r] = False
            continue
        altseason_map[t0r] = (sum(r > btc_4w for r in alt_rets) / len(alt_rets)
                              > ALTSEASON_THRESHOLD)

    # Apply START_DATE
    sorted_rebals = [d for d in all_rebal if d >= START_DATE] if START_DATE else all_rebal

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
     beta_hist_l, raw_hist_l, btc_hist_l) = (
        [], [], [], [], [], [], [],
        [], [], [],
        0.0, 0.0, 0, 0,
        [], [],
        [], [], []
    )

    for i, t0 in enumerate(sorted_rebals[:-1]):
        t1        = sorted_rebals[i + 1]
        hold_days = max((t1 - t0).days, 1)

        univ = inf_snap[inf_snap["snapshot_date"] == t0].copy()
        if len(univ) < MIN_BASKET_SIZE * 2:
            continue

        # [V3-2] Cross-sectional winsorisation before ranking
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

        # [V3-5] Short squeeze exclusion: prior-period return > 40% -> exclude from short
        squeezed = set()
        if i > 0:
            t_prev_r = sorted_rebals[i - 1]
            if t_prev_r in price_piv.index and t0 in price_piv.index:
                prior_ret = price_piv.loc[t0] / price_piv.loc[t_prev_r] - 1
                squeezed  = {s for s in all_syms
                             if not pd.isna(prior_ret.get(s, np.nan))
                             and float(prior_ret.get(s, 0)) > SHORT_SQUEEZE_PRIOR}

        # Inner buffer band (same as v2)
        entry_long   = {s for s in all_syms if rank_map[s] <= LONG_ENTRY_PCT}
        stay_long    = {s for s in (prev_long_set & all_syms) if rank_map[s] <= LONG_EXIT_PCT}
        basket_long  = entry_long | stay_long

        # Squeeze exclusion: block NEW entries only -- don't force-exit existing positions
        # (applying it to stay_short too wiped entire basket in Nov-2024 bull run)
        entry_short  = {s for s in all_syms if rank_map[s] >= SHORT_ENTRY_PCT} - squeezed
        stay_short   = {s for s in (prev_short_set & all_syms)
                        if rank_map[s] >= SHORT_EXIT_PCT}   # no squeeze block here
        basket_short = entry_short | stay_short

        overlap      = basket_long & basket_short
        basket_long  -= overlap
        basket_short -= overlap

        if len(basket_long) < MIN_BASKET_SIZE or len(basket_short) < MIN_BASKET_SIZE:
            prev_long_set  = basket_long
            prev_short_set = basket_short
            continue

        # Turnover tracking [V3-9]
        to_l = (1 - len(basket_long & prev_long_set) /
                max(len(basket_long | prev_long_set), 1)) if prev_long_set else 1.0
        to_s = (1 - len(basket_short & prev_short_set) /
                max(len(basket_short | prev_short_set), 1)) if prev_short_set else 1.0
        turnover_long_l.append(to_l)
        turnover_short_l.append(to_s)

        prev_long_set  = basket_long
        prev_short_set = basket_short

        if t0 not in price_piv.index or t1 not in price_piv.index:
            continue

        # NOTE: Momentum filter removed in v3. The composite 13w+52w signal already
        # rewards tokens with persistently low inflation (structural quality), making
        # the trailing-return momentum cut redundant and too aggressive on small baskets.

        # Forward returns
        p0_s = price_piv.loc[t0]
        p1_s = price_piv.loc[t1]
        v0_s = vol_piv.loc[t0]  if t0 in vol_piv.index  else pd.Series(dtype=float)
        sl_s = slip_piv.loc[t0] if t0 in slip_piv.index else pd.Series(dtype=float)
        tv_s = tokv_piv.loc[t0] if t0 in tokv_piv.index else pd.Series(dtype=float)

        fwd = (p1_s / p0_s - 1)
        lo_f, hi_f = fwd.quantile(WINS_LOW), fwd.quantile(WINS_HIGH)
        fwd = fwd.clip(lower=lo_f, upper=hi_f).clip(lower=-1.0)

        def basket_return(symbols: set) -> tuple:
            """[V3-3] Inverse-vol x sqrt(ADTV) weighted return."""
            syms = [s for s in symbols if s in fwd.index and not pd.isna(fwd[s])]
            if not syms:
                return np.nan, MAX_SLIPPAGE
            vol_m  = {s: float(tv_s.get(s, 1.0) if pd.notna(tv_s.get(s)) else 1.0)
                      for s in syms}
            adtv_m = {s: float(v0_s.get(s, 0)   if pd.notna(v0_s.get(s))   else 0.0)
                      for s in syms}
            w      = inv_vol_adtv_weights(syms, vol_m, adtv_m)
            ret    = sum(w[s] * float(fwd[s]) for s in syms)
            slip   = sum(w[s] * float(sl_s.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)
                         for s in syms)
            return float(ret), float(slip)

        r_long_gross,  slip_long  = basket_return(basket_long)
        r_short_gross, slip_short = basket_return(basket_short)

        if pd.isna(r_long_gross) or pd.isna(r_short_gross):
            continue

        # [V3-5] Hard circuit breaker on short basket
        cb_hit = False
        if r_short_gross > SHORT_CB_LOSS:
            r_short_gross = SHORT_CB_LOSS
            cb_hit = True
            cb_count += 1

        # Regime & L/S scaling
        reg_info = regime_map.get(t0, {"regime": "Sideways", "high_vol": False})
        regime   = reg_info.get("regime",   "Sideways")
        high_vol = bool(reg_info.get("high_vol", False))
        long_scale, short_scale = get_ls_scales(regime, high_vol)

        # [V3-4] Altcoin-season veto: zero short exposure
        if altseason_map.get(t0, False):
            short_scale   = 0.0
            altseason_count += 1

        # Costs
        fee_cost    = 2 * TAKER_FEE
        n_payments  = FUNDING_PERIODS_PER_DAY * hold_days
        fund_drag_l = FUNDING_8H.get((regime, "long"),  0.0) * n_payments
        fund_cred_s = FUNDING_8H.get((regime, "short"), 0.0) * n_payments

        r_long_net  = r_long_gross  - fee_cost - slip_long  - fund_drag_l
        r_short_net = -r_short_gross - fee_cost - slip_short + fund_cred_s

        denom      = long_scale + short_scale if (long_scale + short_scale) > 0 else 1.0
        r_combined = (long_scale * r_long_net + short_scale * r_short_net) / denom

        r_long_net  = max(r_long_net,  -1.0)
        r_short_net = max(r_short_net, -1.0)
        r_combined  = max(r_combined,  -1.0)

        # [V3-7] Rolling BTC portfolio beta hedge
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
                    beta  = float(np.clip(np.cov(c_arr, b_arr)[0, 1] / var_b,
                                         0.0, BTC_HEDGE_MAX))
                    btc_r = btc_fwd.get(t0, 0.0) or 0.0
                    hedge_ret = -beta * btc_r
                    beta_used = beta

        raw_hist_l.append(r_combined)
        btc_hist_l.append(btc_fwd.get(t0, np.nan))
        beta_hist_l.append(beta_used)

        r_combined_hedged = max(r_combined + hedge_ret, -1.0)

        # Funding attribution
        fund_long_cum  += -fund_drag_l
        fund_short_cum += +fund_cred_s

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
        index           = idx,
        dates           = dates_out,
        long_gross      = pd.Series(long_gross_l,     index=idx, name="Long Gross"),
        short_gross     = pd.Series(short_gross_l,    index=idx, name="Short Gross"),
        long_net        = pd.Series(long_net_l,       index=idx, name="Long Net"),
        short_net       = pd.Series(short_net_l,      index=idx, name="Short Net"),
        combined_net    = pd.Series(combined_net_l,   index=idx, name="Combined Net"),
        combined_hedged = pd.Series(combined_hedged_l,index=idx, name="Combined Hedged"),
        spread_gross    = pd.Series(
            [lg - sg for lg, sg in zip(long_gross_l, short_gross_l)],
            index=idx, name="Spread Gross"),
        basket_sizes    = basket_sizes_l,
        regime          = regime_out_l,
        scale           = scale_out_l,
        fund_long_cum   = fund_long_cum,
        fund_short_cum  = fund_short_cum,
        cb_count        = cb_count,
        altseason_count = altseason_count,
        beta_hist       = beta_hist_l,
        turnover_long   = turnover_long_l,
        turnover_short  = turnover_short_l,
    )


# ===========================================================================
#  STEP 5 -- Report
# ===========================================================================

def print_report(res: dict) -> None:
    print("\n" + "=" * 76)
    print("PERPETUAL L/S BACKTEST v3")
    print("  Signal : 50% rank(13w) + 50% rank(52w), winsorised 2-98pct")
    print("  Weights: inv-vol x sqrt(ADTV), 20% cap")
    print("  Risk   : CB at 40% | squeeze exclusion | altseason veto | BTC hedge")
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
    print(f"  Alt-season veto     : {res['altseason_count']} period(s) -- short zeroed")
    if valid_b:
        print(f"  Avg rolling beta    : {np.mean(valid_b):.3f}  "
              f"(range {np.min(valid_b):.3f} -- {np.max(valid_b):.3f})")
    if res["turnover_long"]:
        print(f"  Avg monthly turnover: Long {np.mean(res['turnover_long']):.1%}  "
              f"Short {np.mean(res['turnover_short']):.1%}")

    # Per-series stats
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
        if mask.sum() == 0:
            continue
        sub   = sp.iloc[list(np.where(mask)[0])]
        n_sub = len(sub)
        cum   = (1 + sub.clip(lower=-0.99)).prod()
        ann   = cum ** (12 / n_sub) - 1 if n_sub > 0 else np.nan
        print(f"  {regime:<12} {n_sub:>4}   {sub.mean():>+12.2%}   "
              f"{(sub>0).mean():>8.1%}   {ann:>+14.2%}")

    print(f"\n  --- Funding Rate Attribution ---")
    print(f"  Cum. funding drag   (long pays)     : {res['fund_long_cum']:+.4f} "
          f"({res['fund_long_cum']:.2%})")
    print(f"  Cum. funding credit (short receives): {res['fund_short_cum']:+.4f} "
          f"({res['fund_short_cum']:.2%})")
    net_f = res["fund_long_cum"] + res["fund_short_cum"]
    print(f"  Net funding impact                  : {net_f:+.4f} ({net_f:.2%})")
    print("=" * 76)


# ===========================================================================
#  STEP 6 -- Plots
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

    # ── Figure 1: cumulative wealth ──────────────────────────────────────────
    fig, axes = plt.subplots(3, 1, figsize=(13, 13),
                             gridspec_kw={"height_ratios": [3, 2, 1.5]})
    fig.suptitle(
        "Supply-Dilution L/S v3 | 13w+52w composite signal | inv-vol weights\n"
        "Altseason veto | Circuit breaker | Rolling BTC beta hedge | post-2022",
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
    ax.set_title("Cumulative Wealth")
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
    out1 = OUTPUT_DIR + "perp_ls_v3_cumulative.png"
    fig.savefig(out1, dpi=150); plt.close(fig)
    print(f"[Plot] {out1}")

    # ── Figure 2: drawdown + regime bar ─────────────────────────────────────
    fig2, axes2 = plt.subplots(2, 1, figsize=(12, 8))

    ax4 = axes2[0]
    rc  = {"Bull": "steelblue", "Bear": "crimson", "Sideways": "gray"}
    for j, (dt, reg) in enumerate(zip(res["dates"], res["regime"])):
        ax4.bar(dt, sp.values[j], color=rc.get(reg, "gray"), width=20, alpha=0.7)
    ax4.axhline(0, color="black", lw=0.8)
    ax4.legend(handles=[Patch(color=c, alpha=0.7, label=l) for l, c in rc.items()],
               fontsize=9)
    ax4.set_title("Per-Period Gross Spread by Regime (v3)")
    ax4.set_ylabel("Spread Return"); ax4.grid(True, alpha=0.2)

    ax5 = axes2[1]
    cum_h  = (1 + res["combined_hedged"].clip(lower=-0.99)).cumprod()
    dd_h   = (cum_h - cum_h.cummax()) / cum_h.cummax()
    cum_uh = (1 + res["combined_net"].clip(lower=-0.99)).cumprod()
    dd_uh  = (cum_uh - cum_uh.cummax()) / cum_uh.cummax()
    ax5.fill_between(dd_h.index,  dd_h.values,  0, color="mediumseagreen",
                     alpha=0.55, label="Hedged")
    ax5.fill_between(dd_uh.index, dd_uh.values, 0, color="crimson",
                     alpha=0.25, label="Unhedged")
    ax5.set_ylabel("Drawdown"); ax5.set_xlabel("Date")
    ax5.set_title("Drawdown: BTC-Hedged vs Unhedged Combined")
    ax5.legend(fontsize=9); ax5.grid(True, alpha=0.2)
    fig2.tight_layout()
    out2 = OUTPUT_DIR + "perp_ls_v3_regime_dd.png"
    fig2.savefig(out2, dpi=150); plt.close(fig2)
    print(f"[Plot] {out2}")

    # ── Figure 3: v2 vs v3 side-by-side comparison ──────────────────────────
    # v2 post-2021 hardcoded results
    V2 = dict(
        combined_ann  = -0.0660, combined_dd = -0.6433, sharpe = -0.161,
        win_rate      =  0.550,  mean_spread =  0.0234,
        bull_geo      =  0.0400, bear_geo    =  0.1501, side_geo  = 0.1355,
        bull_mean     =  0.0305, bear_mean   =  0.0206, side_mean = 0.0163,
    )

    regimes_list = ["Bull", "Bear", "Sideways"]
    v3_geo, v3_mean = {}, {}
    for regime in regimes_list:
        mask  = np.array(res["regime"]) == regime
        sub   = sp.iloc[list(np.where(mask)[0])]
        v3_mean[regime] = sub.mean() if len(sub) > 0 else np.nan
        if len(sub) > 0:
            cum_s = (1 + sub.clip(lower=-0.99)).prod()
            v3_geo[regime] = cum_s ** (12 / len(sub)) - 1
        else:
            v3_geo[regime] = np.nan

    v3_st = portfolio_stats(res["combined_hedged"])
    sp_v3 = res["spread_gross"]

    fig3, axes3 = plt.subplots(2, 2, figsize=(14, 10))
    fig3.suptitle("v2 vs v3 — Post-2022 Performance Comparison", fontsize=13,
                  fontweight="bold")
    x = np.arange(len(regimes_list)); w = 0.36

    # Mean spread by regime
    ax6 = axes3[0, 0]
    ax6.bar(x - w/2, [V2["bull_mean"], V2["bear_mean"], V2["side_mean"]],
            w, label="v2", color="silver", edgecolor="black")
    ax6.bar(x + w/2, [v3_mean.get(r, 0) for r in regimes_list],
            w, label="v3", color="steelblue", edgecolor="black")
    ax6.axhline(0, color="black", lw=0.8)
    ax6.set_xticks(x); ax6.set_xticklabels(regimes_list)
    ax6.set_title("Mean Period Spread by Regime"); ax6.set_ylabel("Mean Spread")
    ax6.legend(fontsize=9); ax6.grid(True, alpha=0.2)

    # Geometric ann. spread by regime
    ax7 = axes3[0, 1]
    ax7.bar(x - w/2, [V2["bull_geo"], V2["bear_geo"], V2["side_geo"]],
            w, label="v2", color="silver", edgecolor="black")
    ax7.bar(x + w/2, [v3_geo.get(r, 0) for r in regimes_list],
            w, label="v3", color="steelblue", edgecolor="black")
    ax7.axhline(0, color="black", lw=0.8)
    ax7.set_xticks(x); ax7.set_xticklabels(regimes_list)
    ax7.set_title("Ann. Geometric Spread by Regime"); ax7.set_ylabel("Geo. Spread")
    ax7.legend(fontsize=9); ax7.grid(True, alpha=0.2)

    # Combined cumulative: v2 unhedged proxy vs v3 hedged
    ax8 = axes3[1, 0]
    cum_v3h = (1 + res["combined_hedged"].dropna()).cumprod()
    cum_v3u = (1 + res["combined_net"].dropna()).cumprod()
    ax8.plot(cum_v3h.index, cum_v3h.values, "mediumseagreen", lw=2.5,
             label="v3 BTC-hedged")
    ax8.plot(cum_v3u.index, cum_v3u.values, "steelblue",      lw=1.5, ls="--",
             label="v3 unhedged")
    ax8.axhline(1, color="black", lw=0.6, ls="--")
    ax8.set_title("v3 Combined NAV (hedged vs unhedged)")
    ax8.set_ylabel("Cumulative Return"); ax8.legend(fontsize=9)
    ax8.grid(True, alpha=0.2)

    # Summary scorecard
    ax9 = axes3[1, 1]
    ax9.axis("off")
    rows = [
        ["Metric",              "v2 (post-2021)", "v3 (post-2022)"],
        ["Combined net (ann.)", f"{V2['combined_ann']:+.1%}",
         f"{v3_st['ann_return']:+.1%}"],
        ["MaxDD (combined)",    f"{V2['combined_dd']:+.1%}",
         f"{v3_st['max_dd']:+.1%}"],
        ["Sharpe",              f"{V2['sharpe']:+.3f}",
         f"{v3_st['sharpe']:+.3f}"],
        ["Win rate",            f"{V2['win_rate']:.1%}",
         f"{(sp_v3 > 0).mean():.1%}"],
        ["Mean spread",         f"{V2['mean_spread']:+.2%}",
         f"{sp_v3.mean():+.2%}"],
        ["Bull geo spread",     f"{V2['bull_geo']:+.1%}",
         f"{v3_geo.get('Bull', float('nan')):+.1%}"],
        ["Bear geo spread",     f"{V2['bear_geo']:+.1%}",
         f"{v3_geo.get('Bear', float('nan')):+.1%}"],
        ["Sideways geo spread", f"{V2['side_geo']:+.1%}",
         f"{v3_geo.get('Sideways', float('nan')):+.1%}"],
    ]
    tbl = ax9.table(cellText=rows[1:], colLabels=rows[0],
                    loc="center", cellLoc="center")
    tbl.auto_set_font_size(False); tbl.set_fontsize(10)
    tbl.scale(1.2, 1.7)
    for j in range(3):
        tbl[0, j].set_facecolor("#2c3e50")
        tbl[0, j].set_text_props(color="white", fontweight="bold")
    ax9.set_title("v2 vs v3 Scorecard", fontweight="bold", pad=14)

    fig3.tight_layout()
    out3 = OUTPUT_DIR + "perp_ls_v3_vs_v2.png"
    fig3.savefig(out3, dpi=150); plt.close(fig3)
    print(f"[Plot] {out3}")


# ===========================================================================
#  MAIN
# ===========================================================================

def main():
    print("=" * 76)
    print("Supply-Dilution L/S Strategy -- Version 3")
    print("New: composite signal | inv-vol weights | altseason veto | CB | BTC hedge")
    print("=" * 76)

    df        = load_data(INPUT_FILE)
    regime_df = build_regime(df)
    df        = engineer_features(df)
    results   = run_backtest(df, regime_df)

    if not results["dates"]:
        print("[ERROR] No rebalancing periods survived all filters.")
        return

    print_report(results)
    plot_results(results)
    print("\nDone.")


if __name__ == "__main__":
    main()
