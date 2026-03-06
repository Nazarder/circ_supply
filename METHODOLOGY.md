# Supply-Dilution L/S Perpetuals — Strategy v9

**Version:** 9 (de-overfit)
**Last updated:** 2026-03-06
**Backtest period:** 2022-01-02 to 2026-01-04 | 44 monthly periods

---

## Quick Reference

| Metric | Parametric slippage | Real book + ADTV slippage |
|--------|--------------------|--------------------------:|
| SR (HAC-corrected) | +1.154 | +1.773 |
| Ann. return | +14.8% | +24.1% |
| MaxDD | -14.1% | -11.4% |
| Sortino | +1.430 | — |

**Realistic live-trading range (ZEC excluded, CB=50%):** SR +0.84, Ann +11.6%, MDD -18.0%

---

## Charts

| Chart | File |
|-------|------|
| Cumulative net returns + regime | `perp_ls_v9_cumulative.png` |
| Regime drawdown | `perp_ls_v9_regime_dd.png` |
| v9 vs v8 comparison | `perp_ls_v9_vs_v8.png` |

---

## 1. Data Sources

### Signal — CoinMarketCap historical
- File: `cmc_historical_top300_filtered_with_supply.csv`
- Coverage: 2017-01-01 to 2026-02-22 | 135,639 rows | 2,266 symbols
- Fields: weekly CMC rank, market cap, circulating supply, close price
- Used for: supply inflation signal, universe construction, regime index
- **Known limitation:** CMC retroactively revises historical supply figures. Point-in-time data not available publicly. Results should be interpreted with this in mind.

### Price & funding — Binance USDT-M Perpetuals
- File: `binance_perp_data/weekly_ohlcv.parquet` — 322 weeks x 396 symbols
- File: `binance_perp_data/symbol_meta.csv` — onboard dates, symbol mapping
- Funding: actual 8-hour rates from Binance historical API, summed weekly (51,346 rows)
- Used for: execution prices, P&L calculation, cost attribution, ADTV-based liquidity scaling

### Order book slippage — Binance live snapshot
- File: `orderbook_slippage.csv` — March 2026 snapshot, 396 symbols, 20 depth levels
- Fields: half-spread, VWAP market impact at $100K/$375K/$1M/$5M, depth, reference ADTV
- Fetched via: `fetch_orderbook_slippage.py` (public Binance `/fapi/v1/depth` endpoint, no API key needed)
- Used for: realistic per-token slippage with time-varying ADTV scaling

---

## 2. Universe Construction

Each period, before signal ranking:

1. CMC rank <= 200 and rank > 20 (excludes BTC, ETH, top 20)
2. Market cap >= $50M
3. Weekly ADTV >= $5M (Binance proxy)
4. >= 32 weeks of supply history (matches signal lookback — bug fixed from 26w)
5. Forward-fill supply gaps <= 1 period only
6. Exclude: stablecoins, CEX tokens, memecoins, wrapped tokens, commodity-backed tokens
7. Active Binance USDT-M perpetual listing required

Typical eligible universe: ~100-150 tokens per period.

---

## 3. Signal

Two-layer cross-sectional supply inflation rank:

```
inf_32w[t]  = (supply[t] - supply[t-32]) / supply[t-32]
inf_52w[t]  = (supply[t] - supply[t-52]) / supply[t-52]

Both winsorized at [2%, 98%] cross-sectionally.

pct_rank_32w   = cross-sectional percentile rank of inf_32w
pct_rank_52w   = cross-sectional percentile rank of inf_52w

composite_rank = 0.50 * pct_rank_32w + 0.50 * pct_rank_52w
```

Low composite rank = low supply inflation = Long candidate
High composite rank = high supply inflation = Short candidate

---

## 4. Portfolio Construction

### Entry/exit thresholds (data-driven quantiles, recomputed each period)

| Basket | Entry | Exit |
|--------|-------|------|
| Long | bottom 12th pct | above 18th pct |
| Short | top 12th pct (88th) | below 82nd pct |

Buffer bands keep existing positions unless they breach the exit threshold.

### Rules
- Equal-weighted within each basket
- ADTV position cap: max 20% of daily volume per token
- Min basket size: 6 tokens per leg (period skipped if fewer pass filters)
- Rebalancing: monthly, always

---

## 5. Regime Detection

### Altcoin index
CMC cap-weighted index of eligible universe (BTC/ETH excluded).

```
Bull      : index > 20w MA x 1.05
Bear      : index < 20w MA x 0.95
Sideways  : between the bands
```

Regime breakdown over 44 periods: Bull=22, Bear=15, Sideways=7

### BTC volatility overlay
BTC 8-week realised vol vs 80th pct historical threshold. High-vol scales down exposure.

### L/S scaling by regime

| State | Long | Short |
|-------|------|-------|
| Sideways | 0.00 | 0.00 (cash) |
| Bull, low-vol | 0.75 | 0.75 |
| Bull, high-vol | 0.50 | 0.25 |
| Bear, low-vol | 0.75 | 0.75 |
| Bear, high-vol | 0.50 | 0.25 |

---

## 6. Execution & Cost Model

### Fees
2 x 0.04% taker fee per leg on turnover fraction.

### Slippage (two models)

**Parametric (used in headline backtest):**
```
slip[s] = 0.0005 / (CMC_24h_volume[s] / market_cap[s])
capped at 2%
```

**Real book + ADTV scaling (honest model, $5M AUM):**
```
pos_per_token  = AUM * 0.75 / n_tokens
impact_t[s]    = impact_375K[s]
                 * sqrt(pos / 375_000)          # position size scaling
                 * sqrt(adtv_ref[s] / adtv_t[s])  # liquidity scaling (capped at 5x)
slip[s]        = max(half_spread[s], impact_t[s]), capped at 2%
```

ADTV scaling means bear-market periods automatically receive higher slippage
(lower historical ADTV -> higher impact multiplier). No arbitrary multipliers needed.

### Funding
Actual Binance 8h rates summed weekly:
- Longs pay funding (drag)
- Shorts receive funding (credit)
- Net over backtest: +2.31% (positive — high-emission shorts carry positive funding)

### Risk controls

| Control | Setting | Periods triggered |
|---------|---------|-------------------|
| Short circuit breaker | Cap short loss at 40% per period | 5 / 44 (11%) |
| Altseason veto | Skip shorts if >75% alts beat BTC (4w) | 1 / 44 (2%) |
| Short squeeze filter | Exclude tokens up >40% prior month from shorts | Active each period |
| Momentum veto | OFF (0.000 dSR ablation) | — |
| Long quality veto | OFF (IS-tuned lookback removed) | — |

---

## 7. Net Performance

### Full return table

| Series | Ann. Return | Vol | Sharpe | Sharpe* (HAC) | Sortino | MaxDD |
|--------|------------|-----|--------|---------------|---------|-------|
| Long basket (gross) | -20.5% | 95.2% | -0.215 | +0.228 | -0.259 | -69.0% |
| Short basket (gross) | -47.4% | 82.1% | -0.577 | -0.739 | -0.508 | -93.0% |
| Long leg (net) | -28.2% | 93.2% | -0.303 | -0.005 | -0.359 | -77.5% |
| Short leg (net) | -4.2% | 80.3% | -0.052 | +0.565 | -0.049 | -71.0% |
| **L/S Combined (net)** | **+14.8%** | **17.7%** | **+0.838** | **+1.154** | **+1.430** | **-14.1%** |

Short leg net: positive = profit for short. Sharpe* = Lo (2002) HAC-corrected.

### Spread statistics

| Metric | Value |
|--------|-------|
| Win rate (long > short gross) | 50.0% (22/44) |
| Mean period spread | +3.93% |
| Spread vol (annualised) | +32.7% |
| Spread skewness | +2.11 (right-skewed) |
| Spread excess kurtosis | 6.78 |

### Regime-conditional spread (gross)

| Regime | N | Mean spread | Win rate | Ann. geo spread |
|--------|---|------------|---------|----------------|
| Bull | 22 | +4.61% | 63.6% | +65.7% |
| Bear | 15 | +4.77% | 53.3% | +64.5% |
| Sideways | 7 | 0.00% | — | 0.00% |

### Funding attribution

| | Cumulative | Per period avg |
|-|-----------|---------------|
| Long leg (drag) | -2.95% | -0.07% |
| Short leg (credit) | +5.26% | +0.12% |
| **Net** | **+2.31%** | **+0.05%** |

---

## 8. Trade Counts & Activity

| Metric | Value |
|--------|-------|
| Rebalancing periods | 44 |
| Avg long basket size | 11.7 tokens |
| Avg short basket size | 10.9 tokens |
| Avg effective scale (L / S) | 0.63x / 0.61x |
| Circuit breaker triggered | 5 periods (11%) |
| Altseason veto triggered | 1 period (2%) |
| Avg monthly turnover | Long 31.1% / Short 38.4% |
| Long opens / closes | 101 / 93 |
| Short opens / closes | 133 / 84 |
| **Total trades** | **411 (9.3 per period)** |

### Basket size by regime

| Regime | N | Avg Long | Avg Short |
|--------|---|---------|---------|
| Bull | 22 | 12.1 | 11.1 |
| Bear | 15 | 11.1 | 10.7 |
| Sideways | 7 | 11.7 | 10.7 |

---

## 9. Token Concentration

### Most persistent long positions (44 periods)

| Token | Periods | % | Notes |
|-------|---------|---|-------|
| NEO | 43 | 98% | Zero inflation since initial dist. |
| THETA | 40 | 91% | Fixed supply model |
| QNT | 35 | 80% | Capped supply |
| ZRX | 30 | 68% | Low ongoing emissions |
| IOTX | 29 | 66% | Deflationary mechanics |
| AR | 26 | 59% | Storage endowment model |
| KSM | 23 | 52% | Moderate stable inflation |
| ZEC | 17 | 39% | Low inflation; Sep-2025 +242% event |
| YFI | 17 | 39% | Hard capped |
| BAT | 15 | 34% | Fixed supply |

### Most persistent short positions (44 periods)

| Token | Periods | % | Notes |
|-------|---------|---|-------|
| KAVA | 27 | 61% | High staking emissions |
| 1INCH | 19 | 43% | Ongoing vesting unlocks |
| FIL | 16 | 36% | Block reward inflation |
| OP | 16 | 36% | Scheduled unlocks |
| GMT | 15 | 34% | Move-to-earn emissions |
| DYDX | 14 | 32% | Governance unlock schedule |
| JUP | 14 | 32% | Recent launch, large unlock calendar |
| GALA | 13 | 30% | High mint rate |
| ARB | 13 | 30% | Ongoing unlock schedule |
| SEI | 13 | 30% | Validator + ecosystem emissions |

---

## 10. Per-Period Baskets

| Date | Regime | Long | Short |
|------|--------|------|-------|
| 2022-01 | Sideways | AR,BAT,KSM,NEO,RLC,SUSHI,THETA,VET | CELO,COTI,FIL,HBAR,KAVA,NEAR,OGN |
| 2022-02 | Bear | AR,BAT,IOTX,KNC,KSM,LPT,NEO,SUSHI,THETA | ALGO,CELO,COTI,FIL,HBAR,KAVA,NEAR,RUNE |
| 2022-03 | Bear | AR,IOTX,KNC,KSM,LPT,NEO,SNX,SUSHI,THETA,VET | ALGO,CELO,COTI,FIL,GRT,HBAR,KAVA,NEAR,ROSE,SKL |
| 2022-05 | Bear | AR,IOTX,KNC,KSM,LPT,NEO,SNX,SUSHI,THETA,VET | 1INCH,ALGO,CELO,FIL,FLOW,HBAR,KAVA,ROSE,SKL |
| 2022-06 | Bear | AR,DENT,KNC,KSM,NEO,SNX,SUSHI,THETA,VET | 1INCH,CELO,FIL,FLOW,GRT,HBAR,KAVA,ROSE,SKL,WOO |
| 2022-07 | Bear | AR,IOTX,KNC,KSM,NEO,ONT,SNX,SUSHI,THETA | 1INCH,CELO,FIL,FLOW,GRT,KAVA,ROSE,SKL,WOO |
| 2022-09 | Bear | AR,DENT,GALA,IOTX,KSM,NEO,ONT,SUSHI,THETA,YFI | 1INCH,FIL,FLOW,GRT,INJ,KAVA,ROSE,RSR,SAND,SNX |
| 2022-10 | Bear | AR,DENT,GALA,IOTX,KSM,NEO,ONT,SUSHI,THETA,YFI,ZRX | 1INCH,FIL,FLOW,GRT,INJ,KAVA,ROSE,SAND,SNX |
| 2022-11 | Bull | AR,BAT,GALA,IOTX,KNC,KSM,NEO,QNT,SUSHI,THETA,YFI | 1INCH,AXS,FIL,FLOW,KAVA,ROSE,RSR,SAND,SNX |
| 2022-12 | Bear | AR,BAT,DYDX,GALA,IOTX,KNC,KSM,NEO,QNT,SUSHI,THETA,YFI | 1INCH,AXS,FIL,FLOW,INJ,KAVA,ROSE,RSR,SAND,SNX |
| 2023-01 | Bear | AR,BAT,DYDX,GALA,KNC,KSM,NEO,ONT,QNT,THETA,YFI | 1INCH,AXS,FIL,GRT,INJ,KAVA,RSR,SAND,SNX,SUSHI |
| 2023-03 | Bull | AR,BAT,ENS,IOTX,KSM,NEO,ONT,QNT,RUNE,THETA,YFI | 1INCH,BAND,CFX,DYDX,FIL,IMX,KAVA,MINA,RSR,SNX,SUSHI |
| 2023-04 | Bull | AR,ATOM,BAT,ENS,HOT,KSM,NEO,QNT,RUNE,THETA,YFI,ZRX | 1INCH,BAND,DYDX,FIL,IMX,KAVA,MINA,SNX,SUSHI |
| 2023-05 | Bull | AR,ATOM,BAT,IOTX,KSM,NEO,ONT,QNT,RUNE,UNI,YFI,ZRX | 1INCH,AXS,BAND,DYDX,FIL,ICP,IMX,KAVA,MINA,SUSHI |
| 2023-06 | Sideways | AR,BAT,HOT,IOTX,KSM,NEO,ONT,QNT,RLC,UNI,YFI,ZRX | 1INCH,API3,BAND,DYDX,FIL,ICP,IMX,KAVA,LDO,SUSHI |
| 2023-07 | Bull | AR,BAT,HOT,IOTX,KSM,NEO,QNT,RLC,THETA,UNI,YFI,ZRX | 1INCH,BAND,FIL,GALA,GMT,ICP,IMX,KAVA,LDO,MASK,SUSHI |
| 2023-08 | Sideways | BAT,FLOW,HOT,IOTX,KSM,NEO,ONT,QNT,STORJ,THETA,UNI,SSV | 1INCH,AR,BAND,CRV,DYDX,GALA,GMT,ICP,IMX,KAVA,LDO,MASK |
| 2023-09 | Bear | BAT,FLOW,HOT,KSM,NEO,ONT,QNT,SSV,STORJ,THETA,UNI,YFI | 1INCH,ACH,AR,AXS,BAND,GALA,GMT,ICP,IMX,KAVA,MASK,SFP |
| 2023-10 | Sideways | FLOW,HOT,KSM,NEO,ONT,QNT,SSV,STORJ,THETA,UNI,YFI,ZRX | 1INCH,ACH,AR,BICO,C98,CFX,GALA,GMT,ICP,IMX,KAVA,MASK,OP |
| 2023-11 | Bull | ENJ,FLOW,HOT,IOST,IOTX,KSM,NEO,ONT,QNT,SSV,STORJ,YFI | 1INCH,ACH,BICO,CFX,GALA,GMT,KAVA,OP,SFP |
| 2023-12 | Bull | ENJ,HOT,IOTX,KSM,NEO,ONT,QNT,SSV,STORJ,THETA,YFI,ZRX | 1INCH,ACH,AR,CFX,GALA,GAS,GMT,ILV,KAVA,OP,SFP |
| 2024-01 | Bull | BAT,ETHW,HOT,IOTX,KSM,NEO,ONT,PEOPLE,QNT,SSV,STORJ,TWT | 1INCH,APT,AR,CFX,CRV,GALA,GAS,GMT,ILV,IMX,KAVA |
| 2024-02 | Bull | APE,BAT,HOT,IOTX,KSM,NEO,QNT,SSV,THETA,TWT,YFI,ZRX | APT,CFX,CRV,ENJ,FLOW,GALA,GAS,GMT,IMX,KAVA,OP |
| 2024-04 | Bull | BAT,ETHW,GLM,HOT,IOTX,NEO,QNT,QTUM,SSV,THETA,TWT,VET | APT,ARB,AXL,DYDX,GALA,GAS,GMT,KAVA,OP |
| 2024-05 | Bull | ANKR,ETHW,GLM,HOT,IOTX,NEO,QNT,RSR,SSV,THETA,TWT,ZEC | APE,APT,ARB,AXL,BLUR,DYDX,GALA,GAS,GMT,KAVA,OP |
| 2024-06 | Bull | ANKR,GLM,IOTX,NEO,PEOPLE,QNT,RUNE,SSV,THETA,TWT,ZEC,ZRX | APE,APT,ARB,AXL,BLUR,DYDX,ENJ,GALA,GMT,KAVA,OP,YGG |
| 2024-07 | Bear | ANKR,GLM,IOTX,NEO,PENDLE,PEOPLE,QNT,RSR,RUNE,SSV,THETA | APE,APT,ARB,AXL,BLUR,DYDX,ENJ,FET,GALA,GMT,ID,KSM,OP |
| 2024-08 | Bear | ANKR,GLM,IOTX,NEO,PENDLE,PEOPLE,QNT,RUNE,SSV,THETA,ZRX | APE,APT,ARB,AXL,BLUR,DYDX,ENJ,FET,GALA,GMT,ID,JUP,KSM |
| 2024-09 | Bear | AXS,COMP,ENS,HBAR,MANA,PENDLE,ROSE,RUNE,SFP,SSV,ZEC,ZRX | ATOM,CKB,DYDX,ENJ,FET,FLOW,JUP,KAVA,KSM,MASK,POLYX,SEI |
| 2024-10 | Sideways | ANKR,IOTX,NEO,ORDI,PENDLE,PEOPLE,QNT,RUNE,THETA,TWT,ZRX | ARB,AXL,ENJ,FET,GMT,ID,KSM,OP |
| 2024-11 | Bull | ETHW,GLM,IOTX,NEO,ORDI,PENDLE,PEOPLE,QNT,RUNE,THETA,ZRX | ALT,ARB,AXL,DYDX,ENJ,FET,GMT,ID,KSM,SAFE,SEI,WLD |
| 2025-01 | Bull | ETHW,GLM,IOTX,KAVA,NEO,ORDI,QNT,THETA,TWT,ZEC,ZRX | APE,ARB,AXL,DYDX,FET,ID,KSM,SAFE,SEI,STRK,WLD |
| 2025-02 | Bull | ANKR,GAS,GLM,KAVA,MASK,NEO,ORDI,QNT,THETA,TWT,ZEC,ZRX | APE,ARB,AXL,DEXE,DYDX,FET,ID,JUP,SEI,STRK,TIA,WLD |
| 2025-03 | Bull | ANKR,GAS,GLM,KAVA,NEO,ORDI,QNT,THETA,TWT,WOO,XMR,ZEC,ZRX | APE,ARB,AXL,DEXE,ID,JTO,JUP,OP,SAFE,SEI,STRK,TIA,WLD |
| 2025-04 | Sideways | ANKR,AR,ATOM,GLM,IOTX,KAVA,NEO,QNT,THETA,XMR,ZEC,ZRX | ARB,DEXE,ID,JTO,JUP,ONDO,OP,SAFE,SEI,STRK,TIA,TRUMP,W |
| 2025-05 | Bull | AR,ATOM,JST,KAVA,NEO,PENDLE,QNT,THETA,XMR,ZEC,ZRX | ARB,DEXE,ID,JTO,JUP,ONDO,OP,SAFE,STRK,TIA,TRUMP,ZETA |
| 2025-06 | Bull | AR,ATOM,IOTX,KAVA,NEO,QNT,THETA,XMR,ZRX | ARB,DEXE,JTO,JUP,ONDO,OP,PYTH,SEI,STRK,TIA,TRUMP,WLD |
| 2025-07 | Bull | AR,ATOM,FET,GAS,KAVA,MEW,NEO,QNT,THETA,XMR,ZRX | DEXE,ENA,ETHFI,JTO,JUP,ONDO,OP,PYTH,SEI,STRK,TIA,TRUMP |
| 2025-08 | Bull | AR,ATOM,FET,GLM,IOTX,KAVA,MEW,NEO,NOT,QNT,THETA,XMR,ZRX | ETHFI,JTO,JUP,ONDO,OP,PYTH,SEI,STRK,SUN,TIA,TRUMP,WLD |
| 2025-09 | Bull | AR,FET,IOTX,JASMY,KAVA,LDO,NEO,QNT,THETA,XMR,ZEC,ZRX | ENA,ETHFI,JTO,JUP,OP,SEI,STRK,TIA,W,WLD,ZK,ZRO |
| 2025-10 | Bull | AR,FET,JASMY,KAVA,LDO,NEO,QNT,THETA,TON,XMR,ZEC | ENA,JTO,JUP,SEI,SKY,STRK,TIA,WLD,ZK,ZRO |
| 2025-11 | Sideways | APE,AR,FET,JASMY,LDO,NEO,QNT,RUNE,THETA,TON,XMR,ZEC,ZRX | ATH,ENA,ETHFI,JTO,JUP,SEI,STRK,TIA,W,WLD,ZK,ZRO |
| 2025-12 | Bear | APE,AR,FET,GLM,JASMY,JST,LDO,NEO,QNT,RUNE,THETA,TON | ENA,ETHFI,JTO,JUP,PYTH,SEI,SKY,STRK,TIA,WLD,ZK,ZRO |
| 2026-01 | Bear | APE,AR,FET,GAS,JASMY,LDO,NEO,QNT,RUNE,THETA,TON | EIGEN,ENA,ETHFI,JTO,JUP,SEI,STRK,TIA,WLD,ZK,ZRO |

---

## 11. Stress Test Results

All tests use real book + ADTV slippage ($5M AUM) as baseline (SR=+1.773).

### Slippage model comparison

| Model | SR | Ann% | MDD% |
|-------|-----|------|------|
| Parametric k=0.0005 | +1.154 | +14.8% | -14.1% |
| Book snapshot, no ADTV scaling | +1.489 | +19.0% | -12.1% |
| Book + ADTV scaling (baseline) | +1.773 | +24.1% | -11.4% |

### A — Parameter sensitivity

| Test | SR | dSR | Ann% | MDD% |
|------|-----|-----|------|------|
| PCTL_TIGHT (10/15/90/85) | +1.334 | -0.439 | +20.7% | -13.5% |
| PCTL_WIDE (15/25/85/75) | +1.494 | -0.279 | +19.2% | -10.3% |
| SW_SHORTER (24w) | +1.145 | -0.628 | +17.1% | -15.3% |
| SW_LONGER (40w) | +1.542 | -0.231 | +28.3% | -7.4% |
| BANDS_WIDER (1.10/0.90) | +1.400 | -0.373 | +17.6% | -11.4% |
| BANDS_NARROW (1.02/0.98) | +1.837 | +0.064 | +25.0% | -11.4% |

All variants positive. Worst case (24w window): SR +1.15, Ann +17%.

### B — Token concentration

| Test | SR | dSR | Ann% | MDD% |
|------|-----|-----|------|------|
| Excl ZEC | +1.112 | -0.661 | +18.3% | -18.0% |
| Excl NEO | +1.803 | +0.030 | +25.5% | -11.5% |
| Excl NEO + ZEC | +1.104 | -0.669 | +19.0% | -18.8% |
| Excl entire persistent core (top-5 longs) | +1.848 | +0.075 | +24.8% | -9.5% |
| Static fixed basket (signal-free) | -0.275 | -2.048 | -7.2% | -31.2% |

Key findings: NEO is not driving returns (excluding it improves slightly). ZEC is the only real concentration risk (-0.66 SR). Static portfolio loses money — the signal is doing genuine work.

### C — Risk controls

| Test | SR | dSR | Ann% | MDD% |
|------|-----|-----|------|------|
| Circuit breaker OFF | +0.709 | -1.064 | +6.0% | -14.3% |
| Altseason veto OFF | +1.671 | -0.102 | +20.3% | -11.4% |
| Squeeze exclusion OFF | +1.613 | -0.160 | +22.8% | -11.0% |
| All controls OFF | +0.625 | -1.148 | +6.4% | -13.9% |

Circuit breaker is the single most impactful component (-1.06 SR when removed).

### D — IS/OOS regime split

| Period | SR | Ann% | MDD% |
|--------|-----|------|------|
| IS only: 2022-2023 (bear market) | +1.934 | +27.2% | -4.3% |
| OOS only: 2024-2026 (bull market) | +1.631 | +22.7% | -11.4% |

Both periods independently profitable. Gap is narrow (+0.30 SR).

### Circuit breaker sensitivity (ZEC excluded, algo execution)

| CB threshold | SR | Ann% | MDD% |
|---|-----|------|------|
| CB=40% (backtest assumption) | +1.112 | +18.3% | -18.0% |
| CB=50% (realistic algo) | +0.838 | +11.6% | -18.0% |
| CB=60% | +0.637 | +7.6% | -18.0% |
| CB=80% | +0.282 | +2.5% | -18.0% |
| CB=OFF | +0.129 | +0.7% | -19.3% |

Realistic live estimate with algo execution at ~50% CB: **SR +0.84, Ann +12%**.

---

## 12. Version History

| Version | Ann% | MaxDD | Sharpe (HAC) | Key change |
|---------|------|-------|-------------|-----------|
| v4 | -5.1% | -22.9% | -0.222 | Base; sideways = full exposure |
| v6 | +0.1% | -19.2% | +0.003 | Sideways = cash; bi-monthly rebal |
| v8 | +13.0% | -14.5% | +0.765 | Monthly always; 26w+52w signal |
| v9 | +14.8% | -14.1% | +1.154 | 32w+52w; no vetoes; MIN_SUPPLY_HISTORY fixed |

---

## 13. Known Limitations

- **CMC lookahead bias:** Historical supply data is retroactively revised. Results depend on point-in-time accuracy which cannot be fully verified without a dedicated PIT feed.
- **ZEC concentration:** One token's Sep-2025 narrative event (+242%) accounts for ~37% of Sharpe. Excluding ZEC: SR +1.11, Ann +18%.
- **Circuit breaker dependency:** -1.06 SR when removed. Assumes algo can exit at ~40-50% loss during squeezes; in thin markets this may slip to 50-60%.
- **Single order-book snapshot:** Slippage reference is March 2026. ADTV scaling partially corrects for historical liquidity but is not a substitute for historical book data.
- **Multiple-testing:** ~15 free parameters, 44 observations. Strategy does not pass Bailey-Lopez de Prado DSR correction at 5% significance.
- **Regime-correlated IS/OOS:** IS=bear, OOS=bull. Not a random split, though both periods are individually profitable.

---

## 14. Test Scripts

| Script | Purpose |
|--------|---------|
| `fetch_orderbook_slippage.py` | Fetch live Binance order book, compute per-token slippage |
| `slippage_ac_test.py` | Validate Almgren-Chriss model vs parametric |
| `critique_tests.py` | Original 17-test methodology critique suite |
| `book_slippage_test.py` | Book slippage integration at multiple AUM levels |
| `cro_critique_tests.py` | CRO critique: parameter sensitivity, slippage stress, concentration |
| `final_tests.py` | Definitive test suite with book+ADTV slippage baseline |
| `cb_range_test.py` | Circuit breaker threshold sensitivity (40% to OFF) |
