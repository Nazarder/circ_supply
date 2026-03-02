# Institutional-Grade Methodology & Post-Trade Analysis Report
## Supply-Dilution Long/Short Strategy — Perpetual Futures Execution

**Classification:** Internal Research — Quantitative Strategy Review
**Date:** 2026-03-02
**Dataset:** `cmc_historical_top300_filtered_with_supply.csv`
**Scripts Reviewed:** `extreme_percentile.py`, `backtest_v2.py`, `backtest_v3.py`, `perpetual_ls_backtest.py`
**Universe:** 135,652 rows | 2,267 unique symbols | 477 weekly snapshots | Jan 2017 – Feb 2026
**Author:** Lead Quantitative Researcher

---

## Executive Summary

The Supply-Dilution Hypothesis — that tokens with lower circulating supply inflation systematically outperform tokens with higher inflation — demonstrates a statistically significant and reproducible cross-sectional signal at the extreme decile level. Over 108 monthly rebalancing periods (Jan 2017 – Feb 2026), the long basket (bottom 10th percentile by 4-week supply inflation) produced a gross annualized return of **+38.1%** against the short basket's (top 90th percentile) **+5.6%**, generating a **+32.5 ppt gross spread** with a **58.3% monthly win rate**.

**The critical finding, however, is not the signal itself — it is its structural limitations:**

1. The L/S spread carries a **BTC beta of 0.64** and is therefore not market-neutral. The strategy is a levered crypto-beta bet with a supply-inflation tilt, not an absolute-return strategy.
2. The short leg, when implemented as an actual short position via perpetual futures, **went to -100% drawdown** as a standalone leg. All prior L/S configurations across V2 and V3 also went bankrupt.
3. **27.6% of the eligible universe** has less than $1M in daily 24h volume, rendering those positions untradeable at institutional size.
4. The funding rate model in `perpetual_ls_backtest.py` is entirely **synthetic** — no actual exchange data was used. This is the single most material assumption undermining the cost analysis.
5. The dataset covers a **structural secular bull market** in crypto (2017–2026). There is no evidence the alpha is independent of that macroeconomic regime.

The signal is real and exploitable — **exclusively on the long side**, as a smart-beta factor within a directional crypto allocation.

---

## Section 1: Precise Methodology & Mathematical Definitions

### 1.1 Inflation Calculation — Exact Formula

The supply inflation metric used in `perpetual_ls_backtest.py` is a **trailing 4-period (≈28-day) rate of change** of CoinMarketCap-reported circulating supply:

```
inflation_i,t = (CS_i,t - CS_i,t-4) / CS_i,t-4
```

where:
- `CS_i,t` = circulating supply of token `i` at weekly snapshot `t`
- `t-4` = four weekly snapshots prior (~28 calendar days, confirmed median hold = **28 days**)
- The window is a **simple trailing ratio**, not annualized, not continuously compounded

**What this metric measures:**
Cross-sectional variation in the rate at which new tokens enter the circulating supply. A value of +0.05 means the circulating supply grew 5% over the trailing 4 weeks. A value of -0.10 means the supply contracted by 10% (buybacks, burns, re-classifications, or lock-ups).

**What this metric does NOT measure:**
- Fully Diluted Valuation (FDV) / Market Cap ratio — FDV is not used anywhere in the pipeline
- Scheduled vesting schedules or token unlock calendars
- On-chain emission rates (the signal is derived from CoinMarketCap's reported circulating supply, not on-chain `Transfer` events)
- Annualized inflation rate (the raw 4-week rate is used directly for ranking, without annualizing)

**Empirically observed basket inflation levels:**
- Long basket (low inflation ≤ 10th pct): mean trailing-4w supply change = **-41.8%** on average
  *(This reflects a mix of deflationary tokens, buyback programs, supply re-classifications in CMC data, and possible data artifacts from staking lockups removing supply from CMC's circulating count)*
- Short basket (high inflation ≥ 90th pct): **median** trailing-4w supply change = **+26.2%**
  *(Median used because the mean is highly distorted by extreme outliers — tokens launching, unlocking large vesting tranches, or farming/staking reward emissions)*

**Data quality caveat:** The CoinMarketCap circulating supply series is a derived estimate, not a direct on-chain measurement. CMC computes it as `market_cap / price`, where both components carry independent reporting lag and caching artifacts. A 0.5% price feed lag combined with a 0.5% market cap delay can produce phantom supply changes of ~1%, which at the 4-week window scale becomes compound measurement noise. This noise contaminates the inflation ranking for tokens near the decile cut points.

**Prior test comparison:** `extreme_percentile.py` used a **13-week** (SUPPLY_WINDOW=13) trailing inflation window, producing a more stable annual inflation rate signal that reduced turnover. The 4-week window in `perpetual_ls_backtest.py` provides faster signal responsiveness but higher turnover and more noise exposure.

---

### 1.2 Ranking & Selection — Exact Mechanics

**Decile cut implementation:**

```python
lo_cut = universe["supply_inf"].quantile(0.10)   # pandas default: linear interpolation
hi_cut = universe["supply_inf"].quantile(0.90)

basket_long  = universe[universe["supply_inf"] <= lo_cut]   # <=, inclusive
basket_short = universe[universe["supply_inf"] >= hi_cut]   # >=, inclusive
```

**Tie handling:** `pandas.Series.quantile()` with method='linear' (default) produces a real-valued threshold via linear interpolation between adjacent rank values. Tokens are then selected by hard inequality comparison against this threshold. **Ties at exactly the cut point are all included** — if multiple tokens share the exact 10th percentile inflation value, they all enter the basket. In practice this means basket sizes vary around the nominal ~20 tokens, with `avg basket size = 20.2 tokens` per leg.

**Universe filters applied at each rebalancing date:**

| Filter | Exact Criteria | Approximate % of CMC Top 300 Excluded |
|---|---|---|
| Stablecoins | 34 symbols (USDT, USDC, DAI, FRAX, etc.) | ~5–8% |
| CEX platform tokens | 17 symbols (BNB, OKB, KCS, etc.) | ~3–5% |
| Memecoins | 53 symbols (DOGE, SHIB, PEPE, etc.) | ~8–12% |
| Mega-cap exclusion | rank ≤ 20 at snapshot date | Top 20 by market cap |
| Rank ceiling | rank > 250 at snapshot date | Tokens ranked 251–300+ |
| Missing supply | supply_inf must be non-NaN | Tokens with < 4 weeks of history |

**Critical gaps in the exclusion list:**

1. **Wrapped tokens (WBTC, renBTC, cbBTC, WETH):** NOT excluded. These have their supply tied to the underlying asset's bridge utilization, producing supply changes that are purely custodial mechanics, not economic dilution. Their inclusion pollutes both baskets.

2. **Liquid staking derivatives (stETH, rETH, cbETH, sfrxETH, osETH):** `sfrxETH`, `osETH`, and `cmETH` are in the stablecoin exclusion list under a "liquid staking derivative" category, but **stETH, rETH, cbETH, and LsETH are not excluded**. Their circulating supply changes reflect staking inflows/outflows, not inflationary token emissions.

3. **Protocol-controlled locked supply:** Tokens that move supply between "circulating" and "locked" categories on CMC (e.g., governance lockups, veTokens like veCRV) generate negative supply inflation signals while potentially facing future unlock pressure. These are the most dangerous false positives for the long basket.

---

### 1.3 Rebalancing Logic — Exact Mechanics

**Rebalancing date construction:**

```python
df["ym"] = df["snapshot_date"].dt.to_period("M")
rebal_dates = sorted(df.groupby("ym")["snapshot_date"].min().tolist())
```

This selects the **earliest weekly snapshot within each calendar month** as the rebalancing date. Given that CMC snapshots are weekly (Sunday-indexed), this corresponds to the first Sunday of each month, approximately. Over 108 months (Jan 2017 – Feb 2026), this produced a **median holding period of exactly 28 days** with variation between 21 and 35 days depending on the calendar alignment of month boundaries to weekly snapshot dates.

**Rebalancing is full replacement:** At each rebalancing date, the positions from the prior period are fully closed at the `price[t0]` (opening price of the new period), and new positions are opened at the same price. There is no gradual scaling in or out, and there is no intra-month portfolio adjustment for tokens that experience material news events.

**A structural gap:** Monthly rebalancing means the signal is 4-week lagged on entry. A token that begins aggressively diluting its supply mid-month will not trigger a basket exit until the next monthly rebalance, during which time the position continues to accumulate dilution exposure.

---

## Section 2: Data Sources & Integrity

### 2.1 Price & Supply Data

| Data Element | Source | Resolution | Coverage |
|---|---|---|---|
| Price (USD) | CoinMarketCap API exports | **Weekly** (Sunday snapshots) | Jan 2017 – Feb 2026 |
| Market Cap | CoinMarketCap | Weekly | Jan 2017 – Feb 2026 |
| Circulating Supply | CoinMarketCap (computed: `market_cap / price`) | Weekly | Jan 2017 – Feb 2026 |
| 24h Trading Volume | CoinMarketCap | Weekly (trailing 24h at snapshot) | Jan 2017 – Feb 2026 |
| CMC Rank | CoinMarketCap | Weekly | Jan 2017 – Feb 2026 |

**Critical note on circulating supply:** The dataset does not contain a directly sourced `circulating_supply` column from on-chain data. It is the CMC-reported circulating supply, which is itself a partially estimated metric that CMC derives from blockchain explorers, project disclosures, and internal heuristics. It is not equivalent to the on-chain `Transfer`-derived circulating supply available from Glassnode or Dune Analytics.

**The institutional-grade alternative:** Glassnode's `/v1/metrics/supply/current` provides daily on-chain circulating supply for ~150 tokens with <24h latency. The Messari Pro API offers structured token unlock calendars for ~400 tokens. Neither was used in this backtest.

### 2.2 Perpetual Futures Data

**No actual perpetual futures data was used.** This is the most significant limitation of the execution model.

The `perpetual_ls_backtest.py` script employs a **synthetic 8-hour funding rate model** based on approximate historical averages from Binance USDT-M and Bybit, parameterized as follows:

| Regime | Basket | 8h Rate Assumed | Source |
|---|---|---|---|
| Bull market | Long (low inflation) | +0.00800% | Approximated from Binance 2019-2024 avg |
| Bull market | Short (high inflation) | +0.01500% | Approximated from Binance 2019-2024 avg |
| Bear market | Long (low inflation) | +0.00200% | Approximated from Binance 2019-2024 avg |
| Bear market | Short (high inflation) | +0.00500% | Approximated from Binance 2019-2024 avg |

**Why these numbers are insufficient for institutional sign-off:**
- The assumed rates are **uniform cross-sectionally.** In reality, funding rates are highly token-specific. Tokens with concentrated short interest (e.g., around known unlock events) can sustain -0.05% per 8h (the exchange floor/cap) for weeks, meaning shorts receive funding at 5.5% APY equivalent. Tokens in manic uptrends can see funding spike to +0.10% per 8h for days at a time.
- The regime classification (Bull/Bear via 20-week MA) is a coarse approximation. Real funding rate regime dynamics are driven by the 8h mark price deviation from the index price at the token level, not at the broad market level.
- **Real data sources for replacement:** Coinglass API (`/api/pro/v1/futures/funding-rate/history`), Laevitas funding rate analytics, or direct Binance/Bybit WebSocket historical funding endpoint.

### 2.3 Survivorship Bias — Quantified Exposure

**The backtest has survivorship bias in two directions:**

**Direction 1 — Long basket contamination (understates long-side risk):**
Tokens that were in the top 250 at one point but subsequently crashed and delisted are excluded from all future snapshots. However, their final declining months before delisting ARE captured in the dataset (they appear until they drop below rank 300 and stop being tracked). The forward returns during those final months are highly negative and do flow through the backtest. This partial capture is better than pure survivorship bias but is not a complete delisting model.

**Direction 2 — Short basket contamination (overstates short-side profitability):**
High-inflation tokens that were delisted typically declined 70-95% before exit. These exits create profitable short opportunities that, in a real trading environment, **cannot be captured** because:
- The position cannot be closed at the last known price if the token trades only on illiquid DEXes
- The short becomes an open-ended liability when the token is delisted from a derivatives exchange
- CEX perpetuals are typically settled or expired at the last traded price — which may be at a deep premium or discount to fair value
- Exchange counterparty risk (FTX collapse, November 2022) can freeze positions entirely

The `clip(lower=-1.0)` hard floor on forward returns does not model this "debit risk" from an uncloseable short.

**Magnitude estimate:** The Elton-Gruber-Blake correction for survivorship bias in mutual fund databases estimates upward return bias of 150-400bps annually. In the crypto context, given much higher delisting rates (~15-25% of tokens in the Top 300 per year exit the universe), the bias is likely 500-1500bps annually on the short basket's reported gross return.

### 2.4 Missing Data Handling

**NaN values were handled with a single forward-fill:**

```python
df[col] = df.groupby("symbol")[col].transform(lambda s: s.ffill(limit=1))
```

`limit=1` means a gap of one weekly period (7 days) is filled with the prior value. Gaps of 2 or more consecutive weeks result in `NaN`, which excludes that token from the inflation calculation for those periods.

**Implication:** Tokens with erratic reporting (common in smaller-cap projects where CMC data feeds are unreliable) will have their supply change signal computed from stale data. A token that missed a weekly CMC snapshot will show zero supply change at the imputed week, then double the actual supply change at the next valid reading. This creates false signal in both the Z-score (V2 method) and the percentile ranking (current method).

---

## Section 3: Execution Assumptions & Frictions

### 3.1 Trading Fees

The backtest applies a **taker fee of 0.04% per side**, consistent with Binance VIP 0 / Bybit Tier 0 pricing for perpetual futures contracts. Round-trip cost per position is therefore **0.08%** (open + close over the holding period).

**What this misses:**
- Maker/taker dynamic: Large positions may require crossing multiple book levels, blending market (taker) and limit (maker) fills. Assuming pure taker fees is conservative but appropriate for a monthly strategy where you cannot reliably predict fill sequences.
- Gas fees for on-chain positions (not applicable for CEX perps, but relevant if any DeFi execution layer is used).
- Financing cost of posting margin collateral (the opportunity cost of USDT held as margin).

### 3.2 Slippage & Market Impact

The slippage model applied is:

```python
slippage_i = min(SLIPPAGE_K / turnover_i, MAX_SLIPPAGE)
           = min(0.0005 / (volume_24h_i / market_cap_i), 0.02)
```

This is an **inverse-turnover proxy** where lower daily turnover (volume / market cap) implies higher price impact. The cap is 200 bps (2%) per transaction.

**Universe liquidity profile:**

| Liquidity Percentile | 24h Token Volume | Position Feasibility |
|---|---|---|
| 10th percentile | $68,896 | Untradeable above ~$3K position |
| 25th percentile | $767,303 | Untradeable above ~$35K position |
| 50th percentile (median) | $6,281,338 | Feasible up to ~$300K position |
| 75th+ percentile | > $50M | Institutional sizing possible |

**27.6% of universe tokens have less than $1M in 24h volume.** At a 1-5% ADV execution limit (the institutional standard for avoiding material market impact), these positions would be capped at $10K–$50K each — inconsistent with any fund above $2M AUM.

**The slippage model fails in three ways:**
1. **Static, time-invariant:** It does not model the sharp liquidity withdrawal that occurs during market stress events. During a broad selloff, bid-ask spreads widen 3-10x and visible book depth contracts by 60-80%. The 200bps cap is non-binding in normal markets but severely underestimates crisis slippage.
2. **Volume ≠ depth:** A token may trade $10M/day via automated HFT while maintaining only $150K of visible depth within 2% of mid-price. Daily volume is a poor proxy for instantaneous book depth.
3. **Panic covering dynamics:** Closing a losing short into a momentum rally means buying the ask into rising prices with no offsetting offer flow. Realized slippage for $500K+ short covers can reach 10-30% of execution value, not 200bps.

**Almgren-Chriss square-root impact model (recommended replacement):**

```
MI = sigma_i * sqrt(Q_i / ADV_i) * eta
```

where sigma_i = daily vol, Q_i = position size ($), ADV_i = average daily volume ($), and eta ≈ 0.1 empirically. For a hypothetical token ranked 220 with ADV = $500K and sigma = 8%/day, executing $50K:

```
MI = 0.08 * sqrt(50,000 / 500,000) * 0.1 = 0.08 * 0.316 * 0.1 = 25 bps per side
```

This would be 50bps round-trip in normal conditions and is more realistic for mid-cap positions.

### 3.3 Funding Rate Drag — Detailed Breakdown

Over 108 monthly periods with the synthetic model:

| Component | Cumulative Impact |
|---|---|
| Funding drag on long leg (pays funding) | **-57.12%** |
| Funding credit on short leg (receives funding) | **+111.61%** |
| Net funding impact on combined strategy | **+54.49%** |
| Average net funding per period | +0.0050 (+0.50%) |

**Interpretation:** The synthetic model produces a *net favorable* funding outcome because the high-inflation short basket's assumed 8h rate (+0.015% Bull, +0.005% Bear) exceeds the long basket's rate (+0.008% Bull, +0.002% Bear). This is economically plausible — high-inflation tokens attract more speculative long interest, driving positive funding that shorts receive. However:

1. **This narrative inverts in altcoin seasons.** During Q1 2021 and Q4 2021, high-inflation tokens were the targets of retail speculative mania. Funding rates on these tokens spiked to +0.10% per 8h (maximum typical clamp). **Shorts were paying, not receiving, funding at 10x the modeled rate.** The April 2021 period, where the spread returned **-191.6%** in a single month, is direct evidence of this.

2. **Hard-to-borrow tokens exit perp markets.** When a token is actively being shorted by sophisticated players, exchanges delist or restrict the perp contract. The Binance/Bybit derivative universe covers approximately 200-300 tokens. The Q4 (high inflation) basket average size of 20 tokens implies 20 positions in the worst 10% by inflation — a significant fraction of these will not have liquid perpetual futures contracts.

3. **The funding model uses a fixed 30-day period assumption.** The actual monthly holding period in the backtest is 28 days (median), with variation of ±7 days. A 35-day month means 105 funding payments vs. 90 at 30 days — a 16.7% higher funding exposure than modeled.

**Gross funding drag as a % of gross alpha:**
Gross spread mean = 7.65% per month. Net funding impact = +0.50% per month (synthetic model).
Under the favorable synthetic model, funding represents +6.5% boost to the 7.65% gross spread.
Under the adverse scenario (altcoin season, shorts paying +0.10% per 8h for 30 days = 9% paid):
Net funding drag = -9% (shorts paying) - 0.72% (longs paying) = **-9.72% against a 7.65% gross spread → net loss of -2.07%.**

This is the mechanism by which the strategy's short leg went to -100% drawdown.

### 3.4 Liquidity Constraints — Position Sizing vs. Open Interest

No position sizing caps based on perpetual swap open interest or order book depth were implemented. The backtest assumes equal-weight positions can be entered at any size in all 40 basket tokens. In live execution:

- **Minimum OI requirement:** A $10M fund with 20-token equal-weight baskets requires $500K per position. Binance perp open interest for tokens ranked 150-250 is typically $5M–$50M. At $500K per position, the fund represents 1–10% of total OI — large enough to meaningfully move the funding rate against itself.
- **Forced OI-based sizing reduction:** In practice, positions should be capped at 2-5% of OI to avoid price impact and self-funded adverse rate dynamics. This implies a maximum fund AUM of approximately $1M–$5M for a strategy trading these token tiers at current market depth.

---

## Section 4: Risk, Exposure & Performance Attribution

### 4.1 Market Neutrality — Rolling Beta Analysis

**OLS Regression (4-week horizon, 108 monthly periods against BTC 4-week return):**

| Series | Alpha (4w, raw) | Beta vs BTC | Interpretation |
|---|---|---|---|
| Long basket (gross) | +0.150 per period | **+1.203** | Highly leveraged BTC exposure |
| Short basket (gross, as long) | +0.113 per period | **+0.559** | Moderate BTC beta |
| **L/S Spread (dollar-neutral)** | +0.038 per period | **+0.645** | NOT market-neutral |

**The strategy carries a BTC beta of 0.645 despite being dollar-neutral.** This arises mechanically because the long basket selects tokens with beta_BTC ≈ 1.20 while the short basket (which typically contains early-stage, high-emission projects) contains tokens with beta_BTC ≈ 0.56. The dollar notionals cancel, but the beta exposures do not.

**Practical implication:** In a month where BTC falls -40%, the spread is expected to lose:

```
Expected spread loss from BTC = 0.645 * (-0.40) = -25.8%
```

This means the strategy experiences a ~26% drawdown from pure market beta on every significant BTC correction — before the supply-dilution signal even acts.

**Rolling 12-period beta range:** [-1.18, +7.01], median -0.11. The extreme range indicates regime instability. In altcoin season periods, the short basket moves with 3-5x BTC beta (high-inflation tokens are the highest-beta assets in any bull cycle), while in bear markets, defensive low-inflation tokens have lower than typical beta. This means the realized net beta swings from negative (bear: long leg is defensive, short leg has normal beta → short beta > long beta → negative spread beta) to extreme positive (bull: short leg has 3x+ beta, long leg has 1.2x → spread beta could exceed +2.0). No static beta hedge addresses this.

### 4.2 Drawdown Analysis — Top 3 Events

**Combined gross spread drawdowns:**

| # | Period | Duration | Max Drawdown | Market Context |
|---|---|---|---|---|
| 1 | Apr 2021 (single period) | 1 month | **-191.6%** (spread) | Peak altcoin season — ETH 2.0 hype, DeFi/NFT explosion. High-inflation Layer-1s (LUNA, MATIC, SOL, AVAX) gained 100-400% in a single month, while low-inflation deflationary tokens posted flat to +20%. The short basket gained 191.6% more than the long basket in April 2021 alone. |
| 2 | Jan 2021 | 1 month | -43.7% | Early 2021 alt-season commencement. High-inflation DeFi yield tokens +100-200%; low-inflation established projects +20-50%. |
| 3 | Jun 2019 | 1 month | -52.2% | BTC halving anticipation rally. High-emission "generation 2" projects led the rally disproportionately. |

**Net combined portfolio drawdown (from `perpetual_ls_backtest.py`):** -78.0%

This divergence between the gross spread MaxDD and the net combined MaxDD reflects how the perpetual backtest structures the P&L differently from a pure spread. In the combined portfolio, the long leg's positive returns during drawdown periods partially offset the short leg's losses.

**The April 2021 event is the defining stress scenario for this strategy:**
A fund running this strategy at $10M AUM with 100/100 leverage in April 2021 would have experienced a drawdown of approximately $19M on the short leg alone in a single month — more than twice the fund's NAV — before any risk management intervention. This is a margin call scenario under any reasonable leverage constraint.

### 4.3 Return Distribution — Skewness & Kurtosis

| Series | Monthly Mean | Monthly Std | Skewness | Excess Kurtosis |
|---|---|---|---|---|
| Long basket (gross) | +22.28% | 104.73% | +4.69 | +24.20 |
| Short basket (gross) | +14.62% | 78.15% | +6.04 | +47.89 |
| **L/S Spread** | **+7.66%** | **64.62%** | **+4.71** | **+30.37** |

**Interpretation of the distribution shape:**

**Skewness = +4.71:** The spread return distribution has a pronounced right tail — there are occasional months of extraordinary spread performance (+200%+) driven by deflationary tokens massively outperforming in idiosyncratic moves. The distribution is NOT picking up "steady pennies" — the positive mean is generated by a minority of outlier months, not consistent monthly delivery.

**Excess kurtosis = 30.37:** This is approximately 30 standard deviations fatter than a Gaussian distribution's tail. A Gaussian would predict a 4-sigma event occurs once every 15,625 periods (~1,300 years). With kurtosis of 30, the fat-tail-adjusted 4-sigma event occurs far more frequently — the April 2021 event (-191.6% spread) is approximately a 3-sigma event by this distribution. Extreme drawdown events will occur multiple times per decade.

**The Sharpe ratio is misleading at face value.** The combined net Sharpe of 0.14 is computed assuming normally distributed returns. The Lo (2002) heteroskedasticity-and-autocorrelation-consistent correction with lag-1 autocorrelation of the long basket (+0.28) and near-zero lag-1 autocorrelation of the spread (+0.025) would adjust the spread Sharpe modestly, but the kurtosis of 30 means the correct risk metric is tail risk (CVaR/Expected Shortfall at 95% confidence), not Sharpe.

**Autocorrelation of the spread (lag-1 = +0.025):** Near-zero autocorrelation means the spread returns are not momentum-persistent — past outperformance of low-inflation tokens does not predict next-month continuation. This is both methodologically important (no simple momentum filter can improve the signal) and risk-relevant (drawdowns are not mean-reverting on a 1-period basis).

---

## Section 5: Strategy Vulnerabilities & Next Steps

### 5.1 Tail Risks — Structural Threats in Live Execution

**Risk 1: Short Squeeze on Low-Float High-Inflation Tokens**
High-inflation tokens in the short basket are frequently early-stage projects with:
- Total circulating supply < 10-20% of max supply (the remaining 80-90% is in vesting/treasury)
- Concentrated ownership (top 10 wallets control 60-80% of float)
- Active Telegram/Discord communities with documented history of coordinated squeezes

When a token's short interest becomes visible (through DeFi lending utilization dashboards, Coinglass OI data, or on-chain monitoring), retail coordination can push the price up 300-500% in hours. The inverse-volatility weighting in earlier iterations (V2/V3) would have *increased* position size in these "stable" high-inflation tokens — precisely the names where squeeze risk concentrates. Equal weighting in `perpetual_ls_backtest.py` is marginally better but provides no squeeze protection.

**Magnitude:** A 5-weighted-position squeeze to +500% loss wipes 25% of the short leg NAV in a single session. This is not theoretical — examples: JELLY/HYPERLIQUID (March 2025), GME-analog events on altcoins, MEW, WIF in 2024.

**Risk 2: Exchange Counterparty Risk and Forced Liquidation**
The entire strategy is operationally dependent on major derivatives exchanges. FTX's collapse (November 2022) demonstrated that:
- Positions can be frozen with zero notice
- Settlement prices may be set by the exchange at distorted levels
- Margin collateral (USDT) may be rehypothecated and lost
- Insurance funds may be insufficient for catastrophic events

A strategy holding 40 simultaneous perpetual futures positions across potentially 2-3 exchanges (for best liquidity) concentrates operational risk in counterparties that are unregulated, opaque about their reserve status, and subject to contagion from related entities.

**Risk 3: Liquidation Cascades During Broad Derisking**
During acute market stress (BTC -15% in 24h), cross-margined perpetual positions face simultaneous margin calls. The 40-token short basket — all of which are high-beta altcoins — will move against the short position simultaneously. Unlike equity options where you can hedge with index instruments, no deep, liquid altcoin index derivative exists. The hedge for a basket of 40 altcoins is approximate at best.

**Risk 4: Regime Shift — Crypto Entering a Structural Bear**
The entire dataset (2017–2026) constitutes a secular bull market. The "bear" periods identified by the 20-week MA regime filter are **corrections within a rising market, not genuine secular bears.** The +38.1% annualized long basket return decomposed into:
- BTC beta component: 1.20 × BTC_return ≈ 1.20 × ~60% = ~72% (BTC's annualized return over this period)
- Alpha component: 4-week alpha = 0.150, annualized ≈ 15–20%

If crypto enters multi-year price stagnation or decline (as traditional assets do), the long basket's beta-driven return disappears, leaving only the residual alpha — which at 15-20% annualized is positive but insufficient to justify the 149-297% annualized volatility of the basket.

**Risk 5: Signal Crowding and Information Set Deterioration**
The supply-dilution hypothesis is now publicly documented in this repository and in the broader quant crypto research community. As more systematic players implement similar supply-inflation screens:
- The alpha in the long tail will compress as capital concentrates into the same low-inflation names
- Front-running of known low-inflation tokens at rebalance dates will increase entry costs
- High-inflation tokens will have their short squeeze risk amplified by concentrated institutional short interest

The signal is most valuable while obscure and least actionable once crowded.

---

### 5.2 Sensitivity Analysis — Parameter Stress Tests

The following parameters have the highest impact on reported strategy performance and should be swept systematically before drawing deployment conclusions:

**Priority 1 — Inflation window (SUPPLY_WINDOW)**
Test range: [2, 4, 8, 13, 26, 52] weeks.
The 4-week window maximizes signal turnover and recency but is most susceptible to CMC data noise. The 13-week window (used in `extreme_percentile.py`) produced more stable basket composition and lower turnover. A walk-forward test across windows (train: 2017–2021, test: 2022–2026) should identify the optimal window that maximizes out-of-sample Information Ratio.

**Priority 2 — Percentile cut (LOW_PCT / SHORT_PCT)**
Test range: [5%/95%, 10%/90%, 15%/85%, 20%/80%].
The signal is a tail phenomenon — the spread should monotonically increase as the percentile cut tightens. However, tighter cuts reduce basket size (5th percentile yields ~10 tokens per basket), increasing idiosyncratic risk. Sharpe-maximizing cut likely exists around 10–15th percentile.

**Priority 3 — Rebalancing frequency**
Test: [biweekly, monthly, quarterly].
Monthly rebalancing was chosen to match the prompt specification. Quarterly rebalancing reduces turnover costs but allows greater signal decay. A transaction-cost-aware efficient frontier across rebalancing frequencies should be produced.

**Priority 4 — Taker fee sensitivity**
Stress scenarios: [0.02%, 0.04%, 0.06%, 0.10%].
At 0.10% taker fee (applicable to new/small accounts, or in volatile markets where limit orders are rejected and market orders must be used), the 0.08% round-trip assumption becomes 0.20% — a 2.5x increase in friction. At 40 tokens per rebalance × 2 sides × 0.20% = 1.6% per rebalance × 13 rebalances = 20.8% annually in pure fee drag.

**Priority 5 — Universe rank range**
Test: [Top 100, Top 150, Top 250, Top 300, uncapped].
The current configuration (rank 21–250) includes 229 eligible tokens before the supply/exclusion filters. Restricting to rank 21–100 would substantially improve the liquidity profile of the universe (median volume likely 10x higher) while potentially sacrificing some signal breadth.

**Priority 6 — Real funding rate sensitivity**
Replace synthetic rates with actual historical funding rate data from Coinglass. Test scenarios:
- Best case: shorts receive average +0.015%/8h (favorable carry)
- Base case: 0.01%/8h mixed (currently modeled approximately)
- Stress case: shorts pay 0.03%/8h during altcoin seasons (Q1 2021, Q4 2021, Q1 2024)

The adverse funding scenario during the 2021 altcoin season likely reduces the full-period net return to near-zero or negative. The strategy may only be viable with active funding rate monitoring and position reduction when perp funding flips against the short book.

---

## Summary: Key Findings Table

| Dimension | Measured Value | Interpretation |
|---|---|---|
| Gross spread (annualized) | +32.5 ppt | Signal present and significant |
| Win rate | 58.3% (63/108) | Consistent directional edge |
| Net combined return | +13.6% annualized | After synthetic costs |
| BTC beta of spread | **+0.645** | NOT market-neutral — major hidden risk |
| Short leg standalone | **-100% MaxDD** | Short leg permanently non-viable |
| April 2021 single-month loss | **-191.6%** | Fund-ending event in live execution |
| Spread excess kurtosis | **30.4** | Extreme fat tails; Sharpe is misleading |
| Funding rate model | **Synthetic only** | No actual exchange data — core gap |
| Survivorship bias | Partially unaddressed | Short-side returns overstated |
| Universe liquidity (10th pct) | $68,896 daily volume | 27.6% of universe untradeable |
| Wrapped/LSD tokens | Not excluded | False signal contamination |
| Deployable implementation | Long-only, decile filter | Short leg is a capital destroyer |

---

## Section 6: Recommended Deployment Architecture

Based on all findings, the only defensible deployment of this research is a **long-only, smart-beta factor strategy** with the following specifications:

1. **No short leg in any form.** The data across all nine tested configurations is unambiguous — the short leg destroys capital in every unconditional structure. The sole exception (Bear-Only L/S) survives but generates near-zero alpha after real costs.

2. **Long the 10th–15th percentile** of 13-week supply inflation (not 4-week — more stable signal with lower turnover and less noise contamination).

3. **Universe filter:** Top 21–150 by market cap (removes liquidity constraints), explicit exclusion of wrapped tokens and LSDs.

4. **Minimum liquidity gate:** $5M daily 24h volume at snapshot date. Hard exclusion below this threshold.

5. **Minimum supply history:** 26 weeks of uninterrupted circulating supply data before eligibility (eliminates new token launch artifacts).

6. **Position weight cap:** 8% per token (inverse-volatility weighted within that cap, preventing over-concentration in "stable" but potentially illiquid names).

7. **Momentum overlay:** Exclude any token in the bottom quintile of 4-week price momentum within the low-inflation basket. Combines supply fundamental with price trend confirmation.

8. **Regime overlay:** Reduce all position sizes by 50% when the broad crypto market (BTC + ETH market cap proxy) is below its 20-week MA. The signal operates in both regimes but with materially higher volatility in bear markets.

9. **Real-time monitoring:** Subscribe to Glassnode exchange inflow alerts and TokenUnlocks.app calendar for any token in the long basket. Exit preemptively if a confirmed upcoming unlock event is detected for a current holding.

---

*Report generated 2026-03-02 by Lead Quantitative Researcher.
Based on full reproduction of all four backtest scripts against the complete dataset.
This report does not constitute investment advice. Past backtest performance is not indicative of future results.*
