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

### 4.4 Trade counts

| Leg | Opens | Closes | Total |
|-----|------:|-------:|------:|
| Long | 364 | 352 | 716 |
| Short | 342 | 305 | 647 |
| **Both legs** | **706** | **657** | **1,363** |
| Avg per period | — | — | **30.3** |

The high trade count (30 per monthly rebalance) follows directly from the ~82% average monthly turnover — roughly 18 of every 22 basket positions are replaced each period.

### 4.5 Avg basket size by regime

| Regime | N | Avg Long | Avg Short |
|--------|--:|--------:|--------:|
| Bull | 18 | 11.9 | 9.7 |
| Bear | 14 | 10.9 | 9.3 |
| Sideways | 13 | 11.2 | 10.4 |

Sideways periods report basket sizes even though no trades are executed (baskets are constructed for state-tracking but L/S scales are 0.00).

### 4.6 Most frequent basket tokens (of 45 periods)

**Most persistent long tokens (lowest long-run supply inflation):**

| Token | Periods | Frequency |
|-------|--------:|:--------:|
| VET | 14 | 31% |
| XLM | 11 | 24% |
| HOT | 11 | 24% |
| NEO | 10 | 22% |
| IOTX | 10 | 22% |
| GALA | 10 | 22% |
| CHZ | 10 | 22% |
| THETA | 9 | 20% |
| LPT | 8 | 18% |
| RSR | 8 | 18% |

**Most persistent short tokens (highest long-run supply inflation):**

| Token | Periods | Frequency |
|-------|--------:|:--------:|
| KAVA | 10 | 22% |
| AXS | 10 | 22% |
| FLOW | 9 | 20% |
| SAND | 9 | 20% |
| INJ | 9 | 20% |
| CELO | 8 | 18% |
| FIL | 8 | 18% |
| AR | 8 | 18% |
| KSM | 8 | 18% |
| 1INCH | 7 | 16% |

Long tokens are predominantly legacy/lower-emission layer-1s and utility tokens with flat or minimal ongoing issuance. Short tokens are predominantly DeFi infrastructure and gaming tokens with scheduled vesting/staking emission programmes still running.

### 4.7 Per-period basket composition

Complete basket per rebalancing date (truncated to fit; see `v7_full_basket_log.csv` for full token lists):

| Date | Rgm | Long basket | Short basket |
|------|-----|-------------|--------------|
| 2022-01-02 | Side | AR,AXS,KSM,NEO,SUSHI,THETA,VET | 1INCH,CELO,FIL,KAVA,NEAR,ROSE,SKL |
| 2022-02-06 | Bear | AR,IOTX,KNC,KSM,LPT,NEO,SUSHI,THETA,VET,YFI | 1INCH,ALICE,CELO,FIL,HBAR,KAVA,NEAR,ROSE,SKL |
| 2022-03-06 | Bear | AR,GALA,IOTX,KNC,KSM,LPT,NEO,SUSHI,THETA,VET,XLM | CELO,COTI,FIL,FLOW,HBAR,KAVA,NEAR,SAND,SKL |
| 2022-05-01 | Side | CHR,DENT,GALA,IOTX,KNC,KSM,LPT,SNX,SUSHI,THETA,XLM | FIL,FLOW,HBAR,ICX,KAVA,SAND,SKL,WOO |
| 2022-06-05 | Bear | AR,C98,DENT,GALA,KNC,KSM,SNX,THETA,VET | 1INCH,FIL,FLOW,GRT,HBAR,ICX,KAVA,ROSE,SAND,SKL,WOO |
| 2022-07-03 | Bear | ALGO,CHZ,ENJ,GRT,HOT,KNC,VET,XLM,ZIL | AAVE,AR,BCH,C98,KAVA,KSM,LPT,YFI,ZEC |
| 2022-08-07 | Bear | ANKR,BAT,DENT,GALA,HBAR,HOT,MANA,VET,XLM,ZIL | BCH,C98,CELO,COMP,CTSI,KSM,ZEC |
| 2022-09-04 | Bear | ANKR,AXS,DENT,GRT,HOT,ICX,LRC,MANA,ONE,VET | AR,BCH,HBAR,KSM,LTC,YFI,ZEC |
| 2022-10-02 | Bear | HBAR,ICX,IOST,IOTX,KAVA,LRC,ROSE,RVN,VET | AXS,DASH,IMX,KNC,MANA,NEO,RLC |
| 2022-11-06 | Side | 1INCH,COTI,DYDX,ENS,FIL,FLOW,IOTX,KAVA,LRC,PEOPLE,… | CHZ,DASH,IMX,KSM,NEO,QNT,RUNE,XMR |
| 2022-12-04 | Bear | API3,CHZ,GALA,HBAR,IMX,LRC,NEAR,QNT,RUNE,SNX,XLM | AXS,DASH,FIL,GMT,PEOPLE,QTUM,RLC,SUSHI,VET,ZEN |
| 2023-01-01 | Bear | 1INCH,APE,API3,GALA,LDO,NEO,RLC,RSR,XLM | BCH,ENJ,FLOW,INJ,MASK,QTUM,VET,YFI,ZEC,ZIL |
| 2023-03-05 | Bull | 1INCH,ALGO,GMT,HOT,IOST,IOTX,RSR,SKL,THETA,VET,XTZ,… | ACH,AR,BCH,CELO,CRV,DASH,IMX,KSM,XLM,ZRX |
| 2023-04-02 | Bull | CELR,HOT,IOTX,LPT,LRC,MINA,T,VET,ZEC,ZIL | AR,CELO,DASH,HBAR,JOE,KAVA,KSM,NEAR,NEO,ONT,RUNE,SKL |
| 2023-05-07 | Bull | ALGO,HOT,INJ,IOTX,JASMY,MINA,ONE,OP,RSR,ZRX | AR,ASTR,CELO,CTSI,GALA,NEO,ONT,QNT,UNI,XLM |
| 2023-06-04 | Side | ACH,CHZ,CKB,HOT,IMX,INJ,JASMY,OP,STORJ,UNI,XLM | ASTR,BAND,COMP,CTSI,ENS,GMT,IOTX,RSR,STX,SUSHI,XMR,… |
| 2023-07-02 | Side | AAVE,APE,AR,CKB,DASH,INJ,KAVA,KSM,NEAR,SFP,SNX,XLM | 1INCH,ACH,ICX,LINK,LPT,OP,RSR,SKL,SSV,THETA,XMR,ZIL |
| 2023-08-06 | Side | ASTR,CELO,COMP,CTSI,ENS,LRC,NEO,SAND,T,WOO,XLM | APE,API3,FIL,FLOW,GMT,GMX,HOT,JOE,KNC,ZEN,ZRX |
| 2023-09-03 | Side | DYDX,EGLD,ENS,HBAR,HOT,ICP,MINA,NEO,ONE,SUSHI,XMR | ANKR,APE,GMX,INJ,STG,UMA,XLM,XTZ,YFI,ZEC,ZRX |
| 2023-10-01 | Side | ACH,CHZ,GMX,GRT,IMX,LPT,MANA,NEO,OP,RSR,ZEC,ZRX | ANKR,AR,CFX,CTSI,FIL,INJ,KAVA,LDO,MINA,QTUM,SFP,UNI |
| 2023-11-05 | Bull | APE,ASTR,CHZ,GRT,HOT,IMX,INJ,ONE,OP,POLYX,QTUM,YFI,… | ANKR,APT,AXS,CTSI,ENS,JASMY,KNC,LRC,TWT,VET,WOO,ZIL |
| 2023-12-03 | Bull | 1INCH,APE,APT,ASTR,BAND,BAT,DASH,JASMY,ONG,OP,SAND,… | LQTY,LRC,NEO,POLYX,QNT,TRB,TWT |
| 2024-01-07 | Bull | ASTR,BSV,DASH,EGLD,FLOW,GMX,INJ,JOE,KSM,PENDLE,QNT,T | ATOM,BAND,BICO,CFX,GAS,KAVA,MOVR,ONE,SAND,YFI,ZEC |
| 2024-02-04 | Bull | ALGO,CRV,FLOW,GMX,JASMY,JUP,LDO,LRC,QTUM,TWT,XLM,XTZ | 1INCH,APE,ARB,EGLD,GAS,ICX,IMX,INJ,ROSE |
| 2024-04-07 | Bull | AXL,CHZ,ENJ,GALA,GRT,JASMY,MASK,QTUM,SSV,SUI,XMR,ZIL | AAVE,ALT,ASTR,CRV,HOT,LPT,LRC,METIS,SUSHI,TWT,XLM |
| 2024-05-05 | Bull | ASTR,CHZ,CKB,ETHW,ONT,ROSE,RVN,SFP,SUI,WLD,XMR | AAVE,ALT,BLUR,CAKE,FET,FLOW,GMT,JUP,LDO,POLYX,SUPER,… |
| 2024-06-02 | Bull | ASTR,BAT,CAKE,CKB,HOT,LPT,MANA,PENDLE,STX,WLD,XMR,ZRX | AAVE,ALT,AR,DASH,GALA,INJ,POLYX,QNT,SSV,UNI,WOO,XL… |
| 2024-07-07 | Bear | 1INCH,AR,AXS,CFX,CRV,FIL,IOTX,LPT,SAND,SEI,TWT,UNI,… | BICO,CHZ,CKB,FET,RSR,STRK |
| 2024-08-04 | Bear | ARB,BLUR,CAKE,EGLD,ETC,FLOW,KAS,LDO,NEO,SNX,XTZ | ANKR,AXS,CKB,GAS,GMX,ILV,INJ,JUP,SUI,SUPER,TIA |
| 2024-09-01 | Bear | CAKE,CELO,CHZ,DYDX,EGLD,ETC,LDO,PEOPLE,ROSE,SFP,SN… | ARB,AXL,AXS,CKB,ENJ,ENS,GMX,HOT,IMX,KAVA,SAND,SUPER,… |
| 2024-10-06 | Side | AAVE,ASTR,BSV,CAKE,CHZ,DYDX,IMX,JASMY,RPL,SSV,T,THETA | ANKR,ARB,AXS,ETC,HBAR,JTO,JUP,MASK,NEO,WOO,XTZ,ZIL |
| 2024-11-03 | Side | AAVE,AXL,AXS,CFX,FLOW,HBAR,IOTX,JASMY,MANA,PYTH,SA… | CELO,ICP,KAS,NEO,OP,SSV,STX,WOO |
| 2025-01-05 | Bull | APE,ARB,ATOM,AXL,BLUR,CFX,ETHW,ID,LTC,XMR,ZIL,ZRX | ASTR,IOTX,KAS,RUNE,SAFE,STX |
| 2025-02-02 | Bull | AR,AXS,CAKE,ENS,ETHFI,GALA,GRT,NEO,OP,QNT,STX,TIA | AERO,AXL,MEW,ORDI,SUPER,UNI |
| 2025-03-02 | Bull | 1INCH,ANKR,APT,CRV,DASH,ETC,ETHFI,GRT,HOT,INJ,JTO,RSR | AAVE,AR,JASMY,MEW,QTUM,SAND,SUPER |
| 2025-04-06 | Side | ANKR,CRV,GALA,ICP,IOTA,JTO,KAS,KAVA,RENDER,SAND,TH… | APE,ATH,EGLD,FLOW,IMX,JASMY,KSM,MEW,NOT,ONDO,UNI,Z… |
| 2025-05-04 | Side | ALGO,ARB,ENA,FET,IOTA,PENDLE,RENDER,THETA,TRUMP,ZK,… | AXS,CKB,EGLD,MANA,NOT,QNT,RUNE,SAFE,ZETA |
| 2025-06-01 | Bull | AAVE,ATOM,FIL,GALA,IOTA,LDO,OP,QTUM,RSR,STRK,STX,TIA | 1INCH,BAT,CAKE,CFX,CRV,FET,QNT,ROSE,SNX,THETA |
| 2025-07-06 | Bull | AAVE,APE,CHZ,EGLD,IMX,LPT,MANA,ONDO,RSR,RUNE,TRUMP | ALGO,AXL,AXS,CRV,GALA,ICP,INJ,JTO,MINA,THETA,XMR |
| 2025-08-03 | Bull | AERO,ATH,AXS,CFX,CKB,DOT,FIL,IMX,JTO,MANA,QNT,RUNE,… | ALGO,AXL,KAS,MEW,THETA,VIRTUAL,WLD |
| 2025-09-07 | Bull | 1INCH,AXS,CKB,EIGEN,ETHFI,KAIA,ONDO,RSR,VIRTUAL,ZI… | ATOM,COMP,DOT,EGLD,IMX,LDO,NEAR,PENDLE,POL,SAND,XTZ |
| 2025-10-05 | Bull | AERO,ATOM,COMP,DRIFT,ETC,ETHFI,FET,MOCA,NEO,STX,W | AAVE,BSV,FLOW,IOTA,KAS,PYTH,SAND,TAO,VET |
| 2025-11-02 | Side | APT,DOT,ETC,ICP,NEAR,POL,TAO,VET,WLD,XMR,ZRO | AERO,AXS,CFX,CRV,FLOW,GALA,JUP,KSM,MANA,MINA,MOVE,… |
| 2025-12-07 | Bear | APT,ARB,DOT,ETC,FIL,HBAR,ICP,NEAR,POL,TON,VET,WLD,ZEC | 1INCH,AXS,CRV,DYDX,ENS,INJ,KAITO,MANA,MORPHO,SAND,… |
| 2026-01-04 | Bear | APT,DOT,ENA,ETC,FIL,HBAR,ICP,KAS,PENDLE,POL,TAO,TO… | ATOM,FET,FORM,GRT,INJ,JASMY,MANA,QTUM,STRK,UNI |

---

## 5. Full History Results (v7_full — START_DATE=None)

`perpetual_ls_v7_full.py` runs the identical strategy logic from the earliest date that Binance USDT-M perp data is available. **No pre-2021 data exists** — the Binance perp universe was too thin before mid-2021 to form valid baskets. The full-history run adds 6 extra periods (2021-06 → 2021-12) vs the 2022-only backtest.

### 5.1 Full history performance

| Series | Ann. Return | Vol | Sharpe | Sharpe* | MaxDD |
|--------|:-----------:|:---:|:------:|:-------:|:-----:|
| Long leg (net) | −35.70% | +79.25% | −0.451 | −0.339 | −92.06% |
| Short leg (net)† | +10.05% | +66.21% | +0.152 | +0.665 | −71.96% |
| L/S Spread (net) | +5.72% | +34.28% | +0.167 | +0.368 | −42.23% |
| **L/S Combined (net)** | **+3.77%** | **+17.07%** | **+0.221** | **+0.351** | **−22.16%** |

| Metric | Value |
|--------|-------|
| Rebalancing periods | 51 |
| Avg basket size | Long 11.0 / Short 9.5 |
| Avg monthly turnover | Long 75.0% / Short 79.7% |
| Regime breakdown | Bull 20 / Bear 14 / Sideways 17 |
| CB triggered | 3 / 51 periods (5.9%) |
| Alt-season veto | 2 periods |
| Momentum veto | 42 token-periods |

### 5.2 Regime-conditional spread (full history)

| Regime | N | Mean Spread | Win Rate | Ann. Geo Spread |
|--------|:-:|:-----------:|:--------:|:---------------:|
| Bull | 20 | +0.02% | 70.0% | **−6.90%** |
| Bear | 14 | +3.36% | 50.0% | **+37.75%** |
| Sideways | 17 | +0.00% | 0.0% | +0.00% (cash) |

The 2021 Bull periods (altseason) drag Bull geo spread from +18.7% (2022+) to −6.9% (full history). The 2021 altseason is the worst possible environment: altcoins ripping in all directions, short leg exposed to violent squeezes. The alt-season veto fires in 2 of these periods (correctly) but cannot neutralise all 6 extra Bull/Sideways periods.

### 5.3 Performance by window

| Metric | All periods (2021+) | 2022+ only |
|--------|:-------------------:|:----------:|
| Ann. Return | +3.77% | +3.97% |
| Vol | +17.07% | +18.05% |
| Sharpe | +0.221 | +0.220 |
| MaxDD | −22.16% | −20.73% |

The additional 6 periods slightly reduce return (+3.77% vs +3.97%) and MaxDD worsens (−22.16% vs −20.73%) — consistent with 2021 being a particularly difficult environment for the short leg.

### 5.4 Full cost attribution (v7_full)

| Component | Avg/period (bps) | Cumulative |
|-----------|:----------------:|:----------:|
| Fee drag — long (turnover-adj) | +5.3 | +0.0181 |
| Fee drag — short (turnover-adj) | +6.1 | +0.0207 |
| Slippage drag — long | +103.7 | +0.3527 |
| Slippage drag — short | +105.9 | +0.3601 |
| Funding drag — long | −48.1 | −0.1635 |
| Funding credit — short | +21.9 | +0.0743 |
| **Net total cost drag** | **+247.3** | **+0.8407** |
| **Annualised cost drag** | | **+15.44%** |

Slippage dominates (~104bps/period per leg). Gross combined return before costs would be approximately +19%/yr; costs reduce it to +3.77% net.

---

## 6. Version Comparison

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

## 7. Key Structural Lessons

### 7.1 Composite rank distribution vs fixed thresholds

A weighted average of correlated uniform [0,1] signals is not itself uniform — it is compressed toward 0.5 (central limit effect). Applying a fixed `LONG_ENTRY_PCT = 0.12` threshold to a compressed distribution selects far fewer tokens than intended (the 12th percentile of a compressed distribution is well above 0.12 in absolute value).

**Fix:** Compute thresholds at each date using `univ["pct_rank"].quantile(LONG_ENTRY_PCT)`. This is the correct method for any composite rank — always selects exactly the intended fraction of tokens regardless of distribution shape.

### 7.2 Net directional exposure kills relative-value strategies

Any L/S strategy with unequal leg scaling carries net directional exposure. In regimes where the underlying market moves strongly, this directionality can dominate the spread return. For a strategy where the fundamental edge is cross-sectional (supply-dilution rank), the position sizes on both legs should be symmetric (or at most slightly asymmetric) so that combined returns reflect the spread, not the market direction.

### 7.3 Period count as the fundamental constraint

Statistical reliability of any performance metric requires adequate sample size. With 19 periods (v6), the Bear geo spread is based on 11 observations — a 95% confidence interval of approximately ±60 percentage points annualised (using the observed vol). Moving to 45 periods triples the sample and narrows confidence intervals proportionally.

The lesson: strategies with regime-conditional logic should be evaluated on regime-conditional sample counts, not aggregate metrics. A strategy that looks attractive in 2 Bull periods and 11 Bear periods may simply have been in luck.

### 7.4 The BTC hedge double-counts natural hedging

A L/S altcoin strategy is already net-neutral to BTC beta in theory: longs and shorts both move with BTC in proportion to their beta, which largely cancels. Adding a BTC hedge overlay then shorts BTC again, producing a net short BTC position. In Bear regimes (BTC down), the hedge gains; in Bull regimes (BTC up), the hedge loses. Since the strategy is designed to run in both Bull and Bear, the hedge is directionally wrong in Bull and creates drag equal to the strategy's natural BTC beta carry.

---

## 8. Remaining Limitations

### 8.1 Funding drag is the primary cost driver

Net funding impact of −8.68% cumulative (−0.19%/period) represents approximately 2× the annualised combined net. If funding rates were zero, combined net would be approximately +8-12%/yr. Low-emission tokens are consistently heavily longed by the market; their perpetual funding rates are persistently positive (longs pay shorts). Any live implementation must model per-token funding rates dynamically and consider strategies to reduce long-leg funding cost (e.g., prefer tokens on exchanges with lower funding, or weight tokens inversely by their historical funding rate alongside supply rank).

### 8.2 Short leg absolute return remains negative (−44.67% gross)

The short basket contains high-emission tokens. In absolute terms, these tokens often also benefit from broad market rallies (positive BTC correlation), producing positive gross returns even as they underperform the long basket. The short leg earns from the *spread*, not from the basket returning negative — and the portfolio captures that spread via the combined return. Observers should not interpret the negative short gross return as a failure; it reflects a Bull-dominated 2022-2026 period where most altcoins gained in absolute terms.

### 8.3 Statistical significance

At Sharpe +0.220 with 45 monthly periods, the strategy is not yet statistically distinguishable from zero at conventional thresholds. The Lo HAC-corrected Sharpe of +0.357 is stronger. An extended backtest (pre-2022 data, if CMC supply history allows) or out-of-sample testing would be required before live deployment.

### 8.4 Turnover and capacity

Average monthly turnover of ~82% implies high transaction costs relative to the annualised return. At $5M per leg ($10M AUM), the taker fee + slippage model costs approximately 0.12%/period in fees and slippage per leg. The strategy capacity ceiling remains approximately $4-10M total AUM before market impact materially erodes the spread.

---

## 9. Bug Fixes Applied (2026-03-04)

Two defensive fixes were backported from `perpetual_ls_v7_full.py` into `perpetual_ls_v7.py` after being discovered during supply filter investigation work:

### Bug 1: `pandas.apply()` on empty Series returns wrong dtype

**Symptom:** KeyError crash on rebalance dates where the investable universe was empty after filters.

**Root cause:** `pandas.Series.apply()` called on an empty Series defaults to returning a `float64` Series. Pandas then interprets a `float64` boolean mask as *column selection* rather than *row selection*, producing a 0-column DataFrame. Any subsequent column access (e.g. `univ["pct_rank"]`) raises a KeyError.

**Fix:**
```python
# Before (buggy on empty universe)
univ = univ[univ["symbol"].apply(
    lambda s: pd.notna(onboard_map.get(s)) and onboard_map.get(s) <= t0)]

# After (explicit bool cast)
univ = univ[univ["symbol"].apply(
    lambda s: pd.notna(onboard_map.get(s)) and onboard_map.get(s) <= t0
).astype(bool)]
```
Applied to both the onboard-date filter and the ADTV filter.

### Bug 2: Missing early-exit guard for empty universe

**Symptom:** Same crash scenario — if `inf_snap` has no rows matching `snapshot_date == t0` the subsequent `.apply()` would hit the above bug before reaching the basket-size guard.

**Fix:**
```python
univ = inf_snap[inf_snap["snapshot_date"] == t0].copy()
if len(univ) == 0:
    continue
```

### Supply filter investigation (not applied)

The `supply_data_integrity.md` document recommended filtering tokens with anomalous period-over-period supply changes to exclude ve-token/airdrop/bridge artefacts.

**Finding:** The filter is counterproductive at every calibrated threshold (50%–5000%). High-MoM supply tokens are the strategy's intended short candidates — removing them eliminates the cross-sectional inflation signal. CMC monthly supply data has a median MoM change of 55% and a 90th percentile of 5623%; any filtering threshold tight enough to catch genuine artefacts also removes the best short signals. The existing 2-98% winsorisation per period already clips the worst outliers before cross-sectional ranking.

**Decision:** No supply stability filter applied. `SUPPLY_STABILITY_THRESH = 5.00` is retained as a config constant but unused.

### Header string correction

The `perpetual_ls_v7.py` print headers incorrectly described the signal as "3-layer (4w+13w+52w)". The 4w component was dropped in the final v7 design (see §2, [V7-7]). Corrected to "2-layer signal (13w+52w) | Symmetric (0.75L, 0.75S)".

---

## 10. Script Reference

| File | Description |
|------|-------------|
| `perpetual_ls_v7.py` | **v7 (current best)** — monthly rebal, Sideways=cash, symmetric (0.75,0.75), momentum veto. **+3.97% ann. net, Sharpe +0.220** (2022-01 → 2026-02) |
| `perpetual_ls_v7_full.py` | v7 full history — identical logic, START_DATE=None. **+3.77% ann. net, Sharpe +0.221** (2021-06 → 2026-02, 51 periods). Adds basket log CSV, trade counts, cost attribution, per-window analysis |
| `perpetual_ls_v6.py` | v6 — regime-aware bi-monthly Bull step, BTC hedge. +0.10% ann. net |
| `perpetual_ls_v5.py` | v5 — Sideways=cash, no BTC hedge. −2.74% ann. net |
| `perpetual_ls_v4.py` | v4 — Binance data, monthly rebal, BTC hedge. −5.11% ann. net |
| `perpetual_ls_experiments.py` | 12 isolated experiments on v6 base (A-J) |
| `fetch_binance_data.py` | Downloads 396-symbol weekly OHLCV + funding from Binance REST API |

```bash
# Run v7 (2022+)
python perpetual_ls_v7.py

# Run v7 full history (2021+)
python perpetual_ls_v7_full.py

# Full version progression
python perpetual_ls_v4.py    # -5.11% ann.
python perpetual_ls_v5.py    # -2.74% ann.
python perpetual_ls_v6.py    # +0.10% ann.
python perpetual_ls_v7.py    # +3.97% ann. (2022+)
python perpetual_ls_v7_full.py  # +3.77% ann. (2021+)
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

## 11. Output Charts

**v7 (2022+):**

| File | Description |
|------|-------------|
| `perp_ls_v7_cumulative.png` | Cumulative NAV (log scale), per-leg net, per-period spread bar coloured by regime |
| `perp_ls_v7_regime_dd.png` | Per-period gross spread coloured by regime + drawdown |
| `perp_ls_v7_vs_v6.png` | v6 vs v7 period-by-period spread, cumulative NAV, regime-conditional geo spread, stats scorecard |

**v7_full (2021+):**

| File | Description |
|------|-------------|
| `perp_ls_v7_full_cumulative.png` | Full-history cumulative NAV + per-period spread bars |
| `perp_ls_v7_full_regime_dd.png` | Full-history per-period spread by regime + drawdown |
| `perp_ls_v7_full_vs_v6.png` | v6 vs v7_full comparison |
| `v7_full_basket_log.csv` | Complete basket record: 51 periods, long/short token lists, opens/closes, regime per period |
