# Supply-Dilution L/S Perpetuals — Strategy v9 Methodology

**Version:** 9 (de-overfit)
**Last updated:** 2026-03-06
**Net SR (HAC):** +1.154 | Ann: +14.8% | MaxDD: -14.1% | Periods: 44

---

## 1. Data Sources

### Signal data — CoinMarketCap historical
- File: `cmc_historical_top300_filtered_with_supply.csv`
- Coverage: 2017-01-01 to 2026-02-22 | 135,639 rows | 2,266 symbols
- Fields: weekly CMC rank, market cap, circulating supply, close price
- Purpose: supply inflation signal, universe construction, regime index

### Price & funding data — Binance USDT-M Perpetuals
- File: `binance_perp_data/weekly_ohlcv.parquet` (322 weeks x 396 symbols)
- File: `binance_perp_data/symbol_meta.csv` (onboard dates, symbol map)
- Funding: actual 8-hour rates from Binance historical API, summed per weekly period (51,346 rows)
- Purpose: trade execution prices, realistic P&L, cost attribution

### Order book slippage — Binance live snapshot
- File: `orderbook_slippage.csv` (snapshot: March 2026)
- 396 symbols, 20 depth levels, position sizes: $100K / $375K / $1M / $5M
- Fields: half-spread, VWAP market impact, available depth per token
- Calibration: median cost at $375K/token = 35 bps, consistent with parametric k=0.0005

---

## 2. Universe Construction

Applied each period before signal ranking:

1. CMC rank <= 200 and rank > 20 (excludes BTC, ETH, top 20)
2. Market cap >= $50M
3. Weekly ADTV >= $5M (Binance volume proxy)
4. >= 32 weeks of supply history (matches signal lookback)
5. Forward-fill supply gaps <= 1 period
6. Exclude: stablecoins, CEX tokens, memecoins, wrapped tokens, commodity-backed tokens
7. Token must have active Binance USDT-M perpetual listing

Typical eligible universe: ~100-150 tokens per period.

---

## 3. Signal Construction

Two-layer cross-sectional supply inflation rank:

```
inf_32w[t]  = (supply[t] - supply[t-32]) / supply[t-32]
inf_52w[t]  = (supply[t] - supply[t-52]) / supply[t-52]

Both winsorized at [2%, 98%] cross-sectionally each period.

pct_rank_32w    = cross-sectional percentile rank of inf_32w
pct_rank_52w    = cross-sectional percentile rank of inf_52w

composite_rank  = 0.50 * pct_rank_32w + 0.50 * pct_rank_52w
```

- Low composite rank = low supply inflation = **Long candidate**
- High composite rank = high supply inflation = **Short candidate**

---

## 4. Portfolio Construction

### Selection thresholds (data-driven quantiles, computed each period)

| Basket | Entry | Exit |
|--------|-------|------|
| Long | bottom 12th pct | above 18th pct |
| Short | top 12th pct from top (88th) | below 82nd pct |

Buffer bands implement stay-unless logic: existing positions are kept until they breach the exit threshold, reducing unnecessary turnover.

### Other rules
- **Weighting:** Equal-weighted within each basket
- **ADTV cap:** No single token position exceeds 20% of its daily volume
- **Min basket size:** 6 tokens required per leg; period is skipped otherwise
- **Rebalancing:** Monthly, always (no regime-conditional step skipping)

---

## 5. Regime Detection

### Altcoin index
CMC cap-weighted index of all tokens in the eligible universe (BTC/ETH excluded directly).

```
Bull      : index > 20w MA x 1.05
Bear      : index < 20w MA x 0.95
Sideways  : otherwise (between the bands)
```

### BTC volatility overlay
BTC 8-week realised vol vs 80th percentile historical threshold. High-vol flag scales down position sizes.

### Regime L/S scaling

| State | Long scale | Short scale |
|-------|-----------|------------|
| Sideways | 0.00 | 0.00 (hold cash, 0% return) |
| Bull, low-vol | 0.75 | 0.75 |
| Bull, high-vol | 0.50 | 0.25 |
| Bear, low-vol | 0.75 | 0.75 |
| Bear, high-vol | 0.50 | 0.25 |

---

## 6. Execution & Cost Model

### Fees
`2 x 0.04% taker fee` per leg, applied on the turnover fraction (new opens + full closes).

### Slippage
Parametric market-impact model per token:

```
slip[s] = k / (CMC_24h_volume[s] / market_cap[s]),   k = 0.0005
capped at 2% per token
```

Validated against live Binance order-book data (March 2026): median cost at $375K/token is 35 bps,
consistent with k=0.0005 at $5M AUM. Strategy scales to $20M AUM with SR(HAC) ~1.32.

### Funding rates
Actual Binance 8-hour rates, summed to weekly, applied per leg:
- Long leg pays funding (drag): cumulative -2.95% over backtest
- Short leg receives funding (credit): cumulative +5.26%
- Net funding: +2.31% (positive — high-emission shorts also tend to carry positive funding)

### Risk controls
- **Short circuit breaker:** Short basket gross return capped at +40% per period (5 of 44 periods triggered)
- **Altseason veto:** If >75% of altcoins beat BTC over trailing 4 weeks, skip short leg (1 of 44 triggered)

---

## 7. Net Performance (corrected, MIN_SUPPLY_HISTORY=32)

**Period:** 2022-01-02 to 2026-01-04 | **44 monthly periods**

| Series | Ann. Return | Vol | Sharpe | Sharpe* (HAC) | Sortino | MaxDD |
|--------|------------|-----|--------|---------------|---------|-------|
| Long basket (gross) | -20.5% | 95.2% | -0.215 | +0.228 | -0.259 | -69.0% |
| Short basket (gross) | -47.4% | 82.1% | -0.577 | -0.739 | -0.508 | -93.0% |
| Long leg (net) | -28.2% | 93.2% | -0.303 | -0.005 | -0.359 | -77.5% |
| Short leg (net) | -4.2% | 80.3% | -0.052 | +0.565 | -0.049 | -71.0% |
| **L/S Combined (net)** | **+14.8%** | **17.7%** | **+0.838** | **+1.154** | **+1.430** | **-14.1%** |

*Short leg net: positive = profit for short side. Sharpe* = Lo (2002) HAC-corrected.*

### Regime-conditional spread (gross)

| Regime | N | Mean spread | Win rate | Ann. geo spread |
|--------|---|------------|---------|----------------|
| Bull | 22 | +4.61% | 63.6% | +65.7% |
| Bear | 15 | +4.77% | 53.3% | +64.5% |
| Sideways | 7 | 0.00% | — | 0.00% (cash) |

### Funding attribution

| | Cumulative | Per period avg |
|-|-----------|---------------|
| Long leg (drag) | -2.95% | -0.07% |
| Short leg (credit) | +5.26% | +0.12% |
| **Net** | **+2.31%** | **+0.05%** |

### Activity summary

| Metric | Value |
|--------|-------|
| Rebalancing periods | 44 |
| Avg long basket size | 11.7 tokens |
| Avg short basket size | 10.9 tokens |
| Regime: Bull / Bear / Sideways | 22 / 15 / 7 |
| Avg effective scale (L/S) | 0.63x / 0.61x |
| Circuit breaker triggered | 5 periods |
| Altseason veto triggered | 1 period |
| Avg monthly turnover | Long 31.1% / Short 38.4% |
| Total trades | 411 (9.3/period avg) |

### Most persistent positions

**Long (by frequency over 44 periods):**

| Token | Periods | % |
|-------|---------|---|
| NEO | 43 | 98% |
| THETA | 40 | 91% |
| QNT | 35 | 80% |
| ZRX | 30 | 68% |
| IOTX | 29 | 66% |

**Short (by frequency):**

| Token | Periods | % |
|-------|---------|---|
| KAVA | 27 | 61% |
| 1INCH | 19 | 43% |
| FIL | 16 | 36% |
| OP | 16 | 36% |
| GMT | 15 | 34% |

---

## 8. Version Comparison

| Metric | v4 (base) | v6 | v8 | v9 |
|--------|----------|----|----|-----|
| Combined net (ann.) | -5.1% | +0.1% | +13.0% | +14.8% |
| MaxDD | -22.9% | -19.2% | -14.5% | -14.1% |
| Sharpe (basic) | -0.222 | +0.003 | +0.765 | +0.838 |
| Win rate (spread) | 48.0% | 52.6% | 60.0% | 50.0% |
| Mean period spread | +1.95% | +3.34% | +3.44% | +3.93% |
| Bear geo spread | +48.1% | +53.3% | +40.5% | +64.5% |
| Bull geo spread | +21.1% | +51.9% | +63.9% | +65.7% |

---

## 9. Caveats & Known Limitations

- **IS/OOS split is regime-correlated:** IS=2022-2023 (bear market), OOS=2024-2026 (bull market). The split is not random; OOS outperformance may reflect regime rather than true generalization.
- **ZEC concentration:** ZEC appears in the long basket 17/44 periods. Its Sep-2025 +242% move is a single narrative event accounting for a disproportionate share of returns. Excluding ZEC drops SR(HAC) from +1.154 to ~+0.73.
- **Multiple-testing:** With ~15 free parameters and 44 observations, the strategy does not pass the Bailey-Lopez de Prado Deflated Sharpe Ratio correction. Results should be interpreted as exploratory.
- **Single-snapshot slippage:** Order-book cost data is a one-time snapshot (March 2026). Historical liquidity in bear periods was materially thinner.
- **Equal weighting:** No concentration risk adjustment; persistent tokens (NEO 98%, THETA 91%) dominate long-leg exposure.
- **Flat fee model:** `2 x TAKER_FEE` applied uniformly rather than scaled by actual turnover fraction.
