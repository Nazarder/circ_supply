# Supply-Dilution L/S — Strategy Improvement Roadmap
**Base:** perpetual_ls_v7.py  (+3.97% ann., Sharpe +0.220, MaxDD -20.7%)
**Principle:** Every improvement must have a theoretical justification independent of the backtest data

---

## Section 1 — Diagnosed Weaknesses

The following structural issues are confirmed by empirical evidence from the v7 Full backtest. They are not hypotheses — they are observable defects in the current strategy with measured magnitudes.

| Issue | Evidence | Magnitude |
|-------|----------|-----------|
| Funding drag on long leg | -16.4% cumulative across 51 periods | -0.32%/period (largest single cost centre) |
| Short leg absolute return: gross -43.8% | Bull regime lifts all alts; signal captures the spread but not the absolute direction | Combined L/S is positive only via the long/short spread |
| Bull geo spread weak: +26.3% vs Bear +81.9% | Supply signal is noisiest when liquidity is high and narratives dominate price | 3x weaker alpha generation in Bull vs Bear |
| High monthly turnover: 75-80% | ~7-8 position changes per basket per period driving transaction costs | ~0.10-0.15% fees/slippage per period (~1.5%/yr in extra costs) |
| Short basket chronic concentration | KAVA (16x), HBAR (14x), FIL (13x) in short basket across full history | No sizing advantage currently given to structural diluters |

---

## Section 2 — Improvements (in Priority Order)

Each improvement is stated with its theoretical rationale first. The empirical evidence from the backtest is cited as motivation, not as the primary justification.

---

### Improvement 1: Regime-Conditional Unlock Pre-Signal (Bear Only)

**Theoretical rationale**

Supply unlocks are unambiguous negative price catalysts in Bear regimes: capital is scarce, sellers are motivated, and there is no narrative to absorb additional sell pressure from vesting cliffs. In Bull regimes, the same emission events are often framed positively by the market — staking rewards signal ecosystem activity, liquidity mining incentives attract capital, and token issuance is associated with growth rather than dilution. Regime-conditional activation of the unlock signal is therefore theory-consistent: the signal should only fire when the theoretical mechanism (dilution as a bearish catalyst) actually operates.

**Empirical motivation**

The indiscriminate unlock signal delivered +17.1pp Bear geo spread improvement (+99.0% vs +81.9%) but destroyed Bull performance, reducing the overall annualised return by -1.40%. This is the exact pattern predicted by the theory. The Bear improvement can be isolated by restricting signal activation to Bear periods.

**Implementation sketch**

```python
UNLOCK_BEAR_ONLY = True  # New flag

# At each rebalancing:
if regime == "Bear" or not UNLOCK_BEAR_ONLY:
    for token in eligible_universe:
        if token.next_supply_inf > UNLOCK_SIGNAL_THRESHOLD:
            token.pct_rank = 0.95  # Force into short pool
# In Bull/Sideways: pct_rank is not overridden; trailing signal operates normally
```

**Expected benefit**

Approximately +5–15pp Bear geo spread improvement annualised. Bull and Sideways performance unchanged relative to v7 baseline.

**Overfitting risk: Low**

The regime condition is derived from economic theory, not optimised on the backtest. The Bear-only restriction is the theoretically correct activation condition. No threshold is being tuned — the 5% unlock signal threshold remains fixed.

---

### Improvement 2: Funding-Aware Short Selection

**Theoretical rationale**

Perpetual funding rates are the market's continuously updated risk premium signal for each token. Consistently positive funding on a token indicates that long holders are paying a premium to maintain their positions — the market is structurally bullish and willing to pay to stay long. From a short-seller's perspective, this is doubly attractive: (a) shorts collect the funding payment directly, reducing net carry cost, and (b) the crowded-long positioning is exactly where supply dilution should be most damaging, because forced selling from a vesting cliff hits a market where longs are already stretched.

Preferentially selecting short candidates with high positive funding rates therefore aligns directional alpha (supply dilution bearish) with carry alpha (collect funding) and crowding alpha (short the over-bought side).

**Empirical motivation**

The long leg suffers -0.32%/period in net funding drag. The short leg earns only +0.08%/period in funding credit under the current selection process. The funding credit on shorts is far lower than expected, suggesting the current short basket is not systematically positioned against the highest-funding tokens. A funding preference within the short candidate pool would directly improve the short leg's carry without changing the supply-signal selection logic.

**Implementation sketch**

```python
FUND_WEIGHT = 0.2  # Soft preference weight

# After supply-signal ranking and momentum veto have generated the short candidate pool:
for token in short_candidates:
    # Trailing 4-period average 8h funding rate, normalised to [0,1] across candidates
    normalized_fund_rank = rank(token.avg_funding_4p) / len(short_candidates)
    # Secondary sort: apply soft weight to composite rank
    # Do NOT inject into composite rank (tested; hurt performance) — use as tiebreaker
    token.adjusted_rank = token.supply_rank * (1 + FUND_WEIGHT * normalized_fund_rank)

# Sort short_candidates by adjusted_rank descending; select top N as before
short_basket = sorted(short_candidates, key=lambda t: t.adjusted_rank, reverse=True)[:N_SHORT]
```

Note: The funding preference is applied as a secondary sort within the already-filtered short candidate pool — it adjusts the relative ordering of candidates that are already through the supply-signal filter. It is not a signal component in the composite rank.

**Expected benefit**

+0.10–0.30%/period reduction in net funding drag on the combined portfolio. No change to signal quality or basket size.

**Overfitting risk: Low**

The direction of the funding preference (prefer high-funding shorts) is theoretically unambiguous. The magnitude of FUND_WEIGHT (0.2) is intentionally modest and is not being optimised. The separate-from-composite-rank application avoids contaminating the primary supply signal.

---

### Improvement 3: Cross-Sectional Signal Dispersion Gate

**Theoretical rationale**

The supply-dilution L/S signal works by exploiting cross-sectional differences in supply inflation across the eligible universe. When all tokens inflate at similar rates in a given period — a "supply inflationary regime" — there is no meaningful rank separation, and the strategy is selecting the least-bad token from a uniformly bad set, not identifying genuine outliers. A dispersion gate identifies when the signal has genuine discriminatory power ("on") versus when it is selecting noise ("off").

The dispersion gate is theoretically motivated by signal-to-noise reasoning: a strategy that only trades when it has a signal is better than one that trades constantly, including when the signal is absent.

**Empirical motivation**

Bull periods generate weaker geo spread (+26.3% vs Bear +81.9%) despite a reasonable win rate, suggesting that in Bull, the signal produces correct directional calls but with insufficient magnitude to overcome costs. Low cross-sectional dispersion of supply inflation would explain this: in Bull, all tokens are inflating to fund liquidity mining and ecosystem growth, compressing the spread between high and low inflators. Skipping the lowest-dispersion periods would eliminate the weakest-contribution trades.

**Implementation sketch**

```python
DISPERSION_GATE_ENABLED = True
DISPERSION_THRESHOLD_PCT = 25  # Skip if dispersion is in bottom quartile of trailing window
DISPERSION_ROLLING_WINDOW = 12  # Trailing periods for percentile computation

# At each rebalancing:
cs_dispersion = eligible_universe['supply_inf_13w'].std()
trailing_dispersions = dispersion_history[-DISPERSION_ROLLING_WINDOW:]
dispersion_pct = percentileofscore(trailing_dispersions, cs_dispersion)

if DISPERSION_GATE_ENABLED and dispersion_pct < DISPERSION_THRESHOLD_PCT:
    # Signal quality too low — hold cash this period
    regime_override = "Sideways"  # Treat as flat period
    log_skipped_period(period, cs_dispersion, dispersion_pct)
else:
    # Signal quality sufficient — proceed normally
    pass
```

**Expected benefit**

Approximately 3–5 low-quality periods skipped per year, eliminating trades that contribute negative Sharpe. Slight reduction in total trade count reduces transaction costs.

**Overfitting risk: Medium**

The 25th percentile threshold is a parameter choice. However, the adaptive implementation (rolling 12-period baseline) makes the threshold self-calibrating relative to current market conditions rather than fixed to in-sample history. The threshold should not be optimised on the backtest — the 25th percentile is a structural choice motivated by the concept of "bottom quartile quality."

---

### Improvement 4: Chronic Diluter Conviction Weighting

**Theoretical rationale**

A token consistently ranked in the top supply-inflation decile for 12 or more consecutive months has a structural emission problem. This is categorically different from a token that ranks high this month due to a one-time treasury unlock or bridge minting event. Structural diluters — tokens with multi-year vesting schedules generating continuous above-average supply inflation — represent higher-conviction shorts because:

1. The supply pressure is persistent, not transient.
2. The signal is less likely to be a false positive (one-time events wash out; structural vesting does not).
3. The market eventually prices in structural dilution as sentiment declines on the token.

A conviction multiplier for serial diluters is therefore theoretically justified: more evidence (consecutive high-inflation rankings) warrants more capital commitment.

**Empirical motivation**

KAVA (16x in the short basket), HBAR (14x), FIL (13x), and CRV (10x) appear repeatedly across the full 51-period history. These are structural diluters — they were not selected once due to a spike. Currently, they receive the same `inv_vol * sqrt(ADTV)` position weight as any new entrant to the short basket. A conviction multiplier for tokens with >= 6 consecutive short-basket periods would increase short leg return per unit of turnover, since these tokens require zero additional selection logic investment (they are already in the basket).

**Implementation sketch**

```python
CHRONIC_THRESHOLD = 6  # Consecutive periods in short entry zone to qualify
CHRONIC_MULTIPLIER = 1.5  # Weight multiplier for chronic diluters
ADTV_POS_CAP = 0.20  # Existing 20% ADTV-based position cap (not overridden)

# Maintained state (persists across rebalancings):
consecutive_short_periods = defaultdict(int)

# At each rebalancing, before position sizing:
for token in short_basket:
    if token in previous_short_basket:
        consecutive_short_periods[token] += 1
    else:
        consecutive_short_periods[token] = 1

    base_weight = inv_vol_weight(token) * sqrt(token.adtv)

    if consecutive_short_periods[token] >= CHRONIC_THRESHOLD:
        token.target_weight = base_weight * CHRONIC_MULTIPLIER
    else:
        token.target_weight = base_weight

# Renormalise short basket weights to sum to 1.0
# Existing ADTV_POS_CAP applied after renormalisation — multiplier effect is capped
```

**Expected benefit**

Increased concentration on proven structural shorts. Higher short leg return per unit of turnover. The multiplier compounds with the existing inv-vol weighting rather than replacing it.

**Overfitting risk: Medium**

The CHRONIC_THRESHOLD parameter should be motivated by the typical vesting cliff cycle: 6 months is one standard cliff interval (quarterly vesting schedules produce 3-month intervals; annual schedules produce 12-month; 6 months is the midpoint and the most common cliff duration in 2019–2024 token designs). The multiplier (1.5x) should remain modest — large multipliers create concentration risk that the ADTV cap is designed to prevent.

---

### Improvement 5: Extended Slow Signal Window (26w instead of 13w)

**Theoretical rationale**

The current signal uses a 13-week fast window and a 52-week slow window. For a 1-month holding period, the 13-week fast signal is already backward-looking by 2–3 months beyond the holding horizon — it captures supply change that occurred before the position was opened, not the change that will occur during the holding period. More importantly, for tokens mid-vesting-cycle (between cliff events), the 13-week window frequently captures a "quiet" period between two tranches, generating a false low-inflation reading and misclassifying a chronic diluter as a low-inflator.

A 26-week fast window smooths over intra-cycle quiet periods by capturing the full 6-month vesting cycle that is most common in the universe. This produces a more stable, less noisy supply-inflation ranking.

**Empirical motivation**

Earlier experiments showed that adding a 4-week component contaminated long selection because very short-term noise dominated the signal and overrode the structural supply-inflation rank. The same contamination risk exists at 13 weeks for tokens mid-vesting-cycle: a 13-week window can classify a token as a low-inflator if the snapshot happens to land between two cliff events. The 26-week window would capture the full cliff-to-cliff cycle and produce a more reliable classification.

**Implementation sketch**

```python
# Current parameterisation:
SUPPLY_WINDOW = 13       # Fast window (weeks)
SUPPLY_WINDOW_SLOW = 52  # Slow window (weeks)
SIGNAL_SLOW_WEIGHT = 0.50

# Proposed parameterisation:
SUPPLY_WINDOW = 26       # Fast window extended to 6 months
SUPPLY_WINDOW_SLOW = 52  # Slow window unchanged
SIGNAL_SLOW_WEIGHT = 0.50  # Weighting unchanged

# All downstream signal construction, ranking, and basket selection logic unchanged
```

**Expected benefit**

Reduced false-positive long and short selections from transient supply pauses between vesting tranches. Lower turnover (the 26-week signal changes classification less frequently than the 13-week signal for tokens with quarterly cliff schedules). Potentially lower transaction costs as a consequence.

**Overfitting risk: Low**

The signal window is a structural choice motivated by the supply inflation cycle, not optimised on backtest metrics. The 26-week window is the theoretically correct choice for a strategy targeting 6-month vesting cycles. No other parameters are changed.

---

### Improvement 6: Intraperiod Stop-Loss on Individual Shorts

**Theoretical rationale**

The existing circuit breaker (CB) caps total basket loss at 40% in any single period. However, the CB fires at the basket level — multiple tokens must simultaneously move against the portfolio to trigger it. Individual tokens can contribute large positive returns (losses on the short) before the CB activates. A short squeeze on a single token (which is common in low-liquidity altcoin markets following an exchange listing, influencer promotion, or sector narrative) can inflict 40–60%+ losses on an individual position within a single month.

An individual position stop-loss at 20–25% intramonth is consistent with standard derivative risk management practice: a loss of this magnitude on a short position indicates the directional thesis has failed for this token in this period, and holding through the squeeze compounds loss without improving expected value.

**Empirical motivation**

The CB triggered in 3 of 51 periods (5.9%). Each CB firing represents a period where multiple short positions moved strongly against the basket simultaneously. Individual position stops would reduce the frequency of reaching the basket-level CB by exiting the most extreme individual positions before they drag the entire basket to the CB threshold.

**Implementation sketch**

```python
# Requires refactoring to weekly-granularity position tracking
# Currently the backtest operates at monthly close-to-close

INDIVIDUAL_STOP_PCT = 0.20  # Exit short if price rises >20% from entry intramonth

# Weekly rebalancing loop (new):
for week in period_weeks:
    for token in short_basket:
        week_return = (token.price_week / token.entry_price) - 1
        if week_return > INDIVIDUAL_STOP_PCT:
            # Exit at next weekly close
            token.exit_price = token.price_next_weekly_close
            token.exit_reason = "individual_stop"
            short_basket.remove(token)
            log_stop(token, week, week_return)
```

This improvement requires the most significant refactoring: the backtest must shift from monthly close-to-close to weekly-granularity position tracking. Binance USDT-M perpetual OHLCV data at weekly resolution is available in the existing `binance_perp_data/` directory and supports this refactor.

**Expected benefit**

Lower CB frequency (3/51 → estimated 1–2/51). Lower short leg volatility. Improved Sharpe from reduced tail events.

**Overfitting risk: Low**

The 20–25% individual threshold is in line with standard derivative risk management practice for short positions. It is not optimised on the backtest — it is a practitioner-standard risk limit applied consistently.

---

## Section 3 — What NOT to Do (Overfitting Risks)

The following are explicitly rejected as improvement candidates. Each represents a form of in-sample overfitting that would degrade out-of-sample performance.

**Do not optimise LONG_ENTRY_PCT / SHORT_ENTRY_PCT / buffer bands.**
These thresholds determine which tail of the supply-inflation distribution is treated as the entry zone. They are structural parameters motivated by the concept of "select the top and bottom decile of the distribution." Optimising them on 45–51 periods would be selecting the thresholds that happened to work best on this specific history. The correct approach is to set them once from theory (top/bottom decile = 10th and 90th percentile) and not revisit.

**Do not tune regime thresholds (BULL_BAND / BEAR_BAND) on the backtest.**
45–51 data points is insufficient to reliably optimise two correlated parameters. The regime thresholds determine what fraction of the history is classified as Bull, Bear, or Sideways — adjusting them on the backtest amounts to selecting the regime classification that maximises the look-back return. This is a particularly dangerous overfitting vector because regime classification is the strongest source of return heterogeneity in the strategy.

**Do not maximise win rate through basket sizing.**
Win rate is a noisy metric on 51 periods. A strategy that maximises win rate by selecting smaller baskets or tighter entry zones is smoothing the return series, not improving alpha. The correct objective is risk-adjusted spread return, not win frequency.

**Do not add signal components to increase the composite rank.**
Each additional component in the composite rank (supply signal, momentum, funding, liquidity, etc.) requires an additional held-out validation dataset to confirm that it adds genuine out-of-sample value. With 45–51 periods there is no credible held-out set. The composite rank should remain as parsimonious as possible.

**Do not backtest the unlock signal threshold (5%) across multiple values.**
Selecting the threshold on the same data used to evaluate the unlock signal creates look-ahead in signal calibration. The 5% threshold is a structural choice: it represents a material supply event (more than one month's average inflation rate) that is likely to be a scheduled vesting event rather than noise. It should not be swept across a grid.

---

## Section 4 — Implementation Priority

| Priority | Improvement | Theoretical Confidence | Implementation Complexity | Expected Lift |
|----------|-------------|----------------------|--------------------------|---------------|
| 1 | Regime-conditional unlock signal (Bear only) | High | Low (1 flag) | +5–15pp Bear geo spread |
| 2 | Funding-aware short selection | High | Low-Medium (funding data integration) | +0.1–0.3%/period funding drag reduction |
| 3 | Extended slow signal window (26w) | Medium-High | Low (1 parameter change) | Lower turnover, more stable selection |
| 4 | Cross-sectional dispersion gate | Medium | Medium (rolling dispersion computation) | Skip 3–5 noisy periods/yr |
| 5 | Chronic diluter conviction weighting | Medium | Medium (state tracking across periods) | Higher short leg conviction |
| 6 | Intraperiod individual stop-loss | High | High (weekly-granularity backtest refactor) | Lower CB rate, lower short vol |

Improvements 1–3 are recommended as the first implementation batch. They are low-complexity, theoretically well-grounded, and address the three most significant diagnosed weaknesses (Bear alpha underutilisation, funding drag, and signal noise). Improvements 4–6 should follow after the first batch is validated.

---

## Section 5 — What the Data Says About Capacity and Deployment

**Capacity ceiling**

The strategy's AUM capacity is constrained by the ADTV-based position sizing rule (`ADTV_POS_CAP = 20%` of weekly ADTV). With an average long basket of 11 tokens, each with approximately $10M in weekly ADTV, the maximum tradeable position per token is $2M. At 11 tokens, the total long leg capacity is approximately $22M — but this overstates it, since entry and exit must occur at a discount to ADTV to avoid market impact. A realistic capacity estimate is **$4–10M AUM**, with $4M being conservative (low-liquidity Bear periods) and $10M being achievable in high-liquidity Bull periods when ADTV is elevated.

**Structural ceiling on long leg profitability**

The funding drag on the long leg (-0.32%/period) represents a structural ceiling that worsens at scale. As AUM increases, entries move the market against the position, effectively paying a higher implicit funding rate. The long leg's net return is already thin (+0.08%/period before costs) and would compress toward zero at $10M+. This is the most important constraint on the strategy's scalability.

**Most viable live implementation path**

Given the capacity constraint and the empirical evidence that Bear periods generate 3x the alpha of Bull periods, the most viable live implementation is:

1. **Bear-only deployment.** Only activate the strategy when the regime signal is Bear. This concentrates capital deployment in the highest-alpha environment and avoids the long-leg funding drag in Bull (where the carry is most punishing due to high positive funding rates on altcoins).

2. **Glassnode on-chain supply data overlay.** Replace CMC circulating supply (which has known reporting lags) with Glassnode's on-chain issued supply series for tokens where it is available. This eliminates supply reporting artifacts that can generate false signal readings.

3. **Unlock calendar integration.** Implement the regime-conditional unlock pre-signal (Improvement 1) using a TokenUnlocks or Messari data subscription. Activate only in Bear regime. This captures the +17pp Bear geo spread improvement without Bull contamination.

The combination of Bear-only deployment + on-chain supply + unlock calendar overlay represents the theoretically cleanest and most capacity-efficient deployment of the strategy.
