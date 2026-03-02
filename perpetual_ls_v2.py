"""
perpetual_ls_v2.py
==================
Supply-Dilution L/S Strategy — Version 2 (Institutional Risk-Reviewed)

All improvements from the Senior QRM teardown report applied:

  EXCLUSION FIXES
  ---------------
  [FIX-1]  Wrapped BTC/ETH/BNB assets removed (WBTC, BTCB, BTC.b, rBTC, etc.)
  [FIX-2]  Liquid staking derivatives removed (stETH, jSOL, msol, METH, etc.)
  [FIX-3]  Protocol-internal synthetics removed (vBTC, vETH, vBNB, VTHO, etc.)
  [FIX-4]  Commodity-backed tokens removed (PAXG, XAUt, KAU)

  SIGNAL FIXES
  ------------
  [FIX-5]  Supply window extended: 4w -> 13w (reduces noise, lowers turnover)
  [FIX-6]  Minimum supply history: 26 consecutive weeks required before eligible
  [FIX-7]  Minimum $5M daily volume filter (executability gate)
  [FIX-8]  Universe ceiling lowered: rank <= 200 (vs. 250)

  PORTFOLIO CONSTRUCTION
  ----------------------
  [FIX-9]  Inner buffer band: enter <=7th pct / exit <=13th pct  (long)
                               enter >=93rd pct / exit >=87th pct (short)
  [FIX-10] ADTV-weighted allocation replaces equal-weight (sqrt(vol), 15% cap)
  [FIX-11] Momentum overlay: exclude bottom-20th-pct trailing-4w-return tokens
           from long basket (avoids catching dead-project falling knives)

  RISK MANAGEMENT
  ---------------
  [FIX-12] Regime-aware L/S scaling:
             Sideways (BTC in 90-110% of MA20)  : Long 100% / Short 100%
             Bull + moderate vol                 : Long 100% / Short  50%
             Bull + high vol  (vol > 80% ann.)  : Long  75% / Short  25%
             Bear + moderate vol                 : Long  75% / Short  75%
             Bear + high vol                     : Long  50% / Short  25%

SUPPLY MANIPULATION RISKS (documented for live deployment awareness)
======================================================================
  The circulating supply metric can be contaminated without any true token
  issuance occurring. Known attack vectors that corrupt the inflation signal:

  1. CMC Reclassification — Teams submit updated supply tracking to CMC,
     moving tokens between "circulating" and "locked" categories overnight.
     No on-chain activity; purely a reporting change. Creates false +/- signal.

  2. Bridge Minting Double-Count — WBTC/BTCB/BTC.b minted = BTC locked +
     new wrapped token on target chain. CMC counts both. Fixed by [FIX-1].

  3. Staking Lock/Unlock Oscillation — ETH staked into Lido: CMC may reduce
     ETH circulating supply AND add stETH supply. Net double-signal. [FIX-2].

  4. Treasury Reclassification — Team moves tokens from "project treasury"
     to "ecosystem fund." CMC reclassifies as circulating. Zero selling pressure,
     yet appears as a massive supply inflation event.

  5. Token Migration (v1->v2) — Both tokens briefly counted simultaneously,
     creating a spike then collapse in the old token's circulating supply.

  6. Protocol Receipt Token Mechanics — vBTC/vETH supply grows with every
     interest accrual. Pure accounting artifact; [FIX-3] removes these.

  7. Airdrop Timing Manipulation — Knowing systematic funds sort on 4-week
     supply change, a team can time a large airdrop distribution to fall
     just AFTER a monthly rebalancing window to avoid appearing in the
     short basket. Extended 13-week window [FIX-5] reduces but does not
     eliminate this vulnerability.

  8. Burn-and-Reissue Cycles — Tokens burned on Chain A, re-issued on
     Chain B. CMC may record deflation on Chain A and inflation on Chain B,
     each generating opposing basket signals simultaneously.

  9. LP Token Supply Pollution — AMM LP tokens reported as underlying asset
     circulating supply on some data sources.

  10. Float Compression Gaming — Team self-custodies tokens to reduce CMC
      circulating count, then gradually releases to manufacture a controlled
      inflation pattern that games a known rebalancing schedule.

  MITIGATION: Use on-chain data (Glassnode, Dune Analytics) as primary
  circulating supply source. CMC data should be validation layer only.

REQUIRED DATA SOURCES FOR LIVE DEPLOYMENT
==========================================
  Tier 1 (Essential):
    - Binance USDT-M Futures API  : Daily OHLCV + funding rates per token
      Endpoints: GET /fapi/v1/klines, GET /fapi/v1/fundingRate
    - Bybit Linear Perpetuals API : Coverage for tokens Binance does not list
      Endpoints: GET /v5/market/kline, GET /v5/market/funding/history
    - CoinGecko Pro API           : Daily circulating supply (better than CMC)
      Endpoint: GET /coins/{id}/market_chart?vs_currency=usd&days=365
    - Coinglass API               : Historical funding rates + Open Interest
      Endpoint: GET /api/pro/v1/futures/funding-rate/history

  Tier 2 (Important):
    - Glassnode API               : Exchange inflows (leading sell-pressure)
      Endpoint: GET /v1/metrics/transactions/transfers_volume_to_exchanges_sum
    - Token Terminal API          : Protocol revenue (for RER overlay)
      Endpoint: GET /v2/projects/{slug}/metrics
    - DefiLlama API (free)        : Protocol TVL + fees (RER proxy)
      Endpoint: GET https://api.llama.fi/protocols

  Tier 3 (Alpha enhancement):
    - Messari Pro API             : Token unlock calendars + vesting schedules
    - TokenUnlocks.app            : Forward-looking unlock calendar
    - Nansen API                  : Wallet labeling (track VC/team movements)
    - Dune Analytics              : On-chain vesting contract outflow queries
"""

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import warnings
warnings.filterwarnings("ignore")

# ===========================================================================
#  CONFIGURATION
# ===========================================================================

INPUT_FILE   = "D:/AI_Projects/circ_supply/cmc_historical_top300_filtered_with_supply.csv"
OUTPUT_DIR   = "D:/AI_Projects/circ_supply/"

# Universe
MAX_RANK             = 200      # [FIX-8] lowered from 250
TOP_N_EXCLUDE        = 20
MIN_VOLUME           = 5_000_000   # [FIX-7] $5M daily volume gate
MIN_SUPPLY_HISTORY   = 26       # [FIX-6] weeks of non-NaN supply history
FFILL_LIMIT          = 1

# Signal
SUPPLY_WINDOW        = 13       # [FIX-5] weeks (~90 days)

# Portfolio — inner buffer band [FIX-9]
LONG_ENTRY_PCT       = 0.07     # enter long if rank <= 7th pct
LONG_EXIT_PCT        = 0.13     # exit long  if rank > 13th pct
SHORT_ENTRY_PCT      = 0.93     # enter short if rank >= 93rd pct
SHORT_EXIT_PCT       = 0.87     # exit short  if rank < 87th pct

MIN_BASKET_SIZE      = 6        # skip period if fewer tokens than this

# ADTV weighting [FIX-10]
ADTV_POS_CAP         = 0.15     # max weight per position

# Momentum filter [FIX-11]
MOMENTUM_EXCLUDE_PCT = 0.20     # bottom 20th pct trailing 4-week return excluded from long

# Execution costs
TAKER_FEE            = 0.0004
SLIPPAGE_K           = 0.0005
MIN_TURNOVER         = 0.001
MAX_SLIPPAGE         = 0.02

# Regime detection [FIX-12]
REGIME_MA_WINDOW     = 20       # weekly periods
BULL_BAND            = 1.10     # BTC price > MA * 1.10 -> Bull
BEAR_BAND            = 0.90     # BTC price < MA * 0.90 -> Bear
HIGH_VOL_THRESHOLD   = 0.80     # 80% annualized BTC vol -> high vol
VOL_WINDOW           = 8        # weeks for realized vol calculation

# Regime-aware L/S scaling [FIX-12]
REGIME_LS_SCALE = {
    # (regime, high_vol): (long_scale, short_scale)
    ("Sideways", False): (1.00, 1.00),
    ("Sideways", True):  (1.00, 0.75),
    ("Bull",     False): (1.00, 0.50),
    ("Bull",     True):  (0.75, 0.25),
    ("Bear",     False): (0.75, 0.75),
    ("Bear",     True):  (0.50, 0.25),
}

# Funding rate model (synthetic, regime-based)
FUNDING_8H = {
    ("Bull", "long"):  +0.0000800,
    ("Bull", "short"): +0.0001500,
    ("Bear", "long"):  +0.0000200,
    ("Bear", "short"): +0.0000500,
    ("Sideways","long"):  +0.0000500,
    ("Sideways","short"): +0.0000800,
}
FUNDING_PERIODS_PER_DAY = 3

# Backtest date range — start POST-2021 bull-run peak
START_DATE = pd.Timestamp("2022-01-01")   # change to None to use full history

# Forward return winsorisation
WINS_LOW   = 0.01
WINS_HIGH  = 0.99

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

# [FIX-1] Wrapped assets: BTC / ETH / other chain bridges
WRAPPED_ASSETS = {
    # BTC wrappers and bridges
    "WBTC", "BTCB", "BBTC", "RBTC", "rBTC", "FBTC",
    "UNIBTC", "PUMPBTC", "EBTC", "LBTC",
    "SolvBTC", "xSolvBTC",
    # ETH wrappers
    "WETH",
    # BNB wrappers
    "WBNB",
    # Ren protocol bridges (deprecated)
    "renBTC", "renETH", "renDOGE", "renZEC",
    # Avalanche bridged BTC
    "BTC.b",
    # SolvBTC Babylon
    "SolvBTC.BBN",
}

# [FIX-2] Liquid staking derivatives (LSDs and restaking receipts)
LIQUID_STAKING = {
    # ETH liquid staking
    "STETH", "RETH", "CBETH", "ANKRETH", "FRXETH",
    "OSETH", "LSETH", "METH", "EZETH", "EETH",
    "SFRXETH", "CMETH", "BETH", "TETH", "PZETH",
    "ETHX", "PUFETH", "RSETH",
    # SOL liquid staking
    "JITOSOL", "MSOL", "BNSOL", "JUPSOL", "BSOL",
    "BBSOL", "JSOL",
    # Other chains
    "SAVAX", "sAVAX", "STRX", "HASUI", "KHYPE",
    # BNB staking receipts
    "slisBNB", "WBETH",
    # Staking module receipts (Aave safety module)
    "stkAAVE", "STKAAVE",
}

# [FIX-3] Protocol-internal synthetic tokens and dual-token gas
PROTOCOL_SYNTHETICS = {
    # Venus Protocol (BSC lending receipt tokens — no external market)
    "vBTC", "vETH", "vBNB", "vXVS", "VRT",
    # VeThor — continuously minted VeChain gas (not an investable asset)
    "VTHO",
}

# [FIX-4] Commodity-backed tokens
COMMODITY_BACKED = {
    "PAXG",   # PAX Gold (gold-backed, no perp market)
    "XAUT",   # Tether Gold
    "XAUt",   # Tether Gold (alternate ticker)
    "KAU",    # Kinesis Gold
}

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
    med  = float(np.median(gaps)) if len(gaps) > 0 else 30.0
    return 365.25 / max(med, 1.0)


def portfolio_stats(returns: pd.Series) -> dict:
    returns = returns.dropna()
    if len(returns) < 2:
        nan = np.nan
        return dict(ann_return=nan, vol=nan, sharpe=nan, sortino=nan, max_dd=nan)
    cum         = (1 + returns).cumprod()
    total_years = max((returns.index[-1]-returns.index[0]).days / 365.25, 1/52)
    cum_final   = float(cum.iloc[-1])
    ann_return  = (cum_final**(1/total_years)-1) if cum_final > 0 else np.nan
    ppy         = _ppy(returns)
    vol         = returns.std() * np.sqrt(ppy)
    sharpe      = ann_return/vol if vol > 0 and not np.isnan(ann_return) else np.nan
    down        = returns[returns < 0]
    sortino     = (ann_return / (np.sqrt((down**2).mean())*np.sqrt(ppy))
                   if len(down) > 0 else np.nan)
    roll_max    = cum.cummax()
    max_dd      = float(((cum - roll_max) / roll_max).min())
    return dict(ann_return=ann_return, vol=vol, sharpe=sharpe,
                sortino=sortino, max_dd=max_dd)


def _fmt(v): return f"{v:+.2%}" if not np.isnan(v) else "    N/A"
def _fmtf(v, d=3): return f"{v:+.{d}f}" if not np.isnan(v) else "    N/A"


def get_ls_scales(regime: str, high_vol: bool) -> tuple:
    return REGIME_LS_SCALE.get((regime, high_vol),
           REGIME_LS_SCALE.get((regime, False), (1.0, 1.0)))


def adtv_weights(symbols: list, vol_map: dict, cap: float = ADTV_POS_CAP) -> dict:
    """Sqrt-of-ADTV weights, per-position capped, then renormalised."""
    raw = {s: max(float(vol_map.get(s, 0) or 0), 0.0)**0.5 for s in symbols}
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
#  STEP 1 — Load & preprocess
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
#  STEP 2 — Regime + BTC volatility
# ===========================================================================

def build_regime(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns DataFrame[snapshot_date, index_return, regime, high_vol].
    Regime: Bull / Bear / Sideways.
    high_vol: True when 8-week rolling BTC realized vol > HIGH_VOL_THRESHOLD.
    """
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

    # BTC 8-week realized vol (annualised)
    # NOTE: use .values to avoid index-alignment mismatch (idx has integer index)
    btc_rets = (df[df["symbol"] == "BTC"]
                  .set_index("snapshot_date")["price"]
                  .pct_change(1))
    btc_vol_series = (btc_rets.reindex(idx["snapshot_date"])
                               .rolling(VOL_WINDOW, min_periods=4)
                               .std() * np.sqrt(52))
    idx["btc_vol_8w"] = btc_vol_series.values
    idx["high_vol"]   = idx["btc_vol_8w"] > HIGH_VOL_THRESHOLD

    n_bull = (idx["regime"] == "Bull").sum()
    n_bear = (idx["regime"] == "Bear").sum()
    n_side = (idx["regime"] == "Sideways").sum()
    n_hv   = idx["high_vol"].sum()
    print(f"[Regime] Bull={n_bull} Bear={n_bear} Sideways={n_side} HighVol={n_hv}")
    return idx[["snapshot_date", "index_return", "regime", "high_vol"]]


# ===========================================================================
#  STEP 3 — Feature engineering
# ===========================================================================

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    grp = df.groupby("symbol", group_keys=False)

    # [FIX-5] 13-week trailing supply inflation
    df["supply_inf"] = grp["circulating_supply"].transform(
        lambda s: s.pct_change(SUPPLY_WINDOW)
    )

    # [FIX-6] Rolling count of non-NaN supply history
    df["supply_hist_count"] = grp["supply_inf"].transform(
        lambda s: s.notna().cumsum()
    )

    # Slippage proxy
    df["turnover"] = (df["volume_24h"] / df["market_cap"]).clip(lower=MIN_TURNOVER)
    df["slippage"] = (SLIPPAGE_K / df["turnover"]).clip(upper=MAX_SLIPPAGE)

    return df


# ===========================================================================
#  STEP 4 — Main backtest loop
# ===========================================================================

def run_backtest(df: pd.DataFrame, regime_df: pd.DataFrame) -> dict:
    df["ym"]      = df["snapshot_date"].dt.to_period("M")
    rebal_dates   = sorted(df.groupby("ym")["snapshot_date"].min().tolist())
    regime_map    = regime_df.set_index("snapshot_date")[["regime", "high_vol"]].to_dict("index")

    # Eligible universe at each rebalancing date (all filters applied)
    inf_snap = df[df["snapshot_date"].isin(rebal_dates)].copy()
    inf_snap = inf_snap[inf_snap["supply_inf"].notna()]
    inf_snap = inf_snap[~inf_snap["symbol"].isin(EXCLUDED)]
    inf_snap = inf_snap[(inf_snap["rank"] > TOP_N_EXCLUDE) & (inf_snap["rank"] <= MAX_RANK)]
    inf_snap = inf_snap[inf_snap["volume_24h"] >= MIN_VOLUME]              # [FIX-7]
    inf_snap = inf_snap[inf_snap["supply_hist_count"] >= MIN_SUPPLY_HISTORY]  # [FIX-6]

    price_piv = df.pivot_table(index="snapshot_date", columns="symbol",
                               values="price",      aggfunc="last")
    vol_piv   = df.pivot_table(index="snapshot_date", columns="symbol",
                               values="volume_24h", aggfunc="last")
    slip_piv  = df.pivot_table(index="snapshot_date", columns="symbol",
                               values="slippage",   aggfunc="last")

    # State for inner buffer band [FIX-9]
    prev_long_set  = set()
    prev_short_set = set()

    # Accumulators
    (dates_out, long_gross, short_gross,
     long_net, short_net, combined_net,
     basket_sizes, regime_out, scale_out,
     fund_long_cum, fund_short_cum, fund_hist) = ([], [], [], [], [], [],
                                                   [], [], [],
                                                   0.0, 0.0, [])

    sorted_rebals = sorted(rebal_dates)

    # Apply START_DATE filter — data is still loaded fully so history is correct
    if START_DATE is not None:
        sorted_rebals = [d for d in sorted_rebals if d >= START_DATE]

    for i, t0 in enumerate(sorted_rebals[:-1]):
        t1 = sorted_rebals[i + 1]
        hold_days = max((t1 - t0).days, 1)

        univ = inf_snap[inf_snap["snapshot_date"] == t0].copy()
        if len(univ) < MIN_BASKET_SIZE * 2:
            continue

        # Percentile rank within eligible universe
        univ["pct_rank"] = univ["supply_inf"].rank(pct=True)
        rank_map = univ.set_index("symbol")["pct_rank"].to_dict()
        all_syms = set(univ["symbol"])

        # --- Inner buffer band logic [FIX-9] ---
        # Long basket
        entry_long  = {s for s in all_syms if rank_map[s] <= LONG_ENTRY_PCT}
        stay_long   = {s for s in (prev_long_set & all_syms)
                       if rank_map[s] <= LONG_EXIT_PCT}
        basket_long = entry_long | stay_long

        # Short basket
        entry_short  = {s for s in all_syms if rank_map[s] >= SHORT_ENTRY_PCT}
        stay_short   = {s for s in (prev_short_set & all_syms)
                        if rank_map[s] >= SHORT_EXIT_PCT}
        basket_short = entry_short | stay_short

        # Remove overlap (token cannot be in both baskets simultaneously)
        overlap      = basket_long & basket_short
        basket_long  -= overlap
        basket_short -= overlap

        if len(basket_long) < MIN_BASKET_SIZE or len(basket_short) < MIN_BASKET_SIZE:
            prev_long_set  = basket_long
            prev_short_set = basket_short
            continue

        prev_long_set  = basket_long
        prev_short_set = basket_short

        if t0 not in price_piv.index or t1 not in price_piv.index:
            continue

        # --- Momentum filter on long basket [FIX-11] ---
        if i > 0:
            t_prev = sorted_rebals[i - 1]
            if t_prev in price_piv.index:
                p_prev = price_piv.loc[t_prev]
                p_now  = price_piv.loc[t0]
                mom    = (p_now / p_prev - 1)
                univ_mom_vals = [float(mom.get(s, np.nan))
                                 for s in all_syms if not pd.isna(mom.get(s, np.nan))]
                if univ_mom_vals:
                    mom_floor = np.percentile(univ_mom_vals, MOMENTUM_EXCLUDE_PCT * 100)
                    basket_long = {s for s in basket_long
                                   if not pd.isna(mom.get(s, np.nan))
                                   and float(mom.get(s, 0)) > mom_floor}
                    if len(basket_long) < MIN_BASKET_SIZE:
                        continue  # momentum filter too aggressive this period

        # --- Forward returns ---
        p0 = price_piv.loc[t0]
        p1 = price_piv.loc[t1]
        v0 = vol_piv.loc[t0]  if t0 in vol_piv.index  else pd.Series(dtype=float)
        sl = slip_piv.loc[t0] if t0 in slip_piv.index else pd.Series(dtype=float)

        fwd  = (p1 / p0 - 1)
        # Cross-sectional winsorize
        lo, hi = fwd.quantile(WINS_LOW), fwd.quantile(WINS_HIGH)
        fwd  = fwd.clip(lower=lo, upper=hi).clip(lower=-1.0)

        def basket_return_weighted(symbols: set) -> tuple:
            """Return (weighted_ret, avg_slip) using ADTV weights [FIX-10]."""
            syms = [s for s in symbols
                    if s in fwd.index and not pd.isna(fwd[s])]
            if not syms:
                return np.nan, MAX_SLIPPAGE

            vol_map = {s: float(v0.get(s, 0) or 0) for s in syms}
            w       = adtv_weights(syms, vol_map)

            ret     = sum(w[s] * float(fwd[s]) for s in syms)
            slip    = sum(w[s] * float(sl.get(s, MAX_SLIPPAGE) or MAX_SLIPPAGE)
                         for s in syms)
            return float(ret), float(slip)

        r_long_gross,  slip_long  = basket_return_weighted(basket_long)
        r_short_gross, slip_short = basket_return_weighted(basket_short)

        if pd.isna(r_long_gross) or pd.isna(r_short_gross):
            continue

        # --- Regime & L/S scaling [FIX-12] ---
        reg_info = regime_map.get(t0, {"regime": "Sideways", "high_vol": False})
        regime   = reg_info.get("regime",   "Sideways")
        high_vol = bool(reg_info.get("high_vol", False))
        long_scale, short_scale = get_ls_scales(regime, high_vol)

        # --- Costs ---
        fee_cost    = 2 * TAKER_FEE
        n_payments  = FUNDING_PERIODS_PER_DAY * hold_days
        r_fl        = FUNDING_8H.get((regime, "long"),  0.0)
        r_fs        = FUNDING_8H.get((regime, "short"), 0.0)
        fund_drag_l = r_fl * n_payments      # longs pay
        fund_cred_s = r_fs * n_payments      # shorts receive

        # --- Net returns ---
        r_long_net  = r_long_gross  - fee_cost - slip_long  - fund_drag_l
        r_short_net = -r_short_gross - fee_cost - slip_short + fund_cred_s

        # Combined: scaled by regime, normalised to NAV = 1
        denom      = long_scale + short_scale
        r_combined = (long_scale * r_long_net + short_scale * r_short_net) / denom

        # Hard floor
        r_long_net  = max(r_long_net,  -1.0)
        r_short_net = max(r_short_net, -1.0)
        r_combined  = max(r_combined,  -1.0)

        # --- Funding attribution ---
        pl  = -fund_drag_l
        ps  = +fund_cred_s
        fund_long_cum  += pl
        fund_short_cum += ps
        fund_hist.append({"date": t0, "fund_long": pl, "fund_short": ps,
                          "net_funding": pl + ps})

        dates_out.append(t0)
        long_gross.append(max(r_long_gross,  -1.0))
        short_gross.append(max(r_short_gross,-1.0))
        long_net.append(r_long_net)
        short_net.append(r_short_net)
        combined_net.append(r_combined)
        basket_sizes.append((len(basket_long), len(basket_short)))
        regime_out.append(regime)
        scale_out.append((long_scale, short_scale))

    idx = pd.DatetimeIndex(dates_out)
    return dict(
        index        = idx,
        dates        = dates_out,
        long_gross   = pd.Series(long_gross,   index=idx, name="Long  Gross"),
        short_gross  = pd.Series(short_gross,  index=idx, name="Short Gross"),
        long_net     = pd.Series(long_net,     index=idx, name="Long  Net"),
        short_net    = pd.Series(short_net,    index=idx, name="Short Net"),
        combined_net = pd.Series(combined_net, index=idx, name="Combined Net"),
        spread_gross = pd.Series(
            [lg - sg for lg, sg in zip(long_gross, short_gross)],
            index=idx, name="Spread Gross"),
        basket_sizes = basket_sizes,
        regime       = regime_out,
        scale        = scale_out,
        fund_long_cum  = fund_long_cum,
        fund_short_cum = fund_short_cum,
        fund_hist    = pd.DataFrame(fund_hist),
    )


# ===========================================================================
#  STEP 5 — Report
# ===========================================================================

def print_report(res: dict) -> None:
    print("\n" + "=" * 74)
    print("PERPETUAL L/S BACKTEST v2 — INSTITUTIONAL RISK-REVIEWED")
    print(f"  Supply window : {SUPPLY_WINDOW}w | Universe: rank {TOP_N_EXCLUDE+1}-{MAX_RANK} "
          f"| MinVol: ${MIN_VOLUME/1e6:.0f}M")
    print(f"  Buffer entry  : Long<={LONG_ENTRY_PCT:.0%} / Short>={SHORT_ENTRY_PCT:.0%}  "
          f"exit: Long>{LONG_EXIT_PCT:.0%} / Short<{SHORT_EXIT_PCT:.0%}")
    print("=" * 74)

    n = len(res["dates"])
    avg_lo = np.mean([s[0] for s in res["basket_sizes"]])
    avg_hi = np.mean([s[1] for s in res["basket_sizes"]])
    print(f"\n  Rebalancing periods : {n}")
    print(f"  Avg basket size     : Long {avg_lo:.1f} | Short {avg_hi:.1f} tokens")

    regimes = np.array(res["regime"])
    scales  = res["scale"]
    print(f"  Regime breakdown    : Bull={( regimes=='Bull').sum()}  "
          f"Bear={(regimes=='Bear').sum()}  "
          f"Sideways={(regimes=='Sideways').sum()}")
    avg_ls = (np.mean([s[0] for s in scales]), np.mean([s[1] for s in scales]))
    print(f"  Avg effective scale : Long {avg_ls[0]:.2f}x / Short {avg_ls[1]:.2f}x")

    # --- Per-series stats ---
    print(f"\n  {'Series':<32} {'Ann.Ret':>10} {'Vol':>10} "
          f"{'Sharpe':>8} {'Sortino':>8} {'MaxDD':>10}")
    print("  " + "-" * 72)
    for name, s in [
        ("Long basket  (gross)",    res["long_gross"]),
        ("Short basket (gross)",    res["short_gross"]),
        ("Long leg     (net)",      res["long_net"]),
        ("Short leg    (net)*",     res["short_net"]),
        ("L/S Combined (net)",      res["combined_net"]),
    ]:
        st = portfolio_stats(s)
        print(f"  {name:<32} {_fmt(st['ann_return']):>10} {_fmt(st['vol']):>10} "
              f"{_fmtf(st['sharpe']):>8} {_fmtf(st['sortino']):>8} "
              f"{_fmt(st['max_dd']):>10}")
    print("  * Short leg net: +ve = profit for the short position")

    # --- Win rate & spread ---
    sp    = res["spread_gross"]
    wins  = (sp > 0).mean()
    print(f"\n  Win rate (Long > Short, gross) : {(sp>0).sum()}/{len(sp)} ({wins:.1%})")
    print(f"  Mean period spread (gross)     : {sp.mean():.2%}")
    sps   = portfolio_stats(sp)
    print(f"  Spread ann. vol                : {_fmt(sps['vol'])}")
    print(f"  Spread excess kurtosis         : {sp.kurtosis():.2f}")
    print(f"  Spread skewness                : {sp.skew():.2f}")

    # --- Regime breakdown ---
    print(f"\n  --- Regime-Conditional Spread (gross) ---")
    print(f"  {'Regime':<12} {'N':>5} {'Mean Spread':>13} {'Win Rate':>10} "
          f"{'Ann.Geo.Spread':>16}")
    for regime in ["Bull", "Bear", "Sideways"]:
        mask  = np.array(res["regime"]) == regime
        if mask.sum() == 0:
            continue
        sub   = sp.iloc[list(np.where(mask)[0])]
        mean  = sub.mean()
        wr    = (sub > 0).mean()
        n_sub = len(sub)
        cum   = (1 + sub.clip(lower=-0.99)).prod()
        ann   = cum**(12/n_sub) - 1 if n_sub > 0 else np.nan
        print(f"  {regime:<12} {n_sub:>5}   {mean:>+12.2%}   {wr:>8.1%}   {ann:>+14.2%}")

    # --- Funding ---
    print(f"\n  --- Funding Rate Attribution (synthetic model) ---")
    print(f"  Cum. funding drag  (long pays)    : {res['fund_long_cum']:+.4f} "
          f"({res['fund_long_cum']:.2%})")
    print(f"  Cum. funding credit(short receives): {res['fund_short_cum']:+.4f} "
          f"({res['fund_short_cum']:.2%})")
    net_f = res["fund_long_cum"] + res["fund_short_cum"]
    print(f"  Net funding impact                : {net_f:+.4f} ({net_f:.2%})")
    print("\n  NOTE: Funding rates are SYNTHETIC. Replace FUNDING_8H with")
    print("        real per-token 8h rates from Coinglass API for live use.")
    print("=" * 74)


# ===========================================================================
#  STEP 6 — Plots
# ===========================================================================

def plot_results(res: dict) -> None:

    def shade_regimes(ax):
        prev = None
        start = None
        for dt, reg in zip(res["dates"], res["regime"]):
            if reg != prev:
                if start and prev == "Bear":
                    ax.axvspan(start, dt, alpha=0.08, color="crimson", zorder=0)
                elif start and prev == "Bull":
                    ax.axvspan(start, dt, alpha=0.05, color="steelblue", zorder=0)
                start, prev = dt, reg
        if start and prev == "Bear":
            ax.axvspan(start, res["dates"][-1], alpha=0.08, color="crimson", zorder=0)
        elif start and prev == "Bull":
            ax.axvspan(start, res["dates"][-1], alpha=0.05, color="steelblue", zorder=0)

    idx = res["index"]

    # ── Figure 1: cumulative wealth ─────────────────────────────────────────
    fig, axes = plt.subplots(3, 1, figsize=(13, 13),
                             gridspec_kw={"height_ratios": [3, 2, 1.5]})
    fig.suptitle(
        f"Supply-Dilution L/S v2 (Institutional-Reviewed)\n"
        f"{SUPPLY_WINDOW}w signal | 7th/93rd entry buffer | "
        f"ADTV-weighted | Regime-scaled | Vol filter ${MIN_VOLUME/1e6:.0f}M",
        fontsize=12, fontweight="bold"
    )

    ax = axes[0]
    for series, color, lw, label in [
        (res["long_gross"],  "steelblue",       2.0, "Long basket (gross)"),
        (res["short_gross"], "crimson",          2.0, "Short basket (gross)"),
        (res["combined_net"],"mediumseagreen",   2.5, "L/S Combined (net)"),
    ]:
        cum = (1 + series.dropna()).cumprod()
        ax.semilogy(cum.index, cum.values, color=color, lw=lw, label=label)
    ax.axhline(1, color="black", lw=0.6, ls="--")
    shade_regimes(ax)
    ax.set_ylabel("Cumulative Return (log)")
    ax.set_title("Cumulative Wealth — Blue=Bull, Red=Bear shading")
    ax.legend(fontsize=9); ax.grid(True, which="both", alpha=0.2)

    ax2 = axes[1]
    cum_ln = (1 + res["long_net"].dropna()).cumprod()
    cum_sn = (1 + res["short_net"].dropna()).cumprod()
    ax2.plot(cum_ln.index, cum_ln.values, color="steelblue", lw=1.8,
             label="Long net")
    ax2.plot(cum_sn.index, cum_sn.values, color="darkorange", lw=1.8,
             label="Short net")
    ax2.axhline(1, color="black", lw=0.6, ls="--")
    shade_regimes(ax2)
    ax2.set_ylabel("Cumulative Return")
    ax2.set_title("Net Leg Performance (after fees + slippage + funding + regime scaling)")
    ax2.legend(fontsize=9); ax2.grid(True, alpha=0.2)

    ax3 = axes[2]
    sp   = res["spread_gross"]
    cols = ["steelblue" if v >= 0 else "crimson" for v in sp.values]
    ax3.bar(sp.index, sp.values, color=cols, width=20, alpha=0.8)
    ax3.axhline(0, color="black", lw=0.8)
    ax3.set_ylabel("Period Spread\n(Long-Short, gross)")
    ax3.set_xlabel("Rebalance Date")
    ax3.grid(True, alpha=0.2)

    fig.tight_layout()
    out1 = OUTPUT_DIR + "perp_ls_v2_cumulative.png"
    fig.savefig(out1, dpi=150); plt.close(fig)
    print(f"[Plot] {out1}")

    # ── Figure 2: regime bar + drawdown ────────────────────────────────────
    fig2, axes2 = plt.subplots(2, 1, figsize=(12, 8))

    ax4 = axes2[0]
    regime_colors = {"Bull": "steelblue", "Bear": "crimson", "Sideways": "gray"}
    sp_arr = sp.values
    for j, (dt, reg) in enumerate(zip(res["dates"], res["regime"])):
        ax4.bar(dt, sp_arr[j], color=regime_colors.get(reg, "gray"),
                width=20, alpha=0.7)
    from matplotlib.patches import Patch
    patches = [Patch(color=c, alpha=0.7, label=l)
               for l, c in regime_colors.items()]
    ax4.axhline(0, color="black", lw=0.8)
    ax4.legend(handles=patches, fontsize=9)
    ax4.set_title("Per-Period Gross Spread Coloured by Regime")
    ax4.set_ylabel("Spread Return")
    ax4.grid(True, alpha=0.2)

    ax5 = axes2[1]
    cum   = (1 + res["combined_net"].clip(lower=-0.99)).cumprod()
    dd    = (cum - cum.cummax()) / cum.cummax()
    ax5.fill_between(dd.index, dd.values, 0, color="crimson", alpha=0.5)
    ax5.set_ylabel("Drawdown")
    ax5.set_xlabel("Date")
    ax5.set_title("L/S Combined Drawdown")
    ax5.grid(True, alpha=0.2)

    fig2.tight_layout()
    out2 = OUTPUT_DIR + "perp_ls_v2_regime_dd.png"
    fig2.savefig(out2, dpi=150); plt.close(fig2)
    print(f"[Plot] {out2}")

    # ── Figure 3: regime Sharpe comparison v1 vs v2 ─────────────────────
    fig3, ax6 = plt.subplots(figsize=(9, 5))
    v1_regime_sharpe = {"Bull": +0.292, "Bear": -0.517, "Sideways": +0.775}
    regimes_list  = ["Bull", "Bear", "Sideways"]
    v2_sharpes    = []
    for regime in regimes_list:
        mask  = np.array(res["regime"]) == regime
        sub   = sp.iloc[list(np.where(mask)[0])]
        std   = sub.std()
        mean  = sub.mean()
        v2_sharpes.append(mean / std * np.sqrt(12) if std > 0 else np.nan)

    x = np.arange(len(regimes_list))
    w = 0.35
    ax6.bar(x - w/2, [v1_regime_sharpe[r] for r in regimes_list],
            w, label="v1 (equal-wt, 4w signal)", color="silver", edgecolor="black")
    ax6.bar(x + w/2, v2_sharpes,
            w, label="v2 (ADTV-wt, 13w signal, regime scale)", color="steelblue",
            edgecolor="black")
    ax6.axhline(0, color="black", lw=0.8)
    ax6.set_xticks(x); ax6.set_xticklabels(regimes_list)
    ax6.set_ylabel("Ann. Sharpe (Spread)")
    ax6.set_title("v1 vs v2: Regime-Conditional Sharpe Ratio Comparison")
    ax6.legend(fontsize=9); ax6.grid(True, alpha=0.2)

    fig3.tight_layout()
    out3 = OUTPUT_DIR + "perp_ls_v2_v1_comparison.png"
    fig3.savefig(out3, dpi=150); plt.close(fig3)
    print(f"[Plot] {out3}")


# ===========================================================================
#  MAIN
# ===========================================================================

def main():
    print("=" * 74)
    print("Supply-Dilution L/S Strategy — Version 2")
    print("Improvements: wrapped assets removed | 13w signal | $5M vol filter")
    print("  | inner buffer | ADTV weights | momentum filter | regime scaling")
    print("=" * 74)

    df         = load_data(INPUT_FILE)
    regime_df  = build_regime(df)
    df         = engineer_features(df)
    results    = run_backtest(df, regime_df)

    if not results["dates"]:
        print("[ERROR] No rebalancing periods survived all filters.")
        return

    print_report(results)
    plot_results(results)
    print("\nDone.")


if __name__ == "__main__":
    main()
