# Perpetual L/S Backtest — Methodology, Execution & Results

**Strategy:** Supply-Dilution Long/Short on Perpetual Futures
**Versions:** v1 (`perpetual_ls_backtest.py`) → v2 (`perpetual_ls_v2.py`) → v3 (`perpetual_ls_v3.py`)
**Coverage:** 2017-01-01 to 2026-02-22 (v1/v2 full history; v3 post-2022 only)

---

## 1. Core Thesis

The **Supply-Dilution Hypothesis** states that persistent circulating-supply inflation is a
negative price signal: tokens diluting their float impose an ongoing cost on holders that
should systematically compress returns relative to tokens with flat or deflationary supply.

Operationally:
- **Long** tokens with the **lowest** trailing supply inflation (deflationary / zero-emission)
- **Short** tokens with the **highest** trailing supply inflation (persistent diluters)
- Capture the **spread** between the two legs via monthly-rebalanced perpetual futures

The thesis was validated at the decile level in the main backtest suite: the 10th-percentile
basket outperforms the 90th-percentile basket by ~23 percentage points annualised with a
59.4% monthly win rate (see `extreme_percentile.py`). The perpetual-futures L/S scripts
operationalise this edge into an executable portfolio with realistic costs.

---

## 2. Dataset

| Field | Detail |
|-------|--------|
| **Source** | `cmc_historical_top300_filtered_with_supply.csv` |
| **Coverage** | Top 300 cryptos by market cap, weekly snapshots |
| **Date range** | 2017-01-01 → 2026-02-22 (477 snapshots, 2,267 symbols) |
| **Key columns** | `snapshot_date`, `rank`, `symbol`, `market_cap`, `price`, `circulating_supply`, `volume_24h` |
| **Supply derivation** | `circulating_supply = market_cap / price` (computed in `circulating_supply.py`) |

**Survivorship bias caveat.** Tokens that dropped below rank 300 exit the dataset at that
point. Their terminal losses are therefore not captured in the backtest, creating a
systematic upward bias in both the long and short baskets. For the short leg specifically
this is a benefit (dead tokens are removed before they can be shorted again), but it
also means the long basket's worst dead-project outcomes are truncated.

---

## 3. Universe Construction

At each monthly rebalancing date, the eligible universe is defined by applying filters in
order. Filters accumulate (each row must pass all prior filters to advance).

### 3.1 Rank filter

```
rank > TOP_N_EXCLUDE  AND  rank <= MAX_RANK
```

| Parameter | v1 | v2 | v3 |
|-----------|:--:|:--:|:--:|
| `TOP_N_EXCLUDE` | 20 | 20 | 20 |
| `MAX_RANK` | 250 | 200 | 200 |

The top-20 exclusion removes BTC, ETH, and the largest liquid assets whose supply
dynamics differ from the altcoin universe (BTC is capped at 21M; ETH has EIP-1559 burns).

### 3.2 Categorical exclusions

Tokens are excluded by symbol using hard-coded sets:

| Set | Examples | Reason |
|-----|---------|--------|
| `STABLECOINS` | USDT, USDC, DAI, FRAX | Supply changes are operational, not inflationary |
| `CEX_TOKENS` | BNB, OKB, KCS, LEO | Exchange-controlled buybacks/burns create non-fundamental supply dynamics |
| `MEMECOINS` | DOGE, SHIB, PEPE, BONK | No fundamental supply-valuation relationship |
| `WRAPPED_ASSETS` (v2+) | WBTC, BTCB, BTC.b, WETH, WBNB | Bridge minting double-counts native asset supply |
| `LIQUID_STAKING` (v2+) | stETH, rETH, jSOL, mSOL, cbETH | LSD supply growth tracks staking inflows, not protocol dilution |
| `PROTOCOL_SYNTHETICS` (v2+) | vBTC, vETH, vBNB, VTHO | Interest-accrual accounting artifacts; no external market |
| `COMMODITY_BACKED` (v2+) | PAXG, XAUt, KAU | Gold-backed; no perp market; supply tracks AUM not emissions |

**Why this matters (v1 blind spot).** In v1, PAXG appeared 21 times in the short basket
with zero actual perpetual-futures market — the backtest was assigning a notional short
position to an untradeable asset and booking its price return as profit/loss. Similarly,
vBTC showed a $602M CMC market cap with $0 recorded volume. Removing these categories
eliminates phantom liquidity from the results.

### 3.3 Liquidity and history filters

| Filter | v1 | v2 | v3 |
|--------|:--:|:--:|:--:|
| Min daily volume | none | $5,000,000 | $5,000,000 |
| Min market cap | none | none | $50,000,000 |
| Min supply history | none | 26 non-NaN 13w periods | 26 non-NaN 13w periods |

The `$5M` volume gate ensures each position can be entered and exited at a reasonable
size. The `$50M` market cap floor (v3) catches residual low-liquidity names that pass the
volume screen on a single high-activity day. The 26-week history requirement prevents
newly-listed tokens with unusual early-life supply mechanics from contaminating the signal.

---

## 4. Signal Construction

### 4.1 Trailing supply inflation (v1 and v2)

```
supply_inf(t) = circulating_supply(t) / circulating_supply(t - W) - 1
```

where `W = SUPPLY_WINDOW` weeks.

| Parameter | v1 | v2 | v3 |
|-----------|:--:|:--:|:--:|
| `SUPPLY_WINDOW` | 4 weeks | 13 weeks | 13 weeks (fast) |

**Rationale for extending from 4w → 13w.** A 4-week window captures short-term vesting
cliff events and can be gamed by teams timing large distributions to fall just after a
rebalancing date. The 13-week (≈90-day) window captures the structural 3-month dilution
trend and is harder to game. It also reduces single-week noise from CMC data corrections.

### 4.2 Composite signal (v3 only)

```
rank_13w(t) = cross_sectional_rank( supply_inf_13w(t) )      [percentile, 0-1]
rank_52w(t) = cross_sectional_rank( supply_inf_52w(t) )      [percentile, 0-1]
composite_rank(t) = 0.5 × rank_13w(t) + 0.5 × rank_52w(t)
```

If `rank_52w` is unavailable (token has < 52 weeks of supply data), it falls back to
`rank_13w`, preserving eligibility while downgrading the signal quality.

**Rationale.** The 52-week component captures the structural annual dilution rate —
whether a protocol's token issuance schedule is fundamentally dilutive over the medium
term. The 13-week component retains sensitivity to recent changes (new vesting cliffs,
buyback programs, supply bridge mechanics). Blending both produces a rank that is harder
for projects to game on a single horizon while remaining responsive to genuine changes.

### 4.3 Cross-sectional winsorisation (v3 only)

Before computing the percentile rank, raw supply inflation values are winsorised at the
2nd and 98th cross-sectional percentiles:

```python
lo, hi = supply_inf.quantile([0.02, 0.98])
supply_inf_winsorised = supply_inf.clip(lo, hi)
```

This prevents a single token with a 10,000% supply spike (e.g., a token migration or
bridge minting event that escaped the exclusion lists) from anchoring the short basket
every period.

---

## 5. Portfolio Construction

### 5.1 Basket selection — percentile thresholds

| Basket | Signal rank condition | v1 | v2 | v3 |
|--------|----------------------|:--:|:--:|:--:|
| **Long (entry)** | composite_rank ≤ X | ≤ 10th pct | ≤ 7th pct | ≤ 12th pct |
| **Long (exit)** | composite_rank ≤ Y | ≤ 10th pct | ≤ 13th pct | ≤ 18th pct |
| **Short (entry)** | composite_rank ≥ X | ≥ 90th pct | ≥ 93rd pct | ≥ 88th pct |
| **Short (exit)** | composite_rank ≥ Y | ≥ 90th pct | ≥ 87th pct | ≥ 82nd pct |

**Inner buffer band (v2+).** A token entering the long basket requires composite rank
≤ entry threshold. A token already in the long basket from the previous period only exits
if its rank rises above the (wider) exit threshold. This creates a hysteresis band that
reduces unnecessary turnover: a token ranked at the 8th percentile in period T (eligible
at 7th-pct entry in v2) does not exit at the 8th pct in period T+1 — it stays until
it exceeds the 13th-pct exit threshold.

**Why thresholds differ between v2 and v3.** The composite 13w+52w signal produces a
blended rank that is narrower in its extreme tails than either individual signal. A token
must be consistently low-inflation on both horizons to achieve a ≤7th composite rank;
in practice this reduces the eligible long basket to 3-5 tokens at 7%. Widening to 12%
recovers ~13 tokens per basket with 128 eligible symbols — consistent with the v2 basket
size before the composite blending.

### 5.2 Position weighting

| Method | v1 | v2 | v3 |
|--------|:--:|:--:|:--:|
| **Equal weight** | ✓ | — | — |
| **sqrt(ADTV) weighted** | — | ✓ | — |
| **Inv-vol × sqrt(ADTV)** | — | — | ✓ |
| **Per-position cap** | none | 15% | 20% |

**v3 weighting formula:**
```
raw_weight(i) = sqrt(ADTV_i) / realized_vol_i
weight(i) = raw_weight(i) / sum(raw_weight(j))   [then capped at 20% and renormalised]
```

where `realized_vol_i` is the 8-week rolling annualised standard deviation of the token's
weekly returns. This gives more weight to tokens that are both liquid (high ADTV) and
stable (low volatility). The weighting explicitly de-risks the concentrated positions that
caused single-token blow-ups in v1 and v2: a token 3× more volatile than average receives
one-third the weight, irrespective of its supply rank.

### 5.3 Overlap resolution

If a token qualifies for both the long and short basket simultaneously (possible when
entry and exit thresholds are wide), it is removed from both baskets. In practice this
is rare (< 0.5% of period-token pairs) because the two extremes of the supply rank
distribution do not overlap.

### 5.4 Minimum basket size

If either basket has fewer than `MIN_BASKET_SIZE = 6` tokens after all filters and the
buffer band, the period is skipped. This prevents the strategy from operating on a
single-token concentrated position where idiosyncratic risk would dominate.

---

## 6. Regime Detection

### 6.1 Market regime (Bull / Bear / Sideways)

A cap-weighted index of the top-100 tokens by market cap is computed at each weekly
snapshot:

```
index_return(t) = sum_i [ w_i(t) * pct_ret_i(t) ]
where w_i(t) = market_cap_i(t) / sum_j(market_cap_j(t))
```

The index level is the cumulative product of (1 + index_return). A 20-week simple moving
average of the index level is computed. The regime at time t is:

```
Bull     if index_price(t) >= MA20(t) × 1.10
Bear     if index_price(t) <= MA20(t) × 0.90
Sideways otherwise (90% to 110% of MA20)
```

The 10% band around MA prevents frequent regime flips near the moving average.

### 6.2 High-volatility detection (v2+)

The 8-week rolling realised volatility of BTC returns is computed:

```
BTC_vol_8w(t) = std(weekly BTC returns over t-7 to t) × sqrt(52)
```

If `BTC_vol_8w(t) > 80%` annualised, the period is flagged as **high-vol**.

### 6.3 Altcoin-season veto (v3 only)

At each rebalancing date, the fraction of top-50 altcoins (rank 3-50, excluding BTC, ETH,
and all excluded sets) that outperformed BTC over the trailing 4 rebalancing periods is
computed:

```
altseason_index(t) = count(alt_4w_return > BTC_4w_return) / count(alts)
```

If `altseason_index(t) > 0.75`, an **altcoin-season veto** is applied: `short_scale = 0`.
The short leg is zeroed for that period. This prevents the strategy from being short during
the exact environment — manic altcoin rotation — that caused the most catastrophic losses.

**Detection rationale.** During altcoin season, high-emission tokens are bid precisely
*because* their emission is funding the growth narrative the market is chasing (liquidity
mining APYs, staking rewards, ecosystem bootstrapping). The supply-dilution signal inverts:
high supply growth is the *cause* of, not a drag on, the rally. The veto shuts down the
short leg for the duration.

---

## 7. Regime-Aware L/S Scaling (v2+)

Rather than running the strategy at full 100%/100% long/short scale in all environments,
the effective exposure is scaled by regime and volatility:

| Regime | High-Vol BTC? | Long Scale | Short Scale |
|--------|:---:|:---:|:---:|
| Sideways | No | 100% | 100% |
| Sideways | Yes | 100% | 75% |
| Bull | No | 100% | 50% |
| Bull | Yes | 75% | 25% |
| Bear | No | 75% | 75% |
| Bear | Yes | 50% | 25% |

**Rationale.** The QRM teardown found that the strategy only produced positive geometric
alpha in Sideways regimes (+25% annualised) while the short leg became catastrophic in
Bull+HighVol regimes. Scaling the short leg down to 25% in the most dangerous environment
limits the worst-case loss while preserving the Sideways and Bear regime alpha.

The combined return is normalised to a unit NAV:

```
r_combined = (long_scale × r_long_net + short_scale × r_short_net) / (long_scale + short_scale)
```

---

## 8. Execution Model

### 8.1 Perpetual futures mechanics

The backtest executes all positions through perpetual futures (not spot). This allows:
- **Short exposure** without borrowing costs or borrow unavailability
- **Funding rate income/cost** on both legs
- **Leverage flexibility** (all positions at 1× notional in the backtest)

### 8.2 Trading costs

| Cost component | Model | Value |
|---------------|-------|-------|
| Taker fee (entry + exit) | Fixed per-trade | 0.04% × 2 = 0.08% round-trip |
| Slippage | Inverse-turnover proxy | `SLIPPAGE_K / turnover`, capped at 2.00% |
| Funding drag (long) | Regime-dependent 8h rate | +0.008% to +0.008% per 8h |
| Funding credit (short) | Regime-dependent 8h rate | +0.005% to +0.015% per 8h |

**Slippage model:**
```
turnover(t) = max(volume_24h(t) / market_cap(t), 0.001)
slippage(t) = min(SLIPPAGE_K / turnover(t), 0.02)
```

This is an inverse-turnover proxy for market impact. Tokens with high daily turnover
(active markets) have lower slippage. Tokens with low turnover (illiquid markets) have
higher slippage, up to a 200 bps cap. A more rigorous model would use the Almgren-Chriss
square-root model: `MI = σ × sqrt(Q/ADV) × η`.

### 8.3 Synthetic funding rate model

Actual per-token 8h funding rates from Coinglass are not available in the CMC dataset.
The backtest uses a **synthetic regime-dependent model**:

| Regime | Long pays (per 8h) | Short receives (per 8h) |
|--------|:---:|:---:|
| Bull | 0.0080% | 0.0150% |
| Bear | 0.0020% | 0.0050% |
| Sideways | 0.0050% | 0.0080% |

These represent approximate median 8h funding rates observed on Binance USDT-M futures
across each regime type. Bull markets have elevated funding as leveraged longs dominate.
Bear markets have suppressed or near-zero funding.

**Critical limitation.** Real funding rates diverge dramatically during extreme events.
During the April 2021 alt season, funding for tokens like AVAX and LUNA exceeded 0.10%
per 8h for sustained periods — 13× the modelled Bull rate. This means the synthetic model
*materially understates* the funding cost to shorts in altcoin seasons. Replacing
`FUNDING_8H` with per-token real historical rates from the Coinglass API is the single
highest-priority upgrade for live deployment.

The holding period for funding calculation is the number of calendar days between
rebalancing dates, multiplied by 3 funding events per day (standard 8h frequency):

```
n_payments = 3 × hold_days
fund_drag_long  = funding_8h_long  × n_payments   (longs pay this)
fund_credit_short = funding_8h_short × n_payments (shorts receive this)
```

### 8.4 Net return computation

For each leg:
```
r_long_net  = r_long_gross  - taker_fee×2 - slippage_long  - fund_drag_long
r_short_net = -r_short_gross - taker_fee×2 - slippage_short + fund_credit_short
```

Note the sign convention: `r_short_gross` is the raw price return of the short basket.
Shorting a basket that returns `+X%` produces a loss of `X%` for the short position;
shorting a basket that returns `-X%` produces a gain of `X%`.

---

## 9. Risk Management (v3)

### 9.1 Short-basket circuit breaker

If the short basket's gross return in a period exceeds `SHORT_CB_LOSS = 40%` (meaning the
short position loses 40% of notional in one month), the return is capped at −40%:

```python
if r_short_gross > SHORT_CB_LOSS:
    r_short_gross = SHORT_CB_LOSS
```

This simulates an emergency stop-loss rule triggered by extreme short squeezes. In the
post-2022 backtest, the circuit breaker fired 4 times.

**Implementation note.** In a live system, this would be an intra-period monitoring rule
(e.g., daily mark-to-market with a forced close if the position exceeds the threshold),
not a monthly calculation. The backtest approximation is conservative — real losses would
be capped sooner, not at the month-end mark.

### 9.2 Squeeze exclusion for new short entries

Before constructing the short basket at each rebalancing date, tokens whose prior-period
price return exceeded `+40%` are flagged as "recently squeezed" and blocked from entering
new short positions:

```python
squeezed = {s for s in universe if prior_period_return(s) > 0.40}
entry_short = high_inflation_tokens - squeezed
```

Existing short positions (stay_short) are not force-exited by this rule — only new entries
are blocked. This prevents the strategy from adding a new short on a token that is already
in a momentum squeeze while allowing existing rational shorts to remain.

### 9.3 BTC rolling beta hedge (v3)

At each rebalancing date, a 12-period rolling OLS beta of the strategy's combined return
versus BTC's forward return is computed from historical periods:

```
beta(t) = Cov(r_combined[-12:], r_btc[-12:]) / Var(r_btc[-12:])
hedge_return(t) = -min(beta(t), 1.0) × r_btc(t)
r_combined_hedged(t) = r_combined(t) + hedge_return(t)
```

The hedge is only applied when at least 4 historical paired observations are available.
The beta is capped at 1.0 to prevent over-hedging from noisy rolling estimates.

**Interpretation of results.** In the v3 post-2022 backtest, the unhedged combined
return was +1.82% annualised (Sharpe +0.051). The BTC-hedged combined was −13.76%. This
implies that most of the +1.82% gross return was driven by residual BTC beta (avg 0.541),
not by the supply-dilution signal itself. The true market-neutral alpha is approximately
−13.76% — negative. The Lo (2002) HAC-corrected Sharpe on the unhedged combined of +0.228
provides the most honest estimate of the signal's information content, adjusting for the
autocorrelation structure that inflates standard Sharpe estimates in monthly time series.

---

## 10. Performance Metrics

All metrics are computed from the monthly-frequency series of combined net returns.

| Metric | Formula |
|--------|---------|
| Annualised return | `cum_return^(1/years) - 1`, where years = calendar days / 365.25 |
| Annualised volatility | `std(r_t) × sqrt(ppy)`, ppy = periods per year from median gap |
| Sharpe ratio | `ann_return / ann_vol` |
| Lo (2002) HAC Sharpe | Adjusts for autocorrelation: `Sharpe_raw × sqrt(ppy) / sqrt(1 + 2Σw_q×AC_q)` |
| Sortino ratio | `ann_return / (sqrt(mean(r_t²|r_t<0)) × sqrt(ppy))` |
| Maximum drawdown | `min((cum_t - cummax_t) / cummax_t)` over all t |
| Win rate | `count(r_long_gross > r_short_gross) / n_periods` |
| Geometric spread | `cum(1 + spread_gross)^(12/n) - 1` (annualised) |

**Forward return winsorisation.** Cross-sectional winsorisation at the 1st/99th percentile
is applied to forward returns before computing basket returns. This clips the most extreme
single-period token returns that arise from very thin order books or data errors, without
removing the directional information in the tail.

---

## 11. Results Comparison

### 11.1 Version evolution summary

| Metric | v1 | v2 (post-2021) | **v3 (post-2022)** |
|--------|:--:|:--:|:--:|
| Rebalancing periods | 108 | 40 | **39** |
| Coverage | 2017-2026 | 2022-2026 | **2022-2026** |
| Avg long basket | ~22 | 7.0 | **9.6** |
| Avg short basket | ~22 | 9.6 | **10.2** |
| Win rate | 58.3% | 55.0% | **56.4%** |
| Mean period spread | — | +2.34% | **+3.74%** |
| L/S Combined net (ann.) | +13.6% | −6.60% | **+1.82%** |
| Sharpe (combined) | +0.14 | −0.161 | **+0.051** |
| MaxDD (combined) | −78% | −64.3% | **−31.3%** |
| Bull geo spread | — | +4.0% | **+84.4%** |
| Bear geo spread | — | +15.0% | **+37.3%** |
| Sideways geo spread | — | +13.6% | +2.2% |

**Why v1 shows +13.6% combined net.** The 2017-2020 data contains many small-cap tokens
with near-zero supply inflation that happened to also have near-zero price movement (dead
projects). As long basket members, their near-zero returns appear favourable against an
active short basket of growing altcoins during non-mania periods. The full-history number
is flattering but not executable at any meaningful scale — see Section 12 below.

**Why v2 deteriorates to −6.60%.** Two main causes:
1. Phantom tokens (PAXG no perp, vBTC $0 volume) contaminated the short basket in v1,
   producing false short credits. Removing them correctly worsens the measured result.
2. v2 runs on the 2022-2026 window only (due to 26w history requirement + 13w supply
   window requiring 86+ eligible tokens), which includes the brutal 2022 bear market and
   the 2023-2024 bull run — both hostile to the strategy in different ways.

**Why v3 recovers to +1.82%.** The circuit breaker (4 triggers), altseason veto (1 trigger),
and wider composite-signal entry threshold all reduce the severity of the worst periods.
The +3.74% mean spread (vs +2.34% in v2) confirms the composite 13w+52w signal selects
a higher-quality spread than the pure 13w signal.

### 11.2 Regime-conditional behaviour

The strategy's fundamental regime dependency changed significantly from v2 to v3:

| Regime | v2 geo spread | v3 geo spread | Explanation |
|--------|:---:|:---:|------------|
| **Bull** | +4.0% | +84.4% | v3 CB + altseason veto blocks worst squeeze periods; inv-vol weights reduce high-beta short exposure |
| **Bear** | +15.0% | +37.3% | Both work well; v3 higher absolute but fewer periods; CB occasionally fires |
| **Sideways** | +13.6% | +2.2% | v3 composite signal less effective in moderate-inflation environments; wider entry threshold includes borderline tokens |

The Sideways regression is the most important finding for future work: the composite
signal trades Sideways alpha for improved Bull/Bear robustness. A regime-adaptive blend
weight (higher 13w weight in Sideways, higher 52w weight in Bull/Bear) could recover this.

---

## 12. Known Limitations

### 12.1 Capacity ceiling

With `MIN_VOLUME = $5M` daily and positions requiring a maximum of ~5% of daily volume
to avoid significant market impact, the practical AUM ceiling is approximately:

```
Capacity = avg_basket_size × avg_ADTV × max_pct_ADTV / 2_legs
         ≈ 10 tokens × $10M ADTV × 5% / 2
         ≈ $2.5M per leg → $5M total AUM
```

The strategy is not scalable above $5-10M AUM. Beyond this, position entry and exit begin
to account for a meaningful fraction of daily volume, causing slippage that erodes the
spread entirely.

### 12.2 Synthetic funding rate

The regime-based `FUNDING_8H` constants are rough medians. Real per-token funding rates
are highly variable (often 0.01% to 0.30% per 8h for altcoins in mania conditions).
The synthetic model understates short-leg funding costs in bull markets by an estimated
3-10×. This is the single largest cost-model error in the backtest.

**Required upgrade:** Replace `FUNDING_8H` with per-token historical funding rates from
the Coinglass API endpoint `GET /api/pro/v1/futures/funding-rate/history`.

### 12.3 BTC beta not fully neutralised

The documented BTC beta of the spread averages 0.541 (rolling 8-period estimate). Even
with the beta hedge applied, the residual alpha is negative. For a genuinely market-neutral
strategy, each basket's net BTC beta must be computed from individual token betas and
explicitly hedged. The current rolling portfolio-level approach is a first-order
approximation that captures the direction but not the full magnitude.

### 12.4 Survivorship bias

Tokens delisted from the top 300 are not tracked after exit. For the long basket (holding
low-inflation tokens), survivorship creates an upward bias: tokens that went to near-zero
are removed from future periods and do not inflict repeated losses. For the short basket,
survivorship creates a downward bias: a token that went to zero would be a perfect short —
but after delisting it exits the universe, cutting off the remaining profit.

Net effect: unclear direction, but the overall result is that both legs look better than
they would with a truly complete dataset.

### 12.5 Rebalancing drag and turnover

Average monthly turnover in v3 is ~88% per leg. This means the strategy effectively
replaces 88% of each basket every month, generating:

```
One-way annual turnover ≈ 88% × 12 = 1056% (each leg)
Annual cost from turnover ≈ 1056% × (0.04% fee + ~0.10% avg slippage) ≈ 1.48% per leg
```

The round-trip cost of 2× legs × 1.48% ≈ 3.0% annual drag is already captured in the
per-period fee and slippage deductions, but it highlights why a genuine implementation
would need to optimise turnover (e.g., using the inner buffer band more aggressively or
setting a minimum signal change threshold before triggering a rebalance).

### 12.6 Supply manipulation attack vectors

The circulating supply metric is susceptible to manipulation without genuine token
issuance. Known vectors documented in the codebase:

| # | Attack | Effect on signal |
|---|--------|-----------------|
| 1 | CMC reclassification | Team reclassifies treasury tokens as "circulating" → false inflation spike |
| 2 | Bridge minting double-count | WBTC minted = BTC locked + new token; both counted | Fixed by `WRAPPED_ASSETS` exclusion |
| 3 | Staking oscillation | ETH staked → CMC reduces ETH supply and adds stETH supply | Fixed by `LIQUID_STAKING` exclusion |
| 4 | Treasury reclassification | Ecosystem fund reclassified as circulating with no selling pressure |
| 5 | Token migration | v1→v2 migration briefly double-counts both tokens |
| 6 | Protocol receipt mechanics | vBTC/vETH accrues with interest (accounting artifact) | Fixed by `PROTOCOL_SYNTHETICS` exclusion |
| 7 | Airdrop timing | Teams time airdrops to fall just after the monthly rebalancing window | Partially mitigated by 13w window |
| 8 | Burn-and-reissue cycles | Cross-chain migration creates false deflation on one chain and inflation on another |
| 9 | LP token supply pollution | AMM LP tokens reported as underlying asset supply on some data providers |
| 10 | Float compression gaming | Team self-custodies tokens to manufacture controlled inflation patterns |

**Mitigation (for live deployment):** Use Glassnode on-chain circulating supply as the
primary signal source. CMC data should be used only as a validation layer. On-chain data
reflects actual token transfers from vesting contracts and is harder to reclassify
retroactively.

---

## 13. Data Sources Required for Live Deployment

### Tier 1 — Essential

| Source | Data | Endpoint |
|--------|------|---------|
| Binance USDT-M Futures | Daily OHLCV + funding rates per token | `GET /fapi/v1/klines`, `GET /fapi/v1/fundingRate` |
| Bybit Linear Perpetuals | Coverage for tokens not listed on Binance | `GET /v5/market/kline`, `GET /v5/market/funding/history` |
| CoinGecko Pro | Daily circulating supply (better than CMC for point-in-time) | `GET /coins/{id}/market_chart?vs_currency=usd&days=365` |
| Coinglass | Historical funding rates + Open Interest | `GET /api/pro/v1/futures/funding-rate/history` |

### Tier 2 — Important

| Source | Data | Use |
|--------|------|-----|
| Glassnode | Exchange inflows, on-chain supply | Primary supply source; leading sell-pressure indicator |
| Token Terminal | Protocol revenue | Revenue-to-Inflation Ratio (RER) overlay |
| DefiLlama (free) | Protocol TVL + fees | Proxy for RER; free alternative to Token Terminal |

### Tier 3 — Alpha Enhancement

| Source | Data | Use |
|--------|------|-----|
| Messari Pro | Token unlock calendars + vesting schedules | Distinguish scheduled cliffs from genuine dilution |
| TokenUnlocks.app | Forward-looking unlock calendar | Pre-positioning ahead of known cliffs |
| Nansen | Wallet labeling | Track VC and team wallet movements preceding unlocks |
| Dune Analytics | On-chain vesting contract outflows | T-1 signal before CMC records the supply change |

---

## 14. Script Reference

| File | Description |
|------|-------------|
| `perpetual_ls_backtest.py` | **v1** — baseline L/S backtest, 4w signal, equal-weight, 108 periods (2017-2026) |
| `perpetual_ls_v2.py` | **v2** — institutional-reviewed, 13w signal, ADTV-weighted, all exclusions applied, 40 periods post-2021 |
| `perpetual_ls_v3.py` | **v3** — composite 13w+52w signal, inv-vol weights, CB, altseason veto, BTC beta hedge, 39 periods post-2022 |

### Running the backtests

```bash
python perpetual_ls_backtest.py   # v1 baseline
python perpetual_ls_v2.py         # v2 institutional
python perpetual_ls_v3.py         # v3 fully risk-managed
```

Each script is self-contained and reads `cmc_historical_top300_filtered_with_supply.csv`
from the working directory. Output charts are written to the working directory.

### Key configurable parameters (v3)

```python
START_DATE           = pd.Timestamp("2022-01-01")  # backtest start (data still loaded fully)
MAX_RANK             = 200          # universe ceiling
MIN_VOLUME           = 5_000_000    # $5M daily volume gate
MIN_MKTCAP           = 50_000_000   # $50M market cap gate
SUPPLY_WINDOW        = 13           # fast signal (weeks)
SUPPLY_WINDOW_SLOW   = 52           # slow signal (weeks)
SIGNAL_SLOW_WEIGHT   = 0.50         # blend weight for slow signal
LONG_ENTRY_PCT       = 0.12         # enter long at 12th pct composite rank
LONG_EXIT_PCT        = 0.18         # exit long above 18th pct
SHORT_ENTRY_PCT      = 0.88         # enter short at 88th pct
SHORT_EXIT_PCT       = 0.82         # exit short below 82nd pct
SHORT_CB_LOSS        = 0.40         # circuit breaker: cap short loss at 40%
SHORT_SQUEEZE_PRIOR  = 0.40         # exclude new short entries with prior return > 40%
ALTSEASON_THRESHOLD  = 0.75         # veto shorts if >75% of top-50 alts beat BTC
BTC_HEDGE_ENABLED    = True         # add rolling BTC beta short overlay
BTC_HEDGE_LOOKBACK   = 12           # OLS rolling window (periods)
REGIME_MA_WINDOW     = 20           # weeks for Bull/Bear/Sideways MA
BULL_BAND            = 1.10         # index price > MA × 1.10 → Bull
BEAR_BAND            = 0.90         # index price < MA × 0.90 → Bear
HIGH_VOL_THRESHOLD   = 0.80         # BTC vol > 80% ann. → high-vol
```

---

## 15. Output Charts

| File | Description |
|------|-------------|
| `perp_ls_cumulative.png` | v1: cumulative wealth (log), drawdown, regime spread bar |
| `perp_ls_v2_cumulative.png` | v2: cumulative wealth (log), net legs, spread bar |
| `perp_ls_v2_regime_dd.png` | v2: per-period spread coloured by regime + drawdown |
| `perp_ls_v2_v1_comparison.png` | v1 vs v2: regime-conditional Sharpe comparison |
| `perp_ls_v3_cumulative.png` | v3: cumulative wealth (log), hedged vs unhedged, spread bar |
| `perp_ls_v3_regime_dd.png` | v3: regime spread bar + hedged vs unhedged drawdown |
| `perp_ls_v3_vs_v2.png` | v2 vs v3: 2×2 panel — mean spread, geo spread, NAV curves, scorecard |

---

## 16. Reports

| File | Description |
|------|-------------|
| `institutional_analysis_report.md` | Full institutional-grade post-trade analysis: exact formulas, data sources, execution assumptions, beta decomposition (BTC beta = 0.645), return distribution (kurtosis 30.37, skewness 4.71), sector bias |
| `risk_manager_teardown.md` | Senior QRM teardown: basket composition analysis ("Dead Project Cemetery" in long leg), PAXG filter failure, regime geometric returns, capacity analysis ($3-8M AUM ceiling), turnover (89.6% monthly) |
| `perp_ls_methodology.md` | This document |
