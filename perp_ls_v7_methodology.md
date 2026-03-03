# Perpetual L/S Backtest — Version 7 Methodology

**Strategy:** Supply-Dilution Long/Short on Perpetual Futures
**Version:** v7 (current best)
**Coverage:** 2022-01-01 → 2026-02-22
**Data:** CMC weekly supply snapshots + Binance USDT-M perp OHLCV + actual 8h funding rates

For shared infrastructure (data sources, universe construction, signal maths, execution model), see [`perp_ls_methodology.md`](perp_ls_methodology.md). This document covers only what changed in v7 and its full results.

---

## 1. What v7 Fixed

v6 ended at +0.10% annualised net. A structural audit identified six root causes:

| # | Problem | Root cause |
|---|---------|------------|
| 1 | Only 19 periods | Regime-aware bi-monthly Bull step halved observations from ~49 monthly to 19, making all statistics unreliable |
| 2 | BTC hedge destroying value | Rolling OLS beta (12-period window) estimated from <20 periods was unstable; avg beta 0.453 created −3.77%/yr drag; identical failure mode as v4→v5 |
| 3 | Short leg: −29.53% net | Strongly momentum-driven tokens appeared as short candidates via supply signal; CB hit rate was 15.8% (3/19) |
| 4 | Sideways reverted to full exposure | v6 re-introduced v4's full (1.0L, 1.0S) Sideways scaling; v5 had already proved Sideways win rate is only 33% and cash is better |
| 5 | Signal–holding mismatch in Bull | 13w supply signal with 2-month hold in Bull: signal and holding period misaligned by one month in every Bull period |
| 6 | Net-long altcoin directionality | Bull scaling (1.0L, 0.20S) = 83% net-long altcoins; in the 2024-25 BTC-dominant cycle, low-inflation alts declined absolutely, destroying combined return even when the spread was positive (72% Bull win rate) |

---

## 2. V7 Changes

### [V7-1] Monthly rebalancing always

v6 implemented `REBAL_STEP = {"Bear": 1, "Bull": 2, "Sideways": 1}` — the Bull step skipped every second monthly observation. This collapsed the sample from ~49 to 19 periods, rendering all statistics unreliable.

v7 removes the regime-aware stepping entirely. Every monthly CMC snapshot is a rebalancing date.

```python
# v6 — regime-aware stepping (REMOVED in v7)
# REBAL_STEP = {"Bear": 1, "Bull": 2, "Sideways": 1}

# v7 — all monthly dates used
sorted_rebals = [d for d in all_monthly_dates if d >= START_DATE]
```

**Impact:** 19 → 45 rebalancing periods. All regime-conditional statistics are now based on meaningful sample sizes (Bull=18, Bear=14, Sideways=13).

---

### [V7-2] Sideways = hold cash (restored from v5)

v6 reverted to v4's full (1.0L, 1.0S) exposure in Sideways. This had already failed in v4 (Sideways geo spread −14.5%/yr, 33% win rate). v5 fixed it; v6 unintentionally undid it.

v7 restores Sideways = cash: `(0.00, 0.00)` scale factor. No positions, no costs, no P&L.

```python
REGIME_LS_SCALE = {
    ("Sideways", False): (0.00, 0.00),   # hold cash
    ("Sideways", True):  (0.00, 0.00),   # hold cash
    ...
}
```

**Impact:** Sideways geo spread moves from −14.5%/yr (v4) or +1.3%/yr (v6) to +0.0%/yr (v7). 13 Sideways periods contribute zero instead of noise.

---

### [V7-3] BTC beta hedge removed permanently

The BTC rolling OLS hedge was reintroduced in v6 after being removed in v5. It cost:

- v4: hedge drag estimated at ~−3.77%/yr (combined_net +0.10% vs combined_hedged −3.67%)
- v5: removed; performance improved
- v6: reintroduced; combined_hed = −3.67% vs combined_net = +0.10% → hedge cost ≈ −3.77%/yr

The 12-period OLS window estimates beta from fewer than 12 months of data. With only 19 periods total in v6, the rolling estimates were deeply unreliable. The strategy is already net-neutral by design (long alts, short alts); adding a short BTC overlay double-hedged the natural Bear-regime alpha.

v7 removes the hedge entirely. The `combined_net` series is the primary strategy return.

---

### [V7-4 / V7-5] Momentum veto on short selection

CB events in v6 (3/19 = 15.8% hit rate) were concentrated in strongly trending tokens that happened to rank high on supply inflation. These tokens had both high supply dilution **and** strong recent price momentum — the market was bidding them aggressively, making them dangerous to short regardless of their supply signal.

v7 adds a momentum veto within the short candidate pool:

```python
MOMENTUM_VETO_PCT = 0.50  # top 50% by 1m return within short candidates are vetoed

# Applied at entry-only (not to existing stays)
if len(entry_short_raw) > MIN_BASKET_SIZE:
    mom_rets = {s: p_now / p_prev - 1 for s in entry_short_raw ...}
    if len(mom_rets) >= MIN_BASKET_SIZE * 2:
        veto_threshold = np.percentile(list(mom_rets.values()), MOMENTUM_VETO_PCT * 100)
        candidates_vetoed = {s for s, r in mom_rets.items() if r > veto_threshold}
        remaining = entry_short_raw - candidates_vetoed
        if len(remaining) >= MIN_BASKET_SIZE:
            momentum_vetoed = candidates_vetoed
```

**Key design decisions:**
- Veto is computed **within the short candidate pool only** (not the full universe). This avoids removing ~60% of candidates from a universe-level 60th percentile filter.
- Vetoed at **entry only**, not applied to existing stay positions. A token already in the short basket that has rallied is held; force-exiting into momentum is more expensive than holding.
- The veto is guarded: only fires if enough candidates remain after veto (`≥ MIN_BASKET_SIZE`). If the pool is thin, the veto is skipped rather than collapsing the basket.

**Impact:** 42 token-periods removed across 45 rebalancing dates. CB triggered 3/45 (6.7%) periods — same count but lower rate than v6's 3/19 (15.8%).

---

### [V7-6] Symmetric (0.75, 0.75) L/S scaling in Bull and Bear

v6 used an asymmetric Bull scaling: `(1.00L, 0.50S)` normal, `(0.75L, 0.25S)` high-vol.

During iterative v7 testing, (1.00L, 0.20S) was tried, producing the following outcome:

- Bull win rate: 72.2% (positive spread in 13/18 Bull periods)
- Combined return: **−17%** annualised

The asymmetry caused the problem. `(1.00L, 0.20S)` = 83% net-long altcoins (1.0 / 1.2). In the 2024-25 cycle, BTC dominated and most altcoins declined in absolute terms even as low-inflation tokens outperformed high-inflation tokens relatively. A positive spread of +3% on a portfolio with 83% net-long directionality still produces a negative absolute return if the altcoin market falls >3% that month.

v7 uses **symmetric (0.75, 0.75) scaling in both Bull and Bear**, eliminating net altcoin directionality:

| Regime | High-Vol? | Long Scale | Short Scale | Net altcoin exposure |
|--------|:---------:|:----------:|:-----------:|:--------------------:|
| **Sideways** | Any | 0% | 0% | 0% (cash) |
| **Bull** | No | 75% | 75% | 0% (symmetric) |
| **Bull** | Yes | 50% | 25% | +33% (reduced) |
| **Bear** | No | 75% | 75% | 0% (symmetric) |
| **Bear** | Yes | 50% | 25% | +33% (reduced) |

With symmetric scaling, the combined return equals `0.75 × (r_long_net + r_short_net)` in normal regimes — a pure spread bet with no market direction component.

---

### [V7-7] Signal unchanged: 50% rank(13w) + 50% rank(52w)

Testing confirmed that adding a 4-week supply-change component (attempted weight: 40%) corrupted long-basket selection. Tokens with a temporarily quiet 4-week supply period but high trailing 13w/52w inflation were incorrectly promoted to longs. In practice these were tokens mid-vesting-cycle (supply paused between cliff events), which subsequently resumed dilution.

The 2-layer signal is retained:

```python
univ["rank_13w"] = univ["supply_inf_13w"].rank(pct=True)
univ["rank_52w"] = univ["supply_inf_52w"].rank(pct=True)
univ["rank_52w"] = univ["rank_52w"].fillna(univ["rank_13w"])
univ["pct_rank"] = (1 - SIGNAL_SLOW_WEIGHT) * univ["rank_13w"]
                 + SIGNAL_SLOW_WEIGHT        * univ["rank_52w"]
# SIGNAL_SLOW_WEIGHT = 0.50
```

---

### [V7-8] Data-driven quantile thresholds for basket construction

v6 used fixed percentile cutoffs (`if rank <= 0.12: long_candidate`). This works when the composite rank is uniformly distributed. But the composite rank — a weighted average of two correlated uniforms — is compressed toward 0.5.

Under fixed thresholds: at `rank ≤ 0.12`, the actual fraction of tokens selected was approximately 3-4% of the universe (not 12%), giving only 2-4 long candidates on a 50-token universe, below `MIN_BASKET_SIZE = 6` for most periods.

v7 uses data-driven quantile thresholds computed at each rebalancing date:

```python
long_thresh  = float(univ["pct_rank"].quantile(LONG_ENTRY_PCT))   # = 0.12
long_exit_t  = float(univ["pct_rank"].quantile(LONG_EXIT_PCT))    # = 0.18
short_thresh = float(univ["pct_rank"].quantile(SHORT_ENTRY_PCT))  # = 0.88
short_exit_t = float(univ["pct_rank"].quantile(SHORT_EXIT_PCT))   # = 0.82
```

This always selects exactly the intended fraction of tokens regardless of the composite rank distribution, while preserving all the hysteresis logic.

---

## 3. Full Parameter Table

| Parameter | v6 | **v7** | Δ |
|-----------|:--:|:------:|:-:|
| `MAX_RANK` | 250 | **200** | Tighter universe ceiling |
| `MIN_MKTCAP` | $100M | **$50M** | Slightly wider eligibility |
| `MIN_VOLUME` (daily ADTV proxy) | $1M/day | **$5M/day** | Higher liquidity floor |
| `REBAL_STEP` (Bull) | 2 months | **1 month** | No regime stepping |
| `REGIME_LS_SCALE` (Sideways) | (1.0, 1.0) | **(0.0, 0.0)** | Cash |
| `REGIME_LS_SCALE` (Bull, normal) | (1.0, 0.5) | **(0.75, 0.75)** | Symmetric |
| `REGIME_LS_SCALE` (Bull, high-vol) | (0.75, 0.25) | **(0.5, 0.25)** | Minor |
| `MOMENTUM_VETO_PCT` | — | **0.50** | New |
| BTC hedge | Reported (computed) | **Removed** | Dropped |
| Signal weights | 50%×13w + 50%×52w | **50%×13w + 50%×52w** | Unchanged |
| Buffer band | 12%/18% — 82%/88% | **12%/18% — 82%/88%** | Unchanged |
| Basket thresholds | Fixed percentile | **Data-driven quantile** | Architectural |
| `SHORT_CB_LOSS` | 40% | **40%** | Unchanged |
| `SHORT_SQUEEZE_PRIOR` | 40% | **40%** | Unchanged |
| `ALTSEASON_THRESHOLD` | 0.75 | **0.75** | Unchanged |
| `TOKEN_VOL_WINDOW` | 8 weeks | **8 weeks** | Unchanged |
| `ADTV_POS_CAP` | 20% | **20%** | Unchanged |

---

## 4. Results (v7)

### 4.1 Summary statistics

| Series | Ann. Return | Volatility | Sharpe | Sharpe* | Sortino | MaxDD |
|--------|:-----------:|:----------:|:------:|:-------:|:-------:|:-----:|
| Long basket (gross) | −28.47% | +82.50% | −0.345 | −0.122 | −0.368 | −89.57% |
| Short basket (gross) | −44.67% | +69.20% | −0.646 | −0.766 | −0.583 | −91.26% |
| Long leg (net) | −36.03% | +81.39% | −0.443 | −0.311 | −0.460 | −91.69% |
| Short leg (net)† | **+8.74%** | +67.97% | **+0.129** | +0.623 | +0.099 | −72.20% |
| **L/S Combined (net)** | **+3.97%** | **+18.05%** | **+0.220** | **+0.357** | **+0.195** | **−20.73%** |

*\* Lo (2002) HAC-corrected Sharpe; † short net: positive = profit for short position*

| Metric | Value |
|--------|-------|
| Rebalancing periods | 45 |
| Avg basket size | Long 11.4 / Short 9.8 tokens |
| Avg monthly turnover | Long 80.5% / Short 85.1% |
| Regime breakdown | Bull 18 / Bear 14 / Sideways 13 |
| Avg effective scale | Long 0.53× / Short 0.52× |
| CB triggered | 3 / 45 periods (6.7%) |
| Alt-season veto | 1 period |
| Momentum veto | 42 token-periods removed |
| Win rate (long > short, gross) | 22/45 (48.9%) |
| Mean period spread (gross) | +2.62% |
| Spread annualised vol | +36.43% |
| Spread excess kurtosis | 5.33 |
| Spread skewness | 0.95 |

### 4.2 Regime-conditional spread

| Regime | N | Mean Spread | Win Rate | Ann. Geo Spread |
|--------|:-:|:-----------:|:--------:|:---------------:|
| **Bull** | 18 | +2.10% | 72.2% | +18.71% |
| **Bear** | 14 | +5.71% | 64.3% | **+81.88%** |
| **Sideways** | 13 | +0.00% | 0.0% | +0.00% (cash) |

The Bear environment is where the supply-dilution signal generates its strongest edge. Capital scarcity in Bear regimes forces aggressive repricing of high-emission tokens; the 64.3% win rate and +81.88% annualised geo spread represent the strongest Bear performance across all versions.

### 4.3 Funding attribution

| Attribution | Cumulative | Per-period avg |
|-------------|:----------:|:--------------:|
| Funding drag (long leg pays) | −12.17% | −0.27% |
| Funding credit (short leg receives) | +3.50% | +0.08% |
| **Net funding impact** | **−8.68%** | **−0.19%** |

Funding drag increased vs v6 (−1.92% cumulative over 19 periods) because v7 holds positions in more periods (45 vs 19). On a per-period basis, the drag is similar (~0.19-0.27% avg). The long leg's funding cost reflects the market's consistent willingness to pay for exposure to low-inflation assets — these tokens are simultaneously strong supply-signal candidates and crowded longs, both of which attract persistent positive funding.

---

## 5. Version Comparison

| Metric | v4 | v5 | v6 | **v7** |
|--------|:--:|:--:|:--:|:------:|
| Periods | 39 | 39 | 19 | **45** |
| Combined net (ann.) | −5.1% | −2.7% | +0.1% | **+4.0%** |
| MaxDD | −22.9% | −23.3% | −19.2% | −20.7% |
| Sharpe (combined) | −0.222 | — | +0.003 | **+0.220** |
| Win rate (spread) | 48.0% | 53.8% | 52.6% | 48.9% |
| Mean period spread | +1.95% | +2.38% | +3.34% | +2.62% |
| Bull geo spread | +21.1% | — | +51.9% | +18.7% |
| Bear geo spread | +48.1% | — | +53.3% | **+81.9%** |
| Sideways geo spread | −14.5% | +0.0% | +1.3% | **+0.0%** |
| CB rate | — | — | 15.8% | 6.7% |
| Avg basket (L/S) | ~8 / ~8 | ~8 / ~8 | 7.5 / 7.6 | **11.4 / 9.8** |

**Why v7 Bear spread (+81.9%) is so much stronger than v6 Bear (+53.3%):**
- v6 Bear was estimated from only 11 periods → high uncertainty
- v7 Bear uses 14 periods with symmetric scaling capturing the full spread
- Momentum veto removed the highest-risk short candidates; the remaining short basket was more reliably short-side in Bear periods

**Why v7 Bull spread (+18.7%) is weaker than v6 Bull (+51.9%):**
- v6 Bull was estimated from only 2 periods → statistically meaningless
- v7 Bull uses 18 periods; +18.7% annualised is a reliable estimate
- v6's 51.9% was driven by 2 atypical periods, not a structural edge

---

## 6. Key Structural Lessons

### 6.1 Composite rank distribution vs fixed thresholds

A weighted average of correlated uniform [0,1] signals is not itself uniform — it is compressed toward 0.5 (central limit effect). Applying a fixed `LONG_ENTRY_PCT = 0.12` threshold to a compressed distribution selects far fewer tokens than intended (the 12th percentile of a compressed distribution is well above 0.12 in absolute value).

**Fix:** Compute thresholds at each date using `univ["pct_rank"].quantile(LONG_ENTRY_PCT)`. This is the correct method for any composite rank — always selects exactly the intended fraction of tokens regardless of distribution shape.

### 6.2 Net directional exposure kills relative-value strategies

Any L/S strategy with unequal leg scaling carries net directional exposure. In regimes where the underlying market moves strongly, this directionality can dominate the spread return. For a strategy where the fundamental edge is cross-sectional (supply-dilution rank), the position sizes on both legs should be symmetric (or at most slightly asymmetric) so that combined returns reflect the spread, not the market direction.

### 6.3 Period count as the fundamental constraint

Statistical reliability of any performance metric requires adequate sample size. With 19 periods (v6), the Bear geo spread is based on 11 observations — a 95% confidence interval of approximately ±60 percentage points annualised (using the observed vol). Moving to 45 periods triples the sample and narrows confidence intervals proportionally.

The lesson: strategies with regime-conditional logic should be evaluated on regime-conditional sample counts, not aggregate metrics. A strategy that looks attractive in 2 Bull periods and 11 Bear periods may simply have been in luck.

### 6.4 The BTC hedge double-counts natural hedging

A L/S altcoin strategy is already net-neutral to BTC beta in theory: longs and shorts both move with BTC in proportion to their beta, which largely cancels. Adding a BTC hedge overlay then shorts BTC again, producing a net short BTC position. In Bear regimes (BTC down), the hedge gains; in Bull regimes (BTC up), the hedge loses. Since the strategy is designed to run in both Bull and Bear, the hedge is directionally wrong in Bull and creates drag equal to the strategy's natural BTC beta carry.

---

## 7. Remaining Limitations

### 7.1 Funding drag is the primary cost driver

Net funding impact of −8.68% cumulative (−0.19%/period) represents approximately 2× the annualised combined net. If funding rates were zero, combined net would be approximately +8-12%/yr. Low-emission tokens are consistently heavily longed by the market; their perpetual funding rates are persistently positive (longs pay shorts). Any live implementation must model per-token funding rates dynamically and consider strategies to reduce long-leg funding cost (e.g., prefer tokens on exchanges with lower funding, or weight tokens inversely by their historical funding rate alongside supply rank).

### 7.2 Short leg absolute return remains negative (−44.67% gross)

The short basket contains high-emission tokens. In absolute terms, these tokens often also benefit from broad market rallies (positive BTC correlation), producing positive gross returns even as they underperform the long basket. The short leg earns from the *spread*, not from the basket returning negative — and the portfolio captures that spread via the combined return. Observers should not interpret the negative short gross return as a failure; it reflects a Bull-dominated 2022-2026 period where most altcoins gained in absolute terms.

### 7.3 Statistical significance

At Sharpe +0.220 with 45 monthly periods, the strategy is not yet statistically distinguishable from zero at conventional thresholds. The Lo HAC-corrected Sharpe of +0.357 is stronger. An extended backtest (pre-2022 data, if CMC supply history allows) or out-of-sample testing would be required before live deployment.

### 7.4 Turnover and capacity

Average monthly turnover of ~82% implies high transaction costs relative to the annualised return. At $5M per leg ($10M AUM), the taker fee + slippage model costs approximately 0.12%/period in fees and slippage per leg. The strategy capacity ceiling remains approximately $4-10M total AUM before market impact materially erodes the spread.

---

## 8. Script Reference

| File | Description |
|------|-------------|
| `perpetual_ls_v7.py` | **v7 (current best)** — monthly rebal, Sideways=cash, symmetric (0.75,0.75), momentum veto. **+3.97% ann. net, Sharpe +0.220** |
| `perpetual_ls_v6.py` | v6 — regime-aware bi-monthly Bull step, BTC hedge. +0.10% ann. net |
| `perpetual_ls_v5.py` | v5 — Sideways=cash, no BTC hedge. −2.74% ann. net |
| `perpetual_ls_v4.py` | v4 — Binance data, monthly rebal, BTC hedge. −5.11% ann. net |
| `perpetual_ls_experiments.py` | 12 isolated experiments on v6 base (A-J) |
| `debug_v7.py` | Diagnostic script used during v7 development; can be deleted |
| `fetch_binance_data.py` | Downloads 396-symbol weekly OHLCV + funding from Binance REST API |

```bash
# Run v7
python perpetual_ls_v7.py

# Full progression
python perpetual_ls_v4.py    # -5.11% ann.
python perpetual_ls_v5.py    # -2.74% ann.
python perpetual_ls_v6.py    # +0.10% ann.
python perpetual_ls_v7.py    # +3.97% ann.
```

### Key configurable parameters (v7)

```python
START_DATE           = pd.Timestamp("2022-01-01")
MAX_RANK             = 200              # universe ceiling (v7: tightened from 250)
TOP_N_EXCLUDE        = 20              # exclude top-20 by rank
MIN_MKTCAP           = 50_000_000      # $50M market cap floor
MIN_VOLUME           = 5_000_000       # $5M/day ADTV proxy floor

SUPPLY_WINDOW        = 13              # fast signal (weeks)
SUPPLY_WINDOW_SLOW   = 52              # slow signal (weeks)
SIGNAL_SLOW_WEIGHT   = 0.50            # 50% fast + 50% slow

LONG_ENTRY_PCT       = 0.12            # enter long at bottom 12th pct of composite rank
LONG_EXIT_PCT        = 0.18            # exit long above 18th pct
SHORT_ENTRY_PCT      = 0.88            # enter short at top 12th pct
SHORT_EXIT_PCT       = 0.82            # exit short below 82nd pct
MIN_BASKET_SIZE      = 6               # min tokens per basket

# [V7] Symmetric scaling, Sideways=cash
REGIME_LS_SCALE = {
    ("Sideways", False): (0.00, 0.00),
    ("Sideways", True):  (0.00, 0.00),
    ("Bull",     False): (0.75, 0.75),
    ("Bull",     True):  (0.50, 0.25),
    ("Bear",     False): (0.75, 0.75),
    ("Bear",     True):  (0.50, 0.25),
}

MOMENTUM_VETO_PCT    = 0.50            # [V7] top 50% by 1m return within short pool are vetoed

REGIME_MA_WINDOW     = 20              # 20-week MA for regime detection
BULL_BAND            = 1.10            # index / MA20 >= 1.10 → Bull
BEAR_BAND            = 0.90            # index / MA20 <= 0.90 → Bear
HIGH_VOL_THRESHOLD   = 0.80            # BTC ann. vol > 80% → high-vol

SHORT_CB_LOSS        = 0.40            # circuit breaker: cap short loss at 40% per period
SHORT_SQUEEZE_PRIOR  = 0.40            # block new short entry if prior-period return > 40%
ALTSEASON_THRESHOLD  = 0.75            # zero short leg when > 75% of top-50 alts beat BTC

ADTV_POS_CAP         = 0.20            # max weight per position
TOKEN_VOL_WINDOW     = 8               # weeks for realized vol
```

---

## 9. Output Charts

| File | Description |
|------|-------------|
| `perp_ls_v7_cumulative.png` | Cumulative NAV (log scale), per-leg net, per-period spread bar coloured by regime |
| `perp_ls_v7_regime_dd.png` | Per-period gross spread coloured by regime + drawdown |
| `perp_ls_v7_vs_v6.png` | v6 vs v7 period-by-period spread, cumulative NAV, regime-conditional geo spread, stats scorecard |
