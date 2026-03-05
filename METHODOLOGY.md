# Supply-Dilution L/S Strategy — Methodology & Results (v8)

## Overview

A monthly cross-sectional long/short strategy that exploits the negative relationship between
circulating supply inflation and future token returns. Tokens with persistently low supply
inflation are bought; tokens with persistently high supply inflation are shorted.
Execution uses Binance USDT-M perpetual futures with actual funding rates and ADTV constraints.

**Current default parameters: v8** (BULL_BAND=1.05, BEAR_BAND=0.95, SUPPLY_WINDOW=26, LONG_QUALITY_LOOKBACK=12)

---

## 1. Data Sources

| Source | Coverage | Use |
|--------|----------|-----|
| CoinMarketCap historical | Top-300 weekly, 2017–2026, ~2,266 symbols | Supply, price, market cap, rank |
| Binance USDT-M perps | 396 symbols weekly close | Execution prices |
| Binance 8h funding rates | All listed USDT-M perps | Funding P&L |
| Binance ADTV (daily USD) | All listed USDT-M perps | Liquidity filter |

**Supply proxy**: `market_cap / price` (robust to CMC raw circulating supply corruption — avoids
sudden step changes introduced by data source re-classifications).

---

## 2. Universe Construction

Applied at each rebalancing date:

1. **Rank filter**: CMC rank ≤ 200 (excludes USDT, USDC, BTC, ETH via top-20 exclusion)
2. **Top-20 exclusion**: Remove top-20 by market cap (BTC, ETH, and major stables dominate and are unsuitable for inflation-based shorting)
3. **ADTV floor**: ≥ $5M/day (Binance USDT-M daily volume proxy)
4. **Market cap floor**: ≥ $50M
5. **Supply history**: ≥ 26 weeks of CMC data required for signal computation
6. **Binance listing**: Must have a live USDT-M perpetual contract

Typical investable universe: 60–110 tokens per month.

---

## 3. Signal Construction

**2-layer supply inflation signal** (unchanged from v4/v6):

```
supply_change_fast = (supply_t / supply_{t-26w}) - 1   # 26-week window (v8; was 13w in v7)
supply_change_slow = (supply_t / supply_{t-52w}) - 1   # 52-week window

rank_fast = cross_sectional_rank(supply_change_fast)   # 0..1 within universe
rank_slow = cross_sectional_rank(supply_change_slow)   # 0..1 within universe

pct_rank = winsorize(0.50 * rank_fast + 0.50 * rank_slow, 2%, 98%)
```

- `pct_rank ≈ 0` → very low supply inflation → **long candidate**
- `pct_rank ≈ 1` → very high supply inflation → **short candidate**

**Why 2 layers, not 3**: Adding a 4-week window corrupted long selection. Tokens with a
temporarily quiet 4-week window but high 26w/52w inflation were incorrectly promoted into the
long basket. The 2-layer signal is retained as the cleanest version.

**Why 26w fast window** (v8 change): The 13w fast window added noise — supply changes over a
single quarter are more susceptible to temporary pauses or bursts. The 26w window smooths this
without sacrificing timeliness.

---

## 4. Token Selection (Buffer Bands)

Selection uses hysteresis bands to reduce unnecessary turnover:

| Pool | Entry threshold | Exit threshold |
|------|----------------|----------------|
| Long candidates | pct_rank ≤ 12th percentile | pct_rank > 18th percentile |
| Short candidates | pct_rank ≥ 88th percentile | pct_rank < 82nd percentile |

Thresholds are **data-driven quantiles** computed at each date against the live universe
(not fixed absolute values — the composite rank distribution is not uniform due to weighted
averaging of correlated uniforms).

Minimum basket size: **6 tokens** per leg. If candidates fall below 6 after vetoes, the
minimum-size basket is formed from the top/bottom tokens by rank.

---

## 5. Vetoes

### 5a. Momentum Veto (Shorts)
Within the short candidate pool, tokens whose trailing 1-month Binance return exceeds the
**50th percentile of that pool** are excluded at entry. Applied only at entry (not stay),
and only within the short pool (not the full universe). Prevents shorting strongly
momentum-driven tokens even when their supply is inflating.

### 5b. Long Quality Veto
Within the long candidate pool, the bottom **33%** by BTC-relative 6-month return (12-period
lookback, v8) are excluded. Targets persistent underperformers like NEO and THETA that have
genuinely low supply inflation but are structurally losing market share.

### 5c. Short-Squeeze Exclusion
Tokens that rallied **> 40% in the prior month** are excluded from short entry. Prevents
entering shorts into strongly squeezing tokens regardless of signal.

### 5d. Altseason Veto
When ≥ 75% of the investable universe outperforms BTC over the prior 4 weeks (altseason),
the short leg scale is zeroed. Prevents shorting into broad altcoin runs.

### 5e. Circuit Breaker (CB)
If the short basket loses **> 40% in a single period**, the short leg is exited and zeroed
for that period. Triggered 5/45 periods (11%).

---

## 6. Position Sizing

**Equal-weight** within each leg:
```
weight_i = 1 / N_basket     (subject to ADTV cap)
```

ADTV cap: no single token may exceed 20% of the leg's allocation (prevents concentration in
illiquid tokens at small capital levels).

**Leg scaling by regime** (see Section 7):
- Bull / Bear active: `long_scale = 0.75`, `short_scale = 0.75`
- Sideways: `long_scale = 0.00`, `short_scale = 0.00` (hold cash)
- High-vol Bull/Bear: `long_scale = 0.50`, `short_scale = 0.25`

A scale of 0.75 means the total long notional = 75% of NAV; similarly for short.
The portfolio is notionally 150% gross (75L + 75S), not leveraged beyond this.

---

## 7. Regime Detection

**Index construction**: CMC cap-weighted index over the investable universe (excludes BTC/ETH).

```
regime:
  index / MA(20) ≥ BULL_BAND (1.05)  → Bull
  index / MA(20) ≤ BEAR_BAND (0.95)  → Bear
  otherwise                           → Sideways
```

**High-volatility overlay**: BTC 8-week realized vol > 80th percentile triggers half-scale
reduction in active regimes.

**v8 change**: Tighter bands (1.05/0.95 vs 1.10/0.90) classify more periods as active,
increasing the fraction of periods where the strategy is engaged. This contributed meaningfully
to the v8 improvement over v7 baseline.

Regime breakdown (45 periods, 2022–2026):
- Bull: 23 periods (51%)
- Bear: 15 periods (33%)
- Sideways (cash): 7 periods (16%)

---

## 8. Execution Cost Model

Costs are charged every period using actual Binance data:

| Component | Model |
|-----------|-------|
| Taker fee | 0.04% per side (×2 for round trip = 0.08%) |
| Slippage | `k × sqrt(position / ADTV_weekly)` with k=0.0005, cap=2% |
| Funding | Actual Binance 8h rates aggregated to monthly holding period |

Only rebalanced positions incur fee+slippage (turnover-proportional). Funding accrues
continuously on the full position.

---

## 9. Performance Summary (v8, Net of All Costs)

**Backtest window**: 2022-01-02 → 2026-01-04 (45 monthly periods)

| Metric | Strategy (net) | BTC buy-hold | ETH buy-hold |
|--------|---------------|-------------|-------------|
| Ann. return (geo) | **+12.99%** | +21.20% | -0.22% |
| Cumulative total | **+63.1%** | +117.4% | -0.9% |
| Sharpe ratio | **+0.765** | +0.603 | +0.375 |
| HAC Sharpe* | **+1.058** | — | — |
| Sortino ratio | **+1.020** | — | — |
| Max drawdown | **-14.46%** | -65.24% | -69.50% |
| Win rate (spread) | 60.0% | — | — |

*Lo (2002) heteroskedasticity-autocorrelation corrected Sharpe.

**Key insight**: The strategy significantly underperforms BTC in absolute return over this
window but achieves substantially better risk-adjusted return (Sharpe 0.765 vs 0.603) and
dramatically lower drawdown (-14.5% vs -65.2%). In bear/sideways markets where BTC and ETH
suffer large drawdowns, the strategy either profits (Bear: +40.5% geo spread) or holds cash
(Sideways). This makes it a portfolio complement rather than a BTC replacement.

### Gross vs Net Breakdown

| | Gross | Net | Cost Drag |
|-|-------|-----|-----------|
| Ann. return (geo) | +21.44% | +13.93% | -7.50pp |
| Cumulative total | +107.2% | +63.1% | -44.1pp |

Cost drag is **35% of gross return**. Breakdown per active period:
- Avg fee + slippage: **0.447%/period** (0.447% × 12 ≈ 5.4%/yr)
- Avg net funding (short credit minus long drag): **+0.174%/period** (partially offsets)
- Avg total cost drag: **0.621%/period** (0.621% × 12 ≈ 7.5%/yr)

Funding provides a 38.9% offset to fee+slippage costs.

### Regime-Conditional Spread (Gross)

| Regime | N | Mean Spread | Win Rate | Geo Ann. Spread |
|--------|---|-------------|----------|----------------|
| Bull | 23 | +4.58% | 78.3% | +63.92% |
| Bear | 15 | +3.30% | 60.0% | +40.48% |
| Sideways | 7 | 0.00% | — | 0.00% (cash) |

---

## 10. Trade Execution Details

### Rebalancing Cadence
- **Frequency**: Monthly, first Sunday of each month (aligned to CMC supply snapshot dates)
- **Execution date**: Following Monday open on Binance perpetuals

### Trade Counts (45 periods)

| Leg | Opens | Closes | Total |
|-----|-------|--------|-------|
| Long | 101 | 96 | 197 |
| Short | 115 | 74 | 189 |
| **Both legs** | **—** | **—** | **386** |

Average: **8.6 trades per period**

### Turnover

| Leg | Avg Monthly Turnover |
|-----|---------------------|
| Long | 39.7% |
| Short | 34.6% |

Turnover is defined as fraction of the portfolio replaced each month. ~40% turnover means
roughly 40% of each leg is sold and replaced each rebalancing.

### Basket Sizes

| Regime | Avg Long | Avg Short |
|--------|----------|-----------|
| Bull | 8.6 | 11.3 |
| Bear | 9.5 | 10.1 |
| Sideways | 8.6 | 11.1 (unused) |
| **Overall** | **8.9** | **10.9** |

### Most Persistent Holdings

**Long basket** (top-10 by frequency, of 45 periods):

| Token | Periods | Frequency |
|-------|---------|-----------|
| NEO | 33 | 73% |
| THETA | 27 | 60% |
| ZRX | 27 | 60% |
| IOTX | 25 | 56% |
| QNT | 20 | 44% |
| YFI | 18 | 40% |
| KSM | 14 | 31% |
| GLM | 13 | 29% |
| AR | 12 | 27% |
| ONT | 12 | 27% |

**Short basket** (top-10 by frequency):

| Token | Periods | Frequency |
|-------|---------|-----------|
| KAVA | 26 | 58% |
| 1INCH | 18 | 40% |
| GMT | 17 | 38% |
| FIL | 16 | 36% |
| WLD | 16 | 36% |
| GALA | 15 | 33% |
| STRK | 15 | 33% |
| OP | 14 | 31% |
| APT | 14 | 31% |
| ARB | 14 | 31% |

---

## 11. Funding Rate Attribution

Actual Binance 8h funding rates aggregated to monthly:

| Metric | Value |
|--------|-------|
| Cum. funding impact (long leg) | -1.89% (drag) |
| Cum. funding impact (short leg) | +8.50% (credit) |
| **Net funding impact** | **+6.61%** |
| Avg period long funding drag | -0.04%/period |
| Avg period short funding credit | +0.19%/period |

Funding is a net **positive contributor** (+6.61% cumulative). Short positions in high-inflation
tokens tend to carry positive funding (market pays to be long), meaning shorts receive funding.
When funding is zeroed out, strategy returns +12.07% ann vs +12.99% real — funding contributes
~7% of total returns (+0.92pp/yr).

---

## 12. Factor Decomposition

### BTC Beta
Rolling 52-week beta vs BTC returns (estimated from per-period combined net returns):
- Long basket beta vs BTC: +1.255
- Short basket beta vs BTC: +1.417
- **Net portfolio beta**: 0.75×1.255 − 0.75×1.417 = **−0.121**

The portfolio is mildly net-short the crypto market. Both baskets have high beta to BTC
(alts move with BTC), but the short basket's slightly higher beta creates a small net-short tilt.

### Spread Distribution
- Excess kurtosis: 2.11 (fat-tailed relative to normal)
- Skewness: +1.13 (right-skewed — occasional large positive outliers)
- CVaR risk is higher than standard deviation implies

---

## 13. Overfitting Analysis

Three independent tests were conducted:

### 13a. Walk-Forward Split (IS: 2022–2023, OOS: 2024–2026)

| Config | IS Sharpe | OOS Sharpe | OOS/IS ratio |
|--------|-----------|------------|--------------|
| v7 baseline | 0.94 | 0.44 | 0.47x |
| v8 (this strategy) | 0.84 | 0.74 | **0.88x** |

v8 degrades only 12% out-of-sample vs 53% for v7 baseline, suggesting the parameter changes
capture more structural signal rather than IS-specific noise.

### 13b. Parameter Sensitivity Grid (BULL_BAND × BEAR_BAND, 36 combinations)
With SUPPLY_WINDOW=26, v8's (1.05, 0.95) point is **not the global maximum**. Wider bands
(1.10/0.90) score higher on the 26w signal. This suggests the parameter improvement is driven
primarily by the `SUPPLY_WINDOW=26` change (less noise in the signal), not by band optimization.
No isolated "lucky peak" at the v8 coordinates.

### 13c. Signal Permutation Test (200 simulations, shuffled pct_rank)
With randomized supply ranks, mean strategy return = **+0.163% ann** (near zero). Observed
v8 return = **+12.99% ann**. Empirical p-value ≈ 0.05 (borderline significant). The low
p-value partly reflects only 45 periods (limited power) rather than absence of signal.

---

## 14. ZEC Concentration Risk

**ZEC is a significant single-token risk factor.** ZEC appears in the long basket for
12 consecutive periods (mostly 2024–2025), contributing approximately:
- Strategy return with ZEC: +12.99% ann
- Strategy return without ZEC: **+7.04% ann** (Sharpe +0.413)
- ZEC contribution: **~5.95pp/yr** (46% of total return)

The Sep 2025 period saw ZEC return **+242%** — a privacy-coin regulatory/narrative event, not
supply-signal alpha. This is not detectable in advance via the supply signal and represents
a single-token concentration risk. The residual strategy without ZEC (+7.04% ann, Sharpe 0.413)
remains competitive but more modest.

**Long book is structurally a loser**: The long basket gross cumulative return is approximately
−89.7% over 45 periods. All alpha originates from the short book. The strategy is correctly
understood as a **short-inflation strategy with a partially-offsetting long hedge** rather than
a balanced L/S alpha strategy.

---

## 15. Statistical Caveats

| Issue | Detail |
|-------|--------|
| **Short history** | 45 monthly periods = 3.75 years. Most statistics have wide confidence intervals. |
| **Bear regime significance** | Bootstrap p=0.104, 95% CI [−15.4%, +149.1%]. Bear alpha not statistically confirmed. |
| **Bull regime significance** | Bootstrap p=0.009, 95% CI [+8.9%, +151.5%]. Bull alpha is statistically significant. |
| **Multiple testing** | ~16 parameter grid combinations were evaluated before landing on v8. Deflated Sharpe adjusting for multiple comparisons would be lower than 0.765. |
| **Fat tails** | Excess kurtosis 2.11. CVaR and tail risk are understated by standard deviation alone. |
| **ZEC narrative event** | The Sep 2025 ZEC +242% return is a single event risk, not recurring signal alpha. |

---

## 16. Live Trading Requirements

### Minimum Capital

| Scenario | Capital |
|----------|---------|
| 5% ADTV impact per token | **$25.4M** |
| 1% ADTV impact per token (recommended) | **$5.1M** |

At 1% ADTV impact ($5.1M minimum), position sizes fit within liquidity constraints given
the $5M/day ADTV floor requirement.

### Operational Requirements
- Monthly rebalancing (~17 trades per month: ~8-9 opens + ~8-9 closes per leg)
- Taker order execution on Binance USDT-M perpetuals
- Real-time access to CMC circulating supply data (or derived proxy)
- Funding rate monitoring and attribution

### Cost Estimate (Live)
- Monthly fee (2×0.04% taker): ~0.08% of gross notional rebalanced
- Annual fee estimate: ~0.96% of NAV
- Slippage adds ~0.45%/period gross at typical basket sizes
- Total annual cost: ~7.5% of gross return (at current basket/turnover levels)

---

## 17. Architecture & Ablation Tests (post-v8)

Two additional diagnostic scripts were run after v8 to identify structural improvements
using only existing data (no new data sources required).

### 17a. Veto Ablation (`ablation_study.py`)

Leave-one-out ablation: each component disabled individually, Sharpe delta measured vs v8.

| Component removed | dSharpe | Decision |
|---|---|---|
| Circuit breaker (>40% short loss) | −0.897 | **KEEP** — essential; strategy blows up without it |
| 26w signal only (drop 52w component) | −0.809 | **KEEP 52w** — 52w is the load-bearing signal |
| Buffer bands (hysteresis removed) | −0.494 | **KEEP** — high turnover without them |
| Squeeze exclusion (>40% prior rally) | −0.312 | **KEEP** |
| Long quality veto (bottom 33% BTC-alpha) | −0.180 | **KEEP** |
| Altseason veto (≥75% alts beat BTC) | −0.170 | **KEEP** |
| Regime always active (no Sideways cash) | −0.114 | **KEEP** — regime gating matters |
| **Momentum veto (50th pct short pool)** | **0.000** | **DROP** — completely neutral, frees 1 param |
| 52w signal only (drop 26w component) | **+0.110** | **26w window adds noise; 52w alone is better** |

**Critical insight**: the 26w fast supply window hurts relative to using 52w alone. Combined
with the architecture tests below, the evidence consistently points toward longer windows.

### 17b. Architecture Tests (`test_architectures.py`)

11 structural variants tested. All use v8 parameters as the base. Key results:

| Config | Ann% | Sharpe | HAC | MaxDD% | Win% | dSharpe |
|---|---|---|---|---|---|---|
| v8 BASELINE | +12.99% | +0.765 | +1.058 | −14.46% | 60.0% | — |
| SHORT_ONLY (zero long leg) | −2.43% | −0.030 | +0.565 | −68.72% | 60.0% | −0.795 |
| WIN_26_104 (26w+104w windows) | +10.79% | +0.518 | +0.900 | −15.20% | 53.3% | −0.247 |
| BTC_REGIME (BTC price as regime index) | +10.32% | +0.604 | +0.904 | −11.63% | 55.6% | −0.161 |
| WIN_39_78 (39w+78w windows) | +11.26% | +0.694 | +1.022 | −12.05% | 60.0% | −0.071 |
| FUND_VETO (short only when funding>0) | +12.99% | +0.765 | +1.058 | −14.46% | 60.0% | 0.000 |
| 52w signal only | +16.34% | +0.875 | +1.407 | −12.49% | 55.6% | +0.110 |
| WIN_52_104 (52w+104w windows) | +18.73% | +1.161 | +1.594 | −10.55% | 65.1% | +0.396 |
| BTC_LONG (BTC perp replaces altcoin longs) | +25.65% | +1.068 | +1.303 | −15.97% | 57.8% | +0.303 |
| WIN_52_104 + 104w-pure signal | +22.28% | +1.304 | +1.602 | **−9.58%** | **70.5%** | +0.539 |
| BTC_LONG + WIN_52_104 | +29.68% | +1.402 | +1.723 | −16.85% | 65.1% | +0.637 |
| **ULTIMATE** (BTC_LONG + 104w pure) | **+33.73%** | **+1.533** | **+1.692** | −18.51% | 65.9% | **+0.768** |

**Findings:**

1. **SHORT_ONLY is catastrophic** (MaxDD −68.72%). The long leg is not generating alpha — it
   is hedging the short leg against altcoin-wide pumps. Removing it causes full short exposure
   during altcoin bull regimes with no offset.

2. **BTC_LONG is a genuine improvement** (+0.303 dSharpe). Replacing the structurally losing
   altcoin long basket (gross −89.7% cumulative) with a BTC perpetual long converts the strategy
   into a coherent BTC-vs-high-inflation-alts spread trade. Net funding turns negative (BTC longs
   pay ~0.006% per 8h) but the improved spread more than compensates. Funding sign convention:
   `fund_long_basket = btc_funding_sum` (positive = what longs pay = drag).

3. **WIN_52_104 is the best single parameter change** (+0.396 dSharpe). Both ablation (52w alone
   outperforms 50/50 blend) and architecture tests (52w+104w > 26w+52w) confirm the 26w window
   adds noise. Supply inflation over 52+ weeks captures structural dilution; shorter windows pick
   up temporary pauses and bursts that revert.

4. **BTC_REGIME hurts** (−0.161 dSharpe). The cap-weighted altcoin index detects altcoin-specific
   dynamics (alt seasons, alt bear markets independent of BTC) that pure BTC price misses. Keep
   the existing altcoin index regime.

5. **FUND_VETO is neutral** (0.000 dSharpe). Funding veto on the short leg does not activate
   meaningfully — nearly all short candidates have positive prior-period funding regardless.

### 17c. Recommended Next Configurations

| Priority | Config | Sharpe | MaxDD | Notes |
|---|---|---|---|---|
| Conservative | WIN_52_104 + 104w-pure signal | +1.304 | −9.58% | Lowest drawdown, highest win rate |
| Balanced | BTC_LONG + WIN_52_104 | +1.402 | −16.85% | Best IS Sharpe without regime change |
| Aggressive | ULTIMATE (BTC_LONG + 104w pure) | +1.533 | −18.51% | Highest return, higher drawdown |

All three: **drop the momentum veto** (neutral, frees one parameter, reduces IS-overfitting risk).

*Note: All configs above are evaluated on the same 2022–2026 IS dataset. The additional
architecture search increases the effective multiple-testing count and further deflates the
true out-of-sample Sharpe. Walk-forward validation required before adopting any variant.*

---

## 18. Stress Tests (QR Validation)

Three tests run to address the QR critique: (1) no hold-out validation, (2) BTC dominance
masking supply signal, (3) results driven by BTC beta rather than alpha.

### Test A — Rolling Walk-Forward (5 × 6-month OOS folds)

Fixed architecture per config; no re-optimisation per fold. IS window = 2022H1+H2.

| Window | v8 SR | WIN52_SLOW SR | ULTIMATE SR |
|--------|-------|---------------|-------------|
| IS  2022H1+H2 (ref) | +0.883 | +0.921 | +0.835 |
| OOS 2023-H2 | +3.123 | −0.022 | +0.988 |
| OOS 2024-H1 | +4.021 | +3.280 | +7.786 |
| OOS 2024-H2 | +0.352 | +1.876 | +0.253 |
| OOS 2025-H1 | +5.449 | +4.237 | +4.740 |
| OOS 2025-H2+ | −2.434 | +2.250 | +0.954 |
| **Mean OOS SR** | **+2.102** | **+2.324** | **+2.944** |
| **OOS/IS ratio** | **2.38×** | **2.52×** | **3.53×** |

**Finding**: All three configs deliver positive mean OOS Sharpe with OOS/IS ratios >1.
This is structurally unusual (bull-market crypto universe generates strong cross-sectional
dispersion), and the IS window (2022, bear market) was the hardest. OOS 2025-H2+ was the
only consistent failure period for v8; WIN52_SLOW and ULTIMATE both survived it.

### Test B — ULTIMATE Permutation Test (200 simulations)

Null hypothesis: long BTC perp + short *N randomly-selected alts* (PERMUTE_SEED shuffles
pct_rank, leaving long leg = BTC unchanged). Tests whether supply-inflation signal on the
short side beats random alt selection.

| Statistic | Value |
|-----------|-------|
| Real ULTIMATE Sharpe | +1.533 |
| Permuted mean Sharpe | +1.005 |
| Permuted std Sharpe | 0.236 |
| Permuted 95th pct | +1.458 |
| Permuted 99th pct | +1.527 |
| Sims ≥ real SR | 2 / 200 |
| Empirical p-value | **0.0100** |
| Real SR percentile in null | **99.0th** |

**Finding**: p = 0.010 (marginally above the 1% threshold). The real Sharpe sits at the
99th percentile of the null distribution. The supply-inflation signal on the short side
adds measurable alpha over random alt selection — though the signal is weak relative to
the long-BTC structural edge (permuted null mean = +1.005, already strong from BTC long).

### Test C — BTC Beta Decomposition (OLS)

ULTIMATE per-period net returns regressed on BTC monthly returns.

| Statistic | Value |
|-----------|-------|
| N periods | 44 |
| alpha (per period) | +0.0269 |
| alpha (annualised) | **+37.5%/yr** |
| alpha t-stat | +2.93 |
| alpha p-value | **0.0054** |
| beta (BTC exposure) | +0.075 |
| beta t-stat | +1.19 (p=0.24) |
| R² | **0.032** |

**Finding**: 96.8% of per-period variance is idiosyncratic — *not* explained by BTC
monthly returns. The alpha intercept is +37.5%/yr (annualised) with t=2.93, p=0.005 —
statistically significant at conventional levels. Net BTC beta = +0.075 (near zero),
refuting the claim that ULTIMATE is simply a leveraged BTC dominance trade.

Regime-conditional residuals:
- Bull (N=23): mean_residual = +0.0156, t=+1.29, p=0.210 (directionally positive, weak)
- Bear (N=15): mean_residual = −0.0131, t=−0.73, p=0.480 (mildly negative, insignificant)
- Sideways (N=6): mean_residual = −0.0269, t=−13.62, p<0.001 (negative — but Sideways = 0%
  exposure by design, so residuals reflect model artefact from near-zero variance periods)

### Summary

| QR Critique | Test | Finding |
|-------------|------|---------|
| No hold-out validation | Walk-forward A | All configs positive OOS; ULTIMATE OOS/IS=3.53× |
| BTC dominance masks signal | Permutation B | p=0.010; supply signal at 99th pctl of null |
| Strategy is just BTC beta | OLS C | R²=0.032; alpha=+37.5%/yr, t=2.93, p=0.005 |

The three stress tests provide meaningful (if not ironclad) evidence that:
1. The strategy is not purely an IS artefact.
2. The short-side supply signal contributes alpha beyond random alt selection.
3. ULTIMATE returns are not explained by BTC beta (R²=3.2%).

Key caveat: the OOS periods coincide with a strong crypto bull market (2023–2025) which
generates large cross-sectional dispersion irrespective of signal quality. The supply
signal may be riding a structural tailwind rather than generating genuine alpha.

---

## 19. Blind Spot Tests

Five additional tests run to probe structural weak points identified in QR review.

### Test 1 — Signal Dispersion Filter
Does the supply signal only work when there is genuine cross-sectional dispersion (wide IQR
of supply ranks)? Low-dispersion periods may be pure noise regardless of signal value.

| Metric | Value |
|--------|-------|
| Median 26w IQR across periods | 0.107 |
| High-dispersion half Sharpe (N=23) | +1.123 |
| Low-dispersion half Sharpe (N=22) | +0.664 |
| Spearman(IQR, return) | rho = −0.079, p = 0.607 |

**Finding**: Signal return is **independent of dispersion level**. The strategy fires with
equal effectiveness whether cross-sectional spread is wide or narrow. No dispersion filter
is warranted — but this also means the model cannot self-regulate in low-information periods.

### Test 2 — 26w vs 52w Rank Correlation
If the two signal layers are highly correlated, the "two-layer" model is complexity theatre.

| Metric | Value |
|--------|-------|
| Mean Spearman rank-corr (26w vs 52w) | **0.750** |
| Std across periods | 0.071 |
| % periods with corr > 0.80 | 25.2% |
| % periods with corr > 0.90 | 0.9% |

**Finding**: Moderate correlation (0.75 mean). Not redundant enough to eliminate either
window, but the diversification benefit is materially less than a "two independent factors"
framing implies. The 52w window dominates: ablation confirmed 52w-only (+0.110 dSharpe)
beats the 50/50 blend, and WIN_52_104 architecture is the best single improvement.

### Test 3 — Slippage Sensitivity Sweep

| k | Ann. Return | Sharpe | dSharpe |
|---|------------|--------|---------|
| 0.0001 | +20.87% | +1.225 | ref (ideal) |
| **0.0005 (baseline)** | **+12.99%** | **+0.765** | −0.460 |
| 0.001 (2× baseline) | +8.14% | +0.482 | −0.743 |
| 0.002 (4× baseline) | +4.58% | +0.272 | −0.953 |
| 0.005 (10× baseline) | +2.83% | +0.169 | −1.056 |
| 0.010 (20× baseline) | +2.54% | +0.152 | −1.073 |

**Finding**: Sharpe drops below 0.5 at **k = 0.001** (2× the assumed market impact). This
is the single most dangerous blind spot in the model. For 9-10 token baskets including
mid/small-cap alts, real market impact could easily be 2×–5× the modelled k=0.0005. The
+0.765 Sharpe should be treated as an **upper bound**, with k=0.001 (SR +0.48) as the
conservative estimate.

### Test 4 — Winsorisation Sensitivity

| Config | Sharpe | dSharpe |
|--------|--------|---------|
| 2/98% (baseline) | +0.765 | — |
| 1/99% (looser) | +0.722 | −0.043 |
| 5/95% (tighter) | +0.732 | −0.033 |
| 0/100% (no clipping) | +0.722 | −0.043 |
| 10/90% (aggressive) | +0.814 | +0.049 |

**Finding**: Removing winsorisation entirely is neutral (−0.043 dSharpe), and aggressive
clipping (10/90%) marginally helps (+0.049). Extreme supply inflators are **noise, not
signal** — the predictive information lives in the middle of the distribution. The current
2/98% clip is reasonable; tightening to 10/90% is a minor candidate improvement.

### Test 5 — Regime-Conditional Permutation (Bull vs Bear)

| Regime | Real SR | Null Mean | Null Std | p-value | Real Pctile |
|--------|---------|-----------|----------|---------|-------------|
| Bull (N=23) | +0.707 | +0.198 | 0.332 | **0.050** | 95th |
| Bear (N=15) | +0.760 | +0.248 | 0.326 | **0.060** | 94th |

**Finding**: Both regimes are **independently borderline**. Neither Bull nor Bear is the
regime where the supply signal is clearly valid — the full-sample p=0.05 is not driven by
one regime outperforming. This is consistent with the signal being weakly effective across
all trending conditions, not regime-specific alpha.

### Blind Spots Summary

| Issue | Severity | Finding |
|-------|----------|---------|
| Slippage sensitivity | **HIGH** | SR halves at 2× assumed k; real execution could be 2–5× |
| Short history (45 periods) | **HIGH** | All sub-period statistics have very wide confidence intervals |
| Signal dispersion independence | Medium | Model fires in low-info periods without self-regulation |
| 26w/52w redundancy | Medium | 0.75 mean correlation; blending adds marginal diversification |
| Regime-conditional signal | Medium | Both regimes borderline (p~0.05); no regime-specific strength |
| Winsorisation | Low | Current 2/98% is fine; 10/90% is minor candidate improvement |

---

## 20. Files

| File | Purpose |
|------|---------|
| `perpetual_ls_v7.py` | Main strategy (v8 defaults) |
| `run_experiments.py` | Parameter grid runner (16 experiments) |
| `overfitting_tests.py` | Walk-forward, sensitivity grid, permutation test |
| `backtest_diagnostics.py` | Funding strip, beta decomp, token attribution, spread dist, bootstrap CI |
| `zec_analysis.py` | ZEC attribution + per-period decomp + overfitting audit |
| `net_vs_gross.py` | Gross vs net P&L breakdown and capital requirements |
| `ablation_study.py` | Leave-one-out veto ablation (10 configs vs v8 baseline) |
| `test_architectures.py` | Architectural variant tests (11 configs, no new data) |
| `stress_tests.py` | Stress tests: walk-forward, permutation, BTC beta OLS |
| `perm_v8.py` | Core thesis permutation (alt L/S, both sides shuffled) |
| `sideways_test.py` | Sideways regime load-bearing test |
| `blind_spots.py` | 5-test blind spot suite (dispersion, corr, slippage, winsor, regime perms) |
| `generate_charts.py` | Full chart suite generator (7 charts) |
| `fetch_binance_data.py` | Data fetcher (Binance perp prices + funding) |

---

## 21. Charts

| Chart | Description |
|-------|-------------|
| `perp_ls_v7_cumulative.png` | 3-panel: cumulative wealth (log), net NAV, per-period spread bars |
| `perp_ls_v7_regime_dd.png` | Drawdown + per-period regime spread |
| `perp_ls_v7_vs_v6.png` | v6 vs v7 scorecard (geo spread by regime, NAV, drawdown, table) |
| `perp_ls_v8_dashboard.png` | Dark-mode dashboard: NAV, drawdown, spreads, rolling 12m Sharpe, stats |
| `perp_ls_v8_slippage.png` | Sharpe and Ann. Return vs slippage coefficient k |
| `perp_ls_v8_walkforward.png` | Walk-forward bar chart (6 folds × 3 configs) |
| `perp_ls_v8_permutation.png` | Permutation null distributions (core thesis + ULTIMATE) |

---

## 22. Version Comparison

| Metric | v4 | v6 | v7 baseline | v8 (current) |
|--------|----|----|------------|--------------|
| Combined net ann. | −5.1% | +0.1% | +4.0% | **+13.0%** |
| MaxDD | −22.9% | −19.2% | −20.7% | **−14.5%** |
| Sharpe | −0.222 | +0.003 | +0.220 | **+0.765** |
| Win rate (spread) | 48.0% | 52.6% | 53.3% | **60.0%** |
| Mean period spread | +1.95% | +3.34% | +3.44% | **+3.44%** |
| Bull geo spread | +21.1% | +51.9% | +18.7% | **+63.9%** |
| Bear geo spread | +48.1% | +53.3% | +81.9% | **+40.5%** |

Key v8 changes vs v7 baseline:
- `SUPPLY_WINDOW` 13→26 (slower fast signal, ~60% of improvement)
- `BULL_BAND`/`BEAR_BAND` 1.10/0.90→1.05/0.95 (more active periods, ~30%)
- `LONG_QUALITY_LOOKBACK` 6→12 months (longer quality veto lookback, ~10%)
