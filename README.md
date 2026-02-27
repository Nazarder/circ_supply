# Crypto Supply Inflation Backtesting

Quantitative research into whether token supply unlocks and inflation rates predict
subsequent price performance in the top 300 cryptocurrencies.

Three progressively more rigorous backtesting engines (V1 → V3) test the same core
hypotheses against weekly CoinMarketCap snapshots spanning January 2017 to February 2026.
A standalone extreme-percentile script (`extreme_percentile.py`) provides a benchmark-free
absolute performance test of the tail ends of the supply inflation distribution.

---

## Hypotheses

| ID | Name | Question |
|----|------|----------|
| H1 | Sudden Unlocks | Do anomalous single-period supply spikes predict negative abnormal returns over the following 2–4 weeks? |
| H2 | 90-Day Pressure | Do tokens in the top quartile of 13-week trailing supply growth underperform the bottom quartile? |
| H3 | 365-Day Pressure | Same question using 52-week trailing supply growth. |

---

## Dataset

| File | Description |
|------|-------------|
| `CMC.xlsx` / `CMC.csv` | Raw CoinMarketCap historical export |
| `cmc_historical_top300_filtered.csv` | Filtered to rows with valid price and market cap |
| `cmc_historical_top300_filtered_with_supply.csv` | Main dataset — adds derived `circulating_supply` column |
| `circulating_supply.csv` | Standalone derived supply series |

**Coverage:** Top 300 cryptocurrencies, weekly snapshots, 2017-01-01 to 2026-02-22
**Rows:** 135,652 after filtering
**Symbols:** 2,267 unique tokens
**Periods:** 477 weekly snapshots
**Columns:** `snapshot_date`, `rank`, `name`, `symbol`, `market_cap`, `price`,
`circulating_supply`, `volume_24h`, `pct_1h`, `pct_24h`, `pct_7d`

> **Note:** Survivorship bias is not corrected. Tokens that dropped below rank 300
> have no data after exit.

---

## Pipeline

```
CMC.xlsx
   └── circulating_supply.py
           └── cmc_historical_top300_filtered_with_supply.csv
                   ├── backtest.py            (V1)
                   ├── backtest_v2.py         (V2)
                   ├── backtest_v3.py         (V3)
                   └── extreme_percentile.py  (decile absolute test)
```

### 1. Derive circulating supply

```bash
python circulating_supply.py
```

Computes `circulating_supply = market_cap / price` for rows where price > 0,
writes `cmc_historical_top300_filtered_with_supply.csv`.

### 2. Run backtests

```bash
python backtest.py            # V1 -- baseline
python backtest_v2.py         # V2 -- institutional-grade
python backtest_v3.py         # V3 -- regime-conditional L/S
python extreme_percentile.py  # decile absolute basket test
```

Each script is self-contained and reads directly from the CSV. Output charts are
saved to the working directory.

---

## Methodology Evolution

### V1 — `backtest.py`

Proof-of-concept implementation.

- **Signal:** Static 3% absolute spike above 12-period rolling median
- **Outlier handling:** Hard clip ±100% on single-period returns
- **Abnormal return:** Raw excess return (`R_token − R_index`)
- **H2/H3 structure:** Two separate equal-weight long-only portfolios (Q1 vs Q4)
- **Annualization:** `cum^(52/n)` — contains a bug treating monthly periods as weekly

### V2 — `backtest_v2.py`

Addresses all V1 methodological weaknesses.

| Upgrade | Detail |
|---------|--------|
| Signal | Z-score > +3.0 vs 12-period rolling mean/std (per-token adaptive threshold) |
| Outlier handling | Cross-sectional Winsorization at 1st/99th percentile per snapshot date |
| Abnormal return | Beta-adjusted: `R_token − (β × R_index)`, trailing 12-period OLS β |
| Slippage | Inverse-turnover drag: `0.05% / turnover`, capped at 200 bps |
| H2/H3 structure | Dollar-neutral L/S with inverse-volatility weighting |
| Annualization | `cum^(1/years)` using actual elapsed calendar days |
| Regime context | Bull/Bear classification via 20-week MA of cap-weighted index price |

### V3 — `backtest_v3.py`

Adds regime-conditional L/S variants for H2/H3, motivated by V2's H1 finding that
the supply-dilution ACAR is negative only in Bear markets (−2.49%) and positive in
Bull markets (+6.33%).

| Variant | Bear period | Bull period |
|---------|-------------|-------------|
| Bear-Only | Long Q1 / Short Q4 | Cash (0%) |
| Bull-Reverse | Cash (0%) | Long Q4 / Short Q1 |
| Regime-Switch | Long Q1 / Short Q4 | Long Q4 / Short Q1 |

### Extreme Percentile — `extreme_percentile.py`

Benchmark-free absolute test using the tail ends of the 13-week trailing supply
inflation distribution. No index, no beta-hedging, no L/S — two separate equal-weight
long-only baskets rebalanced monthly.

| Parameter | Value |
|-----------|-------|
| Low basket | Tokens at or below 10th percentile of supply inflation |
| High basket | Tokens at or above 90th percentile of supply inflation |
| Avg basket size | ~23 tokens each |
| Rebalancing | Monthly (first snapshot of each calendar month) |
| Forward return | 4 weeks, cross-sectional winsorized, slippage-adjusted |

---

## Results Summary

### H1 — Event Study

| Version | Events | Method | Post-event ACAR | t-stat | p-value |
|---------|--------|--------|-----------------|--------|---------|
| V1 | 10,403 | Raw excess return | Negative | −6.42 | ≈0 |
| V2/V3 | 3,713 | Beta-adjusted | +2.47% (all) | +2.01 | 0.044 |
| V2/V3 Bull | 2,085 | Beta-adjusted | +6.33% | — | — |
| V2/V3 Bear | 1,628 | Beta-adjusted | −2.49% | — | — |

**Key finding:** The supply-dilution hypothesis holds conditionally in Bear markets only.
In Bull markets, Z-score unlock events are positively associated with beta-adjusted returns
(momentum and project-activity effects dominate dilution).

### H2/H3 — L/S Portfolio

| Portfolio | Ann. Return | Volatility | Sharpe | Max Drawdown |
|-----------|-------------|------------|--------|--------------|
| V2 Unconditional L/S (H2) | N/A | 63.40% | N/A | −100% |
| V2 Unconditional L/S (H3) | N/A | 62.78% | N/A | −100% |
| V3 Bear-Only (H2) | −0.12% | 16.82% | −0.007 | −42.62% |
| V3 Bear-Only (H3) | −0.09% | 20.93% | −0.004 | −38.26% |
| V3 Bull-Reverse (H2) | N/A | 81.19% | N/A | −100% |
| V3 Bull-Reverse (H3) | −17.65% | 61.89% | −0.285 | −94.00% |
| V3 Regime-Switch (H2) | N/A | 82.91% | N/A | −100% |
| V3 Regime-Switch (H3) | −17.72% | 65.31% | −0.271 | −95.31% |
| Index (H2) | +111.06% | 113.95% | 0.975 | −80.39% |
| Index (H3) | +34.06% | 74.59% | 0.457 | −80.39% |

**Key finding:** H2/H3 are rejected across all strategy variants. Regime-gating (Bear-Only)
prevents capital ruin and reduces volatility 4×, but produces near-zero return after
slippage — the supply quartile spread is not exploitable at the 4-week horizon.

### Extreme Percentile — Absolute Basket Test

| Basket | Ann. Return | Volatility | MaxDD |
|--------|-------------|------------|-------|
| 10th Pct (Low Inflation) | +15.20% | 144.63% | -96.08% |
| 90th Pct (High Inflation) | -7.99% | 147.69% | -97.58% |

**Spread (Low minus High)**

| Metric | Value |
|--------|-------|
| Mean per-period spread | +1.74% |
| Win rate (Low > High) | 63 / 106 periods (59.4%) |
| Spread annualized vol | 70.69% |

**Key finding:** The supply-dilution hypothesis holds in absolute terms at the decile
level. Low-inflation tokens outperform high-inflation tokens by ~23 percentage points
annualized (+15.20% vs -7.99%) with no benchmark or beta adjustment. Low beats High
in 59.4% of monthly periods. The quartile-level L/S in V2/V3 failed not because the
effect is absent, but because the middle of the distribution dilutes it — the signal
lives in the tails.

---

## Output Charts

| File | Description |
|------|-------------|
| `h1_event_study.png` | V1 H1 event study ACAR ±95% CI |
| `h2_continuous_pressure_90d.png` | V1 H2 Q1 vs Q4 cumulative return |
| `h3_continuous_pressure_365d.png` | V1 H3 Q1 vs Q4 cumulative return |
| `v2_h1_event_study.png` | V2 H1 beta-hedged ACAR ±95% CI |
| `v2_h1_bull_bear.png` | V2 H1 ACAR split by Bull vs Bear regime |
| `v2_h2_longshort.png` | V2 H2 dollar-neutral L/S vs index |
| `v2_h3_longshort.png` | V2 H3 dollar-neutral L/S vs index |
| `v3_h1_event_study.png` | V3 H1 (identical to V2) |
| `v3_h1_bull_bear.png` | V3 H1 regime split (identical to V2) |
| `v3_h2_regime_ls.png` | V3 H2 all regime-conditional variants vs index |
| `v3_h3_regime_ls.png` | V3 H3 all regime-conditional variants vs index |
| `extreme_pct_cumulative.png` | 10th vs 90th pct cumulative wealth curves + period spread bar chart |

---

## Reports

| File | Description |
|------|-------------|
| `backtest_report.md` | V1 results and methodology |
| `quant_critique_and_roadmap.md` | Critique of V1 and roadmap for V2/V3 |
| `v2_backtest_report.md` | Full quantitative report: V2 methodology, V1 vs V2 comparison, V3 regime-conditional extension, raw script output |
| `extreme_percentile.py` | Standalone decile basket script (no benchmark) |

---

## Requirements

```
python >= 3.9
pandas
numpy
scipy
matplotlib
```

Install with:

```bash
pip install pandas numpy scipy matplotlib
```

---

## Conclusion

Supply dynamics have a detectable effect on crypto token returns, but its exploitability
depends sharply on where in the distribution you look:

- **Present** at the individual event level (H1 Z-score, bear markets, ACAR = -2.49%)
- **Present** at the distribution tails (10th vs 90th pct: +15.20% vs -7.99% absolute, 59.4% win rate)
- **Absent** at the cross-sectional quartile level (H2/H3 L/S, any regime, any strategy variant)

The signal lives in the extremes. Middle-of-the-distribution tokens dilute the effect
to noise. A practitioner seeking to exploit supply dynamics has two defensible approaches:

1. **Event-driven:** Trade Z-score > 3.0 unlock events in bear-market regimes (H1)
2. **Structural basket:** Long the 10th percentile (lowest inflation), avoid or short the
   90th percentile (highest inflation), equal-weighted, monthly rebalanced
