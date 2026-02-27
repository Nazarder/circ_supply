# Quantitative Research Critique & Strategy Upgrade Roadmap
**From Basic Backtest to Deployable Alpha — Senior QR Review**

---

## Part 1 — Methodological Tear-Down

### 1.1 Data Granularity & Lags: The Cost of Weekly Blindness

The single most structurally damaging decision in the current setup is using weekly CMC snapshots as a proxy for supply changes. This compounds three separate measurement errors simultaneously.

**Error 1: The circulating supply estimator is doubly noisy.**
You are computing `circulating_supply = market_cap / price`. Both the numerator and denominator carry independent measurement error from CMC's data ingestion pipeline (delayed reporting, stale feeds, API caching). The division amplifies both noise sources multiplicatively. A 0.5% CMC price feed lag combined with a 0.5% market cap reporting lag can produce a phantom supply change of ~1% — comparable in magnitude to a genuine small emission. At the 3% threshold this matters less, but the rolling-median baseline is calibrated on this noisy series, which means your `baseline_emission` estimate is itself noisy.

**Error 2: Weekly granularity destroys the unlock timing signal.**
On-chain vesting contracts execute at block time (seconds). The actual supply release hits the circulating supply metric between snapshot T and T+1 — but *where* in that 7-day window is unknown. A token that unlocked on Monday vs Friday relative to the Sunday CMC snapshot will show the same T=0 flag, but the post-event price window has already begun decaying. You are effectively adding +-3.5 days of timing jitter to every event. For a signal whose entire post-event window is only +-14 days, that is 25% timing noise by construction.

**Error 3: You are measuring price impact ex-post, not alpha opportunity ex-ante.**
With daily on-chain data, the pipeline looks like:

```
Block event (vesting contract Transfer) --> T_actual
CMC weekly snapshot reflects it         --> T_actual + [0, 7] days of lag
Your T=0 detection                      --> T_actual + [7, 14] days (next snapshot)
```

In the absolute best case, your T=0 detection lags the real-world event by one full week. The T-2 pre-event decline you observed is almost certainly partly a detection artifact — those tokens were already in post-unlock price decay when you classified them as "pre-event."

**What to build instead:**
The correct data architecture is:
1. **Dune Analytics**: Query ERC-20 `Transfer` events from known vesting contract addresses. A vesting cliff shows up as a large transfer from a tagged contract (`0xVesting...`) to a team wallet or exchange deposit address. Latency: ~2 hours from block confirmation.
2. **Glassnode API** (`/v1/metrics/supply/current`): Daily circulating supply per token with proper on-chain sourcing. Cost: ~$29/month for mid-tier plan covering most assets.
3. **The Messari Pro API**: Provides structured token unlock calendars for ~400 tokens, letting you cross-reference inferred unlocks (your current method) against the published schedule.

Moving to daily data alone — without on-chain integration — tightens the event window to T-1 to T+1 (3 days total) and reduces timing jitter by roughly 7x. You would expect the ACAR signal to strengthen materially because you are catching the price impact closer to its actual origin.

---

### 1.2 Signal Construction: Why Your Threshold is Statistically Naive

The current trigger condition is:

```
supply_spike = supply_pct_1p - rolling_median(12p)
Flag if: supply_spike > 0.03
```

This is an absolute threshold applied to a rate-of-change series. It has three structural failures.

**Failure 1: Volatility blindness across the cross-section.**
A token like Chainlink that reliably emits ~0.2% per week has supply volatility of perhaps 0.05% per week. A DeFi yield token that emits erratically might have supply volatility of 2% per week. Your 3% threshold flags a 15-sigma event for LINK and a 1.5-sigma event for the DeFi token — treating them as equivalent "large unlocks" when they are categorically different events.

**The correct formulation is a volatility-scaled Z-score:**

Define the supply spike in standard-deviation units:

```
z_t = (delta_S_t - median_12) / sigma_hat_12
```

where:
- `delta_S_t` = supply_pct_1p at time t
- `median_12` = rolling 12-period median (robust to outliers)
- `sigma_hat_12` = 1.4826 * MAD_12(delta_S_t)

The factor 1.4826 makes the MAD a consistent estimator of the Gaussian standard deviation. This is the Rousseeuw-Croux estimator, standard in robust statistics. Flag if z_t > 2.5 (adjustable; cross-validate on held-out years).

**Why MAD, not rolling std?** Rolling standard deviation is inflated by the very spikes you are trying to detect — a previous large unlock raises `rolling_std`, which lowers the z-score for subsequent unlocks in the same cooldown window. MAD is breakdown-point 50%, meaning it remains unaffected as long as fewer than half the observations are contaminated.

**Failure 2: The rolling window is arbitrary and untested.**
The 12-period (~3-month) window is not validated against any performance criterion. Too short, and you confound the spike signal with recent normal emission variance. Too long, and you fail to adapt to regime changes in the token's emission schedule (e.g., a halving event or a protocol parameter change). You should run a **walk-forward hyperparameter sweep**:

```python
for window in [4, 8, 12, 16, 26, 52]:
    for threshold in [1.5, 2.0, 2.5, 3.0]:
        # compute events, ACAR, Sharpe of H1 strategy
        # on 2017-2020 training set, evaluate on 2021-2026 test set
```

Select the (window, threshold) pair that maximizes out-of-sample Information Ratio, not in-sample ACAR.

**Failure 3: No structural break detection.**
Your rolling-median baseline treats all 12 prior observations as equally informative. But a token that migrated from one vesting contract to another (a common event during protocol upgrades) will show a permanent step-change in its baseline emission. A robust alternative is **CUSUM (Cumulative Sum Control Chart)** for supply change detection:

```
C_t+ = max(0, C_{t-1}+ + delta_S_t - mu_0 - k)
```

where `mu_0` is the in-control mean, `k = delta/2` is the slack (half the shift to detect), and you signal when `C_t+ > h` (the decision threshold). CUSUM is optimal by the Wald sequential probability ratio test for detecting a mean shift of size delta. The `ruptures` Python package implements this with `ruptures.Pelt` for offline detection and `ruptures.Binseg` for real-time monitoring.

---

### 1.3 Outlier Handling: What Hard Clipping Actually Does to Your Distribution

The current code clips `pct_return` at +-100% per period. This is analytically dangerous for three reasons.

**Reason 1: It creates artificial mass at the boundary.**
Crypto returns in the altcoin universe have a Pareto-distributed right tail and a left tail that frequently reaches -95% to -100% (complete death). Hard clipping at -100% is nearly non-binding (a token can only lose 100% of its value), but clipping at +100% on weekly data is extremely binding during bull markets. A token that 4x'd in one week gets clipped from +300% to +100%. This creates a synthetic pile-up of observations at exactly +100% that does not reflect the true distribution and biases downward the mean return of high-momentum baskets.

**Reason 2: It destroys cross-sectional information in the ranking.**
For the quartile portfolios, what matters is the *rank order* of tokens, not their cardinal return values. A return of +50% and a return of +300% both map to "very good" and should be in Q4 of the momentum sort — but after clipping, the +300% token looks identical to a +100% token and different from a +50% token, inverting the rank for the top of the distribution.

**Reason 3: The true risk profile is masked.**
Crypto token distributions have excess kurtosis in the range of 20-100, far outside Gaussian assumptions. By clipping, you are reporting an artificially platykurtic (thin-tailed) distribution. This makes Sharpe ratios and VaR estimates appear more favorable than reality.

**Better alternatives, in order of sophistication:**

**Option A — Cross-sectional rank transform (best for quartile sorting):**
At each snapshot date, convert returns to their cross-sectional percentile rank [0, 1]. This is the standard in academic factor research (Jegadeesh & Titman 1993). No information is lost about rank order, outliers are automatically handled, and the returns become uniformly distributed cross-sectionally.

```python
df['ret_rank'] = df.groupby('snapshot_date')['pct_return']\
                   .transform(lambda s: s.rank(pct=True))
```

**Option B — Signed log-modulus transform (best for preserving scale while compressing tails):**
```
r_tilde = sign(r) * log(1 + |r|)
```
This is a bijective transformation that maps all real numbers to all real numbers, compresses large values non-linearly, is symmetric, and has no arbitrary clipping boundary. A +300% return maps to log(4) ~= 1.39; a +100% return maps to log(2) ~= 0.693; a +10% return maps to log(1.1) ~= 0.095. The ordering is preserved, the scale is meaningful, and the transform is fully invertible.

**Option C — Winsorization at cross-sectional percentiles (best for the event study):**
Rather than a fixed +-100% bound, winsorize at the 1st and 99th percentile *within each snapshot date*. This adapts the bound to the current distribution rather than imposing a static one from 2017 that may be miscalibrated for a 2025 market.

---

### 1.4 Annualization Artifacts: The Exact Mathematical Problem and Correction

The current annualization formula is:

```
r_ann = (product of (1 + r_t) for t=1..N) ^ (52/N) - 1
```

where N ~= 108 monthly observations and r_t is the 4-week forward return at each monthly rebalancing date.

**The bug:** The exponent 52/N = 52/108 ~= 0.481 is wrong. Each r_t is a 4-week return (approximately 1 monthly period). There are approximately 12 monthly periods in a year. Therefore the correct exponent is:

```
r_ann = (product of (1 + r_t)) ^ (12/N) - 1
```

**The volatility underestimation problem (Lo, 2002):**
When returns are computed from overlapping windows, the resulting series exhibits positive autocorrelation even if the underlying true returns are i.i.d. The standard deviation of an autocorrelated series underestimates true risk. The Lo (2002) correction for annualizing a Sharpe ratio estimated from a return series with lag-q autocorrelation rho_q is:

```
SR_annual = SR_period * sqrt(T_periods_per_year) / sqrt(1 + 2 * sum(rho_q * (1 - q/Q)))
```

For an AR(1) process with autocorrelation rho, the correction factor simplifies to:
```
correction = 1 / sqrt(1 + 2*rho / (1 - rho))
```

**Practical correction in Python:**
```python
def annualized_sharpe_lo_corrected(returns, periods_per_year=12, max_lags=4):
    sr_period = returns.mean() / returns.std()
    autocorrs = [returns.autocorr(lag=q) for q in range(1, max_lags + 1)]
    weights = [1 - q / max_lags for q in range(1, max_lags + 1)]
    correction = 1 + 2 * sum(rho * w for rho, w in zip(autocorrs, weights))
    return sr_period * np.sqrt(periods_per_year) / np.sqrt(correction)
```

The Newey-West heteroskedasticity-and-autocorrelation-consistent (HAC) estimator of variance — available via `statsmodels.stats.sandwich_covariance.cov_hac` — is the institutional standard for computing standard errors on overlapping return series.

---

## Part 2 — Blind Spots & Execution Realities

### 2.1 The Shorting Mechanism: Your Alpha is Partly Borrowed Away

The H1 event study suggests a -2.5% ACAR over +-2 weeks. Before celebrating, price out the cost of achieving that short:

**Spot margin borrow (CEX):** On Binance or Bybit, small-cap token borrow rates for isolated margin range from 30% to 300% APR when supply is tight. For a 4-week holding period, the borrow cost at 150% APR is:

```
Borrow cost (4 weeks) = (1 + 1.50/52)^4 - 1 ~= 11.5%
```

This alone would consume the entire -2.5% ACAR on the short side. Many tokens in the Top 300 tail are simply not available for spot borrow at any price.

**Perpetual futures (the practical alternative):**
The actual execution vehicle for this strategy is perpetual swaps (perps), not spot borrow. Perp funding rates serve as the economic substitute for borrow cost. The funding rate is paid every 8 hours:

```
Funding_t = Clamp((P_perp - P_spot) / P_spot, -0.05%, +0.05%)
```

When the market is heavily short-biased (e.g., around a major unlock that everyone can see coming), funding rates go **negative** — shorts *receive* funding from longs. This is the efficient scenario where the alpha is already being extracted. When funding is positive (bulls paying bears), a short position pays funding continuously.

Historical funding rate data is available from:
- **Coinglass API** (`/api/pro/v1/futures/funding-rate/history`)
- **Laevitas** funding rate analytics
- Direct exchange APIs (Binance, Bybit, OKX)

**Model integration:**
```
net_return_H1_short = ACAR_gross - avg_borrow_cost - slippage - entry_exit_spread
```

For a rigorous backtest, load historical funding rates per token-date and subtract them from the event-window return for each event. Based on the current -2.5% ACAR over 4 weeks, and typical small-cap perp funding of 0.01-0.05% per 8h (~1.2-6% over 4 weeks), the strategy is marginal to unprofitable for the illiquid tail after funding costs.

**The corollary implication:** The H1 signal is likely only actionable for tokens with liquid perp markets — approximately the top 50-80 by volume on major derivatives exchanges. The bottom 150 of the Top 300 universe may have no viable short vehicle at all.

---

### 2.2 Market Microstructure: Slippage in the Illiquid Tail

The current backtest implicitly assumes you can enter and exit any position at the observed closing price with zero market impact. For tokens ranked 200-300, this is wildly unrealistic.

**Quantifying market impact (Almgren et al. 2005 square-root model):**

```
MI = sigma * sqrt(Q / ADV) * eta
```

where:
- sigma = daily volatility of the asset
- Q = order size in dollars
- ADV = average daily volume in dollars
- eta ~= 0.1 (empirically estimated coefficient)

For a hypothetical token ranked 250 with ADV = $500k and sigma = 8% daily, executing a $50k position gives:

```
MI = 0.08 * sqrt(50,000 / 500,000) * 0.1 = 0.08 * 0.316 * 0.1 ~= 0.25%
```

That is 25 basis points of slippage on entry and roughly the same on exit — 50bps round-trip just from market impact, on top of a 20-50bp bid-ask spread. For a 4-week event with -2.5% ACAR, you are consuming 30-80% of your gross alpha in transaction costs alone.

**The Amihud illiquidity ratio as a position-sizing input:**
Your dataset includes `volume_24h`. This allows you to compute, per token per snapshot:

```
ILLIQ_i,t = |r_i,t| / V_i,t
```

where r_i,t is the absolute return and V_i,t is dollar volume. The time-series average of ILLIQ is a proxy for Kyle's lambda (price impact per dollar of order flow). Use this to:
1. Exclude from the tradeable universe any token where mean(ILLIQ) > threshold
2. Scale position sizes inversely to ILLIQ
3. Apply a liquidity-adjusted ACAR that subtracts estimated round-trip cost proportional to ILLIQ

**Minimum viable liquidity filter for a $1M strategy:**
As a rule of thumb, position size should not exceed 1-5% of ADV to avoid meaningful market impact. If targeting a $20k position per token, you need ADV > $400k-$2M. This filters out a substantial fraction of the bottom-ranked tokens.

---

### 2.3 Survivorship Bias: Modeling Delisting Without Look-Ahead Bias

The current approach has a specific, underappreciated look-ahead bias not in the return data but in the **quartile assignment**. Here is the mechanism:

At rebalancing date T, you assign a token to Q1 or Q4 based on its supply metrics. For a token that was delisted six months later, you correctly include its poor returns in the forward-return calculation. However, the quartile assignment itself uses the token's rank among all tokens at snapshot T — including tokens that will survive for the full 9 years. The composition of the Q4 basket has been implicitly selected to include tokens that were still present in the dataset at that snapshot date. Tokens that died earlier are not in the ranking pool, which means the universe used for cross-sectional quartile assignment gets progressively smaller and more survivorship-biased over time.

**The correct survivorship-bias-aware approach:**

1. Build a **point-in-time (PIT) universe** by tagging each token with its entry and exit date from the Top 300. A token's observations are valid only between these two dates.

2. When a token exits the dataset (rank drops below 300 or it is delisted), record the exit as a "forced liquidation" at the last available price.

3. For the event study, ensure that the T+2 return window for events near a token's exit date uses the actual exit price, not forward-filled values.

4. Compute a **delisting return adjustment**. Tokens that exit the top 300 typically lose 70-95% of their peak value over the following 12 months. Estimate the bias by computing the performance difference between the full universe (including tokens that exited) and a surviving-only subset. Use this as a downward adjustment to reported gross returns (the Elton-Gruber-Blake correction).

---

### 2.4 The "Priced In" Problem: Scheduled vs. Surprise Unlocks

The monotonically declining ACAR from T-2 onward — with no acceleration at T=0 — is the clearest signal that the current event definition is mixing two fundamentally different populations:

**Population A: Scheduled, publicly known vesting cliffs.**
These are published in the token's whitepaper, on Messari, TokenUnlocks.app, or TGE documentation. Sophisticated market participants begin positioning weeks to months before the on-chain unlock. By the time the supply change appears in CMC data, the price impact has already been realized. Your T=0 detection is arriving at the crime scene after the investigation is complete.

**Population B: Unscheduled team/treasury dumps.**
A founding team decides to liquidate treasury holdings. A VC fund hits its lockup expiration and sells. A protocol emergency causes an unexpected emission. These events are not foreseeable from public documents and should produce a larger and more concentrated price impact around T=0.

**How to separate them:**

1. **Messari / TokenUnlocks.app API integration**: Build a lookup table of (token, expected_unlock_date, expected_amount). Classify each detected supply event as "scheduled" (within +-2 weeks of a known cliff) or "unscheduled" (no matching scheduled event within +-4 weeks).

2. **Re-run the H1 event study separately for each population.** Hypothesis: scheduled events should show steeper T-4 to T=0 slope (longer front-running) and flat T=0 to T+2 (all impact priced in). Unscheduled events should show flat T-4 to T=0 and steeper T=0 to T+2.

3. **On-chain leading indicator**: Monitor the number of outbound transfers from known vesting contract addresses in the 2-4 weeks *before* the supply change hits CMC. A rising transfer count from a vesting address before a cliff is a direct observable signal of forthcoming unlock pressure — allowing you to move from T=0 CMC detection to T-2 or T-3 on-chain detection for scheduled events.

4. **The information-theoretic framing**: Your strategy is profitable to the extent that your detection has information content the market has not fully incorporated. The current evidence suggests the market *has* front-run scheduled cliffs but may *not* have fully front-run unscheduled dumps. Alpha accrues to the latter bucket.

---

## Part 3 — Strategy Architecture Upgrades

### 3.1 On-Chain Data Integration: Getting Ahead of CMC by 7-14 Days

The architecture for a real-time on-chain unlock detector has the following components:

**Layer 1 — Contract taxonomy:**
Build a mapping of `{token_address: vesting_contract_addresses}` for each token in your universe. Sources:
- Etherscan contract labels API
- Nansen's entity labeling (tags addresses as "Project: Treasury", "VC: Multicoin", etc.)
- Manual review of TGE documentation for the top 100 tokens by market cap

**Layer 2 — Event stream monitoring (Dune Analytics):**
```sql
SELECT
    evt_block_time,
    contract_address AS token,
    "from"           AS sender,
    "to"             AS recipient,
    value / 1e18     AS amount
FROM erc20_ethereum.evt_Transfer
WHERE "from" IN (SELECT vesting_address FROM your_vesting_table)
  AND value / 1e18 > (
      SELECT threshold FROM your_thresholds
      WHERE token = contract_address
  )
  AND evt_block_time > now() - interval '7 days'
ORDER BY evt_block_time DESC
```

This query, run on a 1-hour schedule via the Dune API (`dune-client` Python package), surfaces vesting contract outflows before they appear in CMC's circulating supply.

**Layer 3 — Exchange inflow monitoring (Glassnode):**
The critical signal is whether tokens moving *out* of vesting contracts are flowing *into* exchange deposit addresses. Glassnode's `exchange_inflow` metric, available at hourly resolution for ~150 tokens, measures exactly this. A spike in exchange inflow from a vesting-adjacent wallet is a sell-pressure signal that precedes actual price impact by hours.

**Layer 4 — Signal aggregation:**
```python
score_i = (
    w1 * z_score_supply_change       # CMC-inferred (current method, lagging)
  + w2 * vesting_contract_outflow    # on-chain early warning (hours lag)
  + w3 * exchange_inflow_spike       # sell-pressure confirmation (hours lag)
  + w4 * scheduled_unlock_proximity  # calendar-based prior (days in advance)
)
```
Weights optimized via elastic-net regression on the training set (2017-2021), validated on 2022-2026.

**Python pipeline:**
```
web3.py          --> direct node queries for contract events
dune-client      --> Dune Analytics API
glassnode-sdk    --> Glassnode metrics API
messari-api      --> unlock calendar data
pandas + asyncio --> async data aggregation
APScheduler      --> hourly job scheduling
```

---

### 3.2 Dollar-Neutral, Beta-Hedged Long/Short Architecture

**Step 1 — Factor model for beta estimation.**
For each token i, estimate the 2-factor market model via rolling OLS over the trailing 60 periods:

```
r_i,t = alpha_i + beta_i_BTC * r_BTC,t + beta_i_ETH * r_ETH,t + epsilon_i,t
```

This gives each token a BTC-beta and ETH-beta. The residual epsilon_i,t is the "idiosyncratic" return — the component the supply-inflation signal should be predicting.

**Step 2 — Signal score construction.**
Assign each token a composite signal score s_i:
- Positive score --> candidate for long book (low supply inflation, no unlock event)
- Negative score --> candidate for short book (high supply inflation or unlock event detected)

**Step 3 — Portfolio optimization (dollar-neutral + beta-neutral).**
Let w_i be the portfolio weight (negative = short). Solve:

```
minimize:   w' * Sigma * w  -  lambda * s' * w
subject to:
    sum(w_i) = 0                          (dollar neutral)
    sum(w_i * beta_i_BTC) = 0            (BTC beta neutral)
    sum(w_i * beta_i_ETH) = 0            (ETH beta neutral)
    |w_i| <= w_max  for all i             (position limit)
    sum(|w_i|) = 2                        (gross leverage = 2x)
```

where Sigma is the Ledoit-Wolf shrinkage covariance matrix and lambda controls the signal-to-risk tradeoff.

**Python implementation:**
```python
from sklearn.covariance import LedoitWolf
import cvxpy as cp

lw = LedoitWolf().fit(returns_matrix)   # returns_matrix: T x N
Sigma = lw.covariance_

w          = cp.Variable(N)
s          = np.array([signal_score_i for each token])
beta_BTC   = np.array([beta_BTC_i     for each token])
beta_ETH   = np.array([beta_ETH_i     for each token])

objective = cp.Minimize(
    cp.quad_form(w, Sigma) - lam * s @ w
)
constraints = [
    cp.sum(w) == 0,
    beta_BTC @ w == 0,
    beta_ETH @ w == 0,
    cp.norm1(w) <= 2,
    w >= -w_max, w <= w_max,
]
prob = cp.Problem(objective, constraints)
prob.solve(solver=cp.CLARABEL)
```

**Step 4 — Dynamic position sizing by liquidity.**
Apply an inverse-volatility, liquidity-adjusted weight modifier:

```
w_i_adj = w_i * (1 / sigma_i) * min(ADV_i, ADV_max) / ADV_max
```

This simultaneously down-scales positions in high-volatility tokens and tokens with limited liquidity.

**Step 5 — Turnover budget.**
Add a transaction-cost-aware term to the objective:

```
minimize:   w' * Sigma * w  -  lambda * s' * w  +  kappa * ||w - w_prev||_1
```

where kappa is the half-spread cost and w_prev is the previous portfolio. This penalizes deviating from the prior portfolio in proportion to trading cost, naturally limiting turnover.

---

### 3.3 Derivatives & Options Overlay

The options strategy layer is primarily applicable to BTC and ETH (deep, liquid options on Deribit and CME) and a small number of large-cap altcoins (SOL, BNB). For the broader universe, perpetual funding rates serve as the sentiment analog.

**Pre-event signal: Put/call skew as a secondary confirmation filter.**

The 25-delta risk reversal (RR25) measures the implied volatility premium of OTM puts over OTM calls:

```
RR25 = IV(25-delta put) - IV(25-delta call)
```

When market participants anticipate downside (e.g., a known unlock cliff is approaching), demand for protective puts increases, pushing RR25 more negative. Systematic filter: only activate the short-side of the H1 strategy when RR25 has deteriorated by more than 1 vol point in the prior 7 days.

**Post-event: Volatility surface arbitrage (IV crush capture).**

When an unlock event is publicized (T-2 to T=0), implied volatility typically spikes as hedgers buy puts. After the event passes (T+1), the uncertainty resolves and IV collapses — the IV crush. The structured trade:

**Short Straddle entered at T-1, closed at T+1:**
- Sell 1 ATM call (strike K) expiring post-event
- Sell 1 ATM put (strike K) same expiry
- Net position: short gamma, short vega, positive theta

```
P&L = P_put + P_call - 0.5 * Gamma * (delta_S)^2
```

The strategy profits if realized volatility over the event window is less than the implied volatility priced into the options. Given that the H1 analysis shows only a -2.5% directional move over 4 weeks (small for crypto), the realized vol may underrun the IV spike, making this a viable complementary trade.

**Regime-conditioned options overlay:**
In a crypto bull regime (BTC trending above 20-week SMA), short volatility strategies (short straddles, short gamma) are systematically profitable because realized vol tends to be below IV. In bear regimes, the opposite holds. Apply the options overlay only in bull regimes where the IV-crush capture is historically reliable.

**For tokens without liquid options markets:**
Use the **perpetual funding rate** as a sentiment proxy. When expected unlock pressure pushes funding rates deeply negative (shorts in high demand), entering a long-the-perp position during this funding period earns the funding rate as a carry trade — partially offsetting the directional risk of being long into a declining token.

---

### 3.4 Macro Regime Filter: Don't Short Into a Raging Bull

The H2 and H3 charts make this visually obvious. Both Q1 and Q4 portfolios experienced their deepest drawdowns during the 2018-2019 and 2022 bear markets. An unregulated H1 short strategy would have been systematically unprofitable in 2021, when the market went up so fast that even supply-inflating tokens posted huge gains, swamping the -2.5% ACAR signal.

**Regime definition (multi-indicator, avoid overfit):**

| Signal | Bull | Bear |
|---|---|---|
| BTC 20-week SMA trend | Price > SMA | Price < SMA |
| BTC MVRV-Z score | Z < 3.5 (fair value) | Z > 7 (euphoria) or Z < -1 (capitulation) |
| Altcoin Season Index | < 50 (BTC-dominated) | > 75 (altcoin mania) |

**MVRV-Z Score** measures how far current market cap deviates from the "realized cap" (the aggregate cost basis of all on-chain holders). Available via Glassnode. Historically:
- MVRV-Z > 7: peak euphoria, high probability of multi-month bear market to follow
- MVRV-Z < 0: capitulation, high probability of cycle bottom

**Hamilton Markov Switching Model (rigorous alternative):**

```python
from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

model = MarkovRegression(
    btc_returns,
    k_regimes=2,
    trend='c',
    switching_variance=True
)
result = model.fit()
regime_probs = result.smoothed_marginal_probabilities
```

This yields a time-varying probability P(bull_t) used as a continuous position scalar:

```
w_regime_adjusted = w_raw * (2 * P(bear_t) - 1)
```

- P(bear) = 1.0 --> full short book activation
- P(bear) = 0.5 --> zero exposure (neutral)
- P(bear) = 0.0 --> positions reversed or turned off

**Regime-stratified backtest obligation:**
Before deploying, re-run H1, H2, and H3 separately within each regime bucket. Expected findings from the visual data:
- H1 signal: stronger in bear regimes (supply pressure compounds with market headwind), weaker or unprofitable in bull regimes
- H2/H3 quartile spread: wider in bear regimes where capital flows discriminately; collapsed in bull regimes where "everything pumps"

---

## Priority Build Roadmap

Ranked by signal improvement per engineering hour:

| Priority | Upgrade | Expected Impact | Python Modules |
|---|---|---|---|
| 1 | Replace hard-clip with cross-sectional rank transform | Fixes quartile sorting immediately | `pandas.rank` |
| 2 | Z-score signal with MAD scaling (volatility-adaptive threshold) | Removes false positives in high-vol tokens | `numpy`, `scipy.stats` |
| 3 | Add per-token funding rate / borrow cost to H1 net return | Kills unrealistic gross ACAR | Coinglass API + `requests` |
| 4 | Amihud liquidity filter + square-root market impact model | Removes untradeable tail from universe | `pandas` (computed from existing data) |
| 5 | Lo (2002) HAC-corrected Sharpe computation | Fixes annualization artifact | `statsmodels.stats` |
| 6 | Scheduled vs. unscheduled event classification | Separates exploitable from priced-in events | Messari API |
| 7 | Hamilton Markov regime filter on BTC returns | Gates strategy on macro environment | `statsmodels.tsa` |
| 8 | Dollar-neutral beta-hedged optimizer | Removes BTC/ETH systemic risk from L/S book | `cvxpy`, `sklearn.covariance` |
| 9 | Glassnode exchange inflow as leading indicator | Moves detection from T=0 to T-1 week | Glassnode API |
| 10 | Dune on-chain vesting contract monitoring | Moves detection to T-2 to T-3 weeks | `dune-client`, `web3.py` |
| 11 | Deribit IV surface / RR25 confirmation filter | Removes low-conviction event signals | Deribit WebSocket API |

Priorities 1-5 are pure mathematical corrections that improve the *measurement validity* of what you already have. They should be completed before drawing any further conclusions from the existing results. Priorities 6-8 are signal and portfolio architecture upgrades representing the core alpha improvement. Priorities 9-11 are institutional-grade data integrations that create genuine information-set advantages over other market participants.
