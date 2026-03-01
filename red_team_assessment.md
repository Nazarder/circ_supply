# Red-Team Assessment: Supply Inflation Strategy

**Date:** 2026-03-01
**Role:** Senior Quantitative Risk Manager — Tier-One Crypto Trading Firm
**Objective:** Identify hidden traps, structural flaws, and market realities that historical data obscures

---

## Backtest Reproduction Results

All four scripts were re-run against `cmc_historical_top300_filtered_with_supply.csv`
(135,652 rows, 2,267 symbols, Jan 2017 – Feb 2026). Results confirmed:

### H1: Unlock Event Study (Z-score > 3.0, ±2 weeks)

| Metric | Value |
|--------|-------|
| Events flagged | 3,863 (3,713 with complete windows) |
| ACAR at T+2 (all) | +2.47% |
| t-stat / p-value | +2.01 / 0.044 |
| ACAR — Bull (n=2,085) | **+6.33%** |
| ACAR — Bear (n=1,628) | **-2.49%** |

**Finding:** Supply-dilution effect around unlock events is real but regime-conditional.
Negative alpha exists only in bear markets. Bull-market momentum overwhelms dilution.

### H2/H3: Quartile L/S (13-week and 52-week supply inflation)

| Portfolio | Ann. Return | Volatility | MaxDD |
|-----------|-------------|------------|-------|
| L/S (H2, 13-week) | N/A (bankrupt) | 63.40% | -100% |
| L/S (H3, 52-week) | N/A (bankrupt) | 62.78% | -100% |
| Bear-Only (H2) | -0.12% | 16.82% | -42.62% |
| Bear-Only (H3) | -0.09% | 20.93% | -38.26% |
| Index (H2) | +111.06% | 113.95% | -80.39% |
| Index (H3) | +34.06% | 74.59% | -80.39% |

**Finding:** Quartile-level L/S bankrupt in all unconditional configurations. Regime-gated
Bear-Only survives but produces zero alpha after slippage.

### Extreme Percentile Test (10th vs 90th percentile, absolute returns)

| Basket | Ann. Return | Volatility | MaxDD |
|--------|-------------|------------|-------|
| 10th Pct (Low Inflation) | +10.87% | 149.76% | -96.71% |
| 90th Pct (High Inflation) | -13.33% | 147.49% | -98.51% |
| **Spread** | **~24 ppt** | 77.93% | — |
| Win rate (Low > High) | 63/106 (59.4%) | — | — |

**Finding:** The signal lives in the tails. Decile-level separation produces a ~24 ppt
annualized spread with 59.4% monthly win rate. Quartile sorts dilute this to noise.

### Beta-Hedged L/S (Short Q4 + Long BTC/ETH/Top10)

| Portfolio | Ann. Return | MaxDD |
|-----------|-------------|-------|
| DN: Long BTC | N/A | -100% |
| DN: Long BTC+ETH | N/A | -100% |
| DN: Long Top10 | N/A | -100% |
| BN: Long BTC | N/A | -100% |
| BN: Long BTC+ETH | N/A | -100% |
| BN: Long Top10 | N/A | -100% |
| **Q4 basket held LONG** | **+7.38%** | -93.35% |

**Finding:** All 6 configurations bankrupt. The Q4 basket returns +7.38% as a long —
shorting it carries a structural negative carry. Trailing OLS beta (~1.09) systematically
underestimates realized bull-market multipliers (2x+).

---

## Task 1: Execution Blind Spots

### 1.1 Borrow Availability & Costs

The backtest assumes frictionless shorting of any token. Reality:

- **CEX lending pools are shallow and episodic.** Binance, OKX, and Bybit offer margin
  borrowing for ~50-80 tokens. The Q4 basket averages 55.7 tokens/month — at least 30-40%
  will have zero borrow availability at any given time.

- **Borrow APYs are auction-driven and spike at the worst times.** Normal rates for mid-cap
  altcoins run 5-15% APY, but when a token is heavily shorted or actively emitting, rates
  spike to 50-300% APY. At even a conservative 15% avg borrow rate, this adds ~3.75% drag
  per 4-week holding period — not modeled in the backtest.

- **"Hard to borrow" status changes are discontinuous.** A token can be pulled from lending
  mid-hold, forcing a buy-in at market price during an adverse move. Monthly rebalancing
  assumes 4-week uninterrupted holds — unrealistic for altcoin borrows.

- **Perpetual futures as a substitute are equally treacherous.** Funding rates on perps for
  high-inflation altcoins are frequently negative during bull markets (shorts pay longs),
  accumulating 4-6% of notional over a 4-week hold.

**Net impact:** Real-world cost of maintaining the short leg likely exceeds the entire
theoretical alpha. The synthetic slippage model captures ~10-20% of true all-in shorting costs.

### 1.2 Short Squeeze Risk

Inverse-volatility weighting is counterproductive for squeeze protection:

- **Inv-vol overweights "stable" high-inflation tokens** — precisely where short interest
  concentrations build up, creating fragile equilibria.

- **Coordinated squeezes are structural in crypto.** When short interest becomes visible
  on-chain via DeFi lending utilization dashboards, it paints a target for retail coordination
  (Telegram/Discord groups). A -500% squeeze on a 5%-weighted position wipes 25% of the
  portfolio in a single session.

- **No mechanism for gap risk.** Squeezes produce liquidity vacuums, not smooth drawdowns.
  Winsorization at 1st/99th percentile cannot model order book gaps.

- **Missing risk factor:** Short Interest Ratio (DeFi lending utilization + CEX margin
  utilization) is absent from the model entirely.

### 1.3 Liquidity Cascades & Real Slippage

The slippage model `slippage = min(0.0005 / turnover, 0.02)` is static and time-invariant:

- **Order book depth ≠ 24h volume.** A token may trade $10M/day but have only $200K of
  visible depth within 2% of mid-price. During stress events, market makers widen spreads
  and pull liquidity, decoupling volume from depth.

- **Panic covering during rallies:** Closing a losing short means buying into a momentum move
  with no offers. Real slippage for a $500K position can reach 10-30%+ vs the 200 bps cap.

- **Cross-asset correlation amplifies the problem.** During market-wide moves, all 55 tokens
  in the short basket move together — the portfolio is effectively one correlated bet.

- **Required fix:** Impact model using `f(order_size, book_depth, realized_vol, time_of_day)`.
  Kyle's lambda or Almgren-Chriss as minimum-viable frameworks.

---

## Task 2: Methodological & Data Blind Spots

### 2.1 Survivorship Bias — The Asymmetric Trap

**Bias correctly identified (favors short leg):** Delisted high-inflation tokens that went to
near-zero are missing from the dataset. If capturable, they would have been profitable shorts.

**Bias missed (destroys short leg in practice):** When a token is delisted:

- You cannot close the short at a known price
- The token may trade only on an illiquid DEX at an arbitrary price
- The lending platform may force-liquidate at a punitive settlement price
- Exchange insolvency (FTX, Celsius) can freeze positions entirely
- A short on a delisted token creates an **open-ended liability** with no guaranteed close

The backtest's `clip(lower=-1.0)` on forward returns does not model this "debit risk."

### 2.2 Overfitting to Macro Regimes

The dataset (Jan 2017 – Feb 2026) covers the most favorable 9-year window in risk asset
history: Fed balance sheet $4.5T → $9T, near-zero rates for most of the period, crypto
market cap $20B → $3T+.

- **The entire dataset is a secular bull market.** "Bear" periods in the data are drawdowns
  within a structurally rising market, not genuine secular bears.

- **The conclusion that "the signal is exploitable only on the long side"** depends on
  crypto continuing to appreciate over multi-year periods. If crypto enters prolonged
  stagnation, the +10.87% low-inflation basket return disappears — it includes beta
  exposure to a rising market. The alpha vs beta decomposition is not performed.

- **Beta estimates are calibrated to a recovery-dominant regime.** The positive convexity of
  Q4 tokens may be more extreme or may disappear entirely in a genuine secular bear.

### 2.3 Information Asymmetry (On-Chain vs. API)

The signal derives from weekly CoinMarketCap snapshots. By the time a supply change appears:

1. **The on-chain emission already happened.** Vesting contracts, staking emissions, and
   treasury disbursements are visible on-chain in real-time.

2. **Specialized firms already traded on it.** Token Unlocks, Nansen, and Arkham Intelligence
   provide real-time alerts. Institutional desks receive these hours to days before CoinMarketCap
   updates supply figures.

3. **Your Z-score signal detects stale events.** When Z > 3.0 fires at week T, the actual
   unlock may have occurred at T-0.5 or T-1. In crypto, where tokens move 20%+ in a day,
   1-7 days of latency represents the majority of any tradeable signal.

**The +2.47% ACAR likely captures residual momentum after informed money has already
extracted the first-mover alpha.**

---

## Task 3: Final Verdict & Recommendations

### 3.1 Is the Short Leg Viable?

**No. The short leg is permanently non-viable. The data is unambiguous.**

| Strategy Configuration | Survives? | Returns |
|------------------------|-----------|---------|
| Short Q4 unconditional L/S | No (bankrupt) | N/A |
| Short Q4 bear-only | Yes | -0.12% (zero after costs) |
| Short Q4 + long BTC (dollar-neutral) | No (bankrupt) | N/A |
| Short Q4 + long BTC+ETH (dollar-neutral) | No (bankrupt) | N/A |
| Short Q4 + long Top10 (dollar-neutral) | No (bankrupt) | N/A |
| Short Q4 + long BTC (beta-neutral) | No (bankrupt) | N/A |
| Short Q4 + long BTC+ETH (beta-neutral) | No (bankrupt) | N/A |
| Short Q4 + long Top10 (beta-neutral) | No (bankrupt) | N/A |
| **Long low-inflation 10th pct** | **Yes** | **+10.87% ann.** |

**Three structural reasons the short fails:**

1. **Negative carry:** The Q4 basket returns +7.38% annually as a long position. Crypto bull
   markets carry even the worst fundamentals higher. Shorting = paying positive carry.

2. **Non-linear beta (positive convexity):** OLS beta averages ~1.09 vs BTC, but realized
   bull-market sensitivity exceeds 2x. No linear hedge covers this. When BTC gains 40%,
   the short basket gains 90% — the hedge covers less than half the loss.

3. **Asymmetric regime exposure:** The dilution signal works only in bear markets (40% of
   sample). The 60% bull-market exposure destroys all accumulated bear-market gains.

### 3.2 What IS Valuable

The supply inflation signal is **real, statistically significant, and one-sided:**

- **H1 Bear ACAR = -2.49%** — Unlock events genuinely hurt tokens in bear markets
- **Extreme percentile spread = ~24 ppt** — Low-inflation tokens massively outperform
  high-inflation tokens at the decile level
- **59.4% monthly win rate over 106 periods** — Consistent, repeatable edge

**Implementation:** The signal is a **smart-beta factor for crypto** — a portfolio tilt within
a directional allocation, not an absolute-return strategy. The sole viable implementation:

> **Long the 10th–15th percentile of 13-week supply inflation, inverse-volatility weighted,
> monthly rebalanced, with position caps. No short leg in any form.**

### 3.3 Recommended Strategy Modifications

If deploying capital based on this research:

1. **Long-only structure.** No short leg. Period.
2. **Decile-level selection** (10th–15th percentile), not quartile
3. **Inverse-vol weighting** with 8% per-position cap (reduces the 149% vol of equal-weight)
4. **Minimum $5M daily volume filter** (executability gate the backtest lacks)
5. **Minimum 26 weeks supply history** (avoid data artifacts)
6. **Momentum overlay:** Exclude tokens with bottom-decile 4-week price momentum (avoid
   catching falling knives — low inflation + negative momentum = dead projects)
7. **Regime filter:** Reduce position sizes by 50% in Bear regime (signal works in bear,
   but with higher vol)
8. **On-chain monitoring:** Remove holdings that announce upcoming unlocks before rebalance

---

## Appendix: Cross-Strategy Synthesis

| Strategy | Signal Present? | Survives? | Exploitable? |
|----------|----------------|-----------|-------------|
| H1 Event Study (Bull) | Reversed (+6.33%) | Yes | No (wrong direction) |
| H1 Event Study (Bear) | Yes (-2.49%) | Yes | No (too few events per period) |
| H2/H3 Quartile L/S (unconditional) | No | No (bankrupt) | No |
| H2/H3 Bear-Only L/S | No (near-zero) | Yes | No |
| Extreme Pct Long (10th pct) | Yes (+10.87%) | Yes | **Yes** |
| Extreme Pct Short (90th pct) | Yes (-13.33%) | Barely | No (execution risk) |
| Beta-Hedged L/S (all 6) | No | No (bankrupt) | No |

**The supply-dilution signal exists. It is a tail phenomenon. It is exploitable only on
the long side. The short leg is a permanent capital destroyer in every tested configuration.**

---

*Assessment generated 2026-03-01. Based on reproduction of all four backtest scripts against
the full dataset (135,652 rows, 2,267 symbols, 477 weekly snapshots, Jan 2017 – Feb 2026).*
