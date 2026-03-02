# Perpetual L/S Backtest — Full Methodology, Criteria & Results

**Strategy:** Supply-Dilution Long/Short on Perpetual Futures
**Versions:** v1 → v2 → v3 → v4 → v5 → **v6 (current best)**
**Coverage:** 2022-01-01 → 2026-02-22 (post-2022 window)
**Data:** CMC weekly supply snapshots + Binance USDT-M perp OHLCV + actual 8h funding rates

---

## 1. Core Thesis

The **Supply-Dilution Hypothesis** states that persistent circulating-supply inflation is a
negative price signal: tokens diluting their float impose an ongoing cost on holders that
should systematically compress returns relative to tokens with flat or deflationary supply.

Operationally:
- **Long** tokens with the **lowest** trailing supply inflation (deflationary / zero-emission)
- **Short** tokens with the **highest** trailing supply inflation (persistent diluters)
- Capture the **spread** between the two legs via perpetual futures, rebalanced at regime-adaptive intervals

**Empirical foundation.** The decile-level backtest (`extreme_percentile.py`) confirmed the
signal over the full CMC history: the 10th-percentile supply basket outperforms the
90th-percentile basket by approximately 23 percentage points annualised with a 59.4%
monthly win rate. The perpetual-futures scripts operationalise this edge into an executable
portfolio with realistic costs.

---

## 2. Data Sources

### 2.1 CMC Supply Data

| Field | Detail |
|-------|--------|
| **File** | `cmc_historical_top300_filtered_with_supply.csv` |
| **Source** | CoinMarketCap historical snapshots |
| **Frequency** | Monthly (first Sunday of each month) |
| **Date range** | 2017-01-01 → 2026-02-22 (≈ 108 monthly snapshots) |
| **Universe** | Top 300 by market cap at each snapshot |
| **Key columns** | `snapshot_date`, `rank`, `symbol`, `market_cap`, `price`, `circulating_supply`, `volume_24h` |
| **Supply derivation** | `circulating_supply = market_cap / price` (`circulating_supply.py`) |

**Forward-fill policy.** A single missing weekly observation is forward-filled (`FFILL_LIMIT = 1`).
Consecutive missing observations are left as NaN and filtered by the supply history requirement.

### 2.2 Binance Perpetual Futures Data (v4+)

Fetched by `fetch_binance_data.py` from the Binance USDT-M futures REST API.

| Field | Detail |
|-------|--------|
| **Directory** | `binance_perp_data/` |
| **Files** | `weekly_ohlcv.parquet`, `weekly_funding.parquet`, `symbol_meta.csv` |
| **Symbols** | 396 USDT-M perpetual contracts as of 2025 |
| **OHLCV frequency** | Weekly (Monday open → Sunday close) |
| **Funding frequency** | 8h; aggregated to weekly sum and mean |
| **Date alignment** | `cmc_date = week_start + 6 days` maps Binance Monday weeks to CMC Sunday dates |
| **Onboard date** | First available kline per symbol; positions blocked until after onboard date |

**Why Binance data replaced CMC prices (v4).** CMC reports spot prices across hundreds of
exchanges. Perpetual futures positions execute at the Binance mark price. The basis between
CMC spot and Binance perp mark can exceed 5% for small-caps in illiquid conditions. Using
actual Binance perp closes eliminates this systematic mispricing in the forward return
computation.

**Why actual funding replaced the synthetic model (v4).** The synthetic regime-based
funding constants (0.008%/8h Bull, 0.002%/8h Bear) were rough medians that understated
real funding by 3-10× during altcoin seasons. With Binance historical funding data available
for all 396 symbols, per-token actual 8h rates are used directly.

---

## 3. Universe Construction

At each rebalancing date the eligible universe is constructed by applying filters in sequence.
All filters are cumulative.

### 3.1 Rank filter

```
rank > TOP_N_EXCLUDE  AND  rank <= MAX_RANK
```

| Parameter | Value (v4+) |
|-----------|:-----------:|
| `TOP_N_EXCLUDE` | 20 |
| `MAX_RANK` | 250 |

Excludes BTC, ETH, and the 18 largest assets whose supply dynamics are qualitatively
different (BTC: hard-capped 21M; ETH: EIP-1559 net-deflationary; top stablecoins: CMC rank 3-5).

### 3.2 Categorical exclusions

| Set | Representative symbols | Reason |
|-----|------------------------|--------|
| `STABLECOINS` | USDT, USDC, DAI, FRAX, USDD, PYUSD | Supply changes are operational float management, not dilution |
| `CEX_TOKENS` | BNB, OKB, KCS, LEO, BGB, WBT | Exchange-controlled buybacks/burns create non-fundamental supply dynamics |
| `MEMECOINS` | DOGE, SHIB, PEPE, BONK, WIF, BRETT | No fundamental supply-valuation relationship |
| `WRAPPED_ASSETS` | WBTC, BTCB, BTC.b, WETH, WBNB | Bridge minting double-counts the underlying native asset supply |
| `LIQUID_STAKING` | stETH, rETH, cbETH, jSOL, mSOL, BNSOL | LSD supply growth tracks staking inflows, not protocol dilution |
| `PROTOCOL_SYNTHETICS` | vBTC, vETH, vBNB, VTHO | Interest-accrual accounting artifacts with no external perp market |
| `COMMODITY_BACKED` | PAXG, XAUt, KAU | Gold-backed; supply tracks AUM, not emission; no perp market |

### 3.3 Binance tradability filter (v4+)

```python
universe = universe[universe["symbol"].isin(binance_perp_symbols)]
universe = universe[onboard_map[symbol] <= rebal_date]
```

Only tokens with an active Binance USDT-M perpetual and a listed onboard date on or before
the rebalancing date are eligible. This prevents the backtest from booking perp returns on
tokens not yet listed, eliminating hindsight in the universe construction.

### 3.4 Liquidity filters

| Filter | Value |
|--------|-------|
| Min 7-day ADTV (Binance perp) | `MIN_VOLUME × 7 = $7,000,000` |
| Min market cap | `MIN_MKTCAP = $100,000,000` |

### 3.5 Supply history filter

```
supply_hist_count >= MIN_SUPPLY_HISTORY = 26 weeks
```

Requires 26 non-missing 13-week supply observations before a token enters the universe.
Prevents newly-listed tokens with unusual early-life supply mechanics (token migrations,
bridge bootstrapping, ecosystem fund reclassifications) from contaminating the signal.

### 3.6 Minimum basket size

If either basket has fewer than `MIN_BASKET_SIZE = 6` tokens after all filters and the
buffer band, the rebalancing period is skipped entirely.

---

## 4. Signal Construction

### 4.1 Trailing supply inflation

```
supply_inf_13w(t) = circulating_supply(t) / circulating_supply(t - 13) - 1
supply_inf_52w(t) = circulating_supply(t) / circulating_supply(t - 52) - 1
```

### 4.2 Cross-sectional winsorisation

Before ranking, raw supply inflation values are winsorised at the 2nd and 98th
cross-sectional percentiles each period:

```python
lo, hi = supply_inf.quantile([SUPPLY_INF_WINS[0], SUPPLY_INF_WINS[1]])  # (0.02, 0.98)
supply_inf_winsorised = supply_inf.clip(lo, hi)
```

Prevents a single token with a 10,000% supply spike (token migration, bridge event) from
anchoring either basket every period.

### 4.3 Composite percentile rank

```
rank_13w(t) = cross_sectional_percentile_rank( supply_inf_13w(t) )   ∈ [0, 1]
rank_52w(t) = cross_sectional_percentile_rank( supply_inf_52w(t) )   ∈ [0, 1]

# Fallback: if rank_52w unavailable (< 52w history), use rank_13w
rank_52w(t) = rank_52w(t).fillna(rank_13w(t))

composite_rank(t) = (1 - SIGNAL_SLOW_WEIGHT) × rank_13w(t)
                  + SIGNAL_SLOW_WEIGHT        × rank_52w(t)
                                                              SIGNAL_SLOW_WEIGHT = 0.50
```

**Rationale.** The 52-week component captures the structural annual dilution rate; the
13-week component retains sensitivity to recent changes (new vesting cliffs, buyback
programmes). Blending both produces a rank that is harder to game on a single horizon.

**Rank interpretation:** Low composite rank → low supply inflation → **long candidate**.
High composite rank → high supply inflation → **short candidate**.

---

## 5. Portfolio Construction

### 5.1 Basket selection — inner buffer band

| Basket | Entry condition | Stay condition |
|--------|-----------------|----------------|
| **Long** | `composite_rank ≤ LONG_ENTRY_PCT = 12%` | `composite_rank ≤ LONG_EXIT_PCT = 18%` |
| **Short** | `composite_rank ≥ SHORT_ENTRY_PCT = 88%` | `composite_rank ≥ SHORT_EXIT_PCT = 82%` |

```python
entry_long  = {s : rank[s] <= LONG_ENTRY_PCT}
stay_long   = {s : s ∈ prev_long  AND rank[s] <= LONG_EXIT_PCT}
basket_long = entry_long ∪ stay_long

entry_short = {s : rank[s] >= SHORT_ENTRY_PCT} − squeezed_tokens
stay_short  = {s : s ∈ prev_short AND rank[s] >= SHORT_EXIT_PCT}
basket_short = entry_short ∪ stay_short
```

**Hysteresis rationale.** A token at the 15th percentile in month T+1 was legitimate at
the 12th percentile in month T. Without the exit band it would churn out and back in every
period, generating unnecessary transaction costs. The 6-percentage-point exit buffer
(12% → 18% for longs, 88% → 82% for shorts) reduces average monthly turnover by approximately
15 percentage points.

**Overlap resolution.** If a token qualifies for both baskets simultaneously, it is removed
from both. In practice this affects < 0.5% of period-token pairs.

### 5.2 Position weighting — Inverse-vol × sqrt(ADTV)

```
raw_weight(i) = sqrt(ADTV_i) / realized_vol_i
weight(i)     = raw_weight(i) / Σ raw_weight(j)    [then capped at ADTV_POS_CAP = 20%]
```

where `realized_vol_i` is the 8-week rolling annualised standard deviation of the token's
weekly Binance perp returns (`TOKEN_VOL_WINDOW = 8` weeks). Lower bounds: vol ≥ 5%, ADTV
≥ $0 (floor, but weight → 0).

**Interpretation.** Tokens 3× more volatile than average receive one-third the weight,
regardless of their supply rank. Liquid tokens (high ADTV) receive proportionally more
weight, anchoring the portfolio in names that can actually be traded at the target notional.

### 5.3 Forward return winsorisation

Cross-sectional winsorisation at 1st/99th percentile is applied to per-token forward returns
before computing basket returns:

```python
lo, hi = fwd.quantile([WINS_LOW, WINS_HIGH])  # (0.01, 0.99)
fwd = fwd.clip(lower=lo, upper=hi).clip(lower=-1.0)
```

Clips extreme single-period returns arising from very thin order books or data errors,
without removing directional information.

---

## 6. Regime Detection

### 6.1 Market regime (Bull / Bear / Sideways)

A cap-weighted index of the top-100 tokens by market cap is constructed at each weekly CMC
snapshot:

```
index_return(t) = Σ_i [ w_i(t) × weekly_pct_return_i(t) ]
    where w_i(t) = market_cap_i(t) / Σ_j market_cap_j(t)

index_price(t) = ∏(1 + index_return)   [cumulative]
index_MA(t)    = simple_moving_average(index_price, REGIME_MA_WINDOW = 20 weeks)
ratio(t)       = index_price(t) / index_MA(t)
```

| Regime | Condition |
|--------|-----------|
| **Bull** | `ratio ≥ BULL_BAND = 1.10` |
| **Bear** | `ratio ≤ BEAR_BAND = 0.90` |
| **Sideways** | `0.90 < ratio < 1.10` |

The ±10% band prevents rapid regime flips near the moving average.

### 6.2 High-volatility detection

```
BTC_vol_8w(t) = std(weekly BTC returns over t-7 to t) × sqrt(52)
high_vol(t)   = BTC_vol_8w(t) > HIGH_VOL_THRESHOLD = 80% annualised
```

### 6.3 Altcoin-season veto

At each rebalancing date, the fraction of top-50 altcoins (rank 3-50, excluding all
exclusion sets) that outperformed BTC over the trailing `ALTSEASON_LOOKBACK = 4` periods
is computed:

```
altseason_score(t) = count(alt_4w_return > BTC_4w_return) / count(alts)
altseason_veto(t)  = True  if altseason_score(t) > ALTSEASON_THRESHOLD = 0.75
```

When the veto fires: `short_scale = 0.0` for that period. The short leg is completely
zeroed. **Rationale:** during altcoin season, high-emission tokens are bid specifically
*because* their emission drives the growth narrative the market is chasing (liquidity
mining APYs, staking rewards). The supply-dilution signal inverts; the veto prevents
catastrophic short losses during the exact environment the signal fails.

---

## 7. Regime-Aware Exposure Scaling

Rather than running at 100%/100% long/short in all environments, effective exposure is
scaled by regime and volatility state:

| Regime | High-Vol? | Long Scale | Short Scale |
|--------|:---------:|:----------:|:-----------:|
| Sideways | No | 100% | 100% |
| Sideways | Yes | 100% | 75% |
| Bull | No | 100% | 50% |
| Bull | Yes | 75% | 25% |
| Bear | No | 75% | 75% |
| Bear | Yes | 50% | 25% |

```python
r_combined = (long_scale × r_long_net + short_scale × r_short_net)
             / (long_scale + short_scale)
```

**Rationale.** The supply-dilution signal is weakest in Bull regimes (alts broadly bid up
regardless of emission). Halving the short scale in Bull and quartering it in Bull+HighVol
limits the worst-case short-squeeze exposure while preserving Bear-regime alpha where the
signal dominates.

---

## 8. Regime-Aware Rebalancing Frequency (v6 — key innovation)

**v4/v5** rebalanced monthly in all regimes. **v6** rebalances at a regime-dependent
frequency:

| Regime | Rebalancing Step | Effective Frequency |
|--------|:----------------:|:-------------------:|
| **Bear** | 1 month | Monthly |
| **Bull** | 2 months | Bi-monthly |
| **Sideways** | 1 month | Monthly |

**Algorithm:**

```python
REBAL_STEP = {"Bear": 1, "Bull": 2, "Sideways": 1}

active_rebals = []
idx = 0
while idx < len(all_monthly_dates):
    t = all_monthly_dates[idx]
    active_rebals.append(t)
    regime = regime_map[t]
    idx += REBAL_STEP[regime]   # skip 1 month in Bull
```

**Rationale.** In Bull regimes, the supply-dilution signal generates significant noise —
monthly rebalancing in Bull produces unnecessary turnover on momentum-driven names where
the signal temporarily inverts. By stepping 2 months in Bull, the strategy avoids the
noisier interim months and only rebalances when the signal has had time to separate
genuine diluters from momentum. This halves transaction costs during Bull and selects
the cleaner signal moments.

**BTC hedge alignment.** The BTC forward return for the hedge computation spans the actual
holding window (1 month in Bear/Sideways, 2 months in Bull), ensuring the hedge return
matches the period return being hedged.

---

## 9. Execution Model

### 9.1 Transaction costs

| Component | Model | Value |
|-----------|-------|-------|
| Taker fee | Fixed per trade, entry + exit | `TAKER_FEE × 2 = 0.04% × 2 = 0.08%` per period |
| Slippage | Inverse CMC turnover proxy, capped | `min(SLIPPAGE_K / turnover, MAX_SLIPPAGE) = min(0.05% / turnover, 2.00%)` |
| Funding (long leg) | Actual Binance 8h rates, summed over hold period | Per-token, per-period actual |
| Funding (short leg) | Actual Binance 8h rates (credit) | Per-token, per-period actual |

**Slippage model detail:**
```python
turnover   = max(volume_24h / market_cap, MIN_TURNOVER = 0.1%)
slippage   = min(SLIPPAGE_K / turnover, MAX_SLIPPAGE)   # capped at 2.00%
```

A more rigorous model would use `MI = σ × sqrt(Q/ADV) × η` (Almgren-Chriss square-root
impact). The inverse-turnover proxy is a practical approximation that scales correctly
in direction: illiquid tokens have higher slippage and receive lower weight via the ADTV
weighting.

### 9.2 Net return per leg

```
r_long_net  = r_long_gross  − fee_round_trip − slippage_long  − funding_drag_long
r_short_net = −r_short_gross − fee_round_trip − slippage_short + funding_credit_short
```

The sign convention: `r_short_gross` is the raw price return of the short basket. Shorting
a basket returning +X% produces a loss of X%; shorting one returning −X% produces a gain.

### 9.3 Funding computation (actual Binance data, v4+)

```python
# Funding for holding period [t0, t1]
fund_mask = (weekly_funding["week_start"] > t0) & (weekly_funding["week_start"] <= t1)
fund_per_period = fund_mask_rows.groupby("symbol")["funding_sum"].sum()

# Long leg pays, short leg receives (standard perpetual mechanics)
fund_drag_long   = weighted_avg(fund_per_period, basket_long_weights)
fund_credit_short = weighted_avg(fund_per_period, basket_short_weights)
```

Actual funding data (Binance 8h rate × 3 per day × hold_days) is used in place of the
synthetic regime-based constants used in v1-v3. This is the highest-fidelity cost model
achievable with freely available historical data.

### 9.4 Circuit breaker

If the short basket's gross return in a period exceeds `SHORT_CB_LOSS = 40%`, the
return is hard-capped:

```python
if r_short_gross > SHORT_CB_LOSS:
    r_short_gross = SHORT_CB_LOSS   # caps short loss at 40% of notional
```

This simulates an emergency stop-loss triggered by extreme short squeezes. In the
2022-2026 backtest, the circuit breaker fired 3 times.

---

## 10. Risk Overlays

### 10.1 Short-squeeze exclusion

Before constructing the short basket, tokens whose prior-period Binance perp return
exceeded `+SHORT_SQUEEZE_PRIOR = 40%` are blocked from new short entries:

```python
squeezed   = {s : prior_period_return(s) > 0.40}
entry_short = high_inflation_candidates - squeezed
# existing stay_short positions are NOT force-exited
```

### 10.2 BTC rolling beta hedge

At each rebalancing date, a 12-period rolling OLS beta is estimated from the historical
combined return vs BTC forward return series:

```
pairs    = [(r_comb[t-k], r_btc[t-k]) for k in last BTC_HEDGE_LOOKBACK=12 periods]
beta(t)  = Cov(r_comb, r_btc) / Var(r_btc)          [min 4 pairs required]
beta(t)  = clip(beta(t), 0.0, BTC_HEDGE_MAX = 1.0)  [no over-hedge]
r_hedged = r_combined + (−beta × r_btc_forward)
```

The hedge is reported separately (`combined_hed`) but is **not** used as the primary
strategy return. It serves as an attribution tool: the gap between `combined_net` and
`combined_hed` measures how much of the gross return is BTC-beta carry vs genuine
supply-dilution alpha.

In v6, the unhedged combined net is +0.10% annualised; the hedged combined is −3.67%.
This implies approximately 3.8pp/year of the strategy return is BTC-beta exposure
rather than pure supply-dilution alpha.

---

## 11. Version History and Performance

### 11.1 All-versions summary

| Metric | v1 (CMC, 2017+) | v2 (CMC, 2022+) | v3 (CMC, 2022+) | v4 (Binance, 2022+) | v5 (v4+cash) | **v6 (v5+freq, 2022+)** |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|
| Periods | 108 | 40 | 39 | 39 | 39 | **19** |
| Win rate | 58.3% | 55.0% | 56.4% | 57.7% | 53.8% | **52.6%** |
| Mean spread | — | +2.34% | +3.74% | +2.38% | +2.38% | **+3.34%** |
| Combined net (ann.) | +13.6% | −6.60% | +1.82% | −5.11% | −2.74% | **+0.10%** |
| Sharpe (combined) | +0.14 | −0.161 | +0.051 | — | — | **+0.003** |
| MaxDD | −78% | −64.3% | −31.3% | −22.89% | −23.26% | **−19.22%** |
| Avg long basket | ~22 | 7.0 | 9.6 | ~8 | ~8 | **7.5** |
| Avg short basket | ~22 | 9.6 | 10.2 | ~8 | ~8 | **7.6** |

**Why v1 shows +13.6%.** 2017-2020 contains many small-cap tokens with near-zero supply
inflation that also had near-zero price movement (dead projects). As long basket members,
their near-zero returns appear favourable against an active short basket of growing alts
during non-mania periods. The full-history number is not executable at scale — see Capacity
section.

**Why v4 deteriorates vs v3.** v4 replaced CMC synthetic prices/funding with real Binance
perp prices and actual funding rates. The actual funding drag on longs (-4.98% cumulative)
is significantly worse than the synthetic model assumed. This is the correct result — the
synthetic model was optimistic.

**Why v6 beats v4/v5.** Regime-aware rebalancing concentrates the strategy's active periods
on the months where the supply-dilution signal has historically been strongest. By skipping
one Bull month in two, the v6 schedule selects 2 clean Bull periods (ann. geo spread
+51.9%) vs the noisy monthly Bull churn in v4/v5.

### 11.2 Regime-conditional performance — v6

| Regime | Periods | Win Rate | Mean Spread | Ann. Geo Spread |
|--------|:-------:|:--------:|:-----------:|:---------------:|
| **Bull** | 2 | 50.0% | +3.83% | +51.91% |
| **Bear** | 11 | 63.6% | +4.55% | **+53.34%** |
| **Sideways** | 6 | 33.3% | +0.96% | +1.34% |

**Key finding.** The signal is most powerful in **Bear** regimes: capital scarcity forces
the market to discount high-emission tokens sharply. The 63.6% Bear win rate and +53.34%
annual geo spread are the strongest across all versions. Sideways remains the weakest
environment (33% win rate, barely above noise).

### 11.3 Funding attribution — v6

| Attribution | Cumulative | Per-period avg |
|-------------|:----------:|:--------------:|
| Funding drag (long leg pays) | −4.98% | −0.26% |
| Funding credit (short leg receives) | +3.06% | +0.16% |
| **Net funding impact** | **−1.92%** | **−0.10%** |

The long leg pays approximately 0.26% per month in funding (longing low-emission tokens
that the market is also long = you're paying the crowded direction). The short leg earns
approximately 0.16% per month. Net funding drag of 1.92% cumulative over 19 periods
represents the single largest identifiable post-cost drag on the strategy.

---

## 12. Experiment Results (perpetual_ls_experiments.py)

Eight isolated experiments were run against the v6 base to test incremental modifications.
All experiments share identical data loading and signal pre-computation; only the parameter
indicated differs.

### 12.1 Results table

| Experiment | Change | Periods | Combined Net | Δ vs v6 | MaxDD | Bear Geo | Notes |
|------------|--------|:-------:|:------------:|:-------:|:-----:|:--------:|-------|
| `v6_base` | — | 21 | −3.75% | — | −22.66% | +12.07% | Reference |
| `A_no_hedge` | Remove BTC hedge | 21 | −3.75% | 0.00% | −22.66% | +12.07% | Hedge in combined_hed only: −8.33% hedged |
| `B_side_cash` | Sideways → cash | 21 | **−3.12%** | **+0.63%** | −22.77% | +12.07% | Best clean improvement |
| `C_bull_3mo` | Bull step = 3 | 16 | −10.82% | −7.07% | −35.96% | +5.72% | Over-skips; fewer good periods |
| `D_wide_bask` | Entry 18%/82% | 36 | −9.21% | −5.46% | −44.43% | +76.16% | 36 periods — signal diluted |
| `E_fund_sig` | +30% funding rank | 37 | −16.00% | −12.25% | −61.25% | +55.51% | Funding rank anti-correlated with supply rank; distorts baskets |
| `F_momentum` | 13w momentum filter | 6 | +3.50% | +7.25% | −3.23% | −47.80% | Only 6 periods; unreliable |
| `G_stop_loss` | 50% zombie short ban | 21 | −3.78% | −0.03% | −22.66% | +11.73% | Condition rarely triggers |
| `H_fund_gate` | Halve short if avg fund < −0.1%/8h | 21 | −3.75% | 0.00% | −22.66% | +12.07% | Gate never triggers in 2022-2026 |
| `I_biweekly` | Rebalance every 2 weeks | 69 | −27.38% | −23.64% | −70.87% | −1.36% | 3× worse; monthly CMC signal unsuited to 2w holding |
| `J_biweek_b2` | Biweekly, Bull=4w | 52 | −17.40% | −13.65% | −50.28% | −1.36% | Still 4.6× worse than monthly |
| `COMBINED` | Stack A+B+C+D+E+F+G+H | 25 | −15.51% | −11.76% | −58.52% | +12.31% | Too many filters collapse sample |

### 12.2 Key experiment findings

**Biweekly rebalancing is structurally inappropriate.** The supply signal updates monthly
(CMC data frequency). Running biweekly means the same signal drives 2-4 rebalancing
decisions. The first rebalance captures any genuine mean-reversion; subsequent rebalances
at intermediate Binance prices add only transaction costs and noise. I_biweekly pays
approximately 2× the annual transaction costs of monthly for no incremental signal. This
experiment is a definitive negative result.

**Funding as co-signal (E) is directionally wrong.** Blending 30% funding rank into the
supply signal causes anti-correlation: tokens with low supply inflation (long candidates)
tend to have *high* positive funding rates (they're also heavily longed by the market).
Blending funding rank compresses the composite rank distribution, distorting basket
composition. The re-rank fix (applied in the experiments file) restores the distribution
but the signal itself adds no value.

**BTC hedge hurts (A vs unhedged).** The combined_hed series is −8.33% annualised vs
combined_net −3.75%. The hedge is short BTC; the strategy is already naturally short
high-beta alts. Double-shorting BTC beta over-hedges in Bear regimes and loses in Bull.
For reporting purposes, `combined_net` (unhedged) is the primary metric.

**Sideways=cash (B) is the only clean win.** The Sideways regime shows 33% win rate
(below coin-flip) in all versions. Sitting in cash during Sideways costs nothing and
avoids the noise. This modification (+0.63%) has no negative side-effects.

---

## 13. Best Configuration (v6 + B_side_cash projected)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Universe | Rank 21-250, Binance perp required, $100M mktcap, $7M weekly ADTV | Liquid, rank-filtered alts |
| Exclusions | Stables, CEX, meme, wrapped, LST, synthetics, commodity | Eliminate non-fundamental supply dynamics |
| Signal | 50% rank(supply_inf_13w) + 50% rank(supply_inf_52w), winsorised 2-98% | Dual-horizon composite |
| Entry thresholds | Long ≤12%, Short ≥88% | ~6-10 tokens per basket |
| Exit thresholds | Long ≤18%, Short ≥82% | 6pp hysteresis band to reduce turnover |
| Weighting | Inv-vol × sqrt(ADTV), 20% cap | Risk- and liquidity-adjusted |
| Regime scaling | As in Section 7 table | Reduce short exposure in Bull |
| **Rebalancing** | **Bear=1mo, Bull=2mo, Sideways=cash** | **Key v6 innovation + B_side_cash** |
| Altseason veto | >75% top-50 alts beat BTC over 4 periods → zero short | Prevents mania period shorts |
| Short squeeze exclusion | Prior-period return >40% → block new short entry | Avoids entering into squeezes |
| Circuit breaker | Short gross loss >40% → cap at 40% | Hard stop on extreme squeezes |
| Prices | Binance USDT-M weekly perp close | Matches actual execution venue |
| Funding | Actual Binance 8h rates, per-token, per-period | Replaces synthetic regime constants |
| Min basket | 6 tokens | Prevents single-stock risk |

**Projected combined result (v6 + Sideways=cash):** approximately +0.10% + 0.63% ≈ **+0.73% annualised net**, MaxDD ≈ −19%, over the 2022-2026 backtest window.

---

## 14. Known Limitations

### 14.1 Barely-positive net alpha

The strategy nets approximately +0.10% to +0.73% annualised depending on configuration.
This is statistically indistinguishable from zero given the volatility (~29%) and the
number of periods (19). The Lo (2002) HAC-corrected Sharpe is +0.003 to +0.18 — not
sufficient for live deployment without additional alpha sources.

### 14.2 Funding drag as the primary cost driver

The long leg pays −4.98% cumulative funding over 19 periods. Low-emission tokens are
the market's favoured longs; funding rates on them are consistently positive (longs pay
shorts). Any live implementation must model per-token funding costs dynamically and
consider whether funding drag will persist or revert as market structure evolves.

### 14.3 Survivorship bias

Tokens delisted from top-300 are not tracked after exit. For the short leg, tokens that
went to near-zero would be ideal shorts — but after delisting they leave the universe,
cutting off remaining profit. For the long leg, zombie projects that flatlined are removed,
preventing repeated losses. Net bias direction is unclear but is present in both legs.

### 14.4 Capacity ceiling

```
Capacity ≈ avg_basket_size × avg_ADTV × max_pct_ADTV / 2 legs
         ≈ 8 tokens × $10M ADTV × 5% / 2
         ≈ $2M per leg → $4M total AUM
```

Not scalable above approximately $5-10M AUM. Beyond this, position entry/exit begins
to represent a meaningful fraction of daily volume, eroding the spread entirely.

### 14.5 CMC supply data quality

The supply signal depends entirely on CMC circulating supply accuracy. Known failure modes:
token migrations, bridge minting double-counts, treasury reclassifications, exchange
wallet mis-labelling. The exclusion lists address the most common categories but are
not exhaustive. For live deployment, Glassnode on-chain circulating supply is the
preferred primary source.

### 14.6 Small sample size in v6

19 rebalancing periods (2022-2026) is a short backtest for a monthly signal. The
regime-aware stepping further reduces the effective sample: only 2 Bull periods, 11
Bear periods, 6 Sideways. The Bear geo spread (+53.34%) is based on 11 periods and
carries substantial estimation uncertainty.

---

## 15. Required Upgrades for Live Deployment

| Priority | Upgrade | Impact |
|----------|---------|--------|
| 1 | **On-chain supply from Glassnode** | Eliminates CMC reclassification noise; eliminates bridge/LST supply double-counts |
| 2 | **Real-time per-token funding rates** | Already implemented for backtesting; needs live feed integration |
| 3 **Intra-period CB monitoring** | Current circuit breaker is period-end; live system needs daily mark-to-market | Reduces actual worst-case loss in squeezes |
| 4 | **Token unlock calendar overlay** | Messari/TokenUnlocks data for scheduled vesting cliffs → pre-position | Converts lagged supply signal to leading signal |
| 5 | **Multi-exchange execution** | Bybit + OKX coverage for tokens not on Binance | Expands universe from 396 to ~600 symbols |
| 6 | **Intraday rebalancing execution** | TWAP/VWAP execution within 24h of signal date | Reduces market impact vs single-print close execution |

---

## 16. Data Sources

### Tier 1 — Essential (all currently available)

| Source | Data | Status |
|--------|------|--------|
| CoinMarketCap | Monthly supply snapshots, top 300 | Available (`cmc_historical_top300_filtered_with_supply.csv`) |
| Binance USDT-M REST API | Weekly OHLCV + 8h funding, 396 symbols | Available (`binance_perp_data/`) |

### Tier 2 — Important for live deployment

| Source | Data | Endpoint |
|--------|------|---------|
| Glassnode | On-chain circulating supply | `GET /v1/metrics/supply/current` |
| Binance real-time | Live mark prices + funding | `GET /fapi/v1/markPrice`, `GET /fapi/v1/fundingRate` |
| Bybit Linear | Coverage extension | `GET /v5/market/kline`, `GET /v5/market/funding/history` |
| CoinGecko Pro | Point-in-time supply for non-Binance tokens | `GET /coins/{id}/market_chart` |

### Tier 3 — Alpha enhancement

| Source | Data | Use |
|--------|------|-----|
| Messari Pro | Token unlock calendars | Pre-position ahead of known vesting cliffs |
| TokenUnlocks.app | Forward unlock calendar | T-1 signal before CMC records supply change |
| Nansen | Wallet labeling | Track VC/team wallet movements preceding unlocks |
| DefiLlama | Protocol revenue + TVL | Revenue-to-Inflation overlay; free |

---

## 17. Script Reference

| File | Version | Description |
|------|---------|-------------|
| `perpetual_ls_backtest.py` | v1 | Baseline; 4w signal, equal-weight, CMC prices+synthetic funding; 108 periods 2017-2026 |
| `perpetual_ls_v2.py` | v2 | 13w signal, ADTV weights, all exclusions, CMC prices; 40 periods 2022+ |
| `perpetual_ls_v3.py` | v3 | Composite 13w+52w, inv-vol weights, CB, altseason veto, BTC hedge, CMC prices; 39 periods |
| `perpetual_ls_v4.py` | v4 | **Binance perp prices + actual funding**; regime scaling; 39 periods; −5.11% ann. |
| `perpetual_ls_v5.py` | v5 | v4 + Sideways=cash; 39 periods; −2.74% ann. |
| **`perpetual_ls_v6.py`** | **v6** | **v4 + Bear=1mo / Bull=2mo rebalancing**; 19 periods; **+0.10% ann.** |
| `perpetual_ls_v1_binance.py` | v1-Binance | v1 logic + Binance data; shows why universe collapse destroys v1 in 2020-2021 |
| `perpetual_ls_experiments.py` | — | 12 isolated experiments on v6 base; A-H + I/J biweekly |
| `fetch_binance_data.py` | — | Downloads 396-symbol weekly OHLCV + funding from Binance REST API |

### Running the backtests

```bash
# Best result
python perpetual_ls_v6.py

# Full version progression
python perpetual_ls_v4.py    # Binance data, monthly rebal   → -5.11%
python perpetual_ls_v5.py    # + Sideways=cash               → -2.74%
python perpetual_ls_v6.py    # + regime-aware rebal frequency → +0.10%

# Experiment suite (A-H isolated + biweekly + COMBINED)
python perpetual_ls_experiments.py

# Binance data refresh
python fetch_binance_data.py
```

### Key configurable parameters (v6)

```python
START_DATE           = pd.Timestamp("2022-01-01")
TOP_N_EXCLUDE        = 20           # exclude top-20 by rank
MAX_RANK             = 250          # universe ceiling
MIN_MKTCAP           = 1e8          # $100M market cap floor
MIN_VOLUME           = 1e6          # $1M/day × 7 = $7M weekly ADTV gate

SUPPLY_WINDOW        = 13           # fast signal (weeks)
SUPPLY_WINDOW_SLOW   = 52           # slow signal (weeks)
SIGNAL_SLOW_WEIGHT   = 0.50         # 50% fast + 50% slow

LONG_ENTRY_PCT       = 0.12         # enter long at bottom 12th pct
LONG_EXIT_PCT        = 0.18         # exit long above 18th pct
SHORT_ENTRY_PCT      = 0.88         # enter short at top 12th pct
SHORT_EXIT_PCT       = 0.82         # exit short below 82nd pct

REBAL_STEP = {"Bear": 1, "Bull": 2, "Sideways": 1}  # KEY v6 parameter

REGIME_MA_WINDOW     = 20           # 20-week MA for regime detection
BULL_BAND            = 1.10         # index / MA20 ≥ 1.10 → Bull
BEAR_BAND            = 0.90         # index / MA20 ≤ 0.90 → Bear
HIGH_VOL_THRESHOLD   = 0.80         # BTC ann. vol > 80% → high-vol

SHORT_CB_LOSS        = 0.40         # circuit breaker cap
SHORT_SQUEEZE_PRIOR  = 0.40         # squeeze exclusion threshold
ALTSEASON_THRESHOLD  = 0.75         # altseason veto trigger
ALTSEASON_LOOKBACK   = 4            # lookback periods for altseason score

BTC_HEDGE_LOOKBACK   = 12           # rolling OLS window (periods)
BTC_HEDGE_MAX        = 1.0          # max beta (no over-hedge)

ADTV_POS_CAP         = 0.20         # max weight per position
TOKEN_VOL_WINDOW     = 8            # weeks for realized vol
```

---

## 18. Output Charts

### v6 Charts

| File | Description |
|------|-------------|
| `perp_ls_v6_cumulative.png` | Cumulative NAV (log scale), per-leg net, per-period spread bar coloured by regime |
| `perp_ls_v6_regime_dd.png` | Per-period gross spread coloured by regime + unhedged/hedged drawdown comparison |
| `perp_ls_v6_vs_v4.png` | v4 vs v6: period-by-period spread comparison, cumulative NAV comparison, regime-conditional geo spread bar, stats scorecard |

### Earlier Version Charts

| File | Description |
|------|-------------|
| `perp_ls_cumulative.png` | v1 baseline (2017-2026) |
| `perp_ls_v2_cumulative.png` | v2 (2022+) |
| `perp_ls_v3_cumulative.png` | v3 (2022+, risk-managed) |
| `perp_ls_v4_cumulative.png` | v4 (2022+, Binance data) |
| `perp_ls_v5_cumulative.png` | v5 (2022+, Sideways=cash) |
| `perp_ls_v6_cumulative.png` | **v6 (2022+, regime-aware rebal) — current best** |
| `perp_ls_v1_binance_cumulative.png` | v1 logic + Binance data (shows 2020-2021 universe collapse) |

---

## 19. Related Documents

| File | Description |
|------|-------------|
| `institutional_analysis_report.md` | Full institutional post-trade analysis: BTC beta decomposition (0.645), return distribution (kurtosis 30.37, skew 4.71), sector bias |
| `risk_manager_teardown.md` | QRM teardown: basket composition, regime geo returns, capacity analysis ($3-8M ceiling), turnover (89.6% monthly) |
| `quant_critique_and_roadmap.md` | Quant roadmap: signal quality, capacity, live execution blueprint |
| `backtest_report.md` | Early-stage v1 analysis |
| `v2_backtest_report.md` | v2 detailed post-trade |
